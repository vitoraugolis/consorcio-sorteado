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
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import PORT, SECRET_KEY, NOTIFY_PHONES, Stage
from services.slack import slack_error, slack_info
from services.session_store import health_check as redis_health, close_redis
from services.faro import close_faro_pool
from jobs.reativador import run_reativador
from jobs.ativacao_listas import run_ativacao_listas_safe
from jobs.fila_listas import run_fila_listas_safe
from jobs.ativacao_bazar_site import run_ativacao_bazar, run_ativacao_site
from jobs.fila_ativacao import run_fila_ativacao, build_queue, run_watch_novos_leads_safe
from jobs.precificacao import run_precificacao_safe
from jobs.relatorio_funil import run_relatorio_funil, run_relatorio_retroativo
from jobs.relatorio_disparos import run_relatorio_disparos


async def _relatorio_slack_fim_dia():
    """
    Envia resumo diário de disparos para o #log-cs às 20h BRT (23h UTC).
    Coleta os contadores Redis do dia atual e formata mensagem Slack.
    """
    from services.stats import get_daily_stats
    from services.slack import slack_log_cs_raw
    from datetime import datetime
    from config import TZ_BRASILIA

    stats = await get_daily_stats()  # dia atual
    data_fmt = datetime.now(TZ_BRASILIA).strftime("%d/%m/%Y")

    total = stats.get("total_disparos", 0)
    propostas = stats.get("propostas", 0)
    status_emoji = "✅" if total > 0 else "⚠️"

    msg = (
        f"📊 *Relatório de disparos — {data_fmt}*\n\n"
        f"{'─' * 30}\n"
        f"📋 *Listas (nova ativação):*  {stats.get('listas', 0)}\n"
        f"1️⃣  *1ª Ativação:*  {stats.get('ativacao_1', 0)}\n"
        f"2️⃣  *2ª Ativação:*  {stats.get('ativacao_2', 0)}\n"
        f"3️⃣  *3ª Ativação:*  {stats.get('ativacao_3', 0)}\n"
        f"4️⃣  *4ª Ativação:*  {stats.get('ativacao_4', 0)}\n"
        f"{'─' * 30}\n"
        f"{status_emoji} *Total disparos:*  *{total}*\n"
        f"🎯 *Propostas enviadas:*  *{propostas}*"
    )
    await slack_log_cs_raw(msg)
    logger.info("Relatório fim de dia enviado para #log-cs — total=%d propostas=%d", total, propostas)
from jobs.watchdog_extratos import run_watchdog_extratos
from jobs.escalador_bazar_lp import run_escalador_bazar_lp_safe
from jobs.readme_operacoes import run_readme_operacoes_safe
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
    _log_dir / "server.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8",
)
_file_handler.setFormatter(_fmt)
_file_handler.namer  = lambda name: name + ".gz"
_file_handler.rotator = lambda source, dest: __import__("gzip").open(dest, "wb").write(
    open(source, "rb").read()) or __import__("os").remove(source)
_root = logging.getLogger()
_root.setLevel(logging.INFO)
if not any(isinstance(h, RotatingFileHandler) for h in _root.handlers):
    _root.addHandler(_file_handler)

import sentry_sdk
# FastApiIntegration removida — conflito com asynccontextmanager lifespan
from sentry_sdk.integrations.asyncio import AsyncioIntegration

logger = logging.getLogger(__name__)

_SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        integrations=[AsyncioIntegration()],
        traces_sample_rate=0.05,
        environment=os.getenv("ENVIRONMENT", "production"),
    )
    # logger ainda não está configurado aqui — imprimimos diretamente
    print("[startup] Sentry inicializado")
