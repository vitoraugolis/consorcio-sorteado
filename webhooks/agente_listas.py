"""
webhooks/agente_listas.py — Agente SDR para leads do fluxo Listas

Responsável por todas as respostas de leads de Lista nos stages de ativação
(PRIMEIRA → QUARTA_ATIVACAO). A IA classifica intenção e age no CRM.

Mudança arquitetural: o router agora roteia TODAS as mensagens de listas aqui
(não só as ambíguas). A IA classifica com mais inteligência que keywords fixas.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from config import Stage, NOTIFY_PHONES
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm, get_fonte,
    load_history, history_append, save_history,
    build_card_context,
)
from services.whapi import WhapiClient, WhapiError
from services.slack import slack_error

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"


# ---------------------------------------------------------------------------
# Mapeamento de consultores por administradora
# ---------------------------------------------------------------------------

from config import CONSULTANT_PHONES as _ALL_CONSULTANT_PHONES

_ADM_TO_CONSULTOR: dict[str, str] = {
    "itau":  "Sônia | (11) 94788-2916",
    "itaú":  "Sônia | (11) 94788-2916",
}
_DEFAULT_CONSULTOR = "Manuela | (11) 95941-1085"


def _get_consultor_info(adm: str) -> str:
    adm_lower = (adm or "").lower()
    for key, info in _ADM_TO_CONSULTOR.items():
        if key in adm_lower:
            return info
    return _DEFAULT_CONSULTOR


# ---------------------------------------------------------------------------
# System prompt — princípios de comportamento, não script
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
Você é Manuela, consultora SDR da Consórcio Sorteado — empresa que COMPRA cotas
contempladas de consórcio há mais de 18 anos.

SITUAÇÃO: Você entrou em contato com esse lead porque identificamos que ele tem uma
cota contemplada. A primeira mensagem enviada foi uma abordagem inicial perguntando
se teria interesse em receber uma proposta. Agora você está respondendo às mensagens
que o lead enviou.

CONSULTOR RESPONSÁVEL: {consultor_info}

DADOS DO LEAD:
{dados_card}

COMO SE COMPORTAR:
- Leia o histórico da conversa antes de responder — nunca repita uma pergunta que
  o lead já respondeu, e nunca ignore algo que ele disse anteriormente.
- Adapte seu tom ao do lead: se ele é informal ("oi", "blz"), responda informalmente.
  Se é mais formal ("boa tarde"), mantenha um tom um pouco mais cuidado.
- Seja genuinamente curiosa sobre a situação do lead, não apenas transacional.
- Respostas curtas e diretas — máximo 3 linhas no WhatsApp. Nunca parágrafos longos.
- Emojis com parcimônia — só quando naturais, nunca forçados.
- Nunca revele ser IA. Se perguntada, desvie com leveza ("sou humana sim, rs").

OBJETIVO: Gerar interesse em receber uma proposta. Não negocie valores aqui —
apenas desperte curiosidade e construa confiança.

QUANDO O LEAD ACEITAR (INTERESSE):
Informe que já está encaminhando para análise e que o retorno com a proposta chega
em breve. Tom: entusiasmado, mas sem exagero.

QUANDO O LEAD RECUSAR:
Respeite. Se recusou porque vendeu/transferiu a cota, agradeça o aviso e convide
para o grupo de informações: {group_link}
Se recusou por falta de interesse, despeça-se com leveza e o mesmo convite.
Nunca insista após uma recusa clara.

QUANDO O LEAD QUISER FALAR COM HUMANO OU TIVER DÚVIDA FORA DOS DADOS:
Diga que vai acionar o consultor responsável. Classifique como REDIRECIONAR.

INFORMAÇÕES QUE VOCÊ PODE USAR (sem inventar nada além disso):
- CNPJ: 07.931.205/0001-30
- Endereço: Rua Irmã Carolina 45, Belenzinho-SP
- Compra é à vista, direto na conta do lead, antes de qualquer transferência.
- Se perguntarem como obteve o contato: "Recebemos seu contato através de parceiros
  do setor de consórcio. Se quiser ser removido(a), é só pedir — sem problema nenhum."

FORMATO DE RESPOSTA — JSON puro, sem markdown, sem texto fora do JSON:
{{
  "intent": "INTERESSE|RECUSA_COTA_VENDIDA|RECUSA_SEM_INTERESSE|REDIRECIONAR|OUTRO",
  "response": "mensagem para enviar ao lead"
}}

INTENTS:
- INTERESSE: aceita receber proposta ou demonstra vontade de vender a cota
- RECUSA_COTA_VENDIDA: já vendeu, transferiu ou cancelou a cota
- RECUSA_SEM_INTERESSE: não quer vender, pede para parar o contato
- REDIRECIONAR: quer falar com consultor humano, ou pergunta que você não consegue
  responder com os dados disponíveis
- OUTRO: qualquer outra mensagem — objeção contornável, dúvida respondível,
  desconfiança, saudação ou ambiguidade
""".strip()


# ---------------------------------------------------------------------------
# Ações de CRM por intent
# ---------------------------------------------------------------------------

