"""
jobs/sla_monitor.py — Monitor de SLA para FINALIZAÇÃO COM AGENTE COMERCIAL

Varre o stage FINALIZACAO_COMERCIAL a cada ciclo e alerta no Slack quando
um lead está parado há mais de SLA_HORAS sem atividade humana.

Lógica:
  - Lê o campo "Ultima atividade" de cada card
  - Se inativo há > SLA_HORAS E ainda não alertado neste ciclo → alerta Slack
  - Usa Redis para evitar spam de alertas: chave cs:sla:alert:{card_id}
    com TTL de SLA_ALERT_COOLDOWN_H horas (não repete alerta para o mesmo card)
  - Fail-open: se Redis cair, alerta normalmente (melhor alertar 2x que não alertar)
"""

import logging
import time
from datetime import datetime, timezone

from config import Stage, TZ_BRASILIA, SEND_WINDOW_START, SEND_WINDOW_END
from services.faro import FaroClient, FaroError
from services.slack import slack_warning
from services.session_store import get_redis

logger = logging.getLogger(__name__)

# Tempo máximo sem atividade antes de alertar (em horas)
SLA_HORAS: int = 48

# Cooldown entre alertas para o mesmo card (em horas) — evita spam
SLA_ALERT_COOLDOWN_H: int = 24


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _parse_ts(ultima: str | int | None) -> float | None:
    """Converte campo 'Ultima atividade' para timestamp Unix."""
    if not ultima:
        return None
    try:
        if str(ultima).isdigit():
            return float(ultima)
        return datetime.fromisoformat(str(ultima).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


async def _already_alerted(card_id: str) -> bool:
    """Verifica se já alertamos sobre este card recentemente."""
    try:
        redis = await get_redis()
        key = f"cs:sla:alert:{card_id}"
        return bool(await redis.exists(key))
    except Exception:
        return False  # fail-open — se Redis cair, alerta normalmente


async def _mark_alerted(card_id: str) -> None:
    """Marca card como alertado para evitar spam."""
    try:
        redis = await get_redis()
        key = f"cs:sla:alert:{card_id}"
        await redis.set(key, "1", ex=SLA_ALERT_COOLDOWN_H * 3600)
    except Exception:
        pass  # fail-open


async def run_sla_monitor() -> None:
    """
    Varre FINALIZACAO_COMERCIAL e alerta no Slack sobre leads parados há > SLA_HORAS.
    Roda dentro da janela de envio (08h–20h BRT) para não incomodar fora do horário.
    """
    if not _is_within_send_window():
        logger.debug("SLA monitor: fora da janela de envio — pulando")
        return

    logger.info("SLA monitor: iniciando varredura de FINALIZACAO_COMERCIAL")

    try:
        async with FaroClient() as faro:
            cards = await faro.get_cards_from_stage(Stage.FINALIZACAO_COMERCIAL)
    except FaroError as e:
        logger.error("SLA monitor: erro ao buscar cards: %s", e)
        return

    agora = time.time()
    limite = SLA_HORAS * 3600
    alertas = 0

    for card in cards:
        card_id  = card.get("id", "")
        nome     = card.get("title") or card.get("Nome") or "sem nome"
        telefone = card.get("Telefone") or ""
        adm      = card.get("Administradora") or ""
        proposta = card.get("Proposta Realizada") or ""
        fonte    = card.get("Fonte") or ""
        sit      = card.get("Situacao Negociacao") or ""

        ts = _parse_ts(card.get("Ultima atividade"))
        if ts is None:
            # Sem timestamp — usa created_at como fallback
            ts = _parse_ts(card.get("created_at"))
        if ts is None:
            continue

        inativo_h = (agora - ts) / 3600

        if inativo_h < SLA_HORAS:
            continue

        if await _already_alerted(card_id):
            continue

        # Monta alerta
        inativo_str = (
            f"{int(inativo_h)}h"
            if inativo_h < 48
            else f"{inativo_h / 24:.1f} dias"
        )
        await slack_warning(
            f"⏰ *SLA VENCIDO* — Lead parado há {inativo_str} em FINALIZAÇÃO COM AGENTE COMERCIAL",
            context={
                "Cliente":    nome,
                "Telefone":   telefone,
                "Adm":        adm,
                "Proposta":   f"R$ {proposta}" if proposta else "—",
                "Fonte":      fonte,
                "Situação":   sit or "—",
                "Card ID":    card_id[:12],
                "Parado há":  inativo_str,
            },
        )
        await _mark_alerted(card_id)
        alertas += 1
        logger.info(
            "SLA monitor: alerta enviado para %s (%s) — inativo há %s",
            nome, card_id[:8], inativo_str,
        )

    logger.info(
        "SLA monitor: varredura concluída — %d cards verificados, %d alertas enviados",
        len(cards), alertas,
    )


async def run_sla_monitor_safe() -> None:
    """Wrapper com captura de exceções para uso no scheduler."""
    try:
        await run_sla_monitor()
    except Exception as e:
        logger.exception("SLA monitor: erro inesperado: %s", e)
        try:
            from services.sentry import capture_exception
            capture_exception(e)
        except Exception:
            pass
