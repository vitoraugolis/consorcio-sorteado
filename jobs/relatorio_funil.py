import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
import httpx
from config import Stage, TZ_BRASILIA, PIPELINE_ID
from services.faro import FaroClient, FaroError, get_fonte
from services.slack import slack_alert, slack_error

logger = logging.getLogger(__name__)

WEBHOOK_URL = "https://hook.us1.make.com/l2x7w83wlksrycrz7zilorookxnudt26"

RETROATIVO_DATAS = [
    "23/04/2026", "24/04/2026", "27/04/2026", "28/04/2026",
    "29/04/2026", "30/04/2026", "01/05/2026", "03/05/2026",
    "04/05/2026", "05/05/2026", "06/05/2026", "07/05/2026",
]

# Ajustes manuais: eventos confirmados que podem nao estar no FARO
# Gabriell (aceite Porto Seguro) + Ludmilla (aceite delegado Manuela) + Jose (aceite Bazar) = 3 aceites Bazar
# Gabriell = 1 sucesso Bazar
# Douglas (neg Rk) + Matheus Costa (neg Rk) = 2 negociacoes Bazar
# Anderson (precificado) = 1 precificacao Bazar
MANUAL_ADJUSTMENTS = {
    "b_acei": 3,
    "b_suc":  1,
    "b_neg":  2,
    "b_prec": 1,
}


def _is_date(val, iso, br):
    if not val: return False
    v = str(val).strip()
    return v.startswith(iso) or v == br

def _ativado(card, iso, br):
    return _is_date(card.get("Data de primeira ativacao"), iso, br) or _is_date(card.get("Data de primeira ativação"), iso, br)

def _ativado2(card, iso, br):
    return _is_date(card.get("Data de segunda ativacao"), iso, br) or _is_date(card.get("Data de segunda ativação"), iso, br)

def _ativado3(card, iso, br):
    return _is_date(card.get("Data de terceira ativacao"), iso, br) or _is_date(card.get("Data de terceira ativação"), iso, br)

def _ativado4(card, iso, br):
    return _is_date(card.get("Data de quarta ativacao"), iso, br) or _is_date(card.get("Data de quarta ativação"), iso, br)

def _movido(card, iso, br):
    return (
        _is_date(card.get("updated_at"), iso, br) or
        _is_date(card.get("Ultima atividade"), iso, br)
    )


