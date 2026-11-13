import hashlib
import importlib
import json
import logging
import os
import socket
import subprocess
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from oauth_busca.client import AUTH_URL, TOKEN_URL, Client, Config, NoRedirect, challenge, token_request
from oauth_busca.storage import OAuthError, Vault, protect

URI = "https://oauth.exemplo.workers.dev/oauth/callback"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # Nem rede nem .env reais podem participar destes testes.
    def forbidden(*args, **kwargs):
        raise AssertionError("Acesso externo ou .env proibido no teste.")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("dotenv.load_dotenv", forbidden)


@pytest.fixture
def setup(tmp_path):
    # IDs, segredos e respostas sao inteiramente sinteticos.
    config = Config("123456", "segredo-ficticio", URI, "222222")
    now = [1000.0]
    calls = []

    def post(payload):
        calls.append(payload.copy())
        return {
            "access_token": f"acesso-ficticio-{len(calls)}",
            "refresh_token": f"refresh-ficticio-{len(calls)}",
            "expires_in": 21600,
            "token_type": "bearer",
            "user_id": 222222,
            "scope": "read offline_access",
        }

    client = Client(config, Vault(tmp_path / "oauth-sintetico"), post=post, clock=lambda: now[0])
    return client, now, calls


def callback(client, code="CODIGO-FICTICIO", **extra):
    # Usa apenas a tentativa sintetica criada pelo proprio teste.
    state = client.vault.read()["attempt"]["state"]
    return URI + "?" + urlencode({"state": state, "code": code, **extra})


def authorize(client):
    client.begin()
    client.complete(callback(client))


