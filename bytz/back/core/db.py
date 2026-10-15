# conexao e gerenciamento do banco de dados sqlite do bytz
import os
import sqlite3
from pathlib import Path

PASTA_BACK = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_db_path(custom_path: str | Path | None = None) -> Path:
    # define o caminho do banco respeitando argumento, variavel de ambiente ou padrao local
    if custom_path:
        p = Path(custom_path)
        return p if p.is_absolute() else (PASTA_BACK / p).resolve()
    env_path = os.environ.get("DB_PATH")
    if env_path:
        p = Path(env_path)
        return p if p.is_absolute() else (PASTA_BACK / p).resolve()
    return PASTA_BACK / "data" / "bytz.db"


def get_connection(custom_path: str | Path | None = None) -> sqlite3.Connection:
    # abre conexao com sqlite, ativa chaves estrangeiras e retorna linhas como dicionario
    db_path = get_db_path(custom_path)
    # garante que o diretorio pai do banco exista antes de conectar
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(custom_path: str | Path | None = None) -> None:
    # cria as tabelas e indices a partir do schema.sql de forma idempotente
    db_path = get_db_path(custom_path)
    # assegura que o diretorio de destino exista no disco
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # le o arquivo ddl com as definicoes de tabelas e indices
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()
    # executa o script no banco garantindo fechamento seguro da conexao
    conn = get_connection(db_path)
    try:
        conn.executescript(schema_sql)
    finally:
        conn.close()
