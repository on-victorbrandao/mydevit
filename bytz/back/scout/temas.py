# deduplicacao, avaliacao de tendencia (ia com fallback deterministico) e persistencia de temas do scout
import json
import os
import unicodedata
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from core.budget import pode_gastar_ia, registrar_gasto
from core.db import get_connection, init_db
from core.logger import get_logger
from scout.fontes import NICHOS_OFICIAIS, TZ_SP, coletar_candidatos_scout

PASTA_BACK = Path(__file__).resolve().parent.parent
VETOS_PATH = PASTA_BACK / "config" / "vetos.yaml"
TOP_TEMAS_DIA = 15

# palavras-chave por nicho para filtrar noticias gerais fora do escopo do bytz
PALAVRAS_NICHO: dict[str, tuple[str, ...]] = {
    "eletronicos": (
        "fone",
        "bluetooth",
        "earbuds",
        "headphone",
        "caixa de som",
        "jbl",
        "smartwatch",
        "relogio inteligente",
        "tv",
        "smart tv",
        "tablet",
        "kindle",
        "projetor",
        "soundbar",
    ),
    "setup": (
        "teclado",
        "mouse",
        "monitor",
        "gamer",
        "headset",
        "microfone",
        "ssd",
        "nvme",
        "placa de video",
        "mousepad",
        "controle sem fio",
        "gamepad",
    ),
    "smart home": (
        "smart home",
        "casa inteligente",
        "lampada inteligente",
        "lampada smart",
        "alexa",
        "echo dot",
        "tomada inteligente",
        "tomada smart",
        "fechadura digital",
        "camera wifi",
        "camera de seguranca",
        "robo aspirador",
        "interruptor inteligente",
    ),
    "acessorios moveis": (
        "carregador",
        "power bank",
        "bateria portatil",
        "magsafe",
        "cabo usb",
        "usb-c",
        "gan",
        "suporte veicular",
        "capinha",
        "pelicula",
        "tag rastreador",
        "airtag",
        "iphone",
        "galaxy",
        "motorola",
        "xiaomi",
    ),
    "home-office": (
        "home-office",
        "home office",
        "escritorio",
        "ergonomico",
        "ergonomica",
        "cadeira",
        "suporte para notebook",
        "suporte articulado",
        "hub usb",
        "dock station",
        "webcam",
        "luminaria",
        "barra de luz",
        "apoio de pes",
        "notebook",
    ),
    "fitness": (
        "fitness",
        "academia",
        "treino",
        "garrafa termica",
        "copo termico",
        "coqueteleira",
        "halter",
        "elastico",
        "extensor",
        "corda de pular",
        "colchonete",
        "yoga",
        "balanca",
        "bioimpedancia",
        "massageador",
        "corrida",
    ),
}


class TemaAvaliado(BaseModel):
    # schema pydantic de um tema avaliado pelo scout
    tema: str = Field(min_length=3, max_length=140)
    fonte: str = Field(min_length=2, max_length=80)
    nota_tendencia: float = Field(ge=0.0, le=10.0)
    justificativa: str = Field(min_length=3, max_length=220)


class RespostaScoutIA(BaseModel):
    # envelope json esperado da chamada ao gemini no scout
    temas: list[TemaAvaliado]


def sem_acento_minusculo(texto: str) -> str:
    # normaliza texto sem acentos e em letras minusculas para comparacao
    nfkd = unicodedata.normalize("NFKD", str(texto or ""))
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def carregar_vetos(custom_vetos_path: Path | None = None) -> dict[str, list[str]]:
    # le a lista de termos e categorias proibidos do config/vetos.yaml
    caminho = custom_vetos_path or VETOS_PATH
    if not caminho.exists():
        return {"termos": [], "categorias": []}
    with open(caminho, "r", encoding="utf-8") as f:
        dados = yaml.safe_load(f) or {}
    return {
        "termos": [sem_acento_minusculo(t) for t in dados.get("termos", []) if t],
        "categorias": [sem_acento_minusculo(c) for c in dados.get("categorias", []) if c],
    }


