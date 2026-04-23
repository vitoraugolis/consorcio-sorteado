"""
jobs/reativador.py — Reengajamento de leads parados nas etapas de ativação
Provider: Whapi (canal lista para Listas/LP, canal bazar para Bazar)
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

from config import (
    Stage, ACTIVATION_SEQUENCE, REATIVACAO_DIAS,
    REATIVADOR_DELAY_MIN_S, REATIVADOR_DELAY_MAX_S,
    SEND_WINDOW_START, SEND_WINDOW_END, JOB_BATCH_LIMIT,
    TEST_MODE, TZ_BRASILIA, filter_test_cards,
)
from services.faro import FaroClient, FaroError, get_phone, get_name, get_adm, is_lista
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card

logger = logging.getLogger(__name__)

_GRUPO_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

# Mensagens para leads de Listas/LP (botões)
MESSAGES_LISTAS = {
    Stage.PRIMEIRA_ATIVACAO: {
        "text": (
            "Sei que você pode estar pensando sobre nossa proposta para a sua cota {adm}. 😊\n\n"
            "💡 Alguns pontos que vale considerar:\n"
            "• Cotas contempladas estão valorizadas — mas o valor oscila com o tempo\n"
            "• O mercado atual está favorável para quem quer vender\n"
            "• Nossa avaliação é gratuita e sem compromisso\n\n"
            "Ainda tem interesse em receber uma proposta personalizada?"
        ),
        "buttons": [
            {"id": "quero_proposta", "title": "Quero receber proposta"},
            {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
        ],
    },
    Stage.SEGUNDA_ATIVACAO: {
        "text": (
            "Esta semana ajudamos 3 pessoas a vender suas cotas contempladas "
            "— e todas ficaram surpresas com a simplicidade do processo! 🙌✨\n\n"
            "Sua cota {adm} pode ter um valor muito interessante no mercado atual.\n\n"
            "Posso preparar uma proposta personalizada para você?"
        ),
        "buttons": [
            {"id": "quero_proposta", "title": "Quero receber proposta"},
            {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
        ],
    },
    Stage.TERCEIRA_ATIVACAO: {
        "text": (
            "Não quero ser insistente, {nome}, mas o mercado de cotas contempladas "
            "está realmente aquecido agora! 📈\n\n"
            "🎯 Alta demanda por cotas {adm} — o processo é simples e rápido.\n\n"
            "Essa pode ser a última vez que entro em contato. "
            "Você toparia receber uma proposta sem compromisso?"
        ),
        "buttons": [
            {"id": "quero_proposta", "title": "Quero receber proposta"},
            {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
        ],
    },
    Stage.QUARTA_ATIVACAO: {
        "text": (
            "Entendo que a venda da sua cota {adm} não faz sentido agora — tudo bem! 😊\n\n"
            "Se um dia mudar de ideia, é só nos chamar. A Consórcio Sorteado estará aqui.\n\n"
            "Aproveitamos para te convidar para o nosso grupo especial:\n"
            f"{_GRUPO_LINK}\n\n"
            "💛 Obrigada pela atenção, {nome}!"
        ),
        "buttons": [
            {"id": "quero_proposta", "title": "Quero receber proposta"},
            {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
        ],
    },
}

# Mensagens para leads de Bazar (texto simples, canal bazar)
MESSAGES_BAZAR = {
    Stage.PRIMEIRA_ATIVACAO: (
        "Oi, {nome}! 😊 Vi que você demonstrou interesse em vender sua cota {adm}, "
        "mas ainda não conseguimos conversar!\n\n"
        "Só preciso do extrato atualizado da sua cota para fazer a análise. "
        "Tem o extrato em mãos?"
    ),
    Stage.SEGUNDA_ATIVACAO: (
        "{nome}, tudo bem?\n\n"
        "Ontem mesmo fechamos a compra de uma cota {adm} similar à sua — "
        "e o processo foi super rápido! 🎉\n\n"
        "É literalmente só enviar o extrato e nossa equipe já cuida do resto. "
        "Posso esperar você enviar agora?"
    ),
    Stage.TERCEIRA_ATIVACAO: (
        "{nome}, é a Manuela! 😊\n\n"
        "Estou preocupada em não ter conseguido te ajudar ainda...\n\n"
        "Se tiver um 'sim' guardado aí, me manda o extrato da cota {adm} agora "
        "e eu garanto uma análise rápida pra você!"
    ),
    Stage.QUARTA_ATIVACAO: (
        "{nome}, uma mensagem final! 📝\n\n"
        "Entendo que o momento pode não ser ideal. Não tem problema! 😊\n\n"
        "Seu cadastro fica salvo aqui e, quando quiser, é só me chamar.\n\n"
        "Um abraço da Manuela! 💛"
    ),
}


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _is_bazar_source(card: dict) -> bool:
    from services.faro import get_fonte
    fonte = get_fonte(card)
    return "bazar" in fonte


async def _send_lista(card: dict, stage_id: str) -> None:
    """Envia mensagem com botões via Whapi canal lista."""
    phone = get_phone(card)
    if not phone:
        return
    msg_data = MESSAGES_LISTAS[stage_id]
    nome = get_name(card)
    adm = get_adm(card)
    text = msg_data["text"].format(nome=nome, adm=adm)
    async with WhapiClient(canal="lista") as w:
        await w.send_buttons(phone, text, msg_data["buttons"])
    logger.info("Whapi lista OK: card=%s stage=%s", card["id"][:8], stage_id[:8])


async def _send_bazar(card: dict, stage_id: str) -> None:
    """Envia mensagem de texto via Whapi canal bazar."""
    phone = get_phone(card)
    if not phone:
        return
    nome = get_name(card)
    adm = get_adm(card)
    text = MESSAGES_BAZAR[stage_id].format(nome=nome, adm=adm)
    async with WhapiClient(canal="bazar") as w:
        await w.send_text(phone, text)
    logger.info("Whapi bazar OK: card=%s stage=%s", card["id"][:8], stage_id[:8])


async def _process_card(card: dict, from_stage: str) -> bool:
    card_id = card["id"]
    to_stage = ACTIVATION_SEQUENCE.get(from_stage)
    if not to_stage:
        logger.error("Sem próxima etapa mapeada para %s", from_stage)
        return False
    try:
        if _is_bazar_source(card):
            await _send_bazar(card, from_stage)
        else:
            await _send_lista(card, from_stage)
        async with FaroClient() as faro:
            await faro.move_card(card_id, to_stage)
            await faro.update_card(card_id, {
                "Ultima atividade": str(int(datetime.now(timezone.utc).timestamp())),
            })
        logger.info("✅ Card %s: %s → %s", card_id[:8], from_stage[:8], to_stage[:8])
        return True
    except WhapiError as e:
        logger.error("❌ Erro Whapi card %s: %s", card_id[:8], e)
        return False
    except FaroError as e:
        logger.error("❌ Erro FARO card %s: %s", card_id[:8], e)
        return False
    except Exception as e:
        logger.exception("❌ Erro inesperado card %s: %s", card_id[:8], e)
        return False


async def run_reativador():
    if not _is_within_send_window():
        logger.info("Reativador: fora da janela de envio, pulando.")
        return
    logger.info("=== Iniciando Reativador ===")
    stages_to_check = [
        Stage.PRIMEIRA_ATIVACAO,
        Stage.SEGUNDA_ATIVACAO,
        Stage.TERCEIRA_ATIVACAO,
        Stage.QUARTA_ATIVACAO,
    ]
    total_processed = 0
    total_ok = 0
    async with FaroClient() as faro:
        for stage_id in stages_to_check:
            if total_processed >= JOB_BATCH_LIMIT:
                break
            dias = REATIVACAO_DIAS.get(stage_id, 2)
            try:
                cards = await faro.check_stage_time(
                    stage_id=stage_id,
                    days_threshold=dias,
                    limit=min(JOB_BATCH_LIMIT - total_processed, 20),
                )
            except FaroError as e:
                logger.error("Erro buscando cards em %s: %s", stage_id[:8], e)
                continue
            if not cards:
                continue
            cards = filter_test_cards(cards)
            if not cards:
                continue
            logger.info("Stage %s: %d cards para reativar", stage_id[:8], len(cards))
            for card in cards:
                success = await _process_card(card, stage_id)
                total_processed += 1
                if success:
                    total_ok += 1
                delay = random.randint(REATIVADOR_DELAY_MIN_S, REATIVADOR_DELAY_MAX_S)
                await asyncio.sleep(delay)
    logger.info("=== Reativador concluído: %d/%d ===", total_ok, total_processed)
