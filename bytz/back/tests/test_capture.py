# testes essenciais do parser, filtros e deduplicacao da captacao no sqlite
import sys
from pathlib import Path

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from capture.filtros import filtrar_produtos, passa_filtros
from capture.parser import (
    extrair_id_produto,
    limpar_url,
    parse_html_busca,
    parse_nota,
    parse_preco,
    parse_vendas,
)
from capture.scraper import executar_captacao_termo
from core.db import get_connection

PASTA_FIXTURES = Path(__file__).resolve().parent / "fixtures"
PASTA_DATA_TESTS = PASTA_BACK / "data" / "tests"


def test_parser_funcoes_puras_preco_id_nota_vendas():
    # valida preco com centavos e milhar, nota com virgula, vendas em mil e id nas regras da secao E
    assert parse_preco("299", "90") == 299.90
    assert parse_preco("1.499", "90") == 1499.90
    assert parse_preco("150", None) == 150.0
    assert parse_preco(None, "50") is None

    assert parse_nota("4,8") == 4.8
    assert parse_nota("4.5") == 4.5
    assert parse_nota(None) is None

    assert parse_vendas("| +10 mil vendidos") == 10000
    assert parse_vendas("| +5 mil vendidos") == 5000
    assert parse_vendas("| +500 vendidos") == 500
    assert parse_vendas(None) == 0

    url_catalogo = (
        "https://www.mercadolivre.com.br/fone-jbl/p/MLB22849102?pdp_filters=category:MLB1000#polycard"
    )
    assert extrair_id_produto(url_catalogo) == "MLB22849102"
    assert limpar_url(url_catalogo) == "https://www.mercadolivre.com.br/fone-jbl/p/MLB22849102"

    url_mlbu_com_wid = "https://www.mercadolivre.com.br/fone-qcy/up/MLBU3232389631?item_id=MLB4093699351&wid=MLB4093699351#polycard"
    assert extrair_id_produto(url_mlbu_com_wid) == "MLB4093699351"

    url_fallback_hifen = "https://produto.mercadolivre.com.br/MLB-3519283746-headphone-sony-_JM#polycard"
    assert extrair_id_produto(url_fallback_hifen) == "MLB3519283746"


def test_parser_e_filtros_com_fixture():
    # valida parsing dos 8 cards da fixture e aprovacao apenas dos 5 que cumprem os filtros
    fixture_file = PASTA_FIXTURES / "busca_fone_bluetooth.html"
    html = fixture_file.read_text(encoding="utf-8")

    cards = parse_html_busca(html)
    assert len(cards) == 8

    aprovados = filtrar_produtos(cards)
    assert len(aprovados) == 5

    ids_aprovados = [item["id_produto"] for item in aprovados]
    assert ids_aprovados == [
        "MLB22849102",
        "MLB4093699351",
        "MLB3519283746",
        "MLB19827364",
        "MLB18456721",
    ]

    # confere que card sem valor_original e card mclics sao barrados
    ok_sem_orig, motivo_sem_orig = passa_filtros(cards[5])
    assert ok_sem_orig is False
    assert "valor_original" in motivo_sem_orig

    ok_mclics, motivo_mclics = passa_filtros(cards[7])
    assert ok_mclics is False
    assert "mclics" in motivo_mclics


def test_gravacao_e_deduplicacao_sqlite():
    # valida que rodar a captacao duas vezes com o mesmo lote nao duplica produtos no sqlite
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco_teste = PASTA_DATA_TESTS / "test_capture_dedup.db"
    if banco_teste.exists():
        banco_teste.unlink()

    fixture_file = PASTA_FIXTURES / "busca_fone_bluetooth.html"
    html = fixture_file.read_text(encoding="utf-8")

    try:
        res1 = executar_captacao_termo(
            termo="fone bluetooth",
            custom_db_path=banco_teste,
            html_override=html,
        )
        assert res1["total_cards"] == 8
        assert res1["total_aprovados"] == 5
        assert res1["inseridos"] == 5
        assert res1["ignorados"] == 0

        # segunda execucao com os mesmos itens deve ignorar os 5 ja existentes
        res2 = executar_captacao_termo(
            termo="fone bluetooth",
            custom_db_path=banco_teste,
            html_override=html,
        )
        assert res2["inseridos"] == 0
        assert res2["ignorados"] == 5

        conn = get_connection(banco_teste)
        try:
            total_prod = conn.execute("SELECT count(*) as c FROM produtos;").fetchone()["c"]
            total_hist = conn.execute("SELECT count(*) as c FROM historico_preco;").fetchone()["c"]
            total_exec = conn.execute(
                "SELECT count(*) as c FROM execucoes WHERE modulo='capture';"
            ).fetchone()["c"]
        finally:
            conn.close()

        assert total_prod == 5
        assert total_hist == 8
        assert total_exec == 2
    finally:
        if banco_teste.exists():
            banco_teste.unlink()
