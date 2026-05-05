"""
services/session_store.py — Redis-backed session store para agentes conversacionais

Substitui dicts/sets em memória por estado persistente no Redis local.
Garante que reinícios do servidor não percam conversas em andamento.

Estrutura das chaves:
  cs:conv:{phone}        → histórico de mensagens (list, max 50)
  cs:mutex:{resource}    → distributed lock / mutex (string, com TTL)
  cs:debounce:{phone}    → buffer de debounce (list de textos)
"""

import json
import logging
from typing import Any

import redis.asyncio as aioredis

from config import REDIS_URL

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None

# TTLs
_CONV_TTL_SEC    = 60 * 60 * 24 * 30  # 30 dias — histórico de conversa
_MUTEX_TTL_SEC   = 60 * 5              # 5 min — mutex de processamento
_DEBOUNCE_TTL_SEC = 60                 # 1 min — buffer de debounce


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


# ─── Histórico de conversa ────────────────────────────────────────────────────

async def get_history(phone: str) -> list[dict]:
    """Retorna o histórico de mensagens do lead. Lista de {role, content}."""
    try:
        r = await get_redis()
        raw = await r.get(f"cs:conv:{phone}")
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("Redis get_history(%s): %s", phone[-6:], e)
        return []


async def append_history(phone: str, role: str, content: str, max_turns: int = 50) -> None:
    """Adiciona uma mensagem ao histórico. Mantém no máximo max_turns entradas."""
    try:
        r = await get_redis()
        key = f"cs:conv:{phone}"
        history = await get_history(phone)
        history.append({"role": role, "content": content})
        if len(history) > max_turns:
            history = history[-max_turns:]
        await r.set(key, json.dumps(history, ensure_ascii=False), ex=_CONV_TTL_SEC)
    except Exception as e:
        logger.warning("Redis append_history(%s): %s", phone[-6:], e)


async def clear_history(phone: str) -> None:
    """Remove o histórico de conversa (ex: lead fechou negócio ou foi descartado)."""
    try:
        r = await get_redis()
        await r.delete(f"cs:conv:{phone}")
    except Exception as e:
        logger.warning("Redis clear_history(%s): %s", phone[-6:], e)


# ─── Mutex distribuído ────────────────────────────────────────────────────────

async def acquire_mutex(resource: str, ttl: int = _MUTEX_TTL_SEC) -> bool:
    """
    Tenta adquirir um mutex para o resource (ex: card_id).
    Retorna True se adquiriu, False se já estava travado.
    Usa SET NX (atômico) — seguro contra race conditions.
    """
    try:
        r = await get_redis()
        result = await r.set(f"cs:mutex:{resource}", "1", nx=True, ex=ttl)
        return result is True
    except Exception as e:
        logger.warning("Redis acquire_mutex(%s): %s — assumindo livre", resource[:12], e)
        return True  # fail-open: se Redis cair, não bloqueia o processamento


async def release_mutex(resource: str) -> None:
    """Libera o mutex."""
    try:
        r = await get_redis()
        await r.delete(f"cs:mutex:{resource}")
    except Exception as e:
        logger.warning("Redis release_mutex(%s): %s", resource[:12], e)


# ─── Buffer de debounce ───────────────────────────────────────────────────────

async def push_debounce_text(phone: str, text: str) -> None:
    """Adiciona texto ao buffer de debounce do telefone."""
    try:
        r = await get_redis()
        key = f"cs:debounce:{phone}"
        await r.rpush(key, text)
        await r.expire(key, _DEBOUNCE_TTL_SEC)
    except Exception as e:
        logger.warning("Redis push_debounce(%s): %s", phone[-6:], e)


async def pop_debounce_buffer(phone: str) -> list[str]:
    """Retorna e limpa o buffer de debounce."""
    try:
        r = await get_redis()
        key = f"cs:debounce:{phone}"
        pipe = r.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        results = await pipe.execute()
        return results[0] if results[0] else []
    except Exception as e:
        logger.warning("Redis pop_debounce(%s): %s", phone[-6:], e)
        return []


