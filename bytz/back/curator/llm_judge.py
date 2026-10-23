# juiz llm em lote (ate 20 produtos) com cache por produto+termo, controle de orcamento e fallback
import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from core.alerts import send_alert
from core.budget import (
    BudgetConfigError,
    calcular_custo_estimado,
    carregar_config_ia,
    consultar_gasto_mes,
    pode_gastar_ia,
    registrar_gasto,
)
from core.logger import get_logger
from scout.temas import PALAVRAS_NICHO, carregar_vetos, contem_veto, inferir_nicho_tema, sem_acento_minusculo

PASTA_BACK = Path(__file__).resolve().parent.parent
TZ_SP = ZoneInfo("America/Sao_Paulo")
CACHE_JUIZ_PATH = PASTA_BACK / "data" / "curator_llm_cache.json"
MAX_PRODUTOS_POR_LOTE = 20

PADROES_SUSPEITOS_TITULO = (
    "primeira linha",
    "1a linha",
    "replica",
    "similar ao original",
    "tipo original",
    "igual original",
    "aaa+",
    "falsificado",
    "clone",
    "cura ",
    "milagroso",
    "emagrece",
    "queima gordura",
    "anvisa isento",
    "sem anvisa",
)

STOPWORDS_DUPLICATA = {
    "de",
    "da",
    "do",
    "das",
    "dos",
    "para",
    "com",
    "sem",
    "em",
    "por",
    "e",
    "ou",
    "kit",
    "original",
    "premium",
    "oferta",
    "promocao",
    "preto",
    "branco",
    "azul",
    "rosa",
    "verde",
    "cinza",
    "prata",
    "cor",
    "universal",
    "compativel",
    "todos",
    "os",
    "as",
}


class OrcamentoEstouradoError(RuntimeError):
    # erro disparado quando o custo acumulado ou projetado atinge o teto mensal de ia
    pass


class AvaliacaoJuizItem(BaseModel):
    # schema pydantic de avaliacao individual do juiz llm para cada produto
    id_produto: str = Field(min_length=3, max_length=40)
    afinidade: float = Field(ge=0.0, le=15.0)
    suspeito: bool = False
    motivo: str = Field(min_length=2, max_length=160)
    duplicado_de: str | None = None


class RespostaJuizLote(BaseModel):
    # envelope json retornado pelo gemini contendo as avaliacoes do lote
    avaliacoes: list[AvaliacaoJuizItem]


def gerar_chave_cache_juiz(id_produto: str, termo_origem: str | None = None) -> str:
    # monta chave unica normalizada combinando id_produto e termo_origem para o cache
    id_norm = str(id_produto or "").strip().upper()
    termo_norm = sem_acento_minusculo(str(termo_origem or "geral")) or "geral"
    return f"{id_norm}::{termo_norm}"


def carregar_cache_juiz(custom_cache_path: Path | None = None) -> dict[str, dict]:
    # carrega o dicionario de avaliacoes em cache do disco sem quebrar se ausente ou corrompido
    caminho = custom_cache_path or CACHE_JUIZ_PATH
    if not caminho.exists():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        if isinstance(dados, dict) and isinstance(dados.get("itens"), dict):
            return dados["itens"]
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def salvar_cache_juiz(itens_cache: dict[str, dict], custom_cache_path: Path | None = None) -> None:
    # persiste o cache do juiz llm em escrita atomica com timestamp
    caminho = custom_cache_path or CACHE_JUIZ_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "atualizado_em": datetime.now(TZ_SP).isoformat(timespec="seconds"),
        "itens": itens_cache,
    }
    temp_path = caminho.with_suffix(caminho.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, caminho)


