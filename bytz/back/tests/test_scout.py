# testes unitarios e de integracao do estagio 1 (scout: sazonalidade, web, firecrawl, cache e sqlite)
import json
import sys
from datetime import date
from pathlib import Path

import pytest

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from core.db import get_connection, init_db
from scout.fontes import (
    MODELO_FIRECRAWL,
    avaliar_sazonalidade,
    buscar_fonte_publica,
    calcular_data_regra,
    coletar_via_firecrawl,
    consultar_creditos_firecrawl_hoje,
    extrair_titulos_html,
    registrar_consumo_firecrawl,
    salvar_cache_web,
)
from scout.temas import avaliar_temas, filtrar_e_deduplicar_candidatos
from scout.temas import run as run_scout

PASTA_DATA_TESTS = PASTA_BACK / "data" / "tests"


def test_sazonalidade_regras_moveis_e_virada_de_ano():
    # valida calculo de segundo domingo de maio/agosto, ultima sexta de novembro e janela 12-01 a 03-15
    assert calcular_data_regra(2026, "segundo domingo de maio") == date(2026, 5, 10)
    assert calcular_data_regra(2026, "segundo domingo de agosto") == date(2026, 8, 9)
    assert calcular_data_regra(2026, "ultima sexta de novembro") == date(2026, 11, 27)

    # em janeiro deve ativar volta as aulas e verao e fitness (virada de ano 12-01..03-15)
    cand_jan = avaliar_sazonalidade(data_ref=date(2026, 1, 20))
    fontes_jan = {c["fonte"] for c in cand_jan if c["sazonal"]}
    assert "sazonalidade:volta_as_aulas" in fontes_jan
    assert "sazonalidade:verao_e_fitness" in fontes_jan

    # em 15 de novembro (12 dias antes da black friday) deve ativar black friday para todos os nichos
    cand_bf = avaliar_sazonalidade(data_ref=date(2026, 11, 15))
    nichos_bf = {c["nicho"] for c in cand_bf if c["fonte"] == "sazonalidade:black_friday"}
    assert len(nichos_bf) == 6


def test_extrair_titulos_html_e_proibicao_mercado_livre():
    # garante parsing rss/html sem duplicatas e bloqueio imediato de qualquer url do mercado livre
    rss_exemplo = """
    <rss><channel>
      <item><title>Teclado mecanico gamer em promocao</title></item>
      <item><title>Teclado mecanico gamer em promocao</title></item>
      <item><title>Smartwatch com GPS e bateria de 14 dias</title></item>
    </channel></rss>
    """
    titulos = extrair_titulos_html(rss_exemplo, seletor="item > title")
    assert len(titulos) == 2
    assert titulos[0] == "Teclado mecanico gamer em promocao"

    with pytest.raises(ValueError, match="Mercado Livre proibida"):
        buscar_fonte_publica("ml", "https://lista.mercadolivre.com.br/fone")

    with pytest.raises(ValueError, match="Mercado Livre proibida"):
        coletar_via_firecrawl("ml", "https://www.mercadolivre.com.br/ofertas")


def test_firecrawl_registro_modelo_firecrawl_e_teto_20_creditos(monkeypatch):
    # valida registro em gasto_ia com modelo_firecrawl e bloqueio ao atingir 20 creditos no dia
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_scout_firecrawl.db"
    if banco.exists():
        banco.unlink()

    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-chave-teste-ficticia")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": {"html": "<h2>Fone bluetooth com cancelamento de ruido</h2>"}}

    chamadas = {"post": 0}

    def fake_post(url, headers, json, timeout):
        chamadas["post"] += 1
        return FakeResp()

    try:
        itens = coletar_via_firecrawl(
            nome="portal_tech",
            url="https://exemplo-tech.com.br/tendencias",
            seletor="h2",
            custom_db_path=banco,
            http_post=fake_post,
        )
        assert len(itens) == 1
        assert itens[0]["fonte"] == "firecrawl:portal_tech"
        assert chamadas["post"] == 1
        assert consultar_creditos_firecrawl_hoje(custom_db_path=banco) == 1

        # verifica que o registro em gasto_ia usou exatamente o campo modelo_firecrawl
        conn = get_connection(banco)
        try:
            row = conn.execute("SELECT modelo, tokens_in FROM gasto_ia;").fetchone()
            assert row["modelo"] == MODELO_FIRECRAWL
            assert row["tokens_in"] == 1
        finally:
            conn.close()

        # registra mais 19 creditos para atingir o teto diario de 20 e confere que bloqueia nova chamada
        registrar_consumo_firecrawl(creditos=19, custom_db_path=banco)
        assert consultar_creditos_firecrawl_hoje(custom_db_path=banco) == 20

        itens_bloqueados = coletar_via_firecrawl(
            nome="portal_tech",
            url="https://exemplo-tech.com.br/tendencias",
            seletor="h2",
            custom_db_path=banco,
            http_post=fake_post,
        )
        assert itens_bloqueados == []
        assert chamadas["post"] == 1
    finally:
        if banco.exists():
            banco.unlink()