def _calcular(cards, fluxo, iso, br):
    def match(c):
        f = (get_fonte(c) or c.get("Etiquetas") or "").lower()
        if fluxo == "listas": return "lista" in f
        if fluxo == "bazar":  return "bazar" in f or "organico" in f or "orgânico" in f
        if fluxo == "lp":     return f in ("lp", "landing page") or "site" in f
        return False

    sub = [c for c in cards if match(c)]
    mov = [c for c in sub if _movido(c, iso, br)]

    def cnt(stage_id):
        return sum(1 for c in mov if c.get("stage_id") == stage_id)

    def cnt_motivo(substr):
        return sum(1 for c in mov
                   if c.get("stage_id") == Stage.PERDIDO
                   and substr.lower() in (c.get("Motivo de perda") or "").lower())

    atv1 = sum(1 for c in sub if _ativado(c, iso, br))
    atv2 = sum(1 for c in sub if _ativado2(c, iso, br))
    atv3 = sum(1 for c in sub if _ativado3(c, iso, br))
    atv4 = sum(1 for c in sub if _ativado4(c, iso, br))

    total_ids = {c["id"] for c in sub if _ativado(c, iso, br) or _movido(c, iso, br)}
    prefix = {"listas": "l_", "bazar": "b_", "lp": "p_"}[fluxo]
    p = prefix

    m = {
        f"{p}atv1": atv1, f"{p}atv2": atv2, f"{p}atv3": atv3, f"{p}atv4": atv4,
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
        m.update({
            "l_int":  interessados,
            "l_nint": cnt_motivo("sem interesse"),
            "l_sret": cnt_motivo("sem retorno"),
        })
    else:
        m.update({
            f"{p}cont": cnt(Stage.EM_CONTATO),
            f"{p}extr": sum(1 for c in mov if c.get("Link do Extrato")),
            f"{p}eval": sum(1 for c in mov if c.get("Link do Extrato") and c.get("Credito") or c.get("Crédito")),
            f"{p}einv": sum(1 for c in mov if any(
                t in (c.get("Motivo dispensa") or "").lower()
                for t in ("incorreto", "ilegivel", "invalido", "ilegível")
            )),
            f"{p}ncnt": sum(1 for c in mov if "nao-cont" in (c.get("Tipo contemplacao") or c.get("Tipo contemplação") or "").lower()
                            or "nao contemplado" in (c.get("Tipo contemplacao") or c.get("Tipo contemplação") or "").lower()),
            f"{p}lanc": sum(1 for c in mov if "lance" in (c.get("Tipo contemplacao") or c.get("Tipo contemplação") or "").lower()),
            f"{p}hold": cnt(Stage.ON_HOLD),
        })

    if fluxo == "bazar":
        perdidos_tot = cnt(Stage.PERDIDO)
        psm = cnt_motivo("sem margem")
        psi = cnt_motivo("sem interesse")
        psr = cnt_motivo("sem retorno")
        pnp = sum(1 for c in mov if c.get("stage_id") == Stage.PERDIDO and "adm" in (c.get("Motivo de perda") or "").lower() and "parceira" in (c.get("Motivo de perda") or "").lower())
        pcv = cnt_motivo("cota vendida")
        m.update({"b_psm": psm, "b_psi": psi, "b_psr": psr, "b_pnp": pnp, "b_pcv": pcv, "b_pout": max(0, perdidos_tot - psm - psi - psr - pnp - pcv)})

    return m


def _calcular_consolidado(ml, mb, mp):
    return {
        "g_atv": ml.get("l_atv1", 0) + mb.get("b_atv1", 0) + mp.get("p_atv1", 0),
        "g_neg": ml.get("l_neg", 0)  + mb.get("b_neg", 0)  + mp.get("p_neg", 0),
        "g_suc": ml.get("l_suc", 0)  + mb.get("b_suc", 0)  + mp.get("p_suc", 0),
    }


def _pct(num, den):
    if not den: return "—"
    return f"{num/den*100:.1f}%"

def _calcular_taxas(all_m):
    TAXAS = {
        "bt_extr": ("b_extr", "b_atv1"), "bt_eval": ("b_eval", "b_extr"),
        "bt_prec": ("b_prec", "b_eval"), "bt_neg": ("b_neg", "b_prec"),
        "bt_conv": ("b_suc", "b_atv1"),
        "lt_resp": ("l_int", "l_atv1"), "lt_qual": ("l_prec", "l_int"),
        "lt_neg":  ("l_neg", "l_prec"), "lt_conv": ("l_suc", "l_atv1"),
        "pt_extr": ("p_extr", "p_atv1"), "pt_eval": ("p_eval", "p_extr"),
        "pt_prec": ("p_prec", "p_eval"), "pt_neg": ("p_neg", "p_prec"),
        "pt_conv": ("p_suc", "p_atv1"), "g_conv": ("g_suc", "g_atv"),
    }
    return {k: _pct(all_m.get(n, 0), all_m.get(d, 0)) for k, (n, d) in TAXAS.items()}


async def _fetch_all_cards():
    all_cards = []
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
                logger.error("relatorio_funil: offset=%d: %s", offset, e)
                break
    return all_cards


async def _post_webhook(html):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(WEBHOOK_URL, json={"html": html})
        resp.raise_for_status()
        logger.info("relatorio_funil: webhook OK status=%d", resp.status_code)


def _row(label, value, highlight=False):
    bg = "#f1f8e9" if highlight else "#ffffff"
    fw = "bold" if highlight else "normal"
    if not value:
        return ""
    return (
        f'<tr style="background:{bg};">'
        f'<td style="padding:5px 10px;border-bottom:1px solid #eee;font-size:13px;color:#333;">{label}</td>'
        f'<td style="padding:5px 10px;border-bottom:1px solid #eee;text-align:right;font-weight:{fw};font-size:13px;color:#111;">{value}</td>'
        f'</tr>'
    )


def _secao_fluxo(cor, emoji, nome, metricas_labels, valores, taxas=None, taxas_labels=None):
    rows = ""
    for label, chave in metricas_labels:
        v = valores.get(chave, 0)
        if not v:
            continue
        rows += _row(label, v)
    if taxas and taxas_labels:
        for chave_t, label_t in taxas_labels:
            v = taxas.get(chave_t, "—")
            if v == "—":
                continue
            rows += _row(f"  → {label_t}", v, highlight=True)
    if not rows:
        return ""
    return (
        f'<div style="margin-bottom:14px;">'
        f'<div style="background:{cor};color:#fff;padding:8px 12px;border-radius:4px 4px 0 0;font-weight:bold;font-size:14px;">{emoji} {nome}</div>'
        f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #ddd;border-top:none;border-radius:0 0 4px 4px;">'
        f'{rows}'
        f'</table></div>'
    )


def _build_html_dia(data_br, ml, mb, mp):
    mg = _calcular_consolidado(ml, mb, mp)
    all_m = {**ml, **mb, **mp, **mg}
    taxas = _calcular_taxas(all_m)

    listas_m = [
        ("1a ativacao", "l_atv1"), ("2a ativacao", "l_atv2"),
        ("3a ativacao", "l_atv3"), ("4a ativacao", "l_atv4"),
        ("Problema de contato", "l_prob"),
        ("Demonstraram interesse", "l_int"),
        ("Sem interesse/recusa", "l_nint"), ("Sem retorno", "l_sret"),
        ("Precificacao", "l_prec"), ("Em negociacao", "l_neg"),
        ("Neg. congelada", "l_ngc"), ("Aceites", "l_acei"),
        ("Assinatura", "l_assi"), ("Sucesso", "l_suc"),
        ("Nao qualificados", "l_nq"), ("Perdidos", "l_perd"),
        ("Dispensados", "l_disp"), ("Lixo", "l_lixo"),
    ]
    listas_t = [
        ("lt_resp", "Taxa resposta"), ("lt_qual", "Taxa qualificacao"),
        ("lt_neg", "Taxa negociacao"), ("lt_conv", "Taxa conversao"),
    ]
    bazar_m = [
        ("1a ativacao", "b_atv1"), ("2a ativacao", "b_atv2"),
        ("3a ativacao", "b_atv3"), ("4a ativacao", "b_atv4"),
        ("Problema de contato", "b_prob"),
        ("Em conversa", "b_cont"), ("Extratos recebidos", "b_extr"),
        ("Extrato valido", "b_eval"), ("Extrato invalido", "b_einv"),
        ("Nao contemplado", "b_ncnt"), ("Cotas lance", "b_lanc"),
        ("On hold", "b_hold"), ("Precificacao", "b_prec"),
        ("Em negociacao", "b_neg"), ("Neg. congelada", "b_ngc"),
        ("Aceites", "b_acei"), ("Assinatura", "b_assi"),
        ("Sucesso", "b_suc"), ("Nao qualificados", "b_nq"),
        ("Perdidos", "b_perd"), ("Dispensados", "b_disp"), ("Lixo", "b_lixo"),
    ]
    bazar_t = [
        ("bt_extr", "Taxa extrato recebido"), ("bt_eval", "Taxa extrato valido"),
        ("bt_prec", "Taxa precificacao"), ("bt_neg", "Taxa negociacao"),
        ("bt_conv", "Taxa conversao"),
    ]
    lp_m = [
        ("1a ativacao", "p_atv1"), ("2a ativacao", "p_atv2"),
        ("3a ativacao", "p_atv3"), ("4a ativacao", "p_atv4"),
        ("Problema de contato", "p_prob"),
        ("Em conversa", "p_cont"), ("Extratos recebidos", "p_extr"),
        ("Extrato valido", "p_eval"), ("Extrato invalido", "p_einv"),
        ("Nao contemplado", "p_ncnt"), ("Leads lance", "p_lanc"),
        ("Precificacao", "p_prec"), ("Em negociacao", "p_neg"),
        ("Aceites", "p_acei"), ("Assinatura", "p_assi"),
        ("Sucesso", "p_suc"), ("Nao qualificados", "p_nq"),
        ("Perdidos", "p_perd"), ("Dispensados", "p_disp"),
    ]
    lp_t = [
        ("pt_extr", "Taxa extrato recebido"), ("pt_eval", "Taxa extrato valido"),
        ("pt_prec", "Taxa precificacao"), ("pt_neg", "Taxa negociacao"),
        ("pt_conv", "Taxa conversao"),
    ]

    sl = _secao_fluxo("#1b5e20", "\U0001f4cb", "LISTAS", listas_m, ml, taxas, listas_t)
    sb = _secao_fluxo("#0d47a1", "\U0001f3ea", "BAZAR", bazar_m, mb, taxas, bazar_t)
    sp = _secao_fluxo("#4a148c", "\U0001f310", "LP / SITE", lp_m, mp, taxas, lp_t)

    if not sl and not sb and not sp:
        return f'<div style="color:#aaa;font-style:italic;padding:8px 0;">Sem movimentacao em {data_br}</div>'

    return (
        f'<div style="margin-bottom:28px;border:1px solid #e0e0e0;border-radius:6px;overflow:hidden;">'
        f'<div style="background:#37474f;color:#fff;padding:10px 16px;font-weight:bold;font-size:16px;">\U0001f4c5 {data_br}</div>'
        f'<div style="padding:14px 16px;">{sl}{sb}{sp}</div>'
        f'</div>'
    )


def _build_html_relatorio(titulo, subtitulo, secoes_html, totais_ml, totais_mb, totais_mp):
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    agora_br = _dt.now(_ZI("America/Sao_Paulo")).strftime("%d/%m/%Y %H:%M")

    mg = _calcular_consolidado(totais_ml, totais_mb, totais_mp)
    all_m = {**totais_ml, **totais_mb, **totais_mp, **mg}
    taxas = _calcular_taxas(all_m)

    totais_items = [
        ("Listas — Total ativacoes", totais_ml.get("l_atv1", 0)),
        ("Listas — Precificacoes", totais_ml.get("l_prec", 0)),
        ("Listas — Negociacoes", totais_ml.get("l_neg", 0)),
        ("Listas — Aceites", totais_ml.get("l_acei", 0)),
        ("Listas — Sucesso", totais_ml.get("l_suc", 0)),
        ("Listas — Taxa conversao", taxas.get("lt_conv", "—")),
        ("Bazar — Total ativacoes", totais_mb.get("b_atv1", 0)),
        ("Bazar — Extratos recebidos", totais_mb.get("b_extr", 0)),
        ("Bazar — Precificacoes", totais_mb.get("b_prec", 0)),
        ("Bazar — Negociacoes", totais_mb.get("b_neg", 0)),
        ("Bazar — Aceites", totais_mb.get("b_acei", 0)),
        ("Bazar — Sucesso", totais_mb.get("b_suc", 0)),
        ("Bazar — Taxa conversao", taxas.get("bt_conv", "—")),
        ("LP — Total ativacoes", totais_mp.get("p_atv1", 0)),
        ("LP — Extratos recebidos", totais_mp.get("p_extr", 0)),
        ("LP — Precificacoes", totais_mp.get("p_prec", 0)),
        ("LP — Negociacoes", totais_mp.get("p_neg", 0)),
        ("LP — Aceites", totais_mp.get("p_acei", 0)),
        ("LP — Sucesso", totais_mp.get("p_suc", 0)),
        ("LP — Taxa conversao", taxas.get("pt_conv", "—")),
        ("GERAL — Total ativacoes", mg.get("g_atv", 0)),
        ("GERAL — Total negociacoes", mg.get("g_neg", 0)),
        ("GERAL — Total sucesso", mg.get("g_suc", 0)),
        ("GERAL — Taxa conversao geral", taxas.get("g_conv", "—")),
    ]
    totais_rows = ""
    for lbl, v in totais_items:
        if not v:
            continue
        hl = "GERAL" in lbl
        totais_rows += _row(lbl, v, highlight=hl)

    corpo = "\n".join(secoes_html)

    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head><meta charset=\"UTF-8\"><title>" + titulo + "</title></head>\n"
        "<body style=\"font-family:Arial,Helvetica,sans-serif;background:#f4f4f4;margin:0;padding:20px;\">\n"
        "  <div style=\"max-width:680px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);\">\n"
        "    <div style=\"background:#1a237e;color:#fff;padding:28px 32px;\">\n"
        "      <h1 style=\"margin:0 0 6px;font-size:22px;font-weight:bold;\">" + titulo + "</h1>\n"
        "      <p style=\"margin:0;opacity:0.82;font-size:14px;\">" + subtitulo + "</p>\n"
        "    </div>\n"
        "    <div style=\"padding:24px 28px;\">\n"
        "      " + corpo + "\n"
        "      <div style=\"margin:20px 0;padding:16px 18px;background:#e8f5e9;border:1px solid #a5d6a7;border-radius:6px;\">\n"
        "        <h3 style=\"margin:0 0 10px;color:#1b5e20;font-size:15px;\">&#x2705; Aceites Confirmados no Periodo</h3>\n"
        "        <ul style=\"margin:0;padding-left:18px;color:#2e7d32;font-size:13px;line-height:1.8;\">\n"
        "          <li><strong>Gabriell</strong> &mdash; Porto Seguro &mdash; aceite confirmado, encaminhado para contrato</li>\n"
        "          <li><strong>Ludmilla</strong> &mdash; aceite confirmado, delegado para Manuela</li>\n"
        "          <li><strong>Jose</strong> &mdash; aceite confirmado (canal Bazar)</li>\n"
        "        </ul>\n"
        "      </div>\n"
        "      <div style=\"margin-top:24px;\">\n"
        "        <div style=\"background:#263238;color:#fff;padding:10px 14px;border-radius:4px 4px 0 0;font-weight:bold;font-size:15px;\">&#x1f3c6; Consolidado do Periodo</div>\n"
        "        <table style=\"width:100%;border-collapse:collapse;background:#fff;border:1px solid #ddd;border-top:none;border-radius:0 0 4px 4px;\">\n"
        "          " + totais_rows + "\n"
        "        </table>\n"
        "      </div>\n"
        "    </div>\n"
        "    <div style=\"background:#f5f5f5;padding:14px 28px;font-size:11px;color:#999;text-align:center;border-top:1px solid #e0e0e0;\">\n"
        "      Gerado automaticamente em " + agora_br + " BRT &mdash; Consorcio Sorteado\n"
        "    </div>\n"
        "  </div>\n"
        "</body>\n"
        "</html>"
    )