def detectar_motivo_suspeito(
    produto: dict,
    vetos: dict[str, list[str]] | None = None,
) -> str | None:
    # identifica marca enganosa, replica, promessa medica, item regulado ou preco bom demais
    regras_veto = vetos if vetos is not None else carregar_vetos()
    nome = str(produto.get("nome_produto") or "")
    categoria = str(produto.get("categoria") or "")
    termo = str(produto.get("termo_origem") or "")
    texto_completo = f"{nome} {categoria} {termo}".strip()
    norm = sem_acento_minusculo(texto_completo)

    if not sem_acento_minusculo(nome):
        return "Produto sem titulo valido para curadoria."

    if contem_veto(texto_completo, regras_veto):
        return "Contem termo ou categoria proibido em vetos.yaml."

    for padrao in PADROES_SUSPEITOS_TITULO:
        if padrao in norm:
            return f"Titulo suspeito de replica, marca enganosa ou promessa medica ({padrao.strip()})."

    val_orig = float(produto.get("valor_original") or 0.0)
    val_atual = float(produto.get("valor_atual") or 0.0)
    desc_pct = float(produto.get("desconto_pct") or 0.0)
    if val_orig > 0 and val_atual > 0 and desc_pct <= 0:
        desc_pct = ((val_orig - val_atual) / val_orig) * 100.0

    # desconto acima de 85% em eletronicos quase sempre indica falso preco riscado ou acessorio trocado
    if desc_pct >= 85.0 or (val_orig > 0 and val_atual > 0 and val_atual < val_orig * 0.15):
        return "Preco bom demais para ser verdade (desconto acima de 85%)."

    # power bank prometendo 50.000mAh ou mais por preco baixo indica especificacao falsa
    match_mah = re.search(r"(\d{2,3})\s*\.?\s*000\s*mah", norm)
    if match_mah and int(match_mah.group(1)) >= 50 and val_atual < 220.0:
        return "Capacidade nominal (50.000mAh+) incompativel com a faixa de preco e porte."

    return None


def _tokens_nucleo_titulo(nome_produto: str) -> set[str]:
    # extrai palavras relevantes do titulo para comparacao de duplicidade semantica no lote
    norm = sem_acento_minusculo(nome_produto)
    norm = re.sub(r"[^a-z0-9\s]", " ", norm)
    tokens = [
        t
        for t in norm.split()
        if len(t) >= 3 and t not in STOPWORDS_DUPLICATA and not re.fullmatch(r"\d+w?", t)
    ]
    return set(tokens[:10])


def detectar_duplicatas_no_lote(produtos: list[dict]) -> dict[str, str]:
    # retorna mapa {id_duplicado: id_original} quando dois itens do lote representam o mesmo produto
    ordenados = sorted(
        produtos,
        key=lambda p: (
            int(p.get("vendas") or 0),
            float(p.get("nota") or 0.0),
            float(p.get("desconto_pct") or 0.0),
        ),
        reverse=True,
    )
    assinaturas_vistas: list[tuple[str, set[str], float]] = []
    duplicados: dict[str, str] = {}

    for prod in ordenados:
        id_prod = str(prod.get("id_produto") or "")
        if not id_prod:
            continue
        tokens = _tokens_nucleo_titulo(str(prod.get("nome_produto") or ""))
        preco = float(prod.get("valor_atual") or 0.0)
        if len(tokens) < 4:
            continue

        for id_ref, tokens_ref, preco_ref in assinaturas_vistas:
            intersecao = len(tokens & tokens_ref)
            uniao = len(tokens | tokens_ref)
            similaridade = (intersecao / uniao) if uniao > 0 else 0.0
            diff_preco_rel = abs(preco - preco_ref) / max(preco_ref, 1.0)
            if similaridade >= 0.80 and diff_preco_rel <= 0.12:
                duplicados[id_prod] = id_ref
                break

        if id_prod not in duplicados:
            assinaturas_vistas.append((id_prod, tokens, preco))

    return duplicados


