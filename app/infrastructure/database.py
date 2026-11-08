from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.infrastructure.config import settings

# Parâmetros que o Neon inclui na connection string mas que o driver asyncpg não
# conhece: são específicos do libpq (usado por psycopg). Passá-los adiante levanta
# `TypeError: connect() got an unexpected keyword argument`.
#
# `sslmode` é traduzido para `ssl`, que é o nome equivalente no asyncpg; os demais
# são descartados.
_PARAMETROS_INCOMPATIVEIS = frozenset(
    {"channel_binding", "options", "target_session_attrs", "connect_timeout"}
)


def normalize_async_dsn(dsn: str) -> str:
    """Adapta uma connection string do PostgreSQL ao driver asyncpg.

    O provedor gerenciado entrega a URL no formato do libpq. Esta função a torna
    utilizável pelo asyncpg sem exigir que quem configura o ambiente conheça a
    diferença entre os dois drivers.
    """
    partes = urlsplit(dsn)

    if not partes.scheme.startswith("postgresql+asyncpg"):
        return dsn

    consulta = []
    for chave, valor in parse_qsl(partes.query, keep_blank_values=True):
        if chave in _PARAMETROS_INCOMPATIVEIS:
            continue
        if chave == "sslmode":
            # No asyncpg o parâmetro se chama `ssl`; `require` é o valor equivalente.
            consulta.append(("ssl", "require" if valor != "disable" else "disable"))
            continue
        consulta.append((chave, valor))

    return urlunsplit(partes._replace(query=urlencode(consulta)))


engine = create_async_engine(normalize_async_dsn(settings.DATABASE_URL), echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def database_is_up() -> bool:
    """Verificação de prontidão: o banco responde a uma consulta trivial."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return True
