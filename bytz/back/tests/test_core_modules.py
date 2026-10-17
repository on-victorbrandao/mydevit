# testes unitarios isolados de sanitizacao, alertas, logs, banco e diagnostico
import io
import json
import logging
import os
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from core.alerts import send_alert
from core.db import init_db
from core.logger import (
    JsonLinesFormatter,
    SanitizedConsoleFormatter,
    get_logger,
    sanitize_text,
)
from scripts.doctor import (
    checar_banco,
    checar_cookie_local,
    checar_orcamento_ia_local,
    run_doctor,
)

PASTA_DATA_TESTS = PASTA_BACK / "data" / "tests"
PASTA_LOGS_TESTS = PASTA_DATA_TESTS / "logs"


def _fechar_handlers_logger(logger: logging.Logger) -> None:
    # fecha e remove todos os handlers de um logger para liberar arquivos no windows
    for handler in list(logger.handlers):
        try:
            handler.flush()
            handler.close()
        finally:
            logger.removeHandler(handler)


# 1. testes de sanitizacao de logs e alertas


def test_sanitizacao_atribuicoes_e_credenciais():
    # valida que atribuicoes simples de segredos sao mascaradas
    texto_atribuicao = (
        'senha = "minha_senha_secreta_123"; token: "token_ficticio_abc"; api_key = "chave_secreta_999"'
    )
    limpo_atrib = sanitize_text(texto_atribuicao)
    assert "minha_senha_secreta_123" not in limpo_atrib
    assert "token_ficticio_abc" not in limpo_atrib
    assert "chave_secreta_999" not in limpo_atrib
    assert "<MASCARADO>" in limpo_atrib

    # valida que credenciais embutidas em url sao mascaradas
    url_com_credenciais = "https://admin:senha_super_secreta@meudominio.com/api"
    limpo_url = sanitize_text(url_com_credenciais)
    assert "senha_super_secreta" not in limpo_url
    assert "<SENHA_MASCARADA>" in limpo_url

    # valida que o bot token do telegram e mascarado na url
    url_telegram = "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567/sendMessage"
    limpo_tg = sanitize_text(url_telegram)
    assert "123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567" not in limpo_tg
    assert "bot<TOKEN_MASCARADO>" in limpo_tg

    # valida que cabecalho de cookie e cookies individuais sao mascarados
    header_cookie = "Cookie: _csrf=csrf_secreto_123; ssid=sessao_secreta_456; usuario=teste"
    limpo_cookie = sanitize_text(header_cookie)
    assert "csrf_secreto_123" not in limpo_cookie
    assert "sessao_secreta_456" not in limpo_cookie
    assert "<COOKIE_MASCARADO>" in limpo_cookie

    # valida que query params sensiveis em urls sao mascarados
    url_params = "https://mercadolivre.com.br/item?tag=minha_tag_afiliado&token=xyz789"
    limpo_params = sanitize_text(url_params)
    assert "minha_tag_afiliado" not in limpo_params
    assert "xyz789" not in limpo_params
    assert "tag=<MASCARADO>" in limpo_params
    assert "token=<MASCARADO>" in limpo_params


def test_logger_sanitiza_arquivo_e_console():
    # valida que logs em arquivo json lines e no console sao sanitizados e limpa handlers ao final
    PASTA_LOGS_TESTS.mkdir(parents=True, exist_ok=True)
    arquivo_log = PASTA_LOGS_TESTS / "bytz.log"
    if arquivo_log.exists():
        arquivo_log.unlink()

    nome_logger = "test_logger_isolado"
    logger_ref = logging.getLogger(nome_logger)
    handlers_originais = list(logger_ref.handlers)
    nivel_original = logger_ref.level
    propagate_original = logger_ref.propagate
    _fechar_handlers_logger(logger_ref)

    buffer_console = io.StringIO()
    token_fake = "987654321:XYZabc1234567890qwertyuiopasdfghjkl"
    senha_fake = "senha_extremamente_secreta"
    cookie_fake = "ssid_secreto_console_999"

    try:
        with patch.object(sys, "stderr", buffer_console):
            logger = get_logger(nome_logger, custom_logs_dir=PASTA_LOGS_TESTS)

        assert any(isinstance(h.formatter, JsonLinesFormatter) for h in logger.handlers)
        assert any(isinstance(h.formatter, SanitizedConsoleFormatter) for h in logger.handlers)

        logger.info(
            f"Tentativa com bot{token_fake}, url https://usr:{senha_fake}@host.com e ssid={cookie_fake}",
            extra={"detalhes": {"token_acesso": token_fake, "publico": "ok"}},
        )

        for h in logger.handlers:
            h.flush()

        with open(arquivo_log, "r", encoding="utf-8") as f:
            conteudo_arquivo = f.read()

        saida_console = buffer_console.getvalue()

        assert conteudo_arquivo.strip() != ""
        assert saida_console.strip() != ""

        # garante que nenhum segredo ficticio aparece no arquivo nem no console
        for destino in (conteudo_arquivo, saida_console):
            assert token_fake not in destino
            assert senha_fake not in destino
            assert cookie_fake not in destino
            assert "<TOKEN_MASCARADO>" in destino
            assert "<SENHA_MASCARADA>" in destino
            assert "<MASCARADO>" in destino

        assert f"[INFO] {nome_logger}:" in saida_console
    finally:
        _fechar_handlers_logger(logger_ref)
        logger_ref.setLevel(nivel_original)
        logger_ref.propagate = propagate_original
        for h in handlers_originais:
            logger_ref.addHandler(h)
        if arquivo_log.exists():
            arquivo_log.unlink()