else:
    print("[startup] Sentry: SENTRY_DSN não configurado — monitoramento de erros desabilitado")

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
            # Labels com channel ID para identificação rápida no grupo de alertas
            _LISTA_CHANNEL_IDS = [
                os.getenv("WHAPI_CHANNEL_ID_LISTA_1", "FALCON"),
                os.getenv("WHAPI_CHANNEL_ID_LISTA_2", "GROOTT"),
                os.getenv("WHAPI_CHANNEL_ID_LISTA_3", "WOLVRN"),
                os.getenv("WHAPI_CHANNEL_ID_LISTA_4", "DEADPL"),
                os.getenv("WHAPI_CHANNEL_ID_LISTA_5", "DAREDL"),
                os.getenv("WHAPI_CHANNEL_ID_LISTA_6", "IRONMN"),
            ]
            canais_check: list[tuple[str, str]] = []  # (label, token)
            for i, tok in enumerate(WHAPI_LISTA_TOKENS, 1):
                channel_id = (_LISTA_CHANNEL_IDS[i - 1] if i <= len(_LISTA_CHANNEL_IDS) else f"LISTA-{i}").split("-")[0]
                label = f"LISTA-{i} ({channel_id})"
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
                    mensagens.append(f"🔴 {label} OFFLINE (status: {status_text})")
                    logger.warning("Whapi monitor: %s OFFLINE — %s", label, status_text)
                elif online and not era_online:
                    _whapi_canal_status[label] = True
                    mensagens.append(f"🟢 {label} voltou ONLINE")
                    logger.info("Whapi monitor: %s voltou ONLINE", label)
                else:
                    _whapi_canal_status[label] = online

                if not online:
                    algum_offline = True

            from services.slack import slack_log_cs_raw
            canais_com_token = [label for label, token in canais_check if token]
            todos_offline_agora = all(
                not _whapi_canal_status.get(label, True) for label in canais_com_token
            )
            algum_caiu  = any(msg.startswith("🔴") for msg in mensagens)
            algum_voltou = any(msg.startswith("🟢") for msg in mensagens)

            # Notifica queda no #log-cs
            if algum_caiu:
                canais_offline = [l for l in canais_com_token if not _whapi_canal_status.get(l, True)]
                canais_online  = [l for l in canais_com_token if _whapi_canal_status.get(l, True)]
                linhas_offline = "\n".join(f"  • {l}" for l in canais_offline)
                sufixo = f"\n✅ *Ainda online:* {', '.join(canais_online)}" if canais_online else "\n⛔ *Todos os canais offline — disparos paralisados*"
                msg_queda = (
                    f"🔴 *Canal(is) Whapi offline*\n"
                    f"{linhas_offline}{sufixo}"
                )
                asyncio.create_task(_guarded_task(
                    slack_log_cs_raw(msg_queda),
                    "whapi_monitor slack queda",
                    critical=False,
                ))
                logger.info("Whapi monitor: queda detectada — notificando Slack #log-cs")

            # Notifica retomada no #log-cs
            if algum_voltou and not todos_offline_agora:
                canais_online  = [l for l in canais_com_token if _whapi_canal_status.get(l, True)]
                canais_offline = [l for l in canais_com_token if not _whapi_canal_status.get(l, True)]
                resumo_online  = ", ".join(canais_online)
                sufixo_offline = f"\n⚠️ *Ainda offline:* {', '.join(canais_offline)}" if canais_offline else ""
                msg_retomada = (
                    f"✅ *Disparos retomados* — canais Whapi online\n"
                    f"*Online:* {resumo_online}{sufixo_offline}\n"
                    f"_Fila Listas retoma no próximo ciclo (≤5 min)_"
                )
                asyncio.create_task(_guarded_task(
                    slack_log_cs_raw(msg_retomada),
                    "whapi_monitor slack retomada",
                    critical=False,
                ))
                logger.info("Whapi monitor: disparos retomados — notificando Slack #log-cs")

        except Exception as e:
            logger.error("Whapi monitor: erro inesperado: %s", e)

        await asyncio.sleep(300)



async def _fila_watchdog():
    """Verifica a cada 5 min se a fila está rodando e relança se necessário.
    NOTA: Bazar cadenciado (ativacao_bazar_cadenciada) foi desativado em favor da fila Redis.
    """
    import redis.asyncio as aioredis
    await asyncio.sleep(60)  # aguarda 60s após startup antes de começar a checar
    while True:
        try:
            # Não relança se JOBS_PAUSED
            if os.getenv("JOBS_PAUSED", "false").lower() == "true":
                await asyncio.sleep(300)
                continue
            _r = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
            running = await _r.get("fila_ativacao:running")
            queue_len = await _r.llen("fila_ativacao:queue")
            if not running and queue_len > 0:
                # Usa lock para evitar duplo lançamento
                got_lock = await _r.set("fila_watchdog:lock", "1", nx=True, ex=60)
                if got_lock:
                    logger.warning("🔁 Watchdog: fila parada (%d cards) — relançando.", queue_len)
                    asyncio.create_task(_guarded_task(run_fila_ativacao(), "fila_ativacao", critical=True))
            await _r.aclose()
        except Exception as e:
            logger.warning("Watchdog fila: erro: %s", e)
        await asyncio.sleep(300)  # checa a cada 5 min


