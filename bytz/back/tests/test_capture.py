# testes essenciais do parser, filtros e deduplicacao da captacao no sqlite
import sys
from datetime import date
from pathlib import Path

import pytest

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
    BloqueioCaptacaoError,
    _detectar_captcha_ou_bloqueio,
    calcular_atualizacao_busca,
    carregar_cookie_busca,
    cookies_navegador_para_string,
    cookies_para_navegador,
    executar_captacao_termo,
    merge_cookie,
    parse_cookie_string,
    salvar_cookie_busca_atomico,
    selecionar_buscas_elegiveis,
)
from capture.scraper import run as run_capture
from core.db import get_connection, init_db
from scripts import run_stage

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


def test_selecionar_buscas_elegiveis_e_calcular_atualizacao_busca():
    # valida filtragem pura de buscas ativas (vetos, max_buscas_dia, idempotencia) e calculo de aposentadoria
    registros = [
        {"id": 1, "termo": "fone bluetooth", "nicho": "eletronicos", "prioridade": 9, "ativo": 1},
        {"id": 2, "termo": "emagrecedor termogenico", "nicho": "fitness", "prioridade": 10, "ativo": 1},
        {"id": 3, "termo": "teclado mecanico", "nicho": "setup", "prioridade": 7, "ativo": 0},
        {
            "id": 4,
            "termo": "mouse gamer",
            "nicho": "setup",
            "prioridade": 8,
            "ativo": 1,
            "ultima_execucao": "2026-10-23T06:00:00-03:00",
        },
        {"id": 5, "termo": "webcam full hd", "nicho": "home-office", "prioridade": 6, "ativo": 1},
    ]

    # como id=4 ja rodou hoje (2026-10-23) e max_buscas_dia=2, resta apenas 1 vaga (id=1)
    elegiveis = selecionar_buscas_elegiveis(
        registros_buscas=registros,
        max_buscas_dia=2,
        hoje_str="2026-10-23",
        pular_executadas_hoje=True,
    )
    assert [b["id"] for b in elegiveis] == [1]

    # testa calculo puro de sucesso (zera falhas consecutivas) e de 3a falha seguida (aposenta por 14 dias)
    upd_ok = calcular_atualizacao_busca(
        {"execucoes": 2, "execucoes_sem_aprovado": 2, "resultados_total": 50, "aprovados_total": 5},
        total_cards=60,
        total_aprovados=10,
        agora_iso="2026-10-23T06:05:00-03:00",
        data_ref=date(2026, 10, 23),
    )
    assert upd_ok["execucoes"] == 3
    assert upd_ok["execucoes_sem_aprovado"] == 0
    assert upd_ok["ativo"] == 1
    assert upd_ok["inativo_ate"] is None
    assert upd_ok["aprovados_total"] == 15

    upd_falha3 = calcular_atualizacao_busca(
        {"execucoes": 2, "execucoes_sem_aprovado": 2, "resultados_total": 40, "aprovados_total": 0},
        total_cards=20,
        total_aprovados=0,
        agora_iso="2026-10-23T06:05:00-03:00",
        data_ref=date(2026, 10, 23),
    )
    assert upd_falha3["execucoes"] == 3
    assert upd_falha3["execucoes_sem_aprovado"] == 3
    assert upd_falha3["ativo"] == 0
    assert upd_falha3["inativo_ate"] == "2026-11-06"


def test_capture_run_integra_tabela_buscas_idempotente_e_aposenta():
    # valida integracao completa de capture.scraper.run com a tabela buscas, produtos e execucoes
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_capture_buscas_run.db"
    if banco.exists():
        banco.unlink()

    fixture_html = (PASTA_FIXTURES / "busca_real_carregador_portatil.html").read_text(encoding="utf-8")
    # html com 1 card mas sem desconto nem vendas suficientes (0 aprovados nos filtros)
    html_sem_aprovado = """
    <html><body>
      <div class="poly-card">
        <a class="poly-component__title" href="https://www.mercadolivre.com.br/cabo/p/MLB99999999">Cabo simples</a>
        <div class="poly-price__current"><span class="andes-money-amount__fraction">50</span></div>
      </div>
    </body></html>
    """

    init_db(banco)
    conn = get_connection(banco)
    try:
        with conn:
            conn.execute("""
                INSERT INTO temas (id, data, tema, fonte, nota_tendencia, justificativa, usado)
                VALUES (1, '2026-10-23', 'Energia portatil para viagens', 'sazonalidade:base', 9.0, 'Teste', 1);
                """)
            conn.execute("""
                INSERT INTO buscas (id, termo, tema_id, nicho, prioridade, ativo, execucoes, execucoes_sem_aprovado)
                VALUES
                  (1, 'carregador portatil', 1, 'acessorios moveis', 9, 1, 0, 0),
                  (2, 'cabo sem oferta', 1, 'acessorios moveis', 6, 1, 2, 2),
                  (3, 'remedio emagrecedor proibido', 1, 'fitness', 10, 1, 0, 0);
                """)
    finally:
        conn.close()

    try:
        res1 = run_capture(
            data_ref=date(2026, 10, 23),
            custom_db_path=banco,
            html_por_termo={
                "carregador portatil": fixture_html,
                "cabo sem oferta": html_sem_aprovado,
            },
        )
        assert res1["termos_executados"] == 2
        assert res1["inseridos"] == 37
        assert res1["aposentados"] == 1

        # segunda chamada no mesmo dia deve ser idempotente (nao reexecuta buscas ja rodadas hoje)
        res2 = run_capture(
            data_ref=date(2026, 10, 23),
            custom_db_path=banco,
            html_por_termo={"carregador portatil": fixture_html},
        )
        assert res2["termos_executados"] == 0
        assert res2["inseridos"] == 0

        conn = get_connection(banco)
        try:
            b1 = conn.execute("SELECT * FROM buscas WHERE id = 1;").fetchone()
            b2 = conn.execute("SELECT * FROM buscas WHERE id = 2;").fetchone()
            b3 = conn.execute("SELECT * FROM buscas WHERE id = 3;").fetchone()
            prod = conn.execute("SELECT categoria, tema_origem FROM produtos LIMIT 1;").fetchone()
            execs = conn.execute(
                "SELECT count(*) AS c FROM execucoes WHERE modulo = 'capture' AND status = 'sucesso';"
            ).fetchone()["c"]
        finally:
            conn.close()

        assert b1["execucoes"] == 1
        assert b1["aprovados_total"] == 37
        assert b1["execucoes_sem_aprovado"] == 0
        assert b1["ativo"] == 1
        assert b1["ultima_execucao"].startswith("2026-10-23")

        assert b2["execucoes"] == 3
        assert b2["execucoes_sem_aprovado"] == 3
        assert b2["ativo"] == 0
        assert b2["inativo_ate"] == "2026-11-06"

        # termo vetado foi desativado sem ser executado
        assert b3["ativo"] == 0
        assert b3["execucoes"] == 0

        assert prod["categoria"] == "acessorios moveis"
        assert prod["tema_origem"] == "Energia portatil para viagens"
        assert execs == 2
    finally:
        if banco.exists():
            banco.unlink()


