# testes unitarios e de integracao do estagio 4 (curador: score, casos limite, juiz llm, cache e sqlite)
import json
import sys
from datetime import date
from pathlib import Path

import pytest

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from core.budget import consultar_gasto_mes, registrar_gasto
from core.db import get_connection, init_db
from curator.llm_judge import (
    OrcamentoEstouradoError,
    avaliar_lote_deterministico,
    detectar_duplicatas_no_lote,
    detectar_motivo_suspeito,
    gerar_chave_cache_juiz,
    julgar_produtos_em_lote,
)
from curator.score import (
    aplicar_regras_variedade,
    calcular_desconto_real_pct,
    calcular_pontos_demanda,
    calcular_pontos_desconto,
    calcular_pontos_reputacao,
    calcular_pontos_ticket,
    classificar_status_por_score,
    intercalar_por_categoria,
    validar_pesos_score,
)
from curator.score import run as run_curator
from scripts import run_stage

PASTA_DATA_TESTS = PASTA_BACK / "data" / "tests"


def test_casos_limite_score_sem_historico_desconto_negativo_e_nota_45_exata():
    # valida sem historico (fator 0.6), mediana 30d, desconto negativo (0 pts) e nota 4.5 exata (0 pts)
    # 1. sem historico: usa preco riscado (200 -> 150 = 25%) com fator de confianca 0.6 -> 15.0%
    desc_sem_hist = calcular_desconto_real_pct(150.0, 200.0, precos_historico_30d=[])
    assert desc_sem_hist == 15.0
    pts_sem_hist = calcular_pontos_desconto(150.0, 200.0, precos_historico_30d=[], peso_max=25.0)
    assert pts_sem_hist == round((15.0 / 35.0) * 25.0, 2)

    # 2. com historico de 30 dias: usa mediana (mediana de [190, 200, 210] = 200; atual 140 -> 30%)
    desc_com_hist = calcular_desconto_real_pct(140.0, 300.0, precos_historico_30d=[190.0, 200.0, 210.0])
    assert desc_com_hist == 30.0
    pts_com_hist = calcular_pontos_desconto(
        140.0, 300.0, precos_historico_30d=[190.0, 200.0, 210.0], peso_max=25.0
    )
    assert pts_com_hist > pts_sem_hist

    # 3. desconto negativo (preco atual maior que mediana 30d ou maior que valor_original) -> 0 pontos
    desc_neg_hist = calcular_desconto_real_pct(120.0, 200.0, precos_historico_30d=[100.0, 105.0, 95.0])
    assert desc_neg_hist < 0
    assert calcular_pontos_desconto(120.0, 200.0, precos_historico_30d=[100.0, 105.0, 95.0]) == 0.0
    assert calcular_pontos_desconto(220.0, 200.0, precos_historico_30d=[]) == 0.0

    # 4. nota 4.5 exata vale 0.0 em reputacao; nota 5.0 com alto volume vale 15.0
    assert calcular_pontos_reputacao(4.5, vendas=10000, peso_max=15.0) == 0.0
    assert calcular_pontos_reputacao(4.4, vendas=10000, peso_max=15.0) == 0.0
    assert calcular_pontos_reputacao(5.0, vendas=10000, peso_max=15.0) == 15.0
    assert 0.0 < calcular_pontos_reputacao(4.8, vendas=1000, peso_max=15.0) < 15.0

    # 5. demanda em log-escala e bonus de crescimento entre capturas
    dem_base = calcular_pontos_demanda(1000, vendas_anterior=1000, peso_max=20.0)
    dem_cresc = calcular_pontos_demanda(1500, vendas_anterior=1000, peso_max=20.0)
    assert dem_cresc > dem_base

    # 6. ticket com e sem comissao explicita por categoria
    cfg_com = {
        "padrao_pct": None,
        "categorias": {"eletronicos": 12.0},
        "faixas_preco_brl": [50, 150, 400, 1000],
    }
    assert calcular_pontos_ticket(200.0, categoria="eletronicos", cfg_comissao=cfg_com) == 10.0
    assert calcular_pontos_ticket(30.0, categoria="eletronicos", cfg_comissao=cfg_com) < 10.0


