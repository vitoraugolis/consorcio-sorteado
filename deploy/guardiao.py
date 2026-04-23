"""
deploy/guardiao.py — Guardião Inteligente do Consórcio Sorteado

Monitoramento contínuo com interface conversacional via Slack Socket Mode.
Conecta-se ao Slack via WebSocket (sem necessidade de HTTPS ou domínio).

Capacidades:
  - Health check a cada 2 minutos com auto-restart em caso de falha
  - Relatório automático a cada 6 horas
  - Interface conversacional: responde qualquer pergunta em PT-BR usando Claude
  - Comandos rápidos: status, logs, restart, report, leads, ajuda

Variáveis de ambiente necessárias (no .env):
  SLACK_BOT_TOKEN        xoxb-...   (Bot User OAuth Token)
  SLACK_APP_TOKEN        xapp-...   (App-Level Token com scope connections:write)
  SLACK_GUARDIAN_CHANNEL             ID ou nome do canal de alertas (ex: C0XXXXXXXXX)
  ANTHROPIC_API_KEY                  Já configurado no projeto
  APP_URL                            http://127.0.0.1:8000 (padrão)
  SERVICE_NAME                       consorcio-sorteado (padrão)
  GUARDIAN_CHECK_INTERVAL            120 segundos (padrão)
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

IS_WINDOWS = sys.platform == "win32"
LOG_FILE   = os.environ.get("LOG_FILE", "")  # caminho do log em texto (necessário no Windows)

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN     = os.environ["SLACK_APP_TOKEN"]
SLACK_CHANNEL       = os.environ.get("SLACK_GUARDIAN_CHANNEL", "#alertas-sistemas")
APP_URL             = os.environ.get("APP_URL", "http://127.0.0.1:8000")
SERVICE_NAME        = os.environ.get("SERVICE_NAME", "consorcio-sorteado")
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
CHECK_INTERVAL      = int(os.environ.get("GUARDIAN_CHECK_INTERVAL", "120"))
MAX_RESTARTS        = int(os.environ.get("GUARDIAN_MAX_RESTARTS", "3"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [guardiao] %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

app    = AsyncApp(token=SLACK_BOT_TOKEN)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# Estado interno
# ---------------------------------------------------------------------------

_estado = {
    "ultima_checagem":      None,
    "status":               "iniciando",
    "falhas_consecutivas":  0,
    "restarts_na_sessao":   0,
    "ultimo_restart":       None,
    "health_data":          None,
    "inicio":               datetime.now(timezone.utc).isoformat(),
}

# ---------------------------------------------------------------------------
# Ferramentas do sistema
# ---------------------------------------------------------------------------

async def checar_saude() -> dict:
    """Chama GET /health e retorna resultado."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{APP_URL}/health")
            if r.status_code == 200:
                return {"ok": True, "data": r.json()}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def status_servico() -> dict:
    """Verifica se o serviço está ativo (Linux: systemd; Windows: sc)."""
    try:
        if IS_WINDOWS:
            r = subprocess.run(
                ["sc", "query", SERVICE_NAME],
                capture_output=True, text=True, timeout=5,
            )
            ativo  = "RUNNING" in r.stdout
            estado = "running" if ativo else "stopped"
        else:
            r = subprocess.run(
                ["systemctl", "is-active", SERVICE_NAME],
                capture_output=True, text=True, timeout=5,
            )
            estado = r.stdout.strip()
            ativo  = estado == "active"
        return {"ativo": ativo, "estado": estado}
    except Exception as e:
        return {"ativo": False, "estado": str(e)}


# Alias retrocompatível usado no resto do código
status_systemd = status_servico


def ler_logs(linhas: int = 80) -> str:
    """Lê as últimas N linhas do log (Linux: journalctl; Windows: arquivo LOG_FILE)."""
    try:
        if IS_WINDOWS:
            if not LOG_FILE:
                return "(LOG_FILE não configurado — defina no .env)"
            if not os.path.exists(LOG_FILE):
                return f"(arquivo de log não encontrado: {LOG_FILE})"
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                todas = f.readlines()
            return "".join(todas[-linhas:]).strip() or "(log vazio)"
        else:
            r = subprocess.run(
                ["journalctl", "-u", SERVICE_NAME, f"-n{linhas}",
                 "--no-pager", "--output=cat"],
                capture_output=True, text=True, timeout=15,
            )
            return r.stdout.strip() or "(nenhum log encontrado)"
    except Exception as e:
        return f"Erro ao ler logs: {e}"


