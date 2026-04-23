"""
jobs/ativacao_bazar_site.py — Ativação de leads orgânicos (Bazar do Consórcio e Site/LP)
Provider: Whapi canal "bazar" (substituiu Z-API)
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

from config import (
    Stage, SEND_WINDOW_START, SEND_WINDOW_END, JOB_BATCH_LIMIT,
    TEST_MODE, TZ_BRASILIA, filter_test_cards,
    ATIVACAO_ADM_EXCLUSOES, ATIVACAO_CONTEMPLACAO_EXCLUSOES, ATIVACAO_TIPO_BEM_EXCLUSOES,
)
from services.faro import FaroClient, FaroError, get_phone, get_name, get_adm
from services.whapi import WhapiClient, WhapiError

logger = logging.getLogger(__name__)

MSG_BAZAR = (
    "Olá {nome}, tudo bem? Sou a Manuela, da Consórcio Sorteado, "
    "empresa referência na compra de cotas contempladas.\n\n"
    "Recebemos seu interesse através da Bazar do Consórcio e temos "
    "interesse real na sua cota {adm}! 😁\n\n"
    "O processo é simples e rápido:\n"
    "1️⃣ Você envia o extrato atualizado da cota\n"
    "2️⃣ Nossa equipe faz a análise\n"
    "3️⃣ Você recebe uma proposta em até 24h\n\n"
    "Pode me enviar o extrato da sua cota {adm}?"
)

MSG_SITE = (
    "Olá {nome}, tudo bem? Sou a Manuela, da Consórcio Sorteado, "
    "empresa referência na compra de cotas contempladas.\n\n"
    "Recebemos seu interesse através do nosso site e temos "
    "interesse real na sua cota {adm}! 😁\n\n"
    "O processo é simples e rápido:\n"
    "1️⃣ Você envia o extrato atualizado da cota\n"
    "2️⃣ Nossa equipe faz a análise\n"
    "3️⃣ Você recebe uma proposta em até 24h\n\n"
    "Pode me enviar o extrato da sua cota {adm}?"
)


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _skip_reason(card: dict) -> str | None:
    adm = (card.get("Adm") or "").lower()
    tipo_cont = (card.get("Tipo contemplação") or "").lower()
    tipo_bem = (card.get("Tipo de bem") or "").lower()
    for excl in ATIVACAO_ADM_EXCLUSOES:
        if excl in adm:
            return f"adm excluída ({excl})"
    for excl in ATIVACAO_CONTEMPLACAO_EXCLUSOES:
        if excl in tipo_cont:
            return f"tipo contemplação excluído ({excl})"
    for excl in ATIVACAO_TIPO_BEM_EXCLUSOES:
        if excl in tipo_bem:
            return f"tipo de bem excluído ({excl})"
    return None


async def _activate_card(card: dict, message_template: str, faro: FaroClient) -> bool:
    card_id = card["id"]
    reason = _skip_reason(card)
    if reason:
        logger.info("Card %s ignorado: %s — movendo para Não Qualificado", card_id[:8], reason)
        try:
            await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
        except FaroError:
            pass
        return False

    phone = get_phone(card)
    if not phone:
        logger.warning("Card %s sem telefone, movendo para Não Qualificado", card_id[:8])
        try:
            await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
        except FaroError:
            pass
        return False

    nome = get_name(card)
    adm = get_adm(card)
    message = message_template.format(nome=nome, adm=adm)

    if not TEST_MODE:
        await asyncio.sleep(random.randint(5, 50))

    try:
        # Whapi canal "bazar" — substitui Z-API
        async with WhapiClient(canal="bazar") as w:
            await w.send_text(phone, message)

        await faro.move_card(card_id, Stage.PRIMEIRA_ATIVACAO)
        await faro.update_card(card_id, {
            "Data de primeira ativação": datetime.now(timezone.utc).strftime("%d/%m/%Y"),
            "Ultima atividade": str(int(datetime.now(timezone.utc).timestamp())),
        })
        logger.info("Whapi bazar OK: card=%s phone=%s", card_id[:8], phone[-4:])
        return True

    except WhapiError as e:
        logger.error("Erro Whapi bazar card %s: %s", card_id[:8], e)
        return False
    except FaroError as e:
        logger.error("Erro FARO card %s: %s", card_id[:8], e)
        return False


async def run_ativacao_bazar():
    if not _is_within_send_window():
        return
    logger.info("=== Ativação Bazar ===")
    async with FaroClient() as faro:
        try:
            cards = await faro.watch_recent(stage_id=Stage.BAZAR, hours=168, limit=JOB_BATCH_LIMIT)
        except FaroError as e:
            logger.error("Erro buscando cards Bazar: %s", e)
            return
        if not cards:
            return
        cards = filter_test_cards(cards)
        if not cards:
            return
        logger.info("%d cards para ativar (Bazar)", len(cards))
        ok = sum([await _activate_card(card, MSG_BAZAR, faro) for card in cards])
        logger.info("Bazar: %d/%d ativados", ok, len(cards))


async def run_ativacao_site():
    if not _is_within_send_window():
        return
    logger.info("=== Ativação Site/LP ===")
    async with FaroClient() as faro:
        try:
            cards = await faro.watch_recent(stage_id=Stage.LP, hours=168, limit=JOB_BATCH_LIMIT)
        except FaroError as e:
            logger.error("Erro buscando cards Site/LP: %s", e)
            return
        if not cards:
            return
        cards = filter_test_cards(cards)
        if not cards:
            return
        logger.info("%d cards para ativar (Site/LP)", len(cards))
        ok = sum([await _activate_card(card, MSG_SITE, faro) for card in cards])
        logger.info("Site/LP: %d/%d ativados", ok, len(cards))
