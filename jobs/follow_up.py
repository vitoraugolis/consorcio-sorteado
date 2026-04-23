"""
jobs/follow_up.py — Follow-up após envio de proposta
Provider: Whapi (get_whapi_for_card — substitui Z-API)
"""

import logging
import time
from datetime import datetime, timezone

from config import (
    Stage, SEND_WINDOW_START, SEND_WINDOW_END,
    JOB_BATCH_LIMIT, NOTIFY_PHONES, TEST_MODE, TZ_BRASILIA, filter_test_cards,
)
from services.faro import (
    FaroClient, FaroError,
    get_phone, get_name, get_adm, is_lista,
    load_history, build_card_context,
    load_journey, journey_to_text,
)
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card
from services.ai import AIClient, AIError

logger = logging.getLogger(__name__)

MAX_FOLLOW_UPS = 8
MIN_INTERVAL_S = 25 * 60


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _should_followup(card: dict) -> bool:
    num_fups = int(card.get("Num Follow Ups") or "0")
    if num_fups >= MAX_FOLLOW_UPS:
        return False
    ultima = card.get("Ultima atividade") or ""
    if not ultima:
        return True
    try:
        if str(ultima).isdigit():
            ts = int(ultima)
        else:
            dt = datetime.fromisoformat(ultima.replace("Z", "+00:00"))
            ts = int(dt.timestamp())
        return (time.time() - ts) >= MIN_INTERVAL_S
    except (ValueError, TypeError):
        return True


_FOLLOWUP_SYSTEM = """
Você é Manuela, consultora da Consórcio Sorteado. Fazendo follow-up com lead que parou de responder.
Tom caloroso, natural. Máximo 3 linhas. Nunca revele ser IA.
""".strip()

_FOLLOWUP_PROMPT = """
DADOS: {dados_card}
JORNADA: {jornada}
SITUAÇÃO: {situacao}
TENTATIVA: {num} de {max_fups}
SAUDAÇÃO: {saudacao}
ÚLTIMA TENTATIVA: {ultima_tentativa}
HISTÓRICO RECENTE: {historico}

Gere UMA mensagem de follow-up curta (máx 3 linhas). Apenas o texto, sem aspas.
""".strip()

_SITUACAO_LABEL: dict[str, str] = {
    "MELHORAR_VALOR": "lead pediu melhora de valor — nova proposta maior foi enviada",
    "CONTRA_PROPOSTA": "lead fez contraproposta — nova proposta foi enviada em resposta",
    "OFERECERAM_MAIS": "concorrente ofereceu mais — nova proposta foi enviada para igualar",
    "NEGOCIAR": "lead pediu negociação — nova proposta melhorada foi enviada",
    "RECUSAR": "lead recusou — nova proposta escalada foi enviada",
    "DUVIDA": "lead tinha dúvida sobre o processo — foi respondido",
    "DESCONFIANCA": "lead demonstrou desconfiança — credenciais apresentadas",
    "AGENDAR": "lead pediu falar com consultor — handoff iniciado",
    "ACEITAR": "lead aceitou — processo em andamento",
    "OUTRO": "conversa em andamento",
}

_PRICE_ESCALATION_INTENTS = {"MELHORAR_VALOR", "CONTRA_PROPOSTA", "OFERECERAM_MAIS", "NEGOCIAR", "RECUSAR"}


