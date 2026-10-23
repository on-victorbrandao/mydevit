# captacao no mercado livre pelo google chrome real com sessao de conta secundaria e gravacao no sqlite
import json
import os
import random
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urlsplit
from zoneinfo import ZoneInfo

import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from capture import selectors
from capture.filtros import (
    carregar_config_filtros,
    carregar_max_buscas_dia,
    carregar_vetos,
    filtrar_produtos,
    termo_contem_veto,
)
from capture.parser import parse_html_busca
from core.alerts import send_alert
from core.db import get_connection, init_db
from core.logger import get_logger

PASTA_BACK = Path(__file__).resolve().parent.parent
TZ_SP = ZoneInfo("America/Sao_Paulo")

DOMINIO_COOKIE = ".mercadolivre.com.br"
URLS_COOKIE = ("https://www.mercadolivre.com.br", "https://lista.mercadolivre.com.br")


class BloqueioCaptacaoError(RuntimeError):
    # erro lancado quando o mercado livre retorna 403, 429 ou desafio de captcha
    pass


def parse_cookie_string(cookie: str) -> dict[str, str]:
    # transforma string de cabecalho Cookie em dicionario mantendo o ultimo valor de chaves repetidas
    jar: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        key, _sep, val = part.strip().partition("=")
        if key:
            jar[key.strip()] = val
    return jar


def merge_cookie(old_cookie: str, set_cookie_headers: list[str]) -> tuple[str, str]:
    # funde o cookie vigente com os Set-Cookie da resposta e devolve cookie atualizado e csrf
    jar = parse_cookie_string(old_cookie)
    for header in set_cookie_headers or []:
        first = header.split(";")[0]
        key, _sep, val = first.strip().partition("=")
        if key:
            jar[key.strip()] = val
    cookie_atualizado = "; ".join(f"{k}={v}" for k, v in jar.items())
    return cookie_atualizado, jar.get("_csrf", "")


def obter_caminho_cookie_busca(custom_cookie_path: Path | None = None) -> Path:
    # resolve o caminho do arquivo json de cookie da conta secundaria de busca
    if custom_cookie_path is not None:
        return Path(custom_cookie_path)
    env_path = os.environ.get("ML_SEARCH_COOKIE_FILE", "").strip()
    if env_path:
        return Path(env_path)
    return PASTA_BACK / "ml_cookies_busca.json"


def carregar_cookie_busca(custom_cookie_path: Path | None = None) -> str:
    # le e valida o campo cookie do arquivo ml_cookies_busca.json sem logar valores sensiveis
    caminho = obter_caminho_cookie_busca(custom_cookie_path)
    if not caminho.exists():
        raise RuntimeError(
            f"Arquivo de cookie de busca nao encontrado ({caminho.name}). "
            "Salve o cookie da conta secundaria com: python back/scripts/salvar_cookie.py --busca"
        )
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"JSON invalido em {caminho.name}") from e

    cookie_str = str(dados.get("cookie") or "").strip()
    if not cookie_str:
        raise RuntimeError(f"Campo 'cookie' vazio em {caminho.name}")
    return cookie_str


def salvar_cookie_busca_atomico(cookie_novo: str, custom_cookie_path: Path | None = None) -> None:
    # grava o cookie atualizado e o timestamp no json de busca usando arquivo temporario e replace
    caminho = obter_caminho_cookie_busca(custom_cookie_path)
    agora_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
    dados = {
        "marketplace": "Mercado Livre",
        "origem": "busca",
        "atualizacao": agora_iso,
        "cookie": cookie_novo,
    }
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporario, caminho)


def cookies_para_navegador(cookie_str: str) -> list[dict]:
    # converte a string k=v; ... no formato de cookies que o contexto do chrome aceita
    cookies = []
    for nome, valor in parse_cookie_string(cookie_str).items():
        if nome.startswith("__Host-"):
            # cookie __Host- nao aceita dominio, entao vai amarrado na url
            cookies.append({"name": nome, "value": valor, "url": URLS_COOKIE[1] + "/"})
        else:
            cookies.append(
                {"name": nome, "value": valor, "domain": DOMINIO_COOKIE, "path": "/", "secure": True}
            )
    return cookies


