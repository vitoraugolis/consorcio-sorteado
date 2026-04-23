"""
main.py — Ponto de entrada do sistema Consórcio Sorteado

Sobe dois componentes no mesmo processo:
  1. FastAPI — servidor HTTP que receberá webhooks de mensagens (Whapi / Z-API)
  2. APScheduler — agendador dos jobs proativos (reativador, ativações, follow-up)

Para rodar localmente:
  uvicorn main:app --reload --port 8000

Para Railway:
  Procfile →  web: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import PORT, SECRET_KEY, Stage, NOTIFY_PHONES
from services.slack import slack_error, slack_info
from jobs.reativador import run_reativador
from jobs.ativacao_listas import run_ativacao_listas
from jobs.ativacao_bazar_site import run_ativacao_bazar, run_ativacao_site
from jobs.follow_up import run_follow_up
from jobs.contrato import run_contrato
from jobs.precificacao import run_precificacao
from webhooks.router import handle_whapi_webhook, handle_zapi_webhook

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_file_handler = RotatingFileHandler(
    _log_dir / "server.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)

# Anexa o file handler ao root logger uma única vez (evita duplicatas com uvicorn)
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
    """Registra todos os jobs com seus horários."""

    # Ativação de Listas: a cada 30 min, das 9h às 20h
    scheduler.add_job(
        run_ativacao_listas,
        trigger=IntervalTrigger(minutes=30),
        id="ativacao_listas",
        name="Ativação de Listas",
        max_instances=1,  # Nunca roda em paralelo consigo mesmo
        misfire_grace_time=120,
    )

    # Ativação Bazar: a cada 5 min (leads orgânicos precisam de resposta rápida)
    scheduler.add_job(
        run_ativacao_bazar,
        trigger=IntervalTrigger(minutes=5),
        id="ativacao_bazar",
        name="Ativação Bazar",
        max_instances=1,
        misfire_grace_time=60,
    )

    # Ativação Site/LP: a cada 5 min
    scheduler.add_job(
        run_ativacao_site,
        trigger=IntervalTrigger(minutes=5),
        id="ativacao_site",
        name="Ativação Site/LP",
        max_instances=1,
        misfire_grace_time=60,
    )

    # Reativador: 1x por hora
    scheduler.add_job(
        run_reativador,
        trigger=IntervalTrigger(hours=1),
        id="reativador",
        name="Reativador",
        max_instances=1,
        misfire_grace_time=300,
    )

    # Follow-up: a cada 30 min
    scheduler.add_job(
        run_follow_up,
        trigger=IntervalTrigger(minutes=30),
        id="follow_up",
        name="Follow-up Propostas",
        max_instances=1,
        misfire_grace_time=120,
    )

    # Contratos: a cada 5 min (lead aceita proposta → gera contrato ZapSign)
    scheduler.add_job(
        run_contrato,
        trigger=IntervalTrigger(minutes=5),
        id="contrato",
        name="Geração de Contratos",
        max_instances=1,
        misfire_grace_time=60,
    )

    # Precificação: a cada 5 min (card entra em PRECIFICACAO → envia proposta)
    scheduler.add_job(
        run_precificacao,
        trigger=IntervalTrigger(minutes=5),
        id="precificacao",
        name="Envio de Propostas",
        max_instances=1,
        misfire_grace_time=60,
    )

    logger.info("Scheduler configurado com %d jobs.", len(scheduler.get_jobs()))


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando sistema Consórcio Sorteado...")
    setup_scheduler()
    scheduler.start()
    logger.info("✅ Scheduler iniciado.")
    # Notifica Slack que o sistema subiu
    asyncio.create_task(slack_info(
        "Sistema Consórcio Sorteado iniciado",
        context={"Jobs ativos": str(len(scheduler.get_jobs())), "Ambiente": "Produção"},
    ))
    yield
    logger.info("🛑 Encerrando sistema...")
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# App FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Consórcio Sorteado — Automação",
    version="1.0.0",
    lifespan=lifespan,
)

# Serve imagens geradas pelo Playwright (propostas, etc.)
import os
from pathlib import Path
_images_dir = Path(os.getenv("IMAGES_DIR", "/tmp/cs_images"))
_images_dir.mkdir(parents=True, exist_ok=True)
app.mount("/images", StaticFiles(directory=str(_images_dir)), name="images")


# ---------------------------------------------------------------------------
# Endpoints de saúde e monitoramento
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Endpoint de health check para Railway."""
    jobs = [
        {
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
        }
        for job in scheduler.get_jobs()
    ]
    return {"status": "ok", "jobs": jobs}


@app.post("/jobs/pause")
async def pause_jobs(key: str = ""):
    """Pausa todos os jobs agendados."""
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    scheduler.pause()
    return {"status": "paused"}


@app.post("/jobs/resume")
async def resume_jobs(key: str = ""):
    """Retoma todos os jobs agendados."""
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    scheduler.resume()
    return {"status": "resumed"}


@app.get("/jobs/run/{job_id}")
async def run_job_manually(job_id: str, key: str = ""):
    """
    Dispara um job manualmente para testes.
    Protegido por query param ?key=SECRET_KEY
    """
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
    import asyncio
    asyncio.create_task(fn())
    return {"status": "triggered", "job": job_id}


# ---------------------------------------------------------------------------
# Webhooks de entrada (Whapi / Z-API)
# Será expandido nos próximos módulos (webhooks/router.py)
# ---------------------------------------------------------------------------

