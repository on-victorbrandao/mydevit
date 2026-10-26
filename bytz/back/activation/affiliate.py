# geracao de links de afiliado do mercado livre nas modalidades individual e lote
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from activation.cookies import (
    CookieError,
    atualizar_sessao_apos_resposta,
    carregar_cookie,
)
from activation.fallback_queue import (
    validar_link_afiliado_url,
    verificar_e_acionar_plano_b,
)
from core.alerts import send_alert
from core.db import get_connection
from core.logger import get_logger

PASTA_BACK = Path(__file__).resolve().parent.parent
TZ_SP = ZoneInfo("America/Sao_Paulo")

LINKBUILDER_URL = "https://www.mercadolivre.com.br/afiliados/linkbuilder"
CREATE_LINK_URL = "https://www.mercadolivre.com.br/affiliate-program/api/v2/affiliates/createLink"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0 Safari/537.36"
)

DOMINIOS_ML_ORIGINAL = (
    "mercadolivre.com.br",
    "www.mercadolivre.com.br",
    "mercadolivre.com",
    "www.mercadolivre.com",
    "produto.mercadolivre.com.br",
)


class AffiliateError(Exception):
    # erro especifico da api de afiliados do mercado livre
    def __init__(
        self,
        mensagem: str,
        http_status: int | None = None,
        body_status: int | None = None,
        bloqueio: bool = False,
    ):
        super().__init__(mensagem)
        self.http_status = http_status
        self.body_status = body_status
        self.bloqueio = bloqueio


def validar_url_produto(url: str) -> bool:
    # valida se a url original e um link valido do mercado livre sem bugs legados
    if not url or not isinstance(url, str):
        return False
    url_limpa = url.strip()
    if url_limpa.startswith("="):
        return False
    if "/mclics/" in url_limpa:
        return False
    try:
        parsed = urlparse(url_limpa)
        if parsed.scheme.lower() != "https":
            return False
        if parsed.username or parsed.password:
            return False
        host = (parsed.hostname or "").lower()
        return host in DOMINIOS_ML_ORIGINAL and bool(parsed.path and parsed.path != "/")
    except Exception:  # noqa: BLE001
        return False


def montar_payload(urls: list[str], tag: str = "") -> dict:
    # monta o dicionario json esperado pelo endpoint createLink
    return {
        "urls": [u.strip() for u in urls if u and isinstance(u, str)],
        "tag": tag.strip() if tag else "",
    }


def extrair_links_resposta(
    resposta_json: dict,
    urls_solicitadas: list[str],
) -> dict:
    # mapeia de forma segura cada url solicitada para sua respectiva url de afiliado gerada
    itens_resp = resposta_json.get("urls") or []
    sucessos: list[dict] = []
    falhas: list[dict] = []
    mapa: dict[str, str] = {}

    # caso 1: itens da resposta contem identificador explicito da url de entrada
    tem_url_explicita = any(
        isinstance(item, dict)
        and any(k in item for k in ("origin_url", "original_url", "originalUrl", "url", "input_url"))
        for item in itens_resp
    )

    if tem_url_explicita:
        for item in itens_resp:
            if not isinstance(item, dict):
                continue
            orig = (
                item.get("origin_url")
                or item.get("original_url")
                or item.get("originalUrl")
                or item.get("input_url")
                or item.get("url")
            )
            short = item.get("short_url") or item.get("shortUrl")
            item_status = item.get("status", 200)

            if orig and short and item_status == 200 and validar_link_afiliado_url(short):
                mapa[orig] = short
                sucessos.append({"url_original": orig, "url_afiliado": short})
            elif orig:
                falhas.append(
                    {
                        "url_original": orig,
                        "motivo": item.get("error") or item.get("message") or f"status {item_status}",
                    }
                )
    else:
        # caso 2: correspondencia posicional preservando a ordem do lote solicitado
        for idx, orig_url in enumerate(urls_solicitadas):
            if idx < len(itens_resp) and isinstance(itens_resp[idx], dict):
                item = itens_resp[idx]
                short = item.get("short_url") or item.get("shortUrl")
                item_status = item.get("status", 200)

                if short and item_status == 200 and validar_link_afiliado_url(short):
                    mapa[orig_url] = short
                    sucessos.append({"url_original": orig_url, "url_afiliado": short})
                else:
                    motivo = item.get("error") or item.get("message") or f"status {item_status}"
                    falhas.append({"url_original": orig_url, "motivo": motivo})
            else:
                falhas.append({"url_original": orig_url, "motivo": "Resposta incompleta no lote"})

    return {
        "sucessos": sucessos,
        "falhas": falhas,
        "mapa": mapa,
    }


