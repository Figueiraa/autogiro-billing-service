"""Pagamento do orçamento aprovado, feito pelo cliente no Checkout Pro do Mercado Pago."""

import enum
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain.exceptions import InvalidStatusTransitionError


class PaymentStatus(str, enum.Enum):
    PENDENTE = "PENDENTE"
    APROVADO = "APROVADO"
    RECUSADO = "RECUSADO"
    EXPIRADO = "EXPIRADO"
    CANCELADO = "CANCELADO"
    ESTORNADO = "ESTORNADO"
    # O provedor recusou o estorno: precisa ser feito manualmente e é conciliado depois.
    ESTORNO_PENDENTE = "ESTORNO_PENDENTE"


S = PaymentStatus

_TRANSITIONS: dict[PaymentStatus, set[PaymentStatus]] = {
    # Expirado ou cancelado ainda pode virar aprovado: o cliente pagou no último
    # instante e a confirmação chegou depois. A Saga estorna esse pagamento tardio.
    S.PENDENTE: {S.APROVADO, S.RECUSADO, S.EXPIRADO, S.CANCELADO},
    S.EXPIRADO: {S.APROVADO},
    S.CANCELADO: {S.APROVADO},
    S.APROVADO: {S.ESTORNADO, S.ESTORNO_PENDENTE},
    S.ESTORNO_PENDENTE: {S.ESTORNADO},
    S.RECUSADO: set(),
    S.ESTORNADO: set(),
}


def new_external_reference(order_id: int) -> str:
    """Identifica o pagamento no Mercado Pago (vai na preferência e volta na consulta)."""
    return f"autogiro-os-{order_id}-{uuid.uuid4().hex[:12]}"


@dataclass
class Payment:
    budget_id: int
    order_id: int
    saga_id: str
    amount: float
    external_reference: str = ""
    status: PaymentStatus = PaymentStatus.PENDENTE
    mp_preference_id: str | None = None
    payment_link: str | None = None
    mp_payment_id: str | None = None
    status_detail: str | None = None
    refund_id: str | None = None
    expires_at: datetime | None = None
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = field(default=None)

    def confirm(self, mp_payment_id: str, now: datetime | None = None) -> None:
        self._move(S.APROVADO, now)
        self.mp_payment_id = mp_payment_id

    def reject(self, mp_payment_id: str, status_detail: str | None, now: datetime | None = None) -> None:
        self._move(S.RECUSADO, now)
        self.mp_payment_id = mp_payment_id
        self.status_detail = status_detail

    def expire(self, now: datetime | None = None) -> None:
        self._move(S.EXPIRADO, now)

    def cancel(self, now: datetime | None = None) -> None:
        self._move(S.CANCELADO, now)

    def refund(self, refund_id: str, now: datetime | None = None) -> None:
        self._move(S.ESTORNADO, now)
        self.refund_id = refund_id

    def refund_pending(self, reason: str, now: datetime | None = None) -> None:
        self._move(S.ESTORNO_PENDENTE, now)
        self.status_detail = reason[:100]

    def is_expired(self, now: datetime) -> bool:
        return (
            self.status == S.PENDENTE
            and self.expires_at is not None
            and _aware(now) >= _aware(self.expires_at)
        )

    def _move(self, target: PaymentStatus, now: datetime | None) -> None:
        if target not in _TRANSITIONS[self.status]:
            raise InvalidStatusTransitionError("Pagamento", self.status.value, target.value)
        self.status = target
        self.updated_at = now or datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