# ─── História Redis-first com fallback FARO ──────────────────────────────────

async def load_history_smart(phone: str, card: dict) -> list[dict]:
    """
    Carrega histórico de conversa priorizando Redis (rápido).
    Se Redis vazio, cai no FARO como fallback (migração gradual).
    Ao encontrar histórico no FARO mas não no Redis, migra automaticamente.
    """
    from services.faro import load_history as faro_load_history

    history = await get_history(phone)
    if history:
        return history

    # Fallback: FARO (dados pré-migração ou Redis zerado após limpeza)
    faro_history = faro_load_history(card)
    if faro_history:
        logger.info("session_store: migrando histórico FARO→Redis para %s (%d turns)",
                    phone[-6:], len(faro_history))
        # Persiste no Redis para próximas chamadas
        r = await get_redis()
        await r.set(
            f"cs:conv:{phone}",
            json.dumps(faro_history, ensure_ascii=False),
            ex=_CONV_TTL_SEC,
        )
    return faro_history


async def save_history_smart(
    phone: str,
    history: list[dict],
    faro_client: Any | None = None,
    card_id: str | None = None,
    max_turns: int = 50,
) -> None:
    """
    Salva histórico no Redis (primário) e opcionalmente no FARO (backup).
    - Redis: sempre, rápido, com TTL
    - FARO: quando faro_client e card_id fornecidos (backup durável)
    """
    import asyncio
    from services.faro import save_history as faro_save_history

    # Trunca para max_turns
    if len(history) > max_turns:
        history = history[-max_turns:]

    # Salva no Redis
    try:
        r = await get_redis()
        await r.set(f"cs:conv:{phone}", json.dumps(history, ensure_ascii=False), ex=_CONV_TTL_SEC)
    except Exception as e:
        logger.warning("Redis save_history_smart(%s): %s", phone[-6:], e)

    # Backup no FARO (fire-and-forget se faro_client disponível)
    if faro_client is not None and card_id is not None:
        try:
            await faro_save_history(faro_client, card_id, history)
        except Exception as e:
            logger.warning("FARO save_history backup(%s): %s", card_id[:8], e)


# ─── Buffer de mídia (multi-extrato) ─────────────────────────────────────────

_MEDIA_BUFFER_TTL = 35   # segundos — janela de espera por novas imagens
_MEDIA_BUFFER_KEY = "cs:media_buf:{phone}"


async def push_media_buffer(phone: str, entry: dict) -> int:
    """
    Adiciona uma entrada de mídia ao buffer do telefone.
    entry = {"url": str, "media_type": str, "raw": dict}
    Retorna o tamanho atual do buffer.
    """
    try:
        r = await get_redis()
        key = _MEDIA_BUFFER_KEY.format(phone=phone)
        await r.rpush(key, json.dumps(entry, ensure_ascii=False))
        await r.expire(key, _MEDIA_BUFFER_TTL)
        length = await r.llen(key)
        return int(length)
    except Exception as e:
        logger.warning("Redis push_media_buffer(%s): %s", phone[-6:], e)
        return 1


async def peek_media_buffer(phone: str) -> list[dict]:
    """Lê o buffer sem remover (para checar se ainda cresce)."""
    try:
        r = await get_redis()
        key = _MEDIA_BUFFER_KEY.format(phone=phone)
        items = await r.lrange(key, 0, -1)
        return [json.loads(i) for i in items]
    except Exception as e:
        logger.warning("Redis peek_media_buffer(%s): %s", phone[-6:], e)
        return []


async def pop_media_buffer(phone: str) -> list[dict]:
    """Drena e retorna todo o buffer de mídia."""
    try:
        r = await get_redis()
        key = _MEDIA_BUFFER_KEY.format(phone=phone)
        pipe = r.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        results = await pipe.execute()
        raw = results[0] if results else []
        return [json.loads(i) for i in raw]
    except Exception as e:
        logger.warning("Redis pop_media_buffer(%s): %s", phone[-6:], e)
        return []


