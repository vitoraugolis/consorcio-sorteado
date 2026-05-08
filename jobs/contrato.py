"""
jobs/contrato.py — Geração de contratos via ZapSign
Provider: Whapi (get_whapi_for_card — substitui Z-API para Bazar/Site)
"""

import asyncio
import logging
from datetime import datetime

from config import NOTIFY_PHONES, Stage, TEST_MODE, filter_test_cards, SEND_WINDOW_START, SEND_WINDOW_END, TZ_BRASILIA

_processing: dict[str, asyncio.Lock] = {}

from services.faro import (
    FaroClient, FaroError, get_phone, get_name, get_adm, is_lista,
    load_history, history_append, save_history, history_to_text,
    load_journey, journey_to_text,
)
from services.ai import AIClient, AIError
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card, notify_team as _notify_team_central
from services.zapsign import ZapSignClient, ZapSignError, get_template_for_adm, build_form_fields

logger = logging.getLogger(__name__)

MSG_CONTRATO = (
    "Olá, {nome}! 🎉\n\n"
    "Que ótima notícia! Sua proposta foi aceita e o contrato já está pronto para assinatura.\n\n"
    "Clique no link abaixo para assinar eletronicamente:\n\n"
    "👉 {sign_url}\n\n"
    "O processo leva menos de 2 minutos. Qualquer dúvida, estou aqui! 😊"
)

MSG_CONTRATO_LISTA = (
    "Pronto, {nome}! 📋 Seu contrato está pronto para assinatura.\n\n"
    "👉 {sign_url}\n\n"
    "Leva menos de 2 minutos. Qualquer dúvida, é só chamar! 😊"
)

MSG_ERRO_INTERNO = (
    "Olá, {nome}! Sua proposta foi aceita! 🎉\n\n"
    "Estamos preparando seu contrato e em breve enviaremos o link. Aguarde!"
)

_ASSINATURA_SYSTEM = """
Você é Manuela, consultora da Consórcio Sorteado.
Lead aceitou a proposta — precisa coletar dados pessoais para o contrato.
Tom entusiasmado, pessoal. Máximo 8 linhas.
""".strip()


async def _generate_assinatura_welcome(card: dict) -> str:
    nome = get_name(card)
    adm = get_adm(card)
    history_ctx = history_to_text(load_history(card))
    journey_ctx = journey_to_text(load_journey(card))
    prompt = (
        f"Lead: {nome} | Administradora: {adm}\n"
        f"Jornada:\n{journey_ctx}\nHistórico:\n{history_ctx}\n\n"
        f"Mensagem de parabéns que: 1) celebre a decisão, 2) peça os 4 dados para o contrato "
        f"(CPF, RG, Endereço completo, E-mail), 3) mencione que após os dados pediremos "
        f"o extrato detalhado da cota {adm}."
    )
    try:
        async with AIClient() as ai:
            return (await ai.complete(prompt=prompt, system=_ASSINATURA_SYSTEM, max_tokens=320)).strip()
    except Exception as e:
        logger.warning("contrato: IA falhou na welcome msg: %s", e)
        return ""


async def _send(card: dict, phone: str, text: str) -> bool:
    """Envia via Whapi — canal correto pelo tipo de lead."""
    try:
        async with get_whapi_for_card(card) as w:
            await w.send_text(phone, text)
        return True
    except WhapiError as e:
        logger.error("Whapi erro ao enviar contrato para %s: %s", phone, e)
        return False


async def _notify_team(text: str) -> None:
    """Notifica a equipe via grupo WhatsApp (com fallback para NOTIFY_PHONES)."""
    await _notify_team_central(text)
async def _process_card(card: dict) -> None:
    card_id = card.get("id", "")
    from services.session_store import acquire_mutex, release_mutex
    mutex_key = f"job:contrato:{card_id}"
    if not await acquire_mutex(mutex_key, ttl=300):
        logger.info("Contrato: card %s ja em processamento - pulando", card_id[:8])
        return
    if card_id not in _processing:
        _processing[card_id] = asyncio.Lock()
    lock = _processing[card_id]
    if lock.locked():
        await release_mutex(mutex_key)
        logger.info("Contrato: card %s ja em processamento -- ignorado.", card_id[:8])
        return
    try:
        async with lock:
            await _process_card_locked(card)
    finally:
        await release_mutex(mutex_key)

