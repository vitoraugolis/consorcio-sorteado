"""
webhooks/agente_listas.py — Agente SDR para leads do fluxo Listas
"""

import json
import logging
import random
import re
import time
from datetime import datetime, timezone

from config import Stage, NOTIFY_PHONES, SDR_MODEL
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm, get_fonte,
    history_append, history_to_text,
    build_card_context,
    load_journey, journey_to_text,
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

JORNADA DO LEAD:
{jornada}

OBJETIVO: Confirmar recebimento do interesse e informar que a proposta chegará em instantes.
NÃO faça perguntas. NÃO peça confirmação. Apenas informe com entusiasmo que a proposta será enviada.
Respostas curtas (máx 2 linhas). Nunca revele ser IA.

QUANDO O LEAD DEMONSTRAR INTERESSE (botão ou texto positivo):
- Classifique como INTERESSE
- Responda de forma entusiasmada que a proposta chegará em instantes no WhatsApp

QUANDO RECUSAR: convide para o grupo: {group_link}
QUANDO QUISER FALAR COM HUMANO: classifique como REDIRECIONAR.

QUANDO O LEAD QUISER COMPRAR UMA COTA OU IMÓVEL (QUER_COMPRAR):
Use intent QUER_COMPRAR. O lead quer COMPRAR — não vender. Responda com naturalidade
informando que vai redirecionar para um representante do departamento de venda de cotas.
NÃO tente converter para venda de cota. Apenas redirecione com cordialidade.

FORMATO JSON puro:
{{
  "intent": "INTERESSE|RECUSA_COTA_VENDIDA|RECUSA_SEM_INTERESSE|REDIRECIONAR|QUER_COMPRAR|OUTRO",
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
    if intent == "QUER_COMPRAR":
        return (
            f"Certo, {primeiro}! Vou redirecionar seu contato para um representante "
            f"comercial do departamento de venda de cotas, tudo bem? "
            f"Ele poderá te mostrar as melhores oportunidades. 😊"
        )
    return random.choice(_FALLBACKS_OUTRO)


async def _handle_intent(
    intent: str,
    card: dict,
    history: list | None = None,
    mensagem_original: str = "",
) -> None:
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
        motivo = (
            "COTA_VENDIDA — lead informou que a cota já foi vendida"
            if intent == "RECUSA_COTA_VENDIDA"
            else "SEM_INTERESSE — lead de lista recusou contato inicial"
        )
        async with FaroClient() as faro:
            try:
                await faro.update_card(card_id, {"Motivo de perda": motivo})
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
        notif_msg, notif_phones = _build_handoff_notification(card, mensagem_original, history=history)
        if notif_phones:
            try:
                async with WhapiClient(canal="lista") as w:
                    for np in notif_phones:
                        await w.send_text(np, notif_msg)
            except WhapiError as e:
                logger.warning("Erro ao notificar consultor no handoff: %s", e)

    elif intent == "QUER_COMPRAR":
        # Lead quer COMPRAR cota/imóvel — fora do escopo de venda da CS.
        # Registra o interesse na descrição e move para FINALIZACAO_COMERCIAL.
        from services.faro import history_to_text as _htt
        historico_txt = _htt(history or [], max_turns=10)
        descricao = (
            f"🏠 Lead com interesse em COMPRAR cota/imóvel (fora do escopo de venda)\n\n"
            f"Mensagem original: \"{mensagem_original[:300]}\"\n\n"
            f"📋 Histórico até o redirecionamento:\n{historico_txt}"
        )
        async with FaroClient() as faro:
            try:
                await faro.append_description(card_id, descricao)
                await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
            except FaroError as e:
                logger.error("Erro ao processar QUER_COMPRAR card %s: %s", card_id[:8], e)
        from webhooks.negociador import _build_handoff_notification
        from services.slack import slack_warning
        import asyncio
        nome  = card.get("Nome do contato") or card.get("title") or "?"
        phone = card.get("Telefone") or ""
        notif_msg, notif_phones = _build_handoff_notification(card, mensagem_original, history=history)
        notif_msg = (
            f"🏠 *Lead quer COMPRAR cota/imóvel*\n"
            f"Redirecionado automaticamente para FINALIZAÇÃO COMERCIAL.\n\n"
        ) + notif_msg
        if notif_phones:
            try:
                async with WhapiClient(canal="lista") as w:
                    for np in notif_phones:
                        await w.send_text(np, notif_msg)
            except WhapiError as e:
                logger.warning("Erro ao notificar consultor (QUER_COMPRAR): %s", e)
        asyncio.create_task(slack_warning(
            f"🏠 *Lead quer comprar cota/imóvel*\n"
            f"Lead: *{nome}* | Tel: `...{phone[-6:]}`\n"
            f"Card: `{card_id[:8]}` → FINALIZAÇÃO COMERCIAL\n"
            f"Mensagem: _{mensagem_original[:120]}_",
            context={"Card": card_id[:12], "Lead": nome},
        ))


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
        jornada=journey_to_text(load_journey(card_fresh)),
        group_link=_GROUP_LINK,
    )

    intent = "OUTRO"
    texto_resposta = _fallback_response("OUTRO", nome)

    try:
        async with AIClient() as ai:
            resposta_raw = await ai.complete_with_history(
                history=history, system=system, max_tokens=350,
                model="gpt-4o-mini", fallback_model=SDR_MODEL,
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
    historico_txt = history_to_text(history[:-1], max_turns=20)
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

    await _handle_intent(intent, card_fresh, history=history, mensagem_original=texto)

    logger.info("Agente Listas: card=%s | intent=%s | turns=%d",
                card_id[:8], intent, len(history) // 2)


async def handle_message(card: dict, text: str) -> None:
    await _respond(card, text)