def cookies_navegador_para_string(cookie_antigo: str, cookies_navegador: list[dict]) -> str:
    # funde os cookies que o chrome terminou a sessao com o cookie antigo usando merge_cookie
    pares = [f"{c['name']}={c['value']}" for c in cookies_navegador if c.get("name")]
    cookie_novo, _csrf = merge_cookie(cookie_antigo, pares)
    return cookie_novo


def carregar_config_captacao(custom_config_path: Path | None = None) -> dict:
    # le timeouts, pausas e retries da secao captacao do config.yaml
    caminho = custom_config_path or (PASTA_BACK / "config.yaml")
    with open(caminho, "r", encoding="utf-8") as f:
        dados = yaml.safe_load(f) or {}
    return dados.get(
        "captacao",
        {"pausa_min_s": 3, "pausa_max_s": 8, "timeout_s": 20, "retries": 2, "headless": False},
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
        # guarda so host e caminho para nao levar ids de rastreio para log e banco
        partes = urlsplit(url_final or "")
        return f"redirecionamento de bloqueio ({partes.netloc}{partes.path})"
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
    max_retries_override: int | None = None,
    custom_cookie_path: Path | None = None,
) -> str:
    # abre a busca no chrome real com o cookie da conta secundaria, salva a sessao renovada e para em bloqueio
    cfg = cfg_captacao or carregar_config_captacao()
    logger = get_logger("capture")

    if aplicar_pausa:
        pausa = random.uniform(float(cfg.get("pausa_min_s", 3)), float(cfg.get("pausa_max_s", 8)))
        time.sleep(pausa)

    cookie_atual = carregar_cookie_busca(custom_cookie_path)
    url = montar_url_busca(termo)
    max_retries = (
        int(max_retries_override) if max_retries_override is not None else int(cfg.get("retries", 2))
    )

    ultimo_erro = None
    for tentativa in range(max_retries + 1):
        try:
            status, url_final, html, cookies_sessao = _abrir_busca_no_chrome(url, cookie_atual, cfg)
        except PlaywrightError as e:
            # falha de rede ou do navegador, nunca bloqueio, entao pode tentar de novo
            ultimo_erro = type(e).__name__
            if tentativa < max_retries:
                time.sleep(2 ** (tentativa + 1))
                continue
            raise RuntimeError(f"Falha no navegador ao buscar '{termo}': {ultimo_erro}") from e

        motivo_bloqueio = _detectar_captcha_ou_bloqueio(status or 0, url_final, html)
        if motivo_bloqueio:
            msg = f"Captacao interrompida para '{termo}': {motivo_bloqueio}. Parando sem contornar."
            logger.error(msg)
            send_alert(msg, nivel="critico", detalhes={"termo": termo, "motivo": motivo_bloqueio})
            raise BloqueioCaptacaoError(msg)

        if status is not None and status >= 500 and tentativa < max_retries:
            time.sleep(2 ** (tentativa + 1))
            continue

        if status is not None and status != 200:
            raise RuntimeError(f"Status HTTP inesperado ({status}) na busca de '{termo}'")

        # so grava a sessao renovada quando a busca deu certo
        if cookies_sessao:
            cookie_novo = cookies_navegador_para_string(cookie_atual, cookies_sessao)
            salvar_cookie_busca_atomico(cookie_novo, custom_cookie_path)

        return html

    raise RuntimeError(f"Falha ao buscar '{termo}': {ultimo_erro}")