def test_pkce_rfc7636_e_tentativas_distintas(setup):
    client, _, _ = setup
    assert (
        challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
    first = parse_qs(urlsplit(client.begin()).query)
    attempt = client.vault.read()["attempt"]
    assert 43 <= len(attempt["verifier"]) <= 128
    assert len(attempt["state"]) == 43
    assert first["code_challenge"] == [challenge(attempt["verifier"])]
    assert first["code_challenge_method"] == ["S256"]
    assert first["scope"] == ["read offline_access"]
    assert first["redirect_uri"] == [URI]
    second = parse_qs(urlsplit(client.begin()).query)
    assert second["state"] != first["state"]
    assert second["code_challenge"] != first["code_challenge"]
    assert client.config.client_secret not in client.begin()


@pytest.mark.parametrize(
    "suffix",
    [
        "?code=x",
        "?code=x&state=errado",
        "?code=x&state=%C3%A9",
        "?code=x&state=",
    ],
)
def test_estado_ausente_ou_divergente(setup, suffix):
    client, _, calls = setup
    client.begin()
    with pytest.raises(OAuthError, match="Estado"):
        client.complete(URI + suffix)
    assert not calls
    assert client.vault.read()["status"] == "waiting"


@pytest.mark.parametrize(
    "change",
    [
        lambda uri: uri.replace("https:", "http:"),
        lambda uri: uri.replace("oauth.", "outro."),
        lambda uri: uri.replace("/oauth/callback", "/oauth/callback/"),
        lambda uri: uri + "#fragmento",
        lambda uri: uri.replace(".dev/", ".dev:443/"),
    ],
)
def test_uri_divergente(setup, change):
    client, _, calls = setup
    client.begin()
    with pytest.raises(OAuthError, match="URI"):
        client.complete(change(callback(client)))
    assert not calls


@pytest.mark.parametrize("code", ["", "<script>", "x" * 2049])
def test_codigo_ausente_ou_invalido_consumido(setup, code):
    client, _, calls = setup
    client.begin()
    with pytest.raises(OAuthError, match="Codigo"):
        client.complete(callback(client, code))
    assert not calls
    assert client.vault.read()["attempt"] is None


def test_codigo_realmente_ausente(setup):
    client, _, calls = setup
    client.begin()
    state = client.vault.read()["attempt"]["state"]
    with pytest.raises(OAuthError, match="Codigo"):
        client.complete(URI + "?" + urlencode({"state": state}))
    assert not calls


def test_codigo_repetido_e_tentativa_expirada(setup):
    client, now, calls = setup
    client.begin()
    url = callback(client)
    now[0] += 300
    with pytest.raises(OAuthError, match="expirada"):
        client.complete(url)
    assert not calls
    client.begin()
    client.complete(callback(client))
    with pytest.raises(OAuthError, match="utilizada"):
        client.complete(url)
    # Simula autorizacao manual posterior sem permitir reuso do mesmo codigo.
    value = client.vault.read()
    value.update(status="refresh_pending", tokens=None)
    client.vault.write(value)
    client.begin()
    with pytest.raises(OAuthError, match="ja utilizado"):
        client.complete(callback(client))
    assert len(calls) == 1
    assert value["used_codes"] == [hashlib.sha256(b"CODIGO-FICTICIO").hexdigest()]


def test_negacao_e_parametros_duplicados(setup):
    client, _, calls = setup
    client.begin()
    url = callback(client)
    for suffix in ["&code=outro", "&state=outro", "&next=https://fora.invalid", "&a=1" * 9]:
        with pytest.raises(OAuthError):
            client.complete(url + suffix)
    state = client.vault.read()["attempt"]["state"]
    denied = (
        URI
        + "?"
        + urlencode({"state": state, "error": "access_denied", "error_description": "segredo-ficticio"})
    )
    with pytest.raises(OAuthError, match="recusada") as error:
        client.complete(denied)
    assert "segredo-ficticio" not in str(error.value)
    assert not calls


def test_troca_e_rotacao_sequencial(setup):
    client, now, calls = setup
    client.begin()
    verifier = client.vault.read()["attempt"]["verifier"]
    client.complete(callback(client))
    assert calls[0]["code_verifier"] == verifier
    assert calls[0]["redirect_uri"] == URI
    assert calls[0]["grant_type"] == "authorization_code"
    assert client.access_token() == "acesso-ficticio-1"
    assert len(calls) == 1
    for expected in [2, 3]:
        now[0] += 21600
        assert client.access_token() == f"acesso-ficticio-{expected}"
        assert calls[-1]["refresh_token"] == f"refresh-ficticio-{expected - 1}"
        assert client.access_token() == f"acesso-ficticio-{expected}"
        assert len(calls) == expected
    with pytest.raises(OAuthError, match="ja esta autorizada"):
        client.begin()


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", 111111),
        ("scope", "read write offline_access"),
        ("scope", "offline_access"),
        ("refresh_token", ""),
        ("access_token", ""),
        ("expires_in", True),
        ("expires_in", -1),
        ("token_type", "basic"),
        ("token_type", 1),
    ],
)
def test_rejeita_conta_ou_resposta_invalida(setup, field, value):
    client, _, calls = setup
    post = client.post
    client.post = lambda payload: {**post(payload), field: value}
    client.begin()
    with pytest.raises(OAuthError, match="inesperada"):
        client.complete(callback(client))
    assert len(calls) == 1
    assert client.vault.read()["tokens"] is None


@pytest.mark.parametrize("renew", [False, True])
def test_falha_na_rede_ou_revogacao_sem_repetir(setup, renew):
    client, now, calls = setup
    if renew:
        authorize(client)
        now[0] += 21600
    else:
        client.begin()
    url = None if renew else callback(client)
    attempts = []

    def fail(payload):
        attempts.append(payload)
        raise OAuthError("Operacao recusada ou incerta; pare.")

    client.post = fail
    operation = client.access_token if renew else lambda: client.complete(url)
    with pytest.raises(OAuthError):
        operation()
    with pytest.raises(OAuthError):
        operation()
    assert len(attempts) == 1
    assert client.vault.read()["tokens"] is None
    assert len(calls) == int(renew)


