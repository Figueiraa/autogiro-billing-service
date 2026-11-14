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
    # O estorno não saiu: o monitor tenta de novo (falha temporária) ou espera o
    # estorno manual (recusa definitiva) e o concilia depois.
    ESTORNO_PENDENTE = "ESTORNO_PENDENTE"


S = PaymentStatus

_TRANSITIONS: dict[PaymentStatus, set[PaymentStatus]] = {
    # Recusado, expirado ou cancelado ainda pode virar aprovado: o cliente pagou no
    # último instante (ou tentou outro cartão) e a confirmação chegou depois. A Saga
    # estorna esse pagamento tardio.
    S.PENDENTE: {S.APROVADO, S.RECUSADO, S.EXPIRADO, S.CANCELADO},
    S.RECUSADO: {S.APROVADO},
    S.EXPIRADO: {S.APROVADO},
    S.CANCELADO: {S.APROVADO},
    S.APROVADO: {S.ESTORNADO, S.ESTORNO_PENDENTE},
    S.ESTORNO_PENDENTE: {S.ESTORNADO},
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
    # Estorno pendente por falha temporária: o monitor tenta de novo. Recusa
    # definitiva fica False, para não repetir a chamada que sempre falha.
    refund_retryable: bool = False
    expires_at: datetime | None = None
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = field(default=None)

    @property
    def rejected_attempt(self) -> bool:
        """Houve tentativa recusada e o prazo ainda corre: o cliente pode tentar outro cartão."""
        return self.status == S.PENDENTE and self.mp_payment_id is not None

    def confirm(
        self, mp_payment_id: str, now: datetime | None = None, status_detail: str | None = None
    ) -> None:
        self._move(S.APROVADO, now)
        self.mp_payment_id = mp_payment_id
        self.status_detail = status_detail

    def record_rejection(
        self, mp_payment_id: str, status_detail: str | None, now: datetime | None = None
    ) -> None:
        """Tentativa recusada no checkout. Não encerra o pagamento: o link segue aberto até o prazo."""
        if self.status != S.PENDENTE:
            raise InvalidStatusTransitionError("Pagamento", self.status.value, S.PENDENTE.value)
        self.mp_payment_id = mp_payment_id
        self.status_detail = status_detail
        self.updated_at = now or datetime.now(UTC)

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
        self.refund_retryable = False

    def refund_pending(self, reason: str, now: datetime | None = None, *, retryable: bool = False) -> None:
        """Estorno que não saiu. Uma nova falha de quem já está pendente só atualiza o motivo."""
        if self.status == S.ESTORNO_PENDENTE:
            self.updated_at = now or datetime.now(UTC)
        else:
            self._move(S.ESTORNO_PENDENTE, now)
        self.status_detail = reason[:100]
        self.refund_retryable = retryable

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
