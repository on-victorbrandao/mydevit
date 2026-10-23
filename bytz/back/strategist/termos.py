# transformacao de temas em termos de busca, regra 80/20, aposentadoria de 14 dias e gravacao em buscas
import json
import os
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, ValidationError

from core.budget import pode_gastar_ia, registrar_gasto
from core.db import get_connection, init_db
from core.logger import get_logger
from scout.fontes import NICHOS_OFICIAIS, avaliar_sazonalidade, carregar_sazonalidade
from scout.temas import carregar_vetos, contem_veto, inferir_nicho_tema, sem_acento_minusculo

PASTA_BACK = Path(__file__).resolve().parent.parent
CONFIG_PATH = PASTA_BACK / "config.yaml"
TZ_SP = ZoneInfo("America/Sao_Paulo")

MAX_BUSCAS_DIA_PADRAO = 30
DIAS_APOSENTADORIA = 14
LIMITE_FALHAS_SEGUIDAS = 3

# templates deterministicos de 3 a 5 termos especificos por nicho e palavra-chave
TEMPLATES_TERMOS_POR_NICHO: dict[str, list[dict]] = {
    "eletronicos": [
        {"termo": "fone bluetooth cancelamento ruido", "preco_min": 80, "preco_max": 600},
        {"termo": "caixa de som bluetooth portatil", "preco_min": 90, "preco_max": 700},
        {"termo": "smartwatch bluetooth a prova dagua", "preco_min": 100, "preco_max": 800},
        {"termo": "fone de ouvido sem fio tws", "preco_min": 60, "preco_max": 450},
    ],
    "setup": [
        {"termo": "teclado mecanico gamer rgb", "preco_min": 120, "preco_max": 650},
        {"termo": "mouse gamer sem fio recarregavel", "preco_min": 80, "preco_max": 500},
        {"termo": "headset gamer surround usb", "preco_min": 100, "preco_max": 600},
        {"termo": "ssd nvme m2 1tb", "preco_min": 250, "preco_max": 850},
    ],
    "smart home": [
        {"termo": "lampada inteligente wifi rgb", "preco_min": 35, "preco_max": 200},
        {"termo": "tomada inteligente wifi", "preco_min": 45, "preco_max": 250},
        {"termo": "fechadura digital biometria", "preco_min": 250, "preco_max": 1200},
        {"termo": "camera seguranca wifi externa", "preco_min": 110, "preco_max": 500},
    ],
    "acessorios moveis": [
        {"termo": "carregador portatil power bank 20000mah", "preco_min": 70, "preco_max": 350},
        {"termo": "carregador turbo usb c gan", "preco_min": 50, "preco_max": 280},
        {"termo": "suporte celular veicular magnetico", "preco_min": 35, "preco_max": 180},
        {"termo": "cabo usb c carga rapida 100w", "preco_min": 30, "preco_max": 150},
    ],
    "home-office": [
        {"termo": "suporte articulado monitor pistao gas", "preco_min": 90, "preco_max": 400},
        {"termo": "hub usb c hdmi gigabit", "preco_min": 70, "preco_max": 350},
        {"termo": "webcam full hd 1080p microfone", "preco_min": 80, "preco_max": 450},
        {"termo": "luminaria monitor light bar led", "preco_min": 75, "preco_max": 320},
    ],
    "fitness": [
        {"termo": "garrafa termica inox 1 litro", "preco_min": 50, "preco_max": 250},
        {"termo": "kit faixas elasticas treino academia", "preco_min": 40, "preco_max": 220},
        {"termo": "balanca digital bioimpedancia bluetooth", "preco_min": 65, "preco_max": 300},
        {"termo": "pistola massageadora muscular recarregavel", "preco_min": 80, "preco_max": 380},
    ],
}

STOPWORDS_TEMA = {
    "de",
    "da",
    "do",
    "das",
    "dos",
    "para",
    "com",
    "sem",
    "em",
    "no",
    "na",
    "nos",
    "nas",
    "por",
    "ate",
    "tem",
    "oferta",
    "ofertas",
    "conheca",
    "modelos",
    "comprar",
    "melhores",
    "recentes",
    "boa",
    "bom",
    "black",
    "friday",
    "natal",
    "verao",
    "volta",
    "aulas",
    "dia",
    "maes",
    "pais",
    "namorados",
    "criancas",
}


