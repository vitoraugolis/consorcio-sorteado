"""
jobs/auditoria_propostas.py — Auditoria diária de propostas vs. teto do cluster

Roda 1x/dia (agendado no main.py). Varre cards em stages ativos e reporta
no Slack qualquer proposta que ultrapasse o percentual máximo permitido para
a administradora (Cluster A=30%, Cluster B=30%, Cluster C/Caixa=27%).

Não altera cards — apenas audita e alerta. Correções são feitas manualmente.
"""

import logging
from datetime import datetime

from config import Stage, TZ_BRASILIA
from services.faro import FaroClient, FaroError, get_name, get_adm, get_phone
from services.slack import slack_error
from jobs.precificacao import (
    _parse_float,
    _validar_proposta_contra_teto,
    _get_cluster,
    _arredondar_milhar,
)

logger = logging.getLogger(__name__)

# Stages que têm proposta ativa — auditados diariamente
_STAGES_AUDITADOS = [
    Stage.PRECIFICACAO,
    Stage.EM_NEGOCIACAO,
    Stage.ACEITO,
    Stage.ASSINATURA,
    Stage.FINALIZACAO_COMERCIAL,
]

# Limite de violações a exibir por alerta (evita Slack flood)
_MAX_VIOLACOES_ALERTA = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_pct(valor: float, credito: float) -> str:
    if credito <= 0:
        return "?%"
    return f"{valor / credito:.1%}"


def _nome_stage(stage_id: str) -> str:
    _mapa = {
        Stage.PRECIFICACAO:          "Precificação",
        Stage.EM_NEGOCIACAO:         "Em Negociação",
        Stage.ACEITO:                "Aceito",
        Stage.ASSINATURA:            "Assinatura",
        Stage.FINALIZACAO_COMERCIAL: "Finalização Comercial",
    }
    return _mapa.get(stage_id, stage_id[:8])


# ---------------------------------------------------------------------------
# Auditoria principal
# ---------------------------------------------------------------------------

async def run_auditoria_propostas() -> None:
    """
    Audita propostas de todos os cards em stages ativos.
    Reporta violações de teto no Slack sem alterar os cards.
    """
    logger.info("=== Iniciando Auditoria de Propostas ===")
    agora_str = datetime.now(TZ_BRASILIA).strftime("%d/%m/%Y %H:%M")

    violacoes: list[dict] = []
    avisos: list[dict] = []   # propostas ≥ 90% do teto (próximas mas dentro do limite)
    total_auditados = 0
    erros_faro = 0

    try:
        async with FaroClient() as faro:
            for stage_id in _STAGES_AUDITADOS:
                try:
                    cards = await faro.get_cards_all_pages(stage_id=stage_id)
                except FaroError as e:
                    logger.error("Auditoria: erro ao listar cards do stage %s: %s", stage_id[:8], e)
                    erros_faro += 1
                    continue

                for card in cards:
                    total_auditados += 1
                    credito  = _parse_float(card.get("Crédito") or "0")
                    proposta = _parse_float(card.get("Proposta Realizada") or "0")
                    adm      = get_adm(card)
                    meses    = int(
                        _parse_float(card.get("Quantidade meses a pagar") or "999") or 999
                    )
                    nome     = get_name(card)
                    phone    = get_phone(card) or "—"
                    card_id  = card.get("id", "")[:8]

                    if credito <= 0 or proposta <= 0:
                        continue

                    valida, teto_val, motivo = _validar_proposta_contra_teto(
                        proposta, credito, adm, meses
                    )

                    if not valida:
                        violacoes.append({
                            "card_id": card_id,
                            "nome":    nome,
                            "phone":   phone,
                            "adm":     adm,
                            "proposta": proposta,
                            "teto":    teto_val,
                            "credito": credito,
                            "stage":   _nome_stage(stage_id),
                            "motivo":  motivo,
                        })
                    else:
                        # Aviso: ≥ 90% do teto (dentro do limite, mas próximo)
                        cluster = _get_cluster(adm, meses)
                        teto_pct = cluster[-1]
                        if credito > 0 and (proposta / credito) >= teto_pct * 0.90:
                            avisos.append({
                                "card_id":  card_id,
                                "nome":     nome,
                                "adm":      adm,
                                "proposta": proposta,
                                "teto":     teto_val,
                                "credito":  credito,
                                "stage":    _nome_stage(stage_id),
                            })

    except Exception as e:
        logger.exception("Auditoria: erro inesperado: %s", e)
        await slack_error(
            "🚨 Job de auditoria de propostas falhou inesperadamente",
            exception=e,
        )
        return

    logger.info(
        "Auditoria concluída: %d auditados | %d violações | %d avisos | %d erros FARO",
        total_auditados, len(violacoes), len(avisos), erros_faro,
    )

    # ── Relatório de violações ────────────────────────────────────────────────
    if violacoes:
        exibir = violacoes[:_MAX_VIOLACOES_ALERTA]
        resto  = len(violacoes) - len(exibir)

        linhas = [
            f"🚨 *AUDITORIA DE PROPOSTAS — {agora_str}*\n",
            f"*{len(violacoes)} proposta(s) ACIMA DO TETO* detectadas em {total_auditados} cards auditados:\n",
        ]
        for v in exibir:
            linhas.append(
                f"• `{v['card_id']}` *{v['nome']}* | {v['adm']} | Stage: {v['stage']}\n"
                f"  Proposta: R$ {v['proposta']:,.0f} ({_fmt_pct(v['proposta'], v['credito'])})"
                f" → Teto: R$ {v['teto']:,.0f} ({_fmt_pct(v['teto'], v['credito'])})\n"
                f"  📞 {v['phone']}"
            )
        if resto > 0:
            linhas.append(f"\n… e mais {resto} violação(ões) não exibidas.")
        linhas.append("\n⚠️ Corrija os campos *Proposta Realizada* no FARO para os cards acima.")

        await slack_error(
            "\n".join(linhas),
            context={"total_violacoes": len(violacoes), "total_auditados": total_auditados},
        )
    elif total_auditados > 0:
        # Tudo ok — log silencioso (não polui Slack quando não há problemas)
        logger.info("Auditoria: nenhuma violação de teto encontrada em %d cards.", total_auditados)

    # ── Relatório de avisos (próximos ao teto) ────────────────────────────────
    if avisos:
        exibir_av = avisos[:_MAX_VIOLACOES_ALERTA]
        linhas_av = [
            f"⚠️ *AUDITORIA — Propostas próximas do teto ({agora_str})*\n",
            f"*{len(avisos)} proposta(s)* estão entre 90% e 100% do teto permitido:\n",
        ]
        for a in exibir_av:
            linhas_av.append(
                f"• `{a['card_id']}` *{a['nome']}* | {a['adm']} | Stage: {a['stage']}\n"
                f"  Proposta: R$ {a['proposta']:,.0f} ({_fmt_pct(a['proposta'], a['credito'])})"
                f" → Teto: R$ {a['teto']:,.0f} ({_fmt_pct(a['teto'], a['credito'])})"
            )
        linhas_av.append("\nℹ️ Dentro do limite, mas revise se foram inseridas manualmente.")
        await slack_error(
            "\n".join(linhas_av),
            context={"total_avisos": len(avisos)},
        )


async def run_auditoria_propostas_safe() -> None:
    """Wrapper resiliente — garante que exceções não derrubam o scheduler."""
    try:
        await run_auditoria_propostas()
    except Exception as e:
        logger.exception("run_auditoria_propostas: erro inesperado: %s", e)
        try:
            await slack_error("Job auditoria_propostas falhou inesperadamente", exception=e)
        except Exception:
            pass