def test_alerts_sanitiza_payload_e_trata_excecao_de_rede():
    # valida envio simulado de alertas com TEST_MODE=0 fixo e tratamento seguro de excecoes de rede
    PASTA_LOGS_TESTS.mkdir(parents=True, exist_ok=True)
    arquivo_log = PASTA_LOGS_TESTS / "bytz.log"
    token_ficticio = "555555555:SecretTelegramTokenForTesting12345"
    cookie_ficticio = "ssid=fake_session_12345"

    try:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": token_ficticio,
                "TELEGRAM_ADMIN_CHAT_ID": "112233",
                "TEST_MODE": "0",
            },
        ):
            with patch("requests.post") as mock_post:
                mock_post.return_value = MagicMock(status_code=200)

                send_alert(
                    f"Alerta com bot{token_ficticio}",
                    nivel="critico",
                    detalhes={"cookie_header": cookie_ficticio},
                    dry_run=False,
                    custom_logs_dir=PASTA_LOGS_TESTS,
                )

                assert mock_post.called
                payload_enviado = mock_post.call_args.kwargs["json"]["text"]
                assert token_ficticio not in payload_enviado
                assert "fake_session_12345" not in payload_enviado

            # valida que excecao de rede nao reproduz a url bruta com o token
            with (
                patch(
                    "requests.post",
                    side_effect=Exception(
                        f"Failed to connect to https://api.telegram.org/bot{token_ficticio}"
                    ),
                ),
                patch("core.alerts.get_logger") as mock_get_logger,
            ):
                mock_logger = MagicMock()
                mock_get_logger.return_value = mock_logger
                sucesso = send_alert("Teste de falha", dry_run=False, custom_logs_dir=PASTA_LOGS_TESTS)
                assert sucesso is False
                assert mock_logger.error.called
                msg_erro = mock_logger.error.call_args[0][0]
                assert token_ficticio not in msg_erro
                assert "tipo=Exception" in msg_erro
    finally:
        _fechar_handlers_logger(logging.getLogger("alerts"))
        if arquivo_log.exists():
            arquivo_log.unlink()


# 2. testes de diagnostico do banco (separacao entre inicializacao e diagnostico)


def test_checar_banco_ausente_nao_cria_arquivo():
    # diagnostico de banco ausente deve retornar false e nunca criar arquivo no disco
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco_ausente = PASTA_DATA_TESTS / "banco_inexistente_para_teste.db"
    if banco_ausente.exists():
        banco_ausente.unlink()

    try:
        ok, msg = checar_banco(banco_ausente)
        assert ok is False
        assert "Banco ausente" in msg
        assert not banco_ausente.exists(), "O diagnostico de banco ausente criou o arquivo indevidamente!"
    finally:
        if banco_ausente.exists():
            banco_ausente.unlink()


def test_checar_banco_incompleto_aponta_faltantes():
    # diagnostico de banco incompleto cria sua propria pasta de teste e aponta itens faltantes
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco_incompleto = PASTA_DATA_TESTS / "banco_incompleto.db"
    if banco_incompleto.exists():
        banco_incompleto.unlink()

    try:
        conn = sqlite3.connect(banco_incompleto)
        try:
            conn.execute("CREATE TABLE outra_coisa (id INT);")
        finally:
            conn.close()

        ok, msg = checar_banco(banco_incompleto)
        assert ok is False
        assert "Banco incompleto" in msg
        assert "tabelas ausentes" in msg
    finally:
        if banco_incompleto.exists():
            banco_incompleto.unlink()