class TermoGerado(BaseModel):
    # schema pydantic de um termo de busca gerado pelo estrategista
    termo: str = Field(min_length=3, max_length=90)
    nicho: str = Field(min_length=3, max_length=40)
    prioridade: int = Field(ge=1, le=10, default=5)
    tema_id: int | None = None
    filtros: dict = Field(default_factory=lambda: {"ordenacao": "mais_vendidos"})


class RespostaEstrategistaIA(BaseModel):
    # envelope json esperado da chamada ao gemini no estrategista
    buscas: list[TermoGerado]


def carregar_max_buscas_dia(custom_config_path: Path | None = None) -> int:
    # le limites.max_buscas_dia do config.yaml (padrao 30)
    caminho = custom_config_path or CONFIG_PATH
    if not caminho.exists():
        return MAX_BUSCAS_DIA_PADRAO
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados = yaml.safe_load(f) or {}
        limites = dados.get("limites") or {}
        val = int(limites.get("max_buscas_dia", MAX_BUSCAS_DIA_PADRAO))
        return val if val > 0 else MAX_BUSCAS_DIA_PADRAO
    except (yaml.YAMLError, OSError, TypeError, ValueError):
        return MAX_BUSCAS_DIA_PADRAO


def limpar_termo_busca(texto: str) -> str:
    # normaliza um termo de busca sem pontuacao estranha e em minusculo
    limpo = sem_acento_minusculo(texto)
    limpo = re.sub(r"[^a-z0-9\s\-]", " ", limpo)
    palavras = [p for p in limpo.split() if len(p) > 1 and p not in STOPWORDS_TEMA]
    resultado = " ".join(palavras[:6]).strip()
    return resultado


def gerar_termos_para_tema_deterministico(
    tema_dict: dict,
    vetos: dict[str, list[str]] | None = None,
) -> list[dict]:
    # gera de 3 a 5 termos de busca validos para um tema usando templates do nicho e palavras do tema
    regras_veto = vetos if vetos is not None else carregar_vetos()
    texto_tema = str(tema_dict.get("tema") or "")
    if contem_veto(texto_tema, regras_veto):
        return []

    nicho = inferir_nicho_tema(texto_tema, tema_dict.get("nicho")) or "eletronicos"
    nota = float(tema_dict.get("nota_tendencia") or 7.0)
    prioridade_base = max(1, min(10, round(nota)))
    tema_id = tema_dict.get("id")

    candidatos_termo: list[dict] = []
    vistos: set[str] = set()

    # extrai nucleo limpo do proprio tema como primeiro termo especifico
    nucleo = limpar_termo_busca(texto_tema)
    if len(nucleo) >= 4 and not contem_veto(nucleo, regras_veto):
        vistos.add(nucleo)
        candidatos_termo.append(
            TermoGerado(
                termo=nucleo,
                nicho=nicho,
                prioridade=prioridade_base,
                tema_id=tema_id,
                filtros={"ordenacao": "mais_vendidos"},
            ).model_dump()
        )

    # complementa com os templates do nicho ate ter entre 3 e 5 termos
    for tpl in TEMPLATES_TERMOS_POR_NICHO.get(nicho, TEMPLATES_TERMOS_POR_NICHO["eletronicos"]):
        termo_tpl = limpar_termo_busca(tpl["termo"])
        if not termo_tpl or termo_tpl in vistos or contem_veto(termo_tpl, regras_veto):
            continue
        vistos.add(termo_tpl)
        filtros = {
            "ordenacao": "mais_vendidos",
            "preco_min": tpl.get("preco_min", 40),
            "preco_max": tpl.get("preco_max", 1200),
        }
        candidatos_termo.append(
            TermoGerado(
                termo=termo_tpl,
                nicho=nicho,
                prioridade=max(1, prioridade_base - 1),
                tema_id=tema_id,
                filtros=filtros,
            ).model_dump()
        )
        if len(candidatos_termo) >= 4:
            break

    return candidatos_termo[:5]


