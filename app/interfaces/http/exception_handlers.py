from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import BusinessRuleError, ConflictError, NotFoundError

_STATUS_BY_EXCEPTION = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    BusinessRuleError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def register_exception_handlers(app: FastAPI) -> None:
    """Mapeia as exceções de domínio para respostas HTTP (camada de interface)."""

    async def _handle(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=_STATUS_BY_EXCEPTION[type(exc)], content={"detail": str(exc)})

    for exception_type in _STATUS_BY_EXCEPTION:
        app.add_exception_handler(exception_type, _handle)
