"""
jobs/ativacao_listas.py — Ativação de leads frios vindos de Listas

Lógica:
  1. Busca cards na etapa "Listas" (leads frios subidos em lote)
  2. Normaliza o telefone
  3. Envia mensagem interativa com botões via Whapi
  4. Move o card para "Primeira ativação"
  5. Sleep aleatório entre cada card (crítico para não banir)

Provider: Whapi (obrigatório para listas — volume alto)
Frequência sugerida: a cada 30 min, mas só processa se houver cards pendentes
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

from config import (
    Stage,
    LISTAS_DELAY_MIN_S,
    LISTAS_DELAY_MAX_S,
    SEND_WINDOW_START,
    SEND_WINDOW_END,
    JOB_BATCH_LIMIT,
    TEST_MODE,
    TZ_BRASILIA,
    filter_test_cards,
)
from services.faro import FaroClient, FaroError, get_adm, get_name
from services.whapi import WhapiClient, WhapiError
from services.ai import AIClient, AIError
from services.session_store import acquire_mutex, release_mutex
from services.slack import slack_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mensagem de ativação de listas
# Whapi suporta mensagens interativas — usamos botões para aumentar resposta
# ---------------------------------------------------------------------------

ACTIVATION_HEADER = (
    "Meu nome é Manuela, da Consórcio Sorteado, empresa que está há 30 anos "
    "no mercado de cotas contempladas."
)

ACTIVATION_MESSAGE = (
    "⚡️ {nome}, identificamos em um dos grupos em que somos consorciados que você "
    "tem uma cota contemplada {adm}! E por isso, gostaríamos de lembrar que sua cota "
    "pode ser vendida com ótima valorização. 🎉\n\n"
    "Por isso, gostaria de saber: você teria interesse em receber uma proposta "
    "personalizada pela sua cota, sem compromisso?"
)

ACTIVATION_BUTTONS = [
    {"id": "quero_proposta", "title": "Quero receber proposta"},
    {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
]


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


async def _normalize_phone(raw_phone: str) -> str:
    """Normaliza telefone usando IA como fallback."""
    digits = "".join(c for c in str(raw_phone) if c.isdigit())
    if digits.startswith("55") and len(digits) in (12, 13):
        return digits
    if not digits.startswith("55"):
        digits = "55" + digits
    if len(digits) in (12, 13):
        return digits

    # Fallback com IA
    try:
        async with AIClient() as ai:
            return await ai.format_phone(raw_phone)
    except AIError:
        logger.warning("Falha ao normalizar telefone '%s' via IA, usando limpeza manual", raw_phone)
        return digits


async def _process_card(card: dict, whapi: WhapiClient, faro: FaroClient) -> bool:
    card_id = card["id"]

    # Mutex distribuído via Redis — evita disparo duplo mesmo após reinício
    acquired = await acquire_mutex(f"ativacao:{card_id}")
    if not acquired:
        logger.debug("Card %s já em processamento, pulando.", card_id[:8])
        return False
    try:
        return await _process_card_inner(card, whapi, faro, card_id)
    finally:
        await release_mutex(f"ativacao:{card_id}")


async def _process_card_inner(card: dict, whapi: WhapiClient, faro: FaroClient, card_id: str) -> bool:
    """Lógica interna de processamento — chamada apenas pelo mutex de _process_card."""
    raw_phone = card.get("Telefone") or card.get("Telefone alternativo") or ""

    if not raw_phone:
        logger.warning("Card %s sem telefone, movendo para Não Qualificado", card_id[:8])
        try:
            await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
        except FaroError:
            pass
        return False

    phone = await _normalize_phone(str(raw_phone))
    nome = get_name(card)
    adm = get_adm(card)
    message = ACTIVATION_MESSAGE.format(nome=nome, adm=adm)


    sent = False
    try:
        await whapi.send_buttons(
            to=phone,
            message=message,
            buttons=ACTIVATION_BUTTONS,
            header=ACTIVATION_HEADER,
        )
        logger.info("Whapi botões OK: card=%s phone=%s", card_id[:8], phone[-4:])
        sent = True
    except WhapiError as e:
        # Se o endpoint de botões não está disponível → tenta texto simples
        endpoint_error = "not found" in str(e).lower() and e.status_code == 404
        if endpoint_error:
            logger.warning(
                "Endpoint de botões indisponível para card %s, tentando texto simples.",
                card_id[:8],
            )
            try:
                await whapi.send_text(phone, message)
                logger.info("Whapi texto OK (fallback): card=%s phone=%s", card_id[:8], phone[-4:])
                sent = True
            except WhapiError as e2:
                logger.error("Fallback texto também falhou card %s: %s", card_id[:8], e2)
                if e2.status_code in (400, 404):
                    try:
                        await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
                        await faro.update_card(card_id, {"Situação": "telefone inválido"})
                    except FaroError:
                        pass
        else:
            # Número inválido ou bloqueado → move para Não Qualificado
            logger.error("Erro Whapi card %s: %s", card_id[:8], e)
            if e.status_code in (400, 404):
                try:
                    await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
                    await faro.update_card(card_id, {"Situação": "telefone inválido"})
                except FaroError:
                    pass

    if sent:
        try:
            await faro.move_card(card_id, Stage.PRIMEIRA_ATIVACAO)
            await faro.update_card(card_id, {
                "Data de primeira ativação": datetime.now(timezone.utc).strftime("%d/%m/%Y"),
                "Ultima atividade": str(int(datetime.now(timezone.utc).timestamp())),
            })
        except FaroError as e:
            logger.error("Erro FARO card %s: %s", card_id[:8], e)
            return False
        return True

    return False


async def run_ativacao_listas():
    """
    Job de ativação de listas.
    Deve ser chamado periodicamente pelo scheduler.
    """
    if not _is_within_send_window():
        logger.info("Ativação Listas: fora da janela de envio, pulando.")
        return

    # Health check — só bloqueia se HTTP falhar (status AUTH é falso positivo no Whapi)
    async with WhapiClient(canal="lista") as w:
        ok, status = await w.health_check()
    if not ok:
        logger.error("Whapi Lista não responde (HTTP erro) — status: %s — abortando ativação", status)
        await slack_error(
            f"⚠️ Canal Whapi Lista não está respondendo (HTTP erro, status: {status}). "
            "Ativação de Listas abortada. Verifique o painel Whapi."
        )
        return

    logger.info("=== Iniciando Ativação de Listas ===")

    async with FaroClient() as faro:
        # Busca TODOS os cards pendentes na etapa Listas
        try:
            cards = await faro.get_cards_all_pages(
                stage_id=Stage.LISTAS,
                page_size=100,
            )
        except FaroError as e:
            logger.error("Erro ao buscar cards de Listas: %s", e)
            return

        if not cards:
            logger.info("Nenhum card na etapa Listas.")
            return

        # Em TEST_MODE, processa apenas o card de teste
        cards = filter_test_cards(cards)
        if TEST_MODE:
            logger.info("TEST_MODE ativo: %d card(s) após filtro de teste.", len(cards))

        # Limita ao batch máximo por ciclo
        batch = cards[:JOB_BATCH_LIMIT]
        logger.info("%d cards encontrados, processando %d neste ciclo", len(cards), len(batch))

        total_ok = 0
        total_err = 0
        async with WhapiClient() as whapi:
            for i, card in enumerate(batch):
                try:
                    success = await _process_card(card, whapi, faro)
                    if success:
                        total_ok += 1
                    else:
                        total_err += 1
                except Exception as e:
                    total_err += 1
                    logger.error(
                        "Ativação Listas: erro inesperado card %s: %s",
                        card.get("id", "?")[:8], e
                    )

                # Sleep entre disparos (anti-ban)
                if i < len(batch) - 1:
                    delay = random.randint(LISTAS_DELAY_MIN_S, LISTAS_DELAY_MAX_S)
                    logger.debug("Aguardando %ds antes do próximo...", delay)
                    await asyncio.sleep(delay)

    logger.info(
        "=== Ativação Listas concluída: %d/%d enviados | %d erros ===",
        total_ok, len(batch), total_err,
    )


async def run_ativacao_listas_safe():
    """Wrapper resiliente — garante que exceções não derrubam o scheduler."""
    try:
        await run_ativacao_listas()
    except Exception as e:
        logger.exception("run_ativacao_listas: erro inesperado: %s", e)
        try:
            from services.slack import slack_error
            await slack_error("Job ativacao_listas falhou inesperadamente", exception=e)
        except Exception:
            pass