@app.post("/webhook/whapi")
async def webhook_whapi(request: Request):
    """
    Recebe mensagens de leads via Whapi (fluxo Listas).
    Despacha para o roteador que identifica o lead e aciona o negociador.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido")

    result = await handle_whapi_webhook(payload)
    return JSONResponse(result)


@app.post("/webhook/zapi")
async def webhook_zapi(request: Request):
    """
    Recebe mensagens de leads via Z-API (fluxos Bazar / Site).
    Despacha para o roteador que identifica o lead e aciona o negociador.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido")

    result = await handle_zapi_webhook(payload)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Webhook ZapSign — notificação de documento assinado
# ---------------------------------------------------------------------------

@app.post("/webhook/zapsign")
async def webhook_zapsign(request: Request):
    """
    Recebe notificação do ZapSign quando um documento é totalmente assinado.

    Payload esperado (simplificado):
        {
            "token": "<doc_token>",
            "status": "signed",   # ou "pending", "refused"
            "open_id": 12345,
            "name": "Contrato - João Silva - Santander",
            "signers": [...]
        }

    Fluxo:
      1. Extrai doc_token do payload
      2. Verifica status == "signed"
      3. Encontra o card pelo campo "ZapSign Token"
      4. Move card: ASSINATURA → SUCESSO → FINALIZACAO_COMERCIAL
      5. Notifica agente humano via WhatsApp
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido")

    logger.info("Webhook ZapSign recebido: %s", str(payload)[:300])

    doc_token  = payload.get("token", "")
    status     = payload.get("status", "")
    doc_name   = payload.get("name", "")

    # Só processa eventos de assinatura completa
    if status != "signed":
        logger.info("ZapSign webhook ignorado: status='%s' (aguardando 'signed')", status)
        return JSONResponse({"status": "ignored", "reason": f"status={status}"})

    if not doc_token:
        logger.warning("ZapSign webhook sem doc_token no payload")
        return JSONResponse({"status": "error", "reason": "missing token"})

    logger.info("ZapSign: documento '%s' (token=%s...) totalmente assinado!", doc_name, doc_token[:8])

    # Processa em background para não bloquear o webhook
    asyncio.create_task(_handle_zapsign_signed(doc_token, doc_name))

    return JSONResponse({"status": "received"})


async def _handle_zapsign_signed(doc_token: str, doc_name: str) -> None:
    """
    Processa a assinatura completa de um documento ZapSign:
    encontra o card pelo token, move para Sucesso e notifica a equipe.
    """
    from services.faro import FaroClient, FaroError
    from services.whapi import WhapiClient, WhapiError
    from services.faro import get_name, get_phone

    logger.info("ZapSign: processando assinatura completa do token %s...", doc_token[:8])

    # Busca card pelo ZapSign Token no FARO
    card = None
    try:
        async with FaroClient() as faro:
            # Busca todos os cards no stage ASSINATURA e filtra pelo token
            cards_assinatura = await faro.get_cards_all_pages(Stage.ASSINATURA)
            for c in cards_assinatura:
                if c.get("ZapSign Token", "") == doc_token:
                    card = c
                    break
    except FaroError as e:
        logger.error("ZapSign webhook: erro ao buscar cards no FARO: %s", e)
        return

    if not card:
        logger.warning(
            "ZapSign webhook: nenhum card encontrado com ZapSign Token='%s...' no stage ASSINATURA. "
            "O card pode ter sido movido manualmente ou o token não foi salvo.",
            doc_token[:8]
        )
        # Mesmo assim, notifica equipe para que tomem ação manual
        if NOTIFY_PHONES:
            try:
                async with WhapiClient() as w:
                    for phone in NOTIFY_PHONES:
                        await w.send_text(
                            phone,
                            f"✅ *Contrato assinado!*\n\n"
                            f"Documento: {doc_name}\n"
                            f"Token: {doc_token[:12]}...\n\n"
                            f"⚠️ Card não encontrado automaticamente — verifique o CRM."
                        )
            except WhapiError as e:
                logger.error("ZapSign webhook: falha ao notificar equipe: %s", e)
        return

    card_id = card.get("id", "")
    nome    = get_name(card)
    phone   = get_phone(card)

    logger.info("ZapSign: card encontrado — %s (%s). Movendo para SUCESSO...", nome, card_id[:8])

    # Move: ASSINATURA → SUCESSO → FINALIZACAO_COMERCIAL
    try:
        async with FaroClient() as faro:
            await faro.move_card(card_id, Stage.SUCESSO)
            logger.info("ZapSign: card %s movido para SUCESSO", card_id[:8])

            await asyncio.sleep(1)  # Pequena pausa para o CRM processar

            await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
            logger.info("ZapSign: card %s movido para FINALIZACAO_COMERCIAL", card_id[:8])

    except FaroError as e:
        logger.error("ZapSign webhook: erro ao mover card %s: %s", card_id[:8], e)

    # Notifica agente humano
    mensagem_equipe = (
        f"🎉 *Contrato assinado com sucesso!*\n\n"
        f"Cliente: {nome}\n"
        f"Telefone: {phone or 'não informado'}\n"
        f"Documento: {doc_name}\n\n"
        f"O card foi movido para *Finalização com Agente Comercial*.\n"
        f"Por favor, prossiga com o processo de finalização. 👆"
    )

    if NOTIFY_PHONES:
        try:
            async with WhapiClient() as w:
                for notify_phone in NOTIFY_PHONES:
                    await w.send_text(notify_phone, mensagem_equipe)
        except WhapiError as e:
            logger.error("ZapSign webhook: falha ao notificar agente: %s", e)


# ---------------------------------------------------------------------------
# Entrypoint direto (sem uvicorn CLI)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
