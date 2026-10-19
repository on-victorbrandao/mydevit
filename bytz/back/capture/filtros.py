# filtros eliminatorios da captacao baseados em config.yaml e vetos.yaml
import unicodedata
from pathlib import Path

import yaml

PASTA_BACK = Path(__file__).resolve().parent.parent


def _sem_acento_minusculo(texto: str) -> str:
    # remove acentos e converte para minusculo para comparacao de vetos
    nfkd = unicodedata.normalize("NFKD", str(texto or ""))
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.lower().strip()


def carregar_config_filtros(custom_config_path: Path | None = None) -> dict:
    # carrega os parametros de filtros do config.yaml
    caminho = custom_config_path or (PASTA_BACK / "config.yaml")
    with open(caminho, "r", encoding="utf-8") as f:
        dados = yaml.safe_load(f) or {}
    return dados.get(
        "filtros",
        {"nota_min": 4.5, "vendas_min": 500, "desconto_min_pct": 15, "repetido_dias": 30},
    )


def carregar_vetos(custom_vetos_path: Path | None = None) -> dict[str, list[str]]:
    # carrega termos e categorias vetados do config/vetos.yaml normalizados sem acento
    caminho = custom_vetos_path or (PASTA_BACK / "config" / "vetos.yaml")
    if not caminho.exists():
        return {"termos": [], "categorias": []}
    with open(caminho, "r", encoding="utf-8") as f:
        dados = yaml.safe_load(f) or {}
    return {
        "termos": [_sem_acento_minusculo(t) for t in dados.get("termos", []) if t],
        "categorias": [_sem_acento_minusculo(c) for c in dados.get("categorias", []) if c],
    }


def passa_filtros(
    item: dict,
    cfg_filtros: dict | None = None,
    vetos: dict[str, list[str]] | None = None,
    ids_recentes: set[str] | None = None,
) -> tuple[bool, str]:
    # avalia se um item capturado atende a todos os filtros eliminatorios
    cfg = cfg_filtros if cfg_filtros is not None else carregar_config_filtros()
    regras_veto = vetos if vetos is not None else carregar_vetos()
    recentes = ids_recentes or set()

    url_orig = str(item.get("url_original") or "").lower()
    if "/mclics/" in url_orig or "click1.mercadolivre" in url_orig:
        return False, "anuncio patrocinado mclics"

    id_prod = item.get("id_produto")
    if not id_prod:
        return False, "sem id_produto"

    if id_prod in recentes:
        return False, "postado nos ultimos 30 dias"

    nome = str(item.get("nome_produto") or "").strip()
    if not nome:
        return False, "sem nome_produto"

    nome_norm = _sem_acento_minusculo(nome)
    for termo_vetado in regras_veto.get("termos", []):
        if termo_vetado and termo_vetado in nome_norm:
            return False, f"termo vetado: {termo_vetado}"

    cat_norm = _sem_acento_minusculo(str(item.get("categoria") or ""))
    for cat_vetada in regras_veto.get("categorias", []):
        if cat_vetada and cat_vetada == cat_norm:
            return False, f"categoria vetada: {cat_vetada}"

    valor_original = item.get("valor_original")
    valor_atual = item.get("valor_atual")
    if valor_original is None or valor_atual is None or valor_original <= valor_atual:
        return False, "sem valor_original ou sem desconto"

    desconto_pct = float(item.get("desconto_pct") or 0.0)
    if desconto_pct < float(cfg.get("desconto_min_pct", 15)):
        return False, "desconto abaixo do minimo"

    nota = item.get("nota")
    if nota is None or float(nota) < float(cfg.get("nota_min", 4.5)):
        return False, "nota abaixo do minimo"

    vendas = item.get("vendas")
    if vendas is None or int(vendas) < int(cfg.get("vendas_min", 500)):
        return False, "vendas abaixo do minimo"

    return True, "aprovado"


def filtrar_produtos(
    itens: list[dict],
    cfg_filtros: dict | None = None,
    vetos: dict[str, list[str]] | None = None,
    ids_recentes: set[str] | None = None,
) -> list[dict]:
    # retorna apenas itens aprovados nos filtros e sem duplicatas de id_produto no lote
    cfg = cfg_filtros if cfg_filtros is not None else carregar_config_filtros()
    regras_veto = vetos if vetos is not None else carregar_vetos()
    recentes = set(ids_recentes or set())

    aprovados = []
    vistos: set[str] = set()
    for item in itens:
        id_prod = item.get("id_produto")
        if id_prod and id_prod in vistos:
            continue
        ok, _motivo = passa_filtros(item, cfg_filtros=cfg, vetos=regras_veto, ids_recentes=recentes)
        if ok and id_prod:
            vistos.add(id_prod)
            aprovados.append(item)
    return aprovados
