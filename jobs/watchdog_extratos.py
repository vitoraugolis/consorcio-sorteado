"""
Watchdog de mensagens não respondidas.

Monitora keys Redis `msg:pendente:{phone}` com TTL configurável.
Qualquer mensagem de lead que chega e espera resposta do sistema é registrada.
Se o sistema não responder em tempo hábil, alerta a equipe.

TTLs:
  - Mensagem de texto: 10 min (agentes têm debounce de ~30s + tempo de IA)
  - Mídia/extrato: 10 min (qualificador tem buffer 30s + análise Gemini ~60s)

Alerta se TTL restante < 5 min (passou mais de 5 min sem resposta).

Rodado a cada 5 minutos pelo APScheduler.
"""
import logging

logger = logging.getLogger(__name__)

_PENDENTE_PREFIX     = "msg:pendente:"
_ALERTA_TTL_RESTANTE = 300   # alerta se TTL restante < 5 min
_MSG_TTL             = 600   # 10 min de janela total
_COOLDOWN_PREFIX     = "msg:alerta_enviado:"
_COOLDOWN_TTL        = 900   # cooldown de 15 min por phone


async def mark_message_pending(phone: str, card_id: str, nome: str, descricao: str) -> None:
    """
    Marca mensagem como pendente de resposta.
    Chamado pelo router ao receber mensagem que espera resposta do sistema.
    Idempotente: se já existe key, não sobrescreve (preserva timestamp original).
    """
    try:
        from services.session_store import get_redis
        rd = await get_redis()
        key = f"{_PENDENTE_PREFIX}{phone}"
        # NX = só grava se não existe (preserva a primeira mensagem do lote)
        await rd.set(key, f"{card_id}|{nome}|{descricao[:80]}", ex=_MSG_TTL, nx=True)
    except Exception as e:
        logger.debug("mark_message_pending(%s): %s", phone[-4:], e)


async def clear_message_pending(phone: str) -> None:
    """
    Remove marcador de pendente — sistema respondeu.
    Chamado pelo WhapiClient ao enviar mensagem para o lead.
    """
    try:
        from services.session_store import get_redis
        rd = await get_redis()
        await rd.delete(f"{_PENDENTE_PREFIX}{phone}")
    except Exception as e:
        logger.debug("clear_message_pending(%s): %s", phone[-4:], e)


async def run_watchdog_extratos() -> None:
    """
    Varre Redis em busca de mensagens recebidas mas não respondidas.
    Alerta equipe via grupo WA + Slack para cada caso encontrado.

    Mantém compatibilidade com o nome original (usado no scheduler).
    """
    try:
        from services.session_store import get_redis
        rd = await get_redis()

        # Varre tanto keys antigas (extrato:pendente:) quanto novas (msg:pendente:)
        all_keys = []
        for prefix in (_PENDENTE_PREFIX, "extrato:pendente:"):
            keys = await rd.keys(f"{prefix}*")
            all_keys.extend([(k, prefix) for k in keys])

        if not all_keys:
            return

        alertas = []
        for key, prefix in all_keys:
            ttl = await rd.ttl(key)
            if ttl < 0:
                continue
            if ttl > _ALERTA_TTL_RESTANTE:
                continue  # ainda dentro da janela normal

            phone = key.replace(prefix, "")
            valor = (await rd.get(key)) or ""
            partes = (valor.split("|") + ["?", "?", ""])[:3]
            card_id, nome, descricao = partes

            # Cooldown: não reenvia alerta para o mesmo phone em 15 min
            cooldown_key = f"{_COOLDOWN_PREFIX}{phone}"
            if await rd.exists(cooldown_key):
                continue

            alertas.append((phone, card_id, nome, descricao, ttl))
            await rd.set(cooldown_key, "1", ex=_COOLDOWN_TTL)

        if not alertas:
            return

        import asyncio
        from services.whapi import notify_team
        from services.slack import slack_alert

        for phone, card_id, nome, descricao, ttl_restante in alertas:
            minutos_preso = int((_MSG_TTL - ttl_restante) / 60)
            tipo = "extrato" if descricao in ("", "extrato") or not descricao else f"mensagem: \"{descricao[:40]}\""
            msg = (
                f"⚠️ *Mensagem sem resposta — ação necessária*\n"
                f"Lead: *{nome}* (`...{phone[-4:]}`)\n"
                f"Card: `{card_id[:8]}`\n"
                f"Tipo: {tipo}\n"
                f"Sem resposta do sistema há ~{minutos_preso} min.\n"
                f"Verificar e responder manualmente."
            )
            logger.warning(
                "Watchdog: mensagem sem resposta há %d min — card=%s phone=...%s tipo=%s",
                minutos_preso, card_id[:8], phone[-4:], tipo,
            )
            try:
                asyncio.ensure_future(notify_team(msg))
                asyncio.ensure_future(slack_alert(msg, level="warning"))
            except Exception as e:
                logger.error("Watchdog: falha ao enviar alerta: %s", e)

    except Exception as e:
        logger.error("Watchdog mensagens: erro na varredura: %s", e)
