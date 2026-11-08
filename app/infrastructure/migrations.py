"""Aplica as migrations SQL de `migrations/` no banco do serviço.

Uso:
    python -m app.infrastructure.migrations

Cada arquivo roda uma única vez, em ordem de nome, dentro de uma transação, e
fica registrado em `schema_migrations`. Uma trava consultiva do PostgreSQL impede
que dois pods subindo ao mesmo tempo apliquem a mesma migration.
"""

import asyncio
import logging
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from app.infrastructure.config import settings
from app.infrastructure.logging_config import setup_logging

logger = logging.getLogger("autogiro.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# Número arbitrário e fixo que identifica a trava das migrations deste serviço.
_ADVISORY_LOCK_ID = 4_172_002

_PARAMETROS_IGNORADOS = frozenset({"channel_binding", "options", "target_session_attrs"})


def to_asyncpg_dsn(database_url: str) -> str:
    """Converte a URL do SQLAlchemy (`postgresql+asyncpg://…?ssl=require`) para o asyncpg."""
    parts = urlsplit(database_url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in _PARAMETROS_IGNORADOS:
            continue
        query.append(("sslmode", value) if key == "ssl" else (key, value))
    return urlunsplit(parts._replace(scheme="postgresql", query=urlencode(query)))


def pending_migrations(applied: set[str], directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Arquivos `.sql` ainda não aplicados, em ordem de nome."""
    return [path for path in sorted(directory.glob("*.sql")) if path.name not in applied]


async def migrate(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Aplica as migrations pendentes e devolve os nomes das que foram aplicadas."""
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_ID)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")}

        done = []
        for path in pending_migrations(applied, directory):
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.name)
            logger.info("Migration aplicada: %s", path.name)
            done.append(path.name)
        return done
    finally:
        await conn.close()


def main() -> None:  # pragma: no cover - ponto de entrada de linha de comando
    setup_logging(settings.LOG_LEVEL, service=settings.SERVICE_NAME)
    done = asyncio.run(migrate(settings.DATABASE_URL))
    logger.info("Migrations concluídas: %d aplicada(s)", len(done))


if __name__ == "__main__":  # pragma: no cover
    main()
