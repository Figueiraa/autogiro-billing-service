"""Migrations no PostgreSQL e mensageria no RabbitMQ, de verdade.

Rodam quando `TEST_POSTGRES_URL` e `TEST_RABBITMQ_URL` estão definidas (no CI,
containers dos dois). Sem elas, são pulados.
"""

import asyncio
import json
import os
import uuid

import aio_pika
import asyncpg
import pytest

from app.application.messages import EXCHANGE_COMMANDS, EXCHANGE_EVENTS, Envelope
from app.infrastructure import composition
from app.infrastructure.config import settings
from app.infrastructure.gateways.mercadopago import DisabledPaymentGateway, MercadoPagoGateway
from app.infrastructure.messaging.consumer import RETRY_HEADER, QueueConsumer
from app.infrastructure.messaging.rabbitmq import RabbitMQClient, dead_letter_queue
from app.infrastructure.messaging.runtime import QUEUE_COMMANDS, MessagingRuntime, publish_pending, run_every
from app.infrastructure.migrations import migrate, to_asyncpg_dsn

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
RABBITMQ_URL = os.getenv("TEST_RABBITMQ_URL")

precisa_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL não definida")
precisa_rabbitmq = pytest.mark.skipif(not RABBITMQ_URL, reason="TEST_RABBITMQ_URL não definida")


async def _esperar(fila, segundos: float = 10.0):
    for _ in range(int(segundos / 0.1)):
        mensagem = await fila.get(no_ack=True, fail=False)
        if mensagem is not None:
            return mensagem
        await asyncio.sleep(0.1)
    return None


# ─── Montagem ───────────────────────────────────────────────────────────────


class TestComposicao:
    def test_sem_token_o_mercado_pago_fica_desligado(self, monkeypatch):
        monkeypatch.setattr(settings, "MERCADOPAGO_ACCESS_TOKEN", None)
        assert isinstance(composition.build_gateway(), DisabledPaymentGateway)

    def test_com_token_usa_o_mercado_pago(self, monkeypatch):
        monkeypatch.setattr(settings, "MERCADOPAGO_ACCESS_TOKEN", "APP_USR-x")
        assert isinstance(composition.build_gateway(), MercadoPagoGateway)

    def test_prazos_vem_da_configuracao(self, monkeypatch, session_factory):
        monkeypatch.setattr(settings, "PAYMENT_TIMEOUT_MINUTES", 15)
        politica = composition.build_policy()
        assert politica.payment_timeout.total_seconds() == 900
        assert composition.build_billing(lambda: None, DisabledPaymentGateway()) is not None


# ─── PostgreSQL ─────────────────────────────────────────────────────────────


