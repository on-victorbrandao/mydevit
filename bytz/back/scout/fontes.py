# coleta de fontes de tendencia (calendario sazonal, web publica, cache e firecrawl opcional)
import calendar
import json
import os
import warnings
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from core.db import get_connection, init_db
from core.logger import get_logger

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

PASTA_BACK = Path(__file__).resolve().parent.parent
TZ_SP = ZoneInfo("America/Sao_Paulo")
SAZONALIDADE_PATH = PASTA_BACK / "config" / "sazonalidade.yaml"
CONFIG_PATH = PASTA_BACK / "config.yaml"
CACHE_PADRAO_PATH = PASTA_BACK / "data" / "scout_cache.json"

MODELO_FIRECRAWL = "modelo_firecrawl"
MAX_CREDITOS_FIRECRAWL_PADRAO = 20
USER_AGENT_PADRAO = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

NICHOS_OFICIAIS = (
    "eletronicos",
    "setup",
    "smart home",
    "acessorios moveis",
    "home-office",
    "fitness",
)

# temas base estaveis por nicho para garantir cobertura mesmo fora de datas comemorativas
TEMAS_BASE_POR_NICHO: dict[str, list[str]] = {
    "eletronicos": [
        "Fone de ouvido bluetooth com cancelamento de ruido",
        "Caixa de som bluetooth portatil a prova dagua",
        "Smartwatch e pulseira inteligente",
    ],
    "setup": [
        "Teclado mecanico e mouse gamer sem fio",
        "Monitor gamer e suporte articulado de mesa",
        "Headset gamer e microfone condensador usb",
    ],
    "smart home": [
        "Lampada inteligente wifi e tomada smart",
        "Fechadura digital e camera de seguranca wifi",
        "Robo aspirador inteligente e caixa de som alexa",
    ],
    "acessorios moveis": [
        "Carregador portatil power bank turbo",
        "Carregador rapido gan usb-c e cabo reforçado",
        "Suporte veicular magnetico e rastreador bluetooth",
    ],
    "home-office": [
        "Suporte ergonomico para notebook e hub usb-c",
        "Webcam full hd e luminaria de monitor barra led",
        "Cadeira ergonomica e apoio de pes escritorio",
    ],
    "fitness": [
        "Garrafa termica inox e coqueteleira academia",
        "Kit elásticos extensores e halteres treino em casa",
        "Balança de bioimpedancia digital bluetooth",
    ],
}

FONTES_WEB_PADRAO: list[dict[str, str]] = [
    {
        "nome": "tecnoblog",
        "url": "https://tecnoblog.net/feed/",
        "seletor": "item > title",
    },
    {
        "nome": "mundoconectado",
        "url": "https://www.mundoconectado.com.br/feed/",
        "seletor": "item > title",
    },
    {
        "nome": "google_trends",
        "url": "https://trends.google.com.br/trending/rss?geo=BR",
        "seletor": "item > title",
    },
]


def _validar_url_nao_ml(url: str) -> None:
    # impede qualquer requisicao para dominios do mercado livre no estagio de descoberta
    host = urlsplit(str(url or "")).netloc.lower()
    if "mercadolivre." in host or "mercadolibre." in host:
        raise ValueError(f"Busca no Mercado Livre proibida no Scout: {host}")


def carregar_sazonalidade(custom_path: Path | None = None) -> dict:
    # carrega o arquivo config/sazonalidade.yaml com as datas e regras sazonais
    caminho = custom_path or SAZONALIDADE_PATH
    if not caminho.exists():
        return {"datas": []}
    with open(caminho, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"datas": []}


def calcular_data_regra(ano: int, regra: str) -> date | None:
    # resolve regras moveis de calendario como segundo domingo de maio/agosto e ultima sexta de novembro
    regra_norm = (regra or "").strip().lower()
    cal = calendar.Calendar(firstweekday=calendar.MONDAY)

    if regra_norm == "segundo domingo de maio":
        domingos = [d for d in cal.itermonthdates(ano, 5) if d.month == 5 and d.weekday() == calendar.SUNDAY]
        return domingos[1] if len(domingos) >= 2 else None

    if regra_norm == "segundo domingo de agosto":
        domingos = [d for d in cal.itermonthdates(ano, 8) if d.month == 8 and d.weekday() == calendar.SUNDAY]
        return domingos[1] if len(domingos) >= 2 else None

    if regra_norm == "ultima sexta de novembro":
        sextas = [d for d in cal.itermonthdates(ano, 11) if d.month == 11 and d.weekday() == calendar.FRIDAY]
        return sextas[-1] if sextas else None

    return None


