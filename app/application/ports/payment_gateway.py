"""Provedor de pagamento (Mercado Pago, Checkout Pro).

O Billing só conhece esta porta. Nos testes entra um dublê; em produção, o
adaptador HTTP do Mercado Pago. Trocar de API (por exemplo, de Preferences para
Orders) é reescrever o adaptador, sem tocar no orçamento nem na Saga.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

# Status do Mercado Pago que encerram a tentativa de pagamento.
APPROVED = {"approved"}
REJECTED = {"rejected", "cancelled"}
REFUNDED = {"refunded", "charged_back"}


@dataclass(frozen=True)
class Preference:
    id: str
    link: str


@dataclass(frozen=True)
class GatewayPayment:
    id: str
    status: str
    status_detail: str | None = None

    @property
    def approved(self) -> bool:
        return self.status in APPROVED

    @property
    def rejected(self) -> bool:
        return self.status in REJECTED

    @property
    def refunded(self) -> bool:
        return self.status in REFUNDED


class PaymentGatewayPort(Protocol):
    async def create_preference(
        self,
        *,
        external_reference: str,
        title: str,
        amount: float,
        payer_email: str | None,
        expires_at: datetime,
    ) -> Preference: ...

    async def find_payment(self, external_reference: str) -> GatewayPayment | None:
        """Pagamento mais recente feito para a referência, ou None se ainda não houve tentativa."""
        ...

    async def refund(self, mp_payment_id: str, idempotency_key: str) -> str:
        """Estorno total; devolve o id do estorno."""
        ...

    async def expire_preference(self, preference_id: str) -> None:
        """Impede novos pagamentos no link (melhor esforço)."""
        ...
