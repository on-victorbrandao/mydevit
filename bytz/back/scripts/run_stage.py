# ponto de entrada cli para rodar estagios isolados do pipeline bytz
import argparse
import sys
from pathlib import Path

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from capture.scraper import executar_captacao_termo
from capture.scraper import run as run_capture
from core.logger import ensure_env_loaded
from scout.temas import run as run_scout
from strategist.termos import run as run_strategist


def _rodar_scout(_args: argparse.Namespace) -> int:
    # executa o estagio 1 (scout) para descobrir e gravar os top temas do dia
    res = run_scout()
    print(f"Data: {res['data']} | Temas avaliados: {len(res['temas'])}")
    print(
        f"Inseridos: {res['inseridos']} | Atualizados: {res['atualizados']} | Usou cache: {res['usou_cache']}"
    )
    return 0


def _rodar_strategist(_args: argparse.Namespace) -> int:
    # executa o estagio 2 (estrategista) gerando termos na tabela buscas
    res = run_strategist()
    print(f"Buscas ativas do dia: {res['buscas_ativas']}")
    print(
        f"Inseridos: {res['inseridos']} | Atualizados: {res['atualizados']} | "
        f"Aposentados: {res['aposentados']} | Reativados: {res['reativados']}"
    )
    return 0


def _rodar_capture(args: argparse.Namespace) -> int:
    # executa a captacao pela fila da tabela buscas ou para um termo avulso se informado
    arquivo_path = Path(args.arquivo).resolve() if getattr(args, "arquivo", None) else None

    if not args.termo:
        res_lote = run_capture(
            max_buscas_override=args.max_buscas,
            arquivo_html_path=arquivo_path,
            max_retries_override=args.retries,
        )
        print(f"Termos executados da tabela buscas: {res_lote['termos_executados']}")
        print(f"Cards encontrados no lote: {res_lote['total_cards']}")
        print(f"Passaram nos filtros: {res_lote['total_aprovados']}")
        print(
            f"Novos inseridos no SQLite: {res_lote['inseridos']} "
            f"(ignorados/duplicados: {res_lote['ignorados']} | aposentados: {res_lote['aposentados']})"
        )
        return 0

    fixture_path = Path(args.salvar_fixture).resolve() if args.salvar_fixture else None
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

    subparsers.add_parser("scout", help="Executa o Scout (descoberta de temas).")
    subparsers.add_parser("strategist", help="Executa o Estrategista (termos na tabela buscas).")

    cap_parser = subparsers.add_parser("capture", help="Executa a captacao pela tabela buscas ou por termo.")
    cap_parser.add_argument(
        "--termo",
        required=False,
        default=None,
        help="Termo avulso opcional. Se omitido, consome os termos ativos da tabela buscas.",
    )
    cap_parser.add_argument(
        "--max-buscas",
        type=int,
        default=None,
        help="Limite opcional de termos da tabela buscas para rodar nesta execucao.",
    )
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
    if args.estagio == "scout":
        return _rodar_scout(args)
    if args.estagio == "strategist":
        return _rodar_strategist(args)
    if args.estagio == "capture":
        return _rodar_capture(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