def _esta_em_janela_mmdd(data_ref: date, inicio_mmdd: str, fim_mmdd: str) -> bool:
    # verifica se a data atual cai dentro de um intervalo MM-DD tratando virada de ano
    atual = data_ref.strftime("%m-%d")
    if inicio_mmdd <= fim_mmdd:
        return inicio_mmdd <= atual <= fim_mmdd
    return atual >= inicio_mmdd or atual <= fim_mmdd


def _evento_sazonal_ativo(data_ref: date, item: dict) -> bool:
    # checa se uma entrada do sazonalidade.yaml esta ativa na data de referencia
    if "inicio" in item and "fim" in item:
        return _esta_em_janela_mmdd(data_ref, str(item["inicio"]), str(item["fim"]))

    antecedencia = int(item.get("antecedencia_dias", 15))
    data_evento: date | None = None

    if "data" in item:
        mes_str, dia_str = str(item["data"]).split("-")
        data_evento = date(data_ref.year, int(mes_str), int(dia_str))
    elif "regra" in item:
        data_evento = calcular_data_regra(data_ref.year, str(item["regra"]))

    if data_evento is None:
        return False

    delta = (data_evento - data_ref).days
    return 0 <= delta <= antecedencia


def avaliar_sazonalidade(
    data_ref: date | None = None,
    dados_sazonalidade: dict | None = None,
) -> list[dict]:
    # gera candidatos do calendario combinando eventos sazonais ativos e a base estavel dos nichos
    ref = data_ref or datetime.now(TZ_SP).date()
    dados = dados_sazonalidade if dados_sazonalidade is not None else carregar_sazonalidade()
    candidatos: list[dict] = []

    for item in dados.get("datas", []):
        if not isinstance(item, dict) or not _evento_sazonal_ativo(ref, item):
            continue
        nome_evento = str(item.get("nome") or "Sazonal").strip()
        nichos_raw = item.get("nichos") or []
        if "todos" in nichos_raw:
            nichos_evento = list(NICHOS_OFICIAIS)
        else:
            nichos_evento = [str(n).strip().lower() for n in nichos_raw if str(n).strip()]

        for nicho in nichos_evento:
            temas_nicho = TEMAS_BASE_POR_NICHO.get(nicho, [f"Ofertas de {nicho}"])
            for idx, tema_base in enumerate(temas_nicho[:2]):
                candidatos.append(
                    {
                        "tema": f"{nome_evento}: {tema_base}",
                        "fonte": f"sazonalidade:{nome_evento.lower().replace(' ', '_')}",
                        "nicho": nicho,
                        "sazonal": True,
                        "peso_base": round(9.2 - (idx * 0.3), 1),
                    }
                )

    # adiciona a base estavel de todos os nichos oficiais para nunca faltar insumo
    for nicho in NICHOS_OFICIAIS:
        for idx, tema_base in enumerate(TEMAS_BASE_POR_NICHO.get(nicho, [])):
            candidatos.append(
                {
                    "tema": tema_base,
                    "fonte": "sazonalidade:calendario_base",
                    "nicho": nicho,
                    "sazonal": False,
                    "peso_base": round(7.2 - (idx * 0.3), 1),
                }
            )

    return candidatos


def extrair_titulos_html(
    conteudo: str,
    seletor: str = "item > title, h2, h3",
    limite: int = 20,
) -> list[str]:
    # extrai titulos limpos de um html ou rss usando beautifulsoup sem duplicar entradas
    if not conteudo or not conteudo.strip():
        return []
    soup = BeautifulSoup(conteudo, "html.parser")
    titulos: list[str] = []
    vistos: set[str] = set()

    for elem in soup.select(seletor):
        texto = " ".join(elem.get_text(" ", strip=True).split())
        if len(texto) < 3:
            continue
        chave = texto.lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        titulos.append(texto)
        if len(titulos) >= limite:
            break

    return titulos


def buscar_fonte_publica(
    nome: str,
    url: str,
    seletor: str = "item > title",
    timeout_s: int = 10,
    http_get: Callable | None = None,
) -> list[dict]:
    # le uma pagina ou feed publico fora do mercado livre usando requests e beautifulsoup4
    _validar_url_nao_ml(url)
    get_fn = http_get or requests.get
    headers = {"User-Agent": os.environ.get("USER_AGENT", USER_AGENT_PADRAO)}
    resp = get_fn(url, headers=headers, timeout=timeout_s)
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"Fonte '{nome}' respondeu HTTP {getattr(resp, 'status_code', None)}")

    titulos = extrair_titulos_html(getattr(resp, "text", ""), seletor=seletor, limite=15)
    if not titulos:
        raise RuntimeError(f"Fonte '{nome}' retornou 0 titulos para o seletor '{seletor}'")

    return [
        {
            "tema": titulo,
            "fonte": f"web:{nome}",
            "nicho": None,
            "sazonal": False,
            "peso_base": 8.0,
        }
        for titulo in titulos
    ]