def test_capture_run_para_na_1a_tentativa_em_bloqueio_e_registra_bloqueado(monkeypatch):
    # garante que em 403, 429, captcha ou account-verification para na 1a tentativa sem tentar o proximo termo
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_capture_bloqueio.db"
    cookie_file = PASTA_DATA_TESTS / "test_cookie_bloqueio.json"
    for arq in (banco, cookie_file):
        if arq.exists():
            arq.unlink()

    salvar_cookie_busca_atomico("ssid=teste; _csrf=abc", custom_cookie_path=cookie_file)
    init_db(banco)
    conn = get_connection(banco)
    try:
        with conn:
            conn.execute("""
                INSERT INTO buscas (id, termo, nicho, prioridade, ativo)
                VALUES
                  (1, 'fone bluetooth', 'eletronicos', 9, 1),
                  (2, 'mouse gamer', 'setup', 8, 1);
                """)
    finally:
        conn.close()

    tentativas_chrome = {"count": 0}
    alertas_disparados: list[dict] = []

    def fake_abrir_chrome(url, cookie_atual, cfg):
        tentativas_chrome["count"] += 1
        return (
            200,
            "https://www.mercadolivre.com.br/gz/account-verification?go=x",
            "<html>captcha</html>",
            [],
        )

    def fake_alert(msg, nivel="warning", detalhes=None, **kwargs):
        alertas_disparados.append({"msg": msg, "nivel": nivel, "detalhes": detalhes})
        return True

    monkeypatch.setattr("capture.scraper._abrir_busca_no_chrome", fake_abrir_chrome)
    monkeypatch.setattr("capture.scraper.send_alert", fake_alert)

    try:
        with pytest.raises(BloqueioCaptacaoError):
            run_capture(
                data_ref=date(2026, 10, 23),
                custom_db_path=banco,
                custom_cookie_path=cookie_file,
                max_retries_override=2,
            )

        # mesmo com retries=2 e 2 termos na fila, parou na 1a tentativa do 1o termo
        assert tentativas_chrome["count"] == 1
        assert len(alertas_disparados) == 1
        assert alertas_disparados[0]["nivel"] == "critico"

        conn = get_connection(banco)
        try:
            row_exec = conn.execute(
                "SELECT status, erro FROM execucoes WHERE modulo = 'capture' ORDER BY id DESC LIMIT 1;"
            ).fetchone()
        finally:
            conn.close()

        assert row_exec is not None
        assert row_exec["status"] == "bloqueado"
        assert "account-verification" in row_exec["erro"]
    finally:
        for arq in (banco, cookie_file):
            if arq.exists():
                arq.unlink()


def test_run_stage_capture_sem_termo_consome_buscas(monkeypatch, capsys):
    # valida que o cli run_stage.py capture sem --termo aciona o lote da tabela buscas
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_cli_capture_buscas.db"
    if banco.exists():
        banco.unlink()

    monkeypatch.setenv("DB_PATH", str(banco))
    init_db(banco)
    conn = get_connection(banco)
    try:
        with conn:
            conn.execute(
                "INSERT INTO buscas (termo, nicho, prioridade, ativo) VALUES ('carregador portatil', 'acessorios moveis', 9, 1);"
            )
    finally:
        conn.close()

    fixture_file = PASTA_FIXTURES / "busca_real_carregador_portatil.html"
    try:
        rc = run_stage.main(["capture", "--arquivo", str(fixture_file), "--max-buscas", "1"])
        saida = capsys.readouterr().out
        assert rc == 0
        assert "Termos executados da tabela buscas: 1" in saida
        assert "Novos inseridos no SQLite: 37" in saida
    finally:
        if banco.exists():
            banco.unlink()
