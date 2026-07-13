"""
jobs/relatorio_disparos.py — Relatório diário de disparos via webhook Make.com

Executa diariamente às 07h BRT (10h UTC).
Coleta os contadores Redis do dia anterior, monta o HTML e envia ao webhook Make.com.

Payload enviado (POST JSON):
{
  "to": "...",           # definido no Make
  "from": "...",         # definido no Make
  "subject": "📊 Relatório Diário CS — DD/MM/YYYY",
  "html": "...email HTML...",
  "pipelines": [
    {"name": "Listas (nova ativação)", "cards_count": N, "total_installment_value": 0},
    {"name": "1ª Ativação",            "cards_count": N, "total_installment_value": 0},
    {"name": "2ª Ativação",            "cards_count": N, "total_installment_value": 0},
    {"name": "3ª Ativação",            "cards_count": N, "total_installment_value": 0},
    {"name": "4ª Ativação",            "cards_count": N, "total_installment_value": 0},
    {"name": "Propostas",              "cards_count": N, "total_installment_value": 0},
  ],
  "total_cards": N,
  "total_installment_value": 0
}
"""

import logging
import os
from datetime import datetime

import httpx

from config import TZ_BRASILIA
from services.stats import get_yesterday_stats

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv(
    "RELATORIO_DISPAROS_WEBHOOK_URL",
    "https://hook.us1.make.com/4vbqrss7lht8t8bev5p1nbhk8lri2uka",
)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _build_html(stats: dict, data_fmt: str) -> str:
    """Monta o HTML do e-mail de relatório."""

    total = stats.get("total_disparos", 0)
    propostas = stats.get("propostas", 0)

    status_cor = "#22c55e" if total > 0 else "#f59e0b"
    status_txt = "✅ Sistema operando normalmente" if total > 0 else "⚠️ Nenhum disparo registrado"

    def row(label: str, valor: int, cor: str = "#1e293b") -> str:
        return f"""
        <tr>
          <td style="padding:10px 16px;border-bottom:1px solid #e2e8f0;color:{cor};font-weight:500">{label}</td>
          <td style="padding:10px 16px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:700;color:{cor}">{valor}</td>
        </tr>"""

    ativacoes_total = (
        stats.get("ativacao_1", 0) +
        stats.get("ativacao_2", 0) +
        stats.get("ativacao_3", 0) +
        stats.get("ativacao_4", 0)
    )

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Relatório Diário CS — {data_fmt}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 0">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

        <!-- Cabeçalho -->
        <tr>
          <td style="background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);padding:32px 40px">
            <p style="margin:0;color:#94a3b8;font-size:13px;text-transform:uppercase;letter-spacing:1px">Consórcio Sorteado</p>
            <h1 style="margin:8px 0 4px;color:#ffffff;font-size:22px;font-weight:700">📊 Relatório Diário de Disparos</h1>
            <p style="margin:0;color:#64748b;font-size:14px">{data_fmt}</p>
          </td>
        </tr>

        <!-- Status geral -->
        <tr>
          <td style="padding:24px 40px 0">
            <div style="background:#f8fafc;border-left:4px solid {status_cor};border-radius:6px;padding:14px 18px">
              <span style="color:{status_cor};font-weight:600;font-size:15px">{status_txt}</span>
            </div>
          </td>
        </tr>

        <!-- Resumo -->
        <tr>
          <td style="padding:24px 40px 0">
            <table width="100%" cellpadding="0" cellspacing="0" style="border-radius:8px;overflow:hidden;border:1px solid #e2e8f0">
              <tr style="background:#0f172a">
                <th style="padding:12px 16px;text-align:left;color:#94a3b8;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Canal</th>
                <th style="padding:12px 16px;text-align:right;color:#94a3b8;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Disparos</th>
              </tr>
              {row("Listas (nova ativação)", stats.get("listas", 0))}
              {row("1ª Ativação (follow-up)", stats.get("ativacao_1", 0))}
              {row("2ª Ativação (follow-up)", stats.get("ativacao_2", 0))}
              {row("3ª Ativação (follow-up)", stats.get("ativacao_3", 0))}
              {row("4ª Ativação (follow-up)", stats.get("ativacao_4", 0))}
              <tr style="background:#f8fafc">
                <td style="padding:12px 16px;font-weight:700;color:#0f172a;font-size:15px">Total de Disparos</td>
                <td style="padding:12px 16px;text-align:right;font-weight:800;color:#0f172a;font-size:17px">{total}</td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Propostas -->
        <tr>
          <td style="padding:20px 40px 0">
            <table width="100%" cellpadding="0" cellspacing="0" style="border-radius:8px;overflow:hidden;border:1px solid #e2e8f0">
              <tr style="background:#064e3b">
                <th colspan="2" style="padding:12px 16px;text-align:left;color:#6ee7b7;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Propostas</th>
              </tr>
              <tr>
                <td style="padding:12px 16px;color:#1e293b;font-weight:500">Propostas enviadas</td>
                <td style="padding:12px 16px;text-align:right;font-weight:800;color:#059669;font-size:17px">{propostas}</td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Rodapé -->
        <tr>
          <td style="padding:28px 40px 32px">
            <p style="margin:0;color:#94a3b8;font-size:12px;text-align:center">
              Gerado automaticamente pelo sistema Consórcio Sorteado — Guará Marketing<br>
              {data_fmt} · Contadores acumulados desde 00h00 BRT
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------