def carregar_teto_firecrawl_dia(custom_config_path: Path | None = None) -> int:
    # obtem o limite diario de creditos do firecrawl a partir do config.yaml (padrao 20)
    caminho = custom_config_path or CONFIG_PATH
    if not caminho.exists():
        return MAX_CREDITOS_FIRECRAWL_PADRAO
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return MAX_CREDITOS_FIRECRAWL_PADRAO

    secao = dados.get("firecrawl")
    if isinstance(secao, dict):
        limite = secao.get("max_creditos_dia")
        if isinstance(limite, int) and limite > 0:
            return min(limite, MAX_CREDITOS_FIRECRAWL_PADRAO)
    return MAX_CREDITOS_FIRECRAWL_PADRAO


def consultar_creditos_firecrawl_hoje(
    data_iso: str | None = None,
    custom_db_path: Path | None = None,
) -> int:
    # soma os creditos consumidos pelo firecrawl no dia atual na tabela gasto_ia
    init_db(custom_db_path)
    hoje = data_iso or datetime.now(TZ_SP).strftime("%Y-%m-%d")
    conn = get_connection(custom_db_path)
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(tokens_in), 0) AS creditos
            FROM gasto_ia
            WHERE data = ? AND modelo IN (?, 'firecrawl');
            """,
            (hoje, MODELO_FIRECRAWL),
        ).fetchone()
        return int(row["creditos"]) if row else 0
    finally:
        conn.close()


def registrar_consumo_firecrawl(
    creditos: int = 1,
    data_iso: str | None = None,
    custom_db_path: Path | None = None,
) -> None:
    # grava o uso de creditos da api do firecrawl em gasto_ia usando o campo modelo_firecrawl
    init_db(custom_db_path)
    hoje = data_iso or datetime.now(TZ_SP).strftime("%Y-%m-%d")
    conn = get_connection(custom_db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO gasto_ia (data, modelo, tokens_in, tokens_out, custo_estimado_brl)
                VALUES (?, ?, ?, 0, 0.0);
                """,
                (hoje, MODELO_FIRECRAWL, int(creditos)),
            )
    finally:
        conn.close()


def coletar_via_firecrawl(
    nome: str,
    url: str,
    seletor: str = "h1, h2, h3, article a",
    custom_db_path: Path | None = None,
    custom_config_path: Path | None = None,
    timeout_s: int = 20,
    http_post: Callable | None = None,
) -> list[dict]:
    # usa a api do firecrawl apenas como auxiliar se houver chave no .env e saldo diario disponivel
    _validar_url_nao_ml(url)
    logger = get_logger("scout")
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        return []

    teto_dia = carregar_teto_firecrawl_dia(custom_config_path)
    usados_hoje = consultar_creditos_firecrawl_hoje(custom_db_path=custom_db_path)
    if usados_hoje + 1 > teto_dia:
        logger.warning(f"Teto diario do Firecrawl atingido ({usados_hoje}/{teto_dia} creditos).")
        return []

    post_fn = http_post or requests.post
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"url": url, "formats": ["html"]}
    resp = post_fn("https://api.firecrawl.dev/v1/scrape", headers=headers, json=payload, timeout=timeout_s)
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"Firecrawl HTTP {getattr(resp, 'status_code', None)} para '{nome}'")

    registrar_consumo_firecrawl(creditos=1, custom_db_path=custom_db_path)
    corpo = resp.json() if hasattr(resp, "json") else {}
    html = str((corpo.get("data") or {}).get("html") or "")
    titulos = extrair_titulos_html(html, seletor=seletor, limite=15)
    return [
        {
            "tema": titulo,
            "fonte": f"firecrawl:{nome}",
            "nicho": None,
            "sazonal": False,
            "peso_base": 7.8,
        }
        for titulo in titulos
    ]


def salvar_cache_web(candidatos: list[dict], custom_cache_path: Path | None = None) -> None:
    # persiste em disco os candidatos web coletados no dia para servir de fallback amanha
    caminho = custom_cache_path or CACHE_PADRAO_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "atualizado_em": datetime.now(TZ_SP).isoformat(timespec="seconds"),
        "candidatos": candidatos,
    }
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporario, caminho)


