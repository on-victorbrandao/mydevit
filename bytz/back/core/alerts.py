# envio de alertas do sistema para o telegram privado do dono com sanitizacao de payload
import os
from pathlib import Path

import requests

from core.logger import get_logger, sanitize_data, sanitize_text

PASTA_BACK = Path(__file__).resolve().parent.parent


def send_alert(
    mensagem: str,
    nivel: str = "warning",
    detalhes: dict | None = None,
    dry_run: bool = False,
    custom_logs_dir: Path | None = None,
) -> bool:
    # sanitiza conteudo antes de montar e enviar alerta para o administrador
    logger = get_logger("alerts", custom_logs_dir=custom_logs_dir)
    msg_limpa = sanitize_text(mensagem)
    detalhes_limpos = sanitize_data(detalhes) if detalhes else None

    # obtem credenciais e define se a execucao e simulada
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    admin_chat_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
    modo_teste = os.environ.get("TEST_MODE", "0") in ("1", "true", "True") or dry_run

    # formata a mensagem com nivel e detalhes sanitizados
    texto = f"[{nivel.upper()}] BYTZ Alerta:\n{msg_limpa}"
    if detalhes_limpos:
        linhas = [f"- {k}: {v}" for k, v in detalhes_limpos.items()]
        texto += "\n" + "\n".join(linhas)

    if modo_teste:
        # em modo de teste ou dry_run nao dispara requisicao de rede real
        logger.info(f"[SIMULACAO DE ALERTA] {msg_limpa}", extra={"detalhes": detalhes_limpos})
        return True

    # valida se as credenciais necessarias foram configuradas
    if not bot_token or not admin_chat_id:
        logger.warning("Alerta nao enviado: TELEGRAM_BOT_TOKEN ou TELEGRAM_ADMIN_CHAT_ID nao configurados")
        return False

    # monta o endpoint e o payload para envio via bot api
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": admin_chat_id,
        "text": texto,
    }

    # dispara a requisicao http com timeout e tratamento de resposta
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("Alerta enviado com sucesso via Telegram")
            return True
        logger.error(f"Falha ao enviar alerta via Telegram: HTTP {resp.status_code}")
        return False
    except Exception as e:  # noqa: BLE001
        # nunca expor str(e) bruto pois requests pode incluir a url com o bot token no erro
        logger.error(f"Erro de conexao ao enviar alerta via Telegram: tipo={type(e).__name__}")
        return False
