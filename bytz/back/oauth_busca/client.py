import base64
import hashlib
import hmac
import json
import re
import secrets
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

from .storage import OAuthError, Vault

AUTH_URL = "https://auth.mercadolivre.com.br/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
SCOPES = {"read", "offline_access"}
ATTEMPT_SECONDS = 300


def challenge(verifier: str) -> str:
    # S256 conforme PKCE, sem padding base64.
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def validate_redirect(uri: str):
    # Aceita somente o endereco estatico HTTPS deste receptor.
    parts = urlsplit(uri)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.netloc != parts.hostname
        or parts.path != "/oauth/callback"
        or parts.query
        or parts.fragment
        or not parts.hostname.endswith(".workers.dev")
        or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", parts.hostname)
    ):
        raise OAuthError("Configure a URL HTTPS real e exata do receptor workers.dev.")


@dataclass(frozen=True, repr=False)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    user_id: str

    def __post_init__(self):
        # O ID esperado e obrigatorio para nao aceitar outra conta por engano.
        if not self.client_id.isdecimal() or not self.user_id.isdecimal() or not self.client_secret:
            raise OAuthError("Preencha a configuracao exclusiva da conta secundaria.")
        validate_redirect(self.redirect_uri)

    def binding(self) -> dict:
        return {"client_id": self.client_id, "redirect_uri": self.redirect_uri, "user_id": self.user_id}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # O POST com segredo nao segue redirecionamentos, nem para outro host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def token_request(payload: dict) -> dict:
    # Uma unica chamada TLS, sem proxy herdado, sem debug e sem retry.
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context)
    )
    request = urllib.request.Request(
        TOKEN_URL,
        data=urlencode(payload).encode("ascii"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "BYTZ-OAuth-Busca/1.0",
        },
    )
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise OAuthError("Resposta OAuth invalida; operacao interrompida.")
            result = json.loads(raw)
            if not isinstance(result, dict) or "error" in result:
                raise OAuthError("Resposta OAuth invalida; operacao interrompida.")
            return result
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (403, 429):
            raise OAuthError("Acesso bloqueado ou limitado; pare e avise o responsavel.") from None
        raise OAuthError(
            "Troca OAuth recusada; revise a autorizacao antes de nova tentativa manual."
        ) from None
    except (OSError, ValueError):
        raise OAuthError("Falha de transporte OAuth; resultado incerto, sem repeticao automatica.") from None


