"""
webhooks/agente_bazar.py — Agente SDR para leads do fluxo Bazar/Site
"""

import json
import logging
import random
import re
from datetime import datetime, timezone

from config import Stage, NOTIFY_PHONES, SDR_MODEL
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm,
    history_append, history_to_text,
    build_card_context,
    load_journey, journey_to_text,
)
from services.slack import slack_error
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card
from services.session_store import load_history_smart, save_history_smart
from services.safety_car import audit_response
from services.agent_knowledge import get_knowledge_for_agent

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

SYSTEM_PROMPT = """
{knowledge}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 SEU PAPEL AGORA — AGENTE SDR BAZAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SITUAÇÃO: Lead veio pela Bazar do Consórcio e demonstrou interesse em vender sua
cota. Você já enviou uma mensagem inicial apresentando o processo.

DADOS DO LEAD:
{dados_card}

JORNADA DO LEAD:
{jornada}

OBJETIVO: Ajudar o lead a enviar o extrato atualizado da cota aqui pelo WhatsApp.

QUANDO O LEAD DISSER QUE JÁ VENDEU A COTA (RECUSA_COTA_VENDIDA):
Agradeça o retorno e deixe a porta aberta: continuamos à disposição caso tenha
outra cota contemplada no futuro. Tom cordial e breve.

QUANDO O LEAD NÃO TIVER INTERESSE (RECUSA_SEM_INTERESSE):
Respeite. Convide para o grupo: {group_link}

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
{{
  "intent": "AGUARDANDO_EXTRATO|JA_ENVIOU_OUTRO_CANAL|RECUSA_COTA_VENDIDA|RECUSA_SEM_INTERESSE|REDIRECIONAR|QUER_COMPRAR|OUTRO",
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

_FALLBACK_JA_ENVIOU = (
    "Obrigada, {nome}! Vou avisar nosso time para verificar. "
    "Um consultor entrará em contato em breve. 😊"
)


def _fallback_response(intent: str, nome: str) -> str:
    primeiro = nome.split()[0] if nome else ""
    if intent == "RECUSA_COTA_VENDIDA":
        return (
            f"Obrigada pelo retorno, {primeiro}! Fico feliz que tenha conseguido negociar. 😊 "
            f"Continuamos à disposição caso tenha outra cota contemplada com interesse em negociar — "
            f"agora ou no futuro. Qualquer coisa é só chamar! 🙏"
        )
    if intent == "RECUSA_SEM_INTERESSE":
        return f"Entendido, {primeiro}! Sem problemas. Se quiser no futuro: {_GROUP_LINK} 😊"
    if intent == "REDIRECIONAR":
        return f"Claro{', ' + primeiro if primeiro else ''}! Vou acionar o consultor responsável agora. 🙏"
    if intent == "AGUARDANDO_EXTRATO":
        return random.choice(_FALLBACKS_AGUARDANDO)
    if intent == "JA_ENVIOU_OUTRO_CANAL":
        return _FALLBACK_JA_ENVIOU.format(nome=primeiro or "")
    if intent == "QUER_COMPRAR":
        return (
            f"Certo{', ' + primeiro if primeiro else ''}! Vou redirecionar seu contato para um "
            f"representante comercial do departamento de venda de cotas, tudo bem? "
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
    if intent in ("RECUSA_COTA_VENDIDA", "RECUSA_SEM_INTERESSE"):
        motivo = (
            "COTA_VENDIDA — lead informou que a cota já foi vendida"
            if intent == "RECUSA_COTA_VENDIDA"
            else "SEM_INTERESSE — lead não tem interesse em vender no momento"
        )
        async with FaroClient() as faro:
            try:
                await faro.update_card(card_id, {"Motivo de perda": motivo})
                await faro.move_card(card_id, Stage.PERDIDO)
            except FaroError as e:
                logger.error("Erro ao mover %s para PERDIDO: %s", card_id[:8], e)

    elif intent == "JA_ENVIOU_OUTRO_CANAL":
        # Lead disse que já enviou por e-mail ou outro canal → handoff comercial
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
            except FaroError as e:
                logger.error("Erro ao mover %s para FINALIZACAO_COMERCIAL: %s", card_id[:8], e)
        nome  = card.get("Nome do contato") or card.get("title") or "?"
        adm   = card.get("Adm") or "?"
        phone = card.get("Telefone") or ""
        from services.whapi import notify_team
        await notify_team(
            f"📧 *Lead enviou extrato por outro canal — verificar*\n"
            f"Lead: {nome} | Adm: {adm} | Tel: {phone}\n"
            f"Card: `{card_id[:8]}` | Movido para FINALIZACAO COMERCIAL"
        )

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
                async with get_whapi_for_card(card) as w:
                    for np in notif_phones:
                        await w.send_text(np, notif_msg)
            except WhapiError as e:
                logger.warning("Erro ao notificar consultor: %s", e)

    elif intent == "QUER_COMPRAR":
        # Lead quer COMPRAR cota/imóvel — fora do escopo de venda da CS.
        # Registra o interesse na descrição do card e move para FINALIZACAO_COMERCIAL.
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
        nome  = card.get("Nome do contato") or card.get("title") or "?"
        phone = card.get("Telefone") or ""
        notif_msg, notif_phones = _build_handoff_notification(card, mensagem_original, history=history)
        # Prefixa a notificação com contexto de compra
        notif_msg = (
            f"🏠 *Lead quer COMPRAR cota/imóvel*\n"
            f"Redirecionado automaticamente para FINALIZAÇÃO COMERCIAL.\n\n"
        ) + notif_msg
        if notif_phones:
            try:
                async with get_whapi_for_card(card) as w:
                    for np in notif_phones:
                        await w.send_text(np, notif_msg)
            except WhapiError as e:
                logger.warning("Erro ao notificar consultor (QUER_COMPRAR): %s", e)
        import asyncio
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

    if not phone:
        logger.warning("Agente Bazar: card %s sem telefone.", card_id[:8])
        return

    # ── Guarda de mídia em processamento ────────────────────────────────────
    # Se o qualificador está analisando um extrato deste lead (lock ativo),
    # registra o turno no histórico mas não envia resposta. Evita respostas
    # incoerentes enquanto o Gemini processa (buffer 30s + retries até 45s).
    from services.session_store import is_media_processing
    if await is_media_processing(phone):
        logger.info(
            "Agente Bazar: card %s — media_lock ativo, silenciando resposta de texto durante análise de extrato.",
            card_id[:8],
        )
        # Registra o turno no histórico para não perder o contexto
        async with FaroClient() as faro:
            card_fresh = await faro.get_card(card_id)
        history = await load_history_smart(phone, card_fresh)
        history = history_append(history, "user", texto)
        async with FaroClient() as faro:
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        return

    # Busca card fresco + histórico num único FaroClient
    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)

    history = await load_history_smart(phone, card_fresh)
    history = history_append(history, "user", texto)

    system = SYSTEM_PROMPT.format(
        knowledge=get_knowledge_for_agent("sdr_bazar"),
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
            # Tentativa 1: regex no raw (tolera markdown ```json e texto antes/depois)
            raw_clean = re.sub(r"```(?:json)?|```", "", resposta_raw).strip()
            m = re.search(r"\{.*\}", raw_clean, re.DOTALL)
            parsed_json: dict | None = None
            if m:
                try:
                    parsed_json = json.loads(m.group())
                except json.JSONDecodeError:
                    parsed_json = None

            if parsed_json is None:
                # Tentativa 2: pede ao modelo explicitamente para reemitir como JSON
                logger.warning(
                    "Agente Bazar: resposta sem JSON válido para card %s — solicitando retry JSON.",
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
                    logger.error("Agente Bazar: retry JSON também falhou para card %s: %s", card_id[:8], retry_exc)

            if parsed_json is not None:
                intent = str(parsed_json.get("intent", "OUTRO")).upper()
                texto_resposta = (str(parsed_json.get("response") or "")).strip() or _fallback_response(intent, nome)
            else:
                # Ambas tentativas falharam — usa fallback seguro, não texto raw do modelo
                logger.error(
                    "Agente Bazar: JSON não obtido após 2 tentativas para card %s — usando fallback.",
                    card_id[:8],
                )
                texto_resposta = _fallback_response("OUTRO", nome)

    except Exception as e:
        logger.error("Agente Bazar: IA falhou para card %s: %s", card_id[:8], e)
        await slack_error("Falha no Agente SDR Bazar", exception=e,
                          context={"card": card_id[:12], "phone": phone})
        texto_resposta = _fallback_response(intent, nome)

    # ── Safety Car: audita resposta antes de enviar ──────────────────────────
    historico_txt = history_to_text(history[:-1], max_turns=20)
    audit = await audit_response(texto_resposta, card_fresh, historico_txt, agente="agente_bazar")
    texto_resposta = audit.mensagem_final

    # ── Executa a mudança de stage ANTES de tentar enviar a mensagem ────────
    # Garante que mudanças de stage críticas (ex: INTERESSE → PRECIFICACAO)
    # aconteçam mesmo se o Whapi falhar na confirmação de texto.
    await _handle_intent(intent, card_fresh, history=history, mensagem_original=texto)

    # Envia via Whapi canal bazar
    whapi_ok = False
    try:
        async with get_whapi_for_card(card_fresh) as w:
            await w.send_text(phone, texto_resposta)
        whapi_ok = True
    except WhapiError as e:
        logger.error("Agente Bazar: Whapi falhou para %s: %s", phone, e)

    if whapi_ok:
        history = history_append(history, "assistant", texto_resposta)
        agora = datetime.now(timezone.utc).isoformat()

        # Persiste histórico e atividade num único FaroClient
        async with FaroClient() as faro:
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
            try:
                await faro.update_card(card_id, {
                    "Ultima atividade": agora,
                    "Ultima resposta lead": texto[:500],
                })
            except FaroError:
                pass

    logger.info("Agente Bazar: card=%s | intent=%s | turns=%d | whapi_ok=%s",
                card_id[:8], intent, len(history) // 2, whapi_ok)


async def handle_message(card: dict, text: str) -> None:
    await _respond(card, text)
