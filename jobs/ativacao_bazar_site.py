"""
jobs/ativacao_bazar_site.py — Ativação de leads orgânicos (Bazar do Consórcio e Site/LP)
Provider: Whapi canal "bazar" (substituiu Z-API)

Filtro de qualificação por fonte:
  BAZAR — administradora na lista Bazar E (Situação == "contemplada-sorteio" OU vazia)
  LP    — administradora na lista LP  E Tipo contemplação == "contemplada-sorteio"

Leads não qualificados recebem mensagem de agradecimento + link do grupo CS
e são movidos para Não Qualificado (sem follow-up posterior).

Listas de administradoras usam match por substring normalizado (sem acento, lowercase)
para tolerar variações de grafia ("Itau", "ITAÚ", "Itaú", etc.).
Para editar as listas, altere ADM_BAZAR_TOKENS / ADM_LP_EXTRA_TOKENS abaixo.
"""

import asyncio
import logging
import re
import unicodedata
import random
from datetime import datetime, timezone

from config import (
    Stage, SEND_WINDOW_START, SEND_WINDOW_END, JOB_BATCH_LIMIT,
    TEST_MODE, TZ_BRASILIA, filter_test_cards,
)
from services.faro import FaroClient, FaroError, get_phone, get_name, get_adm
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Listas de administradoras aceitas
# Cada entrada é uma lista de tokens alternativos (match por substring normalizado).
# Adicione/remova entradas aqui sem precisar mexer na lógica.
# ---------------------------------------------------------------------------

ADM_BAZAR_TOKENS: list[list[str]] = [
    ["porto"],                          # Porto Seguro, Porto Bank, PortoBank, Porto Vale...
    ["bradesco"],
    ["santander"],
    ["itau"],                           # itau, itaú, ITAU, ITAÚ (acento removido na normalização)
    ["caixa", "cef"],                   # Caixa, CEF, Caixa Econômica Federal...
    ["mycon", "coimex"],                # Mycon, Coimex, MyCon (Coiemx)...
    ["sicoob"],
    ["embracon"],
    ["ademicon", "ademicom", "admicon"],  # variantes de grafia
]

# Tokens exclusivos do LP (somam-se ao Bazar)
ADM_LP_EXTRA_TOKENS: list[list[str]] = [
    ["banco do brasil", "bb consorcio", "bb consorcios", "bbrasil", "banco brasil", " bb "],
    ["rodobens"],
    ["disal"],
    ["mapfre"],
    ["hs consorcio", "hs consorcios", "hs "],  # HS Consórcio, HS consórcios...
]

# Siglas exatas para LP (evitam false positives em Bazar)
_LP_EXACT_SIGLAS: set[str] = {"bb", "hs"}

ADM_LP_TOKENS: list[list[str]] = ADM_BAZAR_TOKENS + ADM_LP_EXTRA_TOKENS

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

MSG_NAO_QUALIFICADO = (
    "Olá {nome}, tudo bem? Aqui é a Manuela, da Consórcio Sorteado.\n\n"
    "Obrigada pelo seu contato! Analisamos os dados da sua cota e, "
    "no momento, ela não se enquadra no perfil de cotas que compramos — "
    "trabalhamos exclusivamente com cotas contempladas por sorteio de "
    "administradoras parceiras.\n\n"
    "Não se preocupe! Caso sua situação mude futuramente, ficaremos "
    "felizes em conversar.\n\n"
    "Enquanto isso, te convido a participar do nosso grupo gratuito de "
    "informações sobre consórcios contemplados — lá compartilhamos "
    "dicas, novidades e oportunidades do mercado:\n\n"
    "👉 https://chat.whatsapp.com/KnYUCxhYwXIFsIoD6FAYmY?mode=gi_t\n\n"
    "Qualquer dúvida, estou à disposição. Até mais! 😊"
)

# ---------------------------------------------------------------------------
# Helpers de normalização e matching
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Remove acentos, converte para lowercase, colapsa espaços."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower().strip())


def _adm_matches(adm_raw: str, token_groups: list[list[str]],
                 exact_siglas: set[str] | None = None) -> bool:
    """
    Retorna True se adm_raw corresponder a qualquer token em token_groups.
    Usa substring normalizado; exact_siglas permite match exato para siglas curtas (ex: "BB", "HS").
    """
    adm = _normalize(adm_raw)
    if exact_siglas and adm in exact_siglas:
        return True
    for group in token_groups:
        for token in group:
            if _normalize(token) in adm:
                return True
    return False


