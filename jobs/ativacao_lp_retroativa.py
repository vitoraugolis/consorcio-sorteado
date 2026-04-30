"""
jobs/ativacao_lp_retroativa.py — Ativação retroativa de leads LP (sorteio + lance)

Dispara 1 lead a cada 20 min, ordem cronológica inversa (mais recentes primeiro).
- Sorteio: pede extrato
- Lance: explica limitação de ágio + convida pro grupo
Após envio, move para stage ESPERA.

Controlado via endpoint /jobs/lp-retro/start e /jobs/lp-retro/status.
"""

import asyncio
import logging
from datetime import datetime, timezone

from config import Stage
from services.faro import FaroClient, get_phone, get_name, get_adm
from services.whapi import WhapiClient, WhapiError
from services.slack import slack_info as notify_team

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

MSG_SORTEIO_EXTRATO = (
    "Olá, {nome}! 😊\n\n"
    "Temos interesse em comprar sua cota do *{adm}*!\n\n"
    "Para te passar a melhor proposta possível, preciso do seu *extrato atualizado* do consórcio "
    "(pode ser PDF ou foto do app/site da administradora).\n\n"
    "Pode me enviar aqui? 📄🚀"
)

MSG_LANCE = (
    "Olá, {nome}! Recebemos seu interesse em vender seu consórcio *{adm}*. "
    "No entanto, pelo formulário você nos disse que sua contemplação foi por lance, certo?\n\n"
    "Quando é assim, na maioria das vezes não conseguimos dar uma proposta com ágio "
    "(lucro em relação ao que você já pagou).\n\n"
    "Caso ainda assim você queira receber uma proposta, é só enviar o seu *extrato completo* aqui.\n\n"
    "Caso não, quero te convidar para o nosso grupo de novidades de Consórcio — "
    "sempre colocaremos as melhores oportunidades por lá 👇\n"
    "{link}"
)

MSG_NAO_CONTEMPLADA = (
    "Olá, {nome}! Tudo bem? 😊\n\n"
    "Recebemos seu interesse em vender sua cota *{adm}*, obrigado!\n\n"
    "No entanto, pelo formulário você nos informou que sua cota ainda *não está contemplada*. "
    "Nós compramos apenas cotas já contempladas (por sorteio ou lance).\n\n"
    "Assim que sua cota for contemplada, ficamos à disposição para fazer uma proposta! "
    "Enquanto isso, te convido para o nosso grupo de novidades — sempre colocaremos as "
    "melhores oportunidades por lá 👇\n"
    "{link}"
)

# Estado da fila (em memória — persiste enquanto servidor estiver de pé)
_state: dict = {
    "running": False,
    "queue": [],
    "done": [],
    "current_idx": 0,
    "task": None,
    "started_at": None,
    "last_sent_at": None,
    "interval_min": 15,
    "interval_max": 20,
}


def get_status() -> dict:
    return {
        "running": _state["running"],
        "total": len(_state["queue"]),
        "done": len(_state["done"]),
        "remaining": len(_state["queue"]) - _state["current_idx"],
        "current_idx": _state["current_idx"],
        "started_at": _state["started_at"],
        "last_sent_at": _state["last_sent_at"],
        "next_in_queue": _state["queue"][_state["current_idx"]] if _state["current_idx"] < len(_state["queue"]) else None,
    }


