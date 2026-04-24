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


# ─── Utilitários gerais ───────────────────────────────────────────────────────

async def health_check() -> bool:
    """Verifica se o Redis está acessível."""
    try:
        r = await get_redis()
        return await r.ping()
    except Exception:
        return False
