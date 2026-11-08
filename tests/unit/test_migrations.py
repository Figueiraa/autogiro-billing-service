"""Executor de migrations: conversão da URL e escolha das pendentes."""

from app.infrastructure.migrations import MIGRATIONS_DIR, pending_migrations, to_asyncpg_dsn


class TestToAsyncpgDsn:
    def test_troca_o_esquema_e_o_parametro_de_ssl(self):
        url = "postgresql+asyncpg://app:senha@ep-x.neon.tech/autogiro_os?ssl=require"
        assert to_asyncpg_dsn(url) == "postgresql://app:senha@ep-x.neon.tech/autogiro_os?sslmode=require"

    def test_descarta_parametros_que_o_asyncpg_nao_conhece(self):
        url = "postgresql://app:s@h/db?sslmode=require&channel_binding=require&options=x"
        assert to_asyncpg_dsn(url) == "postgresql://app:s@h/db?sslmode=require"


class TestPendingMigrations:
    def test_em_ordem_de_nome_e_sem_as_ja_aplicadas(self, tmp_path):
        for nome in ("002_b.sql", "001_a.sql", "003_c.sql", "leia-me.txt"):
            (tmp_path / nome).write_text("SELECT 1;", encoding="utf-8")

        pendentes = pending_migrations({"001_a.sql"}, tmp_path)

        assert [p.name for p in pendentes] == ["002_b.sql", "003_c.sql"]

    def test_as_migrations_do_servico_estao_numeradas(self):
        nomes = [p.name for p in pending_migrations(set())]
        assert nomes == sorted(nomes)
        assert nomes[0].startswith("001_")
        assert MIGRATIONS_DIR.name == "migrations"
