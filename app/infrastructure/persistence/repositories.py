"""Adaptadores do PostgreSQL e unidade de trabalho do Billing."""

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.application.messages import Envelope
from app.domain.entities.budget import Budget, BudgetLine, BudgetStatus
from app.domain.entities.payment import Payment, PaymentStatus
from app.infrastructure.observability.tracing import dump_context
from app.infrastructure.persistence.models import (
    BudgetLineModel,
    BudgetModel,
    OutboxMessage,
    PaymentModel,
    ProcessedMessage,
)


def _money(value) -> float | None:
    return float(value) if value is not None else None


# ─── Orçamentos ─────────────────────────────────────────────────────────────


def _budget_from(model: BudgetModel) -> Budget:
    return Budget(
        id=model.id,
        order_id=model.order_id,
        saga_id=model.saga_id,
        client_id=model.client_id,
        client_name=model.client_name,
        client_email=model.client_email,
        status=model.status,
        created_at=model.created_at,
        expires_at=model.expires_at,
        decided_at=model.decided_at,
        lines=[
            BudgetLine(line.kind, line.reference_id, line.description, line.quantity, float(line.unit_price))
            for line in model.lines
        ],
    )


class SqlBudgetRepository:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def _model(self, order_id: int) -> BudgetModel | None:
        result = await self._db.execute(
            select(BudgetModel)
            .options(selectinload(BudgetModel.lines))
            .where(BudgetModel.order_id == order_id)
            # Aprovação, cancelamento e expiração simultâneos do mesmo orçamento: um espera
            # o outro. `populate_existing` traz o status gravado por quem segurava a trava,
            # e não o que já estava carregado na sessão.
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_by_order(self, order_id: int) -> Budget | None:
        model = await self._model(order_id)
        return _budget_from(model) if model else None

    async def add(self, budget: Budget) -> Budget:
        model = BudgetModel(
            order_id=budget.order_id,
            saga_id=budget.saga_id,
            client_id=budget.client_id,
            client_name=budget.client_name,
            client_email=budget.client_email,
            total=budget.total,
            status=budget.status,
            created_at=budget.created_at,
            expires_at=budget.expires_at,
            lines=[
                BudgetLineModel(
                    kind=line.kind,
                    reference_id=line.reference_id,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                )
                for line in budget.lines
            ],
        )
        self._db.add(model)
        await self._db.flush()
        budget.id = model.id
        return budget

    async def save(self, budget: Budget) -> None:
        model = await self._model(budget.order_id)
        model.status = budget.status
        model.decided_at = budget.decided_at
        await self._db.flush()

    async def list_expired_pending(self, now: datetime) -> list[Budget]:
        # Leitura sem trava: só aponta os candidatos. Quem expira relê cada um travado.
        result = await self._db.execute(
            select(BudgetModel)
            .options(selectinload(BudgetModel.lines))
            .where(BudgetModel.status == BudgetStatus.PENDENTE, BudgetModel.expires_at <= now)
        )
        return [_budget_from(model) for model in result.scalars().all()]


# ─── Pagamentos ─────────────────────────────────────────────────────────────


_PAYMENT_FIELDS = (
    "budget_id",
    "order_id",
    "saga_id",
    "status",
    "external_reference",
    "mp_preference_id",
    "payment_link",
    "mp_payment_id",
    "status_detail",
    "refund_id",
    "refund_retryable",
    "expires_at",
)


def _payment_from(model: PaymentModel) -> Payment:
    return Payment(
        id=model.id,
        amount=float(model.amount),
        created_at=model.created_at,
        updated_at=model.updated_at,
        **{name: getattr(model, name) for name in _PAYMENT_FIELDS},
    )


class SqlPaymentRepository:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def _model_by_id(self, payment_id: int) -> PaymentModel:
        result = await self._db.execute(
            select(PaymentModel)
            .where(PaymentModel.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one()

    async def get_by_order(self, order_id: int) -> Payment | None:
        result = await self._db.execute(
            select(PaymentModel)
            .where(PaymentModel.order_id == order_id)
            .order_by(PaymentModel.id.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        model = result.scalar_one_or_none()
        return _payment_from(model) if model else None

    async def add(self, payment: Payment) -> Payment:
        model = PaymentModel(
            amount=payment.amount, **{name: getattr(payment, name) for name in _PAYMENT_FIELDS}
        )
        self._db.add(model)
        await self._db.flush()
        payment.id = model.id
        payment.created_at = model.created_at
        return payment

    async def save(self, payment: Payment) -> None:
        model = await self._model_by_id(payment.id)
        for name in _PAYMENT_FIELDS:
            setattr(model, name, getattr(payment, name))
        if payment.updated_at is not None:
            model.updated_at = payment.updated_at
        await self._db.flush()

    async def list_to_watch(self, late_since: datetime) -> list[Payment]:
        result = await self._db.execute(
            select(PaymentModel)
            .where(
                or_(
                    PaymentModel.status.in_([PaymentStatus.PENDENTE, PaymentStatus.ESTORNO_PENDENTE]),
                    PaymentModel.status.in_(
                        [PaymentStatus.RECUSADO, PaymentStatus.EXPIRADO, PaymentStatus.CANCELADO]
                    )
                    & (PaymentModel.updated_at >= late_since),
                )
            )
            .order_by(PaymentModel.id)
        )
        return [_payment_from(model) for model in result.scalars().all()]


# ─── Outbox, mensagens processadas e unidade de trabalho ────────────────────


class SqlOutbox:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def add(self, envelope: Envelope) -> None:
        self._db.add(
            OutboxMessage(
                message_id=envelope.message_id,
                message_type=envelope.type,
                exchange=envelope.exchange,
                routing_key=envelope.routing_key,
                body=envelope.to_json(),
                trace_context=dump_context(),
            )
        )

    async def next_pending(self) -> OutboxMessage | None:
        """O evento pendente mais antigo que nenhuma outra réplica está publicando.

        A trava dura só até o commit da transação. Por isso é um evento por transação:
        travar um lote e confirmar item a item soltaria os demais na primeira confirmação,
        e outra réplica poderia publicá-los junto.
        """
        result = await self._db.execute(
            select(OutboxMessage)
            .where(OutboxMessage.published_at.is_(None))
            .order_by(OutboxMessage.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return result.scalar_one_or_none()


class SqlInbox:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def register(self, message_id: str) -> bool:
        if await self._db.get(ProcessedMessage, message_id) is not None:
            return False
        self._db.add(ProcessedMessage(message_id=message_id))
        await self._db.flush()
        return True


class SqlUnitOfWork:
    """Uma transação do PostgreSQL: confirma se o bloco termina bem, desfaz se der erro."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def __aenter__(self) -> "SqlUnitOfWork":
        self._session = self._session_factory()
        self.budgets = SqlBudgetRepository(self._session)
        self.payments = SqlPaymentRepository(self._session)
        self.outbox = SqlOutbox(self._session)
        self.inbox = SqlInbox(self._session)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()


def unit_of_work_factory(session_factory: async_sessionmaker[AsyncSession]):
    return lambda: SqlUnitOfWork(session_factory)
