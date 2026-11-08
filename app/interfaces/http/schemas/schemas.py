from datetime import datetime

from pydantic import BaseModel

from app.domain.entities.budget import Budget, BudgetStatus, LineKind
from app.domain.entities.payment import Payment, PaymentStatus


class BudgetLineResponse(BaseModel):
    kind: LineKind
    reference_id: int
    description: str
    quantity: int
    unit_price: float
    subtotal: float


class BudgetResponse(BaseModel):
    budget_id: int
    order_id: int
    status: BudgetStatus
    total: float
    client_name: str | None
    created_at: datetime | None
    expires_at: datetime | None
    decided_at: datetime | None
    lines: list[BudgetLineResponse]

    @classmethod
    def from_domain(cls, budget: Budget) -> "BudgetResponse":
        return cls(
            budget_id=budget.id,
            order_id=budget.order_id,
            status=budget.status,
            total=budget.total,
            client_name=budget.client_name,
            created_at=budget.created_at,
            expires_at=budget.expires_at,
            decided_at=budget.decided_at,
            lines=[
                BudgetLineResponse(
                    kind=line.kind,
                    reference_id=line.reference_id,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    subtotal=line.subtotal,
                )
                for line in budget.lines
            ],
        )


class PaymentResponse(BaseModel):
    payment_id: int
    order_id: int
    status: PaymentStatus
    amount: float
    payment_link: str | None
    mp_payment_id: str | None
    status_detail: str | None
    refund_id: str | None
    expires_at: datetime | None

    @classmethod
    def from_domain(cls, payment: Payment) -> "PaymentResponse":
        return cls(
            payment_id=payment.id,
            order_id=payment.order_id,
            status=payment.status,
            amount=payment.amount,
            payment_link=payment.payment_link,
            mp_payment_id=payment.mp_payment_id,
            status_detail=payment.status_detail,
            refund_id=payment.refund_id,
            expires_at=payment.expires_at,
        )


class ApprovalResponse(BaseModel):
    order_id: int
    budget_status: BudgetStatus
    amount: float
    payment_link: str
    payment_expires_at: datetime | None
