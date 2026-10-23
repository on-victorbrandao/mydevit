# configuracao compartilhada dos testes: impede chamada real ao gemini mesmo se o .env real for carregado
import pytest


@pytest.fixture(autouse=True)
def bloquear_chave_gemini(monkeypatch):
    # chave vazia ja existente no ambiente faz o load_dotenv nao sobrescrever com o valor real
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("TEST_MODE", "1")
