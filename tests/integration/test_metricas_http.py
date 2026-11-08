"""Exposição de métricas (`GET /metrics`) e instrumentação HTTP pelo middleware."""

from prometheus_client.parser import text_string_to_metric_families


def _amostra(payload: str, nome: str, rotulos: dict[str, str] | None = None) -> float:
    """Valor de uma amostra do texto de exposição (0.0 quando a série não existe)."""
    for familia in text_string_to_metric_families(payload):
        for amostra in familia.samples:
            if amostra.name == nome and (rotulos is None or rotulos.items() <= amostra.labels.items()):
                return amostra.value
    return 0.0


async def _scrape(client) -> str:
    r = await client.get("/metrics")
    assert r.status_code == 200
    return r.text


class TestEndpointDeMetricas:
    async def test_responde_no_formato_prometheus(self, client):
        r = await client.get("/metrics")

        assert r.headers["content-type"].startswith("text/plain")
        assert "# TYPE autogiro_http_requests_total counter" in r.text

    async def test_expoe_o_nome_do_servico(self, client):
        from app.infrastructure.config import settings

        payload = await _scrape(client)

        assert _amostra(payload, "autogiro_app_info", {"name": settings.SERVICE_NAME}) == 1

    async def test_nao_instrumenta_a_si_mesmo(self, client):
        await client.get("/metrics")
        payload = await _scrape(client)

        assert _amostra(payload, "autogiro_http_requests_total", {"endpoint": "/metrics"}) == 0


class TestInstrumentacaoHttp:
    async def test_conta_requisicao_bem_sucedida(self, client):
        rotulos = {"method": "GET", "endpoint": "/health", "status_code": "200"}
        antes = _amostra(await _scrape(client), "autogiro_http_requests_total", rotulos)

        await client.get("/health")

        assert _amostra(await _scrape(client), "autogiro_http_requests_total", rotulos) == antes + 1

    async def test_registra_a_duracao(self, client):
        await client.get("/health")
        payload = await _scrape(client)

        contagem = _amostra(
            payload, "autogiro_http_request_duration_seconds_count", {"method": "GET", "endpoint": "/health"}
        )
        assert contagem >= 1

    async def test_usa_o_template_da_rota_e_nao_a_url_concreta(self, client):
        await client.get("/api/v1/_test/items/123")
        payload = await _scrape(client)

        assert _amostra(
            payload, "autogiro_http_requests_total", {"endpoint": "/api/v1/_test/items/{item_id}"}
        ) >= 1
        assert _amostra(
            payload, "autogiro_http_requests_total", {"endpoint": "/api/v1/_test/items/123"}
        ) == 0

    async def test_rota_inexistente_vira_unmatched(self, client):
        await client.get("/rota/que/nao/existe")
        payload = await _scrape(client)

        assert _amostra(
            payload, "autogiro_http_requests_total", {"endpoint": "unmatched", "status_code": "404"}
        ) >= 1

    async def test_excecao_nao_tratada_conta_como_500(self, client):
        await client.get("/api/v1/_test/boom")
        payload = await _scrape(client)

        assert _amostra(
            payload,
            "autogiro_http_exceptions_total",
            {"endpoint": "/api/v1/_test/boom", "exception": "RuntimeError"},
        ) >= 1
        assert _amostra(
            payload, "autogiro_http_requests_total", {"endpoint": "/api/v1/_test/boom", "status_code": "500"}
        ) >= 1
