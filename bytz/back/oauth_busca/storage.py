import ctypes
import json
import os
import tempfile
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path


class OAuthError(Exception):
    # As mensagens publicas nunca recebem valores do retorno ou da API.
    pass


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, *, decrypt: bool = False) -> bytes:
    # DPAPI vincula o arquivo ao usuario atual do Windows, sem chave no projeto.
    if os.name != "nt":
        raise OAuthError("Este armazenamento exige Windows com DPAPI.")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OAuthError("Falha na protecao local do Windows.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class Vault:
    # Caminho exclusivo; nenhuma configuracao de cookie ou conta principal e usada.
    def __init__(self, directory: Path | None = None):
        self.directory = directory or Path(__file__).resolve().parents[1] / "secrets" / "ml-search-oauth"
        self.path = self.directory / "vault.dpapi"

    @contextmanager
    def locked(self):
        # O arquivo de trava e permanente; substituir o cofre nao troca a trava.
        if os.name != "nt":
            raise OAuthError("Esta trava exige Windows.")
        import msvcrt

        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "operation.lock").open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if not lock.tell():
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise OAuthError("Outra execucao OAuth esta em andamento.") from None
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def read(self) -> dict:
        # Falha de leitura nunca vira um cofre vazio nem dispara login automatico.
        try:
            if not self.path.exists():
                return {}
            value = json.loads(protect(self.path.read_bytes(), decrypt=True))
            if not isinstance(value, dict):
                raise OAuthError("Formato invalido no cofre OAuth local.")
            return value
        except (OSError, ValueError, UnicodeError):
            raise OAuthError("Nao foi possivel ler o cofre OAuth local.") from None

    def write(self, value: dict):
        # O temporario contem apenas bytes cifrados e fica no mesmo volume.
        encrypted = protect(json.dumps(value).encode("utf-8"))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.directory, prefix="vault-", suffix=".tmp", delete=False
            ) as f:
                temporary = Path(f.name)
                f.write(encrypted)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise OAuthError("Nao foi possivel gravar o cofre OAuth local.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
