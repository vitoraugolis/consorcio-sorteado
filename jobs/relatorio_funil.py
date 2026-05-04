"""
jobs/relatorio_funil.py — Relatório diário de funil por fluxo (Listas / Bazar / LP)

Roda uma vez por dia (padrão: 08h BRT) e:
  1. Coleta métricas do FARO agrupadas por fluxo e stage
  2. Compila o funil completo para cada fluxo
  3. Posta resumo no Slack (#alertas-sistemas ou canal configurado)
  4. Grava na planilha Google Sheets (uma aba por fluxo + aba consolidada)

Métricas por fluxo
──────────────────
LISTAS
  - Leads Ativados (1ª ativação)
  - Leads em 2ª ativação / 3ª ativação / 4ª ativação
  - Leads Interessados (responderam positivamente)
  - Leads sem interesse / PERDIDO
  - Leads em Precificação
  - Leads em Negociação
  - Leads Aceitos
  - Leads Não Qualificados
  - Leads Dispensados

BAZAR / LP
  - Leads Ativados (1ª ativação)
  - Leads em 2ª / 3ª / 4ª ativação
  - Leads em Contato (aguardando extrato)
  - Extratos Recebidos (enviaram ao menos 1 extrato)
  - Extratos Válidos (qualificados ou não qualificados por mérito)
  - Extratos Inválidos (ilegível / modelo errado)
  - Leads Não Contemplados (extrato de cota ativa)
  - Leads de Lance (LP_LANCE)
  - Leads Não Qualificados (por % pago)
  - Leads Precificáveis (em Precificação)
  - Leads em Negociação
  - Leads Aceitos
  - Leads Dispensados

CONSOLIDADO (todos os fluxos)
  - Totais agregados
  - Taxa de qualificação por fluxo
  - Taxa de conversão até Precificação
"""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from config import Stage, TZ_BRASILIA, PIPELINE_ID
from services.faro import FaroClient, FaroError, get_fonte
from services.slack import slack_alert

logger = logging.getLogger(__name__)

SPREADSHEET_ID = "128hsf77Zgb6IZ9Y7dxwwEIez7r4JLmeQBgs4DB7u5HU"
GOOGLE_CREDS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "secrets", "google_sheets.json"
)

# ─── Mapeamento de stages para nomes legíveis ─────────────────────────────────

STAGE_NAMES: dict[str, str] = {
    Stage.LISTAS:              "Fila Listas",
    Stage.BAZAR:               "Fila Bazar",
    Stage.LP:                  "Fila LP",
    Stage.PRIMEIRA_ATIVACAO:   "1ª Ativação",
    Stage.SEGUNDA_ATIVACAO:    "2ª Ativação",
    Stage.TERCEIRA_ATIVACAO:   "3ª Ativação",
    Stage.QUARTA_ATIVACAO:     "4ª Ativação",
    Stage.EM_CONTATO:          "Em Contato",
    Stage.LP_LANCE:            "LP Lance",
    Stage.ESPERA:              "Em Espera",
    Stage.PRECIFICACAO:        "Precificação",
    Stage.EM_NEGOCIACAO:       "Em Negociação",
    Stage.FINALIZACAO_COMERCIAL: "Finalização Comercial",
    Stage.ACEITO:              "Aceito",
    Stage.ASSINATURA:          "Assinatura",
    Stage.NAO_QUALIFICADO:     "Não Qualificado",
    Stage.PERDIDO:             "Perdido",
    Stage.DISPENSADOS:         "Dispensados",
    Stage.FLUXO_CADENCIA:      "Fluxo Cadência",
    Stage.ON_HOLD:             "On Hold",
}

# ─── Definição dos funis por fluxo ────────────────────────────────────────────

def _is_listas(card: dict) -> bool:
    fonte = (get_fonte(card) or "").lower()
    return "lista" in fonte

def _is_bazar(card: dict) -> bool:
    fonte = (get_fonte(card) or "").lower()
    return "bazar" in fonte or "orgânico" in fonte or "organico" in fonte

