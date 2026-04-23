"""
webhooks/agente_bazar.py — Agente SDR para leads do fluxo Bazar/Site
"""

import json
import logging
import random
import re
from datetime import datetime, timezone

from config import Stage, NOTIFY_PHONES
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm,
    load_history, history_append, save_history,
    build_card_context,
)
from services.slack import slack_error
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

SYSTEM_PROMPT = """
Você é Manuela, consultora da Consórcio Sorteado — empresa especializada na COMPRA
de cotas contempladas de consórcio, parceira da Bazar do Consórcio há anos.

SITUAÇÃO: O lead veio pela Bazar do Consórcio e demonstrou interesse em vender sua
cota. Você já enviou uma mensagem inicial apresentando o processo. Agora está
respondendo às mensagens que ele enviou.

DADOS DO LEAD:
{dados_card}

COMO SE COMPORTAR:
- Leia o histórico da conversa antes de responder.
- Adapte o tom ao do lead.
- Respostas curtas. Máximo 3-4 linhas.
- Nunca revele ser IA.

OBJETIVO: Ajudar o lead a enviar o extrato atualizado da cota.

QUANDO O LEAD RECUSAR:
Respeite. Convide para o grupo: {group_link}

QUANDO PERGUNTAREM SOBRE A EMPRESA:
- CNPJ: 07.931.205/0001-30 | Rua Irmã Carolina 45, Belenzinho-SP
- Compra à vista, direto na conta do lead, ANTES de qualquer transferência.

FORMATO — JSON puro:
{{
  "intent": "AGUARDANDO_EXTRATO|RECUSA_COTA_VENDIDA|RECUSA_SEM_INTERESSE|REDIRECIONAR|OUTRO",
  "response": "mensagem para enviar ao lead"
}}
""".strip()

_FALLBACKS_AGUARDANDO = [
    "Perfeito! Assim que você tiver o extrato, pode me enviar aqui mesmo. Qualquer dúvida estou aqui! 😊",
    "Ótimo! Pode me mandar o extrato quando tiver — analiso rapidinho. 🙏",
]

_FALLBACKS_OUTRO = [
    "Pode me contar mais? Assim consigo te ajudar melhor. 😊",
    "Entendido! O que você precisar, pode falar. 🙏",
]


def _fallback_response(intent: str, nome: str) -> str:
    primeiro = nome.split()[0] if nome else ""
    if intent in ("RECUSA_COTA_VENDIDA", "RECUSA_SEM_INTERESSE"):
        return f"Entendido, {primeiro}! Sem problemas. Se quiser no futuro: {_GROUP_LINK} 😊"
    if intent == "REDIRECIONAR":
        return f"Claro{', ' + primeiro if primeiro else ''}! Vou acionar o consultor responsável agora. 🙏"
    if intent == "AGUARDANDO_EXTRATO":
        return random.choice(_FALLBACKS_AGUARDANDO)
    return random.choice(_FALLBACKS_OUTRO)


async def _handle_intent(intent: str, card: dict) -> None:
    card_id = card.get("id", "")
    if intent in ("RECUSA_COTA_VENDIDA", "RECUSA_SEM_INTERESSE"):
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.PERDIDO)
            except FaroError as e:
                logger.error("Erro ao mover %s para PERDIDO: %s", card_id[:8], e)
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
                logger.warning("Erro ao notificar consultor: %s", e)


async def _respond(card: dict, texto: str) -> None:
    card_id = card.get("id", "")
    nome = get_name(card)
    phone = get_phone(card)

    if not phone:
        logger.warning("Agente Bazar: card %s sem telefone.", card_id[:8])
        return

    # Busca card fresco + histórico num único FaroClient
    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)

    history = load_history(card_fresh)
    history = history_append(history, "user", texto)

    system = SYSTEM_PROMPT.format(
        dados_card=build_card_context(card_fresh),
        group_link=_GROUP_LINK,
    )

    intent = "OUTRO"
    texto_resposta = _fallback_response("OUTRO", nome)
    try:
        async with AIClient() as ai:
            resposta_raw = await ai.complete_with_history(
                history=history,
                system=system,
                max_tokens=350,
                model="gpt-4o-mini",
                fallback_model="gpt-4o-mini",
            )
            m = re.search(r"\{.*\}", resposta_raw, re.DOTALL)
            if m:
                data = json.loads(m.group())
                intent = data.get("intent", "OUTRO").upper()
                texto_resposta = (data.get("response") or "").strip() or _fallback_response(intent, nome)
            else:
                logger.warning("Agente Bazar: resposta sem JSON para card %s.", card_id[:8])
    except Exception as e:
        logger.error("Agente Bazar: IA falhou para card %s: %s", card_id[:8], e)
        await slack_error("Falha no Agente SDR Bazar", exception=e,
                          context={"card": card_id[:12], "phone": phone})
        texto_resposta = _fallback_response(intent, nome)

    # Envia via Whapi canal bazar
    try:
        async with get_whapi_for_card(card_fresh) as w:
            await w.send_text(phone, texto_resposta)
    except WhapiError as e:
        logger.error("Agente Bazar: Whapi falhou para %s: %s", phone, e)
        return

    history = history_append(history, "assistant", texto_resposta)
    agora = datetime.now(timezone.utc).isoformat()

    # Persiste histórico e atividade num único FaroClient
    async with FaroClient() as faro:
        await save_history(faro, card_id, history)
        try:
            await faro.update_card(card_id, {
                "Ultima atividade": agora,
                "Ultima resposta lead": texto[:500],
            })
        except FaroError:
            pass

    await _handle_intent(intent, card_fresh)

    logger.info("Agente Bazar: card=%s | intent=%s | turns=%d",
                card_id[:8], intent, len(history) // 2)


async def handle_message(card: dict, text: str) -> None:
    await _respond(card, text)