async def _build_queue() -> list[dict]:
    """Monta fila de todos os LP (sorteio + lance) em stages ativas, ordem desc updated_at."""
    from config import Stage

    STAGES_VARRER = [
        Stage.LP,
        Stage.PRIMEIRA_ATIVACAO,
        Stage.SEGUNDA_ATIVACAO,
        Stage.TERCEIRA_ATIVACAO,
        Stage.QUARTA_ATIVACAO,
        Stage.LP_LANCE,
    ]

    todos: list[dict] = []
    async with FaroClient() as faro:
        for stage_id in STAGES_VARRER:
            try:
                cards = await asyncio.wait_for(
                    faro.watch_recent(stage_id=stage_id, hours=8760, limit=200),
                    timeout=12,
                )
                lp = [
                    c for c in cards
                    if "lp" in str(c.get("Fonte") or "").lower()
                    or "site" in str(c.get("Fonte") or "").lower()
                ]
                todos.extend(lp)
            except Exception as e:
                logger.warning("_build_queue: erro na stage %s: %s", stage_id[:8], e)

    # Deduplica por id
    seen: set[str] = set()
    unique: list[dict] = []
    for c in todos:
        cid = c.get("id", "")
        if cid and cid not in seen:
            seen.add(cid)
            unique.append(c)

    # Ordena desc updated_at (mais quentes primeiro)
    unique.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or "", reverse=True)

    # Classifica tipo
    queue = []
    for c in unique:
        contemp = str(c.get("Tipo contemplação") or "").lower()
        if "sorteio" in contemp:
            tipo = "sorteio"
        elif "lance" in contemp:
            tipo = "lance"
        elif "nao-contemplada" in contemp or "não contemplada" in contemp:
            tipo = "nao_contemplada"
        else:
            tipo = "sorteio"  # sem info → pede extrato por precaução
        queue.append({
            "id":        c["id"],
            "nome":      get_name(c),
            "adm":       get_adm(c),
            "phone":     get_phone(c),
            "phone_alt": "".join(d for d in str(c.get("Telefone alternativo") or "") if d.isdigit()) or None,
            "tipo":      tipo,
            "stage_atual": c.get("stage_id", ""),
            "updated_at": str(c.get("updated_at", ""))[:10],
        })

    return queue


async def _dispatch_one(item: dict) -> bool:
    """Envia mensagem para um lead e move para PRIMEIRA_ATIVACAO. Retorna True se OK."""
    from services.whapi import resolve_phone
    from services.faro import FaroClient, FaroError

    card_id = item["id"]
    nome    = item["nome"].split()[0] if item["nome"] else "você"
    adm     = item["adm"]
    tipo    = item["tipo"]

    if not item["phone"] and not item.get("phone_alt"):
        logger.warning("LP retro: card %s sem telefone — pulando", card_id[:8])
        return False

    if tipo == "sorteio":
        msg = MSG_SORTEIO_EXTRATO.format(nome=nome, adm=adm)
    elif tipo == "lance":
        msg = MSG_LANCE.format(nome=nome, adm=adm, link=_GROUP_LINK)
    elif tipo == "nao_contemplada":
        msg = MSG_NAO_CONTEMPLADA.format(nome=nome, adm=adm, link=_GROUP_LINK)
    else:
        logger.info("LP retro: card %s tipo '%s' desconhecido — pulando", card_id[:8], tipo)
        return False

    # Monta card mínimo para resolve_phone
    card_stub = {
        "id":                  card_id,
        "Telefone":            item["phone"] or "",
        "Telefone alternativo": item.get("phone_alt") or "",
    }
    phone = await resolve_phone(card_stub, canal="lp")
    if not phone:
        logger.warning("LP retro: card %s — nenhum número com WA, pulando", card_id[:8])
        return False

    try:
        async with WhapiClient(canal="lp") as w:
            await w.send_text(phone, msg, _log_nome=item["nome"], _log_card_id=card_id)

        # nao_contemplada → PERDIDO (não vão enviar extrato)
        # sorteio/lance → PRIMEIRA_ATIVACAO (aguarda resposta normal; extrato move para ESPERA)
        stage_destino = Stage.PERDIDO if tipo == "nao_contemplada" else Stage.PRIMEIRA_ATIVACAO
        motivo = "nao-contemplada — convidado para o grupo" if tipo == "nao_contemplada" else f"lp-retro-{tipo}"

        async with FaroClient() as faro:
            await faro.move_card(card_id, stage_destino)
            update: dict = {
                "Ultima atividade": datetime.now(timezone.utc).isoformat(),
                "Situacao Negociacao": motivo,
                "Data de primeira ativação": datetime.now(timezone.utc).strftime("%d/%m/%Y"),
            }
            if tipo == "nao_contemplada":
                update["Motivo de perda"] = "Cota não contemplada — convidado para o grupo"
            await faro.update_card(card_id, update)

        logger.info("LP retro: ✅ %s (%s) | %s | phone=%s → %s",
                    item["nome"], card_id[:8], tipo, phone[-4:],
                    "PERDIDO" if tipo == "nao_contemplada" else "PRIMEIRA_ATIVACAO")
        return True

    except WhapiError as e:
        logger.error("LP retro: ❌ Whapi card %s: %s", card_id[:8], e)
        return False
    except Exception as e:
        logger.error("LP retro: ❌ card %s: %s", card_id[:8], e)
        return False


