# gerenciamento da fila manual assistida e plano b do estagio de ativacao
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from core.alerts import send_alert
from core.db import get_connection
from core.logger import get_logger

PASTA_BACK = Path(__file__).resolve().parent.parent
TZ_SP = ZoneInfo("America/Sao_Paulo")
DOMINIOS_AFILIADO_VALIDOS = (
    "meli.la",
    "mercadolivre.com",
    "www.mercadolivre.com",
    "mercadolivre.com.br",
    "www.mercadolivre.com.br",
    "ml.com.br",
    "www.ml.com.br",
)


def get_default_queue_path() -> Path:
    # retorna o caminho padrao do arquivo de fila manual em data/fila_links.csv
    pasta_data = PASTA_BACK / "data"
    pasta_data.mkdir(parents=True, exist_ok=True)
    return pasta_data / "fila_links.csv"


def validar_link_afiliado_url(url: str) -> bool:
    # valida se a url tem esquema https e host exato pertencente aos dominios oficiais do mercado livre
    if not url or not isinstance(url, str):
        return False
    url_limpa = url.strip()
    try:
        parsed = urlparse(url_limpa)
        if parsed.scheme.lower() != "https":
            return False
        if parsed.username or parsed.password:
            return False
        host = (parsed.hostname or "").lower()
        return host in DOMINIOS_AFILIADO_VALIDOS and bool(parsed.path and parsed.path != "/")
    except Exception:  # noqa: BLE001
        return False


def exportar_candidatos_fila(
    produtos: list[dict],
    caminho_arquivo: Path | str | None = None,
) -> Path:
    # exporta lista de produtos curados para preenchimento manual de links pelo dono
    arquivo = Path(caminho_arquivo) if caminho_arquivo else get_default_queue_path()
    arquivo.parent.mkdir(parents=True, exist_ok=True)

    campos = ["id_produto", "nome_produto", "score", "url_original", "url_afiliado"]
    with open(arquivo, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        for prod in produtos:
            writer.writerow(
                {
                    "id_produto": prod.get("id_produto", ""),
                    "nome_produto": prod.get("nome_produto", ""),
                    "score": prod.get("score", 0.0),
                    "url_original": prod.get("url_original", ""),
                    "url_afiliado": prod.get("url_afiliado", "") or "",
                }
            )

    logger = get_logger("fallback_queue")
    logger.info(f"Fila exportada com {len(produtos)} produtos em {arquivo.name}")
    return arquivo


def importar_links_fila(
    caminho_arquivo: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    # le o arquivo da fila, valida os links preenchidos e promove os produtos no sqlite
    arquivo = Path(caminho_arquivo) if caminho_arquivo else get_default_queue_path()
    logger = get_logger("fallback_queue")

    if not arquivo.exists():
        logger.warning(f"Arquivo de fila {arquivo.name} nao existe para importacao")
        return {
            "importados": 0,
            "invalidos": 0,
            "ignorados": 0,
            "erros": [f"Arquivo {arquivo.name} nao encontrado"],
        }

    fechar_conn = False
    if conn is None:
        conn = get_connection()
        fechar_conn = True

    importados = 0
    invalidos = 0
    ignorados = 0
    detalhes: list[dict] = []

    try:
        with open(arquivo, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            agora = datetime.now(TZ_SP).isoformat(timespec="seconds")
            for row in reader:
                id_prod = row.get("id_produto", "").strip()
                url_afiliado = row.get("url_afiliado", "").strip()

                if not id_prod or not url_afiliado:
                    ignorados += 1
                    continue

                if not validar_link_afiliado_url(url_afiliado):
                    invalidos += 1
                    detalhes.append({"id_produto": id_prod, "motivo": "URL de afiliado invalida"})
                    continue

                # atualiza no sqlite apenas produtos que existam e estejam em curado ou novo
                cur = conn.execute(
                    """
                    UPDATE produtos
                    SET url_afiliado = ?, status = 'link_pronto', data_atualizacao = ?
                    WHERE id_produto = ? AND status IN ('curado', 'novo', 'reserva')
                    """,
                    (url_afiliado, agora, id_prod),
                )
                if cur.rowcount > 0:
                    importados += 1
                    detalhes.append({"id_produto": id_prod, "status": "link_pronto"})
                else:
                    ignorados += 1
                    detalhes.append(
                        {"id_produto": id_prod, "motivo": "Produto nao encontrado ou status incompativel"}
                    )

        conn.commit()
    finally:
        if fechar_conn:
            conn.close()

    logger.info(
        f"Importacao de fila concluida: {importados} importados, {invalidos} invalidos, {ignorados} ignorados"
    )
    return {
        "importados": importados,
        "invalidos": invalidos,
        "ignorados": ignorados,
        "detalhes": detalhes,
    }


def verificar_e_acionar_plano_b(
    limite_minimo: int = 4,
    max_candidatos: int = 15,
    conn: sqlite3.Connection | None = None,
) -> dict:
    # confere a quantidade de links prontos no banco e gera a fila manual com alerta se estiver abaixo do minimo
    fechar_conn = False
    if conn is None:
        conn = get_connection()
        fechar_conn = True

    try:
        row = conn.execute("SELECT count(*) as qtd FROM produtos WHERE status = 'link_pronto'").fetchone()
        qtd_prontos = row["qtd"] if row else 0

        if qtd_prontos >= limite_minimo:
            return {"acionado": False, "qtd_prontos": qtd_prontos, "motivo": "Fila suficiente"}

        cursor = conn.execute(
            """
            SELECT id_produto, nome_produto, score, url_original, url_afiliado
            FROM produtos
            WHERE status = 'curado' AND (url_afiliado IS NULL OR url_afiliado = '')
            ORDER BY score DESC
            LIMIT ?
            """,
            (max_candidatos,),
        )
        candidatos = [dict(r) for r in cursor.fetchall()]

        if not candidatos:
            return {
                "acionado": False,
                "qtd_prontos": qtd_prontos,
                "motivo": "Sem candidatos curados pendentes de link",
            }

        arquivo_fila = exportar_candidatos_fila(candidatos)
        msg_alerta = (
            f"Fila de links baixa ({qtd_prontos} prontos). "
            f"Plano B ativado: {len(candidatos)} produtos exportados para {arquivo_fila.name}."
        )
        send_alert(
            msg_alerta,
            nivel="warning",
            detalhes={"qtd_prontos": qtd_prontos, "candidatos_exportados": len(candidatos)},
        )

        return {
            "acionado": True,
            "qtd_prontos": qtd_prontos,
            "candidatos_exportados": len(candidatos),
            "arquivo": str(arquivo_fila),
        }
    finally:
        if fechar_conn:
            conn.close()
