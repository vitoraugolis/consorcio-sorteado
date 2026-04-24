"""
webhooks/debounce.py — Debounce central compartilhado por todos os agentes

Acumula mensagens de texto do mesmo número por DEBOUNCE_SECONDS antes de
despachar. Mensagens de mídia bypassam o debounce e são enviadas imediatamente.

O buffer de textos agora é persistido no Redis — reinícios do servidor não
perdem mensagens acumuladas. As tasks asyncio ainda vivem em memória (são
efêmeras por design — um timer não precisa sobreviver a reinícios).
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable

from config import DEBOUNCE_SECONDS
from services.session_store import push_debounce_text, pop_debounce_buffer

logger = logging.getLogger(__name__)

# Tasks asyncio ainda em memória (efêmeras, correto por design)
_pending:     dict[str, asyncio.Task] = {}
_card_latest: dict[str, Any]          = {}  # phone → card mais recente


async def _fire(phone: str, dispatch: Callable[[Any, str], Awaitable[None]]) -> None:
    await asyncio.sleep(DEBOUNCE_SECONDS)
    texts = await pop_debounce_buffer(phone)
    card  = _card_latest.pop(phone, None)
    _pending.pop(phone, None)
    if not texts or card is None:
        return
    combined = " ".join(texts)
    logger.debug("Debounce[%s]: %d msg(s) → dispatch", phone[-6:], len(texts))
    try:
        await dispatch(card, combined)
    except Exception as e:
        logger.error(
            "Debounce: dispatch falhou para %s (card=%s): %s",
            phone[-6:], (card.get("id", "") if isinstance(card, dict) else "?")[:8], e,
            exc_info=True,
        )


def schedule(phone: str, text: str, card: Any, dispatch: Callable) -> None:
    """
    Agenda o dispatch com debounce.
    Cancela task anterior se existir, acumulando o novo texto no buffer Redis.
    """
    asyncio.create_task(push_debounce_text(phone, text))
    _card_latest[phone] = card

    existing = _pending.get(phone)
    if existing and not existing.done():
        existing.cancel()

    _pending[phone] = asyncio.create_task(_fire(phone, dispatch))