def setup_scheduler():
    # ✅ FILA LISTAS — substitui ativacao_listas + reativador para leads lista/lp
    # 1 disparo a cada ~5 min (CICLO_BASE_S=300 ± CICLO_JITTER_S=90)
    # Prioridade: Listas → 1ª → 2ª → 3ª → 4ª Ativação | gap mínimo 25 min por token
    scheduler.add_job(run_fila_listas_safe, IntervalTrigger(minutes=5, jitter=90),
                      id="fila_listas", name="Fila Unificada — Listas + Ativações",
                      max_instances=1, misfire_grace_time=60)
    # ⬇️ ativacao_listas DESATIVADO — substituído por fila_listas
    # scheduler.add_job(run_ativacao_listas_safe, IntervalTrigger(minutes=30, jitter=300), ...)
    # watch_novos_leads: ativo — injeta Bazar/LP novos na fila a cada 5 min
    scheduler.add_job(run_watch_novos_leads_safe, IntervalTrigger(minutes=5),
                      id="watch_novos_leads", name="Watch — Novos Leads Bazar/LP",
                      max_instances=1, misfire_grace_time=60)
    # Bazar: controlado pela fila cadenciada — não pelo scheduler em batch
    # scheduler.add_job(run_ativacao_bazar, IntervalTrigger(minutes=30, jitter=120), ...)
    # scheduler.add_job(run_ativacao_bazar, IntervalTrigger(minutes=5), ...)
    # scheduler.add_job(run_ativacao_site, IntervalTrigger(minutes=5), ...)
    # Reativador: mantido APENAS para leads Bazar (lista/lp agora gerenciados pela fila_listas)
    # scheduler.add_job(run_reativador, IntervalTrigger(hours=1), ...)
    scheduler.add_job(run_reativador, IntervalTrigger(hours=4),
                      id="reativador", name="Reativador de Leads Bazar (Inativos)",
                      max_instances=1, misfire_grace_time=300)
    # follow_up, contrato, sla_monitor, auditoria_propostas DESATIVADOS
    # Último processo automático é PRECIFICAÇÃO — a partir daí, agentes comerciais assumem
    scheduler.add_job(run_precificacao_safe, IntervalTrigger(minutes=30),
                      id="precificacao", name="Envio de Propostas",
                      max_instances=1, misfire_grace_time=60)
    # sla_monitor DESATIVADO — sem negociação não há SLA pós-proposta a monitorar
    # Relatório diário de funil — 08h BRT (11h UTC)
    scheduler.add_job(run_relatorio_funil, CronTrigger(hour=3, minute=0, timezone="UTC"),
                      id="relatorio_funil", name="Relatório Diário de Funil",
                      max_instances=1, misfire_grace_time=600)
    # Relatório diário de disparos — 07h BRT (10h UTC)
    scheduler.add_job(run_relatorio_disparos, CronTrigger(hour=10, minute=0, timezone="UTC"),
                      id="relatorio_disparos", name="Relatório Diário de Disparos",
                      max_instances=1, misfire_grace_time=600)
    # Relatório fim de dia no Slack #log-cs — 23h BRT (02h UTC dia seguinte)
    scheduler.add_job(_relatorio_slack_fim_dia, CronTrigger(hour=2, minute=0, timezone="UTC"),
                      id="relatorio_slack_fim_dia", name="Relatório Slack Fim de Dia",
                      max_instances=1, misfire_grace_time=600)
    # Safety Car — monitor de pipeline a cada 15min
    scheduler.add_job(run_pipeline_monitor, IntervalTrigger(minutes=15),
                      id="safety_car", name="Safety Car — Monitor de Pipeline",
                      max_instances=1, misfire_grace_time=120)
    scheduler.add_job(run_watchdog_extratos, IntervalTrigger(minutes=5),
                      id="watchdog_extratos", name="Watchdog — Extratos não processados",
                      max_instances=1, misfire_grace_time=60)
    scheduler.add_job(run_escalador_bazar_lp_safe, IntervalTrigger(minutes=30),
                      id="escalador_bazar_lp", name="Escalador 3h — Bazar/LP sem resposta",
                      max_instances=1, misfire_grace_time=120)
    # README de operações — atualiza a cada 30 min e faz commit/push
    scheduler.add_job(run_readme_operacoes_safe, IntervalTrigger(minutes=30),
                      id="readme_operacoes", name="README Operações — Auto-update",
                      max_instances=1, misfire_grace_time=120)
    # auditoria_propostas DESATIVADA — sem negociação não há propostas a auditar
    logger.info("Scheduler configurado com %d jobs.", len(scheduler.get_jobs()))




