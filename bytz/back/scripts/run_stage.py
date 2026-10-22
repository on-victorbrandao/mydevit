# ponto de entrada cli para rodar estagios isolados do pipeline bytz
import argparse
import sys
from pathlib import Path

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from capture.scraper import executar_captacao_termo
from core.logger import ensure_env_loaded


def _rodar_capture(args: argparse.Namespace) -> int:
    # executa o estagio de captacao para um termo especifico ou arquivo local e imprime resumo
    fixture_path = Path(args.salvar_fixture).resolve() if args.salvar_fixture else None
    arquivo_path = Path(args.arquivo).resolve() if getattr(args, "arquivo", None) else None
    resultado = executar_captacao_termo(
        termo=args.termo,
        salvar_fixture_path=fixture_path,
        arquivo_html_path=arquivo_path,
        max_retries_override=args.retries,
    )
    print(f"Termo buscado: {resultado['termo']}")
    print(f"Cards encontrados: {resultado['total_cards']}")
    print(f"Passaram nos filtros: {resultado['total_aprovados']}")
    print(
        f"Novos inseridos no SQLite: {resultado['inseridos']} (ignorados/duplicados: {resultado['ignorados']})"
    )

    amostra = resultado["aprovados"][:5]
    if amostra:
        print("\nExemplos aprovados (ate 5):")
        for idx, item in enumerate(amostra, start=1):
            print(
                f"  {idx}. [{item['id_produto']}] {item['nome_produto'][:65]} | "
                f"de R$ {item['valor_original']:.2f} por R$ {item['valor_atual']:.2f} "
                f"(-{item['desconto_pct']:.1f}%) | nota {item['nota']} | vendas {item['vendas']}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_env_loaded()
    parser = argparse.ArgumentParser(description="Executa um estagio do pipeline BYTZ.")
    subparsers = parser.add_subparsers(dest="estagio", required=True)

    cap_parser = subparsers.add_parser("capture", help="Executa a captacao publica por termo.")
    cap_parser.add_argument("--termo", required=True, help="Termo de busca (ex.: 'fone bluetooth').")
    cap_parser.add_argument(
        "--arquivo",
        default=None,
        help="Caminho opcional de HTML local para rodar parser, filtros e SQLite sem rede.",
    )
    cap_parser.add_argument(
        "--retries",
        type=int,
        default=None,
        help="Tentativas extras so para falha de rede (padrao: config.yaml). Use 0 para tentativa unica.",
    )
    cap_parser.add_argument(
        "--salvar-fixture",
        default=None,
        help="Caminho opcional para salvar o HTML anonimizado dos cards como fixture.",
    )

    args = parser.parse_args(argv)
    if args.estagio == "capture":
        return _rodar_capture(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
