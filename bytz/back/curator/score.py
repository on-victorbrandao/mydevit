# calculo puro de score (0 a 100), historico de preco (30d), cortes, variedade e persistencia do curador
import math
import statistics
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from core.db import get_connection, init_db
from core.logger import get_logger
from curator.llm_judge import julgar_produtos_em_lote
from scout.temas import carregar_vetos, inferir_nicho_tema, sem_acento_minusculo

PASTA_BACK = Path(__file__).resolve().parent.parent
CONFIG_PATH = PASTA_BACK / "config.yaml"
COMISSAO_PATH = PASTA_BACK / "config" / "comissao.yaml"
TZ_SP = ZoneInfo("America/Sao_Paulo")

PESOS_PADRAO: dict[str, float] = {
    "desconto": 25.0,
    "demanda": 20.0,
    "reputacao": 15.0,
    "afinidade": 15.0,
    "tendencia": 10.0,
    "ticket": 10.0,
    "frescor": 5.0,
}

FATOR_CONFIANCA_SEM_HISTORICO = 0.6
DESCONTO_REAL_TETO_PCT = 35.0
MAX_APROVADOS_POR_TERMO_DIA = 3


def validar_pesos_score(
    pesos: dict[str, float],
    permitir_desvio_maior: bool = False,
) -> dict[str, float]:
    # garante que os 7 pesos existem e nao desviam mais de 10% do padrao sem aprovacao do dono
    validados: dict[str, float] = {}
    for chave, padrao in PESOS_PADRAO.items():
        valor = float(pesos.get(chave, padrao))
        if valor < 0:
            raise ValueError(f"Peso '{chave}' nao pode ser negativo: {valor}")
        if not permitir_desvio_maior:
            limite_inf = padrao * 0.90 - 1e-6
            limite_sup = padrao * 1.10 + 1e-6
            if valor < limite_inf or valor > limite_sup:
                raise ValueError(
                    f"Peso '{chave}' ({valor}) excede o limite de +-10% sobre {padrao} sem aprovacao."
                )
        validados[chave] = valor
    return validados


def carregar_config_score(
    custom_config_path: Path | None = None,
    permitir_desvio_maior: bool = False,
) -> dict:
    # carrega cortes e pesos da secao score do config.yaml
    caminho = custom_config_path or CONFIG_PATH
    dados: dict = {}
    if caminho.exists():
        with open(caminho, "r", encoding="utf-8") as f:
            dados = yaml.safe_load(f) or {}

    secao = dados.get("score") or {}
    corte_post = float(secao.get("corte_post", 65.0))
    corte_reserva = float(secao.get("corte_reserva", 50.0))
    pesos_brutos = secao.get("pesos") or PESOS_PADRAO
    pesos = validar_pesos_score(pesos_brutos, permitir_desvio_maior=permitir_desvio_maior)

    return {
        "corte_post": corte_post,
        "corte_reserva": corte_reserva,
        "pesos": pesos,
    }


def carregar_config_comissao(custom_comissao_path: Path | None = None) -> dict:
    # le faixas de preco e comissoes por categoria do config/comissao.yaml
    caminho = custom_comissao_path or COMISSAO_PATH
    if not caminho.exists():
        return {"padrao_pct": None, "categorias": {}, "faixas_preco_brl": [50, 150, 400, 1000]}
    with open(caminho, "r", encoding="utf-8") as f:
        dados = yaml.safe_load(f) or {}
    return {
        "padrao_pct": dados.get("padrao_pct"),
        "categorias": dados.get("categorias") or {},
        "faixas_preco_brl": dados.get("faixas_preco_brl") or [50, 150, 400, 1000],
    }


def calcular_desconto_real_pct(
    valor_atual: float | None,
    valor_original: float | None,
    precos_historico_30d: Sequence[float] | None = None,
) -> float:
    # calcula desconto real vs mediana de 30 dias ou usa preco riscado com fator de confianca 0.6
    if valor_atual is None or float(valor_atual) <= 0:
        return 0.0
    atual = float(valor_atual)

    historico_valido = [float(p) for p in (precos_historico_30d or []) if p is not None and float(p) > 0]
    if historico_valido:
        mediana = float(statistics.median(historico_valido))
        if mediana <= 0:
            return 0.0
        desc_mediana = ((mediana - atual) / mediana) * 100.0
        return round(desc_mediana, 2)

    if valor_original is None or float(valor_original) <= 0:
        return 0.0
    original = float(valor_original)
    desc_riscado = ((original - atual) / original) * 100.0
    return round(desc_riscado * FATOR_CONFIANCA_SEM_HISTORICO, 2)


