"""
jobs/readme_operacoes.py — Atualiza reports/README.md e faz commit/push automaticamente.

Roda a cada 30 minutos. Lê:
  - logs/server.log  → disparos, webhooks recebidos, propostas, eventos
  - reports/disparos_YYYY-MM-DD.json → registros detalhados do dia

Gera reports/README.md atualizado e faz git commit + push.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from config import TZ_BRASILIA, LISTAS_DAILY_MAX

logger = logging.getLogger(__name__)

_REPO_DIR = Path(__file__).parent.parent
_REPORTS_DIR = _REPO_DIR / "reports"
_LOG_PATH = _REPO_DIR / "logs" / "server.log"

# ---------------------------------------------------------------------------
# Helpers de extração de log
# ---------------------------------------------------------------------------

def _parse_disparos(today: str) -> list[dict]:
    """Lê reports/disparos_YYYY-MM-DD.json se existir, senão extrai do log."""
    report_path = _REPORTS_DIR / f"disparos_{today}.json"
    if report_path.exists():
        try:
            data = json.loads(report_path.read_text())
            return data.get("disparos", [])
        except Exception:
            pass

    # Fallback: extrai do log
    disparos = []
    if not _LOG_PATH.exists():
        return disparos
    with open(_LOG_PATH) as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if today not in line:
            continue
        m = re.search(
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[INFO\] jobs\.fila_listas: '
            r'✅ Fila Listas \[NOVO\]: card=([a-f0-9]+) phone=\.\.\.(\d+)'
            r'(?: token=\.\.\.([\w]+))?(?: msg_id=([\S]+))?',
            line,
        )
        if m:
            phone_full = ""
            for j in range(max(0, i - 5), i):
                sb = re.search(r'send_buttons → (\d+)', lines[j])
                if sb:
                    phone_full = sb.group(1)
                    break
            disparos.append({
                "n": len(disparos) + 1,
                "timestamp_utc": m.group(1),
                "card_id": m.group(2),
                "telefone": phone_full or f"...{m.group(3)}",
                "token_suffix": m.group(4) or "rotação",
                "channel_id": "—",
                "message_id": m.group(5) or "—",
                "status": True,
            })
    return disparos


def _parse_token_selection(today: str) -> dict[str, str]:
    """Extrai token selecionado por ciclo para enriquecer tabela."""
    mapping: dict[str, str] = {}
    if not _LOG_PATH.exists():
        return mapping
    with open(_LOG_PATH) as f:
        lines = f.readlines()
    for line in lines:
        if today not in line:
            continue
        m = re.search(
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*token selecionado.*slot=(\d+) channel_id=([\w-]+) token=\.\.\.([\w]+)',
            line,
        )
        if m:
            mapping[m.group(1)[:16]] = f"slot={m.group(2)} {m.group(3)} ...{m.group(4)}"
    return mapping


def _parse_eventos(today: str) -> list[dict]:
    """Extrai eventos operacionais relevantes do log."""
    eventos = []
    if not _LOG_PATH.exists():
        return eventos

    patterns = [
        (r"Sistema Consórcio Sorteado iniciado", "▶️ Sistema iniciado/reiniciado"),
        (r"Scheduler iniciado", "▶️ Scheduler iniciado"),
        (r"disparos retomados", "▶️ Disparos retomados"),
        (r"disparos pausados\|LISTAS_ATIVACAO_ENABLED=false", "⏸️ Disparos pausados"),
        (r"jobs/resume", "▶️ Jobs retomados via API"),
        (r"jobs/pause", "⏸️ Jobs pausados via API"),
        (r"BLOQUEIO DE TETO", "🚨 Bloqueio de teto de proposta"),
        (r"BLOQUEIO SEGURANÇA", "🚨 Bloqueio de segurança — cota de lance"),
        (r"run_precificacao: erro inesperado", "⚠️ Job precificacao falhou"),
        (r"escalador_bazar_lp falhou", "⚠️ Job escalador_bazar_lp falhou"),
        (r"Proposta \+ garantia enviadas", "💰 Proposta enviada"),
        (r"LISTAS_DAILY_MAX.*atingido\|limite diário atingido\|daily.*max.*atingido", "🛑 Limite diário de disparos atingido"),
        (r"TODOS os.*tokens offline", "🔴 Todos tokens offline"),
        (r"token.*selecionado.*slot=", "🔑 Token selecionado para disparo"),
    ]

    seen: set[str] = set()
    with open(_LOG_PATH) as f:
        lines = f.readlines()

    for line in lines:
        if today not in line:
            continue
        ts_m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if not ts_m:
            continue
        ts = ts_m.group(1)
        for pattern, label in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                key = f"{ts[:13]}:{label}"  # agrupa por hora+label (evita spam)
                if key not in seen:
                    seen.add(key)
                    eventos.append({"ts": ts, "evento": label})
                break

    return eventos


def _parse_webhooks_recebidos(today: str) -> list[dict]:
    """Conta webhooks recebidos de leads (não from_me)."""
    recebidos = []
    if not _LOG_PATH.exists():
        return recebidos
    with open(_LOG_PATH) as f:
        lines = f.readlines()
    for line in lines:
        if today not in line:
            continue
        # Mensagens reais de leads (não from_me, não status)
        m = re.search(
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*agente.*listas|'
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*agente.*bazar|'
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*handle_message.*phone|'
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*respondeu|'
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*interesse.*lead',
            line, re.IGNORECASE,
        )
        if m:
            ts = next(g for g in m.groups() if g)
            recebidos.append({"ts": ts, "detalhe": line.strip()[-120:]})
    return recebidos


def _parse_propostas(today: str) -> list[dict]:
    """Extrai propostas enviadas do log."""
    propostas = []
    if not _LOG_PATH.exists():
        return propostas
    with open(_LOG_PATH) as f:
        lines = f.readlines()
    for line in lines:
        if today not in line:
            continue
        m = re.search(
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Proposta \+ garantia enviadas.*?(\w{7,})',
            line,
        )
        if m:
            propostas.append({"ts": m.group(1), "phone": m.group(2)})
        # Fallback
        m2 = re.search(
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*proposta enviada.*card=([a-f0-9]+)',
            line, re.IGNORECASE,
        )
        if m2:
            propostas.append({"ts": m2.group(1), "card_id": m2.group(2)})
    return propostas


def _parse_pool_status(today: str) -> list[dict]:
    """Extrai último status conhecido de cada slot do log."""
    slot_status: dict[str, str] = {}
    if not _LOG_PATH.exists():
        return []
    with open(_LOG_PATH) as f:
        lines = f.readlines()
    for line in lines:
        if today not in line:
            continue
        m = re.search(r'Whapi monitor: (LISTA-\d+) \(([\w-]+)\) (ONLINE|OFFLINE).*?— ?(.*)', line)
        if m:
            slot, channel, estado, detalhe = m.groups()
            icon = "✅" if estado == "ONLINE" else "❌"
            slot_status[slot] = f"{icon} {estado} ({channel}) {detalhe.strip()}"
    return [{"slot": k, "status": v} for k, v in sorted(slot_status.items())]


# ---------------------------------------------------------------------------
# Geração do README
# ---------------------------------------------------------------------------

def _build_readme(today: str) -> str:
    now_brt = datetime.now(TZ_BRASILIA).strftime("%Y-%m-%d %H:%M BRT")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    disparos   = _parse_disparos(today)
    eventos    = _parse_eventos(today)
    recebidos  = _parse_webhooks_recebidos(today)
    propostas  = _parse_propostas(today)
    pool_st    = _parse_pool_status(today)

    meta        = LISTAS_DAILY_MAX or 0
    total_disp  = len(disparos)
    faltam      = max(0, meta - total_disp) if meta else "—"

    # ── Tabela de disparos ──────────────────────────────────────────────────
    disp_rows = ""
    for d in disparos:
        disp_rows += (
            f"| {d['n']} | {d['timestamp_utc']} | `{d.get('card_id','—')}` "
            f"| {d.get('telefone','—')} | `{d.get('token_suffix','—')}` "
            f"| {d.get('channel_id','—')} | `{d.get('message_id','—')}` | ✅ |\n"
        )
    if not disp_rows:
        disp_rows = "| — | — | — | — | — | — | — | Nenhum ainda |\n"

    # ── Tabela de propostas ─────────────────────────────────────────────────
    prop_rows = ""
    for p in propostas:
        prop_rows += f"| {p['ts']} | `{p.get('card_id','—')}` | {p.get('phone','—')} | ✅ |\n"
    if not prop_rows:
        prop_rows = "| — | — | — | Nenhuma ainda |\n"

    # ── Tabela de mensagens recebidas ───────────────────────────────────────
    recv_rows = ""
    for r in recebidos:
        recv_rows += f"| {r['ts']} | {r['detalhe'][:100]} |\n"
    if not recv_rows:
        recv_rows = "| — | Nenhuma resposta de lead registrada |\n"

    # ── Tabela de eventos ───────────────────────────────────────────────────
    ev_rows = ""
    for e in eventos[-40:]:  # últimos 40 eventos
        ev_rows += f"| {e['ts']} | {e['evento']} |\n"
    if not ev_rows:
        ev_rows = "| — | Nenhum evento |\n"

    # ── Status do pool ──────────────────────────────────────────────────────
    pool_rows = ""
    for p in pool_st:
        pool_rows += f"| {p['slot']} | {p['status']} |\n"
    if not pool_rows:
        pool_rows = "| — | Status não disponível |\n"

    readme = f"""# 📊 Registro de Operações — Consórcio Sorteado

