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

from config import PORT, SECRET_KEY, NOTIFY_PHONES
from services.slack import slack_error, slack_info
from jobs.reativador import run_reativador
from jobs.ativacao_listas import run_ativacao_listas
from jobs.ativacao_bazar_site import run_ativacao_bazar, run_ativacao_site
from jobs.follow_up import run_follow_up
from jobs.contrato import run_contrato
from jobs.precificacao import run_precificacao
from webhooks.router import handle_whapi_webhook

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


def setup_scheduler():
    scheduler.add_job(run_ativacao_listas, IntervalTrigger(minutes=30),
                      id="ativacao_listas", name="Ativação de Listas",
                      max_instances=1, misfire_grace_time=120)
    scheduler.add_job(run_ativacao_bazar, IntervalTrigger(minutes=5),
                      id="ativacao_bazar", name="Ativação Bazar",
                      max_instances=1, misfire_grace_time=60)
    scheduler.add_job(run_ativacao_site, IntervalTrigger(minutes=5),
                      id="ativacao_site", name="Ativação Site/LP",
                      max_instances=1, misfire_grace_time=60)
    scheduler.add_job(run_reativador, IntervalTrigger(hours=1),
                      id="reativador", name="Reativador",
                      max_instances=1, misfire_grace_time=300)
    scheduler.add_job(run_follow_up, IntervalTrigger(minutes=30),
                      id="follow_up", name="Follow-up Propostas",
                      max_instances=1, misfire_grace_time=120)
    scheduler.add_job(run_contrato, IntervalTrigger(minutes=5),
                      id="contrato", name="Geração de Contratos",
                      max_instances=1, misfire_grace_time=60)
    scheduler.add_job(run_precificacao, IntervalTrigger(minutes=5),
                      id="precificacao", name="Envio de Propostas",
                      max_instances=1, misfire_grace_time=60)
    logger.info("Scheduler configurado com %d jobs.", len(scheduler.get_jobs()))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando sistema Consórcio Sorteado...")
    setup_scheduler()
    scheduler.start()
    logger.info("✅ Scheduler iniciado.")
    asyncio.create_task(_guarded_task(
        slack_info("Sistema Consórcio Sorteado iniciado",
                   context={"Jobs ativos": str(len(scheduler.get_jobs())), "Ambiente": "Produção"}),
        "slack_info startup",
    ))
    yield
    logger.info("🛑 Encerrando sistema...")
    scheduler.shutdown(wait=False)


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
    return {"status": "ok", "jobs": jobs}


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
        "ativacao_listas": run_ativacao_listas,
        "ativacao_bazar": run_ativacao_bazar,
        "ativacao_site": run_ativacao_site,
        "follow_up": run_follow_up,
        "contrato": run_contrato,
        "precificacao": run_precificacao,
    }
    fn = job_map.get(job_id)
    if not fn:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' não encontrado")
    logger.info("Job '%s' disparado manualmente via API", job_id)
    asyncio.create_task(_guarded_task(fn(), f"job manual: {job_id}"))
    return {"status": "triggered", "job": job_id}


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

    from config import Stage as _Stage
    card_id = card.get("id", "")
    nome = get_name(card)
    phone = get_phone(card)

    try:
        async with FaroClient() as faro:
            await faro.move_card(card_id, _Stage.SUCESSO)
            await asyncio.sleep(1)
            await faro.move_card(card_id, _Stage.FINALIZACAO_COMERCIAL)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