async def _generate_followup_message(ai: AIClient, card: dict, hora: int) -> str:
    nome = get_name(card)
    num_fups = int(card.get("Num Follow Ups") or "0") + 1
    saudacao = "Bom dia" if hora < 12 else ("Boa tarde" if hora < 18 else "Boa noite")
    is_last = num_fups >= MAX_FOLLOW_UPS
    history = load_history(card)
    situacao_raw = (card.get("Situacao Negociacao") or "").strip().upper()
    situacao_desc = _SITUACAO_LABEL.get(situacao_raw, "proposta enviada, aguardando resposta")
    historico_txt = "\n".join(
        f"{'Lead' if t['role'] == 'user' else 'Manuela'}: {t['content'][:120]}"
        for t in history[-4:]
    ) if history else "(sem histórico)"
    journey = load_journey(card)
    jornada_txt = journey_to_text(journey)
    prompt = _FOLLOWUP_PROMPT.format(
        dados_card=build_card_context(card),
        jornada=jornada_txt,
        situacao=situacao_desc,
        num=num_fups, max_fups=MAX_FOLLOW_UPS,
        saudacao=saudacao,
        ultima_tentativa="Sim — despedida gentil" if is_last else "Não",
        historico=historico_txt,
    )
    try:
        msg = await ai.complete(prompt=prompt, system=_FOLLOWUP_SYSTEM, max_tokens=120)
        return msg.strip()
    except AIError as e:
        logger.warning("Follow-up IA falhou para %s: %s", card.get("id", "")[:8], e)
    if is_last:
        return (f"{saudacao}, {nome}! Passando para dar um último alô. "
                f"Se mudar de ideia, é só me chamar. Abraço! 😊")
    import random as _r
    if situacao_raw in _PRICE_ESCALATION_INTENTS:
        return _r.choice([
            f"{saudacao}, {nome}! Passou pela nova proposta que enviei? 😊",
            f"{nome}, a oferta melhorada ainda está válida — qualquer dúvida me chama!",
        ])
    return _r.choice([
        f"{saudacao}, {nome}! Já teve chance de ver a proposta? 😊",
        f"{nome}, alguma dúvida sobre a proposta? Estou aqui!",
    ])


async def _send_followup(card: dict, message: str) -> bool:
    phone = get_phone(card)
    if not phone:
        return False
    try:
        async with get_whapi_for_card(card) as w:
            await w.send_text(phone, message)
        return True
    except WhapiError as e:
        logger.error("Erro Whapi follow-up card %s: %s", card["id"][:8], e)
        return False


async def _followup_assinatura_parados(faro: FaroClient) -> None:
    """Verifica leads de lista em ASSINATURA (sem ZapSign Token) parados por 3+ dias."""
    try:
        cards = await faro.get_cards_all_pages(stage_id=Stage.ASSINATURA, page_size=50)
    except FaroError as e:
        logger.error("Follow-up ASSINATURA: erro ao buscar cards: %s", e)
        return
    if not cards:
        return

    _ASSINATURA_PARADO_DIAS = 3
    _ASSINATURA_MAX_LEMBRETES = 3
    limiar_s = _ASSINATURA_PARADO_DIAS * 24 * 3600
    agora = time.time()

    for card in cards:
        if not is_lista(card) or card.get("ZapSign Token"):
            continue
        ultima = card.get("Ultima atividade") or ""
        if not ultima:
            continue
        try:
            ts = int(ultima) if str(ultima).isdigit() else int(
                datetime.fromisoformat(ultima.replace("Z", "+00:00")).timestamp()
            )
        except (ValueError, TypeError):
            continue
        if (agora - ts) < limiar_s:
            continue

        card_id = card["id"]
        nome = get_name(card)
        phone = get_phone(card)
        adm = get_adm(card)
        num_lembretes = int(card.get("Num Follow Ups Assinatura") or "0")

        if num_lembretes >= _ASSINATURA_MAX_LEMBRETES:
            if NOTIFY_PHONES and num_lembretes == _ASSINATURA_MAX_LEMBRETES:
                notif = (f"⏸️ *Lead parado em ASSINATURA*\n"
                         f"Nome: {nome} | Adm: {adm}\n"
                         f"Sem resposta após {num_lembretes} lembretes. Intervenção manual recomendada.")
                try:
                    async with WhapiClient(canal="lista") as w:
                        for ph in NOTIFY_PHONES:
                            await w.send_text(ph, notif)
                except WhapiError:
                    pass
            try:
                await faro.update_card(card_id, {"Num Follow Ups Assinatura": str(num_lembretes + 1)})
            except FaroError:
                pass
            continue

        if not phone:
            continue

        from webhooks.agente_contrato import _REQUIRED_FIELDS, _load_collected, _FIELD_LABELS
        collected = _load_collected(card)
        missing = [f for f in _REQUIRED_FIELDS if not collected.get(f)]
        if not missing:
            bot_msg = (f"Oi, {nome}! 😊 Só passando para lembrar que já tenho seus dados pessoais, "
                       f"mas ainda aguardo o *extrato detalhado* da sua cota {adm}. "
                       f"Pode enviar uma foto ou PDF por aqui mesmo! 📄")
        else:
            bot_msg = (f"Oi, {nome}! 😊 Notei que ainda precisamos de algumas informações:\n\n"
                       + "\n".join(f"• *{_FIELD_LABELS[f]}*" for f in missing)
                       + f"\n\nAssim que me enviar, dou andamento imediato! 📋")
        try:
            async with WhapiClient(canal="lista") as w:
                await w.send_text(phone, bot_msg)
            await faro.update_card(card_id, {
                "Num Follow Ups Assinatura": str(num_lembretes + 1),
                "Ultima atividade": str(int(agora)),
            })
            logger.info("Follow-up ASSINATURA #%d: card=%s", num_lembretes + 1, card_id[:8])
        except Exception as e:
            logger.error("Follow-up ASSINATURA: erro card %s: %s", card_id[:8], e)


