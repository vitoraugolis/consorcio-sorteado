"""
jobs/contrato.py — Job de geração de contratos via ZapSign

Fluxo disparado quando um lead aceita a proposta (card entra no stage ACEITO):
  1. watch_new() retorna cards recém-chegados no stage ACEITO
  2. Seleciona template ZapSign baseado na administradora do card
  3. Preenche campos do contrato com dados do card
  4. Cria documento no ZapSign e obtém URL de assinatura do lead
  5. Envia URL ao lead via WhatsApp (Whapi ou Z-API, conforme fonte)
  6. Move card para stage ASSINATURA
  7. Registra doc_token no card (campo "ZapSign Token") para rastreamento

O webhook /webhook/zapsign em main.py recebe a notificação de assinatura completa
e move o card de ASSINATURA → SUCESSO → FINALIZACAO_COMERCIAL.
"""

import asyncio
import logging
from datetime import datetime

from config import NOTIFY_PHONES, Stage, TEST_MODE, filter_test_cards

# Lock em memória — evita processar o mesmo card em execuções paralelas
_processing: set[str] = set()
from services.faro import (
    FaroClient, FaroError, get_phone, get_name, get_adm, is_lista,
    load_history, history_append, save_history, history_to_text,
    load_journey, journey_to_text,
)
from services.ai import AIClient, AIError
from services.whapi import WhapiClient, WhapiError
from services.zapi import ZAPIClient, ZAPIError, get_zapi_for_card
from services.zapsign import ZapSignClient, ZapSignError, get_template_for_adm, build_form_fields

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mensagem enviada ao lead com o link de assinatura
# ---------------------------------------------------------------------------

MSG_CONTRATO = (
    "Olá, {nome}! 🎉\n\n"
    "Que ótima notícia! Sua proposta foi aceita e o contrato já está pronto para assinatura.\n\n"
    "Clique no link abaixo para assinar eletronicamente de forma rápida e segura:\n\n"
    "👉 {sign_url}\n\n"
    "O processo leva menos de 2 minutos e pode ser feito pelo celular.\n"
    "Qualquer dúvida, estou aqui! 😊"
)

# Versão para leads de lista que já estão em ASSINATURA coletando extrato
MSG_CONTRATO_LISTA = (
    "Pronto, {nome}! 📋 Seu contrato está pronto para assinatura.\n\n"
    "👉 {sign_url}\n\n"
    "O processo leva menos de 2 minutos e pode ser feito pelo celular. "
    "Qualquer dúvida, estou aqui! 😊"
)

MSG_ERRO_INTERNO = (
    "Olá, {nome}! Sua proposta foi aceita! 🎉\n\n"
    "Estamos preparando seu contrato e em breve enviaremos o link de assinatura.\n"
    "Aguarde um instante!"
)

# ---------------------------------------------------------------------------
# Mensagem de boas-vindas em ASSINATURA (IA com histórico)
# ---------------------------------------------------------------------------

_ASSINATURA_SYSTEM = """
Você é Manuela, consultora da Consórcio Sorteado.
O lead acabou de aceitar a proposta de compra da cota contemplada.
Agora precisa coletar dados pessoais para formalizar o contrato.
Tom: entusiasmado, pessoal, continuidade natural da negociação.
Máximo 8 linhas. Apenas o texto da mensagem, sem aspas nem marcadores de tópico no título.
""".strip()


async def _generate_assinatura_welcome(card: dict) -> str:
    """
    Gera mensagem de entrada em ASSINATURA personalizada com base no histórico.
    Fallback para mensagem estática se IA falhar ou histórico vazio.
    """
    nome  = get_name(card)
    adm   = get_adm(card)
    history     = load_history(card)
    history_ctx = history_to_text(history)
    journey     = load_journey(card)
    journey_ctx = journey_to_text(journey)

    prompt = (
        f"Lead: {nome} | Administradora: {adm}\n"
        f"O lead acabou de aceitar a proposta! Preciso coletar 4 dados para o contrato.\n\n"
        f"Resumo da jornada:\n{journey_ctx}\n\n"
        f"Histórico da negociação:\n{history_ctx}\n\n"
        f"Escreva uma mensagem de parabéns personalizada que:\n"
        f"1. Celebre a decisão referenciando algo real do histórico ou da jornada\n"
        f"2. Explique que precisamos de alguns dados para formalizar o contrato\n"
        f"3. Liste os 4 dados necessários de forma clara:\n"
        f"   1️⃣ CPF\n   2️⃣ RG ou CNH\n   3️⃣ Endereço completo\n   4️⃣ E-mail\n"
        f"4. Mencione que após os dados, pediremos o extrato detalhado da cota {adm}"
    )

    try:
        async with AIClient() as ai:
            msg = await ai.complete(prompt=prompt, system=_ASSINATURA_SYSTEM, max_tokens=320)
        return msg.strip()
    except (AIError, Exception) as e:
        import logging
        logging.getLogger(__name__).warning("contrato: IA falhou na welcome msg: %s — usando fallback", e)
        return ""   # caller usa o texto estático