async def run_relatorio_disparos() -> dict:
    """
    Coleta stats do dia anterior, monta o payload e envia ao webhook Make.com.
    Retorna dict com status da execução.
    """
    agora = datetime.now(TZ_BRASILIA)
    stats = await get_yesterday_stats()

    data_iso = stats["data"]                        # YYYY-MM-DD
    data_fmt = datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    subject  = f"📊 Relatório Diário CS — {data_fmt}"

    html = _build_html(stats, data_fmt)

    pipelines = [
        {"name": "Listas (nova ativação)", "cards_count": stats.get("listas", 0),     "total_installment_value": 0},
        {"name": "1ª Ativação",            "cards_count": stats.get("ativacao_1", 0), "total_installment_value": 0},
        {"name": "2ª Ativação",            "cards_count": stats.get("ativacao_2", 0), "total_installment_value": 0},
        {"name": "3ª Ativação",            "cards_count": stats.get("ativacao_3", 0), "total_installment_value": 0},
        {"name": "4ª Ativação",            "cards_count": stats.get("ativacao_4", 0), "total_installment_value": 0},
        {"name": "Propostas",              "cards_count": stats.get("propostas", 0),  "total_installment_value": 0},
    ]

    total_cards = stats.get("total_disparos", 0) + stats.get("propostas", 0)

    payload = {
        "subject": subject,
        "html":    html,
        "pipelines": pipelines,
        "total_cards": total_cards,
        "total_installment_value": 0,
        "data": data_iso,
        "stats_raw": {k: stats.get(k, 0) for k in [
            "listas", "ativacao_1", "ativacao_2", "ativacao_3",
            "ativacao_4", "propostas", "total_disparos"
        ]},
    }

    if not WEBHOOK_URL:
        logger.warning("relatorio_disparos: RELATORIO_DISPAROS_WEBHOOK_URL não configurada — abortando")
        return {"status": "skip", "reason": "webhook_url_not_set"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(WEBHOOK_URL, json=payload)
            resp.raise_for_status()
        logger.info(
            "relatorio_disparos: enviado — data=%s total_disparos=%d propostas=%d status=%d",
            data_iso, stats.get("total_disparos", 0), stats.get("propostas", 0), resp.status_code
        )
        return {"status": "ok", "data": data_iso, "http_status": resp.status_code}
    except httpx.HTTPStatusError as e:
        logger.error("relatorio_disparos: webhook retornou HTTP %s: %s", e.response.status_code, e)
        return {"status": "error", "reason": f"http_{e.response.status_code}"}
    except Exception as e:
        logger.error("relatorio_disparos: falha ao enviar webhook: %s", e)
        return {"status": "error", "reason": str(e)}