def _chamar_gemini_estrategista(temas: list[dict], modelo: str) -> tuple[str, int, int]:
    # chama a api do gemini para expandir temas em 3 a 5 termos de busca por tema
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    client = genai.Client(api_key=api_key)
    resumo = [
        {"id": t.get("id"), "tema": t.get("tema"), "nota_tendencia": t.get("nota_tendencia")}
        for t in temas[:15]
    ]
    prompt = (
        "Voce e o Estrategista do BYTZ. Transforme cada tema em 3 a 5 termos de busca "
        "curtos e especificos para e-commerce nos nichos: eletronicos, setup, smart home, "
        "acessorios moveis, home-office e fitness.\n"
        "Retorne APENAS JSON valido no formato: "
        '{"buscas": [{"termo": "...", "nicho": "eletronicos", "prioridade": 8, '
        '"tema_id": 1, "filtros": {"ordenacao": "mais_vendidos"}}]}\n'
        f"Temas: {json.dumps(resumo, ensure_ascii=False)}"
    )
    resp = client.models.generate_content(
        model=modelo,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=600,
            temperature=0.2,
        ),
    )
    usage = getattr(resp, "usage_metadata", None)
    tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
    tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)
    return str(resp.text or ""), tokens_in, tokens_out


def gerar_termos_exploracao(
    temas: list[dict],
    vetos: dict[str, list[str]] | None = None,
    llm_callable: Callable[[list[dict]], str | tuple[str, int, int]] | None = None,
    custom_db_path: Path | None = None,
    config_ia_override: dict | None = None,
) -> list[dict]:
    # expande a lista de temas em termos de busca validando com pydantic e filtrando vetos
    logger = get_logger("strategist")
    regras_veto = vetos if vetos is not None else carregar_vetos()
    if not temas:
        return []

    modelo_env = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    orcamento_ok = pode_gastar_ia(
        custom_db_path=custom_db_path,
        config_override=config_ia_override,
        modelo=modelo_env,
    )

    deve_tentar_ia = llm_callable is not None or (bool(api_key) and orcamento_ok)
    if deve_tentar_ia and (llm_callable is None or orcamento_ok or config_ia_override is None):
        if config_ia_override is not None and not orcamento_ok:
            logger.info("Orcamento de IA esgotado no Estrategista, acionando templates deterministicos.")
        else:
            try:
                if llm_callable is not None:
                    retorno = llm_callable(temas)
                    if isinstance(retorno, tuple):
                        raw_json, t_in, t_out = retorno
                    else:
                        raw_json, t_in, t_out = str(retorno), 0, 0
                else:
                    raw_json, t_in, t_out = _chamar_gemini_estrategista(temas, modelo_env)

                parsed = RespostaEstrategistaIA.model_validate_json(raw_json)
                aprovados: list[dict] = []
                vistos: set[str] = set()

                for item in parsed.buscas:
                    termo_norm = sem_acento_minusculo(item.termo)
                    if len(termo_norm) < 3 or termo_norm in vistos:
                        continue
                    if contem_veto(termo_norm, regras_veto):
                        continue
                    nicho_valido = inferir_nicho_tema(termo_norm, item.nicho)
                    if nicho_valido is None or nicho_valido not in NICHOS_OFICIAIS:
                        continue
                    vistos.add(termo_norm)
                    dados = item.model_dump()
                    dados["termo"] = termo_norm
                    dados["nicho"] = nicho_valido
                    aprovados.append(dados)

                if aprovados:
                    if (t_in > 0 or t_out > 0) and orcamento_ok:
                        registrar_gasto(
                            modelo=modelo_env,
                            tokens_in=t_in,
                            tokens_out=t_out,
                            custom_db_path=custom_db_path,
                            config_override=config_ia_override,
                        )
                    return aprovados
            except (ValidationError, Exception) as e:  # noqa: BLE001
                logger.warning(f"Fallback deterministico acionado no Estrategista apos falha da IA: {e}")

    # gera termos deterministicamente para cada tema garantindo deduplicacao e respeito aos vetos
    todos_termos: list[dict] = []
    vistos_det: set[str] = set()
    for tema in temas:
        gerados = gerar_termos_para_tema_deterministico(tema, vetos=regras_veto)
        for g in gerados:
            chave = sem_acento_minusculo(g["termo"])
            if chave not in vistos_det and not contem_veto(chave, regras_veto):
                vistos_det.add(chave)
                g["termo"] = chave
                todos_termos.append(g)

    return todos_termos


