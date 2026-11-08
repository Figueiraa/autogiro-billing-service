"""Envelope e rotas das mensagens trocadas pelo RabbitMQ.

Todo comando e todo evento têm o mesmo envelope JSON. Comandos vão para o
exchange `autogiro.commands` (direct), com routing key `<serviço>.<comando>`;
eventos, para `autogiro.events` (topic), com `<produtor>.<evento>`.
"""

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

EXCHANGE_COMMANDS = "autogiro.commands"
EXCHANGE_EVENTS = "autogiro.events"

PRODUCER = "billing-service"
MESSAGE_VERSION = 1

# Serviço que executa cada comando enviado pelo orquestrador.
COMMAND_TARGET: dict[str, str] = {
    "ReservarPecas": "execution",
    "LiberarPecas": "execution",
    "EnfileirarExecucao": "execution",
    "GerarOrcamento": "billing",
    "CancelarCobranca": "billing",
}


class InvalidMessageError(ValueError):
    """Mensagem que não segue o envelope: vai direto para a DLQ."""


def kebab(name: str) -> str:
    """`ReservarPecas` → `reservar-pecas`."""
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def command_routing_key(command_type: str) -> str:
    return f"{COMMAND_TARGET[command_type]}.{kebab(command_type)}"


@dataclass(frozen=True)
class Envelope:
    type: str
    kind: str
    saga_id: str
    order_id: int
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: int = MESSAGE_VERSION
    causation_id: str | None = None
    occurred_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    producer: str = PRODUCER

    @classmethod
    def command(
        cls,
        command_type: str,
        *,
        saga_id: str,
        order_id: int,
        payload: dict[str, Any],
        causation_id: str | None = None,
    ) -> "Envelope":
        if command_type not in COMMAND_TARGET:
            raise ValueError(f"Comando desconhecido: {command_type}")
        return cls(
            type=command_type,
            kind="command",
            saga_id=saga_id,
            order_id=order_id,
            payload=payload,
            causation_id=causation_id,
        )

    @classmethod
    def event(
        cls,
        event_type: str,
        *,
        saga_id: str,
        order_id: int,
        payload: dict[str, Any],
        causation_id: str | None = None,
    ) -> "Envelope":
        return cls(
            type=event_type,
            kind="event",
            saga_id=saga_id,
            order_id=order_id,
            payload=payload,
            causation_id=causation_id,
        )

    @property
    def routing_key(self) -> str:
        if self.kind == "command":
            return command_routing_key(self.type)
        return f"{self.producer.removesuffix('-service')}.{kebab(self.type)}"

    @property
    def exchange(self) -> str:
        return EXCHANGE_COMMANDS if self.kind == "command" else EXCHANGE_EVENTS

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: bytes | str) -> "Envelope":
        try:
            data = json.loads(raw)
            envelope = cls(
                type=str(data["type"]),
                kind=str(data["kind"]),
                saga_id=str(data["saga_id"]),
                order_id=int(data["order_id"]),
                payload=dict(data.get("payload") or {}),
                message_id=str(data["message_id"]),
                version=int(data.get("version", MESSAGE_VERSION)),
                causation_id=data.get("causation_id"),
                occurred_at=str(data.get("occurred_at", "")),
                producer=str(data.get("producer", "")),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidMessageError(f"Envelope inválido: {exc}") from exc

        if envelope.version > MESSAGE_VERSION:
            raise InvalidMessageError(f"Versão {envelope.version} não suportada")
        if envelope.kind not in ("command", "event"):
            raise InvalidMessageError(f"Tipo de mensagem inválido: {envelope.kind}")
        return envelope