def test_checar_banco_correto():
    # banco inicializado com init_db deve passar com sucesso no diagnostico somente leitura
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco_completo = PASTA_DATA_TESTS / "banco_completo.db"
    if banco_completo.exists():
        banco_completo.unlink()

    try:
        init_db(banco_completo)

        ok, msg = checar_banco(banco_completo)
        assert ok is True
        assert "Banco SQLite integro" in msg
        assert "8 tabelas e 2 indices" in msg
    finally:
        if banco_completo.exists():
            banco_completo.unlink()


# 3. testes do doctor e tratamento seguro de datas de cookie


def test_cookie_data_invalida_nao_imprime_conteudo_bruto():
    # data invalida no cookie deve gerar erro generico sem imprimir conteudo bruto
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    caminho_cookie = PASTA_DATA_TESTS / "test_cookie_data_invalida.json"
    dado_bruto = "VALOR_SECRETO_OU_SUJO_QUE_NAO_DEVE_APARECER"

    try:
        with open(caminho_cookie, "w", encoding="utf-8") as f:
            json.dump({"cookie": "_csrf=c; ssid=s; nsa_rotok=r", "atualizacao": dado_bruto}, f)

        ok, msg = checar_cookie_local(custom_cookie_path=caminho_cookie)
        assert ok is False
        assert dado_bruto not in msg
        assert "formato de data ISO invalido" in msg
    finally:
        if caminho_cookie.exists():
            caminho_cookie.unlink()


def test_doctor_resumo_e_diferenciacao_de_status():
    # captura a saida do doctor e valida os rotulos de pendencia, simulacao e orcamento adiado
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    banco_teste = PASTA_DATA_TESTS / "test_doctor_banco.db"
    cookie_teste = PASTA_DATA_TESTS / "test_doctor_cookie.json"
    sa_teste = PASTA_DATA_TESTS / "test_doctor_sa_ficticio.json"

    for arq in (banco_teste, cookie_teste, sa_teste):
        if arq.exists():
            arq.unlink()

    try:
        init_db(banco_teste)

        with open(cookie_teste, "w", encoding="utf-8") as f:
            json.dump(
                {"cookie": "_csrf=c; ssid=s; nsa_rotok=r", "atualizacao": "2026-10-14T03:00:00-03:00"},
                f,
            )

        with open(sa_teste, "w", encoding="utf-8") as f:
            json.dump({"type": "service_account", "project_id": "projeto-ficticio"}, f)

        # 1. cenario com integracoes pendentes
        buffer_pendente = io.StringIO()
        with (
            patch.dict(os.environ, {"SHEET_ID": "", "TELEGRAM_BOT_TOKEN": ""}),
            redirect_stdout(buffer_pendente),
        ):
            res_pend = run_doctor(
                test_mode=False, custom_db_path=banco_teste, custom_cookie_path=cookie_teste
            )

        saida_pendente = buffer_pendente.getvalue()
        assert res_pend["fundacao_saudavel"] is True
        assert res_pend["integracoes_prontas"] is False
        assert "Google Sheets" in res_pend["pendencias_externas"]
        assert "Telegram Bot" in res_pend["pendencias_externas"]
        assert "[ADIADO POR DECISAO DO DONO] Orcamento IA:" in saida_pendente
        assert "[PENDENTE] Google Sheets:" in saida_pendente
        assert "[PENDENTE] Telegram Bot:" in saida_pendente

        # 2. cenario em modo simulado (nao pode declarar integracoes_prontas como True)
        buffer_sim = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "SHEET_ID": "fake_id",
                    "GOOGLE_SERVICE_ACCOUNT_FILE": "fake.json",
                    "TELEGRAM_BOT_TOKEN": "123:fake",
                },
            ),
            redirect_stdout(buffer_sim),
        ):
            res_sim = run_doctor(test_mode=True, custom_db_path=banco_teste, custom_cookie_path=cookie_teste)

        saida_sim = buffer_sim.getvalue()
        assert res_sim["fundacao_saudavel"] is True
        assert res_sim["integracoes_prontas"] is False
        assert "[ADIADO POR DECISAO DO DONO] Orcamento IA:" in saida_sim
        assert "[SIMULADO / NAO VERIFICADO] Telegram Bot:" in saida_sim
        assert "TODAS OPERACIONAIS" not in saida_sim

        # valida tambem rotulo simulado de Google Sheets sem declarar TODAS OPERACIONAIS
        buffer_sim_sheets = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "SHEET_ID": "fake_id",
                    "GOOGLE_SERVICE_ACCOUNT_FILE": str(sa_teste),
                    "TELEGRAM_BOT_TOKEN": "123:fake",
                },
            ),
            redirect_stdout(buffer_sim_sheets),
        ):
            run_doctor(test_mode=True, custom_db_path=banco_teste, custom_cookie_path=cookie_teste)

        saida_sim_sheets = buffer_sim_sheets.getvalue()
        assert "[SIMULADO / NAO VERIFICADO] Google Sheets:" in saida_sim_sheets
        assert "validacao em modo simulado" in saida_sim_sheets
        assert "TODAS OPERACIONAIS" not in saida_sim_sheets

        # 3. confere funcao de orcamento de ia isoladamente
        ok_ia, msg_ia = checar_orcamento_ia_local()
        assert ok_ia is True
        assert "adiado por decisao do dono" in msg_ia.lower()
    finally:
        for arq in (banco_teste, cookie_teste, sa_teste):
            if arq.exists():
                arq.unlink()