def avaliar_aposentadoria_termo(
    registro_busca: dict,
    data_ref: date,
    nichos_sazonais_ativos: set[str] | None = None,
) -> tuple[int, str | None, int]:
    # calcula (ativo, inativo_ate, execucoes_sem_aprovado) aplicando a regra de 3 falhas e 14 dias
    sazonais = {sem_acento_minusculo(n) for n in (nichos_sazonais_ativos or set())}
    nicho_termo = sem_acento_minusculo(str(registro_busca.get("nicho") or ""))
    sem_aprovado = int(registro_busca.get("execucoes_sem_aprovado") or 0)
    ativo_atual = int(registro_busca.get("ativo", 1))
    inativo_ate_atual = registro_busca.get("inativo_ate")
    hoje_iso = data_ref.strftime("%Y-%m-%d")

    # se estava inativo mas o prazo de 14 dias venceu ou o nicho entrou em sazonalidade, reativa
    if ativo_atual == 0:
        if nicho_termo in sazonais or (inativo_ate_atual and str(inativo_ate_atual) <= hoje_iso):
            return 1, None, 0
        return 0, inativo_ate_atual, sem_aprovado

    # se atingiu 3 execucoes seguidas sem produto aprovado, aposenta por 14 dias
    if sem_aprovado >= LIMITE_FALHAS_SEGUIDAS:
        ate_iso = (data_ref + timedelta(days=DIAS_APOSENTADORIA)).strftime("%Y-%m-%d")
        return 0, ate_iso, sem_aprovado

    return 1, None, sem_aprovado


def selecionar_orcamento_80_20(
    buscas_historico: list[dict],
    buscas_exploracao: list[dict],
    max_buscas_dia: int = MAX_BUSCAS_DIA_PADRAO,
    vetos: dict[str, list[str]] | None = None,
) -> list[dict]:
    # aplica a regra 80% historico comprovado e 20% exploracao respeitando max_buscas_dia e vetos
    regras_veto = vetos if vetos is not None else carregar_vetos()
    limite_total = max(1, int(max_buscas_dia))
    cota_historico = round(limite_total * 0.8)

    hist_validos = [
        b
        for b in buscas_historico
        if int(b.get("ativo", 1)) == 1
        and int(b.get("execucoes_sem_aprovado", 0)) < LIMITE_FALHAS_SEGUIDAS
        and not contem_veto(str(b.get("termo") or ""), regras_veto)
    ]
    hist_ordenados = sorted(
        hist_validos,
        key=lambda b: (int(b.get("aprovados_total") or 0), int(b.get("prioridade") or 5)),
        reverse=True,
    )

    selecionados: list[dict] = []
    termos_vistos: set[str] = set()

    for item in hist_ordenados[:cota_historico]:
        termo_norm = sem_acento_minusculo(str(item["termo"]))
        if termo_norm and termo_norm not in termos_vistos:
            termos_vistos.add(termo_norm)
            copia = dict(item)
            copia["termo"] = termo_norm
            copia["origem_selecao"] = "historico"
            selecionados.append(copia)

    expl_ordenados = sorted(
        buscas_exploracao,
        key=lambda b: int(b.get("prioridade") or 5),
        reverse=True,
    )
    for item in expl_ordenados:
        if len(selecionados) >= limite_total:
            break
        termo_norm = sem_acento_minusculo(str(item.get("termo") or ""))
        if not termo_norm or termo_norm in termos_vistos or contem_veto(termo_norm, regras_veto):
            continue
        termos_vistos.add(termo_norm)
        copia = dict(item)
        copia["termo"] = termo_norm
        copia["origem_selecao"] = "exploracao"
        selecionados.append(copia)

    # se ainda sobrar espaco no orcamento do dia, preenche com o restante do historico valido
    for item in hist_ordenados[cota_historico:]:
        if len(selecionados) >= limite_total:
            break
        termo_norm = sem_acento_minusculo(str(item["termo"]))
        if termo_norm and termo_norm not in termos_vistos:
            termos_vistos.add(termo_norm)
            copia = dict(item)
            copia["termo"] = termo_norm
            copia["origem_selecao"] = "historico"
            selecionados.append(copia)

    return selecionados[:limite_total]


