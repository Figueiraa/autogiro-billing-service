import json
import logging
import sys
from contextvars import ContextVar

from opentelemetry import trace

# Correlação de logs por requisição (preenchido pelo RequestIdMiddleware).
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# Saga, OS e tipo da mensagem em processamento (preenchido pelo consumidor da fila).
message_ctx: ContextVar[dict | None] = ContextVar("message_ctx", default=None)


class RequestIdFilter(logging.Filter):
    """Anexa ao registro o request id, o trace atual e a mensagem em processamento."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.trace_id = format(span_context.trace_id, "032x")
            record.span_id = format(span_context.span_id, "016x")
        record.message_fields = message_ctx.get() or {}
        return True


class JsonFormatter(logging.Formatter):
    """Formata logs como JSON estruturado, com o nome do serviço em cada linha.

    O campo `service` permite filtrar os logs dos três microsserviços no mesmo
    painel do New Relic sem depender dos rótulos do Kubernetes.
    """

    def __init__(self, service: str | None = None):
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self._service:
            payload["service"] = self._service
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        # `trace.id` e `span.id` são os nomes que o New Relic usa para ligar log e trace.
        trace_id = getattr(record, "trace_id", None)
        if trace_id:
            payload["trace.id"] = trace_id
            payload["span.id"] = record.span_id
        payload.update(getattr(record, "message_fields", None) or {})
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", service: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