async def run_relatorio_funil(data_alvo=None):
    """Roda para um dia (ontem por default). Envia webhook + Slack."""
    agora_br = datetime.now(TZ_BRASILIA)
    if data_alvo:
        alvo = data_alvo
    elif agora_br.hour < 3:
        alvo = agora_br - timedelta(days=1)
    else:
        alvo = agora_br

    data_br  = alvo.strftime("%d/%m/%Y")
    data_iso = alvo.strftime("%Y-%m-%d")

    try:
        cards = await _fetch_all_cards()
        logger.info("relatorio_funil: %d cards coletados para %s", len(cards), data_br)
    except Exception as e:
        logger.error("relatorio_funil: falha ao buscar cards: %s", e)
        return

    ml = _calcular(cards, "listas", data_iso, data_br)
    mb = _calcular(cards, "bazar",  data_iso, data_br)
    mp = _calcular(cards, "lp",     data_iso, data_br)

    html = _build_html_relatorio(
        "Relatorio de Funil — Consorcio Sorteado",
        f"Data: {data_br}",
        [_build_html_dia(data_br, ml, mb, mp)],
        ml, mb, mp,
    )

    try:
        await _post_webhook(html)
    except Exception as e:
        logger.error("relatorio_funil: erro webhook: %s", e)
        try:
            await slack_error("Falha ao enviar relatorio via webhook", exception=e, context={"data": data_br})
        except Exception:
            pass
        return

    try:
        mg = _calcular_consolidado(ml, mb, mp)
        all_m = {**ml, **mb, **mp, **mg}
        taxas = _calcular_taxas(all_m)
        msg = (
            f"\U0001f4c5 *Relatorio Funil — {data_br}*\n\n"
            f"Listas: {ml.get('l_atv1',0)} ativ | {ml.get('l_int',0)} interesse | {ml.get('l_suc',0)} sucesso\n"
            f"Bazar: {mb.get('b_atv1',0)} ativ | {mb.get('b_prec',0)} prec | {mb.get('b_neg',0)} neg | {mb.get('b_acei',0)} aceites\n"
            f"LP: {mp.get('p_atv1',0)} ativ | {mp.get('p_prec',0)} prec | {mp.get('p_neg',0)} neg | {mp.get('p_acei',0)} aceites\n"
            f"Conversao geral: {taxas.get('g_conv','—')}\n\n"
            f"✅ Relatorio enviado por e-mail."
        )
        await slack_alert(msg, level="info")
    except Exception as e:
        logger.warning("relatorio_funil: slack: %s", e)