def _qualifica_bazar(card: dict) -> tuple[bool, str]:
    """
    Retorna (qualificado, motivo_rejeição).
    Critério Bazar: adm na lista Bazar E Situação == 'contemplada-sorteio' ou vazia.
    """
    adm_raw = card.get("Adm") or ""
    situacao = _normalize(card.get("Situação") or "")

    if not _adm_matches(adm_raw, ADM_BAZAR_TOKENS):
        return False, f"adm '{adm_raw}' não está na lista Bazar"

    if situacao not in ("contemplada-sorteio", ""):
        return False, f"Situação '{situacao}' não é contemplada-sorteio nem vazia"

    return True, ""


def _qualifica_lp(card: dict) -> tuple[bool, str]:
    """
    Retorna (qualificado, motivo_rejeição).
    Critério LP: adm na lista LP E Tipo contemplação == 'contemplada-sorteio'.
    """
    adm_raw = card.get("Adm") or ""
    tipo_cont = _normalize(card.get("Tipo contemplação") or "")

    if not _adm_matches(adm_raw, ADM_LP_TOKENS, exact_siglas=_LP_EXACT_SIGLAS):
        return False, f"adm '{adm_raw}' não está na lista LP"

    if tipo_cont != "contemplada-sorteio":
        return False, f"Tipo contemplação '{tipo_cont}' não é contemplada-sorteio"

    return True, ""


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


async def _activate_card(card: dict, message_template: str,
                         qualifica_fn, faro: FaroClient) -> bool:
    """
    Qualifica o card pelo critério da fonte, envia mensagem e move no FARO.
    Qualificado   → mensagem de ativação + move para Primeira Ativação
    Não qualificado → mensagem de agradecimento + move para Não Qualificado
    """
    card_id = card["id"]
    nome = get_name(card)
    adm = get_adm(card)
    phone = get_phone(card)

    if not phone:
        logger.warning("Card %s sem telefone — movendo para Não Qualificado", card_id[:8])
        try:
            await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
        except FaroError:
            pass
        return False

    # Verifica se o número tem WhatsApp antes de disparar
    try:
        async with get_whapi_for_card(card) as w:
            has_wa = await w.check_phone(phone)
        if not has_wa:
            logger.info("Card %s sem WhatsApp (%s) — movendo para Problema de Contato", card_id[:8], phone[-4:])
            try:
                await faro.move_card(card_id, Stage.PROBLEMA_CONTATO)
            except FaroError as e:
                logger.error("Erro ao mover card %s para Problema de Contato: %s", card_id[:8], e)
            return False
    except Exception as e:
        logger.warning("Card %s falha ao verificar WhatsApp (%s) — prosseguindo mesmo assim: %s", card_id[:8], phone[-4:], e)

    qualificado, motivo = qualifica_fn(card)

    if not qualificado:
        logger.info("Card %s não qualificado (%s) — enviando msg agradecimento", card_id[:8], motivo)
        try:
            if not TEST_MODE:
                await asyncio.sleep(random.randint(5, 30))
            async with get_whapi_for_card(card) as w:
                await w.send_text(phone, MSG_NAO_QUALIFICADO.format(nome=nome),
                                  _log_nome=nome, _log_card_id=card_id)
            await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
        except (WhapiError, FaroError) as e:
            logger.error("Erro ao desqualificar card %s: %s", card_id[:8], e)
        return False

    # Lead qualificado — envia mensagem de ativação
    logger.info("Card %s qualificado (adm='%s') — ativando", card_id[:8], adm)
    message = message_template.format(nome=nome, adm=adm)

    if not TEST_MODE:
        await asyncio.sleep(random.randint(5, 50))

    try:
        async with get_whapi_for_card(card) as w:
            await w.send_text(phone, message, _log_nome=nome, _log_card_id=card_id)

        await faro.move_card(card_id, Stage.PRIMEIRA_ATIVACAO)
        await faro.update_card(card_id, {
            "Data de primeira ativação": datetime.now(timezone.utc).strftime("%d/%m/%Y"),
            "Ultima atividade": str(int(datetime.now(timezone.utc).timestamp())),
        })
        logger.info("Whapi OK: card=%s phone=%s canal=%s", card_id[:8], phone[-4:],
                    "lp" if "lp" in str(card.get("Fonte") or "").lower() else "bazar")
        return True

    except WhapiError as e:
        logger.error("Erro Whapi card %s: %s", card_id[:8], e)
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
        logger.info("%d cards para processar (Bazar)", len(cards))
        ok = sum([await _activate_card(card, MSG_BAZAR, _qualifica_bazar, faro) for card in cards])
        logger.info("Bazar: %d/%d qualificados e ativados", ok, len(cards))


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
        logger.info("%d cards para processar (Site/LP)", len(cards))
        ok = sum([await _activate_card(card, MSG_SITE, _qualifica_lp, faro) for card in cards])
        logger.info("Site/LP: %d/%d qualificados e ativados", ok, len(cards))
