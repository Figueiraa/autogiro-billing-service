"""Montagem do serviço de cobrança com os adaptadores configurados."""

from datetime import timedelta

from app.application.ports.payment_gateway import PaymentGatewayPort
from app.application.ports.unit_of_work import UnitOfWorkFactory
from app.application.use_cases.billing import BillingPolicy, BillingService
from app.infrastructure.config import settings
from app.infrastructure.gateways.mercadopago import DisabledPaymentGateway, MercadoPagoGateway
from app.infrastructure.notifications.notifier import build_notifier


def build_gateway() -> PaymentGatewayPort:
    if not settings.MERCADOPAGO_ACCESS_TOKEN:
        return DisabledPaymentGateway()
    return MercadoPagoGateway(
        settings.MERCADOPAGO_ACCESS_TOKEN,
        base_url=settings.MERCADOPAGO_BASE_URL,
        checkout_link_field=settings.MERCADOPAGO_CHECKOUT_LINK,
        timeout_seconds=settings.MERCADOPAGO_TIMEOUT_SECONDS,
    )


def build_policy() -> BillingPolicy:
    return BillingPolicy(
        approval_timeout=timedelta(minutes=settings.APPROVAL_TIMEOUT_MINUTES),
        payment_timeout=timedelta(minutes=settings.PAYMENT_TIMEOUT_MINUTES),
        late_payment_window=timedelta(hours=settings.LATE_PAYMENT_WINDOW_HOURS),
    )


def build_billing(
    uow_factory: UnitOfWorkFactory, gateway: PaymentGatewayPort | None = None
) -> BillingService:
    return BillingService(uow_factory, gateway or build_gateway(), build_notifier(), build_policy())