async def _process_card_locked(card: dict) -> None:
    card_id = card.get("id", "")
    nome = get_name(card)
    phone = get_phone(card)
    adm = get_adm(card)
    lista_src = is_lista(card)

    async with FaroClient() as faro:
        try:
            card_fresh = await faro.get_card(card_id)
        except FaroError as e:
            logger.error("Contrato: erro ao buscar card %s: %s", card_id[:8], e)
            return

        current_stage = card_fresh.get("stage_id") or card_fresh.get("stageId") or ""
        if current_stage != Stage.ACEITO:
            return

    # Move para ASSINATURA (stage-as-mutex)
    async with FaroClient() as faro:
        try:
            await faro.move_card(card_id, Stage.ASSINATURA)
        except FaroError as e:
            logger.error("Contrato: falha ao mover %s para ASSINATURA: %s", card_id[:8], e)
            return

    # Todos os leads (Lista e Bazar/LP): coleta dados faltantes antes do ZapSign
    primeiro_nome = nome.split()[0] if nome else "prezado(a)"
    if lista_src:
        # Listas: pede CPF, RG, Endereço, E-mail + extrato
        msg_dados = await _generate_assinatura_welcome(card)
        if not msg_dados:
            msg_dados = (
                f"Parabéns, {primeiro_nome}! 🎉 Estamos quase lá!\n\n"
                f"Para preparar seu contrato, precisamos de:\n\n"
                f"1️⃣ *CPF*\n2️⃣ *RG*\n"
                f"3️⃣ *Endereço completo* (rua, número, bairro, cidade, CEP)\n"
                f"4️⃣ *E-mail* para receber o contrato\n\n"
                f"Após os dados, envie o *extrato detalhado* da sua cota {adm}. 📄"
            )
    else:
        # Bazar/LP: CPF e dados da cota já temos — pede só o que falta
        msg_dados = (
            f"Ótimo, {primeiro_nome}! 🎉 Vamos preparar o contrato agora.\n\n"
            f"Para finalizar, preciso de mais alguns dados:\n\n"
            f"1️⃣ *Endereço completo* (rua, número, bairro, cidade, CEP)\n"
            f"2️⃣ *E-mail* para receber o contrato\n"
            f"3️⃣ *Estado civil* (solteiro, casado, etc.)\n"
            f"4️⃣ *Profissão / ocupação*\n"
            f"5️⃣ *Nacionalidade*\n\n"
            f"Pode me mandar tudo de uma vez! 😊"
        )

    if phone:
        await _send(card, phone, msg_dados)
    history = load_history(card)
    history = history_append(history, "assistant", msg_dados)
    async with FaroClient() as faro:
        await faro.update_card(card_id, {"Ultima atividade": datetime.now().isoformat()})
        await save_history(faro, card_id, history)
    # Fase 2 (geração ZapSign) ocorre em agente_contrato.handle_extrato_recebido
    # quando o lead envia o extrato detalhado → generate_and_send_contract é chamado lá.


async def generate_and_send_contract(card: dict) -> bool:
    """Gera ZapSign e envia link ao lead. Usado pelo agente_contrato após receber extrato."""
    card_id = card.get("id", "")
    nome = get_name(card)
    phone = get_phone(card)
    adm = get_adm(card)
    lista_src = is_lista(card)

    template_token = get_template_for_adm(adm)
    if not template_token:
        await _notify_team(f"⚠️ *Contrato sem template ZapSign*\nLead: {nome}\nAdm: {adm}")
        return False

    sign_url = None
    doc_token = None
    try:
        async with ZapSignClient() as zap:
            doc = await zap.create_from_template(
                template_token=template_token,
                doc_name=f"Contrato - {nome} - {adm}",
                lead_signer={"name": nome, "email": card.get("Email", ""), "phone": phone},
                form_fields=build_form_fields(card),
            )
        sign_url = doc.get("lead_sign_url", "")
        doc_token = doc.get("doc_token", "")
    except ZapSignError as e:
        logger.error("Erro ZapSign card %s: %s", card_id[:8], e)
        await _notify_team(f"❌ *Erro ZapSign*\nLead: {nome} | Adm: {adm}\nErro: {e}")
        return False

    if phone and sign_url:
        primeiro_nome = nome.split()[0] if nome else "prezado(a)"
        msg = (MSG_CONTRATO_LISTA if lista_src else MSG_CONTRATO).format(
            nome=primeiro_nome, sign_url=sign_url
        )
        await _send(card, phone, msg)

    async with FaroClient() as faro:
        try:
            update: dict = {"Ultima atividade": datetime.now().isoformat()}
            if doc_token:
                update["ZapSign Token"] = doc_token
            await faro.update_card(card_id, update)
            current_stage = card.get("stage_id") or card.get("stageId") or ""
            if current_stage != Stage.ASSINATURA:
                await faro.move_card(card_id, Stage.ASSINATURA)
        except FaroError as e:
            logger.error("Erro FARO contrato card %s: %s", card_id[:8], e)

    await _notify_team(
        f"✅ *Contrato enviado*\nLead: {nome}\nAdm: {adm}\n"
        f"Doc: {doc_token[:12] if doc_token else 'N/A'}..."
    )
    return True


async def run_contrato() -> None:
    logger.info("Job contrato: verificando stage ACEITO")
    if not (SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END):
        logger.info("Contrato: fora da janela de envio, pulando.")
        return
    try:
        async with FaroClient() as faro:
            cards = await faro.watch_new(Stage.ACEITO)
    except FaroError as e:
        logger.error("Job contrato: erro FARO: %s", e)
        return
    if not cards:
        return
    cards = filter_test_cards(cards)
    if not cards:
        return
    logger.info("Job contrato: %d card(s)", len(cards))

    # Processar em paralelo com semáforo (máx 3 simultâneos — ZapSign tem rate limit)
    sem = asyncio.Semaphore(3)

    async def _bounded(card: dict) -> None:
        async with sem:
            try:
                await _process_card(card)
            except Exception as e:
                logger.exception("Job contrato: erro inesperado card %s: %s",
                                 card.get("id", "?")[:8], e)
            await asyncio.sleep(2)

    await asyncio.gather(*[_bounded(c) for c in cards], return_exceptions=True)


async def process_contrato_card(card: dict) -> None:
    """Ponto de entrada público para o webhook FARO — processa um card específico."""
    await _process_card(card)


async def run_contrato_safe():
    """Wrapper resiliente — garante que exceções não derrubam o scheduler."""
    try:
        await run_contrato()
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("run_contrato: erro inesperado: %s", e)
        try:
            from services.slack import slack_error
            await slack_error("Job contrato falhou inesperadamente", exception=e)
        except Exception:
            pass
