"""Decisão de cada entrega: concluída, nova tentativa ou DLQ."""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application.messages import Envelope
from app.infrastructure.messaging.consumer import Outcome, QueueConsumer, process
from app.infrastructure.observability import tracing

EVENTO = Envelope(type="PecasReservadas", kind="event", saga_id="s-1", order_id=7, producer="execution-service")


async def _ok(_envelope):
    return None


async def _quebra(_envelope):
    raise ConnectionError("banco fora do ar")


class TestProcess:
    async def test_processada_com_sucesso(self):
        recebidos = []

        async def handle(envelope):
            recebidos.append(envelope)

        entrega = await process(EVENTO.to_json().encode(), {}, handle, max_retries=3)

        assert entrega.outcome is Outcome.DONE
        assert recebidos == [EVENTO]

    async def test_mensagem_invalida_vai_direto_para_a_dlq(self):
        entrega = await process(b"{lixo", {}, _ok, max_retries=3)
        assert entrega.outcome is Outcome.DEAD_LETTER
        assert "Envelope inválido" in entrega.error

    async def test_falha_tecnica_tenta_de_novo(self):
        entrega = await process(EVENTO.to_json().encode(), {"x-retry-count": 1}, _quebra, max_retries=3)

        assert (entrega.outcome, entrega.retry_count) == (Outcome.RETRY, 2)
        assert entrega.error == "banco fora do ar"

    async def test_esgotadas_as_tentativas_vai_para_a_dlq(self):
        entrega = await process(EVENTO.to_json().encode(), {"x-retry-count": 3}, _quebra, max_retries=3)
        assert (entrega.outcome, entrega.retry_count) == (Outcome.DEAD_LETTER, 3)

    async def test_cabecalho_ausente_conta_como_primeira_tentativa(self):
        entrega = await process(EVENTO.to_json().encode(), {"x-retry-count": None}, _quebra, max_retries=3)
        assert entrega.retry_count == 1


class MensagemFalsa:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.body = body
        self.headers = headers or {}
        self.message_id = "m-1"
        self.type = "PecasReservadas"
        self.confirmada = False

    async def ack(self):
        self.confirmada = True


class BrokerFalso:
    def __init__(self):
        self.publicadas = []

    async def publish(self, exchange, routing_key, body, *, message_id, message_type, headers=None):
        self.publicadas.append((exchange, routing_key, headers))


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "tracer", provider.get_tracer("teste"))
    yield exporter
    provider.shutdown()


class TestQueueConsumer:
    async def test_concluida_so_confirma(self, spans):
        broker, mensagem = BrokerFalso(), MensagemFalsa(EVENTO.to_json().encode())

        await QueueConsumer(broker, "fila.teste", _ok, max_retries=3)(mensagem)

        assert mensagem.confirmada
        assert broker.publicadas == []
        (span,) = spans.get_finished_spans()
        assert span.name == "fila.teste process"
        assert span.attributes[tracing.ATTR_OUTCOME] == "done"
        assert span.attributes[tracing.ATTR_SAGA_ID] == "s-1"

    async def test_falha_tecnica_vai_para_a_fila_de_retry(self, spans):
        broker, mensagem = BrokerFalso(), MensagemFalsa(EVENTO.to_json().encode())

        await QueueConsumer(broker, "fila.teste", _quebra, max_retries=3)(mensagem)

        ((exchange, destino, headers),) = broker.publicadas
        assert (exchange, destino) == ("", "fila.teste.retry")
        assert headers["x-retry-count"] == 1
        assert headers["x-error"] == "banco fora do ar"
        assert mensagem.confirmada

    async def test_mensagem_invalida_vai_para_a_dlq_e_marca_o_span(self, spans):
        broker, mensagem = BrokerFalso(), MensagemFalsa(b"{lixo")

        await QueueConsumer(broker, "fila.teste", _ok, max_retries=3)(mensagem)

        assert broker.publicadas[0][1] == "fila.teste.dlq"
        assert mensagem.confirmada
        (span,) = spans.get_finished_spans()
        assert span.attributes[tracing.ATTR_OUTCOME] == "dead_letter"
