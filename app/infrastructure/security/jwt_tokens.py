"""Validação do JWT emitido pela Lambda `autogiro-auth`.

O contrato entre emissor e validadores é o segredo HS256 compartilhado e a claim
`iss` (ADR-004). O `sub` carrega o CPF/CNPJ do cliente; `client_id` e `name`
vêm como claims adicionais.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from jwt.exceptions import InvalidTokenError

from app.infrastructure.config import settings


@dataclass(frozen=True)
class Principal:
    """Quem fez a requisição, segundo o token."""

    subject: str
    client_id: int | None
    name: str | None
    # Token original: repassado nas chamadas síncronas entre serviços, para que o
    # serviço chamado também valide quem originou a requisição.
    token: str


def decode_token(token: str) -> Principal | None:
    """Valida assinatura, expiração e emissor. Devolve `None` se o token for inválido."""
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options={"require": ["sub", "exp", "iss"]},
        )
    except InvalidTokenError:
        return None

    return Principal(
        subject=str(claims["sub"]),
        client_id=claims.get("client_id"),
        name=claims.get("name"),
        token=token,
    )


def issue_token(
    subject: str,
    *,
    client_id: int | None = None,
    name: str | None = None,
    expires_in_minutes: int = 60,
) -> str:
    """Emite um token no mesmo formato da Lambda. Uso: testes e desenvolvimento local."""
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "client_id": client_id,
        "name": name,
        "iat": now,
        "exp": now + timedelta(minutes=expires_in_minutes),
        "iss": settings.JWT_ISSUER,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