def contem_veto(texto: str, vetos: dict[str, list[str]] | None = None) -> bool:
    # verifica se o texto contem qualquer palavra ou categoria proibida em vetos.yaml
    regras = vetos if vetos is not None else carregar_vetos()
    norm = sem_acento_minusculo(texto)
    if not norm:
        return True
    for proibido in regras.get("termos", []) + regras.get("categorias", []):
        if proibido and proibido in norm:
            return True
    return False


def inferir_nicho_tema(texto: str, nicho_hint: str | None = None) -> str | None:
    # identifica a qual dos 6 nichos do bytz um candidato pertence
    if nicho_hint:
        hint_norm = sem_acento_minusculo(nicho_hint)
        for n in NICHOS_OFICIAIS:
            if sem_acento_minusculo(n) == hint_norm:
                return n

    norm = sem_acento_minusculo(texto)
    for nicho, palavras in PALAVRAS_NICHO.items():
        for palavra in palavras:
            if palavra in norm:
                return nicho
    return None


def filtrar_e_deduplicar_candidatos(
    candidatos: list[dict],
    vetos: dict[str, list[str]] | None = None,
) -> list[dict]:
    # remove itens vetados, filtra noticias sem relacao com os nichos e elimina duplicatas
    regras_veto = vetos if vetos is not None else carregar_vetos()
    por_chave: dict[str, dict] = {}

    for item in candidatos:
        tema_bruto = " ".join(str(item.get("tema") or "").split())
        if len(tema_bruto) < 3:
            continue
        if contem_veto(tema_bruto, regras_veto):
            continue

        nicho = inferir_nicho_tema(tema_bruto, item.get("nicho"))
        if nicho is None:
            continue

        chave = sem_acento_minusculo(tema_bruto)
        peso = float(item.get("peso_base") or 7.0)
        fonte = str(item.get("fonte") or "desconhecida")
        sazonal = bool(item.get("sazonal", False))

        if chave not in por_chave or peso > float(por_chave[chave]["peso_base"]):
            por_chave[chave] = {
                "tema": tema_bruto[:140],
                "fonte": fonte[:80],
                "nicho": nicho,
                "sazonal": sazonal,
                "peso_base": peso,
            }

    return list(por_chave.values())


def selecionar_top_temas_deterministico(
    candidatos_limpos: list[dict],
    top_n: int = TOP_TEMAS_DIA,
) -> list[dict]:
    # pontua e seleciona os melhores temas balanceando nichos quando a ia nao e utilizada
    ordenados = sorted(
        candidatos_limpos,
        key=lambda c: (1 if c.get("sazonal") else 0, float(c.get("peso_base") or 0.0)),
        reverse=True,
    )

    selecionados: list[dict] = []
    contagem_nicho: dict[str, int] = {}
    sobras: list[dict] = []

    for item in ordenados:
        nicho = str(item.get("nicho") or "eletronicos")
        if contagem_nicho.get(nicho, 0) < 3 and len(selecionados) < top_n:
            selecionados.append(item)
            contagem_nicho[nicho] = contagem_nicho.get(nicho, 0) + 1
        else:
            sobras.append(item)

    for item in sobras:
        if len(selecionados) >= top_n:
            break
        selecionados.append(item)

    resultado: list[dict] = []
    for item in selecionados[:top_n]:
        nota = min(10.0, max(0.0, round(float(item.get("peso_base") or 7.0), 1)))
        fonte = str(item.get("fonte") or "sazonalidade:calendario_base")
        nicho = str(item.get("nicho") or "eletronicos")

        if item.get("sazonal"):
            justificativa = f"Tema sazonal ativo no calendario para o nicho {nicho}."
        elif fonte.startswith(("web:", "firecrawl:")):
            justificativa = f"Tendencia captada na web com afinidade ao nicho {nicho}."
        elif fonte.startswith("historico:"):
            justificativa = f"Termo com historico comprovado de aprovacao no nicho {nicho}."
        elif fonte.startswith("cache:"):
            justificativa = f"Recuperado do cache recente para continuidade do nicho {nicho}."
        else:
            justificativa = f"Demanda recorrente do calendario base para {nicho}."

        validado = TemaAvaliado(
            tema=str(item["tema"]),
            fonte=fonte,
            nota_tendencia=nota,
            justificativa=justificativa,
        )
        resultado.append(validado.model_dump())

    return resultado


