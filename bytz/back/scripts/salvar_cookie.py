# salva o cookie do ml num json valido, sem precisar escapar nada na mao
import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

PASTA_BACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRADA_PADRAO = os.path.join(PASTA_BACK, "ml_cookie_novo.txt")
ENTRADA_BUSCA = os.path.join(PASTA_BACK, "ml_cookie_busca_novo.txt")
SAIDA_PADRAO = os.environ.get("ML_COOKIE_FILE") or os.path.join(PASTA_BACK, "ml_cookies.json")
SAIDA_BUSCA = os.environ.get("ML_SEARCH_COOKIE_FILE") or os.path.join(PASTA_BACK, "ml_cookies_busca.json")
OBRIGATORIOS_LINKBUILDER = ("_csrf", "ssid", "nsa_rotok")
OBRIGATORIOS_BUSCA = ("ssid",)


def limpar(texto):
    texto = texto.strip().strip("'\"")
    # tira o prefixo caso tenha copiado o header inteiro
    if texto.lower().startswith("cookie:"):
        texto = texto[len("cookie:") :].strip()
    return " ".join(texto.split())


def salvar_cookie_arquivo(
    modo_busca: bool = False, entrada_custom: str | None = None, saida_custom: str | None = None
):
    # le o txt de entrada, valida os cookies obrigatorios do modo e grava o json de forma atomica
    if entrada_custom:
        entrada = entrada_custom
    elif modo_busca:
        entrada = ENTRADA_BUSCA if os.path.exists(ENTRADA_BUSCA) else ENTRADA_PADRAO
    else:
        if os.path.exists(ENTRADA_PADRAO):
            entrada = ENTRADA_PADRAO
        elif os.path.exists(ENTRADA_BUSCA):
            entrada = ENTRADA_BUSCA
            modo_busca = True
        else:
            entrada = ENTRADA_PADRAO

    if not os.path.exists(entrada):
        dica = "ml_cookie_busca_novo.txt (ou ml_cookie_novo.txt)" if modo_busca else "ml_cookie_novo.txt"
        sys.exit(f"nao achei {entrada}. cole o cookie em {dica} e rode de novo")

    with open(entrada, encoding="utf-8") as f:
        cookie = limpar(f.read())

    nomes = [p.split("=")[0].strip() for p in cookie.split(";") if "=" in p]
    obrigatorios = OBRIGATORIOS_BUSCA if modo_busca else OBRIGATORIOS_LINKBUILDER
    faltando = [n for n in obrigatorios if n not in nomes]
    if faltando:
        sys.exit(
            f"faltam cookies obrigatorios ({modo_busca and 'busca' or 'linkbuilder'}): {faltando}. confira se copiou do request logado"
        )

    saida = saida_custom or (SAIDA_BUSCA if modo_busca else SAIDA_PADRAO)
    origem = "busca" if modo_busca else "linkbuilder"
    agora = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")
    dados = {
        "marketplace": "Mercado Livre",
        "origem": origem,
        "atualizacao": agora,
        "cookie": cookie,
    }

    # grava em arquivo temporario e troca no final pra nao corromper o json
    temporario = saida + ".tmp"
    with open(temporario, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(temporario, saida)

    # confere se o arquivo salvo abre como json valido
    with open(saida, encoding="utf-8") as f:
        json.load(f)

    os.remove(entrada)
    print(f"salvo ({origem}) em {saida} com {len(nomes)} cookies. arquivo de entrada apagado")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Salva cookie do ML em JSON de forma atomica.")
    parser.add_argument(
        "--busca",
        action="store_true",
        help="Salva o cookie da conta secundaria para a captacao (ml_cookies_busca.json).",
    )
    args = parser.parse_args(argv)
    salvar_cookie_arquivo(modo_busca=args.busca)


if __name__ == "__main__":
    main()