def _abrir_busca_no_chrome(url: str, cookie_atual: str, cfg: dict) -> tuple[int | None, str, str, list[dict]]:
    # abre o google chrome instalado, injeta os cookies, espera os cards e devolve status, url, html e cookies
    timeout_ms = int(float(cfg.get("timeout_s", 20)) * 1000)
    headless = bool(cfg.get("headless", False))
    opcoes_contexto = {"locale": "pt-BR", "timezone_id": "America/Sao_Paulo"}
    user_agent = os.environ.get("USER_AGENT", "").strip()
    if user_agent:
        opcoes_contexto["user_agent"] = user_agent

    with sync_playwright() as pw:
        navegador = pw.chromium.launch(channel="chrome", headless=headless)
        try:
            contexto = navegador.new_context(**opcoes_contexto)
            contexto.add_cookies(cookies_para_navegador(cookie_atual))
            pagina = contexto.new_page()
            resposta = pagina.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            status = resposta.status if resposta else None
            try:
                pagina.wait_for_selector(selectors.CARD, timeout=timeout_ms)
            except PlaywrightTimeoutError:
                # sem cards no tempo limite, devolve o que carregou para a trava avaliar
                pass
            html = pagina.content()
            url_final = pagina.url
            cookies_sessao = contexto.cookies(list(URLS_COOKIE))
        finally:
            navegador.close()
    return status, url_final, html, cookies_sessao


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
    categoria_padrao: str | None = None,
    tema_origem: str | None = None,
    registrar_execucao: bool = True,
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
                        nota, vendas, termo_origem, tema_origem, status, data_captura, data_atualizacao
                    ) VALUES (?, 'Mercado Livre', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'novo', ?, ?);
                    """,
                    (
                        item["id_produto"],
                        item["nome_produto"],
                        item.get("categoria") or categoria_padrao,
                        item.get("imagem_url"),
                        item.get("url_limpa") or item["url_original"],
                        item.get("valor_original"),
                        item.get("valor_atual"),
                        item.get("desconto_pct"),
                        item.get("nota"),
                        item.get("vendas"),
                        termo,
                        item.get("tema_origem") or tema_origem,
                        hoje_str,
                        agora_iso,
                    ),
                )
                if cursor.rowcount == 1:
                    inseridos += 1
                else:
                    ignorados += 1

            if registrar_execucao:
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
    arquivo_html_path: Path | None = None,
    max_retries_override: int | None = None,
    custom_cookie_path: Path | None = None,
    categoria_padrao: str | None = None,
    tema_origem: str | None = None,
    registrar_execucao: bool = True,
    aplicar_pausa: bool = False,
) -> dict:
    # executa o fluxo completo de captacao de um termo ate a gravacao no sqlite
    inicio_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
    logger = get_logger("capture")

    try:
        if html_override is not None:
            html = html_override
        elif arquivo_html_path is not None:
            html = Path(arquivo_html_path).read_text(encoding="utf-8")
        else:
            html = buscar_html_termo(
                termo,
                aplicar_pausa=aplicar_pausa,
                max_retries_override=max_retries_override,
                custom_cookie_path=custom_cookie_path,
            )
    except Exception as e:
        if registrar_execucao:
            gravar_captacao_sqlite(
                termo=termo,
                cards_extraidos=[],
                aprovados=[],
                inicio_iso=inicio_iso,
                custom_db_path=custom_db_path,
                status_exec="bloqueado" if isinstance(e, BloqueioCaptacaoError) else "erro",
                erro_exec=str(e),
                registrar_execucao=True,
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
        if registrar_execucao:
            gravar_captacao_sqlite(
                termo=termo,
                cards_extraidos=[],
                aprovados=[],
                inicio_iso=inicio_iso,
                custom_db_path=custom_db_path,
                status_exec="erro",
                erro_exec="0 cards encontrados",
                registrar_execucao=True,
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
        categoria_padrao=categoria_padrao,
        tema_origem=tema_origem,
        registrar_execucao=registrar_execucao,
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


def selecionar_buscas_elegiveis(
    registros_buscas: list[dict],
    max_buscas_dia: int = 30,
    vetos: dict[str, list[str]] | None = None,
    hoje_str: str | None = None,
    pular_executadas_hoje: bool = True,
) -> list[dict]:
    # filtra buscas com status ativo, sem vetos e dentro do orcamento diario max_buscas_dia
    regras_veto = vetos if vetos is not None else carregar_vetos()
    limite_dia = max(1, int(max_buscas_dia))

    executadas_hoje = 0
    if pular_executadas_hoje and hoje_str:
        for reg in registros_buscas:
            ult = str(reg.get("ultima_execucao") or "")
            if ult.startswith(hoje_str):
                executadas_hoje += 1

    cota_restante = (
        max(0, limite_dia - executadas_hoje) if (pular_executadas_hoje and hoje_str) else limite_dia
    )
    if cota_restante == 0:
        return []

    candidatas: list[dict] = []
    for reg in registros_buscas:
        if int(reg.get("ativo", 0)) != 1:
            continue
        termo = str(reg.get("termo") or "").strip()
        nicho = str(reg.get("nicho") or "").strip()
        if not termo or termo_contem_veto(termo, regras_veto):
            continue
        if nicho and termo_contem_veto(nicho, regras_veto):
            continue
        if pular_executadas_hoje and hoje_str:
            ult = str(reg.get("ultima_execucao") or "")
            if ult.startswith(hoje_str):
                continue
        candidatas.append(dict(reg))

    candidatas.sort(
        key=lambda b: (
            int(b.get("prioridade") or 5),
            int(b.get("aprovados_total") or 0),
            -int(b.get("id") or 0),
        ),
        reverse=True,
    )
    return candidatas[:cota_restante]


def calcular_atualizacao_busca(
    registro_busca: dict,
    total_cards: int,
    total_aprovados: int,
    agora_iso: str,
    data_ref: date | None = None,
) -> dict:
    # calcula novos contadores, ultima_execucao, falhas consecutivas e inativacao apos 3 falhas
    execucoes_novo = int(registro_busca.get("execucoes") or 0) + 1
    resultados_novo = int(registro_busca.get("resultados_total") or 0) + max(0, int(total_cards))
    aprovados_novo = int(registro_busca.get("aprovados_total") or 0) + max(0, int(total_aprovados))

    if int(total_aprovados) > 0:
        sem_aprovado_novo = 0
        ativo_novo = 1
        inativo_ate_novo = None
    else:
        sem_aprovado_novo = int(registro_busca.get("execucoes_sem_aprovado") or 0) + 1
        if sem_aprovado_novo >= 3:
            ativo_novo = 0
            ref_d = data_ref or datetime.fromisoformat(agora_iso).date()
            inativo_ate_novo = (ref_d + timedelta(days=14)).strftime("%Y-%m-%d")
        else:
            ativo_novo = 1
            inativo_ate_novo = None

    return {
        "execucoes": execucoes_novo,
        "execucoes_sem_aprovado": sem_aprovado_novo,
        "ultima_execucao": agora_iso,
        "resultados_total": resultados_novo,
        "aprovados_total": aprovados_novo,
        "ativo": ativo_novo,
        "inativo_ate": inativo_ate_novo,
    }


def _registrar_execucao_capture(
    inicio_iso: str,
    status_exec: str,
    itens: int,
    erro_exec: str | None = None,
    custom_db_path: Path | None = None,
) -> None:
    # insere linha de auditoria da rodada de captacao na tabela execucoes
    fim_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
    conn = get_connection(custom_db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO execucoes (modulo, inicio, fim, status, itens, erro)
                VALUES ('capture', ?, ?, ?, ?, ?);
                """,
                (inicio_iso, fim_iso, status_exec, int(itens), erro_exec),
            )
    finally:
        conn.close()


