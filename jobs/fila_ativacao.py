"""
jobs/fila_ativacao.py — Fila de ativação inteligente Bazar + LP com jitter humano

Lógica:
  1. Busca todos os cards pendentes de Bazar e LP (até 1 ano)
  2. Pré-qualifica cada card antes de enfileirar
  3. Prioridade: qualificados primeiro (intercalados Bazar/LP), depois não qualificados
  4. Dispara um por um com intervalo aleatório de 10-25 min entre qualificados
     Não qualificados são enviados sem jitter (msg rápida de agradecimento)
  5. Respeita janela de envio (SEND_WINDOW_START–SEND_WINDOW_END)
  6. Estado da fila persiste em Redis para sobreviver a restarts
  7. run_watch_novos_leads() detecta cards novos a cada 5 min e os injeta na fila
"""

import asyncio
import logging
import random
import json

import redis.asyncio as aioredis

from config import (
    Stage, TZ_BRASILIA, TEST_MODE,
    BAZAR_JITTER_MIN_S, BAZAR_JITTER_MAX_S,
    BAZAR_WINDOW_START, BAZAR_WINDOW_END,
)
from services.faro import FaroClient, FaroError
from services.whapi import WhapiClient
from services.slack import slack_error
from jobs.ativacao_bazar_site import (
    _activate_card, _qualifica_bazar, _qualifica_lp,
    MSG_BAZAR, MSG_SITE,
)


def _is_within_bazar_window() -> bool:
    from datetime import datetime
    return BAZAR_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < BAZAR_WINDOW_END

logger = logging.getLogger(__name__)

REDIS_QUEUE_KEY   = "fila_ativacao:queue"
REDIS_RUNNING_KEY = "fila_ativacao:running"
REDIS_SEEN_KEY    = "fila_ativacao:seen"   # Set de card IDs já enfileirados/processados

HOURS_LOOKBACK = 8760  # 1 ano — pega todo o acúmulo histórico
FETCH_LIMIT    = 500   # máximo de cards por stage


def _get_redis() -> aioredis.Redis:
    return aioredis.Redis(host="localhost", port=6379, decode_responses=True)


def _interleave(a, fonte_a, b, fonte_b):
    """Intercala duas listas marcando fonte e qualificado=True."""
    result = []
    for x, y in zip(a, b):
        result.append({"card": x, "fonte": fonte_a, "qualificado": True})
        result.append({"card": y, "fonte": fonte_b, "qualificado": True})
    for x in a[len(b):]:
        result.append({"card": x, "fonte": fonte_a, "qualificado": True})
    for y in b[len(a):]:
        result.append({"card": y, "fonte": fonte_b, "qualificado": True})
    return result


async def build_queue() -> dict:
    """
    Busca TODOS os cards de Bazar e LP (até 1 ano), pré-qualifica,
    ordena por prioridade e persiste no Redis.
    Qualificados ficam na frente (intercalados Bazar/LP); não qualificados no final.
    Limpa o set 'seen' e reconstrói do zero.
    Retorna {total, qualificados, nao_qualificados}.
    """
    async with FaroClient() as faro:
        try:
            bazar_cards = await faro.watch_recent(
                stage_id=Stage.BAZAR, hours=HOURS_LOOKBACK, limit=FETCH_LIMIT
            )
        except FaroError as e:
            logger.error("Erro buscando cards Bazar: %s", e)
            bazar_cards = []

        try:
            lp_cards = await faro.watch_recent(
                stage_id=Stage.LP, hours=HOURS_LOOKBACK, limit=FETCH_LIMIT
            )
        except FaroError as e:
            logger.error("Erro buscando cards LP: %s", e)
            lp_cards = []

    # Pré-qualifica
    bazar_qual, bazar_nqual = [], []
    for card in bazar_cards:
        ok, _ = _qualifica_bazar(card)
        (bazar_qual if ok else bazar_nqual).append(card)

    lp_qual, lp_nqual = [], []
    for card in lp_cards:
        ok, _ = _qualifica_lp(card)
        (lp_qual if ok else lp_nqual).append(card)

    logger.info(
        "Pré-qualificação: Bazar %d✅ %d❌ | LP %d✅ %d❌",
        len(bazar_qual), len(bazar_nqual), len(lp_qual), len(lp_nqual),
    )

    # Ordena cada grupo: mais recente primeiro (maior created_at = topo da fila)
    def _sort_key(card: dict) -> str:
        return card.get("created_at") or card.get("updated_at") or ""

    bazar_qual.sort(key=_sort_key, reverse=True)
    lp_qual.sort(key=_sort_key, reverse=True)
    bazar_nqual.sort(key=_sort_key, reverse=True)
    lp_nqual.sort(key=_sort_key, reverse=True)

    # Qualificados primeiro (intercalados), depois não qualificados
    queue = _interleave(bazar_qual, "bazar", lp_qual, "lp")
    for card in bazar_nqual:
        queue.append({"card": card, "fonte": "bazar", "qualificado": False})
    for card in lp_nqual:
        queue.append({"card": card, "fonte": "lp", "qualificado": False})

    if not queue:
        logger.info("Fila vazia — nenhum card encontrado em Bazar ou LP")
        return {"total": 0, "qualificados": 0, "nao_qualificados": 0}

    r = _get_redis()
    try:
        await r.delete(REDIS_QUEUE_KEY)
        await r.delete(REDIS_SEEN_KEY)
        pipeline = r.pipeline()
        for item in queue:
            pipeline.rpush(REDIS_QUEUE_KEY, json.dumps(item, ensure_ascii=False))
            pipeline.sadd(REDIS_SEEN_KEY, item["card"]["id"])
        await pipeline.execute()
        await r.expire(REDIS_SEEN_KEY, 3600 * 24 * 7)  # TTL 7 dias

        total_qual  = len(bazar_qual) + len(lp_qual)
        total_nqual = len(bazar_nqual) + len(lp_nqual)
        logger.info(
            "Fila construída: %d qualificados + %d não qualificados = %d total",
            total_qual, total_nqual, len(queue),
        )
        return {"total": len(queue), "qualificados": total_qual, "nao_qualificados": total_nqual}
    finally:
        await r.aclose()


