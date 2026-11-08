"""Orçamento de uma ordem de serviço: gerado pela Saga, decidido pelo cliente."""

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.domain.exceptions import BusinessRuleError, InvalidStatusTransitionError


class BudgetStatus(str, enum.Enum):
    PENDENTE = "PENDENTE"
    APROVADO = "APROVADO"
    RECUSADO = "RECUSADO"
    EXPIRADO = "EXPIRADO"
    CANCELADO = "CANCELADO"


class LineKind(str, enum.Enum):
    SERVICO = "SERVICO"
    PECA = "PECA"


_TRANSITIONS: dict[BudgetStatus, set[BudgetStatus]] = {
    BudgetStatus.PENDENTE: {
        BudgetStatus.APROVADO,
        BudgetStatus.RECUSADO,
        BudgetStatus.EXPIRADO,
        BudgetStatus.CANCELADO,
    },
    BudgetStatus.APROVADO: {BudgetStatus.CANCELADO},
    BudgetStatus.RECUSADO: set(),
    BudgetStatus.EXPIRADO: set(),
    BudgetStatus.CANCELADO: set(),
}


class InvalidBudgetError(BusinessRuleError):
    """O orçamento não pode ser gerado com os dados recebidos."""


@dataclass
class BudgetLine:
    kind: LineKind
    reference_id: int
    description: str
    quantity: int
    unit_price: float

    @property
    def subtotal(self) -> float:
        return round(self.quantity * self.unit_price, 2)


@dataclass
class Budget:
    order_id: int
    saga_id: str
    client_id: int
    client_name: str | None
    client_email: str | None
    lines: list[BudgetLine] = field(default_factory=list)
    status: BudgetStatus = BudgetStatus.PENDENTE
    expires_at: datetime | None = None
    id: int | None = None
    created_at: datetime | None = None
    decided_at: datetime | None = None

    @classmethod
    def generate(
        cls,
        *,
        order_id: int,
        saga_id: str,
        client: dict,
        services: list[dict],
        parts: list[dict],
        approval_timeout: timedelta,
        now: datetime | None = None,
    ) -> "Budget":
        """Monta o orçamento com os serviços e peças da OS, com os preços que vieram nela."""
        lines = [
            BudgetLine(
                LineKind.SERVICO, s["service_type_id"], s["name"], int(s["quantity"]), _price(s, "serviço")
            )
            for s in services
        ] + [
            BudgetLine(
                LineKind.PECA,
                p["part_id"],
                p.get("name") or f"Peça {p['part_id']}",
                int(p["quantity"]),
                _price(p, "peça"),
            )
            for p in parts
        ]
        if not lines:
            raise InvalidBudgetError("A ordem de serviço não tem serviços nem peças")

        moment = now or datetime.now(UTC)
        budget = cls(
            order_id=order_id,
            saga_id=saga_id,
            client_id=client.get("id"),
            client_name=client.get("name"),
            client_email=client.get("email"),
            lines=lines,
            created_at=moment,
            expires_at=moment + approval_timeout,
        )
        if budget.total <= 0:
            raise InvalidBudgetError("O total do orçamento precisa ser maior que zero")
        return budget

    @property
    def total(self) -> float:
        return round(sum(line.subtotal for line in self.lines), 2)

    def is_expired(self, now: datetime) -> bool:
        return (
            self.status == BudgetStatus.PENDENTE
            and self.expires_at is not None
            and _aware(now) >= _aware(self.expires_at)
        )

    def approve(self, now: datetime | None = None) -> None:
        self._move(BudgetStatus.APROVADO, now)

    def reject(self, now: datetime | None = None) -> None:
        self._move(BudgetStatus.RECUSADO, now)

    def expire(self, now: datetime | None = None) -> None:
        self._move(BudgetStatus.EXPIRADO, now)

    def cancel(self, now: datetime | None = None) -> bool:
        """Compensação. Devolve False quando não há o que cancelar (já recusado, expirado ou cancelado)."""
        if BudgetStatus.CANCELADO not in _TRANSITIONS[self.status]:
            return False
        self._move(BudgetStatus.CANCELADO, now)
        return True

    def _move(self, target: BudgetStatus, now: datetime | None) -> None:
        if target not in _TRANSITIONS[self.status]:
            raise InvalidStatusTransitionError("Orçamento", self.status.value, target.value)
        self.status = target
        self.decided_at = now or datetime.now(UTC)


def _price(item: dict, kind: str) -> float:
    price = item.get("unit_price")
    if price is None:
        raise InvalidBudgetError(f"Preço ausente para {kind} {item.get('name') or item}")
    return float(price)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