def executar_requisicao_linkbuilder(
    cookie_str: str,
    session: requests.Session | None = None,
    timeout_s: int = 20,
) -> tuple[int, list[str], str]:
    # dispara GET inicial para atualizar sessao e capturar todos os headers Set-Cookie
    requester = session or requests
    headers = {
        "cookie": cookie_str,
        "user-agent": USER_AGENT,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        resp = requester.get(
            LINKBUILDER_URL,
            headers=headers,
            timeout=timeout_s,
            allow_redirects=False,
        )
    except Exception as e:
        raise AffiliateError(f"Falha de conexao com linkbuilder: {type(e).__name__}") from e

    # detecta redirecionamento para login ou verificacao de conta como sessao expirada
    if resp.status_code in (301, 302, 303, 307):
        loc = resp.headers.get("Location", "")
        if any(termo in loc for termo in ("/login", "/gz/account-verification", "account_verification")):
            raise AffiliateError(
                "Sessao expirada: linkbuilder redirecionou para autenticacao",
                http_status=resp.status_code,
                bloqueio=True,
            )

    if resp.status_code in (403, 429):
        raise AffiliateError(
            f"Bloqueio ou limitacao detectada no linkbuilder: HTTP {resp.status_code}",
            http_status=resp.status_code,
            bloqueio=True,
        )

    if resp.status_code != 200:
        raise AffiliateError(
            f"GET no linkbuilder retornou status inesperado: HTTP {resp.status_code}",
            http_status=resp.status_code,
        )

    # extrai a lista completa de Set-Cookie do objeto raw de headers
    raw_headers = getattr(resp.raw, "headers", None)
    if raw_headers and hasattr(raw_headers, "getlist"):
        set_cookies = raw_headers.getlist("Set-Cookie")
    else:
        set_cookies = [v for k, v in resp.headers.items() if k.lower() == "set-cookie"]

    return resp.status_code, set_cookies, resp.text


def executar_requisicao_create_link(
    urls: list[str],
    cookie_str: str,
    csrf_token: str,
    tag: str = "",
    session: requests.Session | None = None,
    timeout_s: int = 20,
) -> tuple[int, dict, list[str]]:
    # dispara POST no endpoint oficial createLink com x-csrf-token e payload montado
    requester = session or requests
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://www.mercadolivre.com.br",
        "referer": LINKBUILDER_URL,
        "user-agent": USER_AGENT,
        "x-csrf-token": csrf_token,
        "cookie": cookie_str,
    }
    payload = montar_payload(urls, tag)

    try:
        resp = requester.post(
            CREATE_LINK_URL,
            headers=headers,
            json=payload,
            timeout=timeout_s,
        )
    except Exception as e:
        raise AffiliateError(f"Falha de conexao com createLink: {type(e).__name__}") from e

    if resp.status_code in (401, 403, 429):
        raise AffiliateError(
            f"Falha de autenticacao ou bloqueio no createLink: HTTP {resp.status_code}",
            http_status=resp.status_code,
            bloqueio=True,
        )

    try:
        corpo = resp.json()
    except Exception as e:
        raise AffiliateError(
            f"Resposta do createLink nao e JSON valido (HTTP {resp.status_code})",
            http_status=resp.status_code,
        ) from e

    # captura possiveis novos Set-Cookie retornados pelo POST
    raw_headers = getattr(resp.raw, "headers", None)
    if raw_headers and hasattr(raw_headers, "getlist"):
        set_cookies = raw_headers.getlist("Set-Cookie")
    else:
        set_cookies = [v for k, v in resp.headers.items() if k.lower() == "set-cookie"]

    body_status = corpo.get("status") if isinstance(corpo, dict) else None
    if resp.status_code != 200 or body_status != 200:
        raise AffiliateError(
            f"createLink retornou erro: http={resp.status_code} body.status={body_status}",
            http_status=resp.status_code,
            body_status=body_status,
        )

    return resp.status_code, corpo, set_cookies


