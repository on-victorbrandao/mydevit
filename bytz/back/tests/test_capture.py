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
from capture.scraper import (
    _detectar_captcha_ou_bloqueio,
    carregar_cookie_busca,
    cookies_navegador_para_string,
    cookies_para_navegador,
    executar_captacao_termo,
    merge_cookie,
    parse_cookie_string,
    salvar_cookie_busca_atomico,
)
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
    assert parse_vendas(None) is None

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
    # valida parsing dos 60 cards da fixture real e aprovacao apenas dos 37 que cumprem os filtros
    fixture_file = PASTA_FIXTURES / "busca_real_carregador_portatil.html"
    html = fixture_file.read_text(encoding="utf-8")

    cards = parse_html_busca(html)
    assert len(cards) == 60

    aprovados = filtrar_produtos(cards)
    assert len(aprovados) == 37

    ids_aprovados = [item["id_produto"] for item in aprovados[:5]]
    assert ids_aprovados == [
        "MLB52327865",
        "MLB75714338",
        "MLB56681619",
        "MLB66902915",
        "MLB64084837",
    ]

    # confere que card mclics, card sem valor_original e card com vendas explicitas baixas sao barrados
    ok_mclics, motivo_mclics = passa_filtros(cards[0])
    assert ok_mclics is False
    assert "mclics" in motivo_mclics

    ok_sem_orig, motivo_sem_orig = passa_filtros(cards[30])
    assert ok_sem_orig is False
    assert "valor_original" in motivo_sem_orig

    card_vendas_baixas = dict(aprovados[0], vendas=100)
    ok_vendas, motivo_vendas = passa_filtros(card_vendas_baixas)
    assert ok_vendas is False
    assert "vendas" in motivo_vendas


def test_gravacao_e_deduplicacao_sqlite():
    # valida que rodar a captacao duas vezes com o mesmo arquivo html nao duplica produtos no sqlite
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco_teste = PASTA_DATA_TESTS / "test_capture_dedup.db"
    if banco_teste.exists():
        banco_teste.unlink()

    fixture_file = PASTA_FIXTURES / "busca_real_carregador_portatil.html"

    try:
        res1 = executar_captacao_termo(
            termo="carregador portatil",
            custom_db_path=banco_teste,
            arquivo_html_path=fixture_file,
        )
        assert res1["total_cards"] == 60
        assert res1["total_aprovados"] == 37
        assert res1["inseridos"] == 37
        assert res1["ignorados"] == 0

        # segunda execucao com os mesmos itens deve ignorar os 37 ja existentes
        res2 = executar_captacao_termo(
            termo="carregador portatil",
            custom_db_path=banco_teste,
            arquivo_html_path=fixture_file,
        )
        assert res2["inseridos"] == 0
        assert res2["ignorados"] == 37

        conn = get_connection(banco_teste)
        try:
            total_prod = conn.execute("SELECT count(*) as c FROM produtos;").fetchone()["c"]
            total_hist = conn.execute("SELECT count(*) as c FROM historico_preco;").fetchone()["c"]
            total_exec = conn.execute(
                "SELECT count(*) as c FROM execucoes WHERE modulo='capture';"
            ).fetchone()["c"]
        finally:
            conn.close()

        assert total_prod == 37
        assert total_hist == 60
        assert total_exec == 2
    finally:
        if banco_teste.exists():
            banco_teste.unlink()


def test_cookie_busca_merge_e_persistencia_atomica():
    # valida parse_cookie_string, merge_cookie e escrita atomica do cookie de busca isolado
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    cookie_teste = PASTA_DATA_TESTS / "test_ml_cookies_busca.json"
    if cookie_teste.exists():
        cookie_teste.unlink()

    try:
        jar = parse_cookie_string("a=1; b=2; a=3")
        assert jar == {"a": "3", "b": "2"}

        atualizado, csrf = merge_cookie(
            "ssid=antigo; _csrf=token1",
            ["ssid=novo; Path=/; HttpOnly", "_csrf=token2; Secure"],
        )
        assert "ssid=novo" in atualizado
        assert csrf == "token2"

        salvar_cookie_busca_atomico(atualizado, custom_cookie_path=cookie_teste)
        lido = carregar_cookie_busca(custom_cookie_path=cookie_teste)
        assert lido == atualizado
    finally:
        if cookie_teste.exists():
            cookie_teste.unlink()


def test_cookies_do_chrome_e_trava_de_bloqueio():
    # valida a conversao de cookies para o chrome, a volta via merge_cookie e a trava sem vazar query da url
    convertidos = cookies_para_navegador("ssid=abc; __Host-x=1")
    ssid = next(c for c in convertidos if c["name"] == "ssid")
    host = next(c for c in convertidos if c["name"] == "__Host-x")
    assert ssid["domain"] == ".mercadolivre.com.br"
    assert ssid["secure"] is True
    assert "domain" not in host and host["url"].startswith("https://")

    renovado = cookies_navegador_para_string("ssid=abc; cp=1", [{"name": "ssid", "value": "novo"}])
    assert parse_cookie_string(renovado) == {"ssid": "novo", "cp": "1"}

    motivo = _detectar_captcha_ou_bloqueio(
        200, "https://www.mercadolivre.com.br/captcha/wall/logged?tid=123", ""
    )
    assert motivo is not None and "tid=" not in motivo
    assert _detectar_captcha_ou_bloqueio(429, "https://lista.mercadolivre.com.br/x", "") == "HTTP 429"
    assert _detectar_captcha_ou_bloqueio(200, "https://lista.mercadolivre.com.br/x", "") is None
