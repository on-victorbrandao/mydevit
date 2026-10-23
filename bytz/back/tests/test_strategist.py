# testes unitarios e de integracao do estagio 2 (estrategista: 80/20, vetos, aposentadoria e sqlite)
import json
import sys
from datetime import date
from pathlib import Path

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from core.db import get_connection, init_db
from strategist.termos import (
    avaliar_aposentadoria_termo,
    gerar_termos_exploracao,
    gerar_termos_para_tema_deterministico,
    selecionar_orcamento_80_20,
)
from strategist.termos import run as run_strategist

PASTA_DATA_TESTS = PASTA_BACK / "data" / "tests"


def test_geracao_3_a_5_termos_por_tema_e_respeito_aos_vetos():
    # garante que cada tema valido gera de 3 a 5 termos com filtros e que temas vetados geram zero
    tema_valido = {
        "id": 10,
        "tema": "Teclado mecanico e mouse gamer sem fio",
        "nicho": "setup",
        "nota_tendencia": 8.6,
    }
    termos = gerar_termos_para_tema_deterministico(tema_valido)
    assert 3 <= len(termos) <= 5
    for t in termos:
        assert t["nicho"] == "setup"
        assert t["tema_id"] == 10
        assert t["filtros"]["ordenacao"] == "mais_vendidos"

    tema_vetado = {
        "id": 11,
        "tema": "Termogenico emagrecedor replica primeira linha",
        "nicho": "fitness",
        "nota_tendencia": 9.5,
    }
    assert gerar_termos_para_tema_deterministico(tema_vetado) == []


def test_regra_80_20_e_teto_max_buscas_dia():
    # valida proporcao de 80% historico (24) e 20% exploracao (6) respeitando max_buscas_dia=30
    historico = [
        {
            "termo": f"produto historico {i}",
            "nicho": "eletronicos",
            "prioridade": 8,
            "ativo": 1,
            "execucoes": 5,
            "execucoes_sem_aprovado": 0,
            "aprovados_total": 10 + i,
        }
        for i in range(30)
    ]
    exploracao = [
        {
            "termo": f"novo termo exploracao {i}",
            "nicho": "setup",
            "prioridade": 9,
            "tema_id": 1,
        }
        for i in range(15)
    ]

    selecionados = selecionar_orcamento_80_20(
        buscas_historico=historico,
        buscas_exploracao=exploracao,
        max_buscas_dia=30,
    )
    assert len(selecionados) == 30
    qtd_hist = sum(1 for s in selecionados if s["origem_selecao"] == "historico")
    qtd_expl = sum(1 for s in selecionados if s["origem_selecao"] == "exploracao")
    assert qtd_hist == 24
    assert qtd_expl == 6


def test_aposentadoria_3_execucoes_sem_aprovado_e_reativacao_14_dias_ou_sazonal():
    # valida inativacao por 14 dias apos 3 falhas seguidas e reativacao apos prazo ou sazonalidade
    hoje = date(2026, 10, 23)
    busca_3_falhas = {
        "termo": "webcam usb antiga",
        "nicho": "home-office",
        "ativo": 1,
        "inativo_ate": None,
        "execucoes_sem_aprovado": 3,
    }
    ativo, inativo_ate, sem_aprov = avaliar_aposentadoria_termo(busca_3_falhas, data_ref=hoje)
    assert ativo == 0
    assert inativo_ate == "2026-11-06"
    assert sem_aprov == 3

    # antes de vencer os 14 dias e fora de sazonalidade continua inativo
    busca_inativa = {
        "termo": "webcam usb antiga",
        "nicho": "home-office",
        "ativo": 0,
        "inativo_ate": "2026-11-06",
        "execucoes_sem_aprovado": 3,
    }
    ativo_antes, _, _ = avaliar_aposentadoria_termo(busca_inativa, data_ref=date(2026, 11, 1))
    assert ativo_antes == 0

    # ao chegar na data de vencimento (ou em sazonalidade do nicho), reativa e zera contador
    ativo_vencido, ate_vencido, cont_vencido = avaliar_aposentadoria_termo(
        busca_inativa, data_ref=date(2026, 11, 6)
    )
    assert ativo_vencido == 1
    assert ate_vencido is None
    assert cont_vencido == 0

    ativo_sazonal, _, cont_sazonal = avaliar_aposentadoria_termo(
        busca_inativa,
        data_ref=date(2026, 10, 25),
        nichos_sazonais_ativos={"home-office"},
    )
    assert ativo_sazonal == 1
    assert cont_sazonal == 0


