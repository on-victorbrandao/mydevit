# salva o cookie do ml num json valido, sem precisar escapar nada na mao
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

PASTA_BACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRADA = os.path.join(PASTA_BACK, "ml_cookie_novo.txt")
SAIDA = os.environ.get("ML_COOKIE_FILE") or os.path.join(PASTA_BACK, "ml_cookies.json")
OBRIGATORIOS = ("_csrf", "ssid", "nsa_rotok")


def limpar(texto):
    texto = texto.strip().strip("'\"")
    # tira o prefixo caso tenha copiado o header inteiro
    if texto.lower().startswith("cookie:"):
        texto = texto[len("cookie:"):].strip()
    return " ".join(texto.split())


def main():
    if not os.path.exists(ENTRADA):
        sys.exit(f"nao achei {ENTRADA}. cole o cookie nesse arquivo e rode de novo")

    with open(ENTRADA, encoding="utf-8") as f:
        cookie = limpar(f.read())

    nomes = [p.split("=")[0].strip() for p in cookie.split(";") if "=" in p]
    faltando = [n for n in OBRIGATORIOS if n not in nomes]
    if faltando:
        sys.exit(f"faltam cookies obrigatorios: {faltando}. confira se copiou do request certo")

    agora = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")
    dados = {
        "marketplace": "Mercado Livre",
        "origem": "linkbuilder",
        "atualizacao": agora,
        "cookie": cookie,
    }

    # grava em arquivo temporario e troca no final pra nao corromper o json
    temporario = SAIDA + ".tmp"
    with open(temporario, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(temporario, SAIDA)

    # confere se o arquivo salvo abre como json valido
    with open(SAIDA, encoding="utf-8") as f:
        json.load(f)

    os.remove(ENTRADA)
    print(f"salvo em {SAIDA} com {len(nomes)} cookies. arquivo de entrada apagado")


if __name__ == "__main__":
    main()