class Client:
    def __init__(self, config: Config, vault: Vault | None = None, *, post=token_request, clock=time.time):
        self.config = config
        self.vault = vault or Vault()
        self.post = post
        self.clock = clock

    def _read(self) -> dict:
        # Um cofre de outro aplicativo, URI ou usuario nao e reaproveitado.
        value = self.vault.read()
        if value and value.get("binding") != self.config.binding():
            raise OAuthError("O cofre pertence a outra configuracao; pare e revise os IDs e a URI.")
        return value or {"binding": self.config.binding(), "status": "new"}

    def begin(self) -> str:
        # Uma nova tentativa invalida a anterior, sem alterar cookies ou o app principal.
        with self.vault.locked():
            value = self._read()
            if value["status"] == "ready":
                raise OAuthError("A conta secundaria ja esta autorizada; use a renovacao.")
            verifier = secrets.token_urlsafe(64)
            state = secrets.token_urlsafe(32)
            value.update(
                status="waiting",
                tokens=None,
                attempt={
                    "state": state,
                    "verifier": verifier,
                    "expires_at": self.clock() + ATTEMPT_SECONDS,
                },
            )
            self.vault.write(value)
            return (
                AUTH_URL
                + "?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": self.config.client_id,
                        "redirect_uri": self.config.redirect_uri,
                        "scope": "read offline_access",
                        "state": state,
                        "code_challenge": challenge(verifier),
                        "code_challenge_method": "S256",
                    }
                )
            )

    def _tokens(self, response: dict, old_refresh: str | None = None) -> dict:
        # Rejeita identidade divergente, permissao extra e refresh antigo.
        scopes = response.get("scope")
        lifetime = response.get("expires_in")
        access, refresh = response.get("access_token"), response.get("refresh_token")
        if (
            str(response.get("user_id")) != self.config.user_id
            or not isinstance(response.get("token_type"), str)
            or response["token_type"].lower() != "bearer"
            or not isinstance(scopes, str)
            or not set(scopes.split()) <= SCOPES
            or "read" not in scopes.split()
            or not isinstance(lifetime, int)
            or isinstance(lifetime, bool)
            or not 60 < lifetime <= 86400
            or not isinstance(access, str)
            or not access
            or len(access) > 8192
            or not isinstance(refresh, str)
            or not refresh
            or len(refresh) > 8192
            or refresh == old_refresh
        ):
            raise OAuthError("Resposta OAuth com conta, permissao ou credencial inesperada; pare e revise.")
        return {
            "access_token": access,
            "refresh_token": refresh,
            "scope": scopes,
            "expires_at": self.clock() + lifetime - 60,
        }

    def complete(self, callback: str):
        # A URL inteira chega somente pela entrada local oculta, nunca por argumento do terminal.
        with self.vault.locked():
            value = self._read()
            attempt = value.get("attempt")
            if value.get("status") != "waiting" or not attempt:
                raise OAuthError("Tentativa ausente ou ja utilizada.")
            if self.clock() >= attempt["expires_at"]:
                value.update(status="expired", attempt=None)
                self.vault.write(value)
                raise OAuthError("Tentativa expirada; inicie outra somente quando estiver pronto.")
            parts = urlsplit(callback)
            if (
                len(callback) > 8192
                or parts.fragment
                or callback.split("?", 1)[0] != self.config.redirect_uri
            ):
                raise OAuthError("URI de retorno divergente.")
            try:
                pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=8)
            except ValueError:
                raise OAuthError("Retorno OAuth invalido.") from None
            params = dict(pairs)
            if (
                len(params) != len(pairs)
                or set(params) - {"state", "code", "error", "error_description", "error_uri"}
                or not hmac.compare_digest(params.get("state", "").encode(), attempt["state"].encode())
            ):
                raise OAuthError("Estado ausente ou incorreto, ou retorno invalido.")
            # Consome antes de trocar, inclusive se houver negacao, timeout ou queda do processo.
            value.update(status="exchange_pending", attempt=None, tokens=None)
            self.vault.write(value)
            if "error" in params:
                raise OAuthError("Autorizacao recusada; nenhuma troca foi realizada.")
            code = params.get("code", "")
            if not re.fullmatch(r"[A-Za-z0-9._~-]{1,2048}", code):
                raise OAuthError("Codigo de autorizacao ausente ou invalido.")
            digest = hashlib.sha256(code.encode()).hexdigest()
            if digest in value.get("used_codes", []):
                raise OAuthError("Codigo de autorizacao ja utilizado.")
            value["used_codes"] = (value.get("used_codes", []) + [digest])[-100:]
            self.vault.write(value)
            response = self.post(
                {
                    "grant_type": "authorization_code",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "redirect_uri": self.config.redirect_uri,
                    "code": code,
                    "code_verifier": attempt["verifier"],
                }
            )
            value.update(status="ready", tokens=self._tokens(response))
            self.vault.write(value)

    def access_token(self) -> str:
        # Le o refresh mais recente sob a mesma trava; concorrentes reutilizam o token novo.
        with self.vault.locked():
            value = self._read()
            tokens = value.get("tokens")
            if value.get("status") != "ready" or not tokens:
                raise OAuthError("Credencial indisponivel; revise a falha antes de autorizar manualmente.")
            if self.clock() < tokens["expires_at"]:
                return tokens["access_token"]
            # Remove a copia utilizavel antes da rede; falha incerta nunca repete o refresh antigo.
            refresh = tokens["refresh_token"]
            value.update(status="refresh_pending", tokens=None)
            self.vault.write(value)
            response = self.post(
                {
                    "grant_type": "refresh_token",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": refresh,
                }
            )
            value.update(status="ready", tokens=self._tokens(response, refresh))
            self.vault.write(value)
            return value["tokens"]["access_token"]