def test_refresh_precisa_rotacionar(setup):
    client, now, _ = setup
    authorize(client)
    now[0] += 21600
    post = client.post
    client.post = lambda payload: {**post(payload), "refresh_token": "refresh-ficticio-1"}
    with pytest.raises(OAuthError, match="inesperada"):
        client.access_token()
    assert client.vault.read()["status"] == "refresh_pending"


def test_renovacao_concorrente_faz_um_post_e_publica_token_novo(setup):
    # Um concorrente para na trava enquanto o primeiro aguarda a resposta sintetica.
    client, now, calls = setup
    authorize(client)
    now[0] += 21600
    entered, release = Event(), Event()
    post = client.post

    def delayed(payload):
        entered.set()
        assert release.wait(10)
        return post(payload)

    client.post = delayed
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(client.access_token)
        try:
            assert entered.wait(10)
            with pytest.raises(OAuthError, match="Outra execucao"):
                client.access_token()
        finally:
            release.set()
        assert first.result(timeout=10) == "acesso-ficticio-2"
    assert client.access_token() == "acesso-ficticio-2"
    assert len(calls) == 2


@pytest.mark.parametrize("renew", [False, True])
def test_invalid_grant_real_simulado_consume_codigo_ou_refresh(setup, monkeypatch, renew):
    # HTTP 400 simula codigo expirado ou grant revogado sem expor o corpo de erro.
    client, now, _ = setup
    if renew:
        authorize(client)
        now[0] += 21600
    else:
        client.begin()
    url = None if renew else callback(client)
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(TOKEN_URL, 400, "invalid_grant-ficticio", {}, None)

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    client.post = token_request
    operation = client.access_token if renew else lambda: client.complete(url)
    with pytest.raises(OAuthError, match="recusada"):
        operation()
    with pytest.raises(OAuthError):
        operation()
    assert len(calls) == 1


def test_isolamento_da_configuracao_e_dos_cookies(setup, tmp_path, monkeypatch):
    client, _, calls = setup
    principal = tmp_path / "principal-sintetica.json"
    principal.write_text('"cookie-ficticio"')
    monkeypatch.setenv("ML_COOKIE_FILE", str(principal))
    monkeypatch.setenv("ML_SEARCH_COOKIE_FILE", str(principal))
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path / "outro"))
    assert Vault().directory.parts[-2:] == ("secrets", "ml-search-oauth")
    authorize(client)
    for config in [Config("654321", "ficticio", URI, "222222"), Config("123456", "ficticio", URI, "111111")]:
        with pytest.raises(OAuthError, match="outra configuracao"):
            Client(config, client.vault).begin()
    assert principal.read_text() == '"cookie-ficticio"'
    assert len(calls) == 1


def test_dpapi_atomica_e_corrupcao(setup, monkeypatch):
    client, _, _ = setup
    authorize(client)
    before = client.vault.path.read_bytes()
    assert b"acesso-ficticio" not in before
    assert b"refresh-ficticio" not in before
    assert protect(protect(b"sintetico"), decrypt=True) == b"sintetico"

    def fail_replace(*args):
        raise OSError("valor-sensivel-ficticio")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OAuthError, match="gravar") as error:
        client.vault.write({"segredo": "novo-ficticio"})
    assert "valor-sensivel" not in str(error.value)
    assert client.vault.path.read_bytes() == before
    assert not list(client.vault.directory.glob("vault-*.tmp"))
    client.vault.path.write_bytes(b"corrompido")
    with pytest.raises(OAuthError, match="protecao"):
        client.vault.read()


def test_falha_ao_persistir_consumo_nao_envia_rede(setup, monkeypatch):
    client, _, calls = setup
    client.begin()
    url = callback(client)
    monkeypatch.setattr(client.vault, "write", lambda _: (_ for _ in ()).throw(OAuthError("Falha local.")))
    with pytest.raises(OAuthError):
        client.complete(url)
    assert not calls


