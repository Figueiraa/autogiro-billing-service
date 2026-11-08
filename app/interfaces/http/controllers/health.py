"""Sondas do Kubernetes e exposição de métricas."""

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app.infrastructure.observability.metrics import CONTENT_TYPE_PROMETHEUS, render_latest

router = APIRouter()


@router.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}


@router.get("/health/live", tags=["Health"])
async def liveness():
    return {"status": "alive"}


@router.get("/health/ready", tags=["Health"])
async def readiness(request: Request):
    """Pronto para receber tráfego quando todas as dependências respondem.

    Cada serviço registra suas verificações (banco, broker) em
    `app.state.readiness_checks`: um dicionário nome → função assíncrona que
    devolve `True` quando a dependência está de pé.
    """
    results: dict[str, str] = {}
    for name, check in request.app.state.readiness_checks.items():
        try:
            healthy = await check()
        except Exception:
            healthy = False
        results[name] = "up" if healthy else "down"

    ready = all(state == "up" for state in results.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ready" if ready else "not_ready", "checks": results},
    )


@router.get(
    "/metrics",
    tags=["Observabilidade"],
    summary="Métricas no formato Prometheus",
    response_class=Response,
    responses={200: {"content": {CONTENT_TYPE_PROMETHEUS: {}}}},
)
async def metrics() -> Response:
    """Endpoint de scrape do Prometheus (agente do New Relic no cluster)."""
    return Response(content=render_latest(), media_type=CONTENT_TYPE_PROMETHEUS)