def reiniciar_servico() -> dict:
    """Reinicia o serviço (Linux: sudo systemctl; Windows: sc stop + sc start)."""
    try:
        if IS_WINDOWS:
            subprocess.run(["sc", "stop",  SERVICE_NAME], capture_output=True, timeout=15)
            import time; time.sleep(3)
            r = subprocess.run(["sc", "start", SERVICE_NAME], capture_output=True, text=True, timeout=15)
            ok = r.returncode == 0
        else:
            r  = subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME],
                                 capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0

        ts = datetime.now(timezone.utc).isoformat()
        _estado["ultimo_restart"]     = ts
        _estado["restarts_na_sessao"] += 1
        if ok:
            return {"ok": True, "msg": "Serviço reiniciado com sucesso."}
        return {"ok": False, "msg": (r.stderr or b"").strip() if IS_WINDOWS else r.stderr.strip()}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def parar_servico() -> dict:
    try:
        cmd = ["sc", "stop", SERVICE_NAME] if IS_WINDOWS else ["sudo", "systemctl", "stop", SERVICE_NAME]
        r   = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return {"ok": r.returncode == 0, "msg": r.stderr.strip()}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def uso_disco_e_memoria() -> str:
    """Retorna resumo de disco e memória."""
    linhas = []
    try:
        if IS_WINDOWS:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-PSDrive C | Select-Object @{n='Disco';e={'C:'}},@{n='Usado(GB)';e={[math]::Round($_.Used/1GB,1)}},@{n='Livre(GB)';e={[math]::Round($_.Free/1GB,1)}} | Format-List; "
                 "Get-CimInstance Win32_OperatingSystem | Select-Object @{n='RAM Total(MB)';e={[math]::Round($_.TotalVisibleMemorySize/1KB)}},@{n='RAM Livre(MB)';e={[math]::Round($_.FreePhysicalMemory/1KB)}} | Format-List"],
                capture_output=True, text=True, timeout=10,
            )
            linhas.append(r.stdout.strip())
        else:
            r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
            linhas.append("Disco:\n" + r.stdout.strip())
            r = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5)
            linhas.append("Memória:\n" + r.stdout.strip())
    except Exception as e:
        linhas.append(f"(erro ao coletar recursos: {e})")
    return "\n\n".join(linhas) or "(não disponível)"


# ---------------------------------------------------------------------------
# Contexto para o Claude
# ---------------------------------------------------------------------------

async def coletar_contexto(linhas_log: int = 60) -> str:
    """Monta string de contexto completo do sistema para análise do Claude."""
    saude   = await checar_saude()
    systemd = status_systemd()
    logs    = ler_logs(linhas_log)
    recursos = uso_disco_e_memoria()

    agora = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    partes = [
        f"=== GUARDIÃO — SNAPSHOT EM {agora} ===",
        f"Plataforma: {'Windows' if IS_WINDOWS else 'Linux'}",
        "",
        "--- ESTADO INTERNO DO GUARDIÃO ---",
        f"Iniciado em:           {_estado['inicio']}",
        f"Falhas consecutivas:   {_estado['falhas_consecutivas']}",
        f"Restarts na sessão:    {_estado['restarts_na_sessao']}",
        f"Último restart:        {_estado['ultimo_restart'] or 'nenhum'}",
        "",
        "--- SERVIÇO ---",
        f"Serviço '{SERVICE_NAME}': {'✅ ativo' if systemd['ativo'] else '❌ ' + systemd['estado']}",
        "",
        "--- HEALTH CHECK ---",
    ]

    if saude["ok"] and saude.get("data"):
        d = saude["data"]
        partes.append(f"Status: {d.get('status', '?')}")
        jobs = d.get("jobs", [])
        if jobs:
            partes.append(f"Jobs agendados ({len(jobs)}):")
            for j in jobs:
                partes.append(
                    f"  [{j.get('id','?')}] próximo: {j.get('next_run','?')} | "
                    f"último: {j.get('last_run','?')}"
                )
        else:
            partes.append("Nenhum job listado no health.")
    else:
        partes.append(f"❌ Health falhou: {saude.get('error', 'sem resposta')}")

    partes += [
        "",
        "--- RECURSOS DO VPS ---",
        recursos,
        "",
        f"--- LOGS RECENTES (últimas {linhas_log} linhas) ---",
        logs,
    ]

    return "\n".join(partes)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