def gerar_links_afiliado(
    urls: list[str],
    modalidade: str = "lote",
    tag: str | None = None,
    cookie_path: Path | str | None = None,
    session: requests.Session | None = None,
) -> dict:
    # orquestra o fluxo de geracao de links suportando modalidade individual ou lote
    logger = get_logger("affiliate")
    urls_validas = [u for u in urls if validar_url_produto(u)]

    if not urls_validas:
        return {"sucessos": [], "falhas": [], "mapa": {}, "total": 0}

    # resolve a tag a partir do parametro ou da variavel de ambiente
    tag_efetiva = tag if tag is not None else os.environ.get("AFFILIATE_TAG", "")

    # carrega o cookie da conta principal
    try:
        dados_cookie = carregar_cookie(cookie_path)
    except CookieError as e:
        logger.error(f"Erro ao carregar cookie de afiliado: {e}")
        send_alert(f"Ativacao parada: cookie de afiliado invalido ({e})", nivel="critical")
        raise

    sucessos_totais: list[dict] = []
    falhas_totais: list[dict] = []
    mapa_total: dict[str, str] = {}

    if modalidade == "individual":
        for idx, url_prod in enumerate(urls_validas):
            if idx > 0:
                time.sleep(3)
            try:
                # 1. GET inicial no linkbuilder
                _, set_cookies_get, _ = executar_requisicao_linkbuilder(
                    dados_cookie.get("cookie", ""),
                    session=session,
                )
                dados_cookie, csrf = atualizar_sessao_apos_resposta(
                    dados_cookie,
                    set_cookies_get,
                    caminho=cookie_path,
                )

                # 2. POST createLink individual
                _, corpo_post, set_cookies_post = executar_requisicao_create_link(
                    [url_prod],
                    dados_cookie.get("cookie", ""),
                    csrf,
                    tag=tag_efetiva,
                    session=session,
                )
                if set_cookies_post:
                    dados_cookie, _ = atualizar_sessao_apos_resposta(
                        dados_cookie,
                        set_cookies_post,
                        caminho=cookie_path,
                    )

                res = extrair_links_resposta(corpo_post, [url_prod])
                sucessos_totais.extend(res["sucessos"])
                falhas_totais.extend(res["falhas"])
                mapa_total.update(res["mapa"])
            except AffiliateError as e:
                logger.error(f"Falha ao gerar link individual para {url_prod[:40]}: {e}")
                falhas_totais.append({"url_original": url_prod, "motivo": str(e)})
                if e.bloqueio:
                    send_alert(f"Bloqueio ou sessao expirada no linkbuilder: {e}", nivel="critical")
                    break
    else:
        # modalidade lote: uma operacao com multiplas URLs
        try:
            # 1. GET inicial no linkbuilder
            _, set_cookies_get, _ = executar_requisicao_linkbuilder(
                dados_cookie.get("cookie", ""),
                session=session,
            )
            dados_cookie, csrf = atualizar_sessao_apos_resposta(
                dados_cookie,
                set_cookies_get,
                caminho=cookie_path,
            )

            # 2. POST createLink com lote completo
            _, corpo_post, set_cookies_post = executar_requisicao_create_link(
                urls_validas,
                dados_cookie.get("cookie", ""),
                csrf,
                tag=tag_efetiva,
                session=session,
            )
            if set_cookies_post:
                dados_cookie, _ = atualizar_sessao_apos_resposta(
                    dados_cookie,
                    set_cookies_post,
                    caminho=cookie_path,
                )

            res = extrair_links_resposta(corpo_post, urls_validas)
            sucessos_totais.extend(res["sucessos"])
            falhas_totais.extend(res["falhas"])
            mapa_total.update(res["mapa"])
        except AffiliateError as e:
            logger.error(f"Falha ao gerar links em lote: {e}")
            for u in urls_validas:
                falhas_totais.append({"url_original": u, "motivo": str(e)})
            if e.bloqueio:
                send_alert(f"Bloqueio ou sessao expirada no lote: {e}", nivel="critical")

    return {
        "sucessos": sucessos_totais,
        "falhas": falhas_totais,
        "mapa": mapa_total,
        "total": len(urls_validas),
        "modalidade": modalidade,
    }