def calcular_pontos_desconto(
    valor_atual: float | None,
    valor_original: float | None,
    precos_historico_30d: Sequence[float] | None = None,
    peso_max: float = 25.0,
) -> float:
    # converte o desconto real em pontos de 0 ate peso_max (desconto negativo ou zero vale 0)
    desc_real = calcular_desconto_real_pct(valor_atual, valor_original, precos_historico_30d)
    if desc_real <= 0:
        return 0.0
    pontos = (desc_real / DESCONTO_REAL_TETO_PCT) * float(peso_max)
    return round(min(float(peso_max), max(0.0, pontos)), 2)


def calcular_pontos_demanda(
    vendas: int | None,
    vendas_anterior: int | None = None,
    peso_max: float = 20.0,
) -> float:
    # calcula demanda em log-escala de vendas com bonus se houve crescimento entre capturas
    if vendas is None or int(vendas) <= 0:
        return 0.0
    v_int = int(vendas)
    log_v = math.log10(max(1, v_int))
    log_min = math.log10(50)
    log_max = math.log10(15000)

    fator = min(1.0, max(0.0, (log_v - log_min) / (log_max - log_min)))
    pontos = fator * float(peso_max)

    if vendas_anterior is not None and int(vendas_anterior) > 0 and v_int > int(vendas_anterior):
        pontos += float(peso_max) * 0.10

    return round(min(float(peso_max), max(0.0, pontos)), 2)


def calcular_pontos_reputacao(
    nota: float | None,
    vendas: int | None = None,
    peso_max: float = 15.0,
) -> float:
    # calcula reputacao ponderada por nota e volume (nota <= 4.5 vale 0; nota 5.0 vale peso_max)
    if nota is None or float(nota) <= 4.5:
        return 0.0
    fator_nota = min(1.0, max(0.0, (float(nota) - 4.5) / 0.5))

    if vendas is None or int(vendas) <= 0:
        fator_volume = 0.8
    else:
        log_vol = math.log10(max(1, int(vendas)))
        fator_volume = min(1.0, 0.8 + 0.2 * min(1.0, max(0.0, (log_vol - 2.0) / 2.0)))

    pontos = fator_nota * fator_volume * float(peso_max)
    return round(min(float(peso_max), max(0.0, pontos)), 2)


def calcular_pontos_afinidade(afinidade_ia: float | None, peso_max: float = 15.0) -> float:
    # normaliza a nota de afinidade do juiz (0 a 15) para o peso configurado
    if afinidade_ia is None or float(afinidade_ia) <= 0:
        return 0.0
    pontos = (float(afinidade_ia) / 15.0) * float(peso_max)
    return round(min(float(peso_max), max(0.0, pontos)), 2)


def calcular_pontos_tendencia(nota_tendencia: float | None, peso_max: float = 10.0) -> float:
    # converte a nota_tendencia (0 a 10) do tema de origem para o peso configurado
    nota = 7.0 if nota_tendencia is None else float(nota_tendencia)
    if nota <= 0:
        return 0.0
    pontos = (nota / 10.0) * float(peso_max)
    return round(min(float(peso_max), max(0.0, pontos)), 2)


def _obter_comissao_categoria(categoria: str | None, cfg_comissao: dict) -> float | None:
    # busca percentual de comissao da categoria ou padrao no config/comissao.yaml
    mapa = cfg_comissao.get("categorias") or {}
    cat_norm = sem_acento_minusculo(str(categoria or ""))
    if cat_norm:
        for nome_cat, valor in mapa.items():
            if sem_acento_minusculo(str(nome_cat)) == cat_norm and isinstance(valor, (int, float)):
                return float(valor)
    padrao = cfg_comissao.get("padrao_pct")
    if isinstance(padrao, (int, float)):
        return float(padrao)
    return None