def calcular_afinidade_deterministica(
    produto: dict,
    termos_alto_ctr: set[str] | None = None,
) -> tuple[float, str]:
    # calcula nota de afinidade (0 a 15) e justificativa curta com base no nicho e historico
    nome = str(produto.get("nome_produto") or "")
    termo = str(produto.get("termo_origem") or "")
    categoria = str(produto.get("categoria") or "")
    nicho = inferir_nicho_tema(f"{nome} {termo}", categoria)

    if nicho is None:
        return 6.0, "Afinidade moderada: item fora das palavras-chave centrais dos nichos."

    norm_nome = sem_acento_minusculo(nome)
    palavras_nicho = PALAVRAS_NICHO.get(nicho, ())
    matches = sum(1 for p in palavras_nicho if p in norm_nome)

    # base de 10.5 para item confirmado no nicho + bonus por especificidade e historico
    nota = 10.5 + min(2.5, matches * 1.0)
    vendas = int(produto.get("vendas") or 0)
    avaliacao = float(produto.get("nota") or 0.0)
    if vendas >= 5000 and avaliacao >= 4.8:
        nota += 1.0
    elif vendas >= 1000 and avaliacao >= 4.7:
        nota += 0.5

    termos_bons = {sem_acento_minusculo(t) for t in (termos_alto_ctr or set()) if t}
    if sem_acento_minusculo(termo) in termos_bons:
        nota += 1.0

    nota_final = round(min(15.0, max(0.0, nota)), 2)
    return nota_final, f"Alta aderencia ao nicho {nicho} com especificacao clara."


def avaliar_lote_deterministico(
    lote_produtos: list[dict],
    vetos: dict[str, list[str]] | None = None,
    termos_alto_ctr: set[str] | None = None,
) -> dict[str, dict]:
    # avalia um lote de produtos sem ia aplicando regras de suspeita, duplicidade e afinidade
    regras_veto = vetos if vetos is not None else carregar_vetos()
    mapa_duplicatas = detectar_duplicatas_no_lote(lote_produtos)
    resultados: dict[str, dict] = {}

    for prod in lote_produtos:
        id_prod = str(prod.get("id_produto") or "")
        if not id_prod:
            continue

        motivo_susp = detectar_motivo_suspeito(prod, vetos=regras_veto)
        duplicado_de = mapa_duplicatas.get(id_prod)

        if motivo_susp:
            item = AvaliacaoJuizItem(
                id_produto=id_prod,
                afinidade=0.0,
                suspeito=True,
                motivo=motivo_susp[:160],
                duplicado_de=duplicado_de,
            )
        elif duplicado_de:
            afinidade, _ = calcular_afinidade_deterministica(prod, termos_alto_ctr=termos_alto_ctr)
            item = AvaliacaoJuizItem(
                id_produto=id_prod,
                afinidade=afinidade,
                suspeito=False,
                motivo=f"Anuncio duplicado de {duplicado_de} no mesmo lote.",
                duplicado_de=duplicado_de,
            )
        else:
            afinidade, motivo_ok = calcular_afinidade_deterministica(prod, termos_alto_ctr=termos_alto_ctr)
            item = AvaliacaoJuizItem(
                id_produto=id_prod,
                afinidade=afinidade,
                suspeito=False,
                motivo=motivo_ok[:160],
                duplicado_de=None,
            )

        resultados[id_prod] = item.model_dump()

    return resultados


def montar_prompt_juiz_lote(
    lote_produtos: list[dict],
    termos_alto_ctr: set[str] | None = None,
) -> str:
    # monta prompt enxuto para avaliar ate 20 produtos de uma vez com saida json controlada
    itens_resumo = [
        {
            "id_produto": str(p.get("id_produto") or ""),
            "nome": str(p.get("nome_produto") or "")[:110],
            "termo": str(p.get("termo_origem") or ""),
            "categoria": str(p.get("categoria") or ""),
            "de": round(float(p.get("valor_original") or 0.0), 2),
            "por": round(float(p.get("valor_atual") or 0.0), 2),
            "desc_pct": round(float(p.get("desconto_pct") or 0.0), 1),
            "nota": float(p.get("nota") or 0.0),
            "vendas": int(p.get("vendas") or 0),
        }
        for p in lote_produtos[:MAX_PRODUTOS_POR_LOTE]
    ]
    historico_str = ", ".join(sorted(termos_alto_ctr)) if termos_alto_ctr else "sem historico previo"
    return (
        "Voce e o Juiz Curador do BYTZ (ofertas de tecnologia, setup, smart home, acessorios moveis, "
        "home-office e fitness).\n"
        "Avalie cada produto e retorne APENAS JSON no formato:\n"
        '{"avaliacoes": [{"id_produto": "...", "afinidade": 0-15, "suspeito": false, '
        '"motivo": "ate 70 chars", "duplicado_de": null}]}\n'
        "Regras:\n"
        "- afinidade (0 a 15): aderencia aos nichos do BYTZ, utilidade real e apelo de clique.\n"
        "- suspeito=true para: marca generica enganosa, replica/falsificacao, preco bom demais "
        "(ex: power bank >=50000mAh barato ou desconto irreal), promessa medica/emagrecimento, item regulado.\n"
        "- duplicado_de: se houver 2 anuncios do mesmo produto no lote, aponte o id_produto do melhor.\n"
        f"- Termos com melhor engajamento recente: {historico_str}\n"
        f"Produtos: {json.dumps(itens_resumo, ensure_ascii=False)}"
    )


