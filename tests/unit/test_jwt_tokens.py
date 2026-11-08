"""O serviço aceita só tokens do `autogiro-auth`: assinatura, emissor e validade."""

from datetime import UTC, datetime, timedelta

import jwt

from app.infrastructure.config import settings
from app.infrastructure.security.jwt_tokens import decode_token, issue_token


def _assinar(payload: dict, secret: str | None = None) -> str:
    return jwt.encode(payload, secret or settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _payload_valido(**sobrescritas) -> dict:
    payload = {
        "sub": "11144477735",
        "client_id": 7,
        "name": "Maria",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "iss": settings.JWT_ISSUER,
    }
    payload.update(sobrescritas)
    return payload


class TestDecodeToken:
    def test_token_valido_devolve_o_principal_com_as_claims(self):
        token = issue_token("11144477735", client_id=7, name="Maria")

        principal = decode_token(token)

        assert principal is not None
        assert principal.subject == "11144477735"
        assert principal.client_id == 7
        assert principal.name == "Maria"
        assert principal.token == token

    def test_token_expirado_e_rejeitado(self):
        token = _assinar(_payload_valido(exp=datetime.now(UTC) - timedelta(seconds=1)))
        assert decode_token(token) is None

    def test_token_assinado_com_outro_segredo_e_rejeitado(self):
        token = _assinar(_payload_valido(), secret="outro-segredo-com-tamanho-suficiente")
        assert decode_token(token) is None

    def test_token_de_outro_emissor_e_rejeitado(self):
        token = _assinar(_payload_valido(iss="emissor-desconhecido"))
        assert decode_token(token) is None

    def test_token_sem_sub_e_rejeitado(self):
        payload = _payload_valido()
        del payload["sub"]
        assert decode_token(_assinar(payload)) is None

    def test_texto_que_nao_e_jwt_e_rejeitado(self):
        assert decode_token("isto-nao-e-um-jwt") is None

    def test_claims_opcionais_ausentes_viram_none(self):
        payload = _payload_valido()
        del payload["client_id"]
        del payload["name"]

        principal = decode_token(_assinar(payload))

        assert principal is not None
        assert principal.client_id is None
        assert principal.name is None
