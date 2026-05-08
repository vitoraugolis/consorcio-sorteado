"""
Watchdog de extratos não processados.

Monitora keys Redis `extrato:pendente:{phone}` com TTL de 10 min.
Se uma key tiver TTL < 5 min (ou seja, passou mais de 5 min desde o recebimento
sem conclusão), significa que o qualificador não conseguiu processar — alerta equipe.

Rodado a cada 5 minutos pelo APScheduler.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_PENDENTE_PREFIX = "extrato:pendente:"
_ALERTA_TTL_THRESHOLD = 300  # alerta se TTL restante < 5 min (passou mais de 5 min)
_ALERTA_COOLDOWN_PREFIX = "extrato:alerta_enviado:"


async def run_watchdog_extratos() -> None:
    """
    Varre Redis em busca de extratos recebidos mas não processados.
    Alerta equipe via grupo WA + Slack para cada caso encontrado.
    """
    try:
        from services.session_store import get_redis
        rd = await get_redis()

        keys = await rd.keys(f"{_PENDENTE_PREFIX}*")
        if not keys:
            return

        alertas = []
        for key in keys:
            ttl = await rd.ttl(key)
            if ttl < 0:
                continue  # key expirou mas Redis ainda não limpou — ignora
            if ttl > _ALERTA_TTL_THRESHOLD:
                continue  # ainda dentro da janela normal de processamento

            # Passou mais de 5 min sem processamento — candidato a alerta
            phone = key.replace(_PENDENTE_PREFIX, "")
            valor = (await rd.get(key)) or ""
            card_id, nome = (valor.split("|", 1) + ["?"])[:2]

            # Evita enviar o mesmo alerta duas vezes
            cooldown_key = f"{_ALERTA_COOLDOWN_PREFIX}{phone}"
            if await rd.exists(cooldown_key):
                continue

            alertas.append((phone, card_id, nome, ttl))
            # Marca como alertado por 15 min
            await rd.set(cooldown_key, "1", ex=900)

        if not alertas:
            return

        from services.whapi import notify_team
        from services.slack import slack_alert

        for phone, card_id, nome, ttl_restante in alertas:
            minutos_preso = int((600 - ttl_restante) / 60)
            msg = (
                f"⚠️ *Extrato não processado — ação necessária*\n"
                f"Lead: *{nome}* (`...{phone[-4:]}`)\n"
                f"Card: `{card_id[:8]}`\n"
                f"Extrato recebido há ~{minutos_preso} min sem qualificação.\n"
                f"Possível falha do qualificador — verificar e processar manualmente."
            )
            logger.warning(
                "Watchdog: extrato pendente há %d min — card=%s phone=...%s",
                minutos_preso, card_id[:8], phone[-4:],
            )
            try:
                import asyncio
                asyncio.ensure_future(notify_team(msg))
                asyncio.ensure_future(slack_alert(msg, level="warning"))
            except Exception as e:
                logger.error("Watchdog: falha ao enviar alerta: %s", e)

    except Exception as e:
        logger.error("Watchdog extratos: erro na varredura: %s", e)
