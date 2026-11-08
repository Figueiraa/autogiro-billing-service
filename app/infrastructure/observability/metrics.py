"""Métricas de aplicação no formato Prometheus (OpenMetrics).

Concentra os coletores expostos em `GET /metrics`. Os nomes mantêm o prefixo
`autogiro_` do TC3, para que os painéis do New Relic sirvam aos três serviços;
o serviço de origem é identificado por `autogiro_app_info` e pelos rótulos do pod.

As métricas de negócio do serviço também são registradas neste módulo.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# Registry próprio (em vez do global) para que os testes possam inspecionar os
# coletores sem interferência de bibliotecas de terceiros.
REGISTRY = CollectorRegistry(auto_describe=True)

CONTENT_TYPE_PROMETHEUS = "text/plain; version=0.0.4; charset=utf-8"

# Faixas em segundos alinhadas ao SLO da API (p95 < 300 ms).
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# ─── Métricas técnicas (RED: Rate, Errors, Duration) ─────────────────────────

http_requests_total = Counter(
    "autogiro_http_requests_total",
    "Total de requisições HTTP recebidas pela API.",
    labelnames=("method", "endpoint", "status_code"),
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "autogiro_http_request_duration_seconds",
    "Duração das requisições HTTP em segundos.",
    labelnames=("method", "endpoint"),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

http_requests_in_progress = Gauge(
    "autogiro_http_requests_in_progress",
    "Requisições HTTP sendo processadas neste momento.",
    labelnames=("method", "endpoint"),
    registry=REGISTRY,
)

http_exceptions_total = Counter(
    "autogiro_http_exceptions_total",
    "Exceções não tratadas propagadas até o middleware.",
    labelnames=("method", "endpoint", "exception"),
    registry=REGISTRY,
)

# ─── Métricas de negócio ─────────────────────────────────────────────────────

budget_approvals_total = Counter(
    "autogiro_budget_approvals_total",
    "Respostas de orçamento recebidas do cliente.",
    labelnames=("result",),  # approved | refused
    registry=REGISTRY,
)

notifications_total = Counter(
    "autogiro_notifications_total",
    "Notificações enviadas ao cliente.",
    labelnames=("channel", "result"),  # channel: email|log — result: sent|skipped|failed
    registry=REGISTRY,
)

app_info = Gauge(
    "autogiro_app_info",
    "Metadados do serviço em execução (valor sempre 1).",
    labelnames=("version", "name"),
    registry=REGISTRY,
)


def set_app_info(name: str, version: str) -> None:
    app_info.labels(version=version, name=name).set(1)


def record_budget_approval(approved: bool) -> None:
    budget_approvals_total.labels(result="approved" if approved else "refused").inc()


def record_notification(channel: str, result: str) -> None:
    notifications_total.labels(channel=channel, result=result).inc()


def render_latest() -> bytes:
    """Serializa o snapshot atual das métricas no formato de exposição Prometheus."""
    return generate_latest(REGISTRY)
