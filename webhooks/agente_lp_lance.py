"""
webhooks/agente_lp_lance.py — Agente para leads LP contemplados por LANCE

Fluxo:
  1. Ao entrar na stage LP_LANCE (via ativação), o lead já recebeu uma mensagem
     explicando que não compramos cartas de lance com ágio, mas que se quiser
     pode enviar o extrato e receber uma proposta mesmo assim.

  2. Se o lead responder positivamente (quero proposta / vou enviar extrato):
     - Move para PRIMEIRA_ATIVACAO (fluxo LP normal de extrato)
     - Notifica Slack: "Lead LP Lance quer proposta — acompanhar precificação manual"

  3. Se o lead responder negativamente (não tenho interesse / cota já vendida):
     - Move para PERDIDO

  4. Se enviar extrato diretamente (mídia):
     - Encaminha para qualificador normalmente (trata como se fosse PRIMEIRA_ATIVACAO)
     - Notifica Slack sobre lead de lance

  Em qualquer resposta ambígua: mantém na stage e aguarda.
"""

import json
import logging
import re
from datetime import datetime, timezone

from config import Stage
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm,
    history_append, history_to_text,
    build_card_context,
)
from services.slack import slack_warning
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card
from services.session_store import load_history_smart, save_history_smart

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

# ---------------------------------------------------------------------------
# Mensagem de ativação — enviada quando o lead é movido para LP_LANCE
# ---------------------------------------------------------------------------

MSG_LP_LANCE = (
    "Olá {nome}, tudo bem? Sou a Manuela, da Consórcio Sorteado! 😊\n\n"
    "Recebemos seu interesse em vender sua cota {adm}. Gostaríamos muito de ajudar, "
    "mas preciso ser transparente: *cotas contempladas por lance* têm um custo de ágio "
    "embutido que, infelizmente, impossibilita nossa compra nas condições habituais.\n\n"
    "Dito isso — *se você ainda tiver interesse em receber uma proposta*, é só me "
    "enviar o extrato atualizado da cota que nossa equipe analisa e te retorna. "
    "Não custa nada tentar! 📄\n\n"
    "Caso prefira, te convido para nosso grupo gratuito com dicas e novidades sobre "
    "consórcios contemplados:\n"
    "👉 " + _GROUP_LINK
)

# ---------------------------------------------------------------------------
# Classificador de intent
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
Você é Manuela, consultora da Consórcio Sorteado.
O lead recebeu uma mensagem explicando que cotas de lance têm limitação, mas que
pode enviar o extrato se quiser receber uma proposta mesmo assim.

Classifique a resposta do lead em um dos intents abaixo e gere uma resposta curta
e natural (máx 3 linhas). Retorne EXCLUSIVAMENTE JSON puro, sem markdown.

Intents possíveis:
- QUER_PROPOSTA: lead quer enviar extrato / receber proposta / confirma interesse
- SEM_INTERESSE: lead não quer / cota já vendida / dispensa
- AMBIGUO: mensagem indefinida, pergunta, comentário neutro

DADOS DO LEAD:
{dados_card}