async def run_follow_up():
    if not _is_within_send_window():
        logger.info("Follow-up: fora da janela de envio, pulando.")
        return
    logger.info("=== Iniciando Follow-up ===")
    hora_atual = (datetime.now(timezone.utc).hour - 3) % 24

    async with FaroClient() as faro, AIClient() as ai:
        try:
            cards = await faro.get_cards_all_pages(stage_id=Stage.EM_NEGOCIACAO, page_size=100)
        except FaroError as e:
            logger.error("Erro buscando cards EM_NEGOCIACAO: %s", e)
            return
        if not cards:
            logger.info("Nenhum card em EM_NEGOCIACAO.")
            return

        cards = filter_test_cards(cards)

        # Move esgotados para PERDIDO
        esgotados = [c for c in cards if int(c.get("Num Follow Ups") or "0") >= MAX_FOLLOW_UPS]
        for card in esgotados:
            card_id = card["id"]
            try:
                await faro.move_card(card_id, Stage.PERDIDO)
                logger.info("Follow-up: card %s esgotou %d tentativas → PERDIDO", card_id[:8], MAX_FOLLOW_UPS)
                if NOTIFY_PHONES:
                    notif = (f"📭 *Lead esgotou follow-ups*\nNome: {get_name(card)}\n"
                             f"Adm: {get_adm(card)}\nTentativas: {MAX_FOLLOW_UPS} → PERDIDO.")
                    try:
                        async with WhapiClient(canal="lista") as w:
                            for ph in NOTIFY_PHONES:
                                await w.send_text(ph, notif)
                    except WhapiError:
                        pass
            except FaroError as e:
                logger.error("Follow-up: erro ao mover esgotado %s: %s", card_id[:8], e)

        pendentes = [c for c in cards if _should_followup(c)][:JOB_BATCH_LIMIT]
        if not pendentes:
            logger.info("Follow-up: nenhum card elegível.")
            return

        logger.info("%d cards para follow-up", len(pendentes))
        total_ok = 0
        for card in pendentes:
            followup_msg = await _generate_followup_message(ai, card, hora_atual)
            success = await _send_followup(card, followup_msg)
            if success:
                total_ok += 1
                num_fups = int(card.get("Num Follow Ups") or "0") + 1
                try:
                    await faro.update_card(card["id"], {
                        "Num Follow Ups": str(num_fups),
                        "Ultima atividade": str(int(time.time())),
                    })
                except FaroError:
                    pass
                logger.info("Follow-up #%d OK: card=%s", num_fups, card["id"][:8])

        logger.info("=== Follow-up concluído: %d/%d ===", total_ok, len(pendentes))
        await _followup_assinatura_parados(faro)