async def watch_novos_leads() -> dict:
    """
    Detecta cards novos em Bazar/LP (últimas 2h) que ainda não foram vistos.
    Injeta qualificados no topo da fila e não qualificados no final.
    Retorna {novos_qualificados, novos_nao_qualificados}.
    """
    async with FaroClient() as faro:
        try:
            bazar_cards = await faro.watch_recent(stage_id=Stage.BAZAR, hours=2, limit=100)
        except FaroError as e:
            logger.error("watch_novos: erro Bazar: %s", e)
            bazar_cards = []
        try:
            lp_cards = await faro.watch_recent(stage_id=Stage.LP, hours=2, limit=100)
        except FaroError as e:
            logger.error("watch_novos: erro LP: %s", e)
            lp_cards = []

    r = _get_redis()
    try:
        novos_qual, novos_nqual = [], []

        for card in bazar_cards:
            cid = card["id"]
            if await r.sismember(REDIS_SEEN_KEY, cid):
                continue
            ok, _ = _qualifica_bazar(card)
            entry = {"card": card, "fonte": "bazar", "qualificado": ok}
            (novos_qual if ok else novos_nqual).append(entry)
            await r.sadd(REDIS_SEEN_KEY, cid)

        for card in lp_cards:
            cid = card["id"]
            if await r.sismember(REDIS_SEEN_KEY, cid):
                continue
            ok, _ = _qualifica_lp(card)
            entry = {"card": card, "fonte": "lp", "qualificado": ok}
            (novos_qual if ok else novos_nqual).append(entry)
            await r.sadd(REDIS_SEEN_KEY, cid)

        if not novos_qual and not novos_nqual:
            return {"novos_qualificados": 0, "novos_nao_qualificados": 0}

        # Ordena novos por created_at: mais recente primeiro
        def _sort_entry(e: dict) -> str:
            c = e["card"]
            return c.get("created_at") or c.get("updated_at") or ""

        novos_qual.sort(key=_sort_entry, reverse=True)
        novos_nqual.sort(key=_sort_entry, reverse=True)

        pipeline = r.pipeline()
        # lpush inverte a ordem — usamos reversed para que o mais recente fique no topo
        for entry in reversed(novos_qual):
            pipeline.lpush(REDIS_QUEUE_KEY, json.dumps(entry, ensure_ascii=False))
        for entry in novos_nqual:
            pipeline.rpush(REDIS_QUEUE_KEY, json.dumps(entry, ensure_ascii=False))
        await pipeline.execute()

        logger.info(
            "watch_novos: +%d qualificado(s) e +%d não qualificado(s) injetados na fila",
            len(novos_qual), len(novos_nqual),
        )
        return {"novos_qualificados": len(novos_qual), "novos_nao_qualificados": len(novos_nqual)}
    finally:
        await r.aclose()


async def run_watch_novos_leads():
    """Job periódico (a cada 5 min): detecta e injeta novos leads na fila."""
    result = await watch_novos_leads()
    if result["novos_qualificados"] or result["novos_nao_qualificados"]:
        r = _get_redis()
        try:
            running = await r.get(REDIS_RUNNING_KEY)
        finally:
            await r.aclose()
        if not running:
            logger.info("watch_novos: fila parada com novos leads — reiniciando")
            asyncio.create_task(run_fila_ativacao())


