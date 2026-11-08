"""Rastreamento distribuído com OpenTelemetry, exportado para o New Relic por OTLP.

Uma OS atravessa os três serviços por HTTP e por mensagens. Para que esse caminho
apareça como um trace só:

- HTTP de entrada (FastAPI), HTTP de saída (httpx) e banco são instrumentados
  automaticamente; o httpx propaga o cabeçalho `traceparent` (W3C) nas chamadas.
- Mensagens levam o `traceparent` nos cabeçalhos AMQP. O contexto é capturado
  quando a mensagem entra no outbox, dentro da requisição ou do consumo que a
  originou, e não quando o publicador do outbox a envia. Assim o trace segue o
  fluxo de negócio, e não o laço de publicação.
- Spans de mensagem levam `autogiro.saga_id`, `autogiro.order_id` e o tipo da
  mensagem. Os logs emitidos durante o processamento levam os mesmos campos e o
  `trace.id`, o que liga log e trace no New Relic.

Sem `OTEL_EXPORTER_OTLP_ENDPOINT` nada é exportado: a API do OpenTelemetry fica no
modo no-op e o serviço funciona igual (testes, execução local simples).
"""

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.sdk.trace.sampling import Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.infrastructure.logging_config import message_ctx

logger = logging.getLogger("autogiro.tracing")
tracer = trace.get_tracer("autogiro.messaging")

# Atributos próprios, com prefixo para não colidir com as convenções do OpenTelemetry.
ATTR_SAGA_ID = "autogiro.saga_id"
ATTR_ORDER_ID = "autogiro.order_id"
ATTR_MESSAGE_TYPE = "autogiro.message.type"
ATTR_OUTCOME = "autogiro.message.outcome"
ATTR_RETRY = "autogiro.message.retry_count"

# Rotas que não geram span: sondas do Kubernetes e coleta de métricas.
EXCLUDED_URLS = "/health,/metrics"

_provider_ready = False


class NoClientRootSampler(Sampler):
    """Não deixa um span de cliente (banco, HTTP de saída) começar um trace sozinho.

    Sem uma requisição ou mensagem por trás, esses spans vêm dos laços em segundo
    plano: a leitura do outbox a cada segundo, o monitor de pagamentos, as sondas
    de saúde. Gravá-los só encheria o New Relic de traces de uma linha. Dentro de
    um trace (requisição ou mensagem), o `ParentBased` segue a decisão do pai e
    eles continuam aparecendo.
    """

    def should_sample(
        self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None
    ) -> SamplingResult:
        if kind is SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)

    def get_description(self) -> str:
        return "NoClientRootSampler"


SAMPLER = ParentBased(root=NoClientRootSampler())


def setup_tracing(
    app,
    *,
    service_name: str,
    service_version: str,
    environment: str,
    endpoint: str | None,
    license_key: str | None,
    sqlalchemy_engine=None,
    instrument_pymongo: bool = False,
) -> bool:
    """Liga a exportação de traces e as instrumentações automáticas.

    Devolve `False` (e não instrumenta nada) quando não há endpoint configurado.
    """
    global _provider_ready
    if not endpoint:
        return False

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    if not _provider_ready:
        from opentelemetry.exporter.otlp.proto.http import Compression
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": service_version,
                "deployment.environment.name": environment,
            }
        )
        provider = TracerProvider(resource=resource, sampler=SAMPLER)
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces",
            # O New Relic autentica a ingestão OTLP pela license key no cabeçalho `api-key`.
            headers={"api-key": license_key} if license_key else None,
            compression=Compression.Gzip,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        HTTPXClientInstrumentor().instrument()
        if sqlalchemy_engine is not None:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument(engine=sqlalchemy_engine.sync_engine)
        if instrument_pymongo:
            from opentelemetry.instrumentation.pymongo import PymongoInstrumentor

            PymongoInstrumentor().instrument()
        _provider_ready = True

    # Sem os spans internos de cada `send`/`receive` do ASGI: um por pedaço da resposta.
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS, exclude_spans=["receive", "send"])
    logger.info("Rastreamento ativo: traces de %s exportados para %s", service_name, endpoint)
    return True


def shutdown_tracing() -> None:
    """Envia os spans que ainda estão no buffer antes de o processo terminar."""
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if shutdown is not None:
        shutdown()


# ─── HTTP ────────────────────────────────────────────────────────────────────


