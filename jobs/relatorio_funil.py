"""
jobs/relatorio_funil.py — Relatório diário de funil

Layout: aba única "Resumos diários"
  - Cada execução adiciona um BLOCO com a data no topo
  - Dentro do bloco: 3 seções (Listas / Bazar / LP), cada uma com
    título colorido + tabela de 2 colunas (Métrica | Quantidade)
  - Espaço entre blocos para facilitar leitura
  - Sem fórmulas, sem colunas ocultas — leitura direta

Roda às 08h BRT (11h UTC). Endpoint manual: POST /jobs/relatorio-funil/run
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from config import Stage, TZ_BRASILIA, PIPELINE_ID
from services.faro import FaroClient, FaroError, get_fonte
from services.slack import slack_alert, slack_error

logger = logging.getLogger(__name__)

SPREADSHEET_ID    = "128hsf77Zgb6IZ9Y7dxwwEIez7r4JLmeQBgs4DB7u5HU"
ABA_RELATORIO     = "Resumos diários"
GOOGLE_CREDS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "secrets", "google_sheets.json"
)

# ─── Cores ────────────────────────────────────────────────────────────────────
def _rgb(r, g, b):
    return {"red": r/255, "green": g/255, "blue": b/255}

COR_DATA_BG     = _rgb(30,  30,  30)   # preto quase total → título da data
COR_DATA_FG     = _rgb(255, 255, 255)
COR_LISTAS_BG   = _rgb(46,  125, 50)   # verde escuro → Listas
COR_LISTAS_FG   = _rgb(255, 255, 255)
COR_BAZAR_BG    = _rgb(21,  101, 192)  # azul escuro → Bazar
COR_BAZAR_FG    = _rgb(255, 255, 255)
COR_LP_BG       = _rgb(130, 60,  180)  # roxo → LP
COR_LP_FG       = _rgb(255, 255, 255)
COR_LABEL_BG    = _rgb(245, 245, 245)  # cinza clarinho → rótulo
COR_VALOR_BG    = _rgb(255, 255, 255)  # branco → valor
COR_ZERO        = _rgb(220, 220, 220)  # cinza para zeros
COR_DESTAQUE_BG = _rgb(255, 243, 205)  # amarelo suave → linha de total
COR_DESTAQUE_FG = _rgb(60,  60,  60)


# ─── Coleta de cards ──────────────────────────────────────────────────────────

async def _fetch_all_cards() -> list[dict]:
    all_cards: list[dict] = []
    offset, limit = 0, 200
    async with FaroClient() as faro:
        while True:
            try:
                result = await faro._get("/api-cards-list", params={
                    "pipeline_id": PIPELINE_ID, "limit": limit, "offset": offset,
                })
                cards = result.get("items") or result.get("cards") or []
                if not cards:
                    break
                all_cards.extend(cards)
                if len(cards) < limit:
                    break
                offset += limit
            except FaroError as e:
                logger.error("relatorio_funil: erro offset=%d: %s", offset, e)
                break
    return all_cards


# ─── Filtros de data ──────────────────────────────────────────────────────────

def _is_today(val: Optional[str], hoje_iso: str, hoje_br: str) -> bool:
    if not val:
        return False
    v = str(val).strip()
    return v.startswith(hoje_iso) or v == hoje_br


# ─── Métricas do dia ─────────────────────────────────────────────────────────

def _metricas_dia(cards: list[dict], fluxo: str, hoje_iso: str, hoje_br: str) -> list[tuple[str, int]]:
    """Retorna lista de (label, valor) para o fluxo no dia."""

    def match(c):
        f = (get_fonte(c) or "").lower()
        if fluxo == "listas": return "lista" in f
        if fluxo == "bazar":  return "bazar" in f or "organico" in f or "orgânico" in f
        if fluxo == "lp":     return "lp" in f or "site" in f or "landing" in f
        return False

    sub     = [c for c in cards if match(c)]
    ativ    = [c for c in sub if _is_today(c.get("Data de primeira ativação"), hoje_iso, hoje_br)]
    movidos = [c for c in sub if (
        _is_today(c.get("updated_at"), hoje_iso, hoje_br) or
        _is_today(c.get("Ultima atividade"), hoje_iso, hoje_br)
    )]

    def cnt(stage): return sum(1 for c in movidos if c.get("stage_id") == stage)

    total_ids = {c["id"] for c in ativ} | {c["id"] for c in movidos}

    if fluxo == "listas":
        linhas = [
            ("Leads ativados (1ª vez)",    len(ativ)),
            ("Reativados (2ª ativação)",   cnt(Stage.SEGUNDA_ATIVACAO)),
            ("Reativados (3ª ativação)",   cnt(Stage.TERCEIRA_ATIVACAO)),
            ("Reativados (4ª ativação)",   cnt(Stage.QUARTA_ATIVACAO)),
            ("─────────────────", None),
            ("Demonstraram interesse",     sum(1 for c in movidos if c.get("stage_id") in {
                Stage.PRECIFICACAO, Stage.EM_NEGOCIACAO, Stage.ACEITO,
                Stage.ASSINATURA, Stage.FINALIZACAO_COMERCIAL,
            })),
            ("Sem interesse / recusa",     sum(1 for c in movidos
                if c.get("stage_id") == Stage.PERDIDO
                and "sem_interesse" in (c.get("Motivo de perda") or "").lower())),
            ("─────────────────", None),
            ("Foram para precificação",    cnt(Stage.PRECIFICACAO)),
            ("Estão em negociação",        cnt(Stage.EM_NEGOCIACAO)),
            ("Aceitaram a proposta",       cnt(Stage.ACEITO)),
            ("Em assinatura",              cnt(Stage.ASSINATURA)),
            ("─────────────────", None),
            ("Não qualificados",           cnt(Stage.NAO_QUALIFICADO)),
            ("Perdidos",                   cnt(Stage.PERDIDO)),
            ("Dispensados",               cnt(Stage.DISPENSADOS)),
            ("─────────────────", None),
            ("TOTAL DO DIA",               len(total_ids)),
        ]
    else:
        linhas = [
            ("Leads ativados (1ª vez)",    len(ativ)),
            ("Reativados (2ª ativação)",   cnt(Stage.SEGUNDA_ATIVACAO)),
            ("Reativados (3ª ativação)",   cnt(Stage.TERCEIRA_ATIVACAO)),
            ("Reativados (4ª ativação)",   cnt(Stage.QUARTA_ATIVACAO)),
            ("─────────────────", None),
            ("Aguardando envio de extrato", cnt(Stage.EM_CONTATO)),
            ("Extratos recebidos",         sum(1 for c in movidos if c.get("Link do Extrato"))),
            ("Extratos válidos (analisados)", sum(1 for c in movidos
                if c.get("Link do Extrato") and c.get("Crédito"))),
            ("Extratos inválidos / ilegíveis", sum(1 for c in movidos
                if "incorreto" in (c.get("Motivo dispensa") or "").lower()
                or "ilegível" in (c.get("Motivo dispensa") or "").lower())),
            ("Cotas não contempladas",     sum(1 for c in movidos
                if "nao-contemplada" in (c.get("Tipo contemplação") or "").lower())),
            ("Cotas de lance",             sum(1 for c in movidos
                if "lance" in (c.get("Tipo contemplação") or "").lower())),
            ("─────────────────", None),
            ("Foram para precificação",    cnt(Stage.PRECIFICACAO)),
            ("Estão em negociação",        cnt(Stage.EM_NEGOCIACAO)),
            ("Aceitaram a proposta",       cnt(Stage.ACEITO)),
            ("Em assinatura",              cnt(Stage.ASSINATURA)),
            ("─────────────────", None),
            ("Não qualificados",           cnt(Stage.NAO_QUALIFICADO)),
            ("Perdidos",                   cnt(Stage.PERDIDO)),
            ("Dispensados",               cnt(Stage.DISPENSADOS)),
            ("─────────────────", None),
            ("TOTAL DO DIA",               len(total_ids)),
        ]

    return linhas


# ─── Escrita no Sheets ────────────────────────────────────────────────────────

def _get_sheets_client():
    import gspread
    from google.oauth2.service_account import Credentials
    path = os.path.abspath(GOOGLE_CREDS_PATH)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Credenciais não encontradas: {path}")
    creds = Credentials.from_service_account_file(path, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)


def _cell(row, col, value, bg, fg=None, bold=False, size=10, align="LEFT", italic=False):
    """Monta dict de célula para batch_update."""
    fmt = {
        "backgroundColor": bg,
        "textFormat": {
            "foregroundColor": fg or {"red": 0.1, "green": 0.1, "blue": 0.1},
            "bold": bold,
            "italic": italic,
            "fontSize": size,
        },
        "horizontalAlignment": align,
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
    }
    return {
        "range": {
            "sheetId": None,  # preenchido depois
            "startRowIndex": row, "endRowIndex": row + 1,
            "startColumnIndex": col, "endColumnIndex": col + 1,
        },
        "cell": {"userEnteredFormat": fmt, "userEnteredValue": {"stringValue": str(value) if value is not None else ""}},
        "fields": "userEnteredFormat,userEnteredValue",
    }


def _write_bloco(spreadsheet, ws, data_br: str,
                 listas: list, bazar: list, lp: list) -> None:
    """Escreve um bloco completo do dia na aba, logo abaixo do conteúdo existente."""

    sheet_id = ws.id
    existing = ws.get_all_values()
    # Encontra última linha com conteúdo
    last_row = 0
    for i, row in enumerate(existing):
        if any(c.strip() for c in row):
            last_row = i + 1  # 1-indexed
    start = last_row + 2 if last_row > 0 else 1  # +2 para espaço
    r = start - 1  # 0-indexed para API

    data_cells = []
    format_requests = []

    def add_merge(row_i, col_s, col_e):
        """Merge de células."""
        format_requests.append({
            "mergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_i, "endRowIndex": row_i + 1,
                    "startColumnIndex": col_s, "endColumnIndex": col_e,
                },
                "mergeType": "MERGE_ALL",
            }
        })

    def add_row_format(row_i, col_s, col_e, bg, fg, bold=False, size=10, align="LEFT"):
        format_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_i, "endRowIndex": row_i + 1,
                    "startColumnIndex": col_s, "endColumnIndex": col_e,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": bg,
                        "textFormat": {"foregroundColor": fg, "bold": bold, "fontSize": size},
                        "horizontalAlignment": align,
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        })

    # ── Linha da DATA ────────────────────────────────────────────────────────
    # Col A-E: "📅 Relatório do dia DD/MM/YYYY"
    data_cells.append([f"📅  Relatório do dia  {data_br}", "", "", "", ""])
    add_merge(r, 0, 5)
    add_row_format(r, 0, 5, COR_DATA_BG, COR_DATA_FG, bold=True, size=13, align="CENTER")
    r += 1

    # Linha vazia
    data_cells.append(["", "", "", "", ""])
    r += 1

    # ── Bloco por fluxo ───────────────────────────────────────────────────────
    configs = [
        ("📋  FLUXO LISTAS",   COR_LISTAS_BG, COR_LISTAS_FG, listas),
        ("🏪  FLUXO BAZAR",    COR_BAZAR_BG,  COR_BAZAR_FG,  bazar),
        ("🌐  FLUXO LP / SITE", COR_LP_BG,    COR_LP_FG,     lp),
    ]

    for titulo, cor_bg, cor_fg, linhas in configs:
        # Título do fluxo
        data_cells.append([titulo, "", "", "", ""])
        add_merge(r, 0, 5)
        add_row_format(r, 0, 5, cor_bg, cor_fg, bold=True, size=11, align="LEFT")
        r += 1

        # Cabeçalho da tabela
        data_cells.append(["Etapa", "", "", "Quantidade", ""])
        add_merge(r, 0, 3)
        add_merge(r, 3, 5)
        add_row_format(r, 0, 3, _rgb(55, 55, 55), COR_DATA_FG, bold=True, size=10, align="LEFT")
        add_row_format(r, 3, 5, _rgb(55, 55, 55), COR_DATA_FG, bold=True, size=10, align="CENTER")
        r += 1

        for label, valor in linhas:
            if valor is None:
                # Linha separadora
                data_cells.append(["", "", "", "", ""])
                add_row_format(r, 0, 5, _rgb(230, 230, 230), _rgb(100, 100, 100))
                r += 1
                continue

            is_total = label.startswith("TOTAL")
            row_bg_l = COR_DESTAQUE_BG if is_total else COR_LABEL_BG
            row_bg_v = COR_DESTAQUE_BG if is_total else COR_VALOR_BG
            row_fg   = _rgb(40, 40, 40)
            val_str  = str(valor)

            data_cells.append([f"  {label}", "", "", val_str, ""])
            add_merge(r, 0, 3)
            add_merge(r, 3, 5)
            add_row_format(r, 0, 3, row_bg_l, row_fg, bold=is_total, size=10)
            add_row_format(r, 3, 5, row_bg_v, row_fg, bold=is_total, size=10, align="CENTER")
            r += 1

        # Espaço entre fluxos
        data_cells.append(["", "", "", "", ""])
        r += 1

    # ── Escreve dados ─────────────────────────────────────────────────────────
    start_cell = f"A{start}"
    ws.update(start_cell, data_cells, value_input_option="RAW")

    # ── Aplica formatação ─────────────────────────────────────────────────────
    if format_requests:
        spreadsheet.batch_update({"requests": format_requests})

    # ── Largura das colunas (apenas na primeira vez) ──────────────────────────
    if start <= 3:
        spreadsheet.batch_update({"requests": [
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                               "startIndex": 0, "endIndex": 3},
                    "properties": {"pixelSize": 280},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                               "startIndex": 3, "endIndex": 5},
                    "properties": {"pixelSize": 120},
                    "fields": "pixelSize",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 0},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
        ]})

    logger.info("relatorio_funil: bloco de %s escrito a partir da linha %d", data_br, start)


# ─── Slack ────────────────────────────────────────────────────────────────────

def _fmt_slack(data_br: str, listas: list, bazar: list, lp: list) -> str:
    def bloco(emoji, nome, linhas):
        partes = [f"{emoji} *{nome}*"]
        for label, valor in linhas:
            if valor is None or label.startswith("─"):
                continue
            if valor == 0:
                continue
            partes.append(f"  › {label}: *{valor}*")
        return "\n".join(partes)

    return "\n\n".join([
        f"📅 *Relatório do dia — {data_br}*",
        bloco("📋", "Listas", listas),
        bloco("🏪", "Bazar",  bazar),
        bloco("🌐", "LP / Site", lp),
    ])


# ─── Job principal ────────────────────────────────────────────────────────────

async def run_relatorio_funil() -> None:
    logger.info("relatorio_funil: iniciando...")
    agora_br = datetime.now(TZ_BRASILIA)
    data_br  = agora_br.strftime("%d/%m/%Y")
    data_iso = agora_br.strftime("%Y-%m-%d")

    try:
        cards = await _fetch_all_cards()
        logger.info("relatorio_funil: %d cards coletados", len(cards))
    except Exception as e:
        logger.error("relatorio_funil: falha ao buscar cards: %s", e)
        return

    listas = _metricas_dia(cards, "listas", data_iso, data_br)
    bazar  = _metricas_dia(cards, "bazar",  data_iso, data_br)
    lp     = _metricas_dia(cards, "lp",     data_iso, data_br)

    # Slack
    try:
        await slack_alert(_fmt_slack(data_br, listas, bazar, lp), level="info")
    except Exception as e:
        logger.error("relatorio_funil: slack falhou: %s", e)

    # Sheets
    try:
        gc = _get_sheets_client()
        sp = gc.open_by_key(SPREADSHEET_ID)
        import gspread
        try:
            ws = sp.worksheet(ABA_RELATORIO)
        except gspread.exceptions.WorksheetNotFound:
            ws = sp.add_worksheet(title=ABA_RELATORIO, rows=2000, cols=10)

        _write_bloco(sp, ws, data_br, listas, bazar, lp)
        logger.info("relatorio_funil: Sheets atualizado.")
    except FileNotFoundError as e:
        logger.warning("relatorio_funil: %s", e)
    except Exception as e:
        logger.error("relatorio_funil: erro Sheets: %s", e)
        await slack_error("Falha ao gravar relatório no Google Sheets",
                          exception=e, context={"data": data_br})

    logger.info("relatorio_funil: concluído para %s", data_br)