def projetar_custo_lote_brl(
    prompt: str,
    modelo: str,
    config_ia: dict,
) -> float:
    # estima o custo maximo da chamada antes de envia-la (tokens do prompt + max_tokens_saida)
    tokens_in_est = max(1, len(prompt) // 3)
    tokens_out_max = int(config_ia.get("max_tokens_saida", 600) or 600)
    return calcular_custo_estimado(
        modelo=modelo,
        tokens_in=tokens_in_est,
        tokens_out=tokens_out_max,
        config_override=config_ia,
    )


def _chamar_gemini_juiz_lote(
    prompt: str,
    modelo: str,
    max_tokens_saida: int = 600,
    max_retries: int = 2,
) -> tuple[str, int, int]:
    # chama a api do gemini com schema json, thinking_budget=0, max_tokens_saida baixo e retry com backoff
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    client = genai.Client(api_key=api_key)

    ultimo_erro: Exception | None = None
    for tentativa in range(max_retries + 1):
        try:
            cfg_kwargs: dict = {
                "response_mime_type": "application/json",
                "response_schema": RespostaJuizLote,
                "max_output_tokens": int(max_tokens_saida),
                "temperature": 0.1,
            }
            if hasattr(types, "ThinkingConfig") and any(v in modelo for v in ("2.5", "3.", "3-")):
                cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

            resp = client.models.generate_content(
                model=modelo,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            usage = getattr(resp, "usage_metadata", None)
            tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
            tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)
            if tokens_in == 0:
                tokens_in = max(1, len(prompt) // 4)
            texto_saida = str(resp.text or "")
            if tokens_out == 0 and texto_saida:
                tokens_out = max(1, len(texto_saida) // 4)
            return texto_saida, tokens_in, tokens_out
        except Exception as e:
            ultimo_erro = e
            if tentativa < max_retries:
                time.sleep(2 ** (tentativa + 1))
                continue
            raise RuntimeError(f"Falha na chamada ao Gemini apos {max_retries + 1} tentativas: {e}") from e

    raise RuntimeError(f"Falha ao consultar Gemini: {ultimo_erro}")


def julgar_produtos_em_lote(
    produtos: list[dict],
    vetos: dict[str, list[str]] | None = None,
    termos_alto_ctr: set[str] | None = None,
    custom_db_path: Path | None = None,
    custom_cache_path: Path | None = None,
    config_ia_override: dict | None = None,
    llm_callable: Callable[[list[dict]], str | tuple[str, int, int]] | None = None,
    usar_cache: bool = True,
    parar_ao_estourar: bool = False,
) -> dict:
    # avalia todos os produtos em lotes de ate 20 usando cache por produto+termo, gemini e fallback
    logger = get_logger("curator")
    regras_veto = vetos if vetos is not None else carregar_vetos()
    cache = carregar_cache_juiz(custom_cache_path) if usar_cache else {}

    cfg_ia = config_ia_override if config_ia_override is not None else carregar_config_ia()
    modelo_cfg = str(cfg_ia.get("modelo") or "").strip()
    modelo_env = os.environ.get("GEMINI_MODEL", "").strip()
    if modelo_env:
        modelo = modelo_env
    elif modelo_cfg and not modelo_cfg.startswith("${"):
        modelo = modelo_cfg
    else:
        modelo = "gemini-2.5-flash"

    max_tokens_saida = int(cfg_ia.get("max_tokens_saida", 600) or 600)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    avaliacoes_finais: dict[str, dict] = {}
    pendentes_avaliacao: list[dict] = []
    cache_hits = 0

    for prod in produtos:
        id_prod = str(prod.get("id_produto") or "")
        if not id_prod:
            continue
        chave = gerar_chave_cache_juiz(id_prod, prod.get("termo_origem"))
        if usar_cache and chave in cache:
            item_cache = dict(cache[chave])
            # revalida trava deterministica de seguranca caso vetos.yaml tenha mudado
            motivo_susp = detectar_motivo_suspeito(prod, vetos=regras_veto)
            if motivo_susp:
                item_cache["suspeito"] = True
                item_cache["afinidade"] = 0.0
                item_cache["motivo"] = motivo_susp
            avaliacoes_finais[id_prod] = item_cache
            cache_hits += 1
        else:
            pendentes_avaliacao.append(prod)

    chamadas_ia = 0
    tokens_in_total = 0
    tokens_out_total = 0
    custo_brl_total = 0.0
    usou_fallback = False
    orcamento_estourado = False
    alerta_80_disparado = False
    cache_modificado = False

    # divide os pendentes em lotes de ate 20 produtos por chamada
    for inicio in range(0, len(pendentes_avaliacao), MAX_PRODUTOS_POR_LOTE):
        lote = pendentes_avaliacao[inicio : inicio + MAX_PRODUTOS_POR_LOTE]
        ids_lote = {str(p.get("id_produto") or "") for p in lote}
        duplicatas_det = detectar_duplicatas_no_lote(lote)

        deve_chamar_ia = llm_callable is not None or bool(api_key)
        if not deve_chamar_ia:
            usou_fallback = True
            det_res = avaliar_lote_deterministico(lote, vetos=regras_veto, termos_alto_ctr=termos_alto_ctr)
            avaliacoes_finais.update(det_res)
            continue

        prompt = montar_prompt_juiz_lote(lote, termos_alto_ctr=termos_alto_ctr)

        # valida orcamento atual e custo projetado antes de disparar a chamada
        try:
            orcamento_ok = pode_gastar_ia(
                custom_db_path=custom_db_path,
                config_override=cfg_ia,
                modelo=modelo,
            )
            info_mes = consultar_gasto_mes(custom_db_path=custom_db_path, config_override=cfg_ia)
            custo_proj = projetar_custo_lote_brl(prompt, modelo=modelo, config_ia=cfg_ia)
            vai_estourar = (info_mes["total_brl"] + custo_proj) >= info_mes["teto_brl"]
        except BudgetConfigError as e:
            logger.warning(f"Configuracao de orcamento incompleta para '{modelo}': {e}")
            orcamento_ok = False
            vai_estourar = True
            custo_proj = 0.0
            info_mes = {"total_brl": 0.0, "teto_brl": 50.0}

        if not orcamento_ok or vai_estourar:
            orcamento_estourado = True
            msg_teto = (
                f"Orcamento de IA no limite (atual R$ {info_mes['total_brl']:.2f} + "
                f"projetado R$ {custo_proj:.4f} >= teto R$ {info_mes['teto_brl']:.2f})."
            )
            logger.warning(msg_teto)
            send_alert(
                msg_teto,
                nivel="warning",
                detalhes={
                    "atual_brl": round(info_mes["total_brl"], 4),
                    "projetado_brl": round(custo_proj, 4),
                },
            )
            if parar_ao_estourar:
                raise OrcamentoEstouradoError(msg_teto)
            usou_fallback = True
            det_res = avaliar_lote_deterministico(lote, vetos=regras_veto, termos_alto_ctr=termos_alto_ctr)
            avaliacoes_finais.update(det_res)
            continue

        try:
            if llm_callable is not None:
                retorno = llm_callable(lote)
                if isinstance(retorno, tuple):
                    raw_json, t_in, t_out = retorno
                else:
                    raw_json, t_in, t_out = str(retorno), max(1, len(prompt) // 4), 120
            else:
                tokens_saida_lote = max(max_tokens_saida, len(lote) * 100)
                raw_json, t_in, t_out = _chamar_gemini_juiz_lote(
                    prompt=prompt,
                    modelo=modelo,
                    max_tokens_saida=tokens_saida_lote,
                )

            # registra o gasto antes do parse para nao perder o custo de resposta invalida
            custo_chamada = registrar_gasto(
                modelo=modelo,
                tokens_in=t_in,
                tokens_out=t_out,
                custom_db_path=custom_db_path,
                config_override=cfg_ia,
            )
            chamadas_ia += 1
            tokens_in_total += t_in
            tokens_out_total += t_out
            custo_brl_total += custo_chamada

            parsed = RespostaJuizLote.model_validate_json(raw_json)
            mapa_ia = {item.id_produto.strip(): item for item in parsed.avaliacoes}

            info_pos = consultar_gasto_mes(custom_db_path=custom_db_path, config_override=cfg_ia)
            if info_pos["alerta_80"] and not alerta_80_disparado:
                alerta_80_disparado = True
                send_alert(
                    f"Consumo de IA atingiu {info_pos['pct_teto']}% do teto mensal (R$ {info_pos['total_brl']:.2f}).",
                    nivel="warning",
                    detalhes={"pct_teto": info_pos["pct_teto"], "total_brl": round(info_pos["total_brl"], 2)},
                )

            # consolida resposta da ia com travas deterministicas para cada item do lote
            fallback_lote = avaliar_lote_deterministico(
                lote, vetos=regras_veto, termos_alto_ctr=termos_alto_ctr
            )
            for prod in lote:
                id_prod = str(prod.get("id_produto") or "")
                item_ia = mapa_ia.get(id_prod)
                if item_ia is None:
                    avaliacoes_finais[id_prod] = fallback_lote[id_prod]
                    continue

                motivo_susp_det = detectar_motivo_suspeito(prod, vetos=regras_veto)
                suspeito_final = bool(item_ia.suspeito) or bool(motivo_susp_det)
                motivo_final = motivo_susp_det if motivo_susp_det else item_ia.motivo
                afinidade_final = 0.0 if suspeito_final else round(float(item_ia.afinidade), 2)

                dup_ia = item_ia.duplicado_de.strip() if item_ia.duplicado_de else None
                if dup_ia and (dup_ia == id_prod or dup_ia not in ids_lote):
                    dup_ia = None
                dup_final = dup_ia or duplicatas_det.get(id_prod)

                consolidado = AvaliacaoJuizItem(
                    id_produto=id_prod,
                    afinidade=afinidade_final,
                    suspeito=suspeito_final,
                    motivo=motivo_final[:160],
                    duplicado_de=dup_final,
                ).model_dump()

                avaliacoes_finais[id_prod] = consolidado
                if usar_cache:
                    chave = gerar_chave_cache_juiz(id_prod, prod.get("termo_origem"))
                    cache[chave] = consolidado
                    cache_modificado = True

        except (ValidationError, Exception) as e:  # noqa: BLE001
            logger.warning(f"Fallback deterministico acionado no Juiz LLM apos falha da IA: {e}")
            usou_fallback = True
            det_res = avaliar_lote_deterministico(lote, vetos=regras_veto, termos_alto_ctr=termos_alto_ctr)
            avaliacoes_finais.update(det_res)

    if usar_cache and cache_modificado:
        salvar_cache_juiz(cache, custom_cache_path=custom_cache_path)

    return {
        "avaliacoes": avaliacoes_finais,
        "chamadas_ia": chamadas_ia,
        "cache_hits": cache_hits,
        "tokens_in": tokens_in_total,
        "tokens_out": tokens_out_total,
        "custo_brl": round(custo_brl_total, 6),
        "usou_fallback": usou_fallback,
        "orcamento_estourado": orcamento_estourado,
        "alerta_80": alerta_80_disparado,
    }
