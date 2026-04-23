"""
webhooks/router.py — Roteador central de mensagens WhatsApp recebidas

Recebe payloads brutos de Whapi e Z-API, normaliza para uma estrutura comum,
identifica o lead pelo número de telefone no FARO e despacha para o handler
correto baseado no stage atual do card.

Estrutura normalizada (IncomingMessage):
  - phone:    número do remetente (somente dígitos, com 55)
  - text:     texto da mensagem (None se for mídia)
  - media_type: tipo de mídia se não for texto (image/audio/document/etc.)
  - source:   "whapi" ou "zapi"
  - raw:      payload original completo

Handlers por stage:
  - PRECIFICACAO, EM_NEGOCIACAO, ASSINATURA → negociador.handle_message()
  - Demais stages → log e ignora (agente humano ou stage terminal)

Mensagens ignoradas:
  - Enviadas pelo próprio sistema (fromMe = True)
  - Mensagens de grupo
  - Mídia sem texto (áudio, imagem sem legenda)
  - Remetentes não encontrados no CRM
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from config import Stage
from services.faro import FaroClient, FaroError, is_lista, get_name, get_phone
from webhooks.negociador import handle_message
from webhooks.qualificador import handle_qualification, QUALIFICATION_STAGES
from webhooks.agente_contrato import handle_dados_pessoais, handle_extrato_recebido
from webhooks import debounce
import webhooks.agente_listas as agente_listas
import webhooks.agente_bazar as agente_bazar

_CPF_RE = re.compile(r"\b(\d{3}[.\-]?\d{3}[.\-]?\d{3}[.\-]?\d{2})\b")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Estrutura de mensagem normalizada
# ---------------------------------------------------------------------------

@dataclass
class IncomingMessage:
    phone:       str
    text:        Optional[str]
    source:      str         # "whapi" | "zapi"
    from_me:     bool = False
    is_group:    bool = False
    media_type:  Optional[str] = None
    raw:         dict = field(default_factory=dict)

    @property
    def is_processable(self) -> bool:
        """Verdadeiro se a mensagem de texto deve ser processada pelo sistema."""
        if self.from_me or self.is_group:
            return False
        if not self.text or not self.text.strip():
            return False
        return True

    @property
    def is_media_message(self) -> bool:
        """
        Verdadeiro se é uma mensagem de mídia relevante (doc/imagem) de um
        remetente externo, mesmo sem legenda de texto.
        Usado para detectar envio de extrato na qualificação.
        """
        if self.from_me or self.is_group:
            return False
        return self.media_type in ("image", "document", "video")


# ---------------------------------------------------------------------------
# Stages com ação automática
# ---------------------------------------------------------------------------

HANDLED_STAGES = {
    Stage.PRECIFICACAO,
    Stage.EM_NEGOCIACAO,
    Stage.ASSINATURA,
}

# Stages de ativação onde esperamos resposta de interesse (apenas listas)
ACTIVATION_STAGES = {
    Stage.PRIMEIRA_ATIVACAO,
    Stage.SEGUNDA_ATIVACAO,
    Stage.TERCEIRA_ATIVACAO,
    Stage.QUARTA_ATIVACAO,
}


# ---------------------------------------------------------------------------
# Parsers de payload
# ---------------------------------------------------------------------------

def parse_whapi_payload(payload: dict) -> list[IncomingMessage]:
    """
    Normaliza o payload do Whapi para lista de IncomingMessages.

    Formatos suportados:
      - payload.messages (lista) — evento de mensagem recebida
      - payload.message (objeto) — evento único
    """
    messages_raw = []

    if "messages" in payload:
        messages_raw = payload["messages"] if isinstance(payload["messages"], list) else [payload["messages"]]
    elif "message" in payload:
        messages_raw = [payload["message"]]
    elif payload.get("event", {}).get("type") == "messages":
        messages_raw = payload.get("event", {}).get("data", {}).get("messages", [])

    result = []
    for msg in messages_raw:
        # Filtra status updates e outros tipos não relevantes
        msg_type = msg.get("type", "")
        if msg_type in ("status", "reaction", "revoked"):
            continue

        # Extrai remetente
        chat_id = msg.get("chat_id", "") or msg.get("from", "")
        from_me = msg.get("from_me", False)

        # Grupo: chat_id termina em @g.us
        is_group = "@g.us" in chat_id

        # Phone: remove o sufixo @s.whatsapp.net
        phone_raw = chat_id.replace("@s.whatsapp.net", "").replace("@g.us", "")
        phone = "".join(c for c in phone_raw if c.isdigit())
        if phone and not phone.startswith("55"):
            phone = "55" + phone

        # Texto da mensagem
        text = None
        media_type = None

        if msg_type == "text":
            text = msg.get("body") or msg.get("text", {}).get("body", "")
        elif msg_type in ("image", "video", "audio", "document", "sticker", "voice"):
            media_type = msg_type
            text = msg.get("caption") or msg.get("body") or None
        elif msg_type == "reply":
            # Clique em botão quick_reply: reply.buttons_reply.title
            reply_obj = msg.get("reply", {})
            btn_reply = reply_obj.get("buttons_reply", {})
            text = btn_reply.get("title") or reply_obj.get("text") or None
        elif "body" in msg:
            text = msg["body"]

        # Respostas de botão interativo (formato alternativo)
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


def parse_zapi_payload(payload: dict) -> list[IncomingMessage]:
    """
    Normaliza o payload do Z-API para lista de IncomingMessages.

    Formatos suportados:
      - ReceivedCallback (mensagem recebida)
      - MessageStatusCallback (confirmação de envio — ignorado)
    """
    # Ignora callbacks que não são mensagens recebidas
    callback_type = payload.get("type", "")
    if callback_type and callback_type != "ReceivedCallback":
        return []

    # Também presente em alguns formatos como "isStatusReply"
    if payload.get("isStatusReply"):
        return []

    phone_raw = payload.get("phone", "") or payload.get("from", "")
    phone = "".join(c for c in str(phone_raw) if c.isdigit())
    if phone and not phone.startswith("55"):
        phone = "55" + phone

    from_me = payload.get("fromMe", False) or payload.get("isFromMe", False)

    # Grupos têm "@g.us" no phone
    is_group = "@g.us" in str(payload.get("phone", ""))

    # Extrai texto
    text = None
    media_type = None

    # Formato 1: payload.message.text
    message_obj = payload.get("message") or payload.get("messageData") or {}
    if isinstance(message_obj, dict):
        text = (
            message_obj.get("text")
            or message_obj.get("body")
            or message_obj.get("caption")
            or None
        )
        if not text:
            # Verifica tipos de mídia
            for mtype in ("image", "video", "audio", "document", "sticker"):
                if mtype in message_obj:
                    media_type = mtype
                    text = message_obj[mtype].get("caption") or None
                    break

    # Formato 2: campo direto no payload
    if not text:
        text = payload.get("text") or payload.get("body") or None

    # Botão de resposta
    if not text:
        btn = payload.get("buttonResponse") or payload.get("selectedOption", {})
        if isinstance(btn, dict):
            text = btn.get("displayText") or btn.get("title") or None
        elif isinstance(btn, str):
            text = btn

    if not phone:
        return []

    return [IncomingMessage(
        phone=phone,
        text=text.strip() if text else None,
        source="zapi",
        from_me=from_me,
        is_group=is_group,
        media_type=media_type,
        raw=payload,
    )]


# ---------------------------------------------------------------------------
# Busca de card por telefone (com normalização)
# ---------------------------------------------------------------------------

async def _find_card(phone: str) -> Optional[dict]:
    """
    Busca o card ativo no FARO pelo número de telefone.
    Tenta variações do número para maior compatibilidade.
    """
    digits = "".join(c for c in phone if c.isdigit())

    # Variações para busca: com/sem 55, com/sem 9 extra
    candidates = set()
    candidates.add(digits)

    # Sem prefixo 55
    if digits.startswith("55"):
        candidates.add(digits[2:])

    # Com prefixo 55
    if not digits.startswith("55"):
        candidates.add("55" + digits)

    # Número de SP: 11 + 9 dígitos (adiciona o 9 no celular se faltando)
    if len(digits) == 10:  # DDD + 8 dígitos → add 9
        candidates.add(digits[:2] + "9" + digits[2:])
    if len(digits) == 12 and digits.startswith("55"):  # 5511 + 8 digits
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


# ---------------------------------------------------------------------------
# Router principal
# ---------------------------------------------------------------------------

async def route_message(msg: IncomingMessage) -> None:
    """
    Processa uma mensagem normalizada:
    1. Descarta mensagens próprias e de grupo
    2. Busca o card no FARO pelo telefone
    3. Verifica o stage atual e despacha para o handler correto:
       - Stages de ativação + lead Bazar/Site → qualificador (aceita mídia)
       - PRECIFICACAO / EM_NEGOCIACAO / ASSINATURA → negociador (apenas texto)
       - Demais → ignora (humano ou stage terminal)
    """
    # Descarta sempre mensagens próprias e grupos (independente do stage)
    if msg.from_me or msg.is_group:
        reason = "from_me" if msg.from_me else "group"
        logger.debug("Router: mensagem ignorada (%s) de %s", reason, msg.phone)
        return

    # Se não tem texto nem mídia relevante, ignora
    if not msg.is_processable and not msg.is_media_message:
        logger.debug("Router: mensagem ignorada (sem_conteudo) de %s", msg.phone)
        return

    logger.info(
        "Router [%s]: mensagem de %s → media=%s texto='%s'",
        msg.source, msg.phone, msg.media_type or "none", (msg.text or "")[:60]
    )

    # Busca card
    card = await _find_card(msg.phone)
    if not card:
        logger.info("Router: %s não encontrado no CRM. Mensagem ignorada.", msg.phone)
        return

    card_id        = card.get("id", "")
    current_stage  = card.get("stage_id") or card.get("stageId") or ""
    nome           = card.get("Nome do contato") or card.get("title") or "?"

    logger.info(
        "Router: card encontrado → %s (%s) | stage=%s...",
        nome, card_id[:8], current_stage[:8] if current_stage else "?"
    )

    # ── Listas em stages de ativação → agente SDR com IA (classifica e age) ──
    if current_stage in ACTIVATION_STAGES and is_lista(card):
        if msg.is_processable:
            debounce.schedule(
                phone=msg.phone, text=msg.text, card=card,
                dispatch=agente_listas.handle_message,
            )
        return

    # ── Qualificação: stages de ativação, apenas Bazar/Site ──────────────────
    if current_stage in QUALIFICATION_STAGES and not is_lista(card):
        if msg.is_media_message:
            # Mídia bypassa debounce — processa imediatamente
            await handle_qualification(card=card, msg=msg)
        elif msg.is_processable:
            debounce.schedule(
                phone=msg.phone, text=msg.text, card=card,
                dispatch=agente_bazar.handle_message,
            )
        return

    # ── Lead de lista em ASSINATURA coletando dados/extrato (ZapSign ainda não gerado) ──
    if (
        current_stage == Stage.ASSINATURA
        and is_lista(card)
        and not card.get("ZapSign Token")
    ):
        if msg.is_media_message:
            logger.info(
                "Router: mídia recebida de lista %s (%s) em ASSINATURA → agente_contrato",
                nome, card_id[:8],
            )
            asyncio.create_task(handle_extrato_recebido(card, msg))
        elif msg.is_processable:
            debounce.schedule(
                phone=msg.phone, text=msg.text, card=card,
                dispatch=handle_dados_pessoais,
            )
        return

    # ── Negociação / suporte: apenas mensagens de texto ───────────────────────
    if current_stage in HANDLED_STAGES:
        if not msg.is_processable:
            logger.debug(
                "Router: mídia sem texto ignorada no stage %s... de %s",
                current_stage[:8], nome,
            )
            return

        async def _dispatch_negociador(c: dict, texto: str) -> None:
            await handle_message(card=c, mensagem=texto, current_stage_id=current_stage)

        debounce.schedule(
            phone=msg.phone, text=msg.text, card=card,
            dispatch=_dispatch_negociador,
        )
        return

    logger.info(
        "Router: stage %s... não é tratado pelo bot. "
        "Mensagem de %s registrada mas sem ação automática.",
        current_stage[:8] if current_stage else "?", nome,
    )


# ---------------------------------------------------------------------------
# Handlers de webhook (chamados pelo main.py)
# ---------------------------------------------------------------------------

async def handle_whapi_webhook(payload: dict) -> dict:
    """
    Entry point para o webhook /webhook/whapi.
    Parseia o payload, processa cada mensagem em background.
    Retorna imediatamente para não causar timeout no Whapi.
    """
    messages = parse_whapi_payload(payload)

    if not messages:
        logger.debug("Whapi webhook: nenhuma mensagem processável no payload")
        return {"status": "ok", "processed": 0}

    logger.info("Whapi webhook: %d mensagem(ns) recebida(s)", len(messages))

    for msg in messages:
        asyncio.create_task(route_message(msg))

    return {"status": "ok", "processed": len(messages)}


async def handle_zapi_webhook(payload: dict) -> dict:
    """
    Entry point para o webhook /webhook/zapi.
    Parseia o payload, processa cada mensagem em background.
    """
    messages = parse_zapi_payload(payload)

    if not messages:
        logger.debug("Z-API webhook: nenhuma mensagem processável no payload")
        return {"status": "ok", "processed": 0}

    logger.info("Z-API webhook: %d mensagem(ns) recebida(s)", len(messages))

    for msg in messages:
        asyncio.create_task(route_message(msg))

    return {"status": "ok", "processed": len(messages)}
