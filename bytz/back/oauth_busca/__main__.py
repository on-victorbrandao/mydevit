import argparse
import getpass
import os
import sys
import warnings
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from .client import Client, Config
from .storage import OAuthError


def main(argv=None):
    # Esta entrada explicita e o unico ponto que carrega o .env operacional.
    parser = argparse.ArgumentParser(description="OAuth local exclusivo da conta secundaria de busca.")
    parser.add_argument("action", choices=["iniciar", "concluir", "renovar"])
    args = parser.parse_args(argv)
    try:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        config = Config(
            *(
                os.environ.get(key, "")
                for key in (
                    "ML_SEARCH_CLIENT_ID",
                    "ML_SEARCH_CLIENT_SECRET",
                    "ML_SEARCH_REDIRECT_URI",
                    "ML_SEARCH_USER_ID",
                )
            )
        )
        client = Client(config)
        if args.action == "iniciar":
            if not webbrowser.open(client.begin()):
                raise OAuthError("Nao foi possivel abrir o navegador; revise o navegador padrao.")
            print("Confira a conta secundaria no navegador. Depois execute concluir em ate 5 minutos.")
        elif args.action == "concluir":
            if not sys.stdin.isatty():
                raise OAuthError("Use um terminal local interativo, sem gravacao da sessao.")
            # Se o terminal nao conseguir ocultar a entrada, falha em vez de exibir o codigo.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                callback = getpass.getpass("Cole a URL completa de retorno aqui (entrada oculta): ")
            client.complete(callback.strip())
            print("Autorizacao da conta secundaria salva com protecao local.")
        else:
            client.access_token()
            print("Credencial local disponivel; renovacao so ocorre perto da expiracao.")
        return 0
    except OAuthError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Operacao interrompida; nenhum detalhe sensivel foi registrado.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
