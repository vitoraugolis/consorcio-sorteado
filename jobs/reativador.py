"""
jobs/reativador.py — Reengajamento de leads parados nas etapas de ativação
Provider: Whapi (canal lista para Listas, canal lp para LP, canal bazar para Bazar)

Correção 2026-05-22:
  - check_stage_time retorna apenas metadados (card_id, entered_stage_at) sem campos
    customizados. O reativador agora busca os card_ids via check_stage_time e depois
    faz get_card() individual para obter os campos completos.
  - Guard de 15 min por canal (cs:guard:rate:{canal}:{phone}) via message_guard.
  - Porteiro de stage: re-verifica stage atual antes de enviar para evitar reativar
    leads que já avançaram desde o fetch.
  - Canal LP usa WhapiClient(canal="lp") separado do canal lista.
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

# Reativação válida somente para leads ativados a partir desta data (inclusive)
# Leads anteriores não serão reativados para evitar sobrecarga dos números
REATIVACAO_CUTOFF_DATE = "04/05/2026"  # DD/MM/AAAA — formato do campo FARO

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


def _get_canal(card: dict) -> str:
    """Retorna o canal Whapi correto para o card: 'bazar', 'lp' ou 'lista'."""
    fonte = str(card.get("Fonte") or "").lower()
    if "bazar" in fonte:
        return "bazar"
    if "lp" in fonte or "site" in fonte:
        return "lp"
    return "lista"


def _passou_cutoff(card: dict) -> bool:
    """
    Retorna True se o lead deve ser reativado com base na data de primeira ativação.

    Regras:
      - Campo vazio → True (campo não gravado, mas lead está no stage — reativar)
      - Data preenchida e >= cutoff → True
      - Data preenchida e < cutoff → False (lead antigo, ignorar)
    """
    data_str = (card.get("Data de primeira ativação") or "").strip()
    if not data_str:
        # Campo vazio: lead foi ativado mas a data não foi gravada corretamente.
        # Assumimos válido para não bloquear o fluxo.
        return True
    try:
        from datetime import date
        d, m, y = data_str.split("/")
        data_card = date(int(y), int(m), int(d))
        dc, mc, yc = REATIVACAO_CUTOFF_DATE.split("/")
        cutoff = date(int(yc), int(mc), int(dc))
        return data_card >= cutoff
    except Exception:
        # Data com formato inválido → assume válido por segurança
        return True


async def _send_lista(card: dict, stage_id: str) -> None:
    """Envia mensagem com botões via Whapi canal lista ou lp (conforme Fonte do card)."""
    phone = get_phone(card)
    if not phone:
        return
    msg_data = MESSAGES_LISTAS[stage_id]
    nome = get_name(card)
    adm = get_adm(card)
    text = msg_data["text"].format(nome=nome, adm=adm)
    canal = _get_canal(card)  # "lp" para leads LP, "lista" para Listas
    async with WhapiClient(canal=canal) as w:
        await w.send_buttons(phone, text, msg_data["buttons"])
    logger.info("Whapi %s OK: card=%s stage=%s", canal, card["id"][:8], stage_id[:8])


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
    phone = get_phone(card)
    if not phone:
        logger.warning("Card %s sem telefone — pulando", card_id[:8])
        return False

    to_stage = ACTIVATION_SEQUENCE.get(from_stage)
    if not to_stage:
        logger.error("Sem próxima etapa mapeada para %s", from_stage)
        return False

    canal = _get_canal(card)

    # Guard 1: rate-limit de 15 min por canal por número
    from services.message_guard import check_reactivation_rate, register_reactivation_rate
    if await check_reactivation_rate(phone, canal):
        logger.info("Rate limit 15min: phone=...%s canal=%s — adiando", phone[-4:], canal)
        return False

    # Guard 2: porteiro de stage — re-verifica stage atual antes de enviar
    # Evita reativar lead que já respondeu e avançou desde o fetch
    try:
        async with FaroClient() as faro_check:
            card_atual = await faro_check.get_card(card_id)
        stage_atual = (card_atual or {}).get("stage_id", "")
        if stage_atual and stage_atual != from_stage:
            logger.info(
                "Porteiro stage: card %s saiu de %s para %s — pulando reativação",
                card_id[:8], from_stage[:8], stage_atual[:8],
            )
            return False
    except Exception as e:
        logger.warning("Porteiro stage: falha ao verificar card %s — prosseguindo: %s", card_id[:8], e)

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
        await register_reactivation_rate(phone, canal)
        logger.info("✅ Card %s: %s → %s (canal=%s)", card_id[:8], from_stage[:8], to_stage[:8], canal)
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


async def _fetch_stage_cards_with_fields(
    faro: FaroClient,
    stage_id: str,
    days_threshold: int,
    limit: int,
) -> list[dict]:
    """
    Busca cards elegíveis para reativação com todos os campos customizados.

    Estratégia:
      1. check_stage_time → retorna card_ids + entered_stage_at (sem campos customizados)
      2. Para cada card_id, faz get_card() individual para obter campos completos
      3. Filtra localmente por cutoff de data

    Nota: check_stage_time retorna apenas metadados de stage (card_id, entered_stage_at,
    days_in_stage) — os campos customizados do FARO nunca vêm nessa resposta.
    """
    # Passo 1: obtém lista de card_ids elegíveis
    try:
        meta_cards = await faro.check_stage_time(
            stage_id=stage_id,
            days_threshold=days_threshold,
            limit=limit,
        )
    except FaroError as e:
        logger.error("Erro check_stage_time stage %s: %s", stage_id[:8], e)
        return []

    if not meta_cards:
        return []

    # check_stage_time retorna dicts com "card_id" (não "id")
    card_ids = [
        c.get("card_id") or c.get("id") or ""
        for c in meta_cards
    ]
    card_ids = [cid for cid in card_ids if cid]

    if not card_ids:
        logger.warning("check_stage_time retornou cards sem card_id para stage %s", stage_id[:8])
        return []

    logger.info("Stage %s: %d card_ids elegíveis — buscando campos completos", stage_id[:8], len(card_ids))

    # Passo 2: busca campos completos em paralelo (com semáforo para não sobrecarregar FARO)
    sem = asyncio.Semaphore(5)

    async def _fetch_one(card_id: str) -> dict | None:
        async with sem:
            try:
                return await faro.get_card(card_id)
            except FaroError as e:
                logger.warning("get_card(%s) falhou: %s", card_id[:8], e)
                return None

    results = await asyncio.gather(*[_fetch_one(cid) for cid in card_ids])
    full_cards = [c for c in results if c]

    logger.info("Stage %s: %d/%d cards com campos completos obtidos", stage_id[:8], len(full_cards), len(card_ids))
    return full_cards


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

            full_cards = await _fetch_stage_cards_with_fields(
                faro=faro,
                stage_id=stage_id,
                days_threshold=dias,
                limit=min(JOB_BATCH_LIMIT - total_processed, 20),
            )

            if not full_cards:
                continue

            full_cards = filter_test_cards(full_cards)
            if not full_cards:
                continue

            # Filtra apenas leads ativados a partir do cutoff (04/05/2026)
            cards_validos = [c for c in full_cards if _passou_cutoff(c)]
            ignorados = len(full_cards) - len(cards_validos)
            if ignorados:
                logger.info(
                    "Reativador: stage %s — %d lead(s) ignorado(s) por cutoff de data",
                    stage_id[:8], ignorados,
                )
            if not cards_validos:
                continue

            logger.info("Stage %s: %d cards para reativar", stage_id[:8], len(cards_validos))
            for card in cards_validos:
                success = await _process_card(card, stage_id)
                total_processed += 1
                if success:
                    total_ok += 1
                delay = random.randint(REATIVADOR_DELAY_MIN_S, REATIVADOR_DELAY_MAX_S)
                await asyncio.sleep(delay)

    logger.info("=== Reativador concluído: %d/%d ===", total_ok, total_processed)