@precisa_postgres
class TestMigrations:
    @pytest.fixture
    async def banco_limpo(self):
        conn = await asyncpg.connect(to_asyncpg_dsn(POSTGRES_URL))
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        yield conn
        await conn.close()

    async def test_aplica_uma_vez_e_cria_as_tabelas(self, banco_limpo):
        assert await migrate(POSTGRES_URL) == ["001_schema_inicial.sql"]
        assert await migrate(POSTGRES_URL) == []

        tabelas = {
            r["table_name"]
            for r in await banco_limpo.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        }
        assert {"budgets", "budget_lines", "payments", "outbox", "processed_messages"} <= tabelas

    async def test_status_e_valores_invalidos_sao_recusados(self, banco_limpo):
        await migrate(POSTGRES_URL)
        with pytest.raises(asyncpg.CheckViolationError):
            await banco_limpo.execute(
                "INSERT INTO budgets (order_id, saga_id, total, status) VALUES (1, 's', 10, 'INVENTADO')"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await banco_limpo.execute(
                "INSERT INTO budgets (order_id, saga_id, total, status) VALUES (1, 's', 0, 'PENDENTE')"
            )


# ─── RabbitMQ ───────────────────────────────────────────────────────────────


@pytest.fixture
async def canal():
    conexao = await aio_pika.connect_robust(RABBITMQ_URL)
    canal = await conexao.channel()
    yield canal
    await conexao.close()


@precisa_rabbitmq
class TestMensageria:
    async def test_comando_gera_orcamento_e_o_evento_e_publicado(self, canal, session_factory, billing, monkeypatch):
        monkeypatch.setattr(settings, "OUTBOX_POLL_SECONDS", 0.1)
        monkeypatch.setattr(settings, "PAYMENT_POLL_SECONDS", 3600)
        await canal.queue_delete(QUEUE_COMMANDS)
        runtime = MessagingRuntime(session_factory, billing, RabbitMQClient(RABBITMQ_URL))
        await runtime.start()
        respostas = await canal.declare_queue(exclusive=True)
        await respostas.bind(EXCHANGE_EVENTS, routing_key="billing.#")

        comando = Envelope.command(
            "GerarOrcamento",
            saga_id="saga-9",
            order_id=9,
            payload={
                "client": {"id": 1, "name": "Maria", "email": None},
                "services": [{"service_type_id": 1, "name": "Revisão", "quantity": 1, "unit_price": 499.9}],
                "parts": [],
            },
        )
        exchange = await canal.get_exchange(EXCHANGE_COMMANDS)
        await exchange.publish(aio_pika.Message(comando.to_json().encode()), routing_key=comando.routing_key)
        mensagem = await _esperar(respostas)
        assert await runtime.is_up()
        await runtime.stop()

        assert mensagem is not None
        evento = json.loads(mensagem.body)
        assert (mensagem.type, evento["payload"]["total"], evento["causation_id"]) == (
            "OrcamentoGerado",
            499.9,
            comando.message_id,
        )

    async def test_falha_tecnica_passa_pelo_retry_e_termina_na_dlq(self, canal):
        fila_nome = f"teste.falhas.{uuid.uuid4().hex[:8]}"
        cliente = RabbitMQClient(RABBITMQ_URL, retry_delay_ms=200)
        await cliente.connect()
        await cliente.declare_exchanges()
        fila = await cliente.declare_queue_with_retry(fila_nome, EXCHANGE_COMMANDS, (f"{fila_nome}.x",))

        async def sempre_falha(_envelope):
            raise ConnectionError("banco fora do ar")

        await fila.consume(QueueConsumer(cliente, fila_nome, sempre_falha, max_retries=1))
        comando = Envelope.command("GerarOrcamento", saga_id="s", order_id=1, payload={})
        await cliente.publish("", fila_nome, comando.to_json().encode(), message_id=comando.message_id, message_type="X")
        dlq = await canal.declare_queue(dead_letter_queue(fila_nome), durable=True)
        mensagem = await _esperar(dlq)
        await cliente.close()
        for nome in (fila_nome, f"{fila_nome}.retry", f"{fila_nome}.dlq"):
            await canal.queue_delete(nome)

        assert mensagem is not None
        assert mensagem.headers[RETRY_HEADER] == 1

    async def test_api_sobe_com_a_mensageria(self, monkeypatch):
        from app.main import create_app, lifespan

        monkeypatch.setattr(settings, "RABBITMQ_URL", RABBITMQ_URL)
        monkeypatch.setattr(settings, "OUTBOX_POLL_SECONDS", 3600)
        monkeypatch.setattr(settings, "PAYMENT_POLL_SECONDS", 3600)
        app = create_app()

        async with lifespan(app):
            assert await app.state.readiness_checks["broker"]() is True
            assert await app.state.readiness_checks["database"]() is True


# ─── Sem broker ─────────────────────────────────────────────────────────────


class TestOutboxELaco:
    async def test_publica_cada_evento_uma_vez(self, billing, session_factory):
        await billing.handle_command(
            Envelope.command(
                "GerarOrcamento",
                saga_id="s",
                order_id=1,
                payload={"client": {}, "services": [{"service_type_id": 1, "name": "A", "quantity": 1, "unit_price": 10}]},
            )
        )
        publicadas = []

        async def publicar(exchange, routing_key, body, *, message_id, message_type, headers=None):
            publicadas.append((routing_key, message_type))

        assert await publish_pending(session_factory, publicar) == 1
        assert await publish_pending(session_factory, publicar) == 0
        assert publicadas == [("billing.orcamento-gerado", "OrcamentoGerado")]

    async def test_laco_continua_quando_a_tarefa_falha(self):
        execucoes, segunda = [], asyncio.Event()

        async def tarefa():
            execucoes.append(1)
            if len(execucoes) >= 2:
                segunda.set()
            raise RuntimeError("falha temporária")

        laco = asyncio.create_task(run_every(0.01, tarefa, "teste"))
        await asyncio.wait_for(segunda.wait(), timeout=5)
        laco.cancel()
        await asyncio.gather(laco, return_exceptions=True)
