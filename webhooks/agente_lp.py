"""
webhooks/agente_lp.py -- Agente SDR para leads do fluxo LP/Site (landing page)

Wrapper sobre agente_bazar que reutiliza toda a logica de negociacao,
mas com SYSTEM_PROMPT proprio mencionando o site/landing page em vez do
"Bazar do Consorcio".

Uso identico ao agente_bazar:
    await agente_lp.handle_message(card, text)
"""

import json
import logging
import re
from datetime import datetime, timezone

# from config import Stage, NOTIFY_PHONES  # reservado para uso futuro
from services.ai import AIClient
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone,
    history_append, history_to_text,
    build_card_context,
    load_journey, journey_to_text,
)
from services.slack import slack_error
from services.whapi import WhapiError, get_whapi_for_card
from services.session_store import load_history_smart, save_history_smart
from services.safety_car import audit_response

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

SYSTEM_PROMPT_LP = """
Voce e Manuela, consultora da Consorcio Sorteado -- empresa especializada na COMPRA
de cotas contempladas de consorcio.

SITUACAO: O lead encontrou a empresa atraves do nosso site / landing page e demonstrou
interesse em vender sua cota. Voce ja enviou uma mensagem inicial apresentando o
processo. Agora esta respondendo as mensagens que ele enviou.

DADOS DO LEAD:
{dados_card}

JORNADA DO LEAD:
{jornada}

COMO SE COMPORTAR:
- Leia o historico da conversa antes de responder.
- Adapte o tom ao do lead.
- Respostas curtas. Maximo 3-4 linhas.
- Nunca revele ser IA.

OBJETIVO: Ajudar o lead a enviar o extrato atualizado da cota AQUI pelo WhatsApp.

QUANDO O LEAD RECUSAR:
Respeite. Convide para o grupo: {group_link}

QUANDO O LEAD DISSER QUE JA ENVIOU POR E-MAIL OU OUTRO CANAL:
Use intent JA_ENVIOU_OUTRO_CANAL. Responda agradecendo e informando que
o time vai verificar e que um consultor entrara em contato em breve.

QUANDO O LEAD QUISER COMPRAR UMA COTA OU IMOVEL (QUER_COMPRAR):
Use intent QUER_COMPRAR. O lead quer COMPRAR -- nao vender. Responda com naturalidade
informando que vai redirecionar para um representante do departamento de venda de cotas.
NAO tente converter para venda de cota. Apenas redirecione com cordialidade.

QUANDO PERGUNTAREM SOBRE A EMPRESA:
- CNPJ: 07.931.205/0001-30 | Rua Irma Carolina 45, Belenzinho-SP
- Compra a vista, direto na conta do lead, ANTES de qualquer transferencia.

FORMATO -- JSON puro, sem markdown, sem texto fora do JSON:
{{
  "intent": "AGUARDANDO_EXTRATO|JA_ENVIOU_OUTRO_CANAL|RECUSA_COTA_VENDIDA|RECUSA_SEM_INTERESSE|REDIRECIONAR|QUER_COMPRAR|OUTRO",
  "response": "mensagem para enviar ao lead"
}}
""".strip()

# Reutiliza fallbacks e logica de intent do agente_bazar
from webhooks.agente_bazar import (
    _fallback_response,
    _handle_intent,
)


async def _respond_lp(card: dict, texto: str) -> None:
    """Mesma logica de agente_bazar._respond mas usa SYSTEM_PROMPT_LP."""
    card_id = card.get("id", "")
    nome = get_name(card)
    phone = get_phone(card)

    if not phone:
        logger.warning("Agente LP: card %s sem telefone.", card_id[:8])
        return

    # Busca card fresco + historico num unico FaroClient
    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)

    history = await load_history_smart(phone, card_fresh)
    history = history_append(history, "user", texto)

    system = SYSTEM_PROMPT_LP.format(
        dados_card=build_card_context(card_fresh),
        jornada=journey_to_text(load_journey(card_fresh)),
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
            raw_clean = re.sub(r"```(?:json)?|```", "", resposta_raw).strip()
            m = re.search(r"\{.*\}", raw_clean, re.DOTALL)
            if m:
                data = json.loads(m.group())
                intent = data.get("intent", "OUTRO").upper()
                texto_resposta = (data.get("response") or "").strip() or _fallback_response(intent, nome)
            else:
                logger.warning("Agente LP: resposta sem JSON para card %s -- usando texto direto.", card_id[:8])
                texto_resposta = resposta_raw.strip() or _fallback_response("OUTRO", nome)
    except Exception as e:
        logger.error("Agente LP: IA falhou para card %s: %s", card_id[:8], e)
        await slack_error("Falha no Agente SDR LP", exception=e,
                          context={"card": card_id[:12], "phone": phone})
        texto_resposta = _fallback_response(intent, nome)

    # Safety Car: audita resposta antes de enviar
    historico_txt = history_to_text(history[:-1], max_turns=20)
    audit = await audit_response(texto_resposta, card_fresh, historico_txt, agente="agente_lp")
    texto_resposta = audit.mensagem_final

    # Envia via Whapi canal LP
    try:
        async with get_whapi_for_card(card_fresh) as w:
            await w.send_text(phone, texto_resposta)
    except WhapiError as e:
        logger.error("Agente LP: Whapi falhou para %s: %s", phone, e)
        return

    history = history_append(history, "assistant", texto_resposta)
    agora = datetime.now(timezone.utc).isoformat()

    # Persiste historico e atividade
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

    logger.info("Agente LP: card=%s | intent=%s | turns=%d",
                card_id[:8], intent, len(history) // 2)


async def handle_message(card: dict, text: str) -> None:
    await _respond_lp(card, text)