def executar_estagio_ativacao(
    limite: int | None = None,
    modalidade: str = "lote",
    tag: str | None = None,
    conn: sqlite3.Connection | None = None,
    session: requests.Session | None = None,
    cookie_path: Path | str | None = None,
) -> dict:
    # executa o estagio completo de ativacao consumindo curados do sqlite e promovendo a link_pronto
    logger = get_logger("affiliate")
    fechar_conn = False
    if conn is None:
        conn = get_connection()
        fechar_conn = True

    inicio_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
    status_exec = "sucesso"
    erro_msg = None
    links_gerados = 0

    try:
        query = """
            SELECT id_produto, nome_produto, score, url_original
            FROM produtos
            WHERE status = 'curado' AND (url_afiliado IS NULL OR url_afiliado = '')
            ORDER BY score DESC
        """
        if limite:
            query += f" LIMIT {int(limite)}"

        cursor = conn.execute(query)
        produtos = [dict(r) for r in cursor.fetchall()]

        if not produtos:
            logger.info("Nenhum produto curado pendente de link de afiliado")
            verificar_e_acionar_plano_b(conn=conn)
            return {
                "processados": 0,
                "sucessos": 0,
                "falhas": 0,
                "modalidade": modalidade,
                "produtos": [],
            }

        urls = [p["url_original"] for p in produtos]
        resultado = gerar_links_afiliado(
            urls,
            modalidade=modalidade,
            tag=tag,
            cookie_path=cookie_path,
            session=session,
        )

        mapa_links = resultado.get("mapa", {})
        agora = datetime.now(TZ_SP).isoformat(timespec="seconds")

        for prod in produtos:
            orig = prod["url_original"]
            short = mapa_links.get(orig)
            if short:
                conn.execute(
                    """
                    UPDATE produtos
                    SET url_afiliado = ?, status = 'link_pronto', data_atualizacao = ?
                    WHERE id_produto = ?
                    """,
                    (short, agora, prod["id_produto"]),
                )
                prod["url_afiliado"] = short
                prod["status"] = "link_pronto"
                links_gerados += 1
            else:
                prod["status"] = "curado"

        conn.commit()

        # se houver falhas ou saldo baixo, avalia necessidade do plano b
        verificar_e_acionar_plano_b(conn=conn)

        if not resultado.get("sucessos") and produtos:
            status_exec = "falha"
            erro_msg = "Nenhum link gerado com sucesso"

        return {
            "processados": len(produtos),
            "sucessos": links_gerados,
            "falhas": len(produtos) - links_gerados,
            "modalidade": modalidade,
            "produtos": produtos,
            "detalhes": resultado,
        }

    except Exception as e:  # noqa: BLE001
        status_exec = "erro"
        erro_msg = f"{type(e).__name__}: {e}"
        logger.error(f"Erro critico no estagio de ativacao: {erro_msg}")
        verificar_e_acionar_plano_b(conn=conn)
        return {
            "processados": 0,
            "sucessos": 0,
            "falhas": 0,
            "erro": erro_msg,
            "modalidade": modalidade,
        }
    finally:
        fim_iso = datetime.now(TZ_SP).isoformat(timespec="seconds")
        try:
            conn.execute(
                """
                INSERT INTO execucoes (modulo, inicio, fim, status, itens, erro)
                VALUES ('activation', ?, ?, ?, ?, ?)
                """,
                (inicio_iso, fim_iso, status_exec, links_gerados, erro_msg),
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Nao foi possivel registrar execucao da ativacao: {e}")
        if fechar_conn:
            conn.close()