> ⏱️ Atualizado automaticamente a cada 30 minutos.  
> Última atualização: **{now_brt}** / {now_utc}

---

## 📅 {today}

### 🚀 Disparos — Ativações (Fluxo Listas)

**Meta:** {meta} | **Realizados:** {total_disp} | **Faltam:** {faltam} | **Janela:** 06:15 → 22:30 BRT

| # | Horário (UTC) | Card ID | Telefone | Token | Channel ID | Message ID | Status |
|---|---------------|---------|----------|-------|------------|------------|--------|
{disp_rows}
> 📁 Dados completos: [`reports/disparos_{today}.json`](./disparos_{today}.json)

---

### 💰 Propostas Enviadas

| Horário (UTC) | Card ID | Telefone | Status |
|---------------|---------|----------|--------|
{prop_rows}

---

### 💬 Respostas de Leads Recebidas

| Horário (UTC) | Detalhe |
|---------------|---------|
{recv_rows}

---

### 🔌 Pool de Tokens — Último Status Conhecido

| Slot | Status |
|------|--------|
{pool_rows}

---

### 🔧 Eventos Operacionais

| Horário (UTC) | Evento |
|---------------|--------|
{ev_rows}

---

## 📁 Arquivos de Dados

| Arquivo | Descrição |
|---------|-----------|
| [`reports/disparos_{today}.json`](./disparos_{today}.json) | Registro JSON completo dos disparos com retorno da API |