async def _start_lp_retro_async():
    from jobs.ativacao_lp_retroativa import start as _start
    result = await _start(interval_min=15, interval_max=20)
    if result.get("total", 0) == 0:
        logger.info("LP Retro startup: nenhum lead pendente.")
    else:
        logger.info("LP Retro startup: %d leads na fila.", result.get("total", 0))
# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

async def _recover_debounce() -> None:
    """
    Varredura de startup: detecta e reprocessa mensagens de debounce
    que sobreviveram a um restart (buffer Redis ainda presente).
    Aguarda 10s para estabilização do pool FARO antes de iniciar.
    """
    from services.session_store import get_redis, pop_debounce_buffer
    from services.faro import FaroClient, FaroError, get_canal
    from config import Stage
    await asyncio.sleep(10)  # aguarda pool FARO warm + scheduler estabilizar
    try:
        r = await get_redis()
        keys = await r.keys("cs:debounce:*")
        if not keys:
            return
        logger.info("Debounce recovery: %d chaves encontradas no Redis", len(keys))
        for key in keys:
            raw_key = key if isinstance(key, str) else key.decode()
            phone = raw_key.replace("cs:debounce:", "")
            texts = await pop_debounce_buffer(phone)
            if not texts:
                continue
            combined = " ".join(t if isinstance(t, str) else t.decode() for t in texts)
            logger.info("Debounce recovery: phone=...%s, %d msg(s) pendente(s)", phone[-6:], len(texts))
            # Retry com backoff para o pool FARO
            card = None
            for attempt in range(3):
                try:
                    async with FaroClient() as faro:
                        card = await faro.find_card_by_phone(phone)
                    break
                except Exception as e:
                    wait = (attempt + 1) * 5
                    logger.warning(
                        "Debounce recovery: tentativa %d de buscar card ...%s falhou (%s) — retry em %ds",
                        attempt + 1, phone[-6:], e, wait,
                    )
                    await asyncio.sleep(wait)

            if not card:
                logger.warning("Debounce recovery: card não encontrado para ...%s — descartando", phone[-6:])
                continue
            stage = card.get("stage_id") or ""
            canal = get_canal(card)
            try:
                # PRECIFICACAO e estágios pós-precificação: IA não responde mais
                # Agentes comerciais são responsáveis a partir daqui
                if stage in (Stage.PRECIFICACAO, Stage.EM_NEGOCIACAO, Stage.ACEITO, Stage.ASSINATURA):
                    logger.info("Debounce recovery: stage %s pós-precificação para ...%s — descartando (comercial assume)", stage[:8], phone[-6:])
                    continue
                elif canal == "lista":
                    from webhooks.agente_listas import handle_message as _lista_handle
                    await _lista_handle(card, combined)
                elif canal in ("bazar", "lp"):
                    from webhooks.agente_bazar import handle_message as _bazar_handle
                    await _bazar_handle(card, combined)
                else:
                    logger.info("Debounce recovery: stage/canal desconhecido para ...%s — descartando", phone[-6:])
                    continue
                logger.info("Debounce recovery: mensagem reprocessada para ...%s (stage=%s)", phone[-6:], stage[:8])
            except Exception as e:
                logger.warning("Debounce recovery: erro ao reprocessar ...%s: %s", phone[-6:], e)
    except Exception as e:
        logger.warning("Debounce recovery: falha na varredura Redis: %s", e)



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
            # Lança fila Bazar/LP no startup
            logger.info("▶️  Lançando fila_ativacao Bazar/LP...")
            asyncio.create_task(_guarded_task(run_fila_ativacao(), "fila_ativacao", critical=True))
            # NOTA: Bazar cadenciado desativado (item 2) — fila Redis é o mecanismo principal
            # Relança LP Retroativa se houver leads pendentes
            from jobs.ativacao_lp_retroativa import get_status as lp_retro_status
            _lp_status = lp_retro_status()
            if not _lp_status.get("running"):
                logger.info("▶️  Relançando LP Retroativa no startup...")
                asyncio.create_task(_guarded_task(
                    _start_lp_retro_async(),
                    "lp_retro startup",
                    critical=True,
                ))

    # Recovery de debounce: reprocessa mensagens acumuladas antes do restart
    if redis_ok:
        asyncio.create_task(_guarded_task(_recover_debounce(), "debounce_recovery", critical=True))

    # Monitor Whapi sempre ativo (independente de JOBS_PAUSED)
    asyncio.create_task(_guarded_task(_whapi_monitor(), "whapi_monitor"))

    asyncio.create_task(_guarded_task(
        slack_info("Sistema Consórcio Sorteado iniciado",
                   context={"Jobs ativos": str(len(scheduler.get_jobs())), "Ambiente": "Produção", "Redis": "✅" if redis_ok else "⚠️ offline"}),
        "slack_info startup",
        critical=False,
    ))
    yield
    logger.info("🛑 Encerrando sistema...")
    scheduler.shutdown(wait=False)
    await close_redis()
    await close_faro_pool()
    logger.info("Shutdown completo.")


