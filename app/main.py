"""Ponto de entrada do Billing Service."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.infrastructure.config import settings
from app.infrastructure.logging_config import setup_logging
from app.infrastructure.observability.metrics import set_app_info
from app.interfaces.http.controllers import health
from app.interfaces.http.exception_handlers import register_exception_handlers
from app.interfaces.http.middleware import (
    MetricsMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)

API_V1 = "/api/v1"


def create_app() -> FastAPI:
    setup_logging(settings.LOG_LEVEL, service=settings.SERVICE_NAME)

    app = FastAPI(
        title="AutoGiro · Billing Service",
        description="Cobrança: orçamentos, pagamentos com Mercado Pago e estornos.",
        version=settings.SERVICE_VERSION,
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
    return app


app = create_app()