def calcular_pontos_ticket(
    valor_atual: float | None,
    categoria: str | None = None,
    cfg_comissao: dict | None = None,
    peso_max: float = 10.0,
) -> float:
    # pontua faixa de preco e comissao da categoria conforme config/comissao.yaml
    if valor_atual is None or float(valor_atual) <= 0:
        return 0.0
    preco = float(valor_atual)
    cfg = cfg_comissao if cfg_comissao is not None else carregar_config_comissao()
    faixas = sorted(float(x) for x in (cfg.get("faixas_preco_brl") or [50, 150, 400, 1000]))

    f0 = faixas[0] if len(faixas) > 0 else 50.0
    f1 = faixas[1] if len(faixas) > 1 else 150.0
    f2 = faixas[2] if len(faixas) > 2 else 400.0
    f3 = faixas[3] if len(faixas) > 3 else 1000.0

    if f1 <= preco <= f2:
        fator_ticket = 1.0
    elif f0 <= preco < f1:
        fator_ticket = 0.90
    elif f2 < preco <= f3:
        fator_ticket = 0.80
    elif preco < f0:
        fator_ticket = 0.50
    else:
        fator_ticket = 0.60

    comissao_pct = _obter_comissao_categoria(categoria, cfg)
    if comissao_pct is not None and comissao_pct > 0:
        fator_comissao = min(1.0, max(0.0, comissao_pct / 12.0))
        fator_final = 0.6 * fator_ticket + 0.4 * fator_comissao
    else:
        fator_final = fator_ticket

    return round(min(float(peso_max), max(0.0, fator_final * float(peso_max))), 2)


def calcular_pontos_frescor(
    produto: dict,
    data_ref: date | None = None,
    peso_max: float = 5.0,
) -> float:
    # avalia imagem valida, titulo limpo e disponibilidade/recencia do produto
    pontos_base = 0.0
    img = str(produto.get("imagem_url") or "").strip()
    if img.startswith(("http://", "https://")):
        pontos_base += 2.0

    nome = " ".join(str(produto.get("nome_produto") or "").split())
    if len(nome) >= 12:
        pontos_base += 1.5

    status_atual = str(produto.get("status") or "novo").lower()
    preco_atual = float(produto.get("valor_atual") or 0.0)
    if preco_atual > 0 and status_atual not in ("expirado", "pausado"):
        data_cap_str = str(produto.get("data_captura") or "")[:10]
        if data_cap_str and data_ref is not None:
            try:
                dias = (data_ref - date.fromisoformat(data_cap_str)).days
                if dias <= 3:
                    pontos_base += 1.5
                elif dias <= 7:
                    pontos_base += 1.0
                else:
                    pontos_base += 0.5
            except ValueError:
                pontos_base += 1.5
        else:
            pontos_base += 1.5

    return round(min(float(peso_max), max(0.0, (pontos_base / 5.0) * float(peso_max))), 2)


def classificar_status_por_score(
    score: float,
    suspeito: bool = False,
    duplicado_de: str | None = None,
    corte_post: float = 65.0,
    corte_reserva: float = 50.0,
) -> str:
    # aplica os cortes oficiais (>=65 curado, 50-64 reserva, <50 ou suspeito/duplicado descartado)
    if suspeito or duplicado_de:
        return "descartado"
    if float(score) >= float(corte_post):
        return "curado"
    if float(score) >= float(corte_reserva):
        return "reserva"
    return "descartado"