def test_falha_ao_salvar_refresh_novo_nao_repete_antigo(setup, monkeypatch):
    client, now, calls = setup
    authorize(client)
    now[0] += 21600
    write = client.vault.write
    writes = []

    def fail_last(value):
        writes.append(value.copy())
        if len(writes) == 2:
            raise OAuthError("Falha local.")
        write(value)

    monkeypatch.setattr(client.vault, "write", fail_last)
    with pytest.raises(OAuthError):
        client.access_token()
    with pytest.raises(OAuthError):
        client.access_token()
    assert client.vault.read()["status"] == "refresh_pending"
    assert len(calls) == 2


def test_trava_entre_processos_e_liberacao(setup):
    client, _, _ = setup
    source = "from pathlib import Path\nfrom oauth_busca.storage import Vault, OAuthError\nimport sys\ntry:\n with Vault(Path(sys.argv[1])).locked(): pass\nexcept OAuthError:\n sys.exit(7)\n"
    with client.vault.locked():
        process = subprocess.run(
            [sys.executable, "-c", source, str(client.vault.directory)],
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert process.returncode == 7
    with client.vault.locked():
        pass


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_transporte_sem_vazamento_retry_ou_redirect(monkeypatch, caplog, status):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.full_url == TOKEN_URL
            assert timeout == 20
            raise urllib.error.HTTPError(
                TOKEN_URL + "?code=valor-ficticio", status, "segredo-ficticio", {}, None
            )

    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: Opener())
    with caplog.at_level(logging.DEBUG), pytest.raises(OAuthError) as error:
        token_request({"code": "valor-ficticio", "client_secret": "segredo-ficticio"})
    assert len(calls) == 1
    assert "segredo-ficticio" not in str(error.value) + caplog.text
    assert "valor-ficticio" not in str(error.value) + caplog.text
    assert NoRedirect().redirect_request(None, None, 302, "", {}, "https://fora.invalid") is None


def test_transporte_sucesso_simulado_e_limite_da_resposta(monkeypatch):
    class Response:
        status = 200
        raw = json.dumps({"access_token": "ficticio"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == 65537
            return self.raw

    class Opener:
        def open(self, request, timeout):
            assert parse_qs(request.data.decode())["client_secret"] == ["segredo-ficticio"]
            assert "segredo-ficticio" not in request.full_url
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    assert token_request({"client_secret": "segredo-ficticio"}) == {"access_token": "ficticio"}
    for raw in [b"x" * 65537, b"invalido", b"null", b'{"error":"invalid_grant"}']:
        Response.raw = raw
        with pytest.raises(OAuthError):
            token_request({"client_secret": "segredo-ficticio"})


@pytest.mark.parametrize("uri", [URI + "?x=1", URI + "#x", URI + "/", "http://local.invalid/oauth/callback"])
def test_configuracao_exige_https_estatico(uri):
    with pytest.raises(OAuthError):
        Config("123", "ficticio", uri, "222")


def test_cli_nao_imprime_callback_tokens_ou_excecao_bruta(monkeypatch, capsys, setup):
    cli = importlib.import_module("oauth_busca.__main__")
    client, _, _ = setup
    monkeypatch.setattr(cli, "load_dotenv", lambda _: None)
    for key, value in {
        "ML_SEARCH_CLIENT_ID": "123456",
        "ML_SEARCH_CLIENT_SECRET": "segredo-ficticio",
        "ML_SEARCH_REDIRECT_URI": URI,
        "ML_SEARCH_USER_ID": "222222",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "Client", lambda _: client)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)
    assert cli.main(["iniciar"]) == 0
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: callback(client))
    assert cli.main(["concluir"]) == 0
    assert cli.main(["renovar"]) == 0
    output = capsys.readouterr()
    assert "ficticio" not in output.out + output.err
    assert opened[0].startswith(AUTH_URL)
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda _: (_ for _ in ()).throw(ValueError("segredo-ficticio"))
    )
    assert cli.main(["concluir"]) == 1
    assert "segredo-ficticio" not in capsys.readouterr().err
