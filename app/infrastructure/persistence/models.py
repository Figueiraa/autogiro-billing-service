from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.entities.budget import BudgetStatus, LineKind
from app.domain.entities.payment import PaymentStatus
from app.infrastructure.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _text_enum(enum_type, length: int = 20) -> Enum:
    return Enum(enum_type, native_enum=False, length=length, validate_strings=True)


class BudgetModel(Base):
    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    saga_id: Mapped[str] = mapped_column(String(36), nullable=False)
    client_id: Mapped[int | None] = mapped_column(Integer)
    client_name: Mapped[str | None] = mapped_column(String(255))
    client_email: Mapped[str | None] = mapped_column(String(255))
    total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[BudgetStatus] = mapped_column(_text_enum(BudgetStatus), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["BudgetLineModel"]] = relationship(
        back_populates="budget", cascade="all, delete-orphan", order_by="BudgetLineModel.id"
    )


class BudgetLineModel(Base):
    __tablename__ = "budget_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[LineKind] = mapped_column(_text_enum(LineKind, 10), nullable=False)
    reference_id: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    budget: Mapped[BudgetModel] = relationship(back_populates="lines")


class PaymentModel(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    saga_id: Mapped[str] = mapped_column(String(36), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(_text_enum(PaymentStatus, 30), nullable=False)
    external_reference: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    mp_preference_id: Mapped[str | None] = mapped_column(String(100))
    payment_link: Mapped[str | None] = mapped_column(String(500))
    mp_payment_id: Mapped[str | None] = mapped_column(String(40))
    status_detail: Mapped[str | None] = mapped_column(String(100))
    refund_id: Mapped[str | None] = mapped_column(String(40))
    # Estorno pendente por falha temporária: o monitor tenta de novo.
    refund_retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class OutboxMessage(Base):
    """Evento gravado na mesma transação da mudança de estado e publicado depois."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    message_type: Mapped[str] = mapped_column(String(60), nullable=False)
    exchange: Mapped[str] = mapped_column(String(60), nullable=False)
    routing_key: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Cabeçalhos W3C (`traceparent`) do trace em que o evento foi criado.
    trace_context: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