def calcular_score_produto(
    produto: dict,
    precos_historico_30d: Sequence[float] | None = None,
    avaliacao_juiz: dict | None = None,
    nota_tendencia: float | None = None,
    vendas_anterior: int | None = None,
    cfg_score: dict | None = None,
    cfg_comissao: dict | None = None,
    data_ref: date | None = None,
) -> dict:
    # calcula os 7 componentes do score (0-100) e classifica o status do produto
    cfg_s = cfg_score if cfg_score is not None else carregar_config_score()
    pesos = cfg_s["pesos"]
    juiz = avaliacao_juiz or {}

    afinidade_bruta = float(juiz.get("afinidade", produto.get("afinidade") or 0.0))
    suspeito = bool(juiz.get("suspeito", bool(produto.get("suspeito", 0))))
    duplicado_de = juiz.get("duplicado_de")
    motivo_ia = str(juiz.get("motivo") or produto.get("motivo_ia") or "")

    val_atual = produto.get("valor_atual")
    val_orig = produto.get("valor_original")
    vendas = produto.get("vendas")
    nota = produto.get("nota")
    categoria = produto.get("categoria") or inferir_nicho_tema(
        f"{produto.get('nome_produto') or ''} {produto.get('termo_origem') or ''}"
    )

    desc_real_pct = calcular_desconto_real_pct(val_atual, val_orig, precos_historico_30d)
    pts_desconto = calcular_pontos_desconto(
        val_atual, val_orig, precos_historico_30d, peso_max=pesos["desconto"]
    )
    pts_demanda = calcular_pontos_demanda(vendas, vendas_anterior=vendas_anterior, peso_max=pesos["demanda"])
    pts_reputacao = calcular_pontos_reputacao(nota, vendas=vendas, peso_max=pesos["reputacao"])
    pts_afinidade = calcular_pontos_afinidade(afinidade_bruta, peso_max=pesos["afinidade"])
    pts_tendencia = calcular_pontos_tendencia(nota_tendencia, peso_max=pesos["tendencia"])
    pts_ticket = calcular_pontos_ticket(
        val_atual, categoria=categoria, cfg_comissao=cfg_comissao, peso_max=pesos["ticket"]
    )
    pts_frescor = calcular_pontos_frescor(produto, data_ref=data_ref, peso_max=pesos["frescor"])

    soma = (
        pts_desconto + pts_demanda + pts_reputacao + pts_afinidade + pts_tendencia + pts_ticket + pts_frescor
    )
    score_final = round(min(100.0, max(0.0, soma)), 2)

    status_calc = classificar_status_por_score(
        score=score_final,
        suspeito=suspeito,
        duplicado_de=duplicado_de,
        corte_post=cfg_s["corte_post"],
        corte_reserva=cfg_s["corte_reserva"],
    )

    if (
        score_final >= 75.0
        and status_calc == "curado"
        or score_final >= float(cfg_s["corte_post"])
        and status_calc == "curado"
    ):
        prioridade = "Alta"
    elif status_calc == "reserva":
        prioridade = "Media"
    else:
        prioridade = "Baixa"

    return {
        "id_produto": str(produto.get("id_produto") or ""),
        "nome_produto": str(produto.get("nome_produto") or ""),
        "categoria": categoria,
        "termo_origem": produto.get("termo_origem"),
        "valor_atual": val_atual,
        "valor_original": val_orig,
        "desconto_real_pct": desc_real_pct,
        "score": score_final,
        "afinidade": round(afinidade_bruta, 2),
        "suspeito": 1 if suspeito else 0,
        "duplicado_de": duplicado_de,
        "motivo_ia": motivo_ia,
        "status": status_calc,
        "prioridade": prioridade,
        "componentes": {
            "desconto": pts_desconto,
            "demanda": pts_demanda,
            "reputacao": pts_reputacao,
            "afinidade": pts_afinidade,
            "tendencia": pts_tendencia,
            "ticket": pts_ticket,
            "frescor": pts_frescor,
        },
    }


def intercalar_por_categoria(itens: list[dict]) -> list[dict]:
    # ordena itens mantendo maior score mas evitando repetir a mesma categoria consecutivamente
    restantes = sorted(itens, key=lambda x: float(x.get("score") or 0.0), reverse=True)
    resultado: list[dict] = []
    ultima_cat: str | None = None

    while restantes:
        escolhido_idx = None
        if ultima_cat is not None:
            for idx, item in enumerate(restantes):
                cat_item = sem_acento_minusculo(str(item.get("categoria") or ""))
                if cat_item != ultima_cat:
                    escolhido_idx = idx
                    break
        if escolhido_idx is None:
            escolhido_idx = 0

        escolhido = restantes.pop(escolhido_idx)
        resultado.append(escolhido)
        ultima_cat = sem_acento_minusculo(str(escolhido.get("categoria") or "")) or None

    return resultado


def aplicar_regras_variedade(
    avaliados: list[dict],
    max_por_termo_dia: int = MAX_APROVADOS_POR_TERMO_DIA,
    aprovados_por_termo_hoje: dict[str, int] | None = None,
) -> list[dict]:
    # limita aprovados (curado) a ate 3 por termo_origem no dia (excedente vira reserva) e intercala categorias
    contagem_termo = {sem_acento_minusculo(k): int(v) for k, v in (aprovados_por_termo_hoje or {}).items()}
    ordenados = sorted(avaliados, key=lambda x: float(x.get("score") or 0.0), reverse=True)

    curados: list[dict] = []
    outros: list[dict] = []

    for item in ordenados:
        copia = dict(item)
        if copia.get("status") == "curado":
            termo_key = sem_acento_minusculo(str(copia.get("termo_origem") or "geral")) or "geral"
            qtd_atual = contagem_termo.get(termo_key, 0)
            if qtd_atual < max_por_termo_dia:
                contagem_termo[termo_key] = qtd_atual + 1
                curados.append(copia)
            else:
                copia["status"] = "reserva"
                copia["prioridade"] = "Media"
                obs_antiga = str(copia.get("observacao") or "").strip()
                nota_var = f"Reserva por variedade (limite de {max_por_termo_dia} por termo/dia)."
                copia["observacao"] = f"{obs_antiga} {nota_var}".strip()
                outros.append(copia)
        else:
            outros.append(copia)

    curados_intercalados = intercalar_por_categoria(curados)
    return curados_intercalados + outros