def _obter_nichos_sazonais_ativos(data_ref: date, custom_sazonalidade_path: Path | None = None) -> set[str]:
    # identifica quais nichos estao em periodo sazonal ativo na data de referencia
    dados = carregar_sazonalidade(custom_sazonalidade_path)
    candidatos = avaliar_sazonalidade(data_ref=data_ref, dados_sazonalidade=dados)
    return {str(c["nicho"]) for c in candidatos if c.get("sazonal") and c.get("nicho")}


def run(
    data_ref: date | None = None,
    temas_override: list[dict] | None = None,
    custom_db_path: Path | None = None,
    custom_config_path: Path | None = None,
    custom_vetos_path: Path | None = None,
    custom_sazonalidade_path: Path | None = None,
    llm_callable: Callable | None = None,
    config_ia_override: dict | None = None,
) -> dict:
    # executa o estrategista de forma idempotente, atualizando buscas e registrando em execucoes
    init_db(custom_db_path)
    agora_dt = datetime.now(TZ_SP)
    inicio_iso = agora_dt.isoformat(timespec="seconds")
    ref_date = data_ref or agora_dt.date()
    logger = get_logger("strategist")

    try:
        max_buscas = carregar_max_buscas_dia(custom_config_path)
        vetos = carregar_vetos(custom_vetos_path)
        nichos_sazonais = _obter_nichos_sazonais_ativos(ref_date, custom_sazonalidade_path)

        conn = get_connection(custom_db_path)
        try:
            # 1. aplica aposentadoria (3 falhas -> 14 dias) e reativacao sazonal/temporal no banco
            rows_existentes = conn.execute("SELECT * FROM buscas;").fetchall()
            historico_banco: list[dict] = []
            aposentados = 0
            reativados = 0

            with conn:
                for row in rows_existentes:
                    reg = dict(row)
                    novo_ativo, novo_inativo_ate, novo_sem_aprov = avaliar_aposentadoria_termo(
                        reg, data_ref=ref_date, nichos_sazonais_ativos=nichos_sazonais
                    )
                    if contem_veto(str(reg.get("termo") or ""), vetos):
                        novo_ativo = 0

                    if (
                        novo_ativo != reg["ativo"]
                        or novo_inativo_ate != reg["inativo_ate"]
                        or novo_sem_aprov != reg["execucoes_sem_aprovado"]
                    ):
                        if reg["ativo"] == 1 and novo_ativo == 0:
                            aposentados += 1
                        elif reg["ativo"] == 0 and novo_ativo == 1:
                            reativados += 1
                        conn.execute(
                            """
                            UPDATE buscas
                            SET ativo = ?, inativo_ate = ?, execucoes_sem_aprovado = ?
                            WHERE id = ?;
                            """,
                            (novo_ativo, novo_inativo_ate, novo_sem_aprov, reg["id"]),
                        )
                    reg["ativo"] = novo_ativo
                    reg["inativo_ate"] = novo_inativo_ate
                    reg["execucoes_sem_aprovado"] = novo_sem_aprov
                    if novo_ativo == 1 and (
                        int(reg.get("aprovados_total") or 0) > 0 or int(reg.get("execucoes") or 0) > 0
                    ):
                        historico_banco.append(reg)

            # 2. carrega os temas do scout para transformar em termos de exploracao
            if temas_override is not None:
                temas_insumo = list(temas_override)
            else:
                rows_temas = conn.execute("""
                    SELECT id, data, tema, fonte, nota_tendencia, justificativa, usado
                    FROM temas
                    ORDER BY data DESC, nota_tendencia DESC
                    LIMIT 15;
                    """).fetchall()
                temas_insumo = [dict(r) for r in rows_temas]

                if not temas_insumo:
                    # se ainda nao houver temas no banco, usa temas sazonais/base para nao travar
                    temas_insumo = avaliar_sazonalidade(data_ref=ref_date)[:10]

            termos_exploracao = gerar_termos_exploracao(
                temas=temas_insumo,
                vetos=vetos,
                llm_callable=llm_callable,
                custom_db_path=custom_db_path,
                config_ia_override=config_ia_override,
            )

            selecionados = selecionar_orcamento_80_20(
                buscas_historico=historico_banco,
                buscas_exploracao=termos_exploracao,
                max_buscas_dia=max_buscas,
                vetos=vetos,
            )

            # 3. persiste os termos selecionados respeitando o limite max_buscas_dia de buscas ativas
            inseridos = 0
            atualizados = 0
            termos_ativos_hoje = {s["termo"] for s in selecionados}
            ids_temas_usados = {int(t["id"]) for t in temas_insumo if t.get("id") is not None}

            with conn:
                # desativa buscas ativas excedentes que ficaram fora do orcamento de hoje sem aposenta-las
                if termos_ativos_hoje:
                    placeholders = ",".join("?" for _ in termos_ativos_hoje)
                    conn.execute(
                        f"""
                        UPDATE buscas
                        SET ativo = 0
                        WHERE ativo = 1 AND inativo_ate IS NULL AND termo NOT IN ({placeholders});
                        """,
                        tuple(termos_ativos_hoje),
                    )

                for item in selecionados:
                    termo_str = item["termo"]
                    nicho_str = item.get("nicho") or "eletronicos"
                    prio_int = int(item.get("prioridade") or 5)
                    tema_id_val = item.get("tema_id")
                    filtros_raw = (
                        item.get("filtros") or item.get("filtros_json") or {"ordenacao": "mais_vendidos"}
                    )
                    filtros_str = (
                        filtros_raw
                        if isinstance(filtros_raw, str)
                        else json.dumps(filtros_raw, ensure_ascii=False)
                    )

                    existente = conn.execute(
                        "SELECT id FROM buscas WHERE termo = ?;",
                        (termo_str,),
                    ).fetchone()

                    if existente:
                        conn.execute(
                            """
                            UPDATE buscas
                            SET tema_id = COALESCE(?, tema_id),
                                nicho = ?,
                                prioridade = ?,
                                filtros_json = ?,
                                ativo = 1
                            WHERE id = ?;
                            """,
                            (tema_id_val, nicho_str, prio_int, filtros_str, existente["id"]),
                        )
                        atualizados += 1
                    else:
                        conn.execute(
                            """
                            INSERT INTO buscas (
                                termo, tema_id, nicho, prioridade, filtros_json, ativo
                            ) VALUES (?, ?, ?, ?, ?, 1);
                            """,
                            (termo_str, tema_id_val, nicho_str, prio_int, filtros_str),
                        )
                        inseridos += 1

                for tid in ids_temas_usados:
                    conn.execute("UPDATE temas SET usado = 1 WHERE id = ?;", (tid,))

                fim_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
                conn.execute(
                    """
                    INSERT INTO execucoes (modulo, inicio, fim, status, itens, erro)
                    VALUES ('strategist', ?, ?, 'sucesso', ?, NULL);
                    """,
                    (inicio_iso, fim_iso, len(selecionados)),
                )
        finally:
            conn.close()

        logger.info(
            f"Estrategista concluido: ativas_dia={len(selecionados)} (max={max_buscas}), "
            f"inseridos={inseridos}, atualizados={atualizados}, aposentados={aposentados}"
        )
        return {
            "buscas_ativas": len(selecionados),
            "inseridos": inseridos,
            "atualizados": atualizados,
            "aposentados": aposentados,
            "reativados": reativados,
            "selecionados": selecionados,
        }
    except Exception as e:
        fim_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
        conn = get_connection(custom_db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO execucoes (modulo, inicio, fim, status, itens, erro)
                    VALUES ('strategist', ?, ?, 'erro', 0, ?);
                    """,
                    (inicio_iso, fim_iso, str(e)),
                )
        finally:
            conn.close()
        raise
