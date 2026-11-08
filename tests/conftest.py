import os

# Definido antes de importar a aplicação: o JWT_SECRET é obrigatório e não tem
# valor padrão no código.
os.environ.setdefault("JWT_SECRET", "segredo-de-teste-com-mais-de-32-bytes-de-tamanho")

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends
from httpx import ASGITransport, AsyncClient

from app.domain.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.infrastructure.security.jwt_tokens import Principal, issue_token
from app.interfaces.http.dependencies import get_current_principal
from app.main import API_V1, create_app

# CPF válido pelos dígitos verificadores, usado como o cliente autenticado.
AUTH_CPF = "11144477735"


def pytest_collection_modifyitems(items):
    """Marca cada teste conforme a pasta, para rodar `pytest -m unit` ou `-m integration`."""
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/tests/unit/" in path:
            item.add_marker(pytest.mark.unit)
        elif "/tests/integration/" in path:
            item.add_marker(pytest.mark.integration)


def _rotas_de_teste() -> APIRouter:
    """Rotas que existem só nos testes, para exercitar a infraestrutura comum
    (autenticação, métricas por template de rota, tratamento de exceções)."""
    router = APIRouter(prefix="/_test")

    @router.get("/protected")
    async def protected(principal: Principal = Depends(get_current_principal)):
        return {"subject": principal.subject, "client_id": principal.client_id}

    @router.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    @router.get("/not-found")
    async def not_found():
        raise NotFoundError("Item", 42)

    @router.get("/conflict")
    async def conflict():
        raise ConflictError("Item duplicado")

    @router.get("/business-rule")
    async def business_rule():
        raise BusinessRuleError("Transição inválida")

    @router.get("/boom")
    async def boom():
        raise RuntimeError("falha inesperada")

    return router


@pytest.fixture
def app():
    application = create_app()
    application.include_router(_rotas_de_teste(), prefix=API_V1)
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def token() -> str:
    return issue_token(AUTH_CPF, client_id=1, name="Cliente de Teste")


@pytest.fixture
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
