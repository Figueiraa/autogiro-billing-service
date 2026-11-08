"""Decisão de cada entrega: concluída, nova tentativa ou DLQ."""

from app.application.messages import Envelope
from app.infrastructure.messaging.consumer import Outcome, process

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