# ---------------------------------------------------------------------------
# Helpers de envio
# ---------------------------------------------------------------------------

async def _send_whapi(phone: str, text: str) -> bool:
    try:
        async with WhapiClient() as w:
            await w.send_text(phone, text)
        return True
    except WhapiError as e:
        logger.error("Whapi erro ao enviar contrato para %s: %s", phone, e)
        return False


async def _send_zapi(card: dict, phone: str, text: str) -> bool:
    zapi = get_zapi_for_card(card)  # já retorna ZAPIClient
    try:
        async with zapi as z:
            await z.send_text(phone, text)
        return True
    except ZAPIError as e:
        logger.error("Z-API erro ao enviar contrato para %s: %s", phone, e)
        return False


async def _notify_team(text: str) -> None:
    """Envia notificação de contrato gerado para os números internos."""
    if not NOTIFY_PHONES:
        return
    try:
        async with WhapiClient() as w:
            for phone in NOTIFY_PHONES:
                await w.send_text(phone, text)
    except WhapiError as e:
        logger.warning("Falha ao notificar equipe: %s", e)


# ---------------------------------------------------------------------------
# Processamento de um card
# ---------------------------------------------------------------------------

async def _process_card(card: dict) -> None:
    card_id = card.get("id", "")

    # Lock em memória
    if card_id in _processing:
        logger.info("Contrato: card %s já em processamento, pulando.", card_id[:8])
        return
    _processing.add(card_id)

    try:
        await _process_card_locked(card)
    finally:
        _processing.discard(card_id)


