from fastapi import APIRouter, Depends, Request

from app.application.use_cases.billing import BillingService
from app.infrastructure.observability.metrics import record_budget_approval
from app.interfaces.http.dependencies import get_current_principal
from app.interfaces.http.schemas.schemas import ApprovalResponse, BudgetResponse, PaymentResponse

budgets = APIRouter(prefix="/budgets", tags=["Orçamentos"], dependencies=[Depends(get_current_principal)])
payments = APIRouter(prefix="/payments", tags=["Pagamentos"], dependencies=[Depends(get_current_principal)])


def get_billing(request: Request) -> BillingService:
    return request.app.state.billing


@budgets.get("/{order_id}", response_model=BudgetResponse)
async def get_budget(order_id: int, billing: BillingService = Depends(get_billing)):
    """Orçamento da OS, com os serviços e as peças."""
    return BudgetResponse.from_domain(await billing.get_budget(order_id))


@budgets.post("/{order_id}/approve", response_model=ApprovalResponse)
async def approve(order_id: int, billing: BillingService = Depends(get_billing)):
    """Aprova o orçamento e devolve o link de pagamento (Checkout Pro do Mercado Pago).

    Se o Mercado Pago não responder, devolve 502 e o orçamento continua pendente.
    """
    budget, payment = await billing.approve(order_id)
    record_budget_approval(True)
    return ApprovalResponse(
        order_id=order_id,
        budget_status=budget.status,
        amount=payment.amount,
        payment_link=payment.payment_link,
        payment_expires_at=payment.expires_at,
    )


@budgets.post("/{order_id}/reject", response_model=BudgetResponse)
async def reject(order_id: int, billing: BillingService = Depends(get_billing)):
    """Recusa o orçamento: a Saga libera as peças e cancela a OS."""
    budget = await billing.reject(order_id)
    record_budget_approval(False)
    return BudgetResponse.from_domain(budget)


@payments.get("/{order_id}", response_model=PaymentResponse)
async def get_payment(order_id: int, billing: BillingService = Depends(get_billing)):
    return PaymentResponse.from_domain(await billing.get_payment(order_id))


@payments.post("/{order_id}/sync", response_model=PaymentResponse)
async def sync_payment(order_id: int, billing: BillingService = Depends(get_billing)):
    """Consulta o Mercado Pago agora, sem esperar o próximo ciclo de 15 s."""
    return PaymentResponse.from_domain(await billing.sync_payment(order_id))
