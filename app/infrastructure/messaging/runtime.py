"""Tarefas de mensageria que rodam junto com a API do Billing.

- consumidor da fila `billing.commands` (comandos do orquestrador);
- publicador do outbox;
- monitor de pagamentos (consulta o Mercado Pago e expira o que passou do prazo).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.messages import EXCHANGE_COMMANDS
from app.application.use_cases.billing import BillingService
from app.infrastructure.config import settings
from app.infrastructure.messaging.consumer import QueueConsumer
from app.infrastructure.messaging.rabbitmq import RabbitMQClient
from app.infrastructure.observability.tracing import load_context, publish_span
from app.infrastructure.persistence.repositories import SqlOutbox

logger = logging.getLogger("autogiro.messaging")

QUEUE_COMMANDS = "billing.commands"
COMMAND_BINDINGS = ("billing.gerar-orcamento", "billing.cancelar-cobranca")

Publish = Callable[..., Awaitable[None]]


async def publish_pending(
    session_factory: async_sessionmaker[AsyncSession], publish: Publish, limit: int = 100
) -> int:
    """Publica os eventos pendentes do outbox, na ordem em que foram gravados."""
    published = 0
    async with session_factory() as db:
        for row in await SqlOutbox(db).pending(limit):
            with publish_span(
                exchange=row.exchange,
                routing_key=row.routing_key,
                message_id=row.message_id,
                message_type=row.message_type,
                body=row.body,
                stored_context=load_context(row.trace_context),
            ) as headers:
                await publish(
                    row.exchange,
                    row.routing_key,
                    row.body.encode("utf-8"),
                    message_id=row.message_id,
                    message_type=row.message_type,
                    headers=headers,
                )
            row.published_at = datetime.now(UTC)
            await db.commit()
            published += 1
    return published


async def run_every(interval: float, job: Callable[[], Awaitable[object]], name: str) -> None:
    """Executa `job` periodicamente até a tarefa ser cancelada; erros não derrubam o laço."""
    while True:
        try:
            await job()
        except asyncio.CancelledError:  # pragma: no cover - encerramento normal
            raise
        except Exception:
            logger.exception("Falha na tarefa periódica %s", name)
        await asyncio.sleep(interval)


class MessagingRuntime:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        billing: BillingService,
        client: RabbitMQClient | None = None,
    ):
        self._session_factory = session_factory
        self._billing = billing
        self.client = client or RabbitMQClient(
            settings.RABBITMQ_URL, retry_delay_ms=settings.MESSAGE_RETRY_DELAY_MS
        )
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.client.connect()
        await self.client.declare_exchanges()
        queue = await self.client.declare_queue_with_retry(
            QUEUE_COMMANDS, EXCHANGE_COMMANDS, COMMAND_BINDINGS
        )
        await queue.consume(
            QueueConsumer(
                self.client, QUEUE_COMMANDS, self._billing.handle_command, settings.MESSAGE_MAX_RETRIES
            )
        )
        self._tasks = [
            asyncio.create_task(
                run_every(
                    settings.OUTBOX_POLL_SECONDS,
                    lambda: publish_pending(self._session_factory, self.client.publish),
                    "outbox",
                )
            ),
            asyncio.create_task(
                run_every(settings.PAYMENT_POLL_SECONDS, self._billing.run_monitor, "pagamentos")
            ),
        ]
        logger.info("Mensageria iniciada: consumindo %s", QUEUE_COMMANDS)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.close()

    async def is_up(self) -> bool:
        return self.client.is_connected