async def _process_card_locked(card: dict) -> None:
    card_id   = card.get("id", "")
    nome      = get_name(card)
    phone     = get_phone(card)
    adm       = get_adm(card)
    lista_src = is_lista(card)

    # Busca card fresco e verifica stage — stage-as-mutex
    async with FaroClient() as faro:
        try:
            card_fresh = await faro.get_card(card_id)
        except FaroError as e:
            logger.error("Contrato: erro ao buscar card fresco %s: %s", card_id[:8], e)
            return

    current_stage = card_fresh.get("stage_id") or card_fresh.get("stageId") or ""
    if current_stage != Stage.ACEITO:
        logger.info("Contrato: card %s não está mais em ACEITO (stage=%s...), pulando.", card_id[:8], current_stage[:8])
        return

    # Move para ASSINATURA ANTES de processar (stage-as-mutex — evita duplo processamento)
    async with FaroClient() as faro:
        try:
            await faro.move_card(card_id, Stage.ASSINATURA)
        except FaroError as e:
            logger.error("Contrato: falha ao mover card %s para ASSINATURA: %s", card_id[:8], e)
            return

    logger.info("Contrato: processando card %s | %s | adm=%s", card_id[:8], nome, adm)

    # Para leads de Listas: primeiro solicitar dados pessoais + extrato detalhado
    # (para LP/Bazar o extrato já foi coletado na qualificação)
    if lista_src:
        logger.info("Contrato: lead de lista — solicitando dados e extrato antes do ZapSign.")

        # Tenta gerar mensagem personalizada com histórico da negociação
        primeiro_nome = nome.split()[0] if nome else "prezado(a)"
        msg_dados = await _generate_assinatura_welcome(card)
        if not msg_dados:
            # Fallback estático
            msg_dados = (
                f"Parabéns, {primeiro_nome}! 🎉 Estamos quase lá!\n\n"
                f"Para preparar seu contrato, precisamos de algumas informações:\n\n"
                f"1️⃣ *CPF*\n"
                f"2️⃣ *RG ou CNH*\n"
                f"3️⃣ *Endereço completo* (rua, número, bairro, cidade, CEP)\n"
                f"4️⃣ *E-mail* para receber o contrato\n\n"
                f"Após enviar os dados pessoais, envie também uma foto ou PDF do "
                f"*extrato detalhado* da sua cota {adm}.\n\n"
                f"_(O extrato detalhado mostra o histórico completo da cota — diferente "
                f"do comprovante de pagamento)_ 📄"
            )

        if phone:
            await _send_whapi(phone, msg_dados)

        # Registra no histórico
        history = load_history(card)
        history = history_append(history, "assistant", msg_dados)

        async with FaroClient() as faro:
            try:
                await faro.update_card(card_id, {
                    "Ultima atividade": datetime.now().isoformat(),
                })
                logger.info("Contrato: card %s em ASSINATURA aguardando dados/extrato", card_id[:8])
            except FaroError as e:
                logger.error("Contrato: erro ao atualizar card %s: %s", card_id[:8], e)
            await save_history(faro, card_id, history)
        return

    # 1. Resolve template ZapSign
    template_token = get_template_for_adm(adm)
    if not template_token:
        logger.warning("Contrato: administradora '%s' sem template mapeado (card %s). Notificando equipe.", adm, card_id[:8])
        await _notify_team(
            f"⚠️ *Contrato sem template ZapSign*\n"
            f"Lead: {nome}\nAdministradora: {adm}\n"
            f"Por favor, gere o contrato manualmente."
        )
        return

    # 2. Cria documento no ZapSign
    sign_url = None
    doc_token = None
    try:
        doc_name   = f"Contrato - {nome} - {adm}"
        form_fields = build_form_fields(card)

        lead_signer = {
            "name":  nome,
            "email": card.get("Email", ""),
            "phone": phone,
        }

        async with ZapSignClient() as zap:
            doc = await zap.create_from_template(
                template_token=template_token,
                doc_name=doc_name,
                lead_signer=lead_signer,
                form_fields=form_fields,
            )

        sign_url  = doc.get("lead_sign_url", "")
        doc_token = doc.get("doc_token", "")

        logger.info(
            "Contrato: documento criado token=%s | lead_url=%s...",
            doc_token[:8], sign_url[:40] if sign_url else "(vazio)",
        )

    except ZapSignError as e:
        logger.error("Contrato: erro ao criar documento ZapSign para card %s: %s", card_id[:8], e)
        # Envia mensagem de espera ao lead e notifica equipe
        if phone:
            msg_espera = MSG_ERRO_INTERNO.format(nome=nome.split()[0] if nome else "prezado(a)")
            if lista_src:
                await _send_whapi(phone, msg_espera)
            else:
                await _send_zapi(card, phone, msg_espera)
        await _notify_team(
            f"❌ *Erro ao gerar contrato ZapSign*\n"
            f"Lead: {nome} | Adm: {adm}\n"
            f"Erro: {e}\n"
            f"Card: {card_id}"
        )
        return

    # 3. Envia link ao lead
    if not phone:
        logger.warning("Contrato: card %s sem telefone. Não foi possível enviar contrato.", card_id[:8])
        await _notify_team(
            f"⚠️ *Contrato sem telefone*\n"
            f"Lead: {nome} | Adm: {adm}\n"
            f"URL de assinatura: {sign_url}"
        )
    elif sign_url:
        primeiro_nome = nome.split()[0] if nome else "prezado(a)"
        mensagem = MSG_CONTRATO.format(nome=primeiro_nome, sign_url=sign_url)
        if lista_src:
            await _send_whapi(phone, mensagem)
        else:
            await _send_zapi(card, phone, mensagem)
    else:
        logger.warning("Contrato: sign_url vazio para card %s. Notificando equipe.", card_id[:8])
        await _notify_team(
            f"⚠️ *Contrato gerado sem URL de assinatura*\n"
            f"Lead: {nome} | Doc token: {doc_token}"
        )

    # 4. Atualiza card no FARO (já está em ASSINATURA desde o início)
    async with FaroClient() as faro:
        try:
            update_fields: dict = {"Ultima atividade": datetime.now().isoformat()}
            if doc_token:
                update_fields["ZapSign Token"] = doc_token
            await faro.update_card(card_id, update_fields)
            logger.info("Contrato: card %s atualizado em ASSINATURA", card_id[:8])
        except FaroError as e:
            logger.error("Contrato: erro ao atualizar card %s: %s", card_id[:8], e)

    # 5. Notifica equipe com resumo
    await _notify_team(
        f"✅ *Contrato enviado para assinatura*\n"
        f"Lead: {nome}\n"
        f"Adm: {adm}\n"
        f"Telefone: {phone or 'não informado'}\n"
        f"Doc: {doc_token[:12] if doc_token else 'N/A'}..."
    )


# ---------------------------------------------------------------------------
# Geração de contrato ZapSign — reutilizável pelo router
# ---------------------------------------------------------------------------