async def media_buffer_ttl(phone: str) -> int:
    """Retorna TTL restante do buffer (segundos). -2 se não existe."""
    try:
        r = await get_redis()
        key = _MEDIA_BUFFER_KEY.format(phone=phone)
        return int(await r.ttl(key))
    except Exception:
        return -2


# ─── Fingerprint de mensagens enviadas pelo bot ──────────────────────────────
#
# Problema: o Whapi entrega webhooks from_me=True tanto para mensagens enviadas
# pelo bot quanto para mensagens digitadas manualmente pelo time comercial.
# Solução: ao enviar uma mensagem, gravamos o message_id no Redis com TTL curto.
# Ao receber um webhook from_me, verificamos se o id está nessa lista de bot.
# Se estiver → descarta (é do bot). Se não estiver → é do comercial → handoff.

_BOT_MSG_TTL = 300  # 5 min (mensagens recentes do bot)


async def mark_bot_message(msg_id: str) -> None:
    """
    Registra que este message_id foi enviado pelo bot.
    Deve ser chamado em whapi.py após cada envio bem-sucedido.
    """
    if not msg_id:
        return
    try:
        r = await get_redis()
        await r.set(f"cs:botmsg:{msg_id}", "1", ex=_BOT_MSG_TTL)
    except Exception as e:
        logger.warning("Redis mark_bot_message(%s): %s", msg_id[:12], e)


async def is_bot_message(msg_id: str) -> bool:
    """
    Retorna True se o message_id foi enviado pelo bot.
    Fail-safe: em caso de erro Redis retorna False (assume comercial — melhor
    que ignorar um atendimento real, o pior caso é um handoff redundante protegido
    pela flag set_handoff_flag).
    """
    if not msg_id:
        return False
    try:
        r = await get_redis()
        return bool(await r.exists(f"cs:botmsg:{msg_id}"))
    except Exception as e:
        logger.warning("Redis is_bot_message(%s): %s — assumindo NÃO é bot", msg_id[:12], e)
        return False


# ─── Flag de handoff (deduplicação de movimentação de card) ──────────────────
#
# Garante que múltiplas mensagens manuais em sequência não disparem o handoff
# múltiplas vezes. TTL de 4h — ao fim do expediente ou nova conversa, a flag expira.
# O time pode forçar o reprocessamento apagando a chave no Redis se necessário.

_HANDOFF_FLAG_TTL = 60 * 60 * 4  # 4h


async def set_handoff_flag(phone: str) -> bool:
    """
    Tenta marcar que o handoff já foi executado para este lead (atômica via SET NX).
    Retorna True se era a PRIMEIRA vez (deve executar a movimentação).
    Retorna False se já houve handoff recente (TTL ativo) — não fazer nada além
    de registrar o turno no histórico.
    """
    try:
        r = await get_redis()
        result = await r.set(f"cs:handoff:{phone}", "1", nx=True, ex=_HANDOFF_FLAG_TTL)
        return result is True
    except Exception as e:
        logger.warning(
            "Redis set_handoff_flag(%s): %s — assumindo primeira vez (fail-open)",
            phone[-6:], e,
        )
        # Fail-open: se Redis falhar, assume que é primeira vez.
        # O pior caso é um update redundante no FARO (idempotente).
        return True


async def clear_handoff_flag(phone: str) -> None:
    """
    Remove a flag de handoff para este lead.
    Use quando o lead voltar para o fluxo automatizado (ex: stage alterado para EM_NEGOCIACAO).
    """
    try:
        r = await get_redis()
        await r.delete(f"cs:handoff:{phone}")
    except Exception as e:
        logger.warning("Redis clear_handoff_flag(%s): %s", phone[-6:], e)


# ─── Utilitários gerais ───────────────────────────────────────────────────────

async def health_check() -> bool:
    """Verifica se o Redis está acessível."""
    try:
        r = await get_redis()
        return await r.ping()
    except Exception:
        return False