def _chamar_gemini_scout(candidatos_limpos: list[dict], modelo: str) -> tuple[str, int, int]:
    # executa chamada real ao gemini-api pedindo json estruturado com os top temas
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    client = genai.Client(api_key=api_key)
    resumo_entrada = [
        {"tema": c["tema"], "fonte": c["fonte"], "nicho": c["nicho"], "sazonal": c["sazonal"]}
        for c in candidatos_limpos[:35]
    ]
    prompt = (
        "Voce e o Scout do BYTZ. Agrupe e selecione ate 15 temas promissores de ofertas "
        "nos nichos eletronicos, setup, smart home, acessorios moveis, home-office e fitness.\n"
        "Retorne APENAS JSON valido no formato: "
        '{"temas": [{"tema": "...", "fonte": "...", "nota_tendencia": 8.5, "justificativa": "..."}]}\n'
        f"Candidatos: {json.dumps(resumo_entrada, ensure_ascii=False)}"
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


def avaliar_temas(
    candidatos_brutos: list[dict],
    top_n: int = TOP_TEMAS_DIA,
    vetos: dict[str, list[str]] | None = None,
    llm_callable: Callable[[list[dict]], str | tuple[str, int, int]] | None = None,
    custom_db_path: Path | None = None,
    config_ia_override: dict | None = None,
) -> list[dict]:
    # filtra candidatos e usa ia (quando disponivel e dentro do teto) ou fallback deterministico
    logger = get_logger("scout")
    regras_veto = vetos if vetos is not None else carregar_vetos()
    limpos = filtrar_e_deduplicar_candidatos(candidatos_brutos, vetos=regras_veto)
    if not limpos:
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
            logger.info("Orcamento de IA esgotado ou indisponivel, usando selecao deterministica no Scout.")
        else:
            try:
                if llm_callable is not None:
                    retorno = llm_callable(limpos)
                    if isinstance(retorno, tuple):
                        raw_json, t_in, t_out = retorno
                    else:
                        raw_json, t_in, t_out = str(retorno), 0, 0
                else:
                    raw_json, t_in, t_out = _chamar_gemini_scout(limpos, modelo_env)

                parsed = RespostaScoutIA.model_validate_json(raw_json)
                aprovados_ia: list[dict] = []
                vistos_ia: set[str] = set()

                for t in parsed.temas:
                    if contem_veto(t.tema, regras_veto):
                        continue
                    if inferir_nicho_tema(t.tema) is None:
                        continue
                    chave = sem_acento_minusculo(t.tema)
                    if chave in vistos_ia:
                        continue
                    vistos_ia.add(chave)
                    aprovados_ia.append(t.model_dump())

                if aprovados_ia:
                    if (t_in > 0 or t_out > 0) and orcamento_ok:
                        registrar_gasto(
                            modelo=modelo_env,
                            tokens_in=t_in,
                            tokens_out=t_out,
                            custom_db_path=custom_db_path,
                            config_override=config_ia_override,
                        )
                    aprovados_ia.sort(key=lambda x: float(x["nota_tendencia"]), reverse=True)
                    return aprovados_ia[:top_n]
            except (ValidationError, Exception) as e:  # noqa: BLE001
                logger.warning(f"Fallback deterministico acionado no Scout apos falha da IA: {e}")

    return selecionar_top_temas_deterministico(limpos, top_n=top_n)


def gravar_temas_sqlite(
    temas_avaliados: list[dict],
    data_iso: str,
    inicio_iso: str,
    custom_db_path: Path | None = None,
    status_exec: str = "sucesso",
    erro_exec: str | None = None,
) -> dict:
    # grava os top temas do dia de forma idempotente na tabela temas e registra em execucoes
    init_db(custom_db_path)
    agora_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
    inseridos = 0
    atualizados = 0

    conn = get_connection(custom_db_path)
    try:
        with conn:
            for item in temas_avaliados:
                existente = conn.execute(
                    """
                    SELECT id FROM temas
                    WHERE data = ? AND lower(tema) = lower(?);
                    """,
                    (data_iso, item["tema"]),
                ).fetchone()

                if existente:
                    conn.execute(
                        """
                        UPDATE temas
                        SET fonte = ?, nota_tendencia = ?, justificativa = ?
                        WHERE id = ?;
                        """,
                        (
                            item["fonte"],
                            float(item["nota_tendencia"]),
                            item["justificativa"],
                            int(existente["id"]),
                        ),
                    )
                    atualizados += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO temas (data, tema, fonte, nota_tendencia, justificativa, usado)
                        VALUES (?, ?, ?, ?, ?, 0);
                        """,
                        (
                            data_iso,
                            item["tema"],
                            item["fonte"],
                            float(item["nota_tendencia"]),
                            item["justificativa"],
                        ),
                    )
                    inseridos += 1

            conn.execute(
                """
                INSERT INTO execucoes (modulo, inicio, fim, status, itens, erro)
                VALUES ('scout', ?, ?, ?, ?, ?);
                """,
                (inicio_iso, agora_iso, status_exec, inseridos, erro_exec),
            )
    finally:
        conn.close()

    return {"inseridos": inseridos, "atualizados": atualizados, "total_dia": len(temas_avaliados)}


def run(
    data_ref: date | None = None,
    fontes_web: list[dict[str, str]] | None = None,
    custom_db_path: Path | None = None,
    custom_sazonalidade_path: Path | None = None,
    custom_vetos_path: Path | None = None,
    custom_cache_path: Path | None = None,
    custom_config_path: Path | None = None,
    llm_callable: Callable | None = None,
    config_ia_override: dict | None = None,
    http_get: Callable | None = None,
    http_post: Callable | None = None,
) -> dict:
    # executa o ciclo diario do scout de ponta a ponta com idempotencia e resiliencia
    agora_dt = datetime.now(TZ_SP)
    inicio_iso = agora_dt.isoformat(timespec="seconds")
    ref_date = data_ref or agora_dt.date()
    data_iso = ref_date.strftime("%Y-%m-%d")
    logger = get_logger("scout")

    try:
        coleta = coletar_candidatos_scout(
            data_ref=ref_date,
            fontes_web=fontes_web,
            custom_sazonalidade_path=custom_sazonalidade_path,
            custom_cache_path=custom_cache_path,
            custom_db_path=custom_db_path,
            custom_config_path=custom_config_path,
            http_get=http_get,
            http_post=http_post,
        )
        vetos = carregar_vetos(custom_vetos_path)
        temas_top = avaliar_temas(
            candidatos_brutos=coleta["candidatos"],
            top_n=TOP_TEMAS_DIA,
            vetos=vetos,
            llm_callable=llm_callable,
            custom_db_path=custom_db_path,
            config_ia_override=config_ia_override,
        )
        res_db = gravar_temas_sqlite(
            temas_avaliados=temas_top,
            data_iso=data_iso,
            inicio_iso=inicio_iso,
            custom_db_path=custom_db_path,
            status_exec="sucesso",
        )
        logger.info(
            f"Scout concluido para {data_iso}: top={len(temas_top)}, "
            f"inseridos={res_db['inseridos']}, usou_cache={coleta['usou_cache']}"
        )
        return {
            "data": data_iso,
            "temas": temas_top,
            "inseridos": res_db["inseridos"],
            "atualizados": res_db["atualizados"],
            "usou_cache": coleta["usou_cache"],
            "erros_web": coleta["erros_web"],
        }
    except Exception as e:
        gravar_temas_sqlite(
            temas_avaliados=[],
            data_iso=data_iso,
            inicio_iso=inicio_iso,
            custom_db_path=custom_db_path,
            status_exec="erro",
            erro_exec=str(e),
        )
        raise