async def generate_and_send_contract(card: dict) -> bool:
    """
    Gera o documento ZapSign e envia o link de assinatura ao lead.
    Usado tanto pelo job de contrato (não-listas) quanto pelo router
    quando um lead de lista envia o extrato detalhado.

    Retorna True se o contrato foi criado e enviado com sucesso.
    """
    card_id   = card.get("id", "")
    nome      = get_name(card)
    phone     = get_phone(card)
    adm       = get_adm(card)
    lista_src = is_lista(card)

    logger.info("generate_and_send_contract: card=%s | %s | adm=%s", card_id[:8], nome, adm)

    template_token = get_template_for_adm(adm)
    if not template_token:
        logger.warning("Sem template ZapSign para adm '%s' (card %s)", adm, card_id[:8])
        await _notify_team(
            f"⚠️ *Contrato sem template ZapSign*\n"
            f"Lead: {nome}\nAdministradora: {adm}\n"
            f"Por favor, gere o contrato manualmente."
        )
        return False

    sign_url = None
    doc_token = None
    try:
        lead_signer = {
            "name":  nome,
            "email": card.get("Email", ""),
            "phone": phone,
        }
        async with ZapSignClient() as zap:
            doc = await zap.create_from_template(
                template_token=template_token,
                doc_name=f"Contrato - {nome} - {adm}",
                lead_signer=lead_signer,
                form_fields=build_form_fields(card),
            )
        sign_url  = doc.get("lead_sign_url", "")
        doc_token = doc.get("doc_token", "")
    except ZapSignError as e:
        logger.error("Erro ZapSign para card %s: %s", card_id[:8], e)
        if phone:
            if lista_src:
                # Lead já recebeu "aguarde" do agente_contrato — não mandar msg genérica
                # A equipe vai acompanhar e enviar manualmente se necessário
                pass
            else:
                msg_espera = MSG_ERRO_INTERNO.format(nome=nome.split()[0] if nome else "prezado(a)")
                await _send_zapi(card, phone, msg_espera)
        await _notify_team(
            f"❌ *Erro ao gerar contrato ZapSign*\n"
            f"Lead: {nome} | Adm: {adm}\nErro: {e}\n"
            f"⚠️ Enviar link manualmente após resolver."
        )
        return False

    # Envia link ao lead
    if phone and sign_url:
        primeiro_nome = nome.split()[0] if nome else "prezado(a)"
        if lista_src:
            # Contexto: lead já enviou o extrato e aguarda o link
            mensagem = MSG_CONTRATO_LISTA.format(nome=primeiro_nome, sign_url=sign_url)
            await _send_whapi(phone, mensagem)
        else:
            mensagem = MSG_CONTRATO.format(nome=primeiro_nome, sign_url=sign_url)
            await _send_zapi(card, phone, mensagem)

    # Atualiza card e move para ASSINATURA (ou mantém se já estiver lá)
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
            logger.error("Erro FARO ao finalizar contrato card %s: %s", card_id[:8], e)

    await _notify_team(
        f"✅ *Contrato enviado para assinatura*\n"
        f"Lead: {nome}\nAdm: {adm}\nTelefone: {phone or 'não informado'}\n"
        f"Doc: {doc_token[:12] if doc_token else 'N/A'}..."
    )
    return True


# ---------------------------------------------------------------------------
# Job principal
# ---------------------------------------------------------------------------

async def run_contrato() -> None:
    """
    Verifica cards recém-chegados no stage ACEITO e gera contratos via ZapSign.
    Chamado pelo scheduler a cada 5 minutos.
    """
    logger.info("Job contrato: iniciando verificação do stage ACEITO")

    try:
        async with FaroClient() as faro:
            # watch_new() retorna cards que chegaram recentemente no stage
            # (intervalo padrão: 10 minutos — ajuste em FaroClient se necessário)
            cards = await faro.watch_new(Stage.ACEITO)
    except FaroError as e:
        logger.error("Job contrato: erro ao buscar cards do FARO: %s", e)
        return

    if not cards:
        logger.info("Job contrato: nenhum card novo no stage ACEITO")
        return

    cards = filter_test_cards(cards)
    if TEST_MODE:
        logger.info("TEST_MODE ativo: %d card(s) após filtro de teste.", len(cards))
    if not cards:
        return

    logger.info("Job contrato: %d card(s) aguardando contrato", len(cards))

    for card in cards:
        try:
            await _process_card(card)
        except Exception as e:
            logger.exception("Job contrato: erro inesperado no card %s: %s", card.get("id", "?")[:8], e)

        # Pequena pausa entre criações de documento (evita sobrecarga na API ZapSign)
        await asyncio.sleep(3)

    logger.info("Job contrato: ciclo concluído (%d cards processados)", len(cards))
