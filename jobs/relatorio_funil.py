"""
jobs/relatorio_funil.py — Relatório diário de funil (funil exaustivo, layout colunar)

Layout: aba única "Resumos diários"
  - Linhas = etapas do funil (fixas, com agrupamento visual)
  - Colunas = um dia por coluna (nova coluna adicionada à direita cada dia)
  - Cada fluxo tem seu bloco de linhas separado por título colorido
  - Roda às 00h BRT (03h UTC) referente ao dia ANTERIOR

Endpoint manual: POST /jobs/relatorio-funil/run
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from config import Stage, TZ_BRASILIA, PIPELINE_ID
from services.faro import FaroClient, FaroError, get_fonte
from services.slack import slack_alert, slack_error

logger = logging.getLogger(__name__)

SPREADSHEET_ID    = "128hsf77Zgb6IZ9Y7dxwwEIez7r4JLmeQBgs4DB7u5HU"
ABA               = "Resumos diários"
GOOGLE_CREDS_PATH = os.path.join(os.path.dirname(__file__), "..", "secrets", "google_sheets.json")

# ─── Cores ────────────────────────────────────────────────────────────────────
def _rgb(r, g, b): return {"red": r/255, "green": g/255, "blue": b/255}

C_BRANCO    = _rgb(255, 255, 255)
C_PRETO_FG  = _rgb(40,  40,  40)
C_CINZA_LN  = _rgb(240, 240, 240)   # linha zebra clara
C_CINZA_SEP = _rgb(200, 200, 200)   # separador entre grupos
C_ZERO      = _rgb(250, 250, 250)

# Título de fluxo
C_LISTAS_BG  = _rgb(27,  94,  32)   # verde escuro
C_BAZAR_BG   = _rgb(13,  71, 161)   # azul escuro
C_LP_BG      = _rgb(74,  20, 140)   # roxo escuro
C_FG_WHITE   = _rgb(255, 255, 255)

# Subtítulos de grupo
C_GRP_LISTAS = _rgb(200, 230, 201)
C_GRP_BAZAR  = _rgb(187, 222, 251)
C_GRP_LP     = _rgb(225, 190, 231)
C_GRP_FG     = _rgb(30,  30,  30)

# Linha total
C_TOTAL_BG   = _rgb(255, 243, 205)
C_TOTAL_FG   = _rgb(80,  50,   0)

# Cabeçalho de data
C_DATA_BG    = _rgb(55,  71,  79)   # azul-cinza escuro
C_DATA_FG    = _rgb(255, 255, 255)


# ─── Definição do funil (linhas fixas da planilha) ───────────────────────────
# Cada entry: (chave, label, tipo)
# tipo: "titulo_fluxo" | "grupo" | "metrica" | "total" | "sep" | "espaco"
# chave None = linha visual sem dado

def _funil_listas():
    return [
        ("_tit",   "📋  FLUXO LISTAS",                     "titulo_fluxo", "listas"),
        # ── Disparos
        ("_g1",    "Disparos",                              "grupo",  "listas"),
        ("l_atv1", "1ª ativação enviada",                   "metrica","listas"),
        ("l_atv2", "2ª ativação enviada",                   "metrica","listas"),
        ("l_atv3", "3ª ativação enviada",                   "metrica","listas"),
        ("l_atv4", "4ª ativação enviada",                   "metrica","listas"),
        ("l_prob", "Problema de contato (número inválido)", "metrica","listas"),
        # ── Respostas
        ("_g2",    "Respostas recebidas",                   "grupo",  "listas"),
        ("l_int",  "Demonstraram interesse",                "metrica","listas"),
        ("l_nint", "Sem interesse / recusa",                "metrica","listas"),
        ("l_sret", "Sem retorno (silêncio total)",          "metrica","listas"),
        # ── Qualificação
        ("_g3",    "Qualificação",                          "grupo",  "listas"),
        ("l_prec", "Foram para precificação",               "metrica","listas"),
        ("l_neg",  "Em negociação",                         "metrica","listas"),
        ("l_ngc",  "Negociação congelada",                  "metrica","listas"),
        # ── Fechamento
        ("_g4",    "Fechamento",                            "grupo",  "listas"),
        ("l_acei", "Aceitaram a proposta",                  "metrica","listas"),
        ("l_assi", "Em assinatura",                         "metrica","listas"),
        ("l_suc",  "Sucesso (contrato fechado)",            "metrica","listas"),
        # ── Descarte
        ("_g5",    "Descarte",                              "grupo",  "listas"),
        ("l_nq",   "Não qualificados",                      "metrica","listas"),
        ("l_perd", "Perdidos",                              "metrica","listas"),
        ("l_disp", "Dispensados",                           "metrica","listas"),
        ("l_lixo", "Lixo (duplicata / inválido)",           "metrica","listas"),
        # ── Total
        ("l_tot",  "TOTAL DO DIA — Listas",                 "total",  "listas"),
        ("_esp1",  "",                                      "espaco", "listas"),
    ]

def _funil_bazar():
    return [
        ("_tit",   "🏪  FLUXO BAZAR",                       "titulo_fluxo", "bazar"),
        # ── Disparos
        ("_g1",    "Disparos",                              "grupo",  "bazar"),
        ("b_atv1", "1ª ativação enviada",                   "metrica","bazar"),
        ("b_atv2", "2ª ativação enviada",                   "metrica","bazar"),
        ("b_atv3", "3ª ativação enviada",                   "metrica","bazar"),
        ("b_atv4", "4ª ativação enviada",                   "metrica","bazar"),
        ("b_prob", "Problema de contato (número inválido)", "metrica","bazar"),
        # ── Engajamento
        ("_g2",    "Engajamento",                           "grupo",  "bazar"),
        ("b_cont", "Em conversa (aguardando extrato)",      "metrica","bazar"),
        ("b_extr", "Extratos recebidos",                    "metrica","bazar"),
        ("b_eval", "Extrato válido (analisado com sucesso)","metrica","bazar"),
        ("b_einv", "Extrato inválido / ilegível",           "metrica","bazar"),
        ("b_ncnt", "Cota não contemplada (extrato errado)", "metrica","bazar"),
        ("b_lanc", "Cotas de lance (LP Lance)",             "metrica","bazar"),
        ("b_hold", "On hold (aguardando doc complementar)", "metrica","bazar"),
        # ── Qualificação
        ("_g3",    "Qualificação",                          "grupo",  "bazar"),
        ("b_prec", "Foram para precificação",               "metrica","bazar"),
        ("b_neg",  "Em negociação",                         "metrica","bazar"),
        ("b_ngc",  "Negociação congelada",                  "metrica","bazar"),
        # ── Fechamento
        ("_g4",    "Fechamento",                            "grupo",  "bazar"),
        ("b_acei", "Aceitaram a proposta",                  "metrica","bazar"),
        ("b_assi", "Em assinatura",                         "metrica","bazar"),
        ("b_suc",  "Sucesso (contrato fechado)",            "metrica","bazar"),
        # ── Descarte
        ("_g5",    "Descarte",                              "grupo",  "bazar"),
        ("b_nq",   "Não qualificados",                      "metrica","bazar"),
        ("b_perd", "Perdidos",                              "metrica","bazar"),
        ("b_disp", "Dispensados",                           "metrica","bazar"),
        ("b_lixo", "Lixo (duplicata / inválido)",           "metrica","bazar"),
        # ── Subtotal por motivo de perda
        ("_g6",    "Motivo de perda (detalhamento)",        "grupo",  "bazar"),
        ("b_psm",  "  → Sem margem / usou lance",           "metrica","bazar"),
        ("b_psi",  "  → Sem interesse",                     "metrica","bazar"),
        ("b_psr",  "  → Sem retorno",                       "metrica","bazar"),
        ("b_pnp",  "  → ADM não parceira",                  "metrica","bazar"),
        ("b_pcv",  "  → Cota já vendida",                   "metrica","bazar"),
        ("b_pout", "  → Outros motivos",                    "metrica","bazar"),
        # ── Total
        ("b_tot",  "TOTAL DO DIA — Bazar",                  "total",  "bazar"),
        ("_esp2",  "",                                      "espaco", "bazar"),
    ]

def _funil_lp():
    return [
        ("_tit",   "🌐  FLUXO LP / SITE",                   "titulo_fluxo", "lp"),
        # ── Disparos
        ("_g1",    "Ativações",                             "grupo",  "lp"),
        ("p_atv1", "1ª ativação enviada",                   "metrica","lp"),
        ("p_atv2", "2ª ativação enviada",                   "metrica","lp"),
        ("p_atv3", "3ª ativação enviada",                   "metrica","lp"),
        ("p_atv4", "4ª ativação enviada",                   "metrica","lp"),
        ("p_prob", "Problema de contato (número inválido)", "metrica","lp"),
        # ── Engajamento
        ("_g2",    "Engajamento",                           "grupo",  "lp"),
        ("p_cont", "Em conversa (aguardando extrato)",      "metrica","lp"),
        ("p_extr", "Extratos recebidos",                    "metrica","lp"),
        ("p_eval", "Extrato válido (analisado)",            "metrica","lp"),
        ("p_einv", "Extrato inválido / ilegível",           "metrica","lp"),
        ("p_ncnt", "Cota não contemplada",                  "metrica","lp"),
        ("p_lanc", "Leads de lance (LP Lance)",             "metrica","lp"),
        # ── Qualificação / Fechamento
        ("_g3",    "Qualificação e fechamento",             "grupo",  "lp"),
        ("p_prec", "Foram para precificação",               "metrica","lp"),
        ("p_neg",  "Em negociação",                         "metrica","lp"),
        ("p_acei", "Aceitaram a proposta",                  "metrica","lp"),
        ("p_assi", "Em assinatura",                         "metrica","lp"),
        ("p_suc",  "Sucesso (contrato fechado)",            "metrica","lp"),
        # ── Descarte
        ("_g4",    "Descarte",                              "grupo",  "lp"),
        ("p_nq",   "Não qualificados",                      "metrica","lp"),
        ("p_perd", "Perdidos",                              "metrica","lp"),
        ("p_disp", "Dispensados",                           "metrica","lp"),
        # ── Total
        ("p_tot",  "TOTAL DO DIA — LP",                     "total",  "lp"),
        ("_esp3",  "",                                      "espaco", "lp"),
    ]


# ─── Coleta ───────────────────────────────────────────────────────────────────

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
                if not cards: break
                all_cards.extend(cards)
                if len(cards) < limit: break
                offset += limit
            except FaroError as e:
                logger.error("relatorio_funil: offset=%d: %s", offset, e)
                break
    return all_cards


# ─── Filtros de data ──────────────────────────────────────────────────────────

def _is_date(val: Optional[str], iso: str, br: str) -> bool:
    if not val: return False
    v = str(val).strip()
    return v.startswith(iso) or v == br

def _ativado(card, iso, br):
    return _is_date(card.get("Data de primeira ativação"), iso, br)

def _movido(card, iso, br):
    return (
        _is_date(card.get("updated_at"), iso, br) or
        _is_date(card.get("Ultima atividade"), iso, br)
    )

def _ativado2(card, iso, br):
    return _is_date(card.get("Data de segunda ativação"), iso, br)

def _ativado3(card, iso, br):
    return _is_date(card.get("Data de terceira ativação"), iso, br)

def _ativado4(card, iso, br):
    return _is_date(card.get("Data de quarta ativação"), iso, br)


# ─── Cálculo de métricas ──────────────────────────────────────────────────────

def _calcular(cards: list[dict], fluxo: str, iso: str, br: str) -> dict[str, int]:
    def match(c):
        f = (get_fonte(c) or c.get("Etiquetas") or "").lower()
        if fluxo == "listas": return "lista" in f
        if fluxo == "bazar":  return "bazar" in f or "organico" in f or "orgânico" in f
        if fluxo == "lp":     return f in ("lp", "landing page") or "site" in f
        return False

    sub  = [c for c in cards if match(c)]
    mov  = [c for c in sub if _movido(c, iso, br)]

    def cnt(stage_id):
        return sum(1 for c in mov if c.get("stage_id") == stage_id)

    def cnt_motivo(substr):
        return sum(1 for c in mov
                   if c.get("stage_id") == Stage.PERDIDO
                   and substr.lower() in (c.get("Motivo de perda") or "").lower())

    # Disparos — usa campos específicos de data de ativação
    atv1 = sum(1 for c in sub if _ativado(c, iso, br))
    atv2 = sum(1 for c in sub if _ativado2(c, iso, br))
    atv3 = sum(1 for c in sub if _ativado3(c, iso, br))
    atv4 = sum(1 for c in sub if _ativado4(c, iso, br))

    total_ids = {c["id"] for c in sub if _ativado(c, iso, br) or _movido(c, iso, br)}

    prefix = {"listas": "l_", "bazar": "b_", "lp": "p_"}[fluxo]
    p = prefix

    m = {
        f"{p}atv1": atv1,
        f"{p}atv2": atv2,
        f"{p}atv3": atv3,
        f"{p}atv4": atv4,
        f"{p}prob": cnt(Stage.PROBLEMA_CONTATO),
        f"{p}prec": cnt(Stage.PRECIFICACAO),
        f"{p}neg":  cnt(Stage.EM_NEGOCIACAO),
        f"{p}ngc":  cnt(Stage.NEG_CONGELADA),
        f"{p}acei": cnt(Stage.ACEITO),
        f"{p}assi": cnt(Stage.ASSINATURA),
        f"{p}suc":  cnt(Stage.SUCESSO),
        f"{p}nq":   cnt(Stage.NAO_QUALIFICADO),
        f"{p}perd": cnt(Stage.PERDIDO),
        f"{p}disp": cnt(Stage.DISPENSADOS),
        f"{p}lixo": cnt(Stage.LIXO),
        f"{p}tot":  len(total_ids),
    }

    if fluxo == "listas":
        interessados = sum(1 for c in mov if c.get("stage_id") in {
            Stage.PRECIFICACAO, Stage.EM_NEGOCIACAO, Stage.ACEITO,
            Stage.ASSINATURA, Stage.FINALIZACAO_COMERCIAL, Stage.SUCESSO,
            Stage.NEG_CONGELADA,
        })
        sem_int = cnt_motivo("sem interesse")
        sem_ret = cnt_motivo("sem retorno")
        m.update({
            "l_int":  interessados,
            "l_nint": sem_int,
            "l_sret": sem_ret,
        })

    else:  # bazar / lp
        m.update({
            f"{p}cont": cnt(Stage.EM_CONTATO),
            f"{p}extr": sum(1 for c in mov if c.get("Link do Extrato")),
            f"{p}eval": sum(1 for c in mov if c.get("Link do Extrato") and c.get("Crédito")),
            f"{p}einv": sum(1 for c in mov if any(
                t in (c.get("Motivo dispensa") or "").lower()
                for t in ("incorreto", "ilegível", "invalido")
            )),
            f"{p}ncnt": sum(1 for c in mov if "nao-cont" in (c.get("Tipo contemplação") or "").lower()
                            or "não contemplado" in (c.get("Tipo contemplação") or "").lower()),
            f"{p}lanc": sum(1 for c in mov if "lance" in (c.get("Tipo contemplação") or "").lower()),
            f"{p}hold": cnt(Stage.ON_HOLD),
        })

    if fluxo == "bazar":
        perdidos_tot = cnt(Stage.PERDIDO)
        psm = cnt_motivo("sem margem")
        psi = cnt_motivo("sem interesse")
        psr = cnt_motivo("sem retorno")
        pnp = sum(1 for c in mov
                  if c.get("stage_id") == Stage.PERDIDO
                  and "adm" in (c.get("Motivo de perda") or "").lower()
                  and "parceira" in (c.get("Motivo de perda") or "").lower())
        pcv = cnt_motivo("cota vendida")
        pout = max(0, perdidos_tot - psm - psi - psr - pnp - pcv)
        m.update({
            "b_psm": psm, "b_psi": psi, "b_psr": psr,
            "b_pnp": pnp, "b_pcv": pcv, "b_pout": pout,
        })

    return m


# ─── Slack ─────────────────────────────────────────────────────────────────────

def _fmt_slack(data_br: str, ml: dict, mb: dict, mp: dict) -> str:
    all_m = {**ml, **mb, **mp}

    def bloco(emoji, nome, funil_fn):
        linhas = [f"{emoji} *{nome}*"]
        for chave, label, tipo, _ in funil_fn():
            if tipo in ("titulo_fluxo", "grupo", "espaco"):
                continue
            v = all_m.get(chave, 0) or 0
            if v == 0:
                continue
            if tipo == "total":
                linhas.append(f"━ *{label}: {v}*")
            else:
                linhas.append(f"  › {label}: *{v}*")
        return "\n".join(linhas)

    return "\n\n".join([
        f"📅 *Relatório — {data_br}*",
        bloco("📋", "Listas",    _funil_listas),
        bloco("🏪", "Bazar",     _funil_bazar),
        bloco("🌐", "LP / Site", _funil_lp),
    ])


# ─── Job principal ─────────────────────────────────────────────────────────────

async def run_relatorio_funil() -> None:
    """
    Roda às 00h BRT (03h UTC) e reporta o dia ANTERIOR.
    Também pode ser chamado manualmente via POST /jobs/relatorio-funil/run,
    nesse caso reporta o dia CORRENTE (útil para testes e verificação intraday).
    """
    agora_br = datetime.now(TZ_BRASILIA)

    # Se rodou às 00h, reporta ontem; caso contrário, reporta hoje
    if agora_br.hour < 3:
        alvo = agora_br - timedelta(days=1)
    else:
        alvo = agora_br

    data_br  = alvo.strftime("%d/%m/%Y")
    data_iso = alvo.strftime("%Y-%m-%d")

    logger.info("relatorio_funil: coletando dados de %s", data_br)

    try:
        cards = await _fetch_all_cards()
        logger.info("relatorio_funil: %d cards coletados", len(cards))
    except Exception as e:
        logger.error("relatorio_funil: falha ao buscar cards: %s", e)
        return

    ml = _calcular(cards, "listas", data_iso, data_br)
    mb = _calcular(cards, "bazar",  data_iso, data_br)
    mp = _calcular(cards, "lp",     data_iso, data_br)

    # Slack
    try:
        await slack_alert(_fmt_slack(data_br, ml, mb, mp), level="info")
    except Exception as e:
        logger.error("relatorio_funil: slack: %s", e)

    # Sheets
    try:
        gc = _get_sheets_client()
        sp = gc.open_by_key(SPREADSHEET_ID)
        ws = _get_or_create_ws(sp, ABA)

        # 1. Garante que o skeleton de linhas existe
        _build_skeleton(ws, sp)

        # 2. Encontra ou cria a coluna para essa data
        col_idx = _find_or_add_col(ws, sp, data_br)

        # 3. Escreve os valores
        _write_col(ws, sp, col_idx, data_br, ml, mb, mp)

        logger.info("relatorio_funil: Sheets atualizado — coluna %s", _col_letter(col_idx + 1))
    except FileNotFoundError as e:
        logger.warning("relatorio_funil: %s", e)
    except Exception as e:
        logger.error("relatorio_funil: erro Sheets: %s", e)
        await slack_error("Falha ao gravar relatório no Sheets",
                          exception=e, context={"data": data_br})

    logger.info("relatorio_funil: concluído para %s", data_br)


# ─── Google Sheets helpers ────────────────────────────────────────────────────

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


def _col_letter(n: int) -> str:
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _get_or_create_ws(sp, nome: str):
    import gspread
    try:
        return sp.worksheet(nome)
    except gspread.exceptions.WorksheetNotFound:
        return sp.add_worksheet(title=nome, rows=200, cols=60)


def _build_skeleton(ws, sp) -> None:
    """
    Escreve estrutura fixa de linhas na planilha. Layout:
      - Linha 1: cabeçalho — B1="Etapa", C1+ = datas (uma por dia)
      - Linhas 2+: col A=chave (quasi-oculto), col B=label legível
    Só executa se a planilha estiver vazia.
    """
    existing = ws.get_all_values()
    if any(cell.strip() for row in existing for cell in row):
        return

    todos = _funil_listas() + _funil_bazar() + _funil_lp()

    # Linha 1: header fixo para col A e B
    ws.update("A1", [["#"]], value_input_option="RAW")
    ws.update("B1", [["Etapa"]], value_input_option="RAW")

    # Linhas 2+: labels
    rows_a = [[chave]  for chave, _, _, _ in todos]
    rows_b = [[label]  for _, label, _, _ in todos]
    ws.update("A2", rows_a, value_input_option="RAW")
    ws.update("B2", rows_b, value_input_option="RAW")

    n = len(todos)
    fmt = [
        # Col A quasi-invisível (linhas 1+)
        {"repeatCell": {"range": {"sheetId": ws.id,
            "startRowIndex": 0, "endRowIndex": n + 1,
            "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {
                "foregroundColor": _rgb(220, 220, 220), "fontSize": 7}}},
            "fields": "userEnteredFormat.textFormat"}},
        # Header linha 1 col B
        {"repeatCell": {"range": {"sheetId": ws.id,
            "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 1, "endColumnIndex": 2},
            "cell": {"userEnteredFormat": {
                "backgroundColor": C_DATA_BG,
                "textFormat": {"foregroundColor": C_DATA_FG, "bold": True, "fontSize": 10},
                "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"}},
        # Congela 2 colunas e 1 linha
        {"updateSheetProperties": {"properties": {"sheetId": ws.id,
            "gridProperties": {"frozenColumnCount": 2, "frozenRowCount": 1}},
            "fields": "gridProperties(frozenColumnCount,frozenRowCount)"}},
        # Larguras
        {"updateDimensionProperties": {"range": {"sheetId": ws.id,
            "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 28}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id,
            "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 310}, "fields": "pixelSize"}},
        # Altura linha 1
        {"updateDimensionProperties": {"range": {"sheetId": ws.id,
            "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 26}, "fields": "pixelSize"}},
    ]

    # Formata cada linha de label (linhas 2+ = índices 1+)
    for i, (chave, label, tipo, fluxo) in enumerate(todos):
        row_i = i + 1  # 0-based: linha 2 da sheet = índice 1
        ct = {"listas": C_LISTAS_BG, "bazar": C_BAZAR_BG, "lp": C_LP_BG}[fluxo]
        cg = {"listas": C_GRP_LISTAS, "bazar": C_GRP_BAZAR, "lp": C_GRP_LP}[fluxo]
        if tipo == "titulo_fluxo": bg, fg, bold, size, h = ct, C_FG_WHITE, True, 11, 30
        elif tipo == "grupo":       bg, fg, bold, size, h = cg, C_GRP_FG, True, 9, 20
        elif tipo == "total":       bg, fg, bold, size, h = C_TOTAL_BG, C_TOTAL_FG, True, 10, 24
        elif tipo == "espaco":      bg, fg, bold, size, h = C_BRANCO, C_BRANCO, False, 8, 8
        else:
            bg = C_CINZA_LN if i % 2 == 0 else C_BRANCO
            fg, bold, size, h = C_PRETO_FG, False, 9, 22

        fmt.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": row_i, "endRowIndex": row_i+1,
                      "startColumnIndex": 1, "endColumnIndex": 2},
            "cell": {"userEnteredFormat": {"backgroundColor": bg,
                "textFormat": {"foregroundColor": fg, "bold": bold, "fontSize": size},
                "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"}})
        fmt.append({"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "ROWS",
                      "startIndex": row_i, "endIndex": row_i+1},
            "properties": {"pixelSize": h}, "fields": "pixelSize"}})

    sp.batch_update({"requests": fmt})
    logger.info("relatorio_funil: skeleton criado (%d linhas)", n)


def _find_or_add_col(ws, sp, data_br: str) -> int:
    """
    Procura a data na linha 1 (header row). Colunas de dados começam no índice 2 (col C).
    Retorna índice 0-based da coluna de dados.
    """
    row1 = ws.row_values(1)
    for i, val in enumerate(row1):
        if val.strip() == data_br:
            return i
    # Adiciona nova coluna à direita, nunca antes da col C (índice 2)
    next_i = max(2, len(row1))
    cl = _col_letter(next_i + 1)
    ws.update(f"{cl}1", [[data_br]], value_input_option="RAW")
    sp.batch_update({"requests": [
        {"repeatCell": {
            "range": {"sheetId": ws.id,
                      "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": next_i, "endColumnIndex": next_i+1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": C_DATA_BG,
                "textFormat": {"foregroundColor": C_DATA_FG, "bold": True, "fontSize": 10},
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                      "startIndex": next_i, "endIndex": next_i+1},
            "properties": {"pixelSize": 110}, "fields": "pixelSize"}},
    ]})
    return next_i


def _write_col(ws, sp, col_idx: int, data_br: str,
               ml: dict, mb: dict, mp: dict) -> None:
    """
    Escreve valores na coluna col_idx (0-based).
    Linha 1 (índice 0) = data (já escrita por _find_or_add_col).
    Linhas 2+ (índice 1+) = valores, alinhados com os labels de col B.
    """
    todos = _funil_listas() + _funil_bazar() + _funil_lp()
    all_m = {**ml, **mb, **mp}
    cl = _col_letter(col_idx + 1)

    valores = []
    for chave, _, tipo, _ in todos:
        if tipo in ("titulo_fluxo", "grupo", "espaco"):
            valores.append("")
        else:
            v = all_m.get(chave)
            valores.append(v if v is not None else "")

    # Linha 2 da sheet = índice de linha 1 (0-based)
    # Os labels estão em B2..B90, então valores vão em C2..C90
    start_sheet_row = 2
    end_sheet_row   = start_sheet_row + len(valores) - 1
    ws.update(f"{cl}{start_sheet_row}:{cl}{end_sheet_row}",
              [[v] for v in valores], value_input_option="RAW")

    fmt = []
    for i, (chave, _, tipo, fluxo) in enumerate(todos):
        row_i = 1 + i  # 0-based: linha 2 da sheet = índice 1
        ct = {"listas": C_LISTAS_BG, "bazar": C_BAZAR_BG, "lp": C_LP_BG}[fluxo]
        cg = {"listas": C_GRP_LISTAS, "bazar": C_GRP_BAZAR, "lp": C_GRP_LP}[fluxo]
        if tipo == "titulo_fluxo": bg, fg, bold = ct, C_FG_WHITE, True
        elif tipo == "grupo":       bg, fg, bold = cg, C_GRP_FG, True
        elif tipo == "total":       bg, fg, bold = C_TOTAL_BG, C_TOTAL_FG, True
        elif tipo == "espaco":      bg, fg, bold = C_BRANCO, C_BRANCO, False
        else:
            v = all_m.get(chave) or 0
            bg = C_ZERO if v == 0 else (C_CINZA_LN if i % 2 == 0 else C_BRANCO)
            fg, bold = C_PRETO_FG, False
        fmt.append({"repeatCell": {
            "range": {"sheetId": ws.id,
                      "startRowIndex": row_i, "endRowIndex": row_i+1,
                      "startColumnIndex": col_idx, "endColumnIndex": col_idx+1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg,
                "textFormat": {"foregroundColor": fg, "bold": bold, "fontSize": 9},
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}})

    if fmt:
        sp.batch_update({"requests": fmt})
    logger.info("relatorio_funil: coluna %s (%s) escrita", cl, data_br)

