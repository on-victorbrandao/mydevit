# captacao em paginas publicas do mercado livre e gravacao no sqlite
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

from capture import selectors
from capture.filtros import carregar_config_filtros, carregar_vetos, filtrar_produtos
from capture.parser import parse_html_busca
from core.alerts import send_alert
from core.db import get_connection, init_db
from core.logger import get_logger

PASTA_BACK = Path(__file__).resolve().parent.parent
TZ_SP = ZoneInfo("America/Sao_Paulo")

USER_AGENT_PADRAO = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class BloqueioCaptacaoError(RuntimeError):
    # erro lancado quando o mercado livre retorna 403, 429 ou desafio de captcha
    pass


def carregar_config_captacao(custom_config_path: Path | None = None) -> dict:
    # le timeouts, pausas e retries da secao captacao do config.yaml
    caminho = custom_config_path or (PASTA_BACK / "config.yaml")
    with open(caminho, "r", encoding="utf-8") as f:
        dados = yaml.safe_load(f) or {}
    return dados.get(
        "captacao",
        {"pausa_min_s": 3, "pausa_max_s": 8, "timeout_s": 20, "retries": 2},
    )


def montar_url_busca(termo: str) -> str:
    # monta a url publica de listagem do mercado livre a partir do termo
    limpo = re.sub(r"\s+", "-", termo.strip().lower())
    slug = quote_plus(limpo, safe="-")
    return f"https://lista.mercadolivre.com.br/{slug}"


def _detectar_captcha_ou_bloqueio(status_code: int, url_final: str, html: str) -> str | None:
    # identifica bloqueio http 403/429 ou redirecionamento para verificacao/captcha
    if status_code in (403, 429):
        return f"HTTP {status_code}"
    url_lower = (url_final or "").lower()
    if any(p in url_lower for p in ("account-verification", "/gz/account-verification", "captcha", "/login")):
        return f"redirecionamento de bloqueio ({url_final})"
    html_lower = (html or "").lower()
    if "g-recaptcha" in html_lower or "ui-empty-state--captcha" in html_lower:
        return "desafio de captcha detectado no html"
    return None


def anonimizar_html_cards(html: str) -> str:
    # extrai apenas os blocos .poly-card sem scripts, estilos, cookies ou metadados de sessao
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(selectors.CARD)
    blocos_limpos = []
    for card in cards:
        for tag_indesejada in card.select("script, style, noscript"):
            tag_indesejada.decompose()
        for a_tag in card.select("a[href]"):
            href = str(a_tag.get("href") or "")
            if "#polycard" in href:
                a_tag["href"] = href.split("#polycard")[0]
        blocos_limpos.append(str(card))
    corpo = "\n".join(blocos_limpos)
    return f'<!DOCTYPE html>\n<html lang="pt-BR">\n<body>\n<div class="ui-search-results">\n{corpo}\n</div>\n</body>\n</html>\n'


def buscar_html_termo(
    termo: str,
    cfg_captacao: dict | None = None,
    aplicar_pausa: bool = False,
) -> str:
    # faz um unico get publico por termo sem sessao logada e para imediatamente em 403/429/captcha
    cfg = cfg_captacao or carregar_config_captacao()
    logger = get_logger("capture")

    if aplicar_pausa:
        pausa = random.uniform(float(cfg.get("pausa_min_s", 3)), float(cfg.get("pausa_max_s", 8)))
        time.sleep(pausa)

    url = montar_url_busca(termo)
    user_agent = os.environ.get("USER_AGENT", USER_AGENT_PADRAO).strip() or USER_AGENT_PADRAO
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    timeout_s = float(cfg.get("timeout_s", 20))
    max_retries = int(cfg.get("retries", 2))

    ultimo_erro = None
    for tentativa in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, cookies={}, timeout=timeout_s)
        except requests.RequestException as e:
            ultimo_erro = f"{type(e).__name__}"
            if tentativa < max_retries:
                time.sleep(2 ** (tentativa + 1))
                continue
            raise RuntimeError(f"Falha de rede ao buscar '{termo}': {ultimo_erro}") from e

        motivo_bloqueio = _detectar_captcha_ou_bloqueio(resp.status_code, resp.url, resp.text)
        if motivo_bloqueio:
            msg = f"Captacao interrompida para '{termo}': {motivo_bloqueio}. Parando sem contornar."
            logger.error(msg)
            send_alert(msg, nivel="critico", detalhes={"termo": termo, "motivo": motivo_bloqueio})
            raise BloqueioCaptacaoError(msg)

        if resp.status_code >= 500 and tentativa < max_retries:
            time.sleep(2 ** (tentativa + 1))
            continue

        if resp.status_code != 200:
            raise RuntimeError(f"Status HTTP inesperado ({resp.status_code}) na busca de '{termo}'")

        return resp.text

    raise RuntimeError(f"Falha ao buscar '{termo}': {ultimo_erro}")


def _buscar_ids_postados_recentes(conn, dias: int = 30) -> set[str]:
    # consulta ids de produtos ja publicados nos ultimos N dias para evitar repeticao
    limite = (datetime.now(TZ_SP) - timedelta(days=dias)).isoformat(timespec="seconds")
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT id_produto FROM posts WHERE data_hora_postagem >= ?;", (limite,))
    return {row["id_produto"] for row in cursor.fetchall() if row["id_produto"]}