def test_validacao_pesos_limite_10_porcento_e_cortes_status():
    # garante bloqueio de alteracao de peso > 10% sem aprovacao e valida cortes 65 e 50
    pesos_ok = {
        "desconto": 26.0,
        "demanda": 19.0,
        "reputacao": 15.0,
        "afinidade": 15.0,
        "tendencia": 10.0,
        "ticket": 10.0,
        "frescor": 5.0,
    }
    validados = validar_pesos_score(pesos_ok)
    assert validados["desconto"] == 26.0

    pesos_abusivos = dict(pesos_ok, desconto=30.0)
    with pytest.raises(ValueError, match="excede o limite"):
        validar_pesos_score(pesos_abusivos, permitir_desvio_maior=False)

    assert classificar_status_por_score(65.0) == "curado"
    assert classificar_status_por_score(64.99) == "reserva"
    assert classificar_status_por_score(50.0) == "reserva"
    assert classificar_status_por_score(49.99) == "descartado"
    assert classificar_status_por_score(85.0, suspeito=True) == "descartado"
    assert classificar_status_por_score(85.0, duplicado_de="MLB1") == "descartado"


def test_variedade_max_3_por_termo_dia_e_intercalacao_de_categorias():
    # garante no maximo 3 curados por termo_origem no dia (4o vira reserva) e categorias alternadas
    candidatos = [
        {
            "id_produto": f"MLB{i}",
            "score": 80.0 - i,
            "status": "curado",
            "termo_origem": "carregador portatil",
            "categoria": "acessorios moveis" if i % 2 == 0 else "eletronicos",
        }
        for i in range(5)
    ]
    resultado = aplicar_regras_variedade(candidatos, max_por_termo_dia=3)
    curados = [x for x in resultado if x["status"] == "curado"]
    reservas = [x for x in resultado if x["status"] == "reserva"]

    assert len(curados) == 3
    assert len(reservas) == 2
    assert all("variedade" in str(r.get("observacao") or "").lower() for r in reservas)

    # valida intercalacao para nao repetir categoria seguida quando ha opcoes
    itens_cat = [
        {"id_produto": "A1", "score": 90.0, "categoria": "setup"},
        {"id_produto": "A2", "score": 88.0, "categoria": "setup"},
        {"id_produto": "B1", "score": 85.0, "categoria": "fitness"},
    ]
    intercalados = intercalar_por_categoria(itens_cat)
    assert [x["id_produto"] for x in intercalados] == ["A1", "B1", "A2"]


