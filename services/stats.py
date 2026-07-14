"""
services/stats.py — Contadores Redis para relatório diário de disparos.

Chaves Redis (TTL 7 dias):
  cs:stats:{data}:listas          → disparos de Listas (nova ativação)
  cs:stats:{data}:ativacao_1      → 1ª ativação (follow-up listas/lp)
  cs:stats:{data}:ativacao_2      → 2ª ativação
  cs:stats:{data}:ativacao_3      → 3ª ativação
  cs:stats:{data}:ativacao_4      → 4ª ativação
  cs:stats:{data}:bazar           → disparos Bazar (1ª ativação)
  cs:stats:{data}:lp              → disparos LP (1ª ativação)
  cs:stats:{data}:propostas       → propostas enviadas

Uso:
  from services.stats import increment_stat, get_daily_stats
  await increment_stat("listas")
  stats = await get_daily_stats()   # retorna dict com contadores do dia atual
"""

import logging
from datetime import date, timedelta
from typing import Optional

from config import TZ_BRASILIA
from datetime import datetime

logger = logging.getLogger(__name__)

_STAT_KEYS = [
    "listas",
    "ativacao_1",
    "ativacao_2",
    "ativacao_3",
    "ativacao_4",
    "bazar",
    "lp",
    "interesse",
    "propostas",
]

_TTL_SECONDS = 7 * 24 * 3600  # 7 dias


def _today_str() -> str:
    """Data atual no timezone Brasil (YYYY-MM-DD)."""
    return datetime.now(TZ_BRASILIA).strftime("%Y-%m-%d")


def _redis_key(stat: str, data: Optional[str] = None) -> str:
    d = data or _today_str()
    return f"cs:stats:{d}:{stat}"


async def increment_stat(stat: str, amount: int = 1) -> None:
    """
    Incrementa o contador de uma estatística para o dia de hoje.
    Silencia erros — nunca bloqueia o fluxo principal.
    """
    try:
        from services.session_store import get_redis
        r = await get_redis()
        key = _redis_key(stat)
        await r.incrby(key, amount)
        await r.expire(key, _TTL_SECONDS)
    except Exception as e:
        logger.debug("stats.increment_stat(%s): erro silencioso: %s", stat, e)


async def get_daily_stats(data: Optional[str] = None) -> dict:
    """
    Retorna dict com todos os contadores do dia.
    data: str no formato YYYY-MM-DD (default: hoje no timezone Brasil)

    Retorna:
    {
        "data": "2026-07-13",
        "listas": 45,
        "ativacao_1": 12,
        "ativacao_2": 8,
        "ativacao_3": 3,
        "ativacao_4": 1,
        "bazar": 7,
        "lp": 4,
        "propostas": 2,
        "total_disparos": 80,
    }
    """
    d = data or _today_str()
    result: dict = {"data": d}
    try:
        from services.session_store import get_redis
        r = await get_redis()
        keys = [_redis_key(k, d) for k in _STAT_KEYS]
        values = await r.mget(*keys)
        for stat, val in zip(_STAT_KEYS, values):
            result[stat] = int(val or 0)
    except Exception as e:
        logger.warning("stats.get_daily_stats: erro ao ler Redis: %s", e)
        for stat in _STAT_KEYS:
            result[stat] = 0

    # Total de disparos (exclui propostas — são um evento diferente)
    result["total_disparos"] = sum(
        result.get(k, 0) for k in ["listas", "ativacao_1", "ativacao_2", "ativacao_3", "ativacao_4", "bazar", "lp"]
    )
    return result


async def get_yesterday_stats() -> dict:
    """Atalho para obter os stats do dia anterior."""
    ontem = (datetime.now(TZ_BRASILIA).date() - timedelta(days=1)).strftime("%Y-%m-%d")
    return await get_daily_stats(ontem)
