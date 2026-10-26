# testes do modulo de fila manual e plano b da ativacao
import sqlite3

from activation.fallback_queue import (
    exportar_candidatos_fila,
    importar_links_fila,
    validar_link_afiliado_url,
    verificar_e_acionar_plano_b,
)
from core.db import init_db


def test_validar_link_afiliado_url():
    # valida se o parser de url reconhece dominios oficiais do mercado livre em https
    assert validar_link_afiliado_url("https://mercadolivre.com/sec/abc1234") is True
    assert validar_link_afiliado_url("https://www.mercadolivre.com.br/sec/xyz987") is True
    assert validar_link_afiliado_url("https://ml.com.br/sec/test") is True
    assert validar_link_afiliado_url("https://meli.la/abc") is True

    # rejeita links sem https, dominios estranhos, hosts parecidos ou paths vazios
    assert validar_link_afiliado_url("http://mercadolivre.com/sec/abc") is False
    assert validar_link_afiliado_url("http://meli.la/abc") is False
    assert validar_link_afiliado_url("https://google.com/search") is False
    assert validar_link_afiliado_url("https://fake-meli.la/abc") is False
    assert validar_link_afiliado_url("https://sub.meli.la/abc") is False
    assert validar_link_afiliado_url("https://meli.la.evil.com/abc") is False
    assert validar_link_afiliado_url("https://mercadolivre.com.br.evil.com/sec/abc") is False
    assert validar_link_afiliado_url("https://meli.la@evil.com/abc") is False
    assert validar_link_afiliado_url("https://mercadolivre.com.br") is False
    assert validar_link_afiliado_url("https://meli.la/") is False
    assert validar_link_afiliado_url("") is False


def test_exportar_e_importar_fila_manual(tmp_path):
    # cria banco temporario com produtos curados e valida exportacao e importacao
    db_file = tmp_path / "test_queue.db"
    init_db(db_file)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row

    conn.execute("""
        INSERT INTO produtos (id_produto, nome_produto, url_original, status, score)
        VALUES
            ('MLB100', 'Produto 100', 'https://www.mercadolivre.com.br/p/MLB100', 'curado', 90.0),
            ('MLB200', 'Produto 200', 'https://www.mercadolivre.com.br/p/MLB200', 'curado', 85.0)
        """)
    conn.commit()

    produtos = [
        {
            "id_produto": "MLB100",
            "nome_produto": "Produto 100",
            "score": 90.0,
            "url_original": "https://www.mercadolivre.com.br/p/MLB100",
        },
        {
            "id_produto": "MLB200",
            "nome_produto": "Produto 200",
            "score": 85.0,
            "url_original": "https://www.mercadolivre.com.br/p/MLB200",
        },
    ]

    arquivo_csv = tmp_path / "fila_teste.csv"
    exportar_candidatos_fila(produtos, arquivo_csv)
    assert arquivo_csv.exists()

    # simula o preenchimento manual pelo dono no csv
    conteudo_preenchido = (
        "id_produto,nome_produto,score,url_original,url_afiliado\n"
        "MLB100,Produto 100,90.0,https://www.mercadolivre.com.br/p/MLB100,https://mercadolivre.com/sec/mlb100\n"
        "MLB200,Produto 200,85.0,https://www.mercadolivre.com.br/p/MLB200,invalido_sem_https\n"
    )
    arquivo_csv.write_text(conteudo_preenchido, encoding="utf-8")

    res = importar_links_fila(arquivo_csv, conn=conn)
    assert res["importados"] == 1
    assert res["invalidos"] == 1

    # confere se o produto 100 foi promovido a link_pronto no sqlite
    p1 = conn.execute("SELECT status, url_afiliado FROM produtos WHERE id_produto = 'MLB100'").fetchone()
    assert p1["status"] == "link_pronto"
    assert p1["url_afiliado"] == "https://mercadolivre.com/sec/mlb100"

    # confere que o produto 200 permaneceu curado
    p2 = conn.execute("SELECT status, url_afiliado FROM produtos WHERE id_produto = 'MLB200'").fetchone()
    assert p2["status"] == "curado"
    assert p2["url_afiliado"] is None
    conn.close()


def test_verificar_e_acionar_plano_b(tmp_path, monkeypatch):
    # valida que o plano b e acionado quando os links prontos estao abaixo do limite
    db_file = tmp_path / "test_pb.db"
    init_db(db_file)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row

    # insere 2 curados e nenhum link_pronto
    conn.execute("""
        INSERT INTO produtos (id_produto, nome_produto, url_original, status, score)
        VALUES
            ('MLB1', 'Item 1', 'https://www.mercadolivre.com.br/p/MLB1', 'curado', 88.0),
            ('MLB2', 'Item 2', 'https://www.mercadolivre.com.br/p/MLB2', 'curado', 80.0)
        """)
    conn.commit()

    res = verificar_e_acionar_plano_b(limite_minimo=4, conn=conn)
    assert res["acionado"] is True
    assert res["qtd_prontos"] == 0
    assert res["candidatos_exportados"] == 2

    # insere 4 produtos com link_pronto e confere que o plano b nao e mais acionado
    for i in range(10, 14):
        conn.execute(
            f"INSERT INTO produtos (id_produto, nome_produto, url_original, url_afiliado, status) VALUES ('MLB{i}', 'Item {i}', 'url', 'https://mercadolivre.com/sec/link', 'link_pronto')"
        )
    conn.commit()

    res2 = verificar_e_acionar_plano_b(limite_minimo=4, conn=conn)
    assert res2["acionado"] is False
    assert res2["qtd_prontos"] == 4
    conn.close()