# 4. testes de comprovacao de isolamento (sem arquivos reais e sem rede)


def test_nenhum_arquivo_real_e_aberto_nos_testes():
    # comprova e bloqueia qualquer tentativa de abrir .env, cookie ou banco real via open, Path.open e sqlite3
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    PASTA_LOGS_TESTS.mkdir(parents=True, exist_ok=True)
    banco_teste = PASTA_DATA_TESTS / "test_isolamento.db"
    cookie_teste = PASTA_DATA_TESTS / "test_isolamento_cookie.json"
    arquivo_log = PASTA_LOGS_TESTS / "bytz.log"

    for arq in (banco_teste, cookie_teste, arquivo_log):
        if arq.exists():
            arq.unlink()

    env_real = (PASTA_BACK / ".env").resolve()
    cookie_real = (PASTA_BACK / "ml_cookies.json").resolve()
    banco_real = (PASTA_BACK / "data" / "bytz.db").resolve()
    caminhos_proibidos = {env_real, cookie_real, banco_real}

    aberturas_reais = []
    real_open = open
    real_path_open = Path.open
    real_sqlite_connect = sqlite3.connect

    def _extrair_caminho_resolvido(alvo) -> Path | None:
        try:
            if isinstance(alvo, str) and alvo.startswith("file:"):
                sem_prefixo = alvo[5:].split("?")[0]
                return Path(sem_prefixo).resolve()
            return Path(alvo).resolve()
        except (TypeError, ValueError, OSError):
            return None

    def _validar_acesso(alvo) -> None:
        caminho = _extrair_caminho_resolvido(alvo)
        if caminho and caminho in caminhos_proibidos:
            aberturas_reais.append(str(caminho))
            raise PermissionError(f"Acesso proibido a arquivo real durante os testes: {caminho}")

    def spy_open(file, *args, **kwargs):
        _validar_acesso(file)
        return real_open(file, *args, **kwargs)

    def spy_path_open(self_path, *args, **kwargs):
        _validar_acesso(self_path)
        return real_path_open(self_path, *args, **kwargs)

    def spy_sqlite_connect(database, *args, **kwargs):
        _validar_acesso(database)
        return real_sqlite_connect(database, *args, **kwargs)

    try:
        init_db(banco_teste)
        with open(cookie_teste, "w", encoding="utf-8") as f:
            json.dump(
                {"cookie": "_csrf=c; ssid=s; nsa_rotok=r", "atualizacao": "2026-10-14T03:00:00-03:00"},
                f,
            )

        with (
            patch("builtins.open", side_effect=spy_open),
            patch.object(Path, "open", autospec=True, side_effect=spy_path_open),
            patch("sqlite3.connect", side_effect=spy_sqlite_connect),
            patch.dict(os.environ, {"TEST_MODE": "1"}, clear=False),
        ):
            res = run_doctor(test_mode=True, custom_db_path=banco_teste, custom_cookie_path=cookie_teste)
            assert res["fundacao_saudavel"] is True
            send_alert("Alerta isolamento", dry_run=True, custom_logs_dir=PASTA_LOGS_TESTS)

            assert aberturas_reais == [], f"Arquivos reais foram acessados: {aberturas_reais}"

            # comprova que a guarda rejeita tentativas de abrir o banco real via sqlite (direto e uri) e arquivos reais
            with pytest.raises(PermissionError):
                sqlite3.connect(banco_real)
            with pytest.raises(PermissionError):
                sqlite3.connect(f"file:{banco_real.as_posix()}?mode=ro", uri=True)
            with pytest.raises(PermissionError), open(env_real, "r", encoding="utf-8"):
                pass
            with pytest.raises(PermissionError), cookie_real.open("r", encoding="utf-8"):
                pass
    finally:
        _fechar_handlers_logger(logging.getLogger("alerts"))
        for arq in (banco_teste, cookie_teste, arquivo_log):
            if arq.exists():
                arq.unlink()