def test_scout_resiliencia_fonte_offline_usa_cache_e_calendario_e_idempotencia():
    # comprova que falha de rede nas fontes web aciona cache do dia anterior + calendario sem parar
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_scout_run.db"
    cache_file = PASTA_DATA_TESTS / "test_scout_cache.json"
    for arq in (banco, cache_file):
        if arq.exists():
            arq.unlink()

    salvar_cache_web(
        [
            {
                "tema": "Monitor gamer 144hz ips",
                "fonte": "web:tecnoblog",
                "nicho": "setup",
                "sazonal": False,
                "peso_base": 9.5,
            }
        ],
        custom_cache_path=cache_file,
    )

    def http_get_offline(url, headers=None, timeout=10):
        raise ConnectionError("Sem internet simulada")

    try:
        res1 = run_scout(
            data_ref=date(2026, 10, 23),
            custom_db_path=banco,
            custom_cache_path=cache_file,
            http_get=http_get_offline,
        )
        assert res1["usou_cache"] is True
        assert len(res1["temas"]) == 15
        assert res1["inseridos"] == 15
        assert any("Monitor gamer 144hz ips" in t["tema"] for t in res1["temas"])

        # segunda execucao no mesmo dia deve ser idempotente (0 inseridos, 15 atualizados)
        res2 = run_scout(
            data_ref=date(2026, 10, 23),
            custom_db_path=banco,
            custom_cache_path=cache_file,
            http_get=http_get_offline,
        )
        assert res2["inseridos"] == 0
        assert res2["atualizados"] == 15

        conn = get_connection(banco)
        try:
            total_temas = conn.execute("SELECT count(*) AS c FROM temas;").fetchone()["c"]
            total_exec = conn.execute(
                "SELECT count(*) AS c FROM execucoes WHERE modulo='scout' AND status='sucesso';"
            ).fetchone()["c"]
        finally:
            conn.close()

        assert total_temas == 15
        assert total_exec == 2
    finally:
        for arq in (banco, cache_file):
            if arq.exists():
                arq.unlink()


def test_scout_ia_json_invalido_estouro_orcamento_e_vetos():
    # valida filtragem de vetos, fallback quando ia retorna json invalido e fallback por orcamento estourado
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_scout_ia.db"
    if banco.exists():
        banco.unlink()

    init_db(banco)
    candidatos = [
        {"tema": "Suplemento termogenico emagrecedor rapido", "fonte": "web:x", "peso_base": 9.9},
        {"tema": "Noticia de novela e futebol ontem", "fonte": "web:x", "peso_base": 9.0},
        {"tema": "Carregador portatil power bank 20000mah", "fonte": "web:tecnoblog", "peso_base": 8.8},
        {"tema": "Lampada inteligente wifi rgb alexa", "fonte": "web:tecnoblog", "peso_base": 8.5},
    ]

    try:
        limpos = filtrar_e_deduplicar_candidatos(candidatos)
        assert len(limpos) == 2

        # 1. json invalido da ia deve cair para o deterministico sem quebrar
        temas_fallback = avaliar_temas(
            candidatos_brutos=candidatos,
            llm_callable=lambda _c: "{json quebrado",
            custom_db_path=banco,
        )
        assert len(temas_fallback) == 2

        # 2. json valido da ia tentando injetar item vetado deve filtrar o item vetado e aprovar o valido
        resposta_ia = json.dumps(
            {
                "temas": [
                    {
                        "tema": "Remedio emagrecedor milagroso",
                        "fonte": "web:x",
                        "nota_tendencia": 9.9,
                        "justificativa": "Vetado",
                    },
                    {
                        "tema": "Carregador portatil power bank turbo",
                        "fonte": "web:tecnoblog",
                        "nota_tendencia": 9.1,
                        "justificativa": "Alta procura para viagens.",
                    },
                ]
            }
        )
        temas_ia = avaliar_temas(
            candidatos_brutos=candidatos,
            llm_callable=lambda _c: (resposta_ia, 100, 50),
            custom_db_path=banco,
        )
        assert len(temas_ia) == 1
        assert temas_ia[0]["tema"] == "Carregador portatil power bank turbo"
    finally:
        if banco.exists():
            banco.unlink()
