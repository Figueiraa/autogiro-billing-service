"""Billing de ponta a ponta sobre o banco: comandos da Saga, API e monitor de pagamentos."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.application.messages import Envelope
from app.application.use_cases.billing import DONE, DUPLICATE, IGNORED
from app.domain.exceptions import PaymentGatewayError
from app.infrastructure.persistence.models import OutboxMessage, PaymentModel, ProcessedMessage

GERAR = {
    "client": {"id": 1, "name": "Maria", "email": "maria@exemplo.com"},
    "services": [{"service_type_id": 1, "name": "Troca de óleo", "quantity": 1, "unit_price": 149.9}],
    "parts": [{"part_id": 2, "name": "Filtro de óleo", "quantity": 2, "unit_price": 32.5}],
}


def _comando(tipo, order_id=7, payload=None) -> Envelope:
    return Envelope.command(tipo, saga_id=f"saga-{order_id}", order_id=order_id, payload=payload or {})


async def _eventos(session_factory) -> list[tuple[str, dict]]:
    async with session_factory() as db:
        linhas = (await db.execute(select(OutboxMessage).order_by(OutboxMessage.id))).scalars().all()
    return [(linha.message_type, json.loads(linha.body)["payload"]) for linha in linhas]


async def _orcamento_gerado(billing, order_id=7):
    resultado = await billing.handle_command(_comando("GerarOrcamento", order_id, GERAR))
    assert resultado.event == "OrcamentoGerado"


async def _referencia(session_factory, order_id=7) -> str:
    async with session_factory() as db:
        return (await db.execute(select(PaymentModel.external_reference).where(PaymentModel.order_id == order_id))).scalar_one()


class TestGerarOrcamento:
    async def test_gera_avisa_o_cliente_e_responde_ao_orquestrador(self, billing, session_factory, notificador):
        comando = _comando("GerarOrcamento", payload=GERAR)

        resultado = await billing.handle_command(comando)

        assert (resultado.outcome, resultado.event) == (DONE, "OrcamentoGerado")
        assert await _eventos(session_factory) == [("OrcamentoGerado", {"budget_id": 1, "total": 214.9})]
        assert notificador.enviadas == [("maria@exemplo.com", "7", "AGUARDANDO_APROVACAO (orçamento de R$ 214,90)")]

    async def test_reenvio_nao_gera_outro_orcamento(self, billing, session_factory, notificador):
        await _orcamento_gerado(billing)
        await _orcamento_gerado(billing)

        assert [payload for _, payload in await _eventos(session_factory)] == [{"budget_id": 1, "total": 214.9}] * 2
        assert len(notificador.enviadas) == 1

    async def test_mesma_mensagem_duas_vezes_e_descartada(self, billing):
        comando = _comando("GerarOrcamento", payload=GERAR)
        await billing.handle_command(comando)
        assert (await billing.handle_command(comando)).outcome == DUPLICATE

    async def test_dados_invalidos_respondem_orcamento_falhou(self, billing, session_factory):
        payload = {**GERAR, "parts": [{"part_id": 2, "name": "Filtro", "quantity": 1, "unit_price": None}]}

        resultado = await billing.handle_command(_comando("GerarOrcamento", payload=payload))

        assert resultado.event == "OrcamentoFalhou"
        assert "Preço ausente" in (await _eventos(session_factory))[0][1]["reason"]

    async def test_mensagens_que_nao_sao_do_billing(self, billing):
        assert (await billing.handle_command(_comando("ReservarPecas"))).outcome == IGNORED
        evento = Envelope.event("PecasReservadas", saga_id="s", order_id=1, payload={})
        assert (await billing.handle_command(evento)).outcome == IGNORED


class TestDecisaoPelaApi:
    async def test_consulta_o_orcamento(self, auth_client, billing):
        await _orcamento_gerado(billing)

        r = await auth_client.get("/api/v1/budgets/7")

        assert (r.json()["status"], r.json()["total"], len(r.json()["lines"])) == ("PENDENTE", 214.9, 2)

    async def test_aprovar_cria_a_cobranca_e_devolve_o_link(self, auth_client, billing, session_factory, mercado_pago):
        await _orcamento_gerado(billing)

        r = await auth_client.post("/api/v1/budgets/7/approve")

        assert r.status_code == 200
        assert (r.json()["budget_status"], r.json()["payment_link"], r.json()["amount"]) == (
            "APROVADO",
            "https://mp.test/checkout/pref-1",
            214.9,
        )
        assert mercado_pago.preferencias["pref-1"]["title"] == "AutoGiro · Ordem de serviço 7"
        tipo, payload = (await _eventos(session_factory))[-1]
        assert (tipo, payload["payment_link"], payload["payment_id"]) == ("OrcamentoAprovado", "https://mp.test/checkout/pref-1", 1)
        pagamento = (await auth_client.get("/api/v1/payments/7")).json()
        assert (pagamento["status"], pagamento["payment_link"]) == ("PENDENTE", "https://mp.test/checkout/pref-1")

    async def test_mercado_pago_fora_do_ar_responde_502_e_nada_muda(self, auth_client, billing, mercado_pago):
        await _orcamento_gerado(billing)
        mercado_pago.falhar_criacao = True

        r = await auth_client.post("/api/v1/budgets/7/approve")

        assert r.status_code == 502
        assert (await auth_client.get("/api/v1/budgets/7")).json()["status"] == "PENDENTE"
        assert (await auth_client.get("/api/v1/payments/7")).status_code == 404

    async def test_recusar(self, auth_client, billing, session_factory):
        await _orcamento_gerado(billing)

        r = await auth_client.post("/api/v1/budgets/7/reject")

        assert r.json()["status"] == "RECUSADO"
        assert (await _eventos(session_factory))[-1] == ("OrcamentoRecusado", {"budget_id": 1})

    async def test_nao_responde_duas_vezes(self, auth_client, billing):
        await _orcamento_gerado(billing)
        await auth_client.post("/api/v1/budgets/7/approve")

        r = await auth_client.post("/api/v1/budgets/7/reject")

        assert r.status_code == 422
        assert "APROVADO" in r.json()["detail"]

    async def test_orcamento_vencido_nao_pode_ser_aprovado(self, auth_client, billing, session_factory):
        await _orcamento_gerado(billing)
        async with session_factory() as db:
            from app.infrastructure.persistence.models import BudgetModel

            orcamento = (await db.execute(select(BudgetModel))).scalar_one()
            orcamento.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await db.commit()

        r = await auth_client.post("/api/v1/budgets/7/approve")

        assert (r.status_code, r.json()["detail"]) == (422, "O prazo para responder este orçamento terminou")

    async def test_inexistentes_e_sem_token(self, auth_client, client):
        assert (await auth_client.get("/api/v1/budgets/99")).status_code == 404
        assert (await auth_client.get("/api/v1/payments/99")).status_code == 404
        assert (await auth_client.post("/api/v1/payments/99/sync")).status_code == 404
        assert (await client.get("/api/v1/budgets/7")).status_code == 401


class TestConfirmacaoDoPagamento:
    async def _aprovado(self, auth_client, billing):
        await _orcamento_gerado(billing)
        await auth_client.post("/api/v1/budgets/7/approve")

    async def test_sincronizar_sem_pagamento_ainda(self, auth_client, billing):
        await self._aprovado(auth_client, billing)
        assert (await auth_client.post("/api/v1/payments/7/sync")).json()["status"] == "PENDENTE"

    async def test_pagamento_aprovado(self, auth_client, billing, session_factory, mercado_pago):
        await self._aprovado(auth_client, billing)
        mercado_pago.pagar(await _referencia(session_factory), "approved", mp_id="mp-55")

        r = await auth_client.post("/api/v1/payments/7/sync")

        assert (r.json()["status"], r.json()["mp_payment_id"]) == ("APROVADO", "mp-55")
        assert (await _eventos(session_factory))[-1] == (
            "PagamentoConfirmado",
            {"payment_id": 1, "mp_payment_id": "mp-55", "amount": 214.9},
        )

    async def test_pagamento_recusado_encerra_o_link(self, auth_client, billing, session_factory, mercado_pago):
        await self._aprovado(auth_client, billing)
        mercado_pago.pagar(await _referencia(session_factory), "rejected", "cc_rejected_other_reason", "mp-56")

        r = await auth_client.post("/api/v1/payments/7/sync")

        assert (r.json()["status"], r.json()["status_detail"]) == ("RECUSADO", "cc_rejected_other_reason")
        assert (await _eventos(session_factory))[-1][0] == "PagamentoRecusado"
        assert mercado_pago.links_expirados == ["pref-1"]

    async def test_falha_na_consulta_nao_muda_nada(self, auth_client, billing, mercado_pago):
        await self._aprovado(auth_client, billing)
        mercado_pago.falhar_consulta = True
        assert (await auth_client.post("/api/v1/payments/7/sync")).json()["status"] == "PENDENTE"


class TestMonitor:
    async def test_orcamento_sem_resposta_expira(self, billing, session_factory):
        await _orcamento_gerado(billing)

        resultado = await billing.run_monitor(datetime.now(UTC) + timedelta(days=2))

        assert resultado.events == ["OrcamentoExpirado"]
        assert (await _eventos(session_factory))[-1] == ("OrcamentoExpirado", {"budget_id": 1})

    async def test_pagamento_sem_resposta_expira_e_encerra_o_link(self, billing, session_factory, mercado_pago):
        await _orcamento_gerado(billing)
        await billing.approve(7)

        resultado = await billing.run_monitor(datetime.now(UTC) + timedelta(hours=2))

        assert resultado.events == ["PagamentoExpirado"]
        assert mercado_pago.links_expirados == ["pref-1"]

    async def test_pagamento_tardio_e_confirmado_para_ser_estornado(self, billing, session_factory, mercado_pago):
        await _orcamento_gerado(billing)
        await billing.approve(7)
        await billing.run_monitor(datetime.now(UTC) + timedelta(hours=2))  # expira
        mercado_pago.pagar(await _referencia(session_factory), "approved", mp_id="mp-tarde")

        resultado = await billing.run_monitor(datetime.now(UTC) + timedelta(hours=3))

        assert resultado.events == ["PagamentoConfirmado"]

    async def test_nada_a_fazer(self, billing):
        assert (await billing.run_monitor()).events == []

    async def test_falha_ao_encerrar_o_link_nao_impede_a_expiracao(self, billing, mercado_pago):
        await _orcamento_gerado(billing)
        await billing.approve(7)
        mercado_pago.falhar_expiracao = True

        assert (await billing.run_monitor(datetime.now(UTC) + timedelta(hours=2))).events == ["PagamentoExpirado"]


class TestCancelarCobranca:
    async def test_sem_orcamento_nada_a_estornar(self, billing, session_factory):
        await billing.handle_command(_comando("CancelarCobranca", payload={"reason": "PECAS_INDISPONIVEIS"}))
        assert (await _eventos(session_factory))[-1] == ("CobrancaCancelada", {"estornado": False, "refund_id": None, "estorno_pendente": False})

    async def test_orcamento_recusado_nao_muda(self, auth_client, billing, session_factory):
        await _orcamento_gerado(billing)
        await auth_client.post("/api/v1/budgets/7/reject")

        await billing.handle_command(_comando("CancelarCobranca"))

        assert (await auth_client.get("/api/v1/budgets/7")).json()["status"] == "RECUSADO"

    async def test_cobranca_pendente_e_cancelada_sem_estorno(self, auth_client, billing, session_factory, mercado_pago):
        await _orcamento_gerado(billing)
        await auth_client.post("/api/v1/budgets/7/approve")

        await billing.handle_command(_comando("CancelarCobranca", payload={"reason": "PAGAMENTO_EXPIRADO"}))

        assert (await auth_client.get("/api/v1/payments/7")).json()["status"] == "CANCELADO"
        assert (await auth_client.get("/api/v1/budgets/7")).json()["status"] == "CANCELADO"
        assert mercado_pago.links_expirados == ["pref-1"]
        assert mercado_pago.estornos == []

    async def test_pagamento_feito_e_estornado_uma_vez_so(self, auth_client, billing, session_factory, mercado_pago):
        await _orcamento_gerado(billing)
        await auth_client.post("/api/v1/budgets/7/approve")
        referencia = await _referencia(session_factory)
        mercado_pago.pagar(referencia, "approved", mp_id="mp-77")
        await auth_client.post("/api/v1/payments/7/sync")

        await billing.handle_command(_comando("CancelarCobranca", payload={"reason": "EXECUCAO_RECUSADA"}))
        await billing.handle_command(_comando("CancelarCobranca", payload={"reason": "EXECUCAO_RECUSADA"}))

        pagamento = (await auth_client.get("/api/v1/payments/7")).json()
        assert (pagamento["status"], pagamento["refund_id"]) == ("ESTORNADO", "refund-mp-77")
        assert mercado_pago.estornos == [("mp-77", f"estorno-{referencia}")]
        respostas = [p for tipo, p in await _eventos(session_factory) if tipo == "CobrancaCancelada"]
        assert respostas == [{"estornado": True, "refund_id": "refund-mp-77", "estorno_pendente": False}] * 2

    async def test_falha_no_estorno_desfaz_tudo_para_tentar_de_novo(self, auth_client, billing, session_factory, mercado_pago):
        await _orcamento_gerado(billing)
        await auth_client.post("/api/v1/budgets/7/approve")
        mercado_pago.pagar(await _referencia(session_factory), "approved", mp_id="mp-78")
        await auth_client.post("/api/v1/payments/7/sync")
        mercado_pago.falhar_estorno = True
        comando = _comando("CancelarCobranca")

        with pytest.raises(PaymentGatewayError):
            await billing.handle_command(comando)

        assert (await auth_client.get("/api/v1/payments/7")).json()["status"] == "APROVADO"
        async with session_factory() as db:
            assert await db.get(ProcessedMessage, comando.message_id) is None


class TestEstornoRecusadoPeloProvedor:
    """Recusa definitiva do estorno (ex.: o 401 do sandbox): a Saga não pode travar."""

    async def _pago(self, auth_client, billing, session_factory, mercado_pago, mp_id="mp-90"):
        await _orcamento_gerado(billing)
        await auth_client.post("/api/v1/budgets/7/approve")
        referencia = await _referencia(session_factory)
        mercado_pago.pagar(referencia, "approved", mp_id=mp_id)
        await auth_client.post("/api/v1/payments/7/sync")
        mercado_pago.falhar_estorno_definitivo = True
        return referencia

    async def test_estorno_fica_pendente_e_a_compensacao_e_confirmada(
        self, auth_client, billing, session_factory, mercado_pago
    ):
        await self._pago(auth_client, billing, session_factory, mercado_pago)

        resultado = await billing.handle_command(_comando("CancelarCobranca", payload={"reason": "EXECUCAO_RECUSADA"}))

        assert resultado.event == "CobrancaCancelada"
        assert (await _eventos(session_factory))[-1][1] == {"estornado": False, "refund_id": None, "estorno_pendente": True}
        pagamento = (await auth_client.get("/api/v1/payments/7")).json()
        assert pagamento["status"] == "ESTORNO_PENDENTE"
        assert "401" in pagamento["status_detail"]

    async def test_comando_repetido_responde_igual(self, auth_client, billing, session_factory, mercado_pago):
        await self._pago(auth_client, billing, session_factory, mercado_pago)
        await billing.handle_command(_comando("CancelarCobranca"))

        await billing.handle_command(_comando("CancelarCobranca"))

        respostas = [p for tipo, p in await _eventos(session_factory) if tipo == "CobrancaCancelada"]
        assert respostas[-1]["estorno_pendente"] is True

    async def test_estorno_manual_e_conciliado_pelo_monitor(self, auth_client, billing, session_factory, mercado_pago):
        referencia = await self._pago(auth_client, billing, session_factory, mercado_pago)
        await billing.handle_command(_comando("CancelarCobranca"))
        assert (await billing.run_monitor()).events == []  # ainda não estornado no provedor

        mercado_pago.pagar(referencia, "refunded", "refunded", "mp-90")
        resultado = await billing.run_monitor()

        assert resultado.events == ["EstornoConciliado"]
        pagamento = (await auth_client.get("/api/v1/payments/7")).json()
        assert (pagamento["status"], pagamento["refund_id"]) == ("ESTORNADO", "manual")
