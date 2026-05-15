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
from config import SDR_MODEL
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
from services.agent_knowledge import get_knowledge_for_agent

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

SYSTEM_PROMPT_LP = """
{knowledge}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 SEU PAPEL AGORA — AGENTE SDR LP/SITE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SITUAÇÃO: Lead encontrou a empresa via site/landing page e demonstrou interesse
em vender sua cota. Você já enviou uma mensagem inicial apresentando o processo.

DADOS DO LEAD:
{{dados_card}}

JORNADA DO LEAD:
{{jornada}}

OBJETIVO: Ajudar o lead a enviar o extrato atualizado da cota aqui pelo WhatsApp.

QUANDO O LEAD RECUSAR:
Respeite. Convide para o grupo: {{group_link}}

QUANDO DISSER QUE JÁ ENVIOU POR OUTRO CANAL (JA_ENVIOU_OUTRO_CANAL):
Agradeça e informe que o time vai verificar.

QUANDO O LEAD QUISER COMPRAR UMA COTA OU IMÓVEL (QUER_COMPRAR):
Informe que vai redirecionar para o departamento de venda de cotas.

QUANDO PERGUNTAREM SOBRE A EMPRESA:
CNPJ: 07.931.205/0001-30 | Rua Irmã Carolina 45, Belenzinho-SP

PARA QUALQUER SITUAÇÃO FORA DO COMUM (reclamação, dúvida, objeção, desconfiança):
Use seu conhecimento completo do sistema. Você tem AUTONOMIA para mover o lead
para o stage mais adequado conforme o mapa de stages acima.

FORMATO — JSON puro:
{{{{
  "intent": "AGUARDANDO_EXTRATO|JA_ENVIOU_OUTRO_CANAL|RECUSA_COTA_VENDIDA|RECUSA_SEM_INTERESSE|REDIRECIONAR|QUER_COMPRAR|OUTRO",
  "response": "mensagem para enviar ao lead"
}}}}
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

    # ── Guarda de mídia em processamento ────────────────────────────────────
    from services.session_store import is_media_processing
    if await is_media_processing(phone):
        logger.info(
            "Agente LP: card %s — media_lock ativo, silenciando resposta de texto durante análise de extrato.",
            card_id[:8],
        )
        async with FaroClient() as faro:
            card_fresh = await faro.get_card(card_id)
        history = await load_history_smart(phone, card_fresh)
        history = history_append(history, "user", texto)
        async with FaroClient() as faro:
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        return

    # Busca card fresco + historico num unico FaroClient
    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)

    history = await load_history_smart(phone, card_fresh)
    history = history_append(history, "user", texto)

    system = SYSTEM_PROMPT_LP.format(
        knowledge=get_knowledge_for_agent("sdr_lp"),
    ).format(
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
                model=SDR_MODEL,
                fallback_model=SDR_MODEL,
            )
            # ── Extração robusta de JSON com retry de instrução ──────────────
            raw_clean = re.sub(r"```(?:json)?|```", "", resposta_raw).strip()
            m = re.search(r"\{.*\}", raw_clean, re.DOTALL)
            parsed_json: dict | None = None
            if m:
                try:
                    parsed_json = json.loads(m.group())
                except json.JSONDecodeError:
                    parsed_json = None

            if parsed_json is None:
                logger.warning(
                    "Agente LP: resposta sem JSON válido para card %s — solicitando retry JSON.",
                    card_id[:8],
                )
                retry_prompt = (
                    "Sua resposta anterior não estava no formato JSON solicitado. "
                    "Reescreva APENAS o JSON abaixo, sem markdown, sem texto antes ou depois:\n"
                    '{"intent": "<INTENT>", "response": "<mensagem ao lead>"}\n\n'
                    f"Resposta anterior: {resposta_raw[:300]}"
                )
                try:
                    async with AIClient() as ai2:
                        resposta_raw2 = await ai2.complete(
                            prompt=retry_prompt,
                            system="Retorne EXCLUSIVAMENTE JSON válido. Sem markdown. Sem texto fora do JSON.",
                            max_tokens=350,
                            model=SDR_MODEL,
                        )
                    raw2_clean = re.sub(r"```(?:json)?|```", "", resposta_raw2).strip()
                    m2 = re.search(r"\{.*\}", raw2_clean, re.DOTALL)
                    if m2:
                        parsed_json = json.loads(m2.group())
                except Exception as retry_exc:
                    logger.error("Agente LP: retry JSON também falhou para card %s: %s", card_id[:8], retry_exc)

            if parsed_json is not None:
                intent = str(parsed_json.get("intent", "OUTRO")).upper()
                texto_resposta = (str(parsed_json.get("response") or "")).strip() or _fallback_response(intent, nome)
            else:
                logger.error(
                    "Agente LP: JSON não obtido após 2 tentativas para card %s — usando fallback.",
                    card_id[:8],
                )
                texto_resposta = _fallback_response("OUTRO", nome)

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
