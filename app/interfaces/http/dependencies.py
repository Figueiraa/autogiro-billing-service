"""Dependências de autenticação das rotas protegidas."""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.infrastructure.security.jwt_tokens import Principal, decode_token

bearer_scheme = HTTPBearer(auto_error=False, description="JWT emitido por autogiro-auth")

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Token inválido ou expirado",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    """Exige um JWT válido do `autogiro-auth` e devolve quem o portava."""
    if credentials is None:
        raise _CREDENTIALS_ERROR

    principal = decode_token(credentials.credentials)
    if principal is None:
        raise _CREDENTIALS_ERROR

    return principal
