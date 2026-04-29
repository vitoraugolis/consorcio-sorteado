"""
webhooks/router.py — Roteador central de mensagens WhatsApp recebidas

Recebe payloads do Whapi, normaliza para IncomingMessage e despacha
para o handler correto baseado no stage do lead no FARO.

Endpoint único: POST /webhook/whapi
(Z-API removido — todos os fluxos agora usam Whapi)
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from config import Stage
from services.faro import FaroClient, FaroError, is_lista, get_name, get_canal
from webhooks.negociador import handle_message
from webhooks.qualificador import handle_qualification, QUALIFICATION_STAGES
from webhooks.agente_contrato import handle_dados_pessoais, handle_extrato_recebido
from webhooks import debounce
import webhooks.agente_listas as agente_listas
import webhooks.agente_bazar as agente_bazar

logger = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    phone: str
    text: Optional[str]
    source: str  # "whapi"
    from_me: bool = False
    is_group: bool = False
    media_type: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def is_processable(self) -> bool:
        if self.from_me or self.is_group:
            return False
        return bool(self.text and self.text.strip())

    @property
    def is_media_message(self) -> bool:
        if self.from_me or self.is_group:
            return False
        return self.media_type in ("image", "document", "video")


HANDLED_STAGES = {Stage.PRECIFICACAO, Stage.EM_NEGOCIACAO, Stage.ASSINATURA}
ACTIVATION_STAGES = {
    Stage.PRIMEIRA_ATIVACAO, Stage.SEGUNDA_ATIVACAO,
    Stage.TERCEIRA_ATIVACAO, Stage.QUARTA_ATIVACAO,
}

# Se a proposta já foi enviada (Proposta Realizada preenchida), o negociador
# assume independente da stage — evita que agente_bazar encerre prematuramente
def _proposta_ja_enviada(card: dict) -> bool:
    p = str(card.get("Proposta Realizada") or "").strip()
    try:
        return float(p.replace("R$","").replace(".","").replace(",",".").strip()) > 0
    except (ValueError, TypeError):
        return False


def parse_whapi_payload(payload: dict) -> list[IncomingMessage]:
    """Normaliza payload Whapi para lista de IncomingMessages."""
    messages_raw = []
    if "messages" in payload:
        messages_raw = payload["messages"] if isinstance(payload["messages"], list) else [payload["messages"]]
    elif "message" in payload:
        messages_raw = [payload["message"]]
    elif payload.get("event", {}).get("type") == "messages":
        messages_raw = payload.get("event", {}).get("data", {}).get("messages", [])

    result = []
    for msg in messages_raw:
        msg_type = msg.get("type", "")
        if msg_type in ("status", "reaction", "revoked"):
            continue

        chat_id = msg.get("chat_id", "") or msg.get("from", "")
        from_me = msg.get("from_me", False)
        is_group = "@g.us" in chat_id
        phone_raw = chat_id.replace("@s.whatsapp.net", "").replace("@g.us", "")
        phone = "".join(c for c in phone_raw if c.isdigit())
        if phone and not phone.startswith("55"):
            phone = "55" + phone

        text = None
        media_type = None

        if msg_type == "text":
            text = msg.get("body") or msg.get("text", {}).get("body", "")
        elif msg_type in ("image", "video", "audio", "document", "sticker", "voice"):
            media_type = msg_type
            text = msg.get("caption") or msg.get("body") or None
        elif msg_type == "reply":
            reply_obj = msg.get("reply", {})
            btn_reply = reply_obj.get("buttons_reply", {})
            text = btn_reply.get("title") or reply_obj.get("text") or None
        elif "body" in msg:
            text = msg["body"]

        if not text:
            interactive = msg.get("interactive", {})
            if interactive:
                btn_reply = interactive.get("button_reply", {})
                list_reply = interactive.get("list_reply", {})
                text = btn_reply.get("title") or list_reply.get("title") or None

        if not phone:
            continue

        result.append(IncomingMessage(
            phone=phone,
            text=text.strip() if text else None,
            source="whapi",
            from_me=from_me,
            is_group=is_group,
            media_type=media_type,
            raw=msg,
        ))

    return result


async def _find_card(phone: str) -> Optional[dict]:
    digits = "".join(c for c in phone if c.isdigit())
    candidates = {digits}
    if digits.startswith("55"):
        candidates.add(digits[2:])
    else:
        candidates.add("55" + digits)
    if len(digits) == 10:
        candidates.add(digits[:2] + "9" + digits[2:])
    if len(digits) == 12 and digits.startswith("55"):
        candidates.add(digits[:4] + "9" + digits[4:])

    try:
        async with FaroClient() as faro:
            for candidate in candidates:
                card = await faro.find_card_by_phone(candidate)
                if card:
                    return card
    except FaroError as e:
        logger.error("Router: erro ao buscar card por telefone %s: %s", phone, e)
    return None


async def route_message(msg: IncomingMessage) -> None:
    if msg.from_me or msg.is_group:
        return
    if not msg.is_processable and not msg.is_media_message:
        return

    logger.info("Router [%s]: %s → media=%s texto='%s'",
                msg.source, msg.phone, msg.media_type or "none", (msg.text or "")[:60])

    card = await _find_card(msg.phone)

    # Log no #log-cs independente de ter card no FARO
    try:
        from services.slack import log_cs
        if card:
            canal_lead = get_canal(card)
            nome       = card.get("Nome do contato") or card.get("title") or "?"
            card_id    = card.get("id", "")
        else:
            canal_lead = "desconhecido"
            nome       = ""
            card_id    = ""
        asyncio.create_task(log_cs(
            direcao="recebido", canal=canal_lead, phone=msg.phone,
            nome=nome, card_id=card_id, mensagem=msg.text or f"[{msg.media_type}]",
            extra={"FARO": "✅ encontrado" if card else "❌ sem cadastro"},
        ))
    except Exception:
        pass

    if not card:
        logger.info("Router: %s não encontrado no CRM.", msg.phone)
        return

    card_id = card.get("id", "")
    current_stage = card.get("stage_id") or card.get("stageId") or ""
    nome = card.get("Nome do contato") or card.get("title") or "?"
    logger.info("Router: %s (%s) | stage=%s...", nome, card_id[:8], current_stage[:8])

    # Se proposta já foi enviada, negociador assume independente da stage
    if _proposta_ja_enviada(card) and msg.is_processable:
        async def _dispatch_neg(c: dict, texto: str) -> None:
            await handle_message(card=c, mensagem=texto, current_stage_id=current_stage)
        debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                          dispatch=_dispatch_neg)
        return

    # Listas em stages de ativação → agente SDR Listas
    # Regra: is_lista()==True OU Fonte não definida (sem origem = lista fria)
    _fonte = str(card.get("Fonte") or "").strip().lower()
    _is_lista_card = is_lista(card) or (not _fonte)
    if current_stage in ACTIVATION_STAGES and _is_lista_card:
        if msg.is_processable:
            debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                              dispatch=agente_listas.handle_message)
        return

    # Qualificação: stages de ativação, apenas Bazar/Site (Fonte definida)
    if current_stage in QUALIFICATION_STAGES and not _is_lista_card:
        if msg.is_media_message:
            await handle_qualification(card=card, msg=msg)
        elif msg.is_processable:
            debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                              dispatch=agente_bazar.handle_message)
        return
    # Lista em ASSINATURA coletando dados/extrato (ZapSign ainda não gerado)
    if current_stage == Stage.ASSINATURA and is_lista(card) and not card.get("ZapSign Token"):
        if msg.is_media_message:
            asyncio.create_task(handle_extrato_recebido(card, msg))
        elif msg.is_processable:
            debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                              dispatch=handle_dados_pessoais)
        return

    # Negociação / suporte
    if current_stage in HANDLED_STAGES:
        if not msg.is_processable:
            return

        async def _dispatch_negociador(c: dict, texto: str) -> None:
            await handle_message(card=c, mensagem=texto, current_stage_id=current_stage)

        debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                          dispatch=_dispatch_negociador)
        return

    logger.info("Router: stage %s não tratado para %s.", current_stage[:8], nome)


async def handle_whapi_webhook(payload: dict) -> dict:
    """Entry point para POST /webhook/whapi."""
    messages = parse_whapi_payload(payload)
    if not messages:
        return {"status": "ok", "processed": 0}
    logger.info("Whapi webhook: %d mensagem(ns)", len(messages))
    for msg in messages:
        asyncio.create_task(route_message(msg))
    return {"status": "ok", "processed": len(messages)}
