"""
services/slack.py — Notificações técnicas via Slack

Separação de responsabilidades:
  - WhatsApp (Whapi) → notificações COMERCIAIS para agentes
    (lead aceitou proposta, contrato assinado, lead quer atendente)
  - Slack → alertas TÉCNICOS para a equipe Guará Lab
    (erros de IA, falhas de API, extrato sem análise, jobs com falha)

Configure um Incoming Webhook em:
  https://api.slack.com/apps → seu app → Incoming Webhooks → Add New Webhook to Workspace

A URL tem o formato:
  https://hooks.slack.com/services/T.../B.../XXXX

Adicione ao .env:
  SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
"""

import logging

import httpx

from config import SLACK_WEBHOOK_URL

logger = logging.getLogger(__name__)


class SlackError(Exception):
    pass


async def slack_alert(
    message: str,
    level: str = "warning",
    context: dict = None,
) -> bool:
    """
    Envia um alerta técnico para o Slack.

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

    # Monta o payload com attachments para melhor legibilidade
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
