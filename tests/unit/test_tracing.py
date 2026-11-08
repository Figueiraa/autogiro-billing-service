"""Rastreamento distribuído: o trace atravessa o outbox, o broker e o consumidor."""

import json
import logging

import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from app.infrastructure.logging_config import JsonFormatter, RequestIdFilter
from app.infrastructure.observability import tracing

CORPO = json.dumps({"saga_id": "saga-1", "order_id": 42, "type": "ReservarPecas"})


@pytest.fixture
def spans(monkeypatch):
    """Spans gravados em memória, sem mexer no provider global do processo."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "tracer", provider.get_tracer("teste"))
    yield exporter
    provider.shutdown()


def _publicar(stored_context):
    with tracing.publish_span(
        exchange="autogiro.commands",
        routing_key="execution.reservar-pecas",
        message_id="m-1",
        message_type="ReservarPecas",
        body=CORPO,
        stored_context=stored_context,
    ) as headers:
        return headers


def _por_nome(exporter, nome):
    return next(span for span in exporter.get_finished_spans() if span.name == nome)


class TestOutboxEEnvio:
    def test_envio_continua_o_trace_em_que_a_mensagem_foi_gravada(self, spans):
        with tracing.tracer.start_as_current_span("POST /service-orders") as requisicao:
            gravado = tracing.dump_context()

        headers = _publicar(tracing.load_context(gravado))

        envio = _por_nome(spans, "autogiro.commands send")
        assert envio.kind is SpanKind.PRODUCER
        assert envio.parent.span_id == requisicao.get_span_context().span_id
        assert envio.context.trace_id == requisicao.get_span_context().trace_id
        assert envio.attributes[tracing.ATTR_SAGA_ID] == "saga-1"
        assert envio.attributes[tracing.ATTR_ORDER_ID] == "42"
        assert envio.attributes["messaging.rabbitmq.destination.routing_key"] == "execution.reservar-pecas"
        # O cabeçalho publicado aponta para o span do envio.
        assert format(envio.context.span_id, "016x") in headers["traceparent"]

    def test_mensagem_sem_contexto_inicia_um_trace_novo(self, spans):
        _publicar(None)
        assert _por_nome(spans, "autogiro.commands send").parent is None

    def test_fora_de_um_trace_nada_e_gravado(self):
        assert tracing.dump_context() is None

    @pytest.mark.parametrize("bruto", [None, "", "{quebrado", "[1, 2"])
    def test_contexto_ilegivel_vira_vazio(self, bruto):
        assert tracing.load_context(bruto) == {}

    def test_corpo_que_nao_e_envelope_nao_impede_o_envio(self, spans):
        with tracing.publish_span(
            exchange="",
            routing_key="os.saga-events.dlq",
            message_id="m-2",
            message_type="",
            body=b"{isto nao e json",
            stored_context={},
        ):
            pass
        envio = _por_nome(spans, "default send")
        assert tracing.ATTR_SAGA_ID not in envio.attributes


class TestConsumo:
    def test_consumo_e_filho_do_envio_mesmo_com_cabecalhos_em_bytes(self, spans):
        headers = _publicar(None)
        recebidos = {chave: valor.encode() for chave, valor in headers.items()} | {"x-retry-count": 1}

        with tracing.consume_span("execution.commands", recebidos, "m-1", "ReservarPecas"):
            pass

        envio = _por_nome(spans, "autogiro.commands send")
        consumo = _por_nome(spans, "execution.commands process")
        assert consumo.kind is SpanKind.CONSUMER
        assert consumo.parent.span_id == envio.context.span_id
        assert consumo.context.trace_id == envio.context.trace_id

    def test_logs_do_processamento_levam_trace_e_saga(self, spans):
        registros = []
        filtro = RequestIdFilter()

        with tracing.consume_span("os.saga-events", {}, "m-1", "PecasReservadas") as span:
            tracing.annotate_message(saga_id="saga-1", order_id=42, message_type="PecasReservadas", retry_count=2)
            registro = logging.LogRecord("autogiro", logging.INFO, __file__, 1, "processando", None, None)
            filtro.filter(registro)
            registros.append(json.loads(JsonFormatter("os-service").format(registro)))
            trace_id = format(span.get_span_context().trace_id, "032x")

        linha = registros[0]
        assert linha["trace.id"] == trace_id
        assert linha["saga_id"] == "saga-1"
        assert linha["order_id"] == 42
        assert linha["message_type"] == "PecasReservadas"

        consumo = _por_nome(spans, "os.saga-events process")
        assert consumo.attributes[tracing.ATTR_SAGA_ID] == "saga-1"
        assert consumo.attributes[tracing.ATTR_RETRY] == 2

    def test_campos_da_mensagem_nao_vazam_para_os_logs_seguintes(self, spans):
        with tracing.consume_span("os.saga-events", {}, "m-1", "PecasReservadas"):
            tracing.annotate_message(saga_id="saga-1", order_id=42, message_type="PecasReservadas")

        registro = logging.LogRecord("autogiro", logging.INFO, __file__, 1, "depois", None, None)
        RequestIdFilter().filter(registro)
        linha = json.loads(JsonFormatter().format(registro))
        assert "saga_id" not in linha
        assert "trace.id" not in linha

    @pytest.mark.parametrize(("desfecho", "erro"), [("retry", "banco fora"), ("dead_letter", None)])
    def test_falha_na_entrega_marca_o_span_como_erro(self, spans, desfecho, erro):
        with tracing.consume_span("os.saga-events", {}, "m-1", "X") as span:
            tracing.record_outcome(span, desfecho, erro)

        consumo = _por_nome(spans, "os.saga-events process")
        assert consumo.attributes[tracing.ATTR_OUTCOME] == desfecho
        assert consumo.status.status_code is StatusCode.ERROR
        assert consumo.status.description == (erro or desfecho)

    def test_entrega_concluida_nao_e_erro(self, spans):
        with tracing.consume_span("os.saga-events", {}, "m-1", "X") as span:
            tracing.record_outcome(span, "done")
        assert _por_nome(spans, "os.saga-events process").status.status_code is StatusCode.UNSET


class TestHttp:
    def test_annotate_marca_o_span_atual_com_a_os_e_a_saga(self, spans):
        with tracing.tracer.start_as_current_span("GET /service-orders/{order_id}"):
            tracing.annotate(order_id=7, saga_id="saga-7")
        span = _por_nome(spans, "GET /service-orders/{order_id}")
        assert span.attributes[tracing.ATTR_ORDER_ID] == "7"
        assert span.attributes[tracing.ATTR_SAGA_ID] == "saga-7"


class TestAmostragem:
    def _gravados(self, monkeypatch):
        exporter = InMemorySpanExporter()
        provider = TracerProvider(sampler=tracing.SAMPLER)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        return exporter, provider.get_tracer("teste")

    def test_consulta_de_laco_em_segundo_plano_nao_vira_trace(self, monkeypatch):
        exporter, tracer = self._gravados(monkeypatch)
        with tracer.start_as_current_span("SELECT outbox", kind=SpanKind.CLIENT):
            pass
        assert exporter.get_finished_spans() == ()

    def test_consulta_dentro_de_uma_requisicao_aparece_no_trace(self, monkeypatch):
        exporter, tracer = self._gravados(monkeypatch)
        with tracer.start_as_current_span("POST /service-orders", kind=SpanKind.SERVER):
            with tracer.start_as_current_span("INSERT service_orders", kind=SpanKind.CLIENT):
                pass
        assert [span.name for span in exporter.get_finished_spans()] == [
            "INSERT service_orders",
            "POST /service-orders",
        ]

    @pytest.mark.parametrize("tipo", [SpanKind.SERVER, SpanKind.CONSUMER, SpanKind.PRODUCER, SpanKind.INTERNAL])
    def test_requisicoes_e_mensagens_comecam_traces(self, monkeypatch, tipo):
        exporter, tracer = self._gravados(monkeypatch)
        with tracer.start_as_current_span("raiz", kind=tipo):
            pass
        assert len(exporter.get_finished_spans()) == 1
        assert tracing.SAMPLER.get_description().startswith("ParentBased")


class TestConfiguracao:
    def test_sem_endpoint_nada_e_instrumentado(self):
        app = FastAPI()
        assert not tracing.setup_tracing(
            app, service_name="x", service_version="0", environment="teste", endpoint=None, license_key=None
        )
        assert not getattr(app, "_is_instrumented_by_opentelemetry", False)

    def test_com_endpoint_exporta_para_o_new_relic_com_a_license_key(self, monkeypatch):
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from sqlalchemy.ext.asyncio import create_async_engine

        providers = []
        monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
        monkeypatch.setattr(tracing, "_provider_ready", False)
        engine = create_async_engine("sqlite+aiosqlite://")
        app = FastAPI()
        try:
            assert tracing.setup_tracing(
                app,
                service_name="os-service",
                service_version="1.2.3",
                environment="teste",
                endpoint="https://otlp.nr-data.net/",
                license_key="chave",
                sqlalchemy_engine=engine,
            )
            (provider,) = providers
            atributos = provider.resource.attributes
            assert atributos["service.name"] == "os-service"
            assert atributos["service.version"] == "1.2.3"
            assert atributos["deployment.environment.name"] == "teste"
            assert app._is_instrumented_by_opentelemetry
        finally:
            HTTPXClientInstrumentor().uninstrument()
            SQLAlchemyInstrumentor().uninstrument()
            for provider in providers:
                provider.shutdown()

    def test_shutdown_sem_sdk_nao_falha(self):
        tracing.shutdown_tracing()
