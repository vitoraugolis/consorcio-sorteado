"""
webhooks/agente_espera.py — Responde leads em stage ESPERA (aguardando extrato LP)

Comportamento:
- Se o lead manda texto (não extrato): resposta gentil lembrando que aguardamos o extrato
- Não faz nada complexo — só mantém a conversa viva até o extrato chegar
"""

import logging
from services.faro import get_name, get_adm, get_phone
from services.whapi import WhapiClient, WhapiError
from services.session_store import load_history_smart, save_history_smart
from services.faro import history_append

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

_RECUSA_KEYWORDS = [
    "não quero", "nao quero", "desistir", "cancelar", "me remova",
    "sem interesse", "não tenho mais interesse", "pode tirar",
    "parem", "para de enviar",
]


async def handle_message(card: dict, mensagem: str) -> None:
    nome   = get_name(card).split()[0] if get_name(card) else "você"
    adm    = get_adm(card)
    phone  = get_phone(card)
    card_id = card.get("id", "")

    if not phone:
        return

    texto_lower = mensagem.lower()

    # Lead desistindo → resposta de encerramento
    if any(k in texto_lower for k in _RECUSA_KEYWORDS):
        msg = (
            f"Tudo bem, {nome}! Entendido. 😊\n\n"
            f"Caso mude de ideia no futuro, é só nos chamar. "
            f"E se quiser acompanhar as melhores oportunidades de consórcio, "
            f"temos um grupo com novidades: {_GROUP_LINK}"
        )
    else:
        # Resposta padrão — lembra gentilmente do extrato
        msg = (
            f"Olá, {nome}! 😊\n\n"
            f"Ainda aguardamos o seu extrato da *{adm}* para conseguirmos fazer a análise "
            f"e te passar uma proposta personalizada.\n\n"
            f"Assim que enviar (PDF ou foto do app/site da administradora), damos sequência rapidinho! 🚀"
        )

    try:
        async with WhapiClient(canal="lp") as w:
            await w.send_text(phone, msg, _log_nome=get_name(card), _log_card_id=card_id)

        history = await load_history_smart(phone, card)
        history = history_append(history, "user", mensagem)
        history = history_append(history, "assistant", msg)
        from services.faro import FaroClient
        async with FaroClient() as faro:
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)

    except WhapiError as e:
        logger.error("agente_espera: erro Whapi card %s: %s", card_id[:8], e)
    except Exception as e:
        logger.error("agente_espera: erro card %s: %s", card_id[:8], e)