async def run_relatorio_retroativo(datas=None):
    """Roda para multiplas datas. Consolida num unico e-mail HTML + Slack."""
    if datas is None:
        datas = RETROATIVO_DATAS

    logger.info("relatorio_funil: retroativo para %d datas", len(datas))

    try:
        cards = await _fetch_all_cards()
        logger.info("relatorio_funil: %d cards coletados", len(cards))
    except Exception as e:
        logger.error("relatorio_funil: falha ao buscar cards: %s", e)
        return

    secoes = []
    totais_ml: dict = {}
    totais_mb: dict = {}
    totais_mp: dict = {}

    for data_str in datas:
        try:
            if "/" in data_str:
                d, m, y = data_str.split("/")
                data_iso = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                data_br  = data_str
            else:
                from datetime import date as _date
                dt = _date.fromisoformat(data_str)
                data_iso = data_str
                data_br  = dt.strftime("%d/%m/%Y")

            ml = _calcular(cards, "listas", data_iso, data_br)
            mb = _calcular(cards, "bazar",  data_iso, data_br)
            mp = _calcular(cards, "lp",     data_iso, data_br)

            for k, v in ml.items():
                totais_ml[k] = totais_ml.get(k, 0) + v
            for k, v in mb.items():
                totais_mb[k] = totais_mb.get(k, 0) + v
            for k, v in mp.items():
                totais_mp[k] = totais_mp.get(k, 0) + v

            secoes.append(_build_html_dia(data_br, ml, mb, mp))
            logger.info("relatorio_funil: processado %s", data_br)

        except Exception as e:
            logger.error("relatorio_funil: erro na data %s: %s", data_str, e)

    # Aplica ajustes manuais nos totais consolidados
    for k, v in MANUAL_ADJUSTMENTS.items():
        if k.startswith("b_"):
            totais_mb[k] = totais_mb.get(k, 0) + v
        elif k.startswith("l_"):
            totais_ml[k] = totais_ml.get(k, 0) + v
        elif k.startswith("p_"):
            totais_mp[k] = totais_mp.get(k, 0) + v

    logger.info("relatorio_funil: ajustes manuais aplicados: %s", MANUAL_ADJUSTMENTS)

    data_inicio = datas[0] if datas else "?"
    data_fim    = datas[-1] if datas else "?"

    html = _build_html_relatorio(
        "Relatorio de Funil — Consorcio Sorteado",
        f"Periodo retroativo: {data_inicio} a {data_fim}",
        secoes,
        totais_ml, totais_mb, totais_mp,
    )

    try:
        await _post_webhook(html)
        logger.info("relatorio_funil: retroativo enviado via webhook")
    except Exception as e:
        logger.error("relatorio_funil: erro webhook retroativo: %s", e)
        try:
            await slack_error("Falha ao enviar relatorio retroativo via webhook", exception=e)
        except Exception:
            pass
        return

    try:
        mg = _calcular_consolidado(totais_ml, totais_mb, totais_mp)
        all_m = {**totais_ml, **totais_mb, **totais_mp, **mg}
        taxas = _calcular_taxas(all_m)
        msg = (
            f"\U0001f4ca *Relatorio Retroativo — {data_inicio} a {data_fim}*\n\n"
            f"Listas: {totais_ml.get('l_atv1',0)} ativ | {totais_ml.get('l_suc',0)} sucesso\n"
            f"Bazar: {totais_mb.get('b_atv1',0)} ativ | {totais_mb.get('b_prec',0)} prec | {totais_mb.get('b_neg',0)} neg | {totais_mb.get('b_acei',0)} aceites\n"
            f"LP: {totais_mp.get('p_atv1',0)} ativ | {totais_mp.get('p_prec',0)} prec | {totais_mp.get('p_neg',0)} neg | {totais_mp.get('p_acei',0)} aceites\n"
            f"Conversao geral: {taxas.get('g_conv','—')}\n\n"
            f"✅ Aceites confirmados: Gabriell, Ludmilla, Jose (incluidos nos totais)\n"
            f"✅ Relatorio enviado por e-mail."
        )
        await slack_alert(msg, level="info")
    except Exception as e:
        logger.warning("relatorio_funil: slack retroativo: %s", e)

    logger.info("relatorio_funil: retroativo concluido")
