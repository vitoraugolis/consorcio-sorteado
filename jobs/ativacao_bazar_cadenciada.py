"""
jobs/ativacao_bazar_cadenciada.py — Ativação Bazar cadenciada: 1 lead a cada 30-35 min

Loop contínuo que:
  1. Busca todos os leads qualificados na stage BAZAR (ordem: mais recente → mais antigo)
  2. Dispara a mensagem para o próximo
  3. Dorme intervalo aleatório 30-35 min
  4. Repete

Se não há leads, dorme 5 min e tenta novamente.
Controlado via /jobs/bazar-loop/start e /jobs/bazar-loop/stop.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

from config import Stage
from jobs.ativacao_bazar_site import (
    _activate_card, _qualifica_bazar, _is_within_send_window,
    filter_test_cards, MSG_BAZAR,
)
from services.faro import FaroClient, FaroError

logger = logging.getLogger(__name__)

_state: dict = {
    "running": False,
    "task": None,
    "interval_min": 30,
    "interval_max": 35,
    "sent_today": 0,
    "last_sent_at": None,
    "last_card": None,
    "started_at": None,
}


def get_status() -> dict:
    return {
        "running": _state["running"],
        "interval_min": _state["interval_min"],
        "interval_max": _state["interval_max"],
        "sent_today": _state["sent_today"],
        "last_sent_at": _state["last_sent_at"],
        "last_card": _state["last_card"],
        "started_at": _state["started_at"],
    }


async def _run_loop() -> None:
    while _state["running"]:
        if not _is_within_send_window():
            logger.info("Bazar loop: fora da janela BRT — aguardando 5 min")
            await asyncio.sleep(300)
            continue

        try:
            async with FaroClient() as faro:
                all_cards = await faro.get_cards_all_pages(stage_id=Stage.BAZAR)

            all_cards = filter_test_cards(all_cards or [])

            # Ordena do mais recente para o mais antigo
            def _dt(c):
                try:
                    from datetime import timezone
                    return datetime.fromisoformat(c.get("created_at", "").replace("Z", "+00:00"))
                except Exception:
                    return datetime.min.replace(tzinfo=timezone.utc)

            qualificados = sorted(
                [c for c in all_cards if _qualifica_bazar(c)[0]],
                key=_dt,
                reverse=True,
            )

            if not qualificados:
                logger.info("Bazar loop: nenhum lead qualificado — aguardando 5 min")
                await asyncio.sleep(300)
                continue

            # Pega o próximo (mais recente)
            card = qualificados[0]

            async with FaroClient() as faro:
                ok = await _activate_card(card, MSG_BAZAR, _qualifica_bazar, faro)

            if ok:
                _state["sent_today"] += 1
                _state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
                _state["last_card"] = {
                    "id": card.get("id", "")[:8],
                    "nome": card.get("Nome do contato", "?"),
                    "adm": card.get("Adm", "?"),
                }
                wait_s = random.randint(_state["interval_min"] * 60, _state["interval_max"] * 60)
                logger.info(
                    "Bazar loop: ✅ %s (%s) enviado — próximo em %ds (~%.0fmin)",
                    card.get("Nome do contato", "?"), card.get("id", "")[:8],
                    wait_s, wait_s / 60,
                )
                await asyncio.sleep(wait_s)
            else:
                # Card não enviado (sem WA, não qualificou) — tenta próximo sem esperar
                logger.info("Bazar loop: ⏩ card pulado — tentando próximo imediatamente")
                await asyncio.sleep(2)

        except Exception as e:
            logger.error("Bazar loop: erro inesperado: %s", e)
            await asyncio.sleep(60)

    logger.info("Bazar loop: encerrado")


def start(interval_min: int = 30, interval_max: int = 35) -> dict:
    if _state["running"]:
        return {"status": "already_running", **get_status()}

    _state.update({
        "running": True,
        "interval_min": interval_min,
        "interval_max": interval_max,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    loop = asyncio.get_event_loop()
    task = loop.create_task(_run_loop())
    _state["task"] = task

    logger.info("Bazar loop: iniciado (intervalo %d-%dmin)", interval_min, interval_max)
    return {"status": "started", **get_status()}


def stop() -> dict:
    _state["running"] = False
    return {"status": "stopping", **get_status()}
