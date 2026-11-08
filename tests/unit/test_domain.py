"""Regras do orçamento e do pagamento."""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.entities.budget import Budget, BudgetStatus, InvalidBudgetError, LineKind
from app.domain.entities.payment import Payment, PaymentStatus, new_external_reference
from app.domain.exceptions import InvalidStatusTransitionError

T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
CLIENTE = {"id": 1, "name": "Maria", "email": "maria@exemplo.com"}
SERVICOS = [{"service_type_id": 1, "name": "Troca de óleo", "quantity": 1, "unit_price": 149.9}]
PECAS = [{"part_id": 2, "name": "Filtro de óleo", "quantity": 2, "unit_price": 32.5}]


def _orcamento(**kwargs) -> Budget:
    dados = {"services": SERVICOS, "parts": PECAS, **kwargs}
    return Budget.generate(
        order_id=7, saga_id="s-7", client=CLIENTE, approval_timeout=timedelta(hours=24), now=T0, **dados
    )


class TestGeracao:
    def test_soma_servicos_e_pecas_com_os_precos_recebidos(self):
        orcamento = _orcamento()

        assert orcamento.total == 214.9
        assert [(linha.kind, linha.description, linha.subtotal) for linha in orcamento.lines] == [
            (LineKind.SERVICO, "Troca de óleo", 149.9),
            (LineKind.PECA, "Filtro de óleo", 65.0),
        ]
        assert (orcamento.status, orcamento.client_email) == (BudgetStatus.PENDENTE, "maria@exemplo.com")
        assert orcamento.expires_at == T0 + timedelta(hours=24)

    def test_peca_sem_nome_recebe_descricao_generica(self):
        orcamento = _orcamento(parts=[{"part_id": 9, "quantity": 1, "unit_price": 10}])
        assert orcamento.lines[-1].description == "Peça 9"

    @pytest.mark.parametrize(
        ("servicos", "pecas", "mensagem"),
        [
            ([], [], "não tem serviços nem peças"),
            (SERVICOS, [{"part_id": 2, "name": "Filtro", "quantity": 1, "unit_price": None}], "Preço ausente"),
            ([{"service_type_id": 1, "name": "Cortesia", "quantity": 1, "unit_price": 0}], [], "maior que zero"),
        ],
    )
    def test_dados_invalidos_nao_geram_orcamento(self, servicos, pecas, mensagem):
        with pytest.raises(InvalidBudgetError, match=mensagem):
            _orcamento(services=servicos, parts=pecas)


class TestDecisao:
    def test_aprovar_e_recusar(self):
        aprovado, recusado = _orcamento(), _orcamento()
        aprovado.approve(T0)
        recusado.reject(T0)
        assert (aprovado.status, recusado.status, aprovado.decided_at) == (BudgetStatus.APROVADO, BudgetStatus.RECUSADO, T0)

    def test_nao_responde_duas_vezes(self):
        orcamento = _orcamento()
        orcamento.approve(T0)
        with pytest.raises(InvalidStatusTransitionError):
            orcamento.reject(T0)

    def test_expira_so_quando_pendente_e_no_prazo(self):
        orcamento = _orcamento()
        assert not orcamento.is_expired(T0 + timedelta(hours=23))
        assert orcamento.is_expired((T0 + timedelta(hours=24)).replace(tzinfo=None))
        orcamento.expire(T0)
        assert orcamento.status == BudgetStatus.EXPIRADO
        assert not orcamento.is_expired(T0 + timedelta(days=9))

    def test_cancelar_pendente_ou_aprovado(self):
        pendente, aprovado, recusado = _orcamento(), _orcamento(), _orcamento()
        aprovado.approve(T0)
        recusado.reject(T0)

        assert pendente.cancel(T0) and aprovado.cancel(T0)
        assert recusado.cancel(T0) is False
        assert recusado.status == BudgetStatus.RECUSADO


def _pagamento() -> Payment:
    return Payment(budget_id=1, order_id=7, saga_id="s-7", amount=214.9, expires_at=T0 + timedelta(hours=1))


class TestPagamento:
    def test_referencia_externa_identifica_a_os(self):
        referencia = new_external_reference(7)
        assert referencia.startswith("autogiro-os-7-")
        assert referencia != new_external_reference(7)

    def test_confirmado_e_depois_estornado(self):
        pagamento = _pagamento()
        pagamento.confirm("mp-1", T0)
        pagamento.refund("refund-1", T0)
        assert (pagamento.status, pagamento.mp_payment_id, pagamento.refund_id) == (PaymentStatus.ESTORNADO, "mp-1", "refund-1")

    def test_recusado_guarda_o_motivo(self):
        pagamento = _pagamento()
        pagamento.reject("mp-2", "cc_rejected_other_reason", T0)
        assert (pagamento.status, pagamento.status_detail) == (PaymentStatus.RECUSADO, "cc_rejected_other_reason")

    @pytest.mark.parametrize("encerrar", ["expire", "cancel"])
    def test_pagamento_tardio_ainda_pode_ser_confirmado(self, encerrar):
        pagamento = _pagamento()
        getattr(pagamento, encerrar)(T0)

        pagamento.confirm("mp-3", T0)

        assert pagamento.status == PaymentStatus.APROVADO

    def test_estorno_pendente_e_depois_conciliado(self):
        pagamento = _pagamento()
        pagamento.confirm("mp-1", T0)
        pagamento.refund_pending("Mercado Pago respondeu 401", T0)
        assert (pagamento.status, pagamento.status_detail) == (PaymentStatus.ESTORNO_PENDENTE, "Mercado Pago respondeu 401")
        pagamento.refund("manual", T0)
        assert pagamento.status == PaymentStatus.ESTORNADO

    def test_estorno_so_de_pagamento_aprovado(self):
        with pytest.raises(InvalidStatusTransitionError):
            _pagamento().refund("refund-1")

    def test_expiracao(self):
        pagamento = _pagamento()
        assert not pagamento.is_expired(T0)
        assert pagamento.is_expired((T0 + timedelta(hours=1)).replace(tzinfo=None))
