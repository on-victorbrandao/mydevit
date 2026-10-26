# configuracao compartilhada dos testes: garante path do back e isola chamadas externas
import sys
from pathlib import Path

import pytest

PASTA_BACK = Path(__file__).resolve().parent.parent
if str(PASTA_BACK) not in sys.path:
    sys.path.insert(0, str(PASTA_BACK))


@pytest.fixture(autouse=True)
def bloquear_chave_gemini(monkeypatch):
    # chave vazia ja existente no ambiente faz o load_dotenv nao sobrescrever com o valor real
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("TEST_MODE", "1")