SISTEMA_CLAUDE = """Você é o Guardião, agente de monitoramento do sistema de automação comercial
da Consórcio Sorteado — empresa que COMPRA cotas contempladas de consórcio.

O sistema gerencia o ciclo completo de vendas via WhatsApp:
- Listas (leads frios) → Whapi (2 tokens anti-ban)
- Bazar/Site (orgânicos) → Z-API
- Pipeline: NOVO → ATIVAÇÕES → QUALIFICAÇÃO → PRECIFICAÇÃO → NEGOCIAÇÃO → ACEITO → ASSINATURA → SUCESSO
- 7 jobs APScheduler: ativacao_listas, ativacao_bazar, ativacao_site, reativador, follow_up, precificacao, contrato
- Integrações: FARO CRM, Anthropic Claude (IA), ZapSign (contratos), Slack (alertas)

Você tem acesso ao estado atual do sistema (journalctl logs, health endpoint, systemd status).

COMO RESPONDER:
- Português direto e claro
- Se houver erros nos logs, identifique-os e sugira ação concreta
- Se jobs estiverem travados, explique o motivo provável
- Se tudo estiver ok, confirme brevemente
- Use emojis com moderação para legibilidade no Slack
- Nunca invente informações — se não souber, diga

PARA RELATÓRIOS, inclua sempre:
1. Estado geral (✅ OK / ⚠️ Atenção / 🔴 Crítico)
2. Jobs: quais estão ou não rodando
3. Erros/warnings identificados nos logs
4. Recomendação de ação (se houver)
"""


