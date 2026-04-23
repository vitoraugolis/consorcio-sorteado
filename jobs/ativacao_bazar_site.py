"""
jobs/ativacao_bazar_site.py — Ativação de leads orgânicos (Bazar do Consórcio e Site/LP)

Lógica:
  1. Busca cards recentes nas etapas "Bazar" e "LP" (leads que chegaram organicamente)
  2. Envia mensagem de apresentação via Z-API
  3. Move para "Primeira ativação"

Diferença das Listas: volume é baixo e intermitente (orgânico), por isso:
  - Não precisa de delay tão alto entre cards
  - Mensagem é personalizada para a fonte (Bazar vs Site)
  - Provider: Z-API (instância por etiqueta/administradora)

Frequência sugerida: a cada 5 minutos (leads orgânicos precisam de resposta rápida)
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

from config import (
    Stage,
    SEND_WINDOW_START,
    SEND_WINDOW_END,
    JOB_BATCH_LIMIT,
    TEST_MODE,
    TZ_BRASILIA,
    filter_test_cards,
    ATIVACAO_ADM_EXCLUSOES,
    ATIVACAO_CONTEMPLACAO_EXCLUSOES,
    ATIVACAO_TIPO_BEM_EXCLUSOES,
)
from services.faro import FaroClient, FaroError, get_phone, get_name, get_adm
from services.zapi import ZAPIClient, ZAPIError, get_zapi_for_card

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------

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

# Mensagem enviada quando a cota não é comprável (valor pago > teto)
MSG_NAO_QUALIFICADO = (
    "Olá {nome}, tudo bem?\n\n"
    "Agradeço por ter enviado as informações sobre a sua cota {adm} "
    "e pelo seu interesse em negociar conosco.\n\n"
    "No momento, após uma análise preliminar, infelizmente não conseguimos "
    "prosseguir com a compra dessa cota. O valor pago até agora excede "
    "o nosso teto de aquisição.\n\n"
    "Caso sua situação mude ou queira tentar novamente no futuro, "
    "pode nos chamar! 😊"
)


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _skip_reason(card: dict) -> str | None:
    """
    Retorna o motivo de pular o card, ou None se deve ser ativado.
    Replica os filtros do Make: Adm excluída, tipo de contemplação ou tipo de bem inválido.
    """
    adm       = (card.get("Adm") or "").lower()
    tipo_cont = (card.get("Tipo contemplação") or "").lower()
    tipo_bem  = (card.get("Tipo de bem") or "").lower()

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


async def _activate_card(
    card: dict,
    message_template: str,
    faro: FaroClient,
) -> bool:
    """Ativa um card de Bazar ou Site. Retorna True se mensagem enviada."""
    card_id = card["id"]

    # Filtros de exclusão (replicam os do Make)
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

    # Delay anti-ban (replica o Sleep 5–50s do Make)
    if not TEST_MODE:
        await asyncio.sleep(random.randint(5, 50))

    try:
        zapi = get_zapi_for_card(card)
        async with zapi:
            await zapi.send_text(phone, message)

        await faro.move_card(card_id, Stage.PRIMEIRA_ATIVACAO)
        await faro.update_card(card_id, {
            "Data de primeira ativação": datetime.now(timezone.utc).strftime("%d/%m/%Y"),
            "Ultima atividade": str(int(datetime.now(timezone.utc).timestamp())),
        })

        logger.info("Z-API OK [bazar/site]: card=%s phone=%s", card_id[:8], phone[-4:])
        return True

    except ZAPIError as e:
        logger.error("Erro Z-API card %s: %s", card_id[:8], e)
        return False
    except FaroError as e:
        logger.error("Erro FARO card %s: %s", card_id[:8], e)
        return False


async def run_ativacao_bazar():
    """Ativa leads recentes da etapa Bazar."""
    if not _is_within_send_window():
        return

    logger.info("=== Ativação Bazar ===")
    async with FaroClient() as faro:
        try:
            # watch_recent: leads que chegaram nas últimas 168h (1 semana)
            # Garante que nenhum lead fique sem atendimento mesmo se o job ficou parado
            cards = await faro.watch_recent(
                stage_id=Stage.BAZAR,
                hours=168,
                limit=JOB_BATCH_LIMIT,
            )
        except FaroError as e:
            logger.error("Erro buscando cards Bazar: %s", e)
            return

        if not cards:
            return

        cards = filter_test_cards(cards)
        if TEST_MODE:
            logger.info("TEST_MODE ativo: %d card(s) após filtro de teste.", len(cards))
        if not cards:
            return

        logger.info("%d cards para ativar (Bazar)", len(cards))
        ok = sum([
            await _activate_card(card, MSG_BAZAR, faro)
            for card in cards
        ])
        logger.info("Bazar: %d/%d ativados", ok, len(cards))


async def run_ativacao_site():
    """Ativa leads recentes da etapa LP (Site)."""
    if not _is_within_send_window():
        return

    logger.info("=== Ativação Site/LP ===")
    async with FaroClient() as faro:
        try:
            cards = await faro.watch_recent(
                stage_id=Stage.LP,
                hours=168,
                limit=JOB_BATCH_LIMIT,
            )
        except FaroError as e:
            logger.error("Erro buscando cards Site/LP: %s", e)
            return

        if not cards:
            return

        cards = filter_test_cards(cards)
        if TEST_MODE:
            logger.info("TEST_MODE ativo: %d card(s) após filtro de teste.", len(cards))
        if not cards:
            return

        logger.info("%d cards para ativar (Site/LP)", len(cards))
        ok = sum([
            await _activate_card(card, MSG_SITE, faro)
            for card in cards
        ])
        logger.info("Site/LP: %d/%d ativados", ok, len(cards))
