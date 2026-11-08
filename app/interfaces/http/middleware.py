import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.routing import Match

from app.infrastructure.logging_config import request_id_ctx
from app.infrastructure.observability.metrics import (
    http_exceptions_total,
    http_request_duration_seconds,
    http_requests_in_progress,
    http_requests_total,
)

# Endpoint de exposição das métricas — não é instrumentado a si mesmo.
METRICS_PATH = "/metrics"


# Logger separado do da aplicação: permite baixar o nível só do log de acesso
# sem perder os logs de negócio.
access_logger = logging.getLogger("autogiro.access")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Gera/propaga um X-Request-ID por requisição para rastreio e correlação de logs.

    Também emite uma linha de log por requisição concluída. Sem ela o
    `request_id` existiria apenas no header da resposta e nos logs de erro — não
    haveria como seguir o rastro de uma requisição bem-sucedida entre as
    réplicas, que é o ponto de ter correlação.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)

            # O log fica DENTRO do try, antes do reset do ContextVar: e o
            # RequestIdFilter que injeta o request_id no registro, lendo o
            # contexto. Logar depois do reset produziria uma linha sem
            # correlacao — exatamente o que este middleware existe para evitar.
            if request.url.path != METRICS_PATH:
                access_logger.info(
                    "%s %s -> %s em %sms",
                    request.method,
                    request.url.path,
                    response.status_code,
                    round((time.perf_counter() - started) * 1000, 2),
                )
        finally:
            request_id_ctx.reset(token)

        response.headers["X-Request-ID"] = request_id
        return response


# Rotas da documentação interativa (Swagger UI / ReDoc): carregam assets de CDN e
# usam script inline, então a CSP restritiva não se aplica a elas.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adiciona cabeçalhos de segurança recomendados (OWASP) às respostas da API."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith(_DOCS_PATHS):
            response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Instrumenta cada requisição HTTP (contagem, duração e concorrência).

    O rótulo `endpoint` usa o *template* da rota (`/api/v1/service-orders/{order_id}`)
    e não a URL concreta, para não explodir a cardinalidade das séries temporais
    com um label por identificador. Requisições sem rota correspondente (404)
    são agrupadas em `unmatched`.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path == METRICS_PATH:
            return await call_next(request)

        method = request.method
        # Antes do roteamento ainda não se sabe a rota: a métrica de requisições em
        # andamento usa um rótulo provisório e as demais usam o template resolvido.
        provisional = _provisional_label(request)
        in_progress = http_requests_in_progress.labels(method=method, endpoint=provisional)

        in_progress.inc()
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            endpoint = _endpoint_label(request, provisional)
            http_exceptions_total.labels(
                method=method, endpoint=endpoint, exception=type(exc).__name__
            ).inc()
            # A exceção vira 500 no handler global; contabiliza como tal.
            http_requests_total.labels(method=method, endpoint=endpoint, status_code="500").inc()
            http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(
                time.perf_counter() - start
            )
            raise
        else:
            endpoint = _endpoint_label(request, provisional)
            http_requests_total.labels(
                method=method, endpoint=endpoint, status_code=str(response.status_code)
            ).inc()
            http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(
                time.perf_counter() - start
            )
            return response
        finally:
            in_progress.dec()


def _endpoint_label(request: Request, fallback: str) -> str:
    """Resolve o template da rota que atendeu à requisição.

    Depois do roteamento o Starlette grava a rota escolhida em `scope["route"]`.
    O `path` de lá vem sem o prefixo do router (`/service-orders/{order_id}`), então
    o prefixo é recuperado do próprio caminho da requisição: o template só descreve
    o final da URL, e o que sobra na frente é o prefixo montado pelos `include`.
    """
    scope = request.scope
    route = scope.get("route")
    template = getattr(route, "path", None) if route is not None else None
    if template is None:
        return fallback

    path = scope.get("path") or request.url.path
    # Quantos segmentos o template descreve; o restante à esquerda é o prefixo.
    depth = template.count("/")
    if depth and path.count("/") >= depth:
        prefix = path.rsplit("/", depth)[0]
        return f"{prefix}{template}"
    return template


def _provisional_label(request: Request) -> str:
    """Rótulo usado antes do roteamento, apenas para requisições em andamento.

    Percorre as rotas em profundidade porque o FastAPI agrupa os routers incluídos
    em objetos intermediários sem atributo `path`.
    """
    template = _match_route(request.app.routes, request.scope)
    return template if template is not None else "unmatched"


def _match_route(routes, scope) -> str | None:
    """Procura, em profundidade, o template da rota que casa com o `scope`."""
    for route in routes:
        try:
            match, _ = route.matches(scope)
        except Exception:  # pragma: no cover - rota sem suporte a match
            continue
        if match is Match.NONE:
            continue

        path = getattr(route, "path", None)
        if path is not None:
            return path

        nested = getattr(route, "routes", None)
        if nested is None:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", ()) if original is not None else ()
        found = _match_route(nested, scope)
        if found is not None:
            return found
    return None