def test_strategist_ia_json_invalido_e_filtro_de_vetos_na_ia():
    # comprova que json quebrado da ia aciona templates e que json valido filtra vetos
    temas = [{"id": 1, "tema": "Garrafa termica inox academia", "nicho": "fitness", "nota_tendencia": 8.5}]

    termos_fallback = gerar_termos_exploracao(temas=temas, llm_callable=lambda _t: "json_invalido")
    assert len(termos_fallback) >= 3

    resposta_ia = json.dumps(
        {
            "buscas": [
                {"termo": "emagrecedor termogenico", "nicho": "fitness", "prioridade": 9, "tema_id": 1},
                {"termo": "garrafa termica inox 1 litro", "nicho": "fitness", "prioridade": 8, "tema_id": 1},
            ]
        }
    )
    termos_ia = gerar_termos_exploracao(temas=temas, llm_callable=lambda _t: resposta_ia)
    assert len(termos_ia) == 1
    assert termos_ia[0]["termo"] == "garrafa termica inox 1 litro"


def test_strategist_run_idempotente_sqlite_aposentadoria_e_execucoes():
    # valida o fluxo completo no sqlite: transforma temas em buscas, aposenta falhas e respeita max_buscas_dia
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco = PASTA_DATA_TESTS / "test_strategist_run.db"
    if banco.exists():
        banco.unlink()

    init_db(banco)
    conn = get_connection(banco)
    try:
        with conn:
            # insere 10 temas do scout ainda nao usados
            for idx, nicho in enumerate(
                ["eletronicos", "setup", "smart home", "acessorios moveis", "home-office", "fitness"],
                start=1,
            ):
                conn.execute(
                    """
                    INSERT INTO temas (id, data, tema, fonte, nota_tendencia, justificativa, usado)
                    VALUES (?, '2026-10-23', ?, 'sazonalidade:calendario_base', 8.5, 'Teste', 0);
                    """,
                    (idx, f"Ofertas de {nicho} em destaque"),
                )
            # insere um termo antigo com 3 execucoes sem aprovado para testar aposentadoria no run
            conn.execute("""
                INSERT INTO buscas (termo, nicho, prioridade, ativo, execucoes, execucoes_sem_aprovado)
                VALUES ('termo desgastado sem conversao', 'eletronicos', 5, 1, 3, 3);
                """)
    finally:
        conn.close()

    try:
        res1 = run_strategist(data_ref=date(2026, 10, 23), custom_db_path=banco)
        assert res1["aposentados"] == 1
        assert 1 <= res1["buscas_ativas"] <= 30
        assert res1["inseridos"] == res1["buscas_ativas"]

        # rodar pela segunda vez no mesmo dia deve ser idempotente (0 novos inseridos)
        res2 = run_strategist(data_ref=date(2026, 10, 23), custom_db_path=banco)
        assert res2["inseridos"] == 0
        assert res2["atualizados"] == res1["buscas_ativas"]
        assert res2["buscas_ativas"] <= 30

        conn = get_connection(banco)
        try:
            ativas = conn.execute("SELECT count(*) AS c FROM buscas WHERE ativo = 1;").fetchone()["c"]
            aposentada = conn.execute(
                "SELECT ativo, inativo_ate FROM buscas WHERE termo = 'termo desgastado sem conversao';"
            ).fetchone()
            temas_usados = conn.execute("SELECT count(*) AS c FROM temas WHERE usado = 1;").fetchone()["c"]
            execs = conn.execute(
                "SELECT count(*) AS c FROM execucoes WHERE modulo = 'strategist' AND status = 'sucesso';"
            ).fetchone()["c"]
        finally:
            conn.close()

        assert ativas == res1["buscas_ativas"]
        assert ativas <= 30
        assert aposentada["ativo"] == 0
        assert aposentada["inativo_ate"] == "2026-11-06"
        assert temas_usados == 6
        assert execs == 2
    finally:
        if banco.exists():
            banco.unlink()