---

## 📌 Legenda

| Ícone | Significado |
|-------|-------------|
| ✅ | Sucesso / Online |
| ❌ | Falha / Offline |
| ⏸️ | Pausado |
| ▶️ | Retomado / Ativo |
| ⚠️ | Alerta |
| 🚨 | Erro crítico / Bloqueio |
| 💰 | Proposta |
| 🔑 | Token selecionado |

---

*Histórico de dias anteriores será adicionado abaixo conforme acumulado.*
"""
    return readme


# ---------------------------------------------------------------------------
# Git commit + push
# ---------------------------------------------------------------------------

def _git_commit_push(today: str) -> None:
    try:
        subprocess.run(
            ["git", "add", f"reports/disparos_{today}.json", "reports/README.md"],
            cwd=_REPO_DIR, capture_output=True, timeout=15,
        )
        result = subprocess.run(
            ["git", "commit", "-m", f"ops: atualização automática do registro {today}"],
            cwd=_REPO_DIR, capture_output=True, text=True, timeout=15,
        )
        if "nothing to commit" in result.stdout:
            logger.debug("readme_operacoes: nada a commitar")
            return
        subprocess.run(
            ["git", "push"],
            cwd=_REPO_DIR, capture_output=True, timeout=30,
        )
        logger.info("readme_operacoes: commit + push OK")
    except Exception as e:
        logger.warning("readme_operacoes: git falhou — %s", e)


# ---------------------------------------------------------------------------
# Job principal
# ---------------------------------------------------------------------------

async def run_readme_operacoes() -> None:
    today = datetime.now(TZ_BRASILIA).strftime("%Y-%m-%d")
    logger.info("readme_operacoes: atualizando README para %s", today)
    try:
        _REPORTS_DIR.mkdir(exist_ok=True)
        readme = _build_readme(today)
        (_REPORTS_DIR / "README.md").write_text(readme, encoding="utf-8")
        logger.info("readme_operacoes: README atualizado (%d chars)", len(readme))
        # Git em thread separada para não bloquear o event loop
        await asyncio.get_event_loop().run_in_executor(None, _git_commit_push, today)
    except Exception as e:
        logger.exception("readme_operacoes: erro inesperado — %s", e)


async def run_readme_operacoes_safe() -> None:
    try:
        await run_readme_operacoes()
    except Exception as e:
        logger.exception("run_readme_operacoes: erro inesperado: %s", e)