def test_nenhuma_requisicao_externa_feita_no_modo_teste():
    # comprova que nem requests.get nem requests.post sao executados durante o modo de teste
    PASTA_DATA_TESTS.mkdir(parents=True, exist_ok=True)
    PASTA_LOGS_TESTS.mkdir(parents=True, exist_ok=True)
    banco_teste = PASTA_DATA_TESTS / "test_rede_isolamento.db"
    cookie_teste = PASTA_DATA_TESTS / "test_rede_cookie.json"
    arquivo_log = PASTA_LOGS_TESTS / "bytz.log"

    for arq in (banco_teste, cookie_teste, arquivo_log):
        if arq.exists():
            arq.unlink()

    try:
        init_db(banco_teste)

        with open(cookie_teste, "w", encoding="utf-8") as f:
            json.dump(
                {"cookie": "_csrf=c; ssid=s; nsa_rotok=r", "atualizacao": "2026-10-14T03:00:00-03:00"},
                f,
            )

        with patch("requests.get") as mock_get, patch("requests.post") as mock_post:
            # executa doctor em modo simulado
            res = run_doctor(test_mode=True, custom_db_path=banco_teste, custom_cookie_path=cookie_teste)
            assert res["fundacao_saudavel"] is True

            # executa alerta em modo dry_run
            send_alert("Alerta simulado", dry_run=True, custom_logs_dir=PASTA_LOGS_TESTS)

            # comprova ausencia total de chamadas de rede
            assert mock_get.call_count == 0, f"requests.get foi chamado {mock_get.call_count} vezes!"
            assert mock_post.call_count == 0, f"requests.post foi chamado {mock_post.call_count} vezes!"
    finally:
        _fechar_handlers_logger(logging.getLogger("alerts"))
        for arq in (banco_teste, cookie_teste, arquivo_log):
            if arq.exists():
                arq.unlink()


if __name__ == "__main__":
    print("Iniciando testes unitarios isolados...")
    test_sanitizacao_atribuicoes_e_credenciais()
    print("  * test_sanitizacao_atribuicoes_e_credenciais: OK")
    test_logger_sanitiza_arquivo_e_console()
    print("  * test_logger_sanitiza_arquivo_e_console: OK")
    test_alerts_sanitiza_payload_e_trata_excecao_de_rede()
    print("  * test_alerts_sanitiza_payload_e_trata_excecao_de_rede: OK")
    test_checar_banco_ausente_nao_cria_arquivo()
    print("  * test_checar_banco_ausente_nao_cria_arquivo: OK")
    test_checar_banco_incompleto_aponta_faltantes()
    print("  * test_checar_banco_incompleto_aponta_faltantes: OK")
    test_checar_banco_correto()
    print("  * test_checar_banco_correto: OK")
    test_cookie_data_invalida_nao_imprime_conteudo_bruto()
    print("  * test_cookie_data_invalida_nao_imprime_conteudo_bruto: OK")
    test_doctor_resumo_e_diferenciacao_de_status()
    print("  * test_doctor_resumo_e_diferenciacao_de_status: OK")
    test_nenhum_arquivo_real_e_aberto_nos_testes()
    print("  * test_nenhum_arquivo_real_e_aberto_nos_testes: OK")
    test_nenhuma_requisicao_externa_feita_no_modo_teste()
    print("  * test_nenhuma_requisicao_externa_feita_no_modo_teste: OK")
    print("\nTODOS OS TESTES UNITARIOS PASSARAM COM 100% DE SUCESSO!")