def test_juiz_llm_em_lote_ate_20_cache_suspeitos_duplicados_e_orcamento(monkeypatch):
    # valida lote de 20 itens, cache por produto+termo, deteccao de suspeito e controle de orcamento/alerta 80%
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr("curator.llm_judge.send_alert", lambda msg, **kw: True)
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_curator_juiz.db"
    cache_file = PASTA_DATA_TESTS / "test_curator_cache.json"
    for arq in (banco, cache_file):
        if arq.exists():
            arq.unlink()

    init_db(banco)

    # cria 25 produtos para comprovar que sao divididos em 2 lotes (20 + 5)
    produtos_25 = [
        {
            "id_produto": f"MLB{1000 + i}",
            "nome_produto": f"Fone Bluetooth TWS Modelo {i} Cancelamento Ruido",
            "termo_origem": "fone bluetooth",
            "categoria": "eletronicos",
            "valor_original": 200.0,
            "valor_atual": 120.0 + i,
            "desconto_pct": 40.0,
            "nota": 4.8,
            "vendas": 1000,
        }
        for i in range(25)
    ]
    # transforma o item 0 em suspeito por promessa medica/veto
    produtos_25[0]["nome_produto"] = "Pulseira Magnetica Emagrecedor Termogenico Milagroso"

    tamanhos_lotes_chamados: list[int] = []

    def fake_llm_lote(lote: list[dict]) -> tuple[str, int, int]:
        tamanhos_lotes_chamados.append(len(lote))
        avaliacoes = [
            {
                "id_produto": p["id_produto"],
                "afinidade": 13.5,
                "suspeito": False,
                "motivo": "Bom produto para o canal.",
                "duplicado_de": None,
            }
            for p in lote
        ]
        return json.dumps({"avaliacoes": avaliacoes}), 800, 300

    cfg_ia_teste = {
        "modelo": "gemini-2.5-flash",
        "teto_mensal_brl": 50.0,
        "alerta_pct": 80.0,
        "max_tokens_saida": 600,
        "cotacao_usd_brl": 6.0,
        "modelos": {"gemini-2.5-flash": {"entrada_usd_1m": 0.30, "saida_usd_1m": 2.50}},
    }

    try:
        res1 = julgar_produtos_em_lote(
            produtos=produtos_25,
            custom_db_path=banco,
            custom_cache_path=cache_file,
            config_ia_override=cfg_ia_teste,
            llm_callable=fake_llm_lote,
            usar_cache=True,
        )
        assert tamanhos_lotes_chamados == [20, 5]
        assert res1["chamadas_ia"] == 2
        assert res1["cache_hits"] == 0
        assert res1["tokens_in"] == 1600
        assert res1["tokens_out"] == 600
        assert res1["custo_brl"] > 0

        # mesmo a ia dizendo suspeito=False, a trava deterministica barrou o item 0 (emagrecedor)
        assert res1["avaliacoes"]["MLB1000"]["suspeito"] is True
        assert res1["avaliacoes"]["MLB1000"]["afinidade"] == 0.0
        assert res1["avaliacoes"]["MLB1001"]["suspeito"] is False
        assert res1["avaliacoes"]["MLB1001"]["afinidade"] == 13.5

        # segunda chamada com os mesmos produtos+termo deve usar 100% cache (0 chamadas a IA)
        res2 = julgar_produtos_em_lote(
            produtos=produtos_25,
            custom_db_path=banco,
            custom_cache_path=cache_file,
            config_ia_override=cfg_ia_teste,
            llm_callable=fake_llm_lote,
            usar_cache=True,
        )
        assert res2["chamadas_ia"] == 0
        assert res2["cache_hits"] == 25

        # testa alerta de 80% e bloqueio quando o custo projetado passa do teto de R$ 50
        alertas: list[str] = []
        monkeypatch.setattr("curator.llm_judge.send_alert", lambda msg, **kw: alertas.append(msg) or True)

        # adiciona gasto acumulado para deixar acima de 80% (R$ 41,00) e faz 1 chamada nova
        registrar_gasto(
            "gemini-2.5-flash", 10_000_000, 1_530_000, custom_db_path=banco, config_override=cfg_ia_teste
        )
        info_80 = consultar_gasto_mes(custom_db_path=banco, config_override=cfg_ia_teste)
        assert info_80["total_brl"] >= 40.0

        prod_novo = [
            {
                "id_produto": "MLB9999",
                "nome_produto": "Mouse Gamer Sem Fio 16000 DPI",
                "termo_origem": "mouse gamer",
                "valor_original": 250.0,
                "valor_atual": 150.0,
                "desconto_pct": 40.0,
                "nota": 4.9,
                "vendas": 5000,
            }
        ]
        res_80 = julgar_produtos_em_lote(
            produtos=prod_novo,
            custom_db_path=banco,
            custom_cache_path=cache_file,
            config_ia_override=cfg_ia_teste,
            llm_callable=fake_llm_lote,
            usar_cache=False,
        )
        assert res_80["alerta_80"] is True
        assert any("80" in a or "teto mensal" in a for a in alertas)

        # agora simula teto quase estourado (R$ 49,9999) onde o custo projetado ultrapassa R$ 50,00
        cfg_teto_baixo = dict(cfg_ia_teste, teto_mensal_brl=info_80["total_brl"] + 0.0001)
        with pytest.raises(OrcamentoEstouradoError):
            julgar_produtos_em_lote(
                produtos=prod_novo,
                custom_db_path=banco,
                custom_cache_path=cache_file,
                config_ia_override=cfg_teto_baixo,
                llm_callable=fake_llm_lote,
                usar_cache=False,
                parar_ao_estourar=True,
            )
    finally:
        for arq in (banco, cache_file):
            if arq.exists():
                arq.unlink()