async def _handle_intent(intent: str, card: dict) -> None:
    card_id = card.get("id", "")

    if intent == "INTERESSE":
        logger.info("Agente Listas: INTERESSE → PRECIFICACAO (card %s)", card_id[:8])
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.PRECIFICACAO)
                await faro.update_card(card_id, {
                    "Ultima atividade": str(int(time.time())),
                })
            except FaroError as e:
                logger.error("Erro ao mover %s para PRECIFICACAO: %s", card_id[:8], e)
                return
        # Dispara proposta imediatamente sem esperar o próximo ciclo do job
        from jobs.precificacao import send_proposal_now
        asyncio.create_task(send_proposal_now(card))

    elif intent in ("RECUSA_COTA_VENDIDA", "RECUSA_SEM_INTERESSE"):
        logger.info("Agente Listas: %s → DISPENSADOS (card %s)", intent, card_id[:8])
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.DISPENSADOS)
            except FaroError as e:
                logger.error("Erro ao mover %s para DISPENSADOS: %s", card_id[:8], e)

    elif intent == "REDIRECIONAR":
        logger.info("Agente Listas: REDIRECIONAR → FINALIZACAO_COMERCIAL (card %s)", card_id[:8])
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
            except FaroError as e:
                logger.error("Erro ao mover %s para FINALIZACAO_COMERCIAL: %s", card_id[:8], e)
        # Notifica o consultor responsável
        from webhooks.negociador import _build_handoff_notification
        notif_msg, notif_phones = _build_handoff_notification(card, "")
        if notif_phones:
            try:
                async with WhapiClient() as w:
                    for np in notif_phones:
                        await w.send_text(np, notif_msg)
            except WhapiError as e:
                logger.warning("Erro ao notificar consultor no handoff: %s", e)


# ---------------------------------------------------------------------------
# Fallbacks humanizados — usados quando a IA falha
# ---------------------------------------------------------------------------

_FALLBACKS_INTERESSE = [
    "Oba, que ótima notícia! 😊 Já estou encaminhando para análise — em breve você recebe a proposta.",
    "Ótimo! Vou mandar pra frente agora mesmo. A proposta chega em breve! 😊",
    "Perfeito! Deixa comigo, já estou cuidando disso pra você. 🙏",
]

_FALLBACKS_OUTRO = [
    "Pode me contar mais? Quero entender melhor como posso te ajudar. 😊",
    "Entendo! Me fala um pouquinho mais sobre sua situação.",
    "Claro! O que você precisar, estou aqui. Pode falar. 😊",
]

import random as _random


def _fallback_response(intent: str, nome: str) -> str:
    primeiro = nome.split()[0] if nome else "olá"
    if intent == "INTERESSE":
        return _random.choice(_FALLBACKS_INTERESSE)
    if intent in ("RECUSA_COTA_VENDIDA", "RECUSA_SEM_INTERESSE"):
        return (
            f"Entendido, {primeiro}! Sem problemas. "
            f"Se quiser acompanhar o mercado de cotas no futuro: {_GROUP_LINK} 😊"
        )
    if intent == "REDIRECIONAR":
        return f"Claro, {primeiro}! Vou acionar o consultor responsável pra você agora. 🙏"
    return _random.choice(_FALLBACKS_OUTRO)


# ---------------------------------------------------------------------------
# Core: busca contexto, chama IA, envia resposta, age no CRM
# ---------------------------------------------------------------------------

async def _respond(card: dict, texto: str) -> None:
    import json
    import re

    card_id = card.get("id", "")
    nome    = get_name(card)
    phone   = get_phone(card)
    adm     = get_adm(card)

    if not phone:
        logger.warning("Agente Listas: card %s sem telefone.", card_id[:8])
        return

    # Busca card fresco e carrega histórico
    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)

    history = load_history(card_fresh)
    history = history_append(history, "user", texto)

    system = SYSTEM_PROMPT.format(
        consultor_info=_get_consultor_info(adm),
        dados_card=build_card_context(card_fresh),
        group_link=_GROUP_LINK,
    )

    # Chama IA com histórico completo (primário + fallback automático do AIClient)
    intent         = "OUTRO"
    texto_resposta = _fallback_response("OUTRO", nome)

    try:
        async with AIClient() as ai:
            resposta_raw = await ai.complete_with_history(
                history=history,
                system=system,
                max_tokens=350,
            )

        m = re.search(r"\{.*\}", resposta_raw, re.DOTALL)
        if m:
            data           = json.loads(m.group())
            intent         = data.get("intent", "OUTRO").upper()
            texto_resposta = (data.get("response") or "").strip() or _fallback_response(intent, nome)
        else:
            logger.warning("Agente Listas: resposta sem JSON para card %s.", card_id[:8])

    except (AIError, json.JSONDecodeError, Exception) as e:
        logger.error("Agente Listas: IA falhou para card %s: %s", card_id[:8], e)
        await slack_error(
            "Falha no Agente SDR Listas",
            exception=e,
            context={"card": card_id[:12], "phone": phone},
        )
        texto_resposta = _fallback_response(intent, nome)

    # Envia mensagem ao lead
    try:
        async with WhapiClient() as w:
            await w.send_text(phone, texto_resposta)
    except WhapiError as e:
        logger.error("Agente Listas: Whapi falhou para %s: %s", phone, e)
        return

    # Persiste histórico e atividade
    history = history_append(history, "assistant", texto_resposta)
    agora   = datetime.now(timezone.utc).isoformat()

    async with FaroClient() as faro:
        await save_history(faro, card_id, history)
        try:
            await faro.update_card(card_id, {
                "Ultima atividade":    agora,
                "Ultima resposta lead": texto[:500],
            })
        except FaroError:
            pass

    # Executa ação de CRM correspondente ao intent
    await _handle_intent(intent, card_fresh)

    logger.info(
        "Agente Listas: card=%s | intent=%s | turns=%d",
        card_id[:8], intent, len(history) // 2,
    )


# ---------------------------------------------------------------------------
# Entry point chamado pelo debounce central (via router)
# ---------------------------------------------------------------------------

async def handle_message(card: dict, text: str) -> None:
    """Chamado pelo debounce central após acumulação de mensagens."""
    await _respond(card, text)