def gravar_captacao_sqlite(
    termo: str,
    cards_extraidos: list[dict],
    aprovados: list[dict],
    inicio_iso: str,
    custom_db_path: Path | None = None,
    status_exec: str = "sucesso",
    erro_exec: str | None = None,
) -> dict:
    # grava historico de preco, insere produtos aprovados com INSERT OR IGNORE e registra execucao
    init_db(custom_db_path)
    agora_dt = datetime.now(TZ_SP)
    agora_iso = agora_dt.isoformat(timespec="seconds")
    hoje_str = agora_dt.strftime("%Y-%m-%d")

    inseridos = 0
    ignorados = 0
    historico_gravado = 0

    conn = get_connection(custom_db_path)
    try:
        with conn:
            for card in cards_extraidos:
                id_prod = card.get("id_produto")
                val_atual = card.get("valor_atual")
                if id_prod and val_atual is not None:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO historico_preco (id_produto, data, valor_atual, valor_original)
                        VALUES (?, ?, ?, ?);
                        """,
                        (id_prod, hoje_str, val_atual, card.get("valor_original")),
                    )
                    historico_gravado += 1

            for item in aprovados:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO produtos (
                        id_produto, marketplace, nome_produto, categoria, imagem_url,
                        url_original, valor_original, valor_atual, desconto_pct,
                        nota, vendas, termo_origem, status, data_captura, data_atualizacao
                    ) VALUES (?, 'Mercado Livre', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'novo', ?, ?);
                    """,
                    (
                        item["id_produto"],
                        item["nome_produto"],
                        item.get("categoria"),
                        item.get("imagem_url"),
                        item.get("url_limpa") or item["url_original"],
                        item.get("valor_original"),
                        item.get("valor_atual"),
                        item.get("desconto_pct"),
                        item.get("nota"),
                        item.get("vendas"),
                        termo,
                        hoje_str,
                        agora_iso,
                    ),
                )
                if cursor.rowcount == 1:
                    inseridos += 1
                else:
                    ignorados += 1

            conn.execute(
                """
                INSERT INTO execucoes (modulo, inicio, fim, status, itens, erro)
                VALUES ('capture', ?, ?, ?, ?, ?);
                """,
                (inicio_iso, agora_iso, status_exec, inseridos, erro_exec),
            )
    finally:
        conn.close()

    return {
        "inseridos": inseridos,
        "ignorados": ignorados,
        "historico_gravado": historico_gravado,
    }


def executar_captacao_termo(
    termo: str,
    custom_db_path: Path | None = None,
    salvar_fixture_path: Path | None = None,
    html_override: str | None = None,
) -> dict:
    # executa o fluxo completo de captacao de um termo ate a gravacao no sqlite
    inicio_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
    logger = get_logger("capture")

    try:
        html = html_override if html_override is not None else buscar_html_termo(termo)
    except Exception as e:
        gravar_captacao_sqlite(
            termo=termo,
            cards_extraidos=[],
            aprovados=[],
            inicio_iso=inicio_iso,
            custom_db_path=custom_db_path,
            status_exec="bloqueado" if isinstance(e, BloqueioCaptacaoError) else "erro",
            erro_exec=str(e),
        )
        raise

    if salvar_fixture_path:
        salvar_fixture_path.parent.mkdir(parents=True, exist_ok=True)
        html_anonimo = anonimizar_html_cards(html)
        with open(salvar_fixture_path, "w", encoding="utf-8") as f:
            f.write(html_anonimo)

    cards_extraidos = parse_html_busca(html)
    if not cards_extraidos:
        msg_zero = f"0 cards encontrados para '{termo}' (seletores quebraram ou pagina vazia)"
        logger.warning(msg_zero)
        send_alert(msg_zero, nivel="warning", detalhes={"termo": termo})
        gravar_captacao_sqlite(
            termo=termo,
            cards_extraidos=[],
            aprovados=[],
            inicio_iso=inicio_iso,
            custom_db_path=custom_db_path,
            status_exec="erro",
            erro_exec="0 cards encontrados",
        )
        return {
            "termo": termo,
            "total_cards": 0,
            "total_aprovados": 0,
            "inseridos": 0,
            "ignorados": 0,
            "aprovados": [],
        }

    cfg_filtros = carregar_config_filtros()
    vetos = carregar_vetos()

    init_db(custom_db_path)
    conn = get_connection(custom_db_path)
    try:
        ids_recentes = _buscar_ids_postados_recentes(conn, dias=int(cfg_filtros.get("repetido_dias", 30)))
    finally:
        conn.close()

    aprovados = filtrar_produtos(
        cards_extraidos,
        cfg_filtros=cfg_filtros,
        vetos=vetos,
        ids_recentes=ids_recentes,
    )

    res_db = gravar_captacao_sqlite(
        termo=termo,
        cards_extraidos=cards_extraidos,
        aprovados=aprovados,
        inicio_iso=inicio_iso,
        custom_db_path=custom_db_path,
        status_exec="sucesso",
    )

    logger.info(
        f"Captacao concluida para '{termo}': cards={len(cards_extraidos)}, aprovados={len(aprovados)}, inseridos={res_db['inseridos']}"
    )

    return {
        "termo": termo,
        "total_cards": len(cards_extraidos),
        "total_aprovados": len(aprovados),
        "inseridos": res_db["inseridos"],
        "ignorados": res_db["ignorados"],
        "aprovados": aprovados,
    }