async def _check_whapi_bazar_health() -> bool:
    """Verifica se o canal Bazar está respondendo com HTTP 200."""
    async with WhapiClient(canal="bazar") as w:
        ok, status = await w.health_check()
    if not ok:
        logger.error("Whapi Bazar não responde (HTTP erro) — status: %s", status)
        await slack_error(
            f"⚠️ Canal Whapi Bazar não está respondendo (HTTP erro). "
            "Fila de ativação pausada. Verifique o painel Whapi."
        )
    return ok


async def run_fila_ativacao():
    """
    Processa a fila item a item.
    - Qualificados: jitter 10-25 min entre disparos
    - Não qualificados: sem jitter
    """
    import os

    # Checagem inicial
    if os.getenv("JOBS_PAUSED", "false").lower() == "true":
        logger.info("Fila: JOBS_PAUSED=true — abortando antes de iniciar")
        return

    r = _get_redis()
    try:
        # SET NX: garante que só um processo/worker inicia a fila
        acquired = await r.set(REDIS_RUNNING_KEY, "1", nx=True, ex=3600 * 12)
        if not acquired:
            logger.info("Fila já está rodando — ignorando chamada duplicada")
            return
        queue_len = await r.llen(REDIS_QUEUE_KEY)
        if queue_len == 0:
            await r.delete(REDIS_RUNNING_KEY)
            logger.info("Fila vazia — nada a processar")
            return
        logger.info("=== Iniciando fila de ativação: %d cards pendentes ===", queue_len)
    finally:
        await r.aclose()

    async with FaroClient() as faro:
        processed = 0
        while True:
            # Checagem a cada iteração — respeita pause em tempo real
            if os.getenv("JOBS_PAUSED", "false").lower() == "true":
                logger.warning("Fila: JOBS_PAUSED=true detectado — interrompendo loop")
                r = _get_redis()
                try:
                    await r.delete(REDIS_RUNNING_KEY)
                finally:
                    await r.aclose()
                return

            r = _get_redis()
            try:
                raw = await r.lpop(REDIS_QUEUE_KEY)
                remaining = await r.llen(REDIS_QUEUE_KEY)
            finally:
                await r.aclose()

            if not raw:
                logger.info("Fila concluída — %d cards processados", processed)
                break

            item = json.loads(raw)
            card = item["card"]
            fonte = item["fonte"]
            qualificado = item.get("qualificado", True)

            if qualificado and not _is_within_bazar_window():
                logger.info("Fora da janela Bazar (%dh–%dh BRT) — pausando fila",
                            BAZAR_WINDOW_START, BAZAR_WINDOW_END)
                r = _get_redis()
                try:
                    await r.lpush(REDIS_QUEUE_KEY, json.dumps(item, ensure_ascii=False))
                finally:
                    await r.aclose()
                await asyncio.sleep(900)
                continue

            # Health check antes de cada disparo qualificado
            if qualificado:
                canal_ok = await _check_whapi_bazar_health()
                if not canal_ok:
                    # Devolve o card e pausa 5 min antes de tentar de novo
                    r = _get_redis()
                    try:
                        await r.lpush(REDIS_QUEUE_KEY, json.dumps(item, ensure_ascii=False))
                    finally:
                        await r.aclose()
                    await asyncio.sleep(300)
                    continue

            if fonte == "bazar":
                await _activate_card(card, MSG_BAZAR, _qualifica_bazar, faro)
            else:
                await _activate_card(card, MSG_SITE, _qualifica_lp, faro)

            processed += 1
            label = "✅" if qualificado else "❌"
            logger.info("Fila: %s processado %s (%s) — %d restantes",
                        label, card["id"][:8], fonte, remaining)

            if remaining == 0:
                logger.info("Fila concluída — %d cards processados", processed)
                break

            if qualificado and not TEST_MODE:
                wait_sec = random.randint(BAZAR_JITTER_MIN_S, BAZAR_JITTER_MAX_S)
                logger.info("Aguardando %dm%ds antes do próximo disparo...",
                            wait_sec // 60, wait_sec % 60)
                # Sleep em fatias de 30s para reagir a JOBS_PAUSED sem esperar o jitter inteiro
                elapsed = 0
                while elapsed < wait_sec:
                    if os.getenv("JOBS_PAUSED", "false").lower() == "true":
                        logger.warning("Fila: JOBS_PAUSED=true durante jitter — interrompendo")
                        r = _get_redis()
                        try:
                            await r.delete(REDIS_RUNNING_KEY)
                        finally:
                            await r.aclose()
                        return
                    await asyncio.sleep(min(30, wait_sec - elapsed))
                    elapsed += 30

    r = _get_redis()
    try:
        await r.delete(REDIS_RUNNING_KEY)
    finally:
        await r.aclose()


async def run_watch_novos_leads_safe():
    """Wrapper resiliente para watch_novos_leads."""
    try:
        await run_watch_novos_leads()
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("run_watch_novos_leads: erro inesperado: %s", e)