def run(
    data_ref: date | None = None,
    custom_db_path: Path | None = None,
    custom_config_path: Path | None = None,
    custom_vetos_path: Path | None = None,
    custom_cookie_path: Path | None = None,
    max_buscas_override: int | None = None,
    pular_executadas_hoje: bool = True,
    html_por_termo: dict[str, str] | Callable[[str], str] | None = None,
    arquivo_html_path: Path | None = None,
    max_retries_override: int | None = None,
) -> dict:
    # consome termos ativos da tabela buscas, executa a captacao, atualiza buscas e para no 1o bloqueio
    init_db(custom_db_path)
    agora_dt = datetime.now(TZ_SP)
    inicio_iso = agora_dt.isoformat(timespec="seconds")
    ref_date = data_ref or agora_dt.date()
    hoje_str = ref_date.strftime("%Y-%m-%d")
    logger = get_logger("capture")

    max_buscas = (
        int(max_buscas_override)
        if max_buscas_override is not None
        else carregar_max_buscas_dia(custom_config_path)
    )
    vetos = carregar_vetos(custom_vetos_path)

    conn = get_connection(custom_db_path)
    try:
        rows_buscas = [dict(r) for r in conn.execute("SELECT * FROM buscas;").fetchall()]
        rows_temas = conn.execute("SELECT id, tema FROM temas;").fetchall()
        mapa_temas = {int(r["id"]): str(r["tema"]) for r in rows_temas if r["id"] is not None}

        # desativa imediatamente qualquer busca ativa cujo termo ou nicho viole vetos.yaml
        with conn:
            for reg in rows_buscas:
                if int(reg.get("ativo", 0)) == 1:
                    t_str = str(reg.get("termo") or "")
                    n_str = str(reg.get("nicho") or "")
                    if termo_contem_veto(t_str, vetos) or (n_str and termo_contem_veto(n_str, vetos)):
                        conn.execute("UPDATE buscas SET ativo = 0 WHERE id = ?;", (reg["id"],))
                        reg["ativo"] = 0
    finally:
        conn.close()

    elegiveis = selecionar_buscas_elegiveis(
        registros_buscas=rows_buscas,
        max_buscas_dia=max_buscas,
        vetos=vetos,
        hoje_str=hoje_str,
        pular_executadas_hoje=pular_executadas_hoje,
    )

    total_cards_lote = 0
    total_aprovados_lote = 0
    total_inseridos_lote = 0
    total_ignorados_lote = 0
    aposentados_lote = 0
    resultados_por_termo: list[dict] = []

    for idx, busca in enumerate(elegiveis):
        termo = str(busca["termo"])
        busca_id = busca.get("id")
        nicho = busca.get("nicho")
        tema_orig = mapa_temas.get(int(busca["tema_id"])) if busca.get("tema_id") is not None else None

        html_override: str | None = None
        if callable(html_por_termo):
            html_override = html_por_termo(termo)
        elif isinstance(html_por_termo, dict) and termo in html_por_termo:
            html_override = html_por_termo[termo]

        usar_pausa = idx > 0 and html_override is None and arquivo_html_path is None

        try:
            res_termo = executar_captacao_termo(
                termo=termo,
                custom_db_path=custom_db_path,
                html_override=html_override,
                arquivo_html_path=arquivo_html_path,
                max_retries_override=max_retries_override,
                custom_cookie_path=custom_cookie_path,
                categoria_padrao=nicho,
                tema_origem=tema_orig,
                registrar_execucao=False,
                aplicar_pausa=usar_pausa,
            )
        except BloqueioCaptacaoError as e:
            _registrar_execucao_capture(
                inicio_iso=inicio_iso,
                status_exec="bloqueado",
                itens=total_inseridos_lote,
                erro_exec=str(e),
                custom_db_path=custom_db_path,
            )
            raise
        except Exception as e:
            _registrar_execucao_capture(
                inicio_iso=inicio_iso,
                status_exec="erro",
                itens=total_inseridos_lote,
                erro_exec=str(e),
                custom_db_path=custom_db_path,
            )
            raise

        agora_termo_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
        atualizacao = calcular_atualizacao_busca(
            registro_busca=busca,
            total_cards=res_termo["total_cards"],
            total_aprovados=res_termo["total_aprovados"],
            agora_iso=agora_termo_iso,
            data_ref=ref_date,
        )
        if atualizacao["ativo"] == 0 and int(busca.get("ativo", 1)) == 1:
            aposentados_lote += 1

        if busca_id is not None:
            conn_upd = get_connection(custom_db_path)
            try:
                with conn_upd:
                    conn_upd.execute(
                        """
                        UPDATE buscas
                        SET execucoes = ?,
                            execucoes_sem_aprovado = ?,
                            ultima_execucao = ?,
                            resultados_total = ?,
                            aprovados_total = ?,
                            ativo = ?,
                            inativo_ate = ?
                        WHERE id = ?;
                        """,
                        (
                            atualizacao["execucoes"],
                            atualizacao["execucoes_sem_aprovado"],
                            atualizacao["ultima_execucao"],
                            atualizacao["resultados_total"],
                            atualizacao["aprovados_total"],
                            atualizacao["ativo"],
                            atualizacao["inativo_ate"],
                            busca_id,
                        ),
                    )
            finally:
                conn_upd.close()

        total_cards_lote += res_termo["total_cards"]
        total_aprovados_lote += res_termo["total_aprovados"]
        total_inseridos_lote += res_termo["inseridos"]
        total_ignorados_lote += res_termo["ignorados"]
        resultados_por_termo.append(res_termo)

    _registrar_execucao_capture(
        inicio_iso=inicio_iso,
        status_exec="sucesso",
        itens=total_inseridos_lote,
        erro_exec=None,
        custom_db_path=custom_db_path,
    )

    logger.info(
        f"Rodada capture concluida: termos={len(elegiveis)}, "
        f"inseridos={total_inseridos_lote}, aposentados={aposentados_lote}"
    )

    return {
        "termos_executados": len(elegiveis),
        "total_cards": total_cards_lote,
        "total_aprovados": total_aprovados_lote,
        "inseridos": total_inseridos_lote,
        "ignorados": total_ignorados_lote,
        "aposentados": aposentados_lote,
        "resultados": resultados_por_termo,
    }
