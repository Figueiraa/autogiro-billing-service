"""Normalização da connection string para o driver asyncpg.

O Neon entrega a URL no formato do libpq, com parâmetros que o asyncpg não
conhece. Sem tratamento, a aplicação não sobe:

    TypeError: connect() got an unexpected keyword argument 'channel_binding'
"""

import pytest

from app.infrastructure.database import normalize_async_dsn

BASE = "postgresql+asyncpg://usuario:senha@host.neon.tech/autogiro"


class TestNormalizeAsyncDsn:
    def test_remove_channel_binding_que_o_asyncpg_nao_conhece(self):
        entrada = f"{BASE}?sslmode=require&channel_binding=require"

        assert normalize_async_dsn(entrada) == f"{BASE}?ssl=require"

    def test_traduz_sslmode_para_ssl(self):
        assert normalize_async_dsn(f"{BASE}?sslmode=require") == f"{BASE}?ssl=require"

    def test_preserva_url_que_ja_esta_correta(self):
        entrada = f"{BASE}?ssl=require"

        assert normalize_async_dsn(entrada) == entrada

    def test_url_sem_parametros_passa_intacta(self):
        assert normalize_async_dsn(BASE) == BASE

    @pytest.mark.parametrize(
        "parametro",
        ["channel_binding=require", "options=-c%20search_path%3Dx", "connect_timeout=10"],
    )
    def test_descarta_parametros_especificos_do_libpq(self, parametro):
        chave = parametro.split("=")[0]

        resultado = normalize_async_dsn(f"{BASE}?{parametro}")

        assert chave not in resultado

    def test_nao_toca_em_dsn_de_outro_driver(self):
        """O SQLite dos testes e o psycopg da Lambda passam intactos."""
        for dsn in ("sqlite+aiosqlite:///:memory:", "postgresql://u:p@h/db?sslmode=require"):
            assert normalize_async_dsn(dsn) == dsn

    def test_preserva_parametros_que_o_asyncpg_aceita(self):
        entrada = f"{BASE}?ssl=require&application_name=autogiro"

        resultado = normalize_async_dsn(entrada)

        assert "ssl=require" in resultado
        assert "application_name=autogiro" in resultado

    def test_sslmode_disable_vira_ssl_disable(self):
        resultado = normalize_async_dsn(f"{BASE}?sslmode=disable")

        assert resultado == f"{BASE}?ssl=disable"
