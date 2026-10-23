# configuracao de logs estruturados em json lines do bytz com sanitizacao completa
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PASTA_BACK = Path(__file__).resolve().parent.parent

TZ_SP = ZoneInfo("America/Sao_Paulo")


def ensure_env_loaded() -> None:
    # carrega explicitamente o .env apenas quando solicitado, sem efeito colateral na importacao
    env_path = PASTA_BACK / ".env"
    if env_path.exists():
        load_dotenv(env_path)


PADROES_SENSIVEIS = [
    # 1. credenciais embutidas em url (http://user:password@host)
    (
        re.compile(r"(https?://)([^:\s/@]+):([^@\s/]+)@", re.IGNORECASE),
        r"\1<USUARIO>:<SENHA_MASCARADA>@",
    ),
    # 2. bot token do telegram (na url ou isolado)
    (
        re.compile(r"bot(\d{8,12}:[A-Za-z0-9_-]{30,50})", re.IGNORECASE),
        r"bot<TOKEN_MASCARADO>",
    ),
    (
        re.compile(r"\b(\d{8,12}:[A-Za-z0-9_-]{30,50})\b"),
        r"<TOKEN_MASCARADO>",
    ),
    # 3. cabecalho cookie completo (Cookie: ...)
    (
        re.compile(r"(?i)(?:^|\b)(cookie\s*:\s*)([^\r\n]+)"),
        r"\1<COOKIE_MASCARADO>",
    ),
    # 4. query params sensiveis em urls (inclusive tag de afiliado)
    (
        re.compile(
            r"([?&](?:api[_-]?key|token|access_token|key|secret|password|senha|auth|sig|tag)=)([^&\s]+)",
            re.IGNORECASE,
        ),
        r"\1<MASCARADO>",
    ),
    # 5. cookies e tokens de sessao individuais
    (
        re.compile(
            r"((?:_csrf|ssid|nsa_rotok|session|jwt)=)([^;\s,]+)",
            re.IGNORECASE,
        ),
        r"\1<MASCARADO>",
    ),
    # 6. autorizacao bearer
    (
        re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
        r"\1<TOKEN_MASCARADO>",
    ),
    # 7. atribuicoes simples de token, senha, chave e secret (com ou sem aspas)
    (
        re.compile(
            r"""(?i)(?<![?&])\b(senha|password|token|api_key|secret|chave)\s*[:=]\s*['"]?([A-Za-z0-9_\-\.\$\@\#\!]{4,})['"]?"""
        ),
        r'\1 = "<MASCARADO>"',
    ),
]


def sanitize_text(texto: str) -> str:
    # remove tokens, senhas, cookies e credenciais de qualquer string
    if not isinstance(texto, str):
        texto = str(texto)
    for padrao, substituto in PADROES_SENSIVEIS:
        texto = padrao.sub(substituto, texto)
    return texto


def sanitize_data(dado: Any) -> Any:
    # percorre recursivamente dicionarios, listas e valores sanitizando dados
    if isinstance(dado, dict):
        resultado = {}
        for k, v in dado.items():
            chave_str = str(k)
            # se a propria chave for sensivel, mascara o valor por completo
            if any(
                s in chave_str.lower()
                for s in ("token", "cookie", "secret", "senha", "password", "key", "cred", "tag")
            ):
                resultado[chave_str] = "<MASCARADO>"
            else:
                resultado[chave_str] = sanitize_data(v)
        return resultado
    if isinstance(dado, (list, tuple, set)):
        return [sanitize_data(item) for item in dado]
    if isinstance(dado, str):
        return sanitize_text(dado)
    return dado


def get_logs_dir(custom_logs_dir: Path | None = None) -> Path:
    # resolve o diretorio de logs usando custom_logs_dir, env ou padrao em back/logs
    if custom_logs_dir:
        caminho = Path(custom_logs_dir)
        caminho = caminho if caminho.is_absolute() else (PASTA_BACK / caminho).resolve()
    else:
        env_dir = os.environ.get("LOGS_DIR")
        if env_dir:
            p = Path(env_dir)
            caminho = p if p.is_absolute() else (PASTA_BACK / p).resolve()
        else:
            caminho = PASTA_BACK / "logs"
    caminho.mkdir(parents=True, exist_ok=True)
    return caminho


class JsonLinesFormatter(logging.Formatter):
    # formata os registros de log como json lines sanitizado
    def format(self, record: logging.LogRecord) -> str:
        agora = datetime.now(TZ_SP).isoformat(timespec="seconds")
        mensagem_limpa = sanitize_text(record.getMessage())
        dados = {
            "timestamp": agora,
            "level": record.levelname,
            "logger": record.name,
            "message": mensagem_limpa,
        }
        if hasattr(record, "detalhes") and isinstance(record.detalhes, dict):
            dados["detalhes"] = sanitize_data(record.detalhes)
        if record.exc_info:
            dados["exception"] = sanitize_text(self.formatException(record.exc_info))
        return json.dumps(dados, ensure_ascii=False)


class SanitizedConsoleFormatter(logging.Formatter):
    # formata a saida do console com sanitizacao ativa
    def format(self, record: logging.LogRecord) -> str:
        bruto = super().format(record)
        return sanitize_text(bruto)


def get_logger(nome: str = "bytz", custom_logs_dir: Path | None = None) -> logging.Logger:
    # cria logger apenas quando solicitado, sem criar arquivos na importacao
    logger = logging.getLogger(nome)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    pasta_logs = get_logs_dir(custom_logs_dir)
    arquivo_log = pasta_logs / "bytz.log"
    file_handler = logging.FileHandler(arquivo_log, encoding="utf-8")
    file_handler.setFormatter(JsonLinesFormatter())
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(SanitizedConsoleFormatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(console_handler)

    return logger
