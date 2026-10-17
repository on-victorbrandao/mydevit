# script de diagnostico e verificacao de saude do ambiente bytz
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from core.db import get_db_path

TZ_SP = ZoneInfo("America/Sao_Paulo")

TABELAS_ESPERADAS = {
    "produtos",
    "historico_preco",
    "temas",
    "buscas",
    "posts",
    "metricas_snapshots",
    "execucoes",
    "gasto_ia",
}

INDICES_ESPERADOS = {
    "ix_prod_status",
    "ix_posts_data",
}


def load_env_if_needed() -> None:
    # carrega explicitamente o .env do back apenas no ponto de entrada
    env_file = PASTA_BACK / ".env"
    if env_file.exists():
        load_dotenv(env_file)


# 1. modulos locais (fundacao s0)


def checar_python() -> tuple[bool, str]:
    # valida versao minima do python (3.11+)
    versao = sys.version_info
    texto = f"{versao.major}.{versao.minor}.{versao.micro}"
    if versao.major == 3 and versao.minor >= 11:
        return True, f"Python {texto} compativel"
    return False, f"Python {texto} incompativel (requer >= 3.11)"


def checar_disco() -> tuple[bool, str]:
    # confere espaco livre em disco na particao do projeto (minimo 1 gb)
    _total, _used, free = shutil.disk_usage(PASTA_BACK)
    livre_gb = round(free / (1024**3), 2)
    if livre_gb >= 1.0:
        return True, f"Espaco em disco: {livre_gb} GB livres"
    return False, f"Espaco em disco critico: apenas {livre_gb} GB livres"


def checar_banco(custom_db_path: Path | None = None) -> tuple[bool, str]:
    # verifica o banco existente em modo somente leitura sem criar arquivo nem alterar schema
    caminho = custom_db_path or get_db_path()
    if not caminho.exists():
        return False, f"Banco ausente em {caminho.name} (arquivo nao existe)"

    try:
        uri_path = f"file:{caminho.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri_path, uri=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tabelas_no_banco = {row[0] for row in cursor.fetchall()}

            cursor.execute("SELECT name FROM sqlite_master WHERE type='index';")
            indices_no_banco = {row[0] for row in cursor.fetchall()}
        finally:
            conn.close()

        faltando_tab = TABELAS_ESPERADAS - tabelas_no_banco
        faltando_idx = INDICES_ESPERADOS - indices_no_banco

        if faltando_tab or faltando_idx:
            detalhes = []
            if faltando_tab:
                detalhes.append(f"tabelas ausentes: {sorted(faltando_tab)}")
            if faltando_idx:
                detalhes.append(f"indices ausentes: {sorted(faltando_idx)}")
            return False, f"Banco incompleto em {caminho.name} ({'; '.join(detalhes)})"

        return (
            True,
            f"Banco SQLite integro ({len(TABELAS_ESPERADAS)} tabelas e {len(INDICES_ESPERADOS)} indices verificados em {caminho.name})",
        )
    except Exception as e:  # noqa: BLE001
        return False, f"Falha ao inspecionar banco SQLite: {type(e).__name__}"


