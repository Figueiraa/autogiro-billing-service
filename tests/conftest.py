import os

# Definido antes de importar a aplicação: o JWT_SECRET é obrigatório e não tem
# valor padrão no código.
os.environ.setdefault("JWT_SECRET", "segredo-de-teste-com-mais-de-32-bytes-de-tamanho")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.application.ports.payment_gateway import GatewayPayment, Preference
from app.application.use_cases.billing import BillingPolicy, BillingService
from app.domain.exceptions import BusinessRuleError, ConflictError, NotFoundError, PaymentGatewayError
from app.infrastructure.database import Base
from app.infrastructure.persistence.repositories import unit_of_work_factory
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


class MercadoPagoFalso:
    """Dublê do Mercado Pago: cobranças, pagamentos e estornos em memória."""

    def __init__(self):
        self.preferencias: dict[str, dict] = {}
        self.pagamentos: dict[str, GatewayPayment] = {}
        self.estornos: list[tuple[str, str]] = []
        self.tentativas_de_estorno = 0
        self.links_expirados: list[str] = []
        self.falhar_criacao = False
        self.falhar_consulta = False
        self.falhar_estorno = False
        self.falhar_estorno_definitivo = False
        self.falhar_expiracao = False

    async def create_preference(self, *, external_reference, title, amount, payer_email, expires_at):
        if self.falhar_criacao:
            raise PaymentGatewayError("Mercado Pago indisponível")
        pref_id = f"pref-{len(self.preferencias) + 1}"
        self.preferencias[pref_id] = {"ref": external_reference, "title": title, "amount": amount, "expires_at": expires_at}
        return Preference(id=pref_id, link=f"https://mp.test/checkout/{pref_id}")

    async def find_payment(self, external_reference):
        if self.falhar_consulta:
            raise PaymentGatewayError("timeout")
        return self.pagamentos.get(external_reference)

    async def refund(self, mp_payment_id, idempotency_key):
        self.tentativas_de_estorno += 1
        if self.falhar_estorno:
            raise PaymentGatewayError("estorno falhou")
        if self.falhar_estorno_definitivo:
            raise PaymentGatewayError("Mercado Pago respondeu 401: Unauthorized use of live credentials", retryable=False)
        self.estornos.append((mp_payment_id, idempotency_key))
        return f"refund-{mp_payment_id}"

    async def expire_preference(self, preference_id):
        if self.falhar_expiracao:
            raise PaymentGatewayError("não expirou")
        self.links_expirados.append(preference_id)

    def pagar(self, external_reference, status="approved", detalhe="accredited", mp_id="mp-1"):
        self.pagamentos[external_reference] = GatewayPayment(id=mp_id, status=status, status_detail=detalhe)


class NotificadorFalso:
    def __init__(self):
        self.enviadas = []

    async def notify_status_change(self, recipient, order_number, new_status):
        self.enviadas.append((recipient, order_number, new_status))


@pytest_asyncio.fixture
async def session_factory():
    """Banco em memória compartilhado entre sessões (StaticPool: uma conexão só)."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def mercado_pago():
    return MercadoPagoFalso()


@pytest.fixture
def notificador():
    return NotificadorFalso()


@pytest.fixture
def billing(session_factory, mercado_pago, notificador):
    return BillingService(unit_of_work_factory(session_factory), mercado_pago, notificador, BillingPolicy())


@pytest.fixture
def app(billing):
    application = create_app()
    application.include_router(_rotas_de_teste(), prefix=API_V1)
    application.state.billing = billing
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


@pytest_asyncio.fixture
async def auth_client(app, auth_headers):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as c:
        yield c
