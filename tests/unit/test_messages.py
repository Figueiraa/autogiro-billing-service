"""Envelope das mensagens e rotas no RabbitMQ."""

import json

import pytest

from app.application.messages import (
    EXCHANGE_COMMANDS,
    EXCHANGE_EVENTS,
    PRODUCER,
    Envelope,
    InvalidMessageError,
    command_routing_key,
    kebab,
)


class TestRotas:
    @pytest.mark.parametrize(
        ("comando", "rota"),
        [
            ("ReservarPecas", "execution.reservar-pecas"),
            ("LiberarPecas", "execution.liberar-pecas"),
            ("EnfileirarExecucao", "execution.enfileirar-execucao"),
            ("GerarOrcamento", "billing.gerar-orcamento"),
            ("CancelarCobranca", "billing.cancelar-cobranca"),
        ],
    )
    def test_cada_comando_vai_para_o_servico_que_o_executa(self, comando, rota):
        assert command_routing_key(comando) == rota

    def test_kebab_case(self):
        assert kebab("EnfileiramentoRecusado") == "enfileiramento-recusado"


class TestEnvelope:
    def test_comando_tem_exchange_rota_e_identificadores(self):
        envelope = Envelope.command("ReservarPecas", saga_id="s-1", order_id=7, payload={"items": []})

        assert (envelope.kind, envelope.exchange, envelope.routing_key) == (
            "command",
            EXCHANGE_COMMANDS,
            "execution.reservar-pecas",
        )
        assert envelope.producer == PRODUCER
        assert len(envelope.message_id) == 36
        assert envelope.version == 1

    def test_comando_desconhecido_e_recusado(self):
        with pytest.raises(ValueError, match="Comando desconhecido"):
            Envelope.command("ApagarTudo", saga_id="s", order_id=1, payload={})

    def test_evento_do_billing_vai_para_o_exchange_de_eventos(self):
        evento = Envelope.event("OrcamentoGerado", saga_id="s", order_id=1, payload={}, causation_id="c")
        assert (evento.kind, evento.producer, evento.routing_key) == (
            "event",
            "billing-service",
            "billing.orcamento-gerado",
        )
        assert evento.causation_id == "c"

    def test_evento_e_roteado_pelo_produtor(self):
        evento = Envelope(type="PecasReservadas", kind="event", saga_id="s", order_id=1, producer="execution-service")
        assert (evento.exchange, evento.routing_key) == (EXCHANGE_EVENTS, "execution.pecas-reservadas")

    def test_ida_e_volta_em_json(self):
        original = Envelope.command(
            "GerarOrcamento", saga_id="s-1", order_id=7, payload={"total": 10.5}, causation_id="c-1"
        )
        assert Envelope.from_json(original.to_json()) == original

    @pytest.mark.parametrize(
        "corpo",
        [
            b"nao e json",
            json.dumps({"type": "X"}).encode(),
            json.dumps({"type": "X", "kind": "event", "saga_id": "s", "order_id": "abc", "message_id": "m"}).encode(),
        ],
    )
    def test_envelope_malformado_e_invalido(self, corpo):
        with pytest.raises(InvalidMessageError):
            Envelope.from_json(corpo)

    def test_versao_futura_e_invalida(self):
        corpo = Envelope(type="X", kind="event", saga_id="s", order_id=1, version=2).to_json()
        with pytest.raises(InvalidMessageError, match="Versão 2"):
            Envelope.from_json(corpo)

    def test_tipo_de_mensagem_desconhecido_e_invalido(self):
        corpo = Envelope(type="X", kind="pedido", saga_id="s", order_id=1).to_json()
        with pytest.raises(InvalidMessageError, match="Tipo de mensagem"):
            Envelope.from_json(corpo)