def checar_cookie_local(custom_cookie_path: Path | None = None) -> tuple[bool, str]:
    # valida tipo, formato e frescor do cookie localmente sem acessar o mercado livre
    if custom_cookie_path:
        arquivo = Path(custom_cookie_path)
    else:
        caminho_cookie = os.environ.get("ML_COOKIE_FILE")
        arquivo = Path(caminho_cookie) if caminho_cookie else (PASTA_BACK / "ml_cookies.json")
    if not arquivo.is_absolute():
        arquivo = (PASTA_BACK / arquivo).resolve()

    if not arquivo.exists():
        return False, f"Arquivo de cookie nao encontrado em: {arquivo.name}"

    try:
        with open(arquivo, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return False, f"Arquivo {arquivo.name} contem JSON malformado: {type(e).__name__}"

    if not isinstance(dados, dict):
        return False, "Conteudo nao e um objeto JSON valido"

    cookie_str = dados.get("cookie")
    if not isinstance(cookie_str, str) or not cookie_str.strip():
        return False, "Campo 'cookie' ausente, nulo ou nao e uma string valida"

    nomes = [p.split("=")[0].strip() for p in cookie_str.split(";") if "=" in p]
    obrigatorios = ("_csrf", "ssid", "nsa_rotok")
    faltando = [n for n in obrigatorios if n not in nomes]
    if faltando:
        return False, f"Cookies obrigatorios ausentes no arquivo: {faltando}"

    # validacao de data sem imprimir o conteudo bruto no erro
    data_str = dados.get("atualizacao")
    if not data_str or not isinstance(data_str, str):
        return False, "Campo 'atualizacao' ausente ou tipo invalido no JSON"

    try:
        data_up = datetime.fromisoformat(data_str)
    except ValueError:
        return False, "Campo 'atualizacao' contem formato de data ISO invalido"

    agora = datetime.now(data_up.tzinfo or TZ_SP)
    if data_up.tzinfo is None:
        data_up = data_up.replace(tzinfo=TZ_SP)

    idade_dias = (agora - data_up).days
    aviso_idade = f" (atencao: atualizado ha {idade_dias} dias)" if idade_dias > 7 else ""

    return (
        True,
        f"Estrutura local verificada ({len(nomes)} cookies, {len(obrigatorios)} obrigatorios presentes){aviso_idade}. Sessao nao verificada remotamente.",
    )


def checar_orcamento_ia_local() -> tuple[bool, str]:
    # controle financeiro e tarifas de ia adiados por decisao do dono
    return (
        True,
        "Controle de tarifas e limites financeiros adiado por decisao do dono (nao bloqueador no S0)",
    )


# 2. integracoes externas (servicos e credenciais)


def checar_sheets_externo(test_mode: bool = False) -> tuple[bool, str]:
    # valida configuracao e conexao somente leitura com o google sheets
    sheet_id = os.environ.get("SHEET_ID")
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")

    if not sheet_id or not sa_path:
        return False, "Configuracao ausente no .env (pendente para espelho e Fila)"

    arquivo_sa = Path(sa_path)
    if not arquivo_sa.is_absolute():
        arquivo_sa = (PASTA_BACK / arquivo_sa).resolve()

    if not arquivo_sa.exists():
        return False, f"Falha de acesso: arquivo de service account nao encontrado ({arquivo_sa.name})"

    if test_mode:
        return True, "Google Sheets: validacao simulada (SIMULADO / NAO VERIFICADO)"

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            str(arquivo_sa),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        client = gspread.authorize(creds)
        planilha = client.open_by_key(sheet_id)
        return True, f"Google Sheets conectado em modo leitura (titulo: '{planilha.title}')"
    except Exception as e:  # noqa: BLE001
        return False, f"Falha de autenticacao/acesso ao Google Sheets: {type(e).__name__}"


def checar_telegram_externo(test_mode: bool = False) -> tuple[bool, str]:
    # valida conectividade do bot do telegram via getme sem enviar mensagens
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not token.strip():
        return False, "Configuracao ausente: TELEGRAM_BOT_TOKEN nao configurado no .env"

    if test_mode:
        return True, "Telegram Bot API: validacao simulada (SIMULADO / NAO VERIFICADO)"

    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        if resp.status_code == 200:
            nome_bot = resp.json().get("result", {}).get("username", "bot")
            return True, f"Telegram Bot conectado (@{nome_bot})"
        return False, f"Falha de acesso: Telegram retornou status HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, f"Falha de conexao com a API do Telegram: {type(e).__name__}"


def run_doctor(
    test_mode: bool = False,
    custom_db_path: Path | None = None,
    custom_cookie_path: Path | None = None,
) -> dict:
    print("=" * 65)
    print("DOCTOR BYTZ - Diagnostico do Ambiente")
    print("=" * 65)

    # executa e consolida os cheques locais da fundacao
    print("\n[MODULOS LOCAIS - FUNDACAO S0]")
    cheques_locais = [
        ("Python", checar_python()),
        ("Espaco em Disco", checar_disco()),
        ("Banco SQLite", checar_banco(custom_db_path)),
        ("Cookie ML (Local)", checar_cookie_local(custom_cookie_path)),
        ("Orcamento IA", checar_orcamento_ia_local()),
    ]

    fundacao_saudavel = True
    for titulo, (sucesso, detalhe) in cheques_locais:
        if titulo == "Orcamento IA":
            simbolo = "[ADIADO POR DECISAO DO DONO]"
        else:
            simbolo = "[OK]" if sucesso else "[ERRO]"
        print(f"  {simbolo} {titulo}: {detalhe}")
        if not sucesso and titulo != "Orcamento IA":
            fundacao_saudavel = False

    # avalia a conectividade dos servicos externos
    print("\n[INTEGRACOES EXTERNAS]")
    cheques_externos = [
        ("Google Sheets", checar_sheets_externo(test_mode=test_mode)),
        ("Telegram Bot", checar_telegram_externo(test_mode=test_mode)),
    ]

    integracoes_prontas = True
    pendencias_externas = []
    for titulo, (sucesso, detalhe) in cheques_externos:
        if test_mode and sucesso:
            simbolo = "[SIMULADO / NAO VERIFICADO]"
            integracoes_prontas = False
        elif sucesso:
            simbolo = "[OK]"
        else:
            simbolo = "[PENDENTE]"
            integracoes_prontas = False
            pendencias_externas.append(titulo)
        print(f"  {simbolo} {titulo}: {detalhe}")

    # exibe o resumo executivo com status e pendencias
    print("\n" + "=" * 65)
    print("RESUMO EXECUTIVO:")
    if fundacao_saudavel:
        print("  * Fundacao local (S0): SAUDAVEL (pronta para execucao)")
    else:
        print("  * Fundacao local (S0): COM FALHAS CRITICAS (corrija antes de prosseguir)")

    if integracoes_prontas and not test_mode:
        print("  * Integracoes externas: TODAS OPERACIONAIS")
    else:
        qtd = len(pendencias_externas)
        detalhes_pend = (
            ", ".join(pendencias_externas) if pendencias_externas else "validacao em modo simulado"
        )
        print(f"  * Integracoes externas: {qtd} pendencia(s) identificada(s) ({detalhes_pend})")
        print("    -> Nota: normal durante o desenvolvimento S0; requer configuracao antes do go-live.")
    print("=" * 65)

    return {
        "fundacao_saudavel": fundacao_saudavel,
        "integracoes_prontas": integracoes_prontas,
        "pendencias_externas": pendencias_externas,
    }


if __name__ == "__main__":
    load_env_if_needed()
    is_test = os.environ.get("TEST_MODE") in ("1", "true", "True")
    resultado = run_doctor(test_mode=is_test)
    sys.exit(0 if resultado["fundacao_saudavel"] else 1)
