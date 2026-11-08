"""Ponto de entrada do Billing Service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Importa os modelos para registrar todas as tabelas no metadata do SQLAlchemy.
import app.infrastructure.persistence.models  # noqa: F401
from app.infrastructure.composition import build_billing
from app.infrastructure.config import settings
from app.infrastructure.database import AsyncSessionLocal, database_is_up
from app.infrastructure.logging_config import setup_logging
from app.infrastructure.messaging.runtime import MessagingRuntime
from app.infrastructure.observability.metrics import set_app_info
from app.infrastructure.persistence.repositories import unit_of_work_factory
from app.interfaces.http.controllers import billing_controller, health
from app.interfaces.http.exception_handlers import register_exception_handlers
from app.interfaces.http.middleware import (
    MetricsMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)

API_V1 = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Monta o serviço de cobrança e, quando o RabbitMQ está configurado, sobe a mensageria."""
    app.state.billing = build_billing(unit_of_work_factory(AsyncSessionLocal))
    app.state.readiness_checks["database"] = database_is_up
    runtime = None
    if settings.RABBITMQ_URL:
        runtime = MessagingRuntime(AsyncSessionLocal, app.state.billing)
        await runtime.start()
        app.state.readiness_checks["broker"] = runtime.is_up
    yield
    if runtime is not None:
        await runtime.stop()


def create_app() -> FastAPI:
    setup_logging(settings.LOG_LEVEL, service=settings.SERVICE_NAME)

    app = FastAPI(
        title="AutoGiro · Billing Service",
        description="Cobrança: orçamentos, pagamentos com Mercado Pago e estornos.",
        version=settings.SERVICE_VERSION,
        lifespan=lifespan,
    )
    # Verificações de prontidão (banco, broker), registradas por cada adapter.
    app.state.readiness_checks = {}

    set_app_info(name=settings.SERVICE_NAME, version=settings.SERVICE_VERSION)

    # Middlewares (o último adicionado é o mais externo).
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=settings.cors_origins_list != ["*"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(billing_controller.budgets, prefix=API_V1)
    app.include_router(billing_controller.payments, prefix=API_V1)
    return app


app = create_app()
