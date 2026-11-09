"""Conexão com o RabbitMQ e topologia usada pelo Billing Service.

Exchanges (compartilhados pelos três serviços, declarados por todos):
    autogiro.commands  direct  — comandos do orquestrador para os participantes
    autogiro.events    topic   — eventos dos participantes

Filas do Billing Service (declaradas em messaging/runtime.py):
    billing.commands        comandos billing.gerar-orcamento e billing.cancelar-cobranca
    billing.commands.retry  espera com TTL; ao expirar, a mensagem volta para a fila principal
    billing.commands.dlq    mensagens que falharam em todas as tentativas
"""

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection

from app.application.messages import EXCHANGE_COMMANDS, EXCHANGE_EVENTS


def retry_queue(queue: str) -> str:
    return f"{queue}.retry"


def dead_letter_queue(queue: str) -> str:
    return f"{queue}.dlq"


class RabbitMQClient:
    def __init__(self, url: str, *, prefetch: int = 10, retry_delay_ms: int = 5000):
        self._url = url
        self._prefetch = prefetch
        self._retry_delay_ms = retry_delay_ms
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url)
        # Publisher confirms: o publish só retorna depois que o broker guardou a mensagem.
        self._channel = await self._connection.channel(publisher_confirms=True)
        await self._channel.set_qos(prefetch_count=self._prefetch)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    async def declare_exchanges(self) -> None:
        await self._channel.declare_exchange(EXCHANGE_COMMANDS, aio_pika.ExchangeType.DIRECT, durable=True)
        await self._channel.declare_exchange(EXCHANGE_EVENTS, aio_pika.ExchangeType.TOPIC, durable=True)

    async def declare_queue_with_retry(
        self, queue: str, exchange: str, routing_keys: tuple[str, ...]
    ) -> AbstractQueue:
        """Fila durável + fila de espera para retry (TTL) + DLQ."""
        main = await self._channel.declare_queue(queue, durable=True)
        target = await self._channel.get_exchange(exchange)
        for routing_key in routing_keys:
            await main.bind(target, routing_key=routing_key)
        await self._channel.declare_queue(
            retry_queue(queue),
            durable=True,
            arguments={
                "x-message-ttl": self._retry_delay_ms,
                # Ao expirar, volta para a fila principal pelo exchange padrão.
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": queue,
            },
        )
        await self._channel.declare_queue(dead_letter_queue(queue), durable=True)
        return main

    async def publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        *,
        message_id: str,
        message_type: str,
        headers: dict | None = None,
    ) -> None:
        message = aio_pika.Message(
            body=body,
            message_id=message_id,
            type=message_type,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers=headers or {},
        )
        target = (
            self._channel.default_exchange if exchange == "" else await self._channel.get_exchange(exchange)
        )
        await target.publish(message, routing_key=routing_key)