def test_deteccao_suspeitos_powerbank_50000mah_e_duplicatas_no_lote():
    # valida que power bank 50.000mAh barato e marcado como suspeito e anuncios identicos como duplicados
    pb_suspeito = {
        "id_produto": "MLB6201092326",
        "nome_produto": "Carregador Portatil Power Bank Solar Turbo 50.000mah Rapido",
        "valor_original": 186.25,
        "valor_atual": 143.41,
        "desconto_pct": 23.0,
    }
    motivo = detectar_motivo_suspeito(pb_suspeito)
    assert motivo is not None
    assert "50.000mAh" in motivo

    lote_dup = [
        {
            "id_produto": "MLB10",
            "nome_produto": "Carregador Portatil Power Bank Basike 20000mAh 22.5W Cabos Embutidos Preto",
            "valor_atual": 150.0,
            "nota": 4.9,
            "vendas": 10000,
            "desconto_pct": 35.0,
        },
        {
            "id_produto": "MLB11",
            "nome_produto": "Carregador Portatil Power Bank Basike 20000mAh 22.5W Cabos Embutidos Branco",
            "valor_atual": 155.0,
            "nota": 4.7,
            "vendas": 1000,
            "desconto_pct": 30.0,
        },
    ]
    dups = detectar_duplicatas_no_lote(lote_dup)
    assert dups == {"MLB11": "MLB10"}
    av_det = avaliar_lote_deterministico(lote_dup)
    assert av_det["MLB11"]["duplicado_de"] == "MLB10"
    assert gerar_chave_cache_juiz("mlb10", "Carregador Portátil") == "MLB10::carregador portatil"


def test_curator_run_sqlite_idempotente_historico_cortes_e_cli(monkeypatch, capsys):
    # valida o ciclo completo de curator.score.run no sqlite e na cli run_stage.py curator
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_curator_run.db"
    cache_file = PASTA_DATA_TESTS / "test_curator_run_cache.json"
    for arq in (banco, cache_file):
        if arq.exists():
            arq.unlink()

    init_db(banco)
    conn = get_connection(banco)
    try:
        with conn:
            conn.execute("""
                INSERT INTO temas (id, data, tema, fonte, nota_tendencia, justificativa, usado)
                VALUES (1, '2026-10-23', 'Energia portatil e setup', 'web:tecnoblog', 9.0, 'Alta', 1);
                """)
            conn.execute("""
                INSERT INTO buscas (id, termo, tema_id, nicho, prioridade, ativo, aprovados_total)
                VALUES (1, 'carregador portatil', 1, 'acessorios moveis', 9, 1, 5);
                """)
            # insere historico de 30 dias para MLB_TOP comprovando uso da mediana (mediana = 250.0)
            conn.executemany(
                "INSERT INTO historico_preco (id_produto, data, valor_atual, valor_original) VALUES (?, ?, ?, ?);",
                [
                    ("MLB_TOP", "2026-10-05", 250.0, 300.0),
                    ("MLB_TOP", "2026-10-12", 260.0, 300.0),
                    ("MLB_TOP", "2026-10-18", 240.0, 300.0),
                ],
            )
            # insere 3 produtos novo: 1 excelente (curado), 1 intermediario (reserva) e 1 suspeito (descartado)
            conn.executemany(
                """
                INSERT INTO produtos (
                    id_produto, nome_produto, categoria, imagem_url, url_original,
                    valor_original, valor_atual, desconto_pct, nota, vendas,
                    termo_origem, status, data_captura
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'novo', '2026-10-23');
                """,
                [
                    (
                        "MLB_TOP",
                        "Carregador Portatil Power Bank Baseus 20000mAh 22.5W Display Digital",
                        "acessorios moveis",
                        "https://http2.mlstatic.com/D_1.jpg",
                        "https://www.mercadolivre.com.br/p/MLB_TOP",
                        300.0,
                        150.0,
                        50.0,
                        4.9,
                        10000,
                        "carregador portatil",
                    ),
                    (
                        "MLB_RES",
                        "Cabo USB C Carga Rapida 60W Nylon Trancado 1 Metro",
                        "acessorios moveis",
                        "https://http2.mlstatic.com/D_2.jpg",
                        "https://www.mercadolivre.com.br/p/MLB_RES",
                        70.0,
                        55.0,
                        21.4,
                        4.6,
                        500,
                        "carregador portatil",
                    ),
                    (
                        "MLB_SUSP",
                        "Carregador Portatil Power Bank Solar Turbo 50.000mah Rapido",
                        "acessorios moveis",
                        "https://http2.mlstatic.com/D_3.jpg",
                        "https://www.mercadolivre.com.br/p/MLB_SUSP",
                        186.25,
                        143.41,
                        23.0,
                        4.7,
                        5000,
                        "carregador portatil",
                    ),
                ],
            )
    finally:
        conn.close()

    try:
        res1 = run_curator(
            data_ref=date(2026, 10, 23),
            custom_db_path=banco,
            custom_cache_path=cache_file,
        )
        assert res1["avaliados"] == 3
        assert res1["curados"] == 1
        assert res1["reservas"] == 1
        assert res1["descartados"] == 1
        assert res1["suspeitos"] == 1
        assert res1["historico_gravado"] == 3

        # segunda chamada sem novos produtos 'novo' deve ser idempotente (avaliados == 0)
        res2 = run_curator(
            data_ref=date(2026, 10, 23),
            custom_db_path=banco,
            custom_cache_path=cache_file,
        )
        assert res2["avaliados"] == 0

        conn = get_connection(banco)
        try:
            p_top = conn.execute("SELECT * FROM produtos WHERE id_produto = 'MLB_TOP';").fetchone()
            p_res = conn.execute("SELECT * FROM produtos WHERE id_produto = 'MLB_RES';").fetchone()
            p_susp = conn.execute("SELECT * FROM produtos WHERE id_produto = 'MLB_SUSP';").fetchone()
            hist_hoje = conn.execute(
                "SELECT count(*) AS c FROM historico_preco WHERE data = '2026-10-23';"
            ).fetchone()["c"]
            execs = conn.execute(
                "SELECT count(*) AS c FROM execucoes WHERE modulo = 'curator' AND status = 'sucesso';"
            ).fetchone()["c"]
        finally:
            conn.close()

        assert p_top["status"] == "curado"
        assert p_top["score"] >= 65.0
        assert p_top["prioridade"] == "Alta"

        assert p_res["status"] == "reserva"
        assert 50.0 <= p_res["score"] < 65.0

        assert p_susp["status"] == "descartado"
        assert p_susp["suspeito"] == 1

        assert hist_hoje == 3
        assert execs == 2

        # testa cli run_stage.py curator --reprocessar-hoje
        monkeypatch.setenv("DB_PATH", str(banco))
        rc = run_stage.main(["curator", "--reprocessar-hoje"])
        saida = capsys.readouterr().out
        assert rc == 0
        assert "Produtos avaliados: 3" in saida
        assert "Curados (>=65): 1" in saida
    finally:
        for arq in (banco, cache_file):
            if arq.exists():
                arq.unlink()


