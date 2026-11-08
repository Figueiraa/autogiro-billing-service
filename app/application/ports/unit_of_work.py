"""Persistência do Billing numa unidade de trabalho (uma transação no PostgreSQL).

Orçamento, pagamento, o evento a publicar (outbox) e o registro da mensagem
processada ficam valendo juntos ou nenhum.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.application.messages import Envelope
from app.domain.entities.budget import Budget
from app.domain.entities.payment import Payment


class BudgetRepositoryPort(Protocol):
    async def get_by_order(self, order_id: int) -> Budget | None: ...

    async def add(self, budget: Budget) -> Budget: ...

    async def save(self, budget: Budget) -> None: ...

    async def list_expired_pending(self, now: datetime) -> list[Budget]: ...


class PaymentRepositoryPort(Protocol):
    async def get_by_order(self, order_id: int) -> Payment | None: ...

    async def add(self, payment: Payment) -> Payment: ...

    async def save(self, payment: Payment) -> None: ...

    async def list_to_watch(self, late_since: datetime) -> list[Payment]:
        """Pendentes, e expirados ou cancelados recentes (um pagamento tardio ainda pode chegar)."""
        ...


class OutboxPort(Protocol):
    async def add(self, envelope: Envelope) -> None: ...


class InboxPort(Protocol):
    async def register(self, message_id: str) -> bool:
        """Registra a mensagem; devolve False se ela já tinha sido processada."""
        ...


class UnitOfWorkPort(Protocol):
    budgets: BudgetRepositoryPort
    payments: PaymentRepositoryPort
    outbox: OutboxPort
    inbox: InboxPort

    async def __aenter__(self) -> "UnitOfWorkPort": ...

    async def __aexit__(self, exc_type, exc, tb) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWorkPort]
