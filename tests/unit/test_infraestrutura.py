"""Configuração, logging estruturado e coletores de métricas."""

import json
import logging
import sys

from app.domain.exceptions import NotFoundError
from app.infrastructure.config import Settings
from app.infrastructure.logging_config import JsonFormatter, request_id_ctx, setup_logging
from app.infrastructure.observability import metrics


class TestSettings:
    def test_cors_origins_vira_lista_sem_espacos_nem_vazios(self):
        config = Settings(JWT_SECRET="x" * 32, CORS_ORIGINS=" https://a.com , ,https://b.com ")
        assert config.cors_origins_list == ["https://a.com", "https://b.com"]

    def test_cors_padrao_libera_qualquer_origem(self):
        assert Settings(JWT_SECRET="x" * 32).cors_origins_list == ["*"]


class TestJsonFormatter:
    def _registro(self, mensagem: str = "ola", exc_info=None) -> logging.LogRecord:
        return logging.LogRecord("teste", logging.INFO, __file__, 1, mensagem, None, exc_info)

    def test_inclui_servico_nivel_e_mensagem(self):
        linha = json.loads(JsonFormatter("os-service").format(self._registro()))

        assert linha["service"] == "os-service"
        assert linha["level"] == "INFO"
        assert linha["message"] == "ola"

    def test_sem_servico_o_campo_nao_aparece(self):
        linha = json.loads(JsonFormatter().format(self._registro()))
        assert "service" not in linha

    def test_inclui_request_id_quando_presente(self):
        registro = self._registro()
        registro.request_id = "abc-123"

        linha = json.loads(JsonFormatter().format(registro))

        assert linha["request_id"] == "abc-123"

    def test_inclui_a_excecao_formatada(self):
        try:
            raise ValueError("quebrou")
        except ValueError:
            registro = self._registro(exc_info=sys.exc_info())

        linha = json.loads(JsonFormatter().format(registro))

        assert "ValueError: quebrou" in linha["exc_info"]

    def test_setup_logging_instala_um_unico_handler_json(self):
        setup_logging("debug", service="teste")
        raiz = logging.getLogger()

        assert len(raiz.handlers) == 1
        assert isinstance(raiz.handlers[0].formatter, JsonFormatter)
        assert raiz.level == logging.DEBUG

    def test_filtro_copia_o_request_id_do_contexto(self):
        setup_logging("info")
        handler = logging.getLogger().handlers[0]
        registro = self._registro()

        token = request_id_ctx.set("req-9")
        try:
            handler.filter(registro)
        finally:
            request_id_ctx.reset(token)

        assert registro.request_id == "req-9"


class TestMetricas:
    def test_expoe_as_familias_red_e_app_info(self):
        payload = metrics.render_latest().decode("utf-8")

        for nome in (
            "autogiro_http_requests_total",
            "autogiro_http_request_duration_seconds",
            "autogiro_http_requests_in_progress",
            "autogiro_http_exceptions_total",
            "autogiro_app_info",
        ):
            assert f"# HELP {nome}" in payload, f"métrica ausente: {nome}"

    def test_app_info_registra_nome_e_versao(self):
        metrics.set_app_info(name="servico-teste", version="9.9.9")
        payload = metrics.render_latest().decode("utf-8")

        assert 'autogiro_app_info{name="servico-teste",version="9.9.9"} 1.0' in payload


class TestExcecoesDeDominio:
    def test_not_found_descreve_entidade_e_identificador(self):
        erro = NotFoundError("Peça", 3)

        assert str(erro) == "Peça 3 não encontrado(a)"
        assert (erro.entity, erro.identifier) == ("Peça", 3)
