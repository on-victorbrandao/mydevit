# ponto de entrada cli para rodar estagios isolados do pipeline bytz
import argparse
import sys
from pathlib import Path

PASTA_BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PASTA_BACK))

from activation.affiliate import executar_estagio_ativacao
from activation.fallback_queue import importar_links_fila, verificar_e_acionar_plano_b
from capture.scraper import executar_captacao_termo
from capture.scraper import run as run_capture
from core.logger import ensure_env_loaded
from curator.score import run as run_curator
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


def _rodar_curator(args: argparse.Namespace) -> int:
    # executa o estagio 4 (curador) pontuando produtos novo e aplicando cortes e juiz llm
    res = run_curator(
        usar_cache_ia=not getattr(args, "sem_cache", False),
        reprocessar_hoje=getattr(args, "reprocessar_hoje", False),
    )
    print(
        f"Produtos avaliados: {res['avaliados']} | Curados (>=65): {res['curados']} | "
        f"Reservas (50-64 ou variedade): {res['reservas']} | Descartados: {res['descartados']} "
        f"(suspeitos: {res['suspeitos']})"
    )
    print(
        f"Historico gravado: {res['historico_gravado']} | Chamadas IA: {res['chamadas_ia']} | "
        f"Cache hits: {res['cache_hits']} | Custo IA: R$ {res['custo_brl']:.4f} | "
        f"Fallback: {res['usou_fallback']}"
    )

    curados = [x for x in res["ranking"] if x["status"] == "curado"][:5]
    if curados:
        print("\nTop curados para ativacao (ate 5):")
        for idx, item in enumerate(curados, start=1):
            print(
                f"  {idx}. [{item['id_produto']}] score={item['score']:.2f} | "
                f"afinidade={item['afinidade']:.1f} | R$ {float(item['valor_atual'] or 0):.2f} | "
                f"termo='{item.get('termo_origem')}' | {item['nome_produto'][:55]}"
            )
    return 0


def _rodar_activation(args: argparse.Namespace) -> int:
    # executa o estagio 5 (ativacao) gerando links de afiliado para produtos curados
    if getattr(args, "importar_fila", None):
        res_imp = importar_links_fila(args.importar_fila)
        print(
            f"Fila importada: {res_imp['importados']} links promovidos a link_pronto | "
            f"{res_imp['invalidos']} invalidos | {res_imp['ignorados']} ignorados."
        )
        return 0

    if getattr(args, "plano_b", False):
        res_pb = verificar_e_acionar_plano_b(limite_minimo=9999)
        print(
            f"Plano B manual acionado: {res_pb.get('candidatos_exportados', 0)} candidatos exportados para {res_pb.get('arquivo')}."
        )
        return 0

    res = executar_estagio_ativacao(
        limite=args.limite,
        modalidade=args.modalidade,
        tag=args.tag,
    )
    print(
        f"Processados: {res['processados']} | Sucessos: {res['sucessos']} | "
        f"Falhas: {res['falhas']} | Modalidade: {res['modalidade']}"
    )
    if res.get("produtos"):
        print("\nProdutos curados avaliados:")
        for p in res["produtos"]:
            link = p.get("url_afiliado") or "sem link"
            print(f"  - [{p['id_produto']}] status={p.get('status')} | {link} | {p['nome_produto'][:50]}")
    if res.get("erro"):
        print(f"Erro na execucao: {res['erro']}")
        return 1
    return 0 if res["falhas"] == 0 else 1


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

    cur_parser = subparsers.add_parser(
        "curator", help="Executa o Curador (score 0-100, historico de preco e juiz LLM em lote)."
    )
    cur_parser.add_argument(
        "--reprocessar-hoje",
        action="store_true",
        help="Reavalia tambem produtos que ja foram curados/reservados/descartados na data de hoje.",
    )
    cur_parser.add_argument(
        "--sem-cache",
        action="store_true",
        help="Ignora o cache local do juiz LLM nesta execucao.",
    )

    act_parser = subparsers.add_parser(
        "activation", help="Executa a Ativacao (geracao de links de afiliado com merge_cookie e createLink)."
    )
    act_parser.add_argument(
        "--modalidade",
        choices=["lote", "individual"],
        default="lote",
        help="Modalidade de geracao: lote (padrao) ou individual.",
    )
    act_parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Limite de produtos curados para processar nesta execucao.",
    )
    act_parser.add_argument(
        "--tag",
        default=None,
        help="Tag de afiliado customizada opcional (padrao: AFFILIATE_TAG do .env).",
    )
    act_parser.add_argument(
        "--plano-b",
        action="store_true",
        help="Aciona exportacao da fila de candidatos para preenchimento manual do Plano B.",
    )
    act_parser.add_argument(
        "--importar-fila",
        default=None,
        help="Caminho do arquivo CSV para importar links preenchidos manualmente pelo dono.",
    )

    args = parser.parse_args(argv)
    if args.estagio == "scout":
        return _rodar_scout(args)
    if args.estagio == "strategist":
        return _rodar_strategist(args)
    if args.estagio == "capture":
        return _rodar_capture(args)
    if args.estagio == "curator":
        return _rodar_curator(args)
    if args.estagio == "activation":
        return _rodar_activation(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
