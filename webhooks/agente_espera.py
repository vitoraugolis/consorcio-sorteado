"""
webhooks/agente_espera.py -- Responde leads em stage ESPERA (aguardando extrato LP)

Comportamento:
- Usa IA (GPT-4o-mini) para gerar respostas contextuais com historico completo
- Se o lead recusa: move para DISPENSADOS e despede-se
- Se o lead tem duvida sobre extrato: orienta
- Fallback estatico se IA falhar
"""

import json
import logging
import re
from datetime import datetime, timezone

from config import Stage
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_adm, get_phone,
    history_append, history_to_text,
    build_card_context,
    load_journey, journey_to_text,
)
from services.whapi import WhapiClient, WhapiError
from services.session_store import load_history_smart, save_history_smart
from services.safety_car import audit_response

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

_RECUSA_KEYWORDS = [
    "nao quero", "nao quero mais", "desistir", "cancelar", "me remova",
    "sem interesse", "nao tenho mais interesse", "pode tirar",
    "parem", "para de enviar", "nao tenho interesse",
]

SYSTEM_PROMPT = """
Voce e Manuela, consultora da Consorcio Sorteado.

O lead entrou em contato pelo site e demonstrou interesse em vender sua cota contemplada.
Voce ja enviou uma mensagem inicial solicitando o extrato da cota. Agora esta respondendo
as mensagens enviadas pelo lead enquanto ele ainda nao enviou o extrato.

DADOS DO LEAD:
{dados_card}

JORNADA DO LEAD:
{jornada}

OBJETIVO: Manter o lead engajado e motivado a enviar o extrato da cota.
Lembre gentilmente que aguardamos o extrato. Seja empatico e natural.
Use o nome do lead se souber. Nunca revele ser IA.

REGRAS:
- Respostas curtas (max 3 linhas). Tom de WhatsApp, pessoal.
- Se o lead tiver duvidas sobre como obter o extrato, oriente brevemente.
- Se o lead demonstrar recusa ou desinteresse: classifique como RECUSA.

FORMATO JSON puro (sem markdown):
{{"intent": "AGUARDANDO_EXTRATO|RECUSA|DUVIDA_EXTRATO|OUTRO", "response": "mensagem para o lead"}}
""".strip()

_FALLBACK_AGUARDANDO = (
    "Ola{nome_fmt}! Ainda aguardamos o seu extrato da {adm} para fazer a analise "
    "e te passar uma proposta personalizada. "
    "Assim que enviar (PDF ou foto do app), damos sequencia rapidinho! 🚀"
)

_FALLBACK_RECUSA = (
    "Tudo bem{nome_fmt}! Entendido. 😊 Caso mude de ideia no futuro, e so nos chamar. "
    "E se quiser acompanhar as melhores oportunidades de consorcio, "
    "temos um grupo com novidades: {group_link}"
)


async def handle_message(card: dict, mensagem: str) -> None:
    nome     = get_name(card)
    primeiro = nome.split()[0] if nome else ""
    nome_fmt = f", {primeiro}" if primeiro else ""
    adm      = get_adm(card) or "sua administradora"
    phone    = get_phone(card)
    card_id  = card.get("id", "")

    if not phone:
        return

    history = await load_history_smart(phone, card)

    texto_lower = mensagem.lower()
    recusa_keyword = any(k in texto_lower for k in _RECUSA_KEYWORDS)

    intent = "OUTRO"
    msg = _FALLBACK_AGUARDANDO.format(nome_fmt=nome_fmt, adm=adm)

    try:
        system = SYSTEM_PROMPT.format(
            dados_card=build_card_context(card),
            jornada=journey_to_text(load_journey(card)),
        )
        history_com_user = history_append(list(history), "user", mensagem)

        async with AIClient() as ai:
            resposta_raw = await ai.complete_with_history(
                history=history_com_user,
                system=system,
                max_tokens=200,
                model="gpt-4o-mini",
                fallback_model="gpt-4o-mini",
            )

        raw_clean = re.sub(r"```(?:json)?|```", "", resposta_raw).strip()
        m_json = re.search(r"\{.*\}", raw_clean, re.DOTALL)
        if m_json:
            data = json.loads(m_json.group())
            intent = data.get("intent", "OUTRO").upper()
            msg = (data.get("response") or "").strip() or msg
        else:
            msg = resposta_raw.strip() or msg
            logger.warning("agente_espera: resposta sem JSON para card %s", card_id[:8])

    except Exception as e:
        logger.error("agente_espera: IA falhou para card %s: %s", card_id[:8], e)
        if recusa_keyword:
            intent = "RECUSA"
            msg = _FALLBACK_RECUSA.format(nome_fmt=nome_fmt, group_link=_GROUP_LINK)

    if recusa_keyword:
        intent = "RECUSA"

    historico_txt = history_to_text(history, max_turns=10)
    audit = await audit_response(msg, card, historico_txt, agente="agente_espera")
    msg = audit.mensagem_final

    try:
        async with WhapiClient(canal="lp") as w:
            await w.send_text(phone, msg)
    except WhapiError as e:
        logger.error("agente_espera: erro Whapi card %s: %s", card_id[:8], e)
        return

    history = history_append(history, "user", mensagem)
    history = history_append(history, "assistant", msg)
    agora = datetime.now(timezone.utc).isoformat()

    async with FaroClient() as faro:
        await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        if intent == "RECUSA":
            try:
                await faro.move_card(card_id, Stage.DISPENSADOS)
                await faro.update_card(card_id, {
                    "Motivo de perda": "SEM_INTERESSE — recusa no stage ESPERA",
                })
                logger.info("agente_espera: card %s movido para DISPENSADOS (recusa)", card_id[:8])
            except FaroError as e:
                logger.error("agente_espera: erro ao mover %s para DISPENSADOS: %s", card_id[:8], e)
        else:
            try:
                await faro.update_card(card_id, {"Ultima atividade": agora})
            except FaroError:
                pass

    logger.info("agente_espera: card=%s | intent=%s", card_id[:8], intent)