def annotate(*, order_id: Any = None, saga_id: Any = None) -> None:
    """Marca o span atual com a OS e a Saga, para achar o trace a partir delas."""
    span = trace.get_current_span()
    if order_id is not None:
        span.set_attribute(ATTR_ORDER_ID, str(order_id))
    if saga_id is not None:
        span.set_attribute(ATTR_SAGA_ID, str(saga_id))


# ─── Mensagens ───────────────────────────────────────────────────────────────


def capture_context() -> dict[str, str]:
    """Contexto do trace atual em cabeçalhos W3C, para gravar com a mensagem no outbox."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def dump_context() -> str | None:
    """`capture_context` serializado para a coluna do outbox (`None` fora de um trace)."""
    carrier = capture_context()
    return json.dumps(carrier) if carrier else None


def load_context(raw: str | None) -> dict[str, str]:
    try:
        return dict(json.loads(raw)) if raw else {}
    except (TypeError, ValueError):
        return {}


def _text_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Cabeçalhos AMQP podem chegar como bytes; o propagador W3C espera texto."""
    result: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            result[key] = value
    return result


def _envelope_ids(body: bytes | str) -> dict[str, str]:
    try:
        data = json.loads(body)
        return {ATTR_SAGA_ID: str(data["saga_id"]), ATTR_ORDER_ID: str(data["order_id"])}
    except (TypeError, ValueError, KeyError):
        return {}


@contextmanager
def publish_span(
    *,
    exchange: str,
    routing_key: str,
    message_id: str,
    message_type: str,
    body: bytes | str,
    stored_context: Mapping[str, str] | None,
) -> Iterator[dict[str, str]]:
    """Span do envio de uma mensagem do outbox; devolve os cabeçalhos a publicar.

    O span é filho do contexto gravado junto com a mensagem. Sem contexto gravado
    (mensagem criada fora de uma requisição, como pelo monitor de pagamentos),
    o envio inicia um trace novo.
    """
    parent = propagate.extract(dict(stored_context or {}))
    attributes = {
        "messaging.system": "rabbitmq",
        "messaging.operation.type": "send",
        "messaging.destination.name": exchange or "(default)",
        "messaging.rabbitmq.destination.routing_key": routing_key,
        "messaging.message.id": message_id,
        ATTR_MESSAGE_TYPE: message_type,
        **_envelope_ids(body),
    }
    with tracer.start_as_current_span(
        f"{exchange or 'default'} send", context=parent, kind=SpanKind.PRODUCER, attributes=attributes
    ):
        yield capture_context()


@contextmanager
def consume_span(
    queue: str, headers: Mapping[str, Any] | None, message_id: str | None, message_type: str | None
) -> Iterator[Span]:
    """Span do processamento de uma mensagem, continuando o trace de quem a enviou.

    Enquanto o span está aberto, os logs saem com a Saga e a OS da mensagem
    (preenchidas por `annotate_message`).
    """
    parent = propagate.extract(_text_headers(headers))
    attributes = {
        "messaging.system": "rabbitmq",
        "messaging.operation.type": "process",
        "messaging.destination.name": queue,
        "messaging.message.id": message_id or "",
        # Sem tipo, a mensagem nem chegou a ser um envelope válido (vai para a DLQ).
        ATTR_MESSAGE_TYPE: message_type or "(sem tipo)",
    }
    token = message_ctx.set({})
    try:
        with tracer.start_as_current_span(
            f"{queue} process", context=parent, kind=SpanKind.CONSUMER, attributes=attributes
        ) as span:
            yield span
    finally:
        message_ctx.reset(token)


def annotate_message(*, saga_id: str, order_id: int, message_type: str, retry_count: int = 0) -> None:
    """Liga o processamento à Saga e à OS: no span atual e nos logs."""
    span = trace.get_current_span()
    span.set_attribute(ATTR_SAGA_ID, saga_id)
    span.set_attribute(ATTR_ORDER_ID, str(order_id))
    span.set_attribute(ATTR_MESSAGE_TYPE, message_type)
    span.set_attribute(ATTR_RETRY, retry_count)
    fields = message_ctx.get()
    if fields is not None:
        fields.update({"saga_id": saga_id, "order_id": order_id, "message_type": message_type})


def record_outcome(span: Span, outcome: str, error: str | None = None) -> None:
    """Desfecho da entrega (done, retry, dead_letter): base do alerta de DLQ."""
    span.set_attribute(ATTR_OUTCOME, outcome)
    if outcome != "done":
        span.set_status(Status(StatusCode.ERROR, error or outcome))