def _extrair_historico_relevante_30d(
    rows_historico: list[dict],
    valor_atual_produto: float,
    data_captura_produto: str | None,
) -> list[float]:
    # retorna precos dos ultimos 30 dias se houver serie historica real (variacao de preco ou >=3 dias)
    if not rows_historico:
        return []

    precos = [float(r["valor_atual"]) for r in rows_historico if r.get("valor_atual") is not None]
    datas = {str(r.get("data") or "")[:10] for r in rows_historico if r.get("data")}
    if not precos:
        return []

    tem_variacao_preco = any(abs(p - float(valor_atual_produto)) > 0.009 for p in precos)
    if tem_variacao_preco or len(datas) >= 3:
        return precos

    # se existe registro historico de pelo menos 2 dias antes da data_captura do produto, considera serie
    if data_captura_produto:
        try:
            dt_cap = date.fromisoformat(str(data_captura_produto)[:10])
            for d_str in datas:
                if (dt_cap - date.fromisoformat(d_str)).days >= 2:
                    return precos
        except ValueError:
            pass

    return []


def run(
    data_ref: date | None = None,
    custom_db_path: Path | None = None,
    custom_config_path: Path | None = None,
    custom_comissao_path: Path | None = None,
    custom_vetos_path: Path | None = None,
    custom_cache_path: Path | None = None,
    config_ia_override: dict | None = None,
    llm_callable: Callable | None = None,
    usar_cache_ia: bool = True,
    parar_ao_estourar: bool = False,
    reprocessar_hoje: bool = False,
) -> dict:
    # executa o estagio 4 (curador) lendo produtos novo, gravando historico_preco e pontuando com juiz llm
    init_db(custom_db_path)
    agora_dt = datetime.now(TZ_SP)
    inicio_iso = agora_dt.isoformat(timespec="seconds")
    ref_date = data_ref or agora_dt.date()
    hoje_str = ref_date.strftime("%Y-%m-%d")
    limite_30d_str = (ref_date - timedelta(days=30)).strftime("%Y-%m-%d")
    logger = get_logger("curator")

    try:
        cfg_score = carregar_config_score(custom_config_path)
        cfg_comissao = carregar_config_comissao(custom_comissao_path)
        vetos = carregar_vetos(custom_vetos_path)

        conn = get_connection(custom_db_path)
        try:
            if reprocessar_hoje:
                rows_prod = conn.execute(
                    """
                    SELECT * FROM produtos
                    WHERE status = 'novo'
                       OR (status IN ('curado', 'reserva', 'descartado') AND data_atualizacao LIKE ?);
                    """,
                    (f"{hoje_str}%",),
                ).fetchall()
            else:
                rows_prod = conn.execute("SELECT * FROM produtos WHERE status = 'novo';").fetchall()

            produtos_novos = [dict(r) for r in rows_prod]

            if not produtos_novos:
                fim_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
                with conn:
                    conn.execute(
                        """
                        INSERT INTO execucoes (modulo, inicio, fim, status, itens, erro)
                        VALUES ('curator', ?, ?, 'sucesso', 0, NULL);
                        """,
                        (inicio_iso, fim_iso),
                    )
                return {
                    "data": hoje_str,
                    "avaliados": 0,
                    "curados": 0,
                    "reservas": 0,
                    "descartados": 0,
                    "suspeitos": 0,
                    "historico_gravado": 0,
                    "chamadas_ia": 0,
                    "cache_hits": 0,
                    "custo_brl": 0.0,
                    "usou_fallback": False,
                    "ranking": [],
                }

            # carrega mapas auxiliares de temas, buscas, historico de cliques e aprovados do dia
            rows_temas = conn.execute("SELECT id, tema, nota_tendencia FROM temas;").fetchall()
            nota_por_tema_nome = {
                sem_acento_minusculo(r["tema"]): float(r["nota_tendencia"])
                for r in rows_temas
                if r["tema"] and r["nota_tendencia"] is not None
            }
            nota_por_tema_id = {
                int(r["id"]): float(r["nota_tendencia"])
                for r in rows_temas
                if r["id"] is not None and r["nota_tendencia"] is not None
            }

            rows_buscas = conn.execute(
                "SELECT termo, tema_id, nicho, prioridade, aprovados_total FROM buscas;"
            ).fetchall()
            info_por_termo: dict[str, dict] = {}
            termos_alto_ctr: set[str] = set()
            for rb in rows_buscas:
                t_key = sem_acento_minusculo(rb["termo"])
                info_por_termo[t_key] = dict(rb)
                if int(rb["aprovados_total"] or 0) > 0:
                    termos_alto_ctr.add(t_key)

            rows_posts = conn.execute(
                "SELECT termo_origem, quantidade_cliques FROM posts WHERE quantidade_cliques > 0;"
            ).fetchall()
            for rp in rows_posts:
                if rp["termo_origem"]:
                    termos_alto_ctr.add(sem_acento_minusculo(rp["termo_origem"]))

            # conta produtos ja aprovados hoje (fora do lote atual) para respeitar variedade de 3/termo
            ids_lote_atual = {str(p["id_produto"]) for p in produtos_novos}
            rows_aprov_hoje = conn.execute(
                """
                SELECT id_produto, termo_origem FROM produtos
                WHERE status IN ('curado', 'link_pronto', 'publicado')
                  AND data_atualizacao LIKE ?;
                """,
                (f"{hoje_str}%",),
            ).fetchall()
            aprovados_por_termo_hoje: dict[str, int] = {}
            for ra in rows_aprov_hoje:
                if str(ra["id_produto"]) in ids_lote_atual:
                    continue
                tk = sem_acento_minusculo(str(ra["termo_origem"] or "geral")) or "geral"
                aprovados_por_termo_hoje[tk] = aprovados_por_termo_hoje.get(tk, 0) + 1

            # le o historico de 30 dias antes de gravar o ponto de hoje
            historico_por_prod: dict[str, list[float]] = {}
            for prod in produtos_novos:
                id_p = str(prod["id_produto"])
                rows_h = conn.execute(
                    """
                    SELECT data, valor_atual FROM historico_preco
                    WHERE id_produto = ? AND data >= ?
                    ORDER BY data ASC;
                    """,
                    (id_p, limite_30d_str),
                ).fetchall()
                historico_por_prod[id_p] = _extrair_historico_relevante_30d(
                    [dict(x) for x in rows_h],
                    valor_atual_produto=float(prod.get("valor_atual") or 0.0),
                    data_captura_produto=prod.get("data_captura"),
                )

            # grava historico de preco do dia para todos os produtos avaliados
            historico_gravado = 0
            with conn:
                for prod in produtos_novos:
                    if prod.get("id_produto") and prod.get("valor_atual") is not None:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO historico_preco (id_produto, data, valor_atual, valor_original)
                            VALUES (?, ?, ?, ?);
                            """,
                            (
                                prod["id_produto"],
                                hoje_str,
                                float(prod["valor_atual"]),
                                prod.get("valor_original"),
                            ),
                        )
                        historico_gravado += 1
        finally:
            conn.close()

        # enriquece categoria/nicho nos dicts em memoria antes de enviar ao juiz llm
        for prod in produtos_novos:
            termo_k = sem_acento_minusculo(str(prod.get("termo_origem") or ""))
            busca_info = info_por_termo.get(termo_k)
            if not prod.get("categoria"):
                nicho_busca = busca_info.get("nicho") if busca_info else None
                prod["categoria"] = inferir_nicho_tema(
                    f"{prod.get('nome_produto') or ''} {prod.get('termo_origem') or ''}",
                    nicho_busca,
                )

        res_juiz = julgar_produtos_em_lote(
            produtos=produtos_novos,
            vetos=vetos,
            termos_alto_ctr=termos_alto_ctr,
            custom_db_path=custom_db_path,
            custom_cache_path=custom_cache_path,
            config_ia_override=config_ia_override,
            llm_callable=llm_callable,
            usar_cache=usar_cache_ia,
            parar_ao_estourar=parar_ao_estourar,
        )
        avaliacoes_juiz = res_juiz["avaliacoes"]

        avaliados_brutos: list[dict] = []
        for prod in produtos_novos:
            id_p = str(prod["id_produto"])
            termo_k = sem_acento_minusculo(str(prod.get("termo_origem") or ""))
            tema_k = sem_acento_minusculo(str(prod.get("tema_origem") or ""))

            nota_tend: float | None = None
            if tema_k and tema_k in nota_por_tema_nome:
                nota_tend = nota_por_tema_nome[tema_k]
            elif termo_k in info_por_termo:
                tid = info_por_termo[termo_k].get("tema_id")
                if tid is not None and int(tid) in nota_por_tema_id:
                    nota_tend = nota_por_tema_id[int(tid)]
                elif info_por_termo[termo_k].get("prioridade") is not None:
                    nota_tend = float(info_por_termo[termo_k]["prioridade"])

            item_score = calcular_score_produto(
                produto=prod,
                precos_historico_30d=historico_por_prod.get(id_p, []),
                avaliacao_juiz=avaliacoes_juiz.get(id_p),
                nota_tendencia=nota_tend,
                cfg_score=cfg_score,
                cfg_comissao=cfg_comissao,
                data_ref=ref_date,
            )
            item_score["observacao"] = prod.get("observacao")
            avaliados_brutos.append(item_score)

        ranking_final = aplicar_regras_variedade(
            avaliados=avaliados_brutos,
            max_por_termo_dia=MAX_APROVADOS_POR_TERMO_DIA,
            aprovados_por_termo_hoje=aprovados_por_termo_hoje,
        )

        agora_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
        qtd_curados = 0
        qtd_reservas = 0
        qtd_descartados = 0
        qtd_suspeitos = 0

        conn = get_connection(custom_db_path)
        try:
            with conn:
                for item in ranking_final:
                    st = item["status"]
                    if st == "curado":
                        qtd_curados += 1
                    elif st == "reserva":
                        qtd_reservas += 1
                    else:
                        qtd_descartados += 1
                    if item["suspeito"]:
                        qtd_suspeitos += 1

                    conn.execute(
                        """
                        UPDATE produtos
                        SET categoria = COALESCE(categoria, ?),
                            score = ?,
                            afinidade = ?,
                            suspeito = ?,
                            motivo_ia = ?,
                            status = ?,
                            prioridade = ?,
                            data_atualizacao = ?,
                            observacao = ?
                        WHERE id_produto = ?;
                        """,
                        (
                            item.get("categoria"),
                            float(item["score"]),
                            float(item["afinidade"]),
                            int(item["suspeito"]),
                            str(item.get("motivo_ia") or ""),
                            st,
                            item.get("prioridade", "Media"),
                            agora_iso,
                            item.get("observacao"),
                            item["id_produto"],
                        ),
                    )

                fim_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
                conn.execute(
                    """
                    INSERT INTO execucoes (modulo, inicio, fim, status, itens, erro)
                    VALUES ('curator', ?, ?, 'sucesso', ?, NULL);
                    """,
                    (inicio_iso, fim_iso, len(ranking_final)),
                )
        finally:
            conn.close()

        logger.info(
            f"Curador concluido: avaliados={len(ranking_final)}, curados={qtd_curados}, "
            f"reservas={qtd_reservas}, descartados={qtd_descartados}, chamadas_ia={res_juiz['chamadas_ia']}"
        )
        return {
            "data": hoje_str,
            "avaliados": len(ranking_final),
            "curados": qtd_curados,
            "reservas": qtd_reservas,
            "descartados": qtd_descartados,
            "suspeitos": qtd_suspeitos,
            "historico_gravado": historico_gravado,
            "chamadas_ia": res_juiz["chamadas_ia"],
            "cache_hits": res_juiz["cache_hits"],
            "custo_brl": res_juiz["custo_brl"],
            "usou_fallback": res_juiz["usou_fallback"],
            "orcamento_estourado": res_juiz.get("orcamento_estourado", False),
            "ranking": ranking_final,
        }
    except Exception as e:
        fim_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
        conn = get_connection(custom_db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO execucoes (modulo, inicio, fim, status, itens, erro)
                    VALUES ('curator', ?, ?, 'erro', 0, ?);
                    """,
                    (inicio_iso, fim_iso, str(e)),
                )
        finally:
            conn.close()
        raise