def test_json_truncado_da_ia_registra_gasto_e_usa_fallback(monkeypatch):
    # garante que resposta truncada da ia tem o custo gravado em gasto_ia e cai no fallback deterministico
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_curator_truncado.db"
    cache_file = PASTA_DATA_TESTS / "test_curator_truncado_cache.json"
    for arq in (banco, cache_file):
        if arq.exists():
            arq.unlink()
    init_db(banco)

    cfg_ia_teste = {
        "modelo": "gemini-2.5-flash",
        "teto_mensal_brl": 50.0,
        "alerta_pct": 80.0,
        "max_tokens_saida": 600,
        "cotacao_usd_brl": 6.0,
        "modelos": {"gemini-2.5-flash": {"entrada_usd_1m": 0.30, "saida_usd_1m": 2.50}},
    }
    produto = [
        {
            "id_produto": "MLB777",
            "nome_produto": "Mouse Gamer Sem Fio Recarregavel 16000 DPI",
            "termo_origem": "mouse gamer",
            "valor_original": 250.0,
            "valor_atual": 150.0,
            "desconto_pct": 40.0,
            "nota": 4.8,
            "vendas": 5000,
        }
    ]

    try:
        res = julgar_produtos_em_lote(
            produtos=produto,
            custom_db_path=banco,
            custom_cache_path=cache_file,
            config_ia_override=cfg_ia_teste,
            llm_callable=lambda _l: ('{"avaliacoes": [{"id_produto": "MLB7', 900, 600),
        )
        assert res["usou_fallback"] is True
        assert res["chamadas_ia"] == 1
        assert res["custo_brl"] > 0
        assert "MLB777" in res["avaliacoes"]
        assert not cache_file.exists()

        info = consultar_gasto_mes(custom_db_path=banco, config_override=cfg_ia_teste)
        assert info["tokens_in"] == 900
        assert info["tokens_out"] == 600
    finally:
        for arq in (banco, cache_file):
            if arq.exists():
                arq.unlink()
