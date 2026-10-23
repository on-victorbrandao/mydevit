# controle basico de orcamento e calculo de custo de ia
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.db import get_connection

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

PASTA_BACK = Path(__file__).resolve().parent.parent
TZ_SP = ZoneInfo("America/Sao_Paulo")
CONFIG_PATH = PASTA_BACK / "config.yaml"


class BudgetConfigError(ValueError):
    # erro disparado quando a configuracao de tarifas ou cotacao for ausente ou invalida
    pass


def carregar_config_ia(custom_config_path: Path | None = None) -> dict:
    # le a secao ia do config.yaml sem aplicar valores arbitrarios de tarifa
    caminho = custom_config_path or CONFIG_PATH
    if not caminho.exists():
        raise BudgetConfigError(f"Arquivo de configuracao nao encontrado em: {caminho}")

    if not HAS_YAML:
        raise BudgetConfigError("Biblioteca PyYAML nao instalada no ambiente Python")

    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        raise BudgetConfigError(f"Erro ao interpretar {caminho.name}: {e}") from e

    secao_ia = dados.get("ia")
    if not isinstance(secao_ia, dict):
        raise BudgetConfigError("Secao 'ia' ausente ou malformatada no config.yaml")

    return secao_ia


def validar_tarifas_modelo(cfg: dict, modelo: str) -> tuple[float, float, float, float]:
    # valida se o modelo, cotacao, teto e tarifas estao configurados e validos
    teto = cfg.get("teto_mensal_brl")
    if teto is None or not isinstance(teto, (int, float)) or teto <= 0:
        raise BudgetConfigError(f"Teto mensal de IA invalido ou nao configurado: {teto}")

    cotacao = cfg.get("cotacao_usd_brl")
    if cotacao is None or not isinstance(cotacao, (int, float)) or cotacao <= 0:
        raise BudgetConfigError(f"Cotacao USD/BRL nao configurada ou invalida: {cotacao}")

    modelos = cfg.get("modelos")
    if not isinstance(modelos, dict):
        raise BudgetConfigError("Dicionario 'modelos' nao configurado na secao ia")

    if modelo not in modelos:
        raise BudgetConfigError(f"Modelo de IA '{modelo}' nao cadastrado no config.yaml")

    tarifa = modelos[modelo]
    if not isinstance(tarifa, dict):
        raise BudgetConfigError(f"Tarifas do modelo '{modelo}' devem ser um dicionario")

    in_usd = tarifa.get("entrada_usd_1m")
    out_usd = tarifa.get("saida_usd_1m")
    if in_usd is None or not isinstance(in_usd, (int, float)) or in_usd < 0:
        raise BudgetConfigError(f"Tarifa de entrada (entrada_usd_1m) invalida para '{modelo}': {in_usd}")
    if out_usd is None or not isinstance(out_usd, (int, float)) or out_usd < 0:
        raise BudgetConfigError(f"Tarifa de saida (saida_usd_1m) invalida para '{modelo}': {out_usd}")

    alerta_pct = float(cfg.get("alerta_pct", 80.0))
    return float(in_usd), float(out_usd), float(cotacao), float(teto), alerta_pct


def calcular_custo_estimado(
    modelo: str,
    tokens_in: int,
    tokens_out: int,
    config_override: dict | None = None,
) -> float:
    # calcula o custo em reais a partir das tarifas explicitas do modelo
    if not isinstance(tokens_in, int) or tokens_in < 0:
        raise ValueError(f"Quantidade de tokens_in invalida: {tokens_in}")
    if not isinstance(tokens_out, int) or tokens_out < 0:
        raise ValueError(f"Quantidade de tokens_out invalida: {tokens_out}")

    cfg = config_override if config_override is not None else carregar_config_ia()
    in_usd, out_usd, cotacao, _, _ = validar_tarifas_modelo(cfg, modelo)

    # calcula o custo em dolares e converte para reais pela cotacao
    custo_usd = (tokens_in / 1_000_000.0 * in_usd) + (tokens_out / 1_000_000.0 * out_usd)
    custo_brl = custo_usd * cotacao
    return float(custo_brl)


def registrar_gasto(
    modelo: str,
    tokens_in: int,
    tokens_out: int,
    custom_db_path: Path | None = None,
    config_override: dict | None = None,
) -> float:
    # registra chamada na tabela gasto_ia
    data_hoje = datetime.now(TZ_SP).strftime("%Y-%m-%d")
    custo_brl = calcular_custo_estimado(modelo, tokens_in, tokens_out, config_override=config_override)

    # grava o registro no banco em transacao fechada
    conn = get_connection(custom_db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO gasto_ia (data, modelo, tokens_in, tokens_out, custo_estimado_brl)
                VALUES (?, ?, ?, ?, ?);
                """,
                (data_hoje, modelo, tokens_in, tokens_out, custo_brl),
            )
    finally:
        conn.close()

    return custo_brl


def consultar_gasto_mes(
    ano_mes: str | None = None,
    custom_db_path: Path | None = None,
    config_override: dict | None = None,
) -> dict:
    # consulta o acumulado do mes corrente na tabela gasto_ia
    if not ano_mes:
        ano_mes = datetime.now(TZ_SP).strftime("%Y-%m")

    # carrega limites e percentual de alerta configurados
    cfg = config_override if config_override is not None else carregar_config_ia()
    teto = cfg.get("teto_mensal_brl")
    teto_brl = float(teto) if isinstance(teto, (int, float)) and teto > 0 else 50.0
    alerta_pct = float(cfg.get("alerta_pct", 80.0))

    # executa a agregacao mensal no banco de dados
    conn = get_connection(custom_db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COALESCE(SUM(tokens_in), 0) AS total_in,
                COALESCE(SUM(tokens_out), 0) AS total_out,
                COALESCE(SUM(custo_estimado_brl), 0.0) AS total_brl
            FROM gasto_ia
            WHERE data LIKE ?;
            """,
            (f"{ano_mes}%",),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    # calcula totais e checa se atingiu alerta de 80% ou teto
    total_brl = float(row["total_brl"]) if row else 0.0
    tokens_in = int(row["total_in"]) if row else 0
    tokens_out = int(row["total_out"]) if row else 0
    pct_teto = round((total_brl / teto_brl * 100), 1) if teto_brl > 0 else 0.0

    return {
        "mes": ano_mes,
        "total_brl": total_brl,
        "total_brl_apresentacao": round(total_brl, 2),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "teto_brl": teto_brl,
        "pct_teto": pct_teto,
        "alerta_80": pct_teto >= alerta_pct,
        "estourou": total_brl >= teto_brl,
    }


def pode_gastar_ia(
    custom_db_path: Path | None = None,
    config_override: dict | None = None,
    modelo: str | None = None,
) -> bool:
    # bloqueia o uso se a configuracao for invalida, modelo sem tarifa ou teto estourado
    try:
        cfg = config_override if config_override is not None else carregar_config_ia()
        if modelo:
            validar_tarifas_modelo(cfg, modelo)
        info = consultar_gasto_mes(custom_db_path=custom_db_path, config_override=cfg)
        return not info["estourou"]
    except BudgetConfigError:
        return False