def carregar_cache_web(custom_cache_path: Path | None = None) -> list[dict]:
    # le os candidatos web do cache local quando as fontes online falham
    caminho = custom_cache_path or CACHE_PADRAO_PATH
    if not caminho.exists():
        return []
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        itens = dados.get("candidatos") or []
        resultado = []
        for item in itens:
            if isinstance(item, dict) and item.get("tema"):
                copia = dict(item)
                fonte_orig = str(copia.get("fonte") or "web")
                if not fonte_orig.startswith("cache:"):
                    copia["fonte"] = f"cache:{fonte_orig}"
                resultado.append(copia)
        return resultado
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return []


def carregar_historico_sqlite(custom_db_path: Path | None = None) -> list[dict]:
    # busca termos com melhor desempenho e temas recentes do banco para retroalimentar o scout
    init_db(custom_db_path)
    candidatos: list[dict] = []
    conn = get_connection(custom_db_path)
    try:
        rows_buscas = conn.execute("""
            SELECT termo, nicho, aprovados_total
            FROM buscas
            WHERE ativo = 1 AND aprovados_total > 0
            ORDER BY aprovados_total DESC, prioridade DESC
            LIMIT 10;
            """).fetchall()
        for r in rows_buscas:
            candidatos.append(
                {
                    "tema": str(r["termo"]),
                    "fonte": "historico:buscas",
                    "nicho": r["nicho"],
                    "sazonal": False,
                    "peso_base": 8.4,
                }
            )

        rows_temas = conn.execute("""
            SELECT tema, fonte, nota_tendencia
            FROM temas
            ORDER BY id DESC
            LIMIT 15;
            """).fetchall()
        for r in rows_temas:
            candidatos.append(
                {
                    "tema": str(r["tema"]),
                    "fonte": "cache:temas_anteriores",
                    "nicho": None,
                    "sazonal": False,
                    "peso_base": float(r["nota_tendencia"] or 7.0),
                }
            )
    finally:
        conn.close()
    return candidatos


def coletar_candidatos_scout(
    data_ref: date | None = None,
    fontes_web: list[dict[str, str]] | None = None,
    custom_sazonalidade_path: Path | None = None,
    custom_cache_path: Path | None = None,
    custom_db_path: Path | None = None,
    custom_config_path: Path | None = None,
    http_get: Callable | None = None,
    http_post: Callable | None = None,
) -> dict:
    # combina calendario sazonal, fontes web publicas e fallback automatico de cache e historico
    logger = get_logger("scout")
    dados_saz = carregar_sazonalidade(custom_sazonalidade_path)
    sazonais = avaliar_sazonalidade(data_ref=data_ref, dados_sazonalidade=dados_saz)

    lista_fontes = fontes_web if fontes_web is not None else FONTES_WEB_PADRAO
    coletados_web: list[dict] = []
    erros_web: list[str] = []

    for fonte in lista_fontes:
        nome = str(fonte.get("nome") or "web")
        url = str(fonte.get("url") or "")
        seletor = str(fonte.get("seletor") or "item > title")
        usar_firecrawl = bool(fonte.get("usar_firecrawl", False))

        try:
            if usar_firecrawl:
                itens = coletar_via_firecrawl(
                    nome=nome,
                    url=url,
                    seletor=seletor,
                    custom_db_path=custom_db_path,
                    custom_config_path=custom_config_path,
                    http_post=http_post,
                )
            else:
                itens = buscar_fonte_publica(
                    nome=nome,
                    url=url,
                    seletor=seletor,
                    http_get=http_get,
                )
            coletados_web.extend(itens)
        except ValueError:
            # erro de seguranca como tentativa de acessar dominio do mercado livre deve propagar
            raise
        except Exception as e:  # noqa: BLE001
            erros_web.append(f"{nome}: {e}")
            logger.warning(f"Falha ao ler fonte web '{nome}', acionando fallback se necessario: {e}")

    usou_cache = False
    if coletados_web:
        salvar_cache_web(coletados_web, custom_cache_path=custom_cache_path)
    else:
        coletados_web = carregar_cache_web(custom_cache_path=custom_cache_path)
        usou_cache = True

    historico = carregar_historico_sqlite(custom_db_path=custom_db_path)
    todos = coletados_web + sazonais + historico

    return {
        "candidatos": todos,
        "total_web": len(coletados_web),
        "total_sazonal": len(sazonais),
        "total_historico": len(historico),
        "usou_cache": usou_cache,
        "erros_web": erros_web,
    }