async def _guarded_task(coro, label: str = "task", critical: bool = False):
    """Wrapper para asyncio.create_task — loga exceções em vez de silenciá-las.
    critical=True: captura no Sentry quando SENTRY_DSN configurado.
    """
    try:
        await coro
    except Exception as e:
        logger.error("Task '%s' falhou: %s", label, e, exc_info=True)
        if critical and _SENTRY_DSN:
            sentry_sdk.capture_exception(e)
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


@app.post("/jobs/bazar/pause")
async def pause_bazar(key: str = ""):
    """Pausa apenas os disparos de primeira ativação do Bazar (LP continua)."""
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    os.environ["BAZAR_ATIVACAO_ENABLED"] = "false"
    logger.warning("⏸️  BAZAR_ATIVACAO_ENABLED=false — disparos Bazar pausados via API")
    return {"status": "paused", "canal": "bazar", "BAZAR_ATIVACAO_ENABLED": "false"}


@app.post("/jobs/bazar/resume")
async def resume_bazar(key: str = ""):
    """Retoma os disparos de primeira ativação do Bazar."""
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    os.environ["BAZAR_ATIVACAO_ENABLED"] = "true"
    logger.info("▶️  BAZAR_ATIVACAO_ENABLED=true — disparos Bazar retomados via API")
    return {"status": "resumed", "canal": "bazar", "BAZAR_ATIVACAO_ENABLED": "true"}


@app.post("/jobs/lp/pause")
async def pause_lp(key: str = ""):
    """Pausa apenas os disparos de primeira ativação da LP (Bazar continua)."""
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    os.environ["LP_ATIVACAO_ENABLED"] = "false"
    # Para LP Retroativa se estiver rodando
    from jobs.ativacao_lp_retroativa import stop as lp_retro_stop, get_status as lp_retro_status
    retro = lp_retro_stop() if lp_retro_status().get("running") else {"status": "already_stopped"}
    logger.warning("⏸️  LP_ATIVACAO_ENABLED=false — disparos LP pausados via API")
    return {"status": "paused", "canal": "lp", "LP_ATIVACAO_ENABLED": "false", "lp_retro": retro}


@app.post("/jobs/lp/resume")
async def resume_lp(key: str = ""):
    """Retoma os disparos de primeira ativação da LP."""
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    os.environ["LP_ATIVACAO_ENABLED"] = "true"
    logger.info("▶️  LP_ATIVACAO_ENABLED=true — disparos LP retomados via API")
    return {"status": "resumed", "canal": "lp", "LP_ATIVACAO_ENABLED": "true"}


