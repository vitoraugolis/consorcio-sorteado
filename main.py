"""
main.py — Ponto de entrada do sistema Consórcio Sorteado
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import PORT, SECRET_KEY, NOTIFY_PHONES, Stage
from services.slack import slack_error, slack_info
from services.session_store import health_check as redis_health, close_redis
from jobs.reativador import run_reativador
from jobs.ativacao_listas import run_ativacao_listas_safe
from jobs.ativacao_bazar_site import run_ativacao_bazar, run_ativacao_site
from jobs.fila_ativacao import run_fila_ativacao, build_queue, run_watch_novos_leads_safe
from jobs.follow_up import run_follow_up_safe
from jobs.contrato import run_contrato_safe
from jobs.precificacao import run_precificacao_safe
from webhooks.router import handle_whapi_webhook
from services.safety_car import run_pipeline_monitor

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
from logging.handlers import RotatingFileHandler
_file_handler = RotatingFileHandler(
    _log_dir / "server.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_file_handler.setFormatter(_fmt)
_root = logging.getLogger()
_root.setLevel(logging.INFO)
if not any(isinstance(h, RotatingFileHandler) for h in _root.handlers):
    _root.addHandler(_file_handler)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")

# Status dos canais Whapi — rastreado em memória para detectar transições
_whapi_canal_status: dict[str, bool] = {}  # canal -> True=online, False=offline

async def _whapi_monitor():
    """
    Monitora canais Whapi a cada 5 min.
    - Se canal cair: pausa jobs + alerta no grupo
    - Se canal voltar: retoma jobs + avisa
    """
    from services.whapi import WhapiClient, notify_team
    from config import WHAPI_LISTA_TOKENS, WHAPI_BAZAR_TOKEN, WHAPI_LP_TOKEN
    global _whapi_canal_status
    await asyncio.sleep(30)  # aguarda estabilização no boot

    while True:
        try:
            algum_offline = False
            mensagens = []

            # Monta lista de canais a checar: cada token individualmente
            canais_check: list[tuple[str, str]] = []  # (label, token)
            canais_check.append(("BAZAR (DAREDL)", WHAPI_BAZAR_TOKEN))
            if WHAPI_LP_TOKEN:
                canais_check.append(("LP (DEADPL)", WHAPI_LP_TOKEN))
            for i, tok in enumerate(WHAPI_LISTA_TOKENS, 1):
                label = f"LISTA-{i} (FALCON)" if i == 1 else f"LISTA-{i}"
                canais_check.append((label, tok))

            for label, token in canais_check:
                if not token:
                    continue
                try:
                    async with WhapiClient(token=token) as w:
                        online, status_text = await w.health_check()
                except Exception as e:
                    online = False
                    status_text = f"ERRO: {e}"

                era_online = _whapi_canal_status.get(label, True)

                if not online and era_online:
                    _whapi_canal_status[label] = False
                    mensagens.append(f"🔴 *{label}* OFFLINE (status: {status_text})")
                    logger.warning("Whapi monitor: %s OFFLINE — %s", label, status_text)
                elif online and not era_online:
                    _whapi_canal_status[label] = True
                    mensagens.append(f"🟢 *{label}* voltou online (status: {status_text})")
                    logger.info("Whapi monitor: %s voltou ONLINE", label)
                else:
                    _whapi_canal_status[label] = online

                if not online:
                    algum_offline = True

            # Pausa/retoma scheduler conforme estado
            jobs_paused_env = os.getenv("JOBS_PAUSED", "false").lower() == "true"
            if algum_offline and scheduler.state != 2:  # 2 = STATE_PAUSED
                scheduler.pause()
                logger.warning("Whapi monitor: scheduler pausado — canal(is) offline")
            elif not algum_offline and scheduler.state == 2 and not jobs_paused_env:
                scheduler.resume()
                import redis.asyncio as aioredis
                _r = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
                running = await _r.get("fila_ativacao:running")
                await _r.aclose()
                if not running:
                    asyncio.create_task(_guarded_task(run_fila_ativacao(), "fila_ativacao"))
                logger.info("Whapi monitor: scheduler retomado — todos os canais online")

            if mensagens:
                alerta = "\n".join(mensagens)
                alerta += "\n\n⚠️ Jobs pausados." if algum_offline else "\n\n✅ Jobs retomados."
                try:
                    await notify_team(alerta)
                except Exception:
                    pass

        except Exception as e:
            logger.error("Whapi monitor: erro inesperado: %s", e)

        await asyncio.sleep(300)



async def _fila_watchdog():
    """Verifica a cada 5 min se a fila está rodando e relança se necessário."""
    import redis.asyncio as aioredis
    await asyncio.sleep(60)  # aguarda 60s após startup antes de começar a checar
    while True:
        try:
            _r = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
            running = await _r.get("fila_ativacao:running")
            queue_len = await _r.llen("fila_ativacao:queue")
            if not running and queue_len > 0:
                # Usa lock para evitar duplo lançamento
                got_lock = await _r.set("fila_watchdog:lock", "1", nx=True, ex=60)
                if got_lock:
                    logger.warning("🔁 Watchdog: fila parada (%d cards) — relançando.", queue_len)
                    asyncio.create_task(_guarded_task(run_fila_ativacao(), "fila_ativacao"))
            await _r.aclose()
        except Exception as e:
            logger.warning("Watchdog fila: erro: %s", e)
        await asyncio.sleep(300)  # checa a cada 5 min


def setup_scheduler():
    # PAUSADO: número Whapi Lista restrito — só Bazar ativo por enquanto
    # Ativação de Listas — modo suave: 10 cards/ciclo, 45 min ± 10 min jitter
    scheduler.add_job(run_ativacao_listas_safe, IntervalTrigger(minutes=45, jitter=600),
                      id="ativacao_listas", name="Ativação de Listas",
                      max_instances=1, misfire_grace_time=300)
    scheduler.add_job(run_watch_novos_leads_safe, IntervalTrigger(minutes=5),
                      id="watch_novos_leads", name="Watch — Novos Leads Bazar/LP",
                      max_instances=1, misfire_grace_time=60)
    # Bazar/Site periódicos desativados — substituídos pela fila com jitter
    # scheduler.add_job(run_ativacao_bazar, IntervalTrigger(minutes=5), ...)
    # scheduler.add_job(run_ativacao_site, IntervalTrigger(minutes=5), ...)
    # Reativador pausado — alto impacto, ativar manualmente
    # scheduler.add_job(run_reativador, IntervalTrigger(hours=1), ...)
    scheduler.add_job(run_follow_up_safe, IntervalTrigger(minutes=30),
                      id="follow_up", name="Follow-up de Propostas",
                      max_instances=1, misfire_grace_time=120)
    scheduler.add_job(run_contrato_safe, IntervalTrigger(minutes=30),
                      id="contrato", name="Geração de Contratos",
                      max_instances=1, misfire_grace_time=120)
    scheduler.add_job(run_precificacao_safe, IntervalTrigger(minutes=30),
                      id="precificacao", name="Envio de Propostas",
                      max_instances=1, misfire_grace_time=60)
    # Safety Car pausado — reativar após testes
    # scheduler.add_job(run_pipeline_monitor, IntervalTrigger(minutes=15),
    #                   id="safety_car", name="Safety Car — Monitor de Pipeline",
    #                   max_instances=1, misfire_grace_time=120)
    logger.info("Scheduler configurado com %d jobs.", len(scheduler.get_jobs()))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando sistema Consórcio Sorteado...")

    # Verifica Redis
    redis_ok = await redis_health()
    if redis_ok:
        logger.info("✅ Redis conectado.")
    else:
        logger.warning("⚠️  Redis indisponível — debounce e mutex em modo degradado.")

    setup_scheduler()
    scheduler.start()
    logger.info("✅ Scheduler iniciado.")

    JOBS_PAUSED = os.getenv("JOBS_PAUSED", "false").lower() == "true"
    if JOBS_PAUSED:
        scheduler.pause()
        logger.warning("⏸️  JOBS_PAUSED=true — scheduler e fila suspensos. Canais Whapi indisponíveis.")
    elif redis_ok:
        # Relança fila de ativação automaticamente — limpa lock órfão de restart anterior
        import redis.asyncio as aioredis
        _r = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
        try:
            await _r.delete("fila_ativacao:running")
            queue_len = await _r.llen("fila_ativacao:queue")
            if queue_len == 0:
                logger.info("🔄 Fila vazia — reconstruindo do FARO...")
                await build_queue()
                queue_len = await _r.llen("fila_ativacao:queue")
            logger.info("♻️  Relançando fila (%d cards).", queue_len)
        finally:
            await _r.aclose()
        asyncio.create_task(_guarded_task(run_fila_ativacao(), "fila_ativacao"))
        asyncio.create_task(_guarded_task(_fila_watchdog(), "fila_watchdog"))

    # Monitor Whapi sempre ativo (independente de JOBS_PAUSED)
    asyncio.create_task(_guarded_task(_whapi_monitor(), "whapi_monitor"))

    asyncio.create_task(_guarded_task(
        slack_info("Sistema Consórcio Sorteado iniciado",
                   context={"Jobs ativos": str(len(scheduler.get_jobs())), "Ambiente": "Produção", "Redis": "✅" if redis_ok else "⚠️ offline"}),
        "slack_info startup",
    ))
    yield
    logger.info("🛑 Encerrando sistema...")
    scheduler.shutdown(wait=False)
    await close_redis()


async def _guarded_task(coro, label: str = "task"):
    """Wrapper para asyncio.create_task — loga exceções em vez de silenciá-las."""
    try:
        await coro
    except Exception as e:
        logger.error("Task '%s' falhou: %s", label, e)
        try:
            await slack_error(f"Task assíncrona falhou: {label}", exception=e)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Consórcio Sorteado — Automação", version="1.0.0", lifespan=lifespan)

_images_dir = Path(os.getenv("IMAGES_DIR", "/tmp/cs_images"))
_images_dir.mkdir(parents=True, exist_ok=True)
app.mount("/images", StaticFiles(directory=str(_images_dir)), name="images")


@app.get("/health")
async def health():
    jobs = [
        {"id": job.id, "name": job.name, "next_run": str(job.next_run_time) if job.next_run_time else None}
        for job in scheduler.get_jobs()
    ]
    redis_ok = await redis_health()
    return {"status": "ok", "redis": "ok" if redis_ok else "offline", "jobs": jobs}


@app.post("/jobs/pause")
async def pause_jobs(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    scheduler.pause()
    return {"status": "paused"}


@app.post("/jobs/resume")
async def resume_jobs(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    scheduler.resume()
    return {"status": "resumed"}


@app.get("/jobs/run/{job_id}")
async def run_job_manually(job_id: str, key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    job_map = {
        "reativador": run_reativador,
        "ativacao_listas": run_ativacao_listas_safe,
        "ativacao_bazar": run_ativacao_bazar,
        "ativacao_site": run_ativacao_site,
        "follow_up": run_follow_up_safe,
        "contrato": run_contrato_safe,
        "precificacao": run_precificacao_safe,
        "fila_ativacao": run_fila_ativacao,
    }
    fn = job_map.get(job_id)
    if not fn:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' não encontrado")
    logger.info("Job '%s' disparado manualmente via API", job_id)
    asyncio.create_task(_guarded_task(fn(), f"job manual: {job_id}"))
    return {"status": "triggered", "job": job_id}


@app.post("/jobs/fila/start")
async def start_fila_ativacao(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    result = await build_queue()
    if result["total"] == 0:
        return {"status": "empty", "message": "Nenhum card encontrado em Bazar ou LP"}
    asyncio.create_task(_guarded_task(run_fila_ativacao(), "fila_ativacao"))
    return {"status": "started", **result}


@app.get("/jobs/fila/status")
async def fila_status(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    import redis.asyncio as aioredis
    r = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    try:
        remaining = await r.llen("fila_ativacao:queue")
        running = await r.get("fila_ativacao:running")
    finally:
        await r.aclose()
    return {"running": bool(running), "remaining": remaining}


# ---------------------------------------------------------------------------
# Webhook único — Whapi (Z-API removido)
# ---------------------------------------------------------------------------

@app.post("/webhook/whapi")
async def webhook_whapi(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido")
    result = await handle_whapi_webhook(payload)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Webhook ZapSign
# ---------------------------------------------------------------------------

@app.post("/webhook/zapsign")
async def webhook_zapsign(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido")

    logger.info("Webhook ZapSign recebido: %s", str(payload)[:300])
    doc_token = payload.get("token", "")
    status = payload.get("status", "")
    doc_name = payload.get("name", "")

    if status != "signed":
        return JSONResponse({"status": "ignored", "reason": f"status={status}"})
    if not doc_token:
        return JSONResponse({"status": "error", "reason": "missing token"})

    asyncio.create_task(_guarded_task(
        _handle_zapsign_signed(doc_token, doc_name),
        f"zapsign signed: {doc_token[:8]}",
    ))
    return JSONResponse({"status": "received"})


async def _handle_zapsign_signed(doc_token: str, doc_name: str) -> None:
    from services.faro import FaroClient, FaroError, get_name, get_phone
    from services.whapi import WhapiClient, WhapiError

    logger.info("ZapSign: processando assinatura token %s...", doc_token[:8])
    card = None
    try:
        async with FaroClient() as faro:
            cards_assinatura = await faro.get_cards_all_pages(Stage.ASSINATURA)
            for c in cards_assinatura:
                if c.get("ZapSign Token", "") == doc_token:
                    card = c
                    break
    except FaroError as e:
        logger.error("ZapSign: erro ao buscar cards: %s", e)
        return

    if not card:
        logger.warning("ZapSign: card não encontrado para token %s...", doc_token[:8])
        if NOTIFY_PHONES:
            try:
                async with WhapiClient(canal="lista") as w:
                    for phone in NOTIFY_PHONES:
                        await w.send_text(phone,
                            f"✅ *Contrato assinado!*\nDocumento: {doc_name}\n"
                            f"⚠️ Card não encontrado automaticamente — verifique o CRM.")
            except WhapiError as e:
                logger.error("ZapSign: falha ao notificar equipe: %s", e)
        return

    card_id = card.get("id", "")
    nome = get_name(card)
    phone = get_phone(card)

    try:
        async with FaroClient() as faro:
            await faro.move_card(card_id, Stage.SUCESSO)
            await asyncio.sleep(1)
            await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
    except FaroError as e:
        logger.error("ZapSign: erro ao mover card %s: %s", card_id[:8], e)

    mensagem_equipe = (
        f"🎉 *Contrato assinado com sucesso!*\n\n"
        f"Cliente: {nome}\nTelefone: {phone or 'não informado'}\n"
        f"Documento: {doc_name}\n\n"
        f"O card foi movido para *Finalização com Agente Comercial*. 👆"
    )
    if NOTIFY_PHONES:
        try:
            async with WhapiClient(canal="lista") as w:
                for notify_phone in NOTIFY_PHONES:
                    await w.send_text(notify_phone, mensagem_equipe)
        except WhapiError as e:
            logger.error("ZapSign: falha ao notificar agente: %s", e)



# ---------------------------------------------------------------------------
# Webhook FARO (card.entered_stage)
# ---------------------------------------------------------------------------

@app.post("/webhook/faro")
async def webhook_faro(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload invalido")

    event       = payload.get("event", "")
    card_id     = payload.get("card_id", "")
    to_stage_id = payload.get("to_stage_id", "")

    logger.info("Webhook FARO: event=%s card=%s to_stage=%s",
                event,
                card_id[:8] if card_id else "",
                to_stage_id[:8] if to_stage_id else "")

    if event != "card.entered_stage" or not card_id:
        return JSONResponse({"status": "ignored", "reason": f"event={event}"})

    if to_stage_id == Stage.PRECIFICACAO:
        asyncio.create_task(_guarded_task(
            _faro_trigger_precificacao(card_id),
            f"faro precificacao: {card_id[:8]}",
        ))
        return JSONResponse({"status": "received", "action": "precificacao"})

    if to_stage_id == Stage.ACEITO:
        asyncio.create_task(_guarded_task(
            _faro_trigger_aceito(card_id),
            f"faro aceito: {card_id[:8]}",
        ))
        return JSONResponse({"status": "received", "action": "aceito"})

    return JSONResponse({"status": "ignored", "reason": f"to_stage={to_stage_id}"})


async def _faro_trigger_precificacao(card_id: str) -> None:
    from services.faro import FaroClient
    from jobs.precificacao import process_precificacao_card
    logger.info("FARO webhook: disparando precificacao para card %s...", card_id[:8])
    try:
        async with FaroClient() as faro:
            card = await faro.get_card(card_id)
        if card:
            await process_precificacao_card(card)
        else:
            logger.warning("FARO webhook: card %s nao encontrado.", card_id[:8])
    except Exception as exc:
        logger.error("FARO webhook precificacao erro card %s: %s", card_id[:8], exc)


async def _faro_trigger_aceito(card_id: str) -> None:
    from services.faro import FaroClient
    from jobs.contrato import process_contrato_card
    logger.info("FARO webhook: disparando contrato para card %s...", card_id[:8])
    try:
        async with FaroClient() as faro:
            card = await faro.get_card(card_id)
        if card:
            await process_contrato_card(card)
        else:
            logger.warning("FARO webhook: card %s nao encontrado.", card_id[:8])
    except Exception as exc:
        logger.error("FARO webhook contrato erro card %s: %s", card_id[:8], exc)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