def _is_lp(card: dict) -> bool:
    fonte = (get_fonte(card) or "").lower()
    return "lp" in fonte or "site" in fonte or "landing" in fonte


# ─── Coleta de cards do FARO ──────────────────────────────────────────────────

async def _fetch_all_cards() -> list[dict]:
    """Busca todos os cards do pipeline (todas as stages)."""
    all_cards: list[dict] = []
    offset = 0
    limit  = 100
    async with FaroClient() as faro:
        while True:
            try:
                params = {"pipeline_id": PIPELINE_ID, "limit": limit, "offset": offset}
                result = await faro._get("/api-cards-list", params=params)
                cards  = result.get("items") or result.get("cards") or []
                if not cards:
                    break
                all_cards.extend(cards)
                if len(cards) < limit:
                    break
                offset += limit
            except FaroError as e:
                logger.error("relatorio_funil: erro ao buscar cards (offset=%d): %s", offset, e)
                break
    return all_cards


# ─── Contagem de métricas ─────────────────────────────────────────────────────

def _count(cards: list[dict], stage_id: str) -> int:
    return sum(1 for c in cards if c.get("stage_id") == stage_id)


def _extratos_recebidos(cards: list[dict]) -> int:
    """Cards que têm Link do Extrato preenchido."""
    return sum(1 for c in cards if c.get("Link do Extrato"))


def _extratos_invalidos(cards: list[dict]) -> int:
    """
    Cards que tentaram enviar extrato mas erraram.
    Proxy: journey.extrato_incorreto_count > 0 ou stage = EM_CONTATO com extrato preenchido.
    Usa campo "Motivo dispensa" como segundo sinal.
    """
    count = 0
    for c in cards:
        motivo = (c.get("Motivo dispensa") or "").lower()
        if "ilegível" in motivo or "ilegivel" in motivo or "não é extrato" in motivo or "incorreto" in motivo:
            count += 1
    return count


def _extratos_validos(cards: list[dict]) -> int:
    """Cards que tiveram extrato analisado com sucesso (qualificado ou não)."""
    count = 0
    for c in cards:
        if c.get("Link do Extrato") and c.get("Crédito"):
            count += 1
    return count


def _leads_lance(cards: list[dict]) -> int:
    """Cards com tipo contemplação = lance (em qualquer stage)."""
    count = 0
    for c in cards:
        tipo = (c.get("Tipo contemplação") or "").lower()
        if "lance" in tipo:
            count += 1
    return count


def _leads_sem_interesse(cards: list[dict]) -> int:
    """Cards em PERDIDO com motivo de sem interesse."""
    count = 0
    for c in cards:
        if c.get("stage_id") == Stage.PERDIDO:
            motivo = (c.get("Motivo de perda") or "").lower()
            if "sem_interesse" in motivo or "sem interesse" in motivo or "recusa" in motivo:
                count += 1
    return count


def _leads_interessados(cards: list[dict]) -> int:
    """
    Leads de Listas que demonstraram interesse (responderam positivamente).
    Proxy: saíram de ativação para negociação/precificação.
    """
    interessados_stages = {
        Stage.PRECIFICACAO, Stage.EM_NEGOCIACAO,
        Stage.FINALIZACAO_COMERCIAL, Stage.ACEITO, Stage.ASSINATURA,
    }
    return sum(1 for c in cards if c.get("stage_id") in interessados_stages)


def _build_funil_listas(cards: list[dict]) -> dict[str, int]:
    listas = [c for c in cards if _is_listas(c)]
    return {
        "Leads na Fila":         _count(listas, Stage.LISTAS),
        "1ª Ativação":           _count(listas, Stage.PRIMEIRA_ATIVACAO),
        "2ª Ativação":           _count(listas, Stage.SEGUNDA_ATIVACAO),
        "3ª Ativação":           _count(listas, Stage.TERCEIRA_ATIVACAO),
        "4ª Ativação":           _count(listas, Stage.QUARTA_ATIVACAO),
        "Interessados":          _leads_interessados(listas),
        "Sem Interesse":         _leads_sem_interesse(listas),
        "Em Precificação":       _count(listas, Stage.PRECIFICACAO),
        "Em Negociação":         _count(listas, Stage.EM_NEGOCIACAO),
        "Aceitos":               _count(listas, Stage.ACEITO),
        "Assinatura":            _count(listas, Stage.ASSINATURA),
        "Não Qualificados":      _count(listas, Stage.NAO_QUALIFICADO),
        "Dispensados":           _count(listas, Stage.DISPENSADOS),
        "Perdidos":              _count(listas, Stage.PERDIDO),
        "On Hold":               _count(listas, Stage.ON_HOLD),
        "Fluxo Cadência":        _count(listas, Stage.FLUXO_CADENCIA),
        "TOTAL":                 len(listas),
    }


