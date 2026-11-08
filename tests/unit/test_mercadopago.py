"""Adaptador do Mercado Pago (chamadas HTTP simuladas)."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.domain.exceptions import PaymentGatewayError
from app.infrastructure.gateways.mercadopago import DisabledPaymentGateway, MercadoPagoGateway

EXPIRA = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)


def _gateway(handler, **kwargs) -> MercadoPagoGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MercadoPagoGateway("APP_USR-teste", base_url="https://mp.test/", client=client, **kwargs)


class TestPreferencia:
    async def test_cria_a_cobranca_com_referencia_valor_e_expiracao(self):
        pedidos = []

        def handler(request: httpx.Request) -> httpx.Response:
            pedidos.append(request)
            return httpx.Response(201, json={"id": "pref-1", "init_point": "https://mp/pagar", "sandbox_init_point": "https://sandbox/pagar"})

        preferencia = await _gateway(handler).create_preference(
            external_reference="autogiro-os-7-abc", title="OS 7", amount=214.904, payer_email="maria@x.com", expires_at=EXPIRA
        )

        assert (preferencia.id, preferencia.link) == ("pref-1", "https://mp/pagar")
        pedido = pedidos[0]
        corpo = json.loads(pedido.content)
        assert (pedido.method, str(pedido.url)) == ("POST", "https://mp.test/checkout/preferences")
        assert pedido.headers["Authorization"] == "Bearer APP_USR-teste"
        assert pedido.headers["X-Idempotency-Key"] == "preferencia-autogiro-os-7-abc"
        assert corpo["external_reference"] == "autogiro-os-7-abc"
        assert corpo["items"][0]["unit_price"] == 214.9
        assert (corpo["expires"], corpo["expiration_date_to"]) == (True, "2026-11-10T18:00:00.000+00:00")
        assert "payer" not in corpo

    async def test_usa_o_link_configurado(self):
        def handler(_):
            return httpx.Response(201, json={"id": "p", "init_point": "https://mp/pagar", "sandbox_init_point": "https://sandbox/pagar"})

        preferencia = await _gateway(handler, checkout_link_field="sandbox_init_point").create_preference(
            external_reference="r", title="t", amount=1, payer_email=None, expires_at=EXPIRA.replace(tzinfo=None)
        )
        assert preferencia.link == "https://sandbox/pagar"

    async def test_encerra_o_link(self):
        pedidos = []

        def handler(request):
            pedidos.append(request)
            return httpx.Response(200, json={})

        await _gateway(handler).expire_preference("pref-1")

        assert (pedidos[0].method, pedidos[0].url.path) == ("PUT", "/checkout/preferences/pref-1")
        assert "expiration_date_to" in json.loads(pedidos[0].content)


class TestConsulta:
    async def test_sem_pagamento_ainda(self):
        assert await _gateway(lambda _: httpx.Response(200, json={"results": []})).find_payment("r") is None

    async def test_aprovado_prevalece_sobre_tentativas_recusadas(self):
        resultados = [
            {"id": 3, "status": "rejected", "status_detail": "cc_rejected_other_reason"},
            {"id": 2, "status": "approved", "status_detail": "accredited"},
        ]

        pagamento = await _gateway(lambda _: httpx.Response(200, json={"results": resultados})).find_payment("r")

        assert (pagamento.id, pagamento.approved) == ("2", True)

    async def test_sem_aprovado_devolve_o_mais_recente(self):
        resultados = [{"id": 5, "status": "rejected", "status_detail": "x"}, {"id": 4, "status": "pending"}]

        pagamento = await _gateway(lambda _: httpx.Response(200, json={"results": resultados})).find_payment("r")

        assert (pagamento.id, pagamento.rejected) == ("5", True)

    async def test_filtra_pela_referencia_mais_recente_primeiro(self):
        pedidos = []

        def handler(request):
            pedidos.append(request)
            return httpx.Response(200, json={"results": []})

        await _gateway(handler).find_payment("autogiro-os-7-abc")

        assert dict(pedidos[0].url.params) == {
            "external_reference": "autogiro-os-7-abc",
            "sort": "date_created",
            "criteria": "desc",
        }


class TestEstorno:
    async def test_estorno_com_chave_de_idempotencia(self):
        pedidos = []

        def handler(request):
            pedidos.append(request)
            return httpx.Response(201, json={"id": 987})

        assert await _gateway(handler).refund("123", "estorno-r") == "987"
        assert (pedidos[0].url.path, pedidos[0].headers["X-Idempotency-Key"]) == ("/v1/payments/123/refunds", "estorno-r")


class TestFalhas:
    async def test_erro_do_mercado_pago_vira_falha_do_gateway(self):
        handler = lambda _: httpx.Response(400, json={"message": "invalid unit_price"})  # noqa: E731
        with pytest.raises(PaymentGatewayError, match="400: invalid unit_price"):
            await _gateway(handler).refund("1", "k")

    @pytest.mark.parametrize(("status", "temporaria"), [(400, False), (401, False), (404, False), (429, True), (500, True)])
    async def test_classifica_a_falha(self, status, temporaria):
        with pytest.raises(PaymentGatewayError) as erro:
            await _gateway(lambda _: httpx.Response(status, json={"message": "x"})).refund("1", "k")
        assert erro.value.retryable is temporaria

    async def test_erro_sem_json(self):
        with pytest.raises(PaymentGatewayError, match="502: bad gateway"):
            await _gateway(lambda _: httpx.Response(502, text="bad gateway")).find_payment("r")

    async def test_falha_de_rede(self):
        def handler(request):
            raise httpx.ConnectTimeout("demorou", request=request)

        with pytest.raises(PaymentGatewayError, match="indisponível"):
            await _gateway(handler).find_payment("r")

    async def test_sem_cliente_injetado_abre_conexao_propria(self):
        gateway = MercadoPagoGateway("t", base_url="http://127.0.0.1:9", timeout_seconds=0.5)
        with pytest.raises(PaymentGatewayError):
            await gateway.find_payment("r")

    async def test_gateway_desligado_explica_o_que_falta(self):
        with pytest.raises(PaymentGatewayError, match="MERCADOPAGO_ACCESS_TOKEN"):
            await DisabledPaymentGateway().refund("1", "k")