JSON esperado:
{{"intent": "QUER_PROPOSTA|SEM_INTERESSE|AMBIGUO", "response": "mensagem para o lead"}}
""".strip()

_FALLBACK_QUER = "Ótimo! Pode me enviar o extrato da cota quando quiser — analiso rapidinho. 😊📄"
_FALLBACK_sem  = "Entendido! Qualquer coisa, estamos por aqui. Até mais! 🙏"
_FALLBACK_amb  = "Pode me contar mais? Assim consigo te ajudar melhor. 😊"


async def _notificar_slack_lance(card: dict) -> None:
    """Alerta o Slack que um lead LP Lance quer proposta — precificação manual necessária."""
    nome  = get_name(card)
    adm   = get_adm(card)
    phone = get_phone(card)
    cid   = card.get("id", "")[:8]
    credito = card.get("Valor do crédito") or card.get("Crédito") or "?"

    msg = (
        f"🟡 *Lead LP Lance quer proposta — precificação manual*\n"
        f"Lead: {nome} | Adm: {adm} | Crédito: {credito}\n"
        f"Tel: {phone} | Card: `{cid}`\n"
        f"⚠️ Não temos regra automática para lance — acompanhar manualmente após extrato."
    )
    await slack_warning(msg)


async def handle_message(card: dict, text: str) -> None:
    """Roteador principal para mensagens de leads na stage LP_LANCE."""
    card_id = card.get("id", "")
    nome    = get_name(card)
    phone   = get_phone(card)

    if not phone:
        logger.warning("agente_lp_lance: card %s sem telefone", card_id[:8])
        return

    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)

    history = await load_history_smart(phone, card_fresh)
    history = history_append(history, "user", text)

    # Classifica intent via IA
    intent = "AMBIGUO"
    texto_resposta = _FALLBACK_amb
    try:
        system = _SYSTEM_PROMPT.format(dados_card=build_card_context(card_fresh))
        async with AIClient() as ai:
            raw = await ai.complete_with_history(
                history=history,
                system=system,
                max_tokens=200,
                model="gpt-4o-mini",
                fallback_model="gpt-4o-mini",
            )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group())
            intent = data.get("intent", "AMBIGUO").upper()
            texto_resposta = (data.get("response") or "").strip()
        if not texto_resposta:
            intent = "AMBIGUO"
    except (AIError, json.JSONDecodeError, Exception) as e:
        logger.error("agente_lp_lance: IA falhou para card %s: %s", card_id[:8], e)

    if not texto_resposta:
        texto_resposta = {"QUER_PROPOSTA": _FALLBACK_QUER, "SEM_INTERESSE": _FALLBACK_sem}.get(intent, _FALLBACK_amb)

    # Envia resposta
    try:
        async with get_whapi_for_card(card_fresh) as w:
            await w.send_text(phone, texto_resposta)
    except WhapiError as e:
        logger.error("agente_lp_lance: Whapi falhou para %s: %s", phone, e)
        return

    # Persiste histórico
    history = history_append(history, "assistant", texto_resposta)
    async with FaroClient() as faro:
        await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        try:
            await faro.update_card(card_id, {
                "Ultima atividade": datetime.now(timezone.utc).isoformat(),
                "Ultima resposta lead": text[:500],
            })
        except FaroError:
            pass

    # Ação por intent
    if intent == "QUER_PROPOSTA":
        logger.info("agente_lp_lance: card %s QUER proposta — movendo para PRIMEIRA_ATIVACAO", card_id[:8])
        await _notificar_slack_lance(card_fresh)
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.PRIMEIRA_ATIVACAO)
            except FaroError as e:
                logger.error("agente_lp_lance: erro ao mover card %s: %s", card_id[:8], e)

    elif intent == "SEM_INTERESSE":
        logger.info("agente_lp_lance: card %s sem interesse — movendo para PERDIDO", card_id[:8])
        async with FaroClient() as faro:
            try:
                await faro.update_card(card_id, {
                    "Motivo de perda": "SEM_INTERESSE — lead LP Lance recusou contato inicial"
                })
                await faro.move_card(card_id, Stage.PERDIDO)
            except FaroError as e:
                logger.error("agente_lp_lance: erro ao mover card %s: %s", card_id[:8], e)

    else:
        logger.info("agente_lp_lance: card %s intent AMBIGUO — aguardando resposta", card_id[:8])


async def handle_extrato_recebido(card: dict, msg) -> None:
    """
    Lead LP Lance enviou mídia (extrato) diretamente.
    Move para PRIMEIRA_ATIVACAO e deixa o qualificador processar.
    """
    card_id = card.get("id", "")
    phone   = get_phone(card)

    logger.info("agente_lp_lance: extrato recebido para card %s — movendo para PRIMEIRA_ATIVACAO", card_id[:8])
    await _notificar_slack_lance(card)

    async with FaroClient() as faro:
        try:
            await faro.move_card(card_id, Stage.PRIMEIRA_ATIVACAO)
        except FaroError as e:
            logger.error("agente_lp_lance: erro ao mover card %s: %s", card_id[:8], e)
            return

    # Re-despacha para o qualificador com o card atualizado (novo stage)
    from webhooks.qualificador import handle_qualification
    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)
    await handle_qualification(card=card_fresh, msg=msg)