def _build_funil_bazar_lp(cards: list[dict], fluxo: str) -> dict[str, int]:
    if fluxo == "bazar":
        subset = [c for c in cards if _is_bazar(c)]
    else:
        subset = [c for c in cards if _is_lp(c)]

    return {
        "Leads na Fila":         _count(subset, Stage.BAZAR if fluxo == "bazar" else Stage.LP),
        "1ª Ativação":           _count(subset, Stage.PRIMEIRA_ATIVACAO),
        "2ª Ativação":           _count(subset, Stage.SEGUNDA_ATIVACAO),
        "3ª Ativação":           _count(subset, Stage.TERCEIRA_ATIVACAO),
        "4ª Ativação":           _count(subset, Stage.QUARTA_ATIVACAO),
        "Em Contato (aguard. extrato)": _count(subset, Stage.EM_CONTATO),
        "Em Espera":             _count(subset, Stage.ESPERA),
        "Extratos Recebidos":    _extratos_recebidos(subset),
        "Extratos Válidos":      _extratos_validos(subset),
        "Extratos Inválidos":    _extratos_invalidos(subset),
        "Não Contemplados":      sum(1 for c in subset if "nao-contemplada" in (c.get("Tipo contemplação") or "").lower()),
        "Leads de Lance":        _leads_lance(subset),
        "LP Lance (sondagem)":   _count(subset, Stage.LP_LANCE) if fluxo == "lp" else 0,
        "Não Qualificados":      _count(subset, Stage.NAO_QUALIFICADO),
        "Em Precificação":       _count(subset, Stage.PRECIFICACAO),
        "Em Negociação":         _count(subset, Stage.EM_NEGOCIACAO),
        "Aceitos":               _count(subset, Stage.ACEITO),
        "Assinatura":            _count(subset, Stage.ASSINATURA),
        "Finaliz. Comercial":    _count(subset, Stage.FINALIZACAO_COMERCIAL),
        "Dispensados":           _count(subset, Stage.DISPENSADOS),
        "Perdidos":              _count(subset, Stage.PERDIDO),
        "On Hold":               _count(subset, Stage.ON_HOLD),
        "TOTAL":                 len(subset),
    }


def _build_funil_consolidado(
    listas: dict, bazar: dict, lp: dict
) -> dict[str, int]:
    all_keys = set(listas) | set(bazar) | set(lp)
    consolidated: dict[str, int] = {}
    for k in all_keys:
        consolidated[k] = listas.get(k, 0) + bazar.get(k, 0) + lp.get(k, 0)
    return consolidated


# ─── Formatação Slack ─────────────────────────────────────────────────────────

def _fmt_funil_slack(nome_fluxo: str, funil: dict[str, int], emoji: str) -> str:
    hoje = datetime.now(TZ_BRASILIA).strftime("%d/%m/%Y")
    lines = [f"{emoji} *{nome_fluxo} — {hoje}*\n"]
    for k, v in funil.items():
        if k == "TOTAL":
            lines.append(f"━━━━━━━━━━━━━━━━")
            lines.append(f"*Total: {v}*")
        else:
            bar = "▪" if v == 0 else "▸"
            lines.append(f"{bar} {k}: *{v}*")
    return "\n".join(lines)


# ─── Google Sheets ────────────────────────────────────────────────────────────

