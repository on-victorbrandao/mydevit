# testes de inicializacao e idempotencia do schema sqlite
import os
import sys
from pathlib import Path
from unittest.mock import patch

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from core.db import get_connection, get_db_path, init_db

PASTA_DATA_TESTS = PASTA_BACK / "data" / "tests"

TABELAS_ESPERADAS = {
    "produtos",
    "historico_preco",
    "temas",
    "buscas",
    "posts",
    "metricas_snapshots",
    "execucoes",
    "gasto_ia",
}

INDICES_ESPERADOS = {
    "ix_prod_status",
    "ix_posts_data",
}


def test_init_db_cria_tabelas_e_e_idempotente():
    # cria o banco explicitamente dentro de bytz/back/data/tests/, ignorado pelo git
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    db_file = PASTA_DATA_TESTS / "test_bytz.db"

    if db_file.exists():
        db_file.unlink()

    try:
        # primeira execucao: cria tabelas e indices
        init_db(db_file)

        conn = get_connection(db_file)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tabelas = {row["name"] for row in cursor.fetchall()}
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%';")
            indices = {row["name"] for row in cursor.fetchall()}
        finally:
            conn.close()

        assert TABELAS_ESPERADAS.issubset(tabelas), f"Tabelas faltando: {TABELAS_ESPERADAS - tabelas}"
        assert INDICES_ESPERADOS.issubset(indices), f"Indices faltando: {INDICES_ESPERADOS - indices}"

        # insere um registro de teste
        conn = get_connection(db_file)
        try:
            with conn:
                conn.execute("""
                    INSERT INTO produtos (id_produto, nome_produto, url_original, valor_original, valor_atual, status)
                    VALUES ('MLB999', 'Produto Teste', 'https://mercadolivre.com.br/item', 50.0, 40.0, 'novo');
                    """)
        finally:
            conn.close()

        # segunda execucao no mesmo banco: nao pode dar erro nem apagar registros
        init_db(db_file)

        conn = get_connection(db_file)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id_produto, status FROM produtos WHERE id_produto='MLB999';")
            row = cursor.fetchone()
        finally:
            conn.close()

        assert row is not None, "Registro foi perdido apos 2a execucao!"
        assert row["id_produto"] == "MLB999"
        assert row["status"] == "novo"
    finally:
        if db_file.exists():
            db_file.unlink()


def test_get_db_path_com_env_customizado():
    # valida que get_db_path respeita DB_PATH customizado apontando para pasta de teste
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco_custom = PASTA_DATA_TESTS / "test_custom_env.db"
    if banco_custom.exists():
        banco_custom.unlink()

    try:
        with patch.dict(os.environ, {"DB_PATH": str(banco_custom)}):
            caminho = get_db_path()
            assert caminho.resolve() == banco_custom.resolve()
            init_db(caminho)
            conn = get_connection(caminho)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT count(*) as total FROM sqlite_master WHERE type='table';")
                total = cursor.fetchone()["total"]
            finally:
                conn.close()
            assert total >= 8
    finally:
        if banco_custom.exists():
            banco_custom.unlink()


if __name__ == "__main__":
    test_init_db_cria_tabelas_e_e_idempotente()
    test_get_db_path_com_env_customizado()
    print("Testes de banco em test_db.py executados com sucesso!")