async def _run_loop() -> None:
    """Loop principal: processa itens, dorme apenas quando efetivamente enviou."""
    import random

    while _state["running"] and _state["current_idx"] < len(_state["queue"]):
        item = _state["queue"][_state["current_idx"]]
        logger.info(
            "LP retro [%d/%d]: %s (%s) | tipo=%s",
            _state["current_idx"] + 1, len(_state["queue"]),
            item["nome"], item["id"][:8], item["tipo"],
        )

        ok = await _dispatch_one(item)
        _state["done"].append({**item, "ok": ok, "sent_at": datetime.now(timezone.utc).isoformat()})
        _state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
        _state["current_idx"] += 1

        remaining = len(_state["queue"]) - _state["current_idx"]

        # Notifica Slack a cada 5 enviados com sucesso ou no último
        successes = sum(1 for d in _state["done"] if d["ok"])
        if successes > 0 and (successes % 5 == 0 or remaining == 0):
            try:
                await notify_team(
                    f"📊 *LP Retroativa* — {_state['current_idx']}/{len(_state['queue'])} processados "
                    f"({successes} enviados) | Restam: {remaining}"
                )
            except Exception:
                pass

        if remaining == 0:
            break

        # Só dorme se enviou com sucesso — se pulou, avança imediatamente para o próximo
        if ok:
            min_s = _state["interval_min"] * 60
            max_s = _state["interval_max"] * 60
            wait_s = random.randint(min_s, max_s)
            logger.info("LP retro: ✉️  enviado — próximo disparo em %ds (~%.1fmin)...", wait_s, wait_s / 60)
            await asyncio.sleep(wait_s)
        else:
            logger.info("LP retro: ⏩ pulado — avançando para o próximo imediatamente")

    _state["running"] = False
    logger.info("LP retro: fila concluída. Total: %d | OK: %d",
                len(_state["done"]), sum(1 for d in _state["done"] if d["ok"]))
    try:
        await notify_team(
            f"✅ *LP Retroativa concluída!* {len(_state['done'])} leads processados "
            f"({sum(1 for d in _state['done'] if d['ok'])} enviados com sucesso)."
        )
    except Exception:
        pass


async def start(interval_min: int = 15, interval_max: int = 20, resume_from: int = 0) -> dict:
    """Inicia (ou retoma) a fila retroativa. interval_min/max em minutos."""
    if _state["running"]:
        return {"status": "already_running", **get_status()}

    # Se retomando e já tem fila montada, usa a existente
    if resume_from > 0 and _state["queue"]:
        _state.update({
            "running": True,
            "current_idx": resume_from,
            "interval_min": interval_min,
            "interval_max": interval_max,
        })
        logger.info("LP retro: retomando do índice %d (intervalo %d-%dmin)", resume_from, interval_min, interval_max)
    else:
        logger.info("LP retro: montando fila...")
        queue = await _build_queue()
        if not queue:
            return {"status": "empty_queue", "total": 0}
        _state.update({
            "running": True,
            "queue": queue,
            "done": _state.get("done", []),
            "current_idx": resume_from,
            "started_at": _state.get("started_at") or datetime.now(timezone.utc).isoformat(),
            "last_sent_at": _state.get("last_sent_at"),
            "interval_min": interval_min,
            "interval_max": interval_max,
        })

    loop = asyncio.get_event_loop()
    task = loop.create_task(_run_loop())
    _state["task"] = task

    logger.info(
        "LP retro: iniciado. %d leads na fila, índice=%d, intervalo=%d-%dmin",
        len(_state["queue"]), _state["current_idx"], interval_min, interval_max,
    )
    return {"status": "started", **get_status()}


def stop() -> dict:
    """Para a fila após o disparo atual."""
    _state["running"] = False
    return {"status": "stopping", **get_status()}