def _get_sheets_client():
    """Retorna cliente gspread autenticado via service account."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_path = os.path.abspath(GOOGLE_CREDS_PATH)
    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"Google credentials não encontrado: {creds_path}")

    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    return gspread.authorize(creds)


def _get_or_create_sheet(spreadsheet, title: str):
    """Retorna aba existente ou cria nova."""
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        return spreadsheet.add_worksheet(title=title, rows=500, cols=30)


def _ensure_header(ws, header: list[str]) -> None:
    """Garante que a primeira linha tem o cabeçalho correto."""
    try:
        first_row = ws.row_values(1)
        if first_row != header:
            ws.update("A1", [header])
    except Exception:
        ws.update("A1", [header])


def _write_funil_to_sheet(
    spreadsheet,
    aba: str,
    funil: dict[str, int],
    data_str: str,
) -> None:
    """Escreve/atualiza a linha do dia na aba correspondente."""
    ws = _get_or_create_sheet(spreadsheet, aba)

    # Cabeçalho: Data + todas as métricas
    metricas = [k for k in funil.keys()]
    header = ["Data"] + metricas
    _ensure_header(ws, header)

    # Busca se já existe linha para essa data
    col_dates = ws.col_values(1)  # coluna A
    row_idx = None
    for i, cell in enumerate(col_dates[1:], start=2):
        if cell == data_str:
            row_idx = i
            break

    valores = [data_str] + [funil[k] for k in metricas]

    if row_idx:
        # Atualiza linha existente
        ws.update(f"A{row_idx}", [valores])
    else:
        # Adiciona nova linha
        ws.append_row(valores)

    logger.info("relatorio_funil: aba '%s' atualizada para %s", aba, data_str)


# ─── Job principal ────────────────────────────────────────────────────────────

async def run_relatorio_funil() -> None:
    """Gera e distribui o relatório diário de funil."""
    logger.info("relatorio_funil: iniciando coleta de métricas...")
    hoje_br = datetime.now(TZ_BRASILIA)
    data_str = hoje_br.strftime("%d/%m/%Y")

    try:
        all_cards = await _fetch_all_cards()
        logger.info("relatorio_funil: %d cards coletados do FARO", len(all_cards))
    except Exception as e:
        logger.error("relatorio_funil: falha ao buscar cards: %s", e)
        return

    # Compila funis
    funil_listas = _build_funil_listas(all_cards)
    funil_bazar  = _build_funil_bazar_lp(all_cards, "bazar")
    funil_lp     = _build_funil_bazar_lp(all_cards, "lp")
    funil_total  = _build_funil_consolidado(funil_listas, funil_bazar, funil_lp)

    # ── Slack ──────────────────────────────────────────────────────────────────
    msgs = [
        _fmt_funil_slack("Fluxo Listas",    funil_listas, "📋"),
        _fmt_funil_slack("Fluxo Bazar",     funil_bazar,  "🏪"),
        _fmt_funil_slack("Fluxo LP / Site", funil_lp,     "🌐"),
        _fmt_funil_slack("Consolidado",     funil_total,  "📊"),
    ]
    for msg in msgs:
        try:
            await slack_alert(msg, level="info")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error("relatorio_funil: falha ao postar Slack: %s", e)

    # ── Google Sheets ──────────────────────────────────────────────────────────
    try:
        gc          = _get_sheets_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        _write_funil_to_sheet(spreadsheet, "Listas",       funil_listas, data_str)
        _write_funil_to_sheet(spreadsheet, "Bazar",        funil_bazar,  data_str)
        _write_funil_to_sheet(spreadsheet, "LP",           funil_lp,     data_str)
        _write_funil_to_sheet(spreadsheet, "Consolidado",  funil_total,  data_str)

        logger.info("relatorio_funil: Google Sheets atualizado com sucesso.")
    except FileNotFoundError as e:
        logger.warning("relatorio_funil: %s — Sheets ignorado.", e)
    except Exception as e:
        logger.error("relatorio_funil: erro ao gravar no Sheets: %s", e)
        from services.slack import slack_error
        await slack_error(
            "Falha ao gravar relatório no Google Sheets",
            exception=e,
            context={"data": data_str},
        )

    logger.info("relatorio_funil: concluído para %s", data_str)
