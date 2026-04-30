"""
services/transcriber.py — Transcrição de áudio via Gemini

Converte mensagens de voz/áudio do WhatsApp em texto.
Usado pelo router antes de despachar para qualquer agente,
tornando a transcrição transparente para todos os handlers.

Suporte: audio/ogg (opus), audio/ogg, audio/mpeg, audio/mp4
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TRANSCRIBE_TIMEOUT = 60.0
_TRANSCRIBE_MODEL   = "gemini-2.5-flash"


async def _fetch_audio(url: str, token: str) -> Optional[bytes]:
    """Baixa o áudio do Wasabi via URL do Whapi com autenticação."""
    # Primeiro tenta pegar a URL real via API do Whapi (msg_id no raw)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200 and b"<?xml" not in r.content[:10]:
                return r.content
    except Exception as e:
        logger.warning("transcriber: erro ao baixar áudio (%s): %s", url[:60], e)
    return None


async def transcribe_audio(raw_msg: dict, whapi_token: str) -> Optional[str]:
    """
    Transcreve um áudio/voz recebido do Whapi.

    Parâmetros:
        raw_msg: mensagem bruta do webhook Whapi
        whapi_token: token do canal que recebeu a mensagem

    Retorna:
        Texto transcrito, ou None se falhar.
    """
    from config import GEMINI_API_KEY

    if not GEMINI_API_KEY:
        logger.warning("transcriber: GEMINI_API_KEY não configurada — transcrição desabilitada")
        return None

    msg_type = raw_msg.get("type", "")
    if msg_type not in ("audio", "voice"):
        return None

    # Pega a URL e o ID da mensagem
    audio_obj = raw_msg.get("audio") or raw_msg.get("voice") or {}
    audio_url = audio_obj.get("link") or audio_obj.get("url") or ""
    msg_id    = raw_msg.get("id", "")
    mime_type = audio_obj.get("mime_type") or "audio/ogg"

    if not audio_url and not msg_id:
        logger.warning("transcriber: sem URL nem ID para o áudio")
        return None

    # Se a URL direta falhar, tenta via endpoint de mensagem do Whapi
    audio_bytes = None
    if audio_url:
        audio_bytes = await _fetch_audio(audio_url, whapi_token)

    if not audio_bytes and msg_id:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(
                    f"https://gate.whapi.cloud/messages/{msg_id}",
                    headers={"Authorization": f"Bearer {whapi_token}"}
                )
                if r.status_code == 200:
                    data = r.json()
                    obj = data.get("audio") or data.get("voice") or {}
                    fresh_url = obj.get("link") or obj.get("url") or ""
                    if fresh_url:
                        audio_bytes = await _fetch_audio(fresh_url, whapi_token)
        except Exception as e:
            logger.warning("transcriber: erro ao buscar URL atualizada: %s", e)

    if not audio_bytes:
        logger.error("transcriber: não foi possível baixar o áudio msg_id=%s", msg_id)
        return None

    # Garante mime_type compatível
    if "ogg" not in mime_type and "mpeg" not in mime_type and "mp4" not in mime_type:
        mime_type = "audio/ogg"

    # Chama Gemini para transcrever
    b64 = base64.b64encode(audio_bytes).decode()
    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mime_type, "data": b64}},
                {"text": (
                    "Transcreva este áudio de WhatsApp em português brasileiro com precisão. "
                    "Retorne apenas o texto transcrito, sem explicações, sem formatação extra. "
                    "Se o áudio for ininteligível, retorne exatamente: [áudio ininteligível]"
                )}
            ]
        }]
    }

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{_TRANSCRIBE_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    try:
        async with httpx.AsyncClient(timeout=_TRANSCRIBE_TIMEOUT) as c:
            r = await c.post(url, json=payload)
        if r.status_code != 200:
            logger.error("transcriber: Gemini HTTP %d — %s", r.status_code, r.text[:200])
            return None
        d = r.json()
        text = (
            d.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            or ""
        ).strip()
        if not text or text == "[áudio ininteligível]":
            logger.warning("transcriber: áudio ininteligível msg_id=%s", msg_id)
            return None
        dur = audio_obj.get("seconds") or audio_obj.get("duration") or "?"
        logger.info("transcriber: áudio %ss transcrito (%d chars)", dur, len(text))
        return text
    except Exception as e:
        logger.error("transcriber: erro na chamada Gemini: %s", e)
        return None
