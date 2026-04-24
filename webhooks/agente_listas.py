"""
webhooks/agente_listas.py — Agente SDR para leads do fluxo Listas
"""

import json
import logging
import random
import re
import time
from datetime import datetime, timezone

from config import Stage, NOTIFY_PHONES
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm, get_fonte,
    history_append, history_to_text,
    build_card_context,
)
from services.whapi import WhapiClient, WhapiError
from services.slack import slack_error
from services.session_store import load_history_smart, save_history_smart
from services.safety_car import audit_response

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

from config import CONSULTANT_PHONES as _ALL_CONSULTANT_PHONES

_ADM_TO_CONSULTOR: dict[str, str] = {
    "itau": "Sônia | (11) 94788-2916",
    "itaú": "Sônia | (11) 94788-2916",
}
_DEFAULT_CONSULTOR = "Manuela | (11) 95941-1085"


def _get_consultor_info(adm: str) -> str:
    adm_lower = (adm or "").lower()
    for key, info in _ADM_TO_CONSULTOR.items():
        if key in adm_lower:
            return info
    return _DEFAULT_CONSULTOR


SYSTEM_PROMPT = """
Você é Manuela, consultora SDR da Consórcio Sorteado.

CONSULTOR RESPONSÁVEL: {consultor_info}

DADOS DO LEAD:
{dados_card}

OBJETIVO: Confirmar recebimento do interesse e informar que a proposta chegará em instantes.
NÃO faça perguntas. NÃO peça confirmação. Apenas informe com entusiasmo que a proposta será enviada.
Respostas curtas (máx 2 linhas). Nunca revele ser IA.

QUANDO O LEAD DEMONSTRAR INTERESSE (botão ou texto positivo):
- Classifique como INTERESSE
- Responda de forma entusiasmada que a proposta chegará em instantes no WhatsApp

QUANDO RECUSAR: convide para o grupo: {group_link}
QUANDO QUISER FALAR COM HUMANO: classifique como REDIRECIONAR.

FORMATO JSON puro:
{{
  "intent": "INTERESSE|RECUSA_COTA_VENDIDA|RECUSA_SEM_INTERESSE|REDIRECIONAR|OUTRO",
  "response": "mensagem para enviar ao lead"
}}
""".strip()

_FALLBACKS_INTERESSE = [
    "Oba! 🎉 Que ótima notícia! Sua proposta está sendo preparada e chegará aqui em instantes!",
    "Perfeito! 🙌 Já encaminhei para nosso time — sua proposta personalizada chegará em instantes!",
]

_FALLBACKS_OUTRO = [
    "Pode me contar mais? Quero entender melhor como posso te ajudar. 😊",
    "Entendo! Me fala um pouquinho mais sobre sua situação.",
]


def _fallback_response(intent: str, nome: str) -> str:
    primeiro = nome.split()[0] if nome else "olá"
    if intent == "INTERESSE":
        return random.choice(_FALLBACKS_INTERESSE)
    if intent in ("RECUSA_COTA_VENDIDA", "RECUSA_SEM_INTERESSE"):
        return f"Entendido, {primeiro}! Sem problemas. Se quiser no futuro: {_GROUP_LINK} 😊"
    if intent == "REDIRECIONAR":
        return f"Claro, {primeiro}! Vou acionar o consultor responsável pra você agora. 🙏"
    return random.choice(_FALLBACKS_OUTRO)


async def _handle_intent(intent: str, card: dict) -> None:
    card_id = card.get("id", "")
    if intent == "INTERESSE":
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.PRECIFICACAO)
                await faro.update_card(card_id, {"Ultima atividade": str(int(time.time()))})
            except FaroError as e:
                logger.error("Erro ao mover %s para PRECIFICACAO: %s", card_id[:8], e)
                return
        # Listas: proposta enviada pelo job de precificação quando
        # a equipe preencher "Proposta Realizada" no FARO.
        logger.info("Agente Listas: card %s → PRECIFICACAO (aguarda proposta manual)", card_id[:8])
    elif intent in ("RECUSA_COTA_VENDIDA", "RECUSA_SEM_INTERESSE"):
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.DISPENSADOS)
            except FaroError as e:
                logger.error("Erro ao mover %s para DISPENSADOS: %s", card_id[:8], e)
    elif intent == "REDIRECIONAR":
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
            except FaroError as e:
                logger.error("Erro ao mover %s para FINALIZACAO_COMERCIAL: %s", card_id[:8], e)
        from webhooks.negociador import _build_handoff_notification
        notif_msg, notif_phones = _build_handoff_notification(card, "")
        if notif_phones:
            try:
                async with WhapiClient(canal="lista") as w:
                    for np in notif_phones:
                        await w.send_text(np, notif_msg)
            except WhapiError as e:
                logger.warning("Erro ao notificar consultor no handoff: %s", e)


async def _respond(card: dict, texto: str) -> None:
    card_id = card.get("id", "")
    nome = get_name(card)
    phone = get_phone(card)
    adm = get_adm(card)

    if not phone:
        logger.warning("Agente Listas: card %s sem telefone.", card_id[:8])
        return

    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)

    history = await load_history_smart(phone, card_fresh)
    history = history_append(history, "user", texto)

    system = SYSTEM_PROMPT.format(
        consultor_info=_get_consultor_info(adm),
        dados_card=build_card_context(card_fresh),
        group_link=_GROUP_LINK,
    )

    intent = "OUTRO"
    texto_resposta = _fallback_response("OUTRO", nome)

    try:
        async with AIClient() as ai:
            resposta_raw = await ai.complete_with_history(
                history=history, system=system, max_tokens=350,
                model="gpt-4o-mini", fallback_model="gpt-4o-mini",
            )
            m = re.search(r"\{.*\}", resposta_raw, re.DOTALL)
            if m:
                data = json.loads(m.group())
                intent = data.get("intent", "OUTRO").upper()
                texto_resposta = (data.get("response") or "").strip() or _fallback_response(intent, nome)
            else:
                logger.warning("Agente Listas: resposta sem JSON para card %s.", card_id[:8])
    except Exception as e:
        logger.error("Agente Listas: IA falhou para card %s: %s", card_id[:8], e)
        await slack_error("Falha no Agente SDR Listas", exception=e,
                          context={"card": card_id[:12], "phone": phone})
        texto_resposta = _fallback_response(intent, nome)

    # ── Safety Car: audita resposta antes de enviar ──────────────────────────
    historico_txt = history_to_text(history[:-1], max_turns=6)  # exclui última msg do user
    audit = await audit_response(texto_resposta, card_fresh, historico_txt, agente="agente_listas")
    texto_resposta = audit.mensagem_final

    try:
        async with WhapiClient(canal="lista") as w:
            await w.send_text(phone, texto_resposta)
    except WhapiError as e:
        logger.error("Agente Listas: Whapi falhou para %s: %s", phone, e)
        return

    history = history_append(history, "assistant", texto_resposta)
    agora = datetime.now(timezone.utc).isoformat()

    async with FaroClient() as faro:
        await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        try:
            await faro.update_card(card_id, {
                "Ultima atividade": agora,
                "Ultima resposta lead": texto[:500],
            })
        except FaroError:
            pass

    await _handle_intent(intent, card_fresh)

    logger.info("Agente Listas: card=%s | intent=%s | turns=%d",
                card_id[:8], intent, len(history) // 2)


async def handle_message(card: dict, text: str) -> None:
    await _respond(card, text)
