# gerenciamento, parsing, merge e persistencia atomica do cookie de afiliado do mercado livre
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PASTA_BACK = Path(__file__).resolve().parent.parent
TZ_SP = ZoneInfo("America/Sao_Paulo")
OBRIGATORIOS_LINKBUILDER = ("_csrf", "ssid", "nsa_rotok")


class CookieError(Exception):
    # excecao customizada para falhas de cookie sem expor segredos
    pass


def parse_cookie_string(cookie: str) -> dict[str, str]:
    # divide a string de cookies em dicionario preservando a ultima ocorrencia de chaves repetidas
    jar: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        key, _, val = part.strip().partition("=")
        if key:
            jar[key.strip()] = val
    return jar


def merge_cookie(old_cookie: str, set_cookie_headers: list[str]) -> tuple[str, str]:
    # funde o cookie vigente com os cabecalhos Set-Cookie da resposta e extrai o _csrf
    jar = parse_cookie_string(old_cookie)
    for header in set_cookie_headers or []:
        first = header.split(";")[0]
        key, _, val = first.strip().partition("=")
        if key:
            jar[key.strip()] = val
    cookie_atualizado = "; ".join(f"{k}={v}" for k, v in jar.items())
    return cookie_atualizado, jar.get("_csrf", "")


def get_cookie_file_path(custom_path: Path | str | None = None) -> Path:
    # define o caminho do arquivo de cookie da conta principal de afiliado
    if custom_path:
        p = Path(custom_path)
        return p if p.is_absolute() else (PASTA_BACK / p).resolve()
    env_path = os.environ.get("ML_COOKIE_FILE")
    if env_path:
        p = Path(env_path)
        return p if p.is_absolute() else (PASTA_BACK / p).resolve()
    return (PASTA_BACK / "ml_cookies.json").resolve()


def validar_cookie_conteudo(cookie_str: str) -> tuple[bool, list[str]]:
    # confere presenca dos cookies obrigatorios de sessao logada
    jar = parse_cookie_string(cookie_str)
    faltando = [nome for nome in OBRIGATORIOS_LINKBUILDER if nome not in jar]
    return (len(faltando) == 0, faltando)


def carregar_cookie(caminho: Path | str | None = None) -> dict:
    # carrega e valida o arquivo de cookie de afiliado de forma segura sem expor dados sensiveis
    arquivo = get_cookie_file_path(caminho)
    if not arquivo.exists():
        raise CookieError(f"Arquivo de cookie de afiliado nao encontrado: {arquivo.name}")

    try:
        with open(arquivo, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except Exception as e:
        raise CookieError(f"Falha ao ler JSON do arquivo de cookie: {type(e).__name__}") from e

    if not isinstance(dados, dict):
        raise CookieError("Conteudo do arquivo de cookie nao e um objeto JSON valido")

    cookie_str = dados.get("cookie")
    if not isinstance(cookie_str, str) or not cookie_str.strip():
        raise CookieError("Campo 'cookie' ausente ou vazio no arquivo de cookie")

    valido, faltando = validar_cookie_conteudo(cookie_str)
    if not valido:
        raise CookieError(f"Cookies obrigatorios ausentes na sessao: {faltando}")

    return dados


def salvar_cookie_atomico(dados: dict, caminho: Path | str | None = None) -> Path:
    # grava o dicionario de cookie em arquivo temporario e substitui atomicamente
    arquivo = get_cookie_file_path(caminho)
    arquivo.parent.mkdir(parents=True, exist_ok=True)

    cookie_str = dados.get("cookie")
    if not isinstance(cookie_str, str) or not cookie_str.strip():
        raise CookieError("Nao e permitido salvar cookie vazio ou invalido")

    temporario = arquivo.with_suffix(".tmp")
    with open(temporario, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

    os.replace(temporario, arquivo)
    return arquivo


def atualizar_sessao_apos_resposta(
    dados_atuais: dict,
    set_cookie_headers: list[str],
    caminho: Path | str | None = None,
) -> tuple[dict, str]:
    # atualiza a string de cookies com novos Set-Cookie e salva o arquivo com timestamp novo
    cookie_antigo = dados_atuais.get("cookie", "")
    cookie_novo, csrf_token = merge_cookie(cookie_antigo, set_cookie_headers)

    agora = datetime.now(TZ_SP).isoformat(timespec="seconds")
    dados_atualizados = {
        "marketplace": dados_atuais.get("marketplace", "Mercado Livre"),
        "origem": dados_atuais.get("origem", "linkbuilder"),
        "atualizacao": agora,
        "cookie": cookie_novo,
    }

    salvar_cookie_atomico(dados_atualizados, caminho)
    return dados_atualizados, csrf_token