async def perguntar_claude(pergunta: str, contexto: str) -> str:
    """Envia pergunta + contexto ao Claude e retorna resposta."""
    try:
        prompt = f"CONTEXTO ATUAL DO SISTEMA:\n{contexto}\n\nMENSAGEM DO USUÁRIO:\n{pergunta}"
        resp = claude.messages.create(
            model="claude-opus-4-7",
            max_tokens=1200,
            system=SISTEMA_CLAUDE,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception as e:
        logger.error("Erro ao chamar Claude: %s", e)
        return f"❌ Erro ao chamar Claude: {e}"


# ---------------------------------------------------------------------------
# Formatadores rápidos
# ---------------------------------------------------------------------------

def _fmt_status_rapido(saude: dict, systemd: dict) -> str:
    ok    = saude["ok"] and systemd["ativo"]
    emoji = "✅" if ok else "❌"
    ts    = datetime.now(timezone.utc).strftime("%H:%M UTC")

    linhas = [f"{emoji} *Consórcio Sorteado — Status Rápido* ({ts})"]
    linhas.append(f"• Serviço: {'ativo' if systemd['ativo'] else '⚠️ ' + systemd['estado']}")

    if saude["ok"] and saude.get("data"):
        d    = saude["data"]
        jobs = d.get("jobs", [])
        linhas.append(f"• API: OK")
        linhas.append(f"• Jobs agendados: {len(jobs)}")
    else:
        linhas.append(f"• API: ❌ {saude.get('error', 'sem resposta')}")

    linhas.append(f"• Falhas consecutivas: {_estado['falhas_consecutivas']}")
    linhas.append(f"• Restarts na sessão:  {_estado['restarts_na_sessao']}")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Slack: roteamento de mensagens
# ---------------------------------------------------------------------------

async def _processar_mensagem(texto: str, say) -> None:
    texto_l = texto.lower().strip()

    # ── Comandos diretos ──────────────────────────────────────────────────
    if texto_l in ("status", "saúde", "saude", "health", "ok"):
        saude   = await checar_saude()
        systemd = status_systemd()
        await say(_fmt_status_rapido(saude, systemd))
        return

    if re.match(r"^logs?(\s+\d+)?$", texto_l):
        partes = texto_l.split()
        n = int(partes[1]) if len(partes) > 1 else 80
        n = min(n, 300)
        logs = ler_logs(n)
        if len(logs) > 3800:
            logs = "…(truncado, mostrando fim)\n" + logs[-3500:]
        await say(f"```\n{logs}\n```")
        return

    if texto_l in ("restart", "reiniciar", "reinicia", "reboot"):
        await say("🔄 Reiniciando o serviço…")
        res = reiniciar_servico()
        if res["ok"]:
            await asyncio.sleep(5)
            saude = await checar_saude()
            if saude["ok"]:
                await say("✅ Serviço reiniciado e respondendo normalmente.")
            else:
                await say(f"⚠️ Reiniciado mas health ainda falha: {saude.get('error')}")
        else:
            await say(f"❌ Falha no restart: {res['msg']}")
        return

    if texto_l in ("parar", "stop"):
        await say("⛔ Parando o serviço…")
        res = parar_servico()
        await say("✅ Serviço parado." if res["ok"] else f"❌ Erro: {res['msg']}")
        return

    if texto_l in ("recursos", "disco", "memoria", "memória", "infra"):
        await say(f"```\n{uso_disco_e_memoria()}\n```")
        return

    if texto_l in ("report", "relatório", "relatorio"):
        await say("📊 Gerando relatório completo, aguarde…")
        contexto  = await coletar_contexto(linhas_log=100)
        resposta  = await perguntar_claude(
            "Faça um relatório completo do sistema: estado geral, jobs, "
            "erros encontrados nos logs e recomendações.",
            contexto,
        )
        await say(resposta)
        return

    if texto_l in ("ajuda", "help", "comandos", "?"):
        await say(
            "*🤖 Comandos do Guardião:*\n\n"
            "• `status` — resumo rápido\n"
            "• `logs [N]` — últimas N linhas de log (padrão: 80)\n"
            "• `restart` — reinicia o serviço\n"
            "• `parar` — para o serviço\n"
            "• `recursos` — uso de disco e memória\n"
            "• `report` — relatório completo gerado pelo Claude\n\n"
            "Ou *qualquer pergunta em linguagem natural* — analiso o sistema e respondo. 💬"
        )
        return

    # ── Linguagem natural → Claude com contexto completo ─────────────────
    await say("🔍 Analisando o sistema…")
    contexto = await coletar_contexto()
    resposta = await perguntar_claude(texto, contexto)
    await say(resposta)


@app.event("app_mention")
async def handle_mention(event, say):
    texto = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
    await _processar_mensagem(texto or "status", say)


@app.event("message")
async def handle_dm(event, say):
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    texto = event.get("text", "").strip()
    if texto:
        await _processar_mensagem(texto, say)


# ---------------------------------------------------------------------------
# Loop de monitoramento proativo
# ---------------------------------------------------------------------------

async def _alertar(mensagem: str) -> None:
    """Envia alerta proativo ao canal configurado."""
    try:
        await app.client.chat_postMessage(channel=SLACK_CHANNEL, text=mensagem)
    except Exception as e:
        logger.error("Falha ao enviar alerta Slack: %s", e)


async def loop_monitoramento() -> None:
    logger.info("Monitor iniciado — intervalo: %ds", CHECK_INTERVAL)
    await _alertar(
        f"🟢 *Guardião iniciado* — monitorando `{SERVICE_NAME}` "
        f"a cada {CHECK_INTERVAL}s."
    )

    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        saude   = await checar_saude()
        systemd = status_systemd()
        _estado["ultima_checagem"] = datetime.now(timezone.utc).isoformat()

        if saude["ok"] and systemd["ativo"]:
            _estado["status"]              = "ok"
            _estado["falhas_consecutivas"] = 0
            _estado["health_data"]         = saude.get("data")
            logger.info("Health check OK")
            continue

        # ── Falha detectada ───────────────────────────────────────────────
        _estado["falhas_consecutivas"] += 1
        falhas = _estado["falhas_consecutivas"]
        erro   = saude.get("error") or f"systemd: {systemd['estado']}"
        logger.warning("Falha #%d: %s", falhas, erro)

        if falhas == 1:
            await _alertar(
                f"⚠️ *Instabilidade detectada* (falha #1)\n"
                f"Erro: `{erro}`\n"
                f"Aguardando próximo ciclo antes de agir…"
            )

        elif falhas == 2:
            if _estado["restarts_na_sessao"] >= MAX_RESTARTS:
                await _alertar(
                    f"🆘 *Sistema inoperante — limite de restarts atingido!*\n"
                    f"Restarts feitos: {_estado['restarts_na_sessao']}\n"
                    f"Erro: `{erro}`\n"
                    f"*Intervenção manual necessária.*"
                )
                continue

            await _alertar(
                f"🔴 *Sistema inoperante — reiniciando automaticamente*\n"
                f"Falhas consecutivas: {falhas} | Erro: `{erro}`"
            )
            res = reiniciar_servico()
            await asyncio.sleep(15)

            nova = await checar_saude()
            if nova["ok"]:
                _estado["falhas_consecutivas"] = 0
                _estado["status"]              = "ok"
                await _alertar("✅ *Sistema recuperado* após reinício automático.")
            else:
                await _alertar(
                    f"🆘 *Reinício não resolveu!*\n"
                    f"Erro pós-restart: `{nova.get('error')}`\n"
                    f"Verificar manualmente."
                )


# ---------------------------------------------------------------------------
# Relatório periódico (a cada 6h)
# ---------------------------------------------------------------------------

async def loop_relatorio() -> None:
    await asyncio.sleep(600)  # aguarda 10min antes do primeiro relatório
    while True:
        try:
            contexto = await coletar_contexto(linhas_log=100)
            relatorio = await perguntar_claude(
                "Gere um relatório de status periódico do sistema. "
                "Inclua: estado geral, jobs funcionando, alertas nos logs, recomendações.",
                contexto,
            )
            await _alertar(
                f"📊 *Relatório Automático — "
                f"{datetime.now(timezone.utc).strftime('%d/%m %H:%M UTC')}*\n\n"
                f"{relatorio}"
            )
        except Exception as e:
            logger.error("Erro no relatório periódico: %s", e)
        await asyncio.sleep(6 * 3600)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)

    asyncio.create_task(loop_monitoramento())
    asyncio.create_task(loop_relatorio())

    logger.info("Guardião conectando ao Slack via Socket Mode…")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
