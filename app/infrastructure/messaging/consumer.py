"""Consumo dos eventos da Saga, com retry e DLQ.

A decisão do que fazer com cada entrega (`process`) não depende do aio-pika:
recebe o corpo e os cabeçalhos e devolve o desfecho. Só falhas técnicas entram
em retry; falhas de negócio (peça indisponível, orçamento recusado) são eventos
normais que o orquestrador trata.
"""

import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aio_pika.abc import AbstractIncomingMessage

from app.application.messages import Envelope, InvalidMessageError
from app.infrastructure.messaging.rabbitmq import RabbitMQClient, dead_letter_queue, retry_queue
from app.infrastructure.observability.tracing import annotate_message, consume_span, record_outcome

logger = logging.getLogger("autogiro.messaging")

RETRY_HEADER = "x-retry-count"
ERROR_HEADER = "x-error"


class Outcome(str, enum.Enum):
    DONE = "done"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


@dataclass
class Delivery:
    outcome: Outcome
    retry_count: int = 0
    error: str | None = None


async def process(
    body: bytes,
    headers: dict,
    handle: Callable[[Envelope], Awaitable[object]],
    max_retries: int,
) -> Delivery:
    """Processa uma entrega e decide: concluída, nova tentativa ou DLQ."""
    try:
        envelope = Envelope.from_json(body)
    except InvalidMessageError as exc:
        logger.error("Mensagem inválida enviada para a DLQ: %s", exc)
        return Delivery(Outcome.DEAD_LETTER, error=str(exc))

    retry_count = int(headers.get(RETRY_HEADER, 0) or 0)
    annotate_message(
        saga_id=envelope.saga_id,
        order_id=envelope.order_id,
        message_type=envelope.type,
        retry_count=retry_count,
    )
    try:
        await handle(envelope)
    except Exception as exc:  # falha técnica: banco fora, bug, payload inesperado
        if retry_count < max_retries:
            logger.warning("Falha ao processar %s (tentativa %d): %s", envelope.type, retry_count + 1, exc)
            return Delivery(Outcome.RETRY, retry_count + 1, str(exc))
        logger.error("%s falhou %d vezes e foi para a DLQ: %s", envelope.type, retry_count + 1, exc)
        return Delivery(Outcome.DEAD_LETTER, retry_count, str(exc))
    return Delivery(Outcome.DONE, retry_count)


class QueueConsumer:
    """Liga `process` a uma fila do RabbitMQ: ack depois do processamento ou do desvio."""

    def __init__(
        self,
        client: RabbitMQClient,
        queue: str,
        handle: Callable[[Envelope], Awaitable[object]],
        max_retries: int,
    ):
        self._client = client
        self._queue = queue
        self._handle = handle
        self._max_retries = max_retries

    async def __call__(self, message: AbstractIncomingMessage) -> None:
        headers = dict(message.headers or {})
        with consume_span(self._queue, headers, message.message_id, message.type) as span:
            delivery = await process(message.body, headers, self._handle, self._max_retries)
            record_outcome(span, delivery.outcome.value, delivery.error)
            await self._divert(message, headers, delivery)
        # O ack só sai depois do commit (DONE) ou depois de a mensagem estar a salvo
        # na fila de retry ou na DLQ: nada se perde se o pod cair no meio.
        await message.ack()

    async def _divert(self, message: AbstractIncomingMessage, headers: dict, delivery: Delivery) -> None:
        """Manda a mensagem para a fila de retry ou para a DLQ, conforme o desfecho."""
        if delivery.outcome is not Outcome.DONE:
            target = (
                retry_queue(self._queue)
                if delivery.outcome is Outcome.RETRY
                else dead_letter_queue(self._queue)
            )
            headers[RETRY_HEADER] = delivery.retry_count
            if delivery.error:
                headers[ERROR_HEADER] = delivery.error[:500]
            await self._client.publish(
                "",
                target,
                message.body,
                message_id=message.message_id or "",
                message_type=message.type or "",
                headers=headers,
            )
