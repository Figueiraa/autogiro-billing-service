"""Sondas de saúde, cabeçalhos HTTP, autenticação e tratamento de erros."""

import logging


class TestSondas:
    async def test_health(self, client):
        r = await client.get("/health")
        assert (r.status_code, r.json()) == (200, {"status": "ok"})

    async def test_liveness(self, client):
        r = await client.get("/health/live")
        assert (r.status_code, r.json()) == (200, {"status": "alive"})

    async def test_readiness_sem_dependencias_esta_pronto(self, client):
        r = await client.get("/health/ready")
        assert (r.status_code, r.json()) == (200, {"status": "ready", "checks": {}})

    async def test_readiness_com_dependencias_de_pe(self, app, client):
        async def ok():
            return True

        app.state.readiness_checks["banco"] = ok

        r = await client.get("/health/ready")

        assert (r.status_code, r.json()["checks"]) == (200, {"banco": "up"})

    async def test_readiness_com_dependencia_fora_responde_503(self, app, client):
        async def ok():
            return True

        async def fora():
            return False

        app.state.readiness_checks.update({"banco": ok, "broker": fora})

        r = await client.get("/health/ready")

        assert r.status_code == 503
        assert r.json() == {"status": "not_ready", "checks": {"banco": "up", "broker": "down"}}

    async def test_readiness_trata_excecao_da_verificacao_como_fora(self, app, client):
        async def explode():
            raise ConnectionError("sem rede")

        app.state.readiness_checks["broker"] = explode

        r = await client.get("/health/ready")

        assert (r.status_code, r.json()["checks"]) == (503, {"broker": "down"})


class TestCabecalhos:
    async def test_gera_request_id_quando_ausente(self, client):
        r = await client.get("/health")
        assert r.headers.get("X-Request-ID")

    async def test_propaga_o_request_id_recebido(self, client):
        r = await client.get("/health", headers={"X-Request-ID": "trace-123"})
        assert r.headers["X-Request-ID"] == "trace-123"

    async def test_cabecalhos_de_seguranca(self, client):
        r = await client.get("/health")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Content-Security-Policy"] == "default-src 'self'"

    async def test_swagger_nao_recebe_csp_restritiva(self, client):
        r = await client.get("/docs")
        assert r.status_code == 200
        assert "Content-Security-Policy" not in r.headers


class TestLogDeAcesso:
    async def test_emite_log_correlacionado_por_requisicao(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="autogiro.access"):
            await client.get("/health", headers={"X-Request-ID": "correlacao-xyz"})

        registros = [r for r in caplog.records if r.name == "autogiro.access"]
        assert registros
        assert registros[-1].request_id == "correlacao-xyz"

    async def test_scrape_de_metricas_nao_gera_log(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="autogiro.access"):
            await client.get("/metrics")

        assert not [r for r in caplog.records if r.name == "autogiro.access"]


class TestAutenticacao:
    async def test_sem_token_responde_401(self, client):
        r = await client.get("/api/v1/_test/protected")
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"] == "Bearer"

    async def test_token_invalido_responde_401(self, client):
        r = await client.get(
            "/api/v1/_test/protected", headers={"Authorization": "Bearer nao-e-jwt"}
        )
        assert r.status_code == 401

    async def test_token_valido_identifica_o_cliente(self, client, auth_headers):
        r = await client.get("/api/v1/_test/protected", headers=auth_headers)
        assert (r.status_code, r.json()) == (200, {"subject": "11144477735", "client_id": 1})


class TestErrosDeDominio:
    async def test_not_found_vira_404(self, client):
        r = await client.get("/api/v1/_test/not-found")
        assert (r.status_code, r.json()) == (404, {"detail": "Item 42 não encontrado(a)"})

    async def test_conflito_vira_409(self, client):
        r = await client.get("/api/v1/_test/conflict")
        assert (r.status_code, r.json()) == (409, {"detail": "Item duplicado"})

    async def test_regra_de_negocio_vira_422(self, client):
        r = await client.get("/api/v1/_test/business-rule")
        assert (r.status_code, r.json()) == (422, {"detail": "Transição inválida"})

    async def test_excecao_inesperada_vira_500(self, client):
        r = await client.get("/api/v1/_test/boom")
        assert r.status_code == 500
