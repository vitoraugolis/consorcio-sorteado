"""
services/slack.py — Notificações via Slack

Dois canais:
  - #alertas-sistemas  (SLACK_WEBHOOK_URL)   → alertas TÉCNICOS para a equipe Guará Lab
    Erros de IA, falhas de API, jobs com falha, extrato sem análise
  - #log-cs            (SLACK_LOG_CS_URL)    → log de TRÁFEGO comercial em tempo real
    Toda mensagem enviada ou recebida pelos números do CS (Bazar, LP, Listas)

Configure Incoming Webhooks em:
  https://api.slack.com/apps → seu app → Incoming Webhooks → Add New Webhook to Workspace

Adicione ao .env:
  SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...   ← alertas técnicos
  SLACK_LOG_CS_URL=https://hooks.slack.com/services/...    ← log-cs
"""

import logging

import httpx

from config import SLACK_WEBHOOK_URL, SLACK_LOG_CS_URL

logger = logging.getLogger(__name__)


class SlackError(Exception):
    pass


async def slack_alert(
    message: str,
    level: str = "warning",
    context: dict = None,
) -> bool:
    """
    Envia um alerta técnico para o Slack (#alertas-sistemas).

    Args:
        message:  Texto principal do alerta.
        level:    "info" | "warning" | "error" — determina o emoji/cor.
        context:  Dict com campos adicionais exibidos como attachment fields.

    Returns:
        True se enviou com sucesso, False se o webhook não está configurado
        ou se houve erro de rede (nunca lança exceção — alerta não pode
        derrubar o fluxo principal).
    """
    if not SLACK_WEBHOOK_URL:
        logger.debug("Slack: SLACK_WEBHOOK_URL não configurado, alerta ignorado.")
        return False

    icons = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}
    colors = {"info": "#36a64f", "warning": "#ffcc00", "error": "#ff0000"}
    icon  = icons.get(level, "⚠️")
    color = colors.get(level, "#ffcc00")

    fields = []
    if context:
        for key, val in context.items():
            fields.append({
                "title": str(key),
                "value": str(val)[:200],
                "short": len(str(val)) < 50,
            })

    payload = {
        "attachments": [
            {
                "color": color,
                "text": f"{icon} *{message}*",
                "fields": fields,
                "footer": "Consórcio Sorteado · Sistema de Automação",
                "mrkdwn_in": ["text"],
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(SLACK_WEBHOOK_URL, json=payload)
            r.raise_for_status()
        logger.info("Slack: alerta enviado (%s): %s", level, message[:80])
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Slack: HTTP %d ao enviar alerta: %s", e.response.status_code, e.response.text[:100])
        return False
    except httpx.RequestError as e:
        logger.error("Slack: erro de rede ao enviar alerta: %s", e)
        return False


async def slack_error(message: str, exception: Exception = None, context: dict = None) -> bool:
    """Atalho para alertas de nível error."""
    ctx = dict(context or {})
    if exception:
        ctx["Exceção"] = f"{type(exception).__name__}: {str(exception)[:200]}"
    return await slack_alert(message, level="error", context=ctx)


async def slack_warning(message: str, context: dict = None) -> bool:
    """Atalho para alertas de nível warning."""
    return await slack_alert(message, level="warning", context=context)


async def slack_info(message: str, context: dict = None) -> bool:
    """Atalho para alertas de nível info."""
    return await slack_alert(message, level="info", context=context)


# ---------------------------------------------------------------------------
# Log de tráfego comercial — #log-cs
# ---------------------------------------------------------------------------

_CANAL_EMOJI = {
    "lista": "📋",
    "bazar": "🏪",
    "lp":    "🌐",
}

_DIR_EMOJI = {
    "enviado":  "📤",
    "recebido": "📥",
}


async def log_cs(
    direcao: str,
    canal: str,
    phone: str,
    nome: str = "",
    card_id: str = "",
    mensagem: str = "",
    extra: dict = None,
) -> bool:
    """
    Registra uma mensagem enviada ou recebida no canal #log-cs do Slack.

    Args:
        direcao:  "enviado" ou "recebido"
        canal:    "lista" | "bazar" | "lp"
        phone:    Número do contato (E.164 sem +)
        nome:     Nome do lead (opcional)
        card_id:  UUID do card no FARO (opcional, primeiros 8 chars)
        mensagem: Texto da mensagem (truncado a 300 chars)
        extra:    Dict com campos adicionais (ex: stage, tipo)

    Returns:
        True se enviou, False se não configurado ou erro.
    """
    if not SLACK_LOG_CS_URL:
        logger.debug("Slack log-cs: SLACK_LOG_CS_URL não configurado, log ignorado.")
        return False

    canal_emoji = _CANAL_EMOJI.get(canal, "📱")
    dir_emoji   = _DIR_EMOJI.get(direcao, "↔️")
    nome_fmt    = f" · {nome}" if nome else ""
    card_fmt    = f" · `{card_id[:8]}`" if card_id else ""
    msg_preview = (mensagem[:297] + "...") if len(mensagem) > 300 else mensagem

    header = f"{dir_emoji} {canal_emoji} *[{canal.upper()}]* {phone}{nome_fmt}{card_fmt}"

    fields = [{"title": "Mensagem", "value": msg_preview or "_(sem texto)_", "short": False}]
    if extra:
        for key, val in extra.items():
            fields.append({"title": str(key), "value": str(val)[:200], "short": True})

    color = "#2eb886" if direcao == "enviado" else "#439fe0"

    payload = {
        "attachments": [
            {
                "color": color,
                "text": header,
                "fields": fields,
                "footer": "Consórcio Sorteado · log-cs",
                "mrkdwn_in": ["text"],
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(SLACK_LOG_CS_URL, json=payload)
            r.raise_for_status()
        logger.debug("Slack log-cs: %s %s %s", direcao, canal, phone)
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Slack log-cs: HTTP %d: %s", e.response.status_code, e.response.text[:100])
        return False
    except httpx.RequestError as e:
        logger.error("Slack log-cs: erro de rede: %s", e)
        return False
