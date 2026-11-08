from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import BusinessRuleError, ConflictError, NotFoundError, PaymentGatewayError

_STATUS_BY_EXCEPTION = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    BusinessRuleError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # O provedor de pagamento falhou: o pedido pode ser repetido.
    PaymentGatewayError: status.HTTP_502_BAD_GATEWAY,
}


def _status_for(exc: Exception) -> int:
    """Status HTTP da exceção, respeitando a herança (subclasses herdam o status da base)."""
    for exception_type, http_status in _STATUS_BY_EXCEPTION.items():
        if isinstance(exc, exception_type):
            return http_status
    return status.HTTP_500_INTERNAL_SERVER_ERROR  # pragma: no cover - só tipos registrados chegam aqui


def register_exception_handlers(app: FastAPI) -> None:
    """Mapeia as exceções de domínio para respostas HTTP (camada de interface)."""

    async def _handle(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=_status_for(exc), content={"detail": str(exc)})

    for exception_type in _STATUS_BY_EXCEPTION:
        app.add_exception_handler(exception_type, _handle)