@app.get("/jobs/run/{job_id}")
async def run_job_manually(job_id: str, key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    job_map = {
        "reativador": run_reativador,
        "ativacao_listas": run_ativacao_listas_safe,  # mantido para trigger manual legado
        "fila_listas": run_fila_listas_safe,
        "ativacao_bazar": run_ativacao_bazar,
        "ativacao_site": run_ativacao_site,
        "precificacao": run_precificacao_safe,
        "fila_ativacao": run_fila_ativacao,
        # follow_up, contrato, auditoria_propostas DESATIVADOS
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
    asyncio.create_task(_guarded_task(run_fila_ativacao(), "fila_ativacao", critical=True))


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


@app.post("/jobs/lp-retro/start")
async def lp_retro_start(key: str = "", interval_min: int = 15, interval_max: int = 20, resume_from: int = 0):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    from jobs.ativacao_lp_retroativa import start
    return await start(interval_min=interval_min, interval_max=interval_max, resume_from=resume_from)


@app.get("/jobs/lp-retro/status")
async def lp_retro_status(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    from jobs.ativacao_lp_retroativa import get_status
    return get_status()


@app.post("/jobs/lp-retro/stop")
async def lp_retro_stop(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    from jobs.ativacao_lp_retroativa import stop
    return stop()


@app.post("/jobs/bazar-loop/start")
async def bazar_loop_start(key: str = "", interval_min: int = 30, interval_max: int = 35):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    from jobs.ativacao_bazar_cadenciada import start
    return start(interval_min=interval_min, interval_max=interval_max)


@app.get("/jobs/bazar-loop/status")
async def bazar_loop_status(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    from jobs.ativacao_bazar_cadenciada import get_status
    return get_status()


@app.post("/jobs/bazar-loop/stop")
async def bazar_loop_stop(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    from jobs.ativacao_bazar_cadenciada import stop
    return stop()


# ---------------------------------------------------------------------------
# Webhook único — Whapi (Z-API removido)
# ---------------------------------------------------------------------------

@app.post("/webhook/whapi")
async def webhook_whapi(request: Request):
    import os
    from config import WHAPI_LISTA_TOKENS, WHAPI_BAZAR_TOKEN, WHAPI_LP_TOKEN
    valid_tokens = set(t for t in [WHAPI_BAZAR_TOKEN, WHAPI_LP_TOKEN] + WHAPI_LISTA_TOKENS if t)

    # Mapeamento estático channel_id → token (evita lookup dinâmico e resolve webhooks sem token no header)
    _channel_id_map: dict[str, str] = {}
    for _i, _tok in enumerate(WHAPI_LISTA_TOKENS, 1):
        _cid = os.getenv(f"WHAPI_CHANNEL_ID_LISTA_{_i}", "")
        if _cid and _tok:
            _channel_id_map[_cid] = _tok
    for _label, _env in (("BAZAR", "WHAPI_CHANNEL_ID_BAZAR"), ("LP", "WHAPI_CHANNEL_ID_LP")):
        _cid = os.getenv(_env, "")
        _tok = WHAPI_BAZAR_TOKEN if _label == "BAZAR" else WHAPI_LP_TOKEN
        if _cid and _tok:
            _channel_id_map[_cid] = _tok

    if valid_tokens:
        received = (
            request.headers.get("X-Whapi-Token", "")
            or request.headers.get("Authorization", "").removeprefix("Bearer ")
            or request.query_params.get("token", "")
        )
        if received not in valid_tokens:
            # Token ausente/inválido — tenta resolver pelo channel_id do body (Whapi às vezes omite token)
            try:
                body_bytes = await request.body()
                import json as _json
                body = _json.loads(body_bytes)
                channel_id = (
                    body.get("channel_id") or body.get("channelId")
                    or (body.get("event") or {}).get("channel_id") or ""
                )
            except Exception:
                body = {}
                channel_id = ""

            if channel_id and channel_id in _channel_id_map:
                # channel_id reconhecido — aceitar e processar normalmente
                logger.debug(
                    "Webhook Whapi: sem token mas channel_id=%s reconhecido (canal=%s) — aceito",
                    channel_id,
                    next((k for k, v in {"BAZAR": WHAPI_BAZAR_TOKEN, "LP": WHAPI_LP_TOKEN}.items()
                          if v == _channel_id_map[channel_id]), "lista"),
                )
                result = await handle_whapi_webhook(body)
                return JSONResponse(result)
            else:
                logger.warning(
                    "Webhook Whapi: rejeitado de %s (received=%r, channel_id=%r)",
                    getattr(request.client, 'host', '?'),
                    received[:20] if received else "",
                    channel_id or "?",
                )
                raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload invalido")
    result = await handle_whapi_webhook(payload)
    return JSONResponse(result)

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

    # Stage.ACEITO e demais pós-precificação: IA desativada — agentes comerciais assumem
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")


# ---------------------------------------------------------------------------
# Webhook Slack — aprovação manual Safety Car
# ---------------------------------------------------------------------------

@app.post("/webhook/slack-approval")
async def slack_approval_webhook(request: Request):
    """
    Recebe mensagens do Slack (via Outgoing Webhook ou Slash Command).
    Formato esperado: "<card_id[:8]> precificação ok" ou "<card_id[:8]> negociação ok"
    """
    try:
        body = await request.body()
        # Suporta JSON e form-encoded (Slack usa form-encoded em webhooks)
        try:
            data = await request.json()
        except Exception:
            from urllib.parse import parse_qs
            parsed = parse_qs(body.decode("utf-8", errors="replace"))
            data = {k: v[0] for k, v in parsed.items()}

        # Texto pode vir em "text" (Slack Outgoing Webhook) ou "payload"
        text = (
            data.get("text") or
            data.get("message") or
            data.get("payload") or ""
        ).strip().lower()

        if not text:
            return {"ok": False, "reason": "empty text"}

        from services.safety_car import ApprovalKind, approve

        # Detecta padrão: "<8chars> precificação ok" ou "<8chars> negociação ok"
        import re
        m = re.match(
            r"^([a-f0-9]{6,8})\s+(precifica[cç][aã]o|negocia[cç][aã]o)\s+ok\b",
            text,
            re.IGNORECASE | re.UNICODE,
        )
        if not m:
            return {"ok": False, "reason": "pattern not matched", "received": text[:80]}

        card_prefix = m.group(1).lower()
        tipo_raw    = m.group(2).lower()

        # Normaliza para o enum
        if "precif" in tipo_raw:
            kind = ApprovalKind.PRECIFICACAO
        else:
            kind = ApprovalKind.NEGOCIACAO

        ok = await approve(card_prefix, kind)
        if ok:
            logger.info("slack_approval: aprovado %s para prefixo %s", kind.value, card_prefix)
            return {"ok": True, "kind": kind.value, "card_prefix": card_prefix}
        else:
            logger.warning("slack_approval: pendência não encontrada para %s (%s)", card_prefix, kind.value)
            return {"ok": False, "reason": "pending not found", "card_prefix": card_prefix, "kind": kind.value}

    except Exception as e:
        logger.error("slack_approval: erro: %s", e)
        return {"ok": False, "error": str(e)}


@app.get("/jobs/safety-car/pending")
async def safety_car_pending(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    from services.safety_car import list_pending
    return {"pending": await list_pending()}


@app.post("/jobs/relatorio-funil/run")
async def trigger_relatorio_funil(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    import asyncio
    asyncio.create_task(run_relatorio_funil())
    return {"status": "started", "message": "Relatório de funil disparado em background"}


@app.post("/jobs/relatorio-disparos/run")
async def trigger_relatorio_disparos(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    import asyncio
    asyncio.create_task(_guarded_task(run_relatorio_disparos(), "relatorio_disparos manual"))
    return {"status": "started", "message": "Relatório de disparos disparado em background"}


@app.post("/jobs/relatorio-funil/retroativo")
async def trigger_relatorio_retroativo(key: str = "", datas: list[str] = None):
    """
    Roda relatório retroativo para uma lista de datas.
    Body: {"datas": ["04/05/2026", "05/05/2026"]}
    Formato aceito: DD/MM/YYYY
    """
    if key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Chave inválida")
    if not datas:
        raise HTTPException(status_code=400, detail="Lista de datas obrigatória")
    import re as _re
    _DATE_RE = _re.compile(r"^\d{2}/\d{2}/\d{4}$")
    invalidas = [d for d in datas if not _DATE_RE.match(d)]
    if invalidas:
        raise HTTPException(
            status_code=422,
            detail=f"Formato inválido (esperado DD/MM/YYYY): {invalidas}"
        )
    import asyncio
    asyncio.create_task(run_relatorio_retroativo(datas))
    return {"status": "started", "datas": datas, "message": f"Retroativo para {len(datas)} data(s) disparado"}
