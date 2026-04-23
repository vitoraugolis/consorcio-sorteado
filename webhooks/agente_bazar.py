"""
webhooks/agente_bazar.py — Agente SDR para leads do fluxo Bazar/Site

Responsável por responder mensagens de texto de leads Bazar/Site nos stages
de ativação (PRIMEIRA → QUARTA_ATIVACAO), conduzindo o lead até o envio
do extrato. A análise do extrato (imagem/PDF) é feita pelo qualificador.py.
"""

import logging
import random
from datetime import datetime, timezone

from config import Stage, NOTIFY_PHONES
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm,
    load_history, history_append, save_history,
    build_card_context,
)
from services.zapi import ZAPIError, get_zapi_for_card
from services.whapi import WhapiClient, WhapiError
from services.slack import slack_error

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"


# ---------------------------------------------------------------------------
# System prompt — princípios, não script
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
Você é Manuela, consultora da Consórcio Sorteado — empresa especializada na COMPRA
de cotas contempladas de consórcio, parceira da Bazar do Consórcio há anos.

SITUAÇÃO: O lead veio pela Bazar do Consórcio e demonstrou interesse em vender sua
cota. Você já enviou uma mensagem inicial apresentando o processo. Agora está
respondendo às mensagens que ele enviou.

DADOS DO LEAD:
{dados_card}

COMO SE COMPORTAR:
- Leia o histórico da conversa antes de responder. Se o lead já disse que vai buscar
  o extrato, não peça de novo — agradeça e aguarde.
- Adapte o tom ao do lead. Se ele é informal, seja informal. Se é mais sério, seja
  mais direta e objetiva.
- Seja paciente e prestativa — leads Bazar muitas vezes não sabem onde buscar o
  extrato. Ajude concretamente.
- Respostas curtas. Máximo 3-4 linhas. Nada de parágrafos longos no WhatsApp.
- Nunca revele ser IA.

OBJETIVO: Ajudar o lead a enviar o extrato atualizado da cota. Sem o extrato, não
conseguimos fazer a análise nem enviar uma proposta.

SE O LEAD NÃO SOUBER COMO OBTER O EXTRATO:
Oriente conforme a administradora — mas só mencione o canal correto para a adm dele,
não liste todas. Se não souber a adm, pergunte primeiro.

Canais por administradora (use apenas o relevante):
- Itaú: ligar para 4004-4224 ou 0800-723-4224 e solicitar "extrato de consórcio"
- Santander: chat.santander.com.br/web/?queue=4c58
- Porto Seguro: aplicativo Porto ou webapp.tech4h.com.br/porto/consorcio
- Bradesco: canalconsorciado.bradesco.com.br/primeiro-acesso
- Demais: app, site ou central de atendimento da administradora

QUANDO O LEAD RECUSAR (cota vendida, sem interesse):
Respeite a decisão. Despeça-se com leveza e convide para o grupo de informações:
{group_link}
Nunca insista após uma recusa clara.

QUANDO PERGUNTAREM SOBRE A EMPRESA (desconfiança):
- CNPJ: 07.931.205/0001-30 | Rua Irmã Carolina 45, Belenzinho-SP
- Compra à vista, direto na conta do lead, ANTES de qualquer transferência.
- A Bazar não compra diretamente por norma do Banco Central — nos indica como
  compradora parceira autorizada.
- Leads da Bazar consentem com contato comercial ao preencher o formulário.

QUANDO QUISER FALAR COM CONSULTOR:
Diga que vai acionar o responsável e classifique como REDIRECIONAR.

FORMATO — JSON puro, sem markdown, sem nada fora do JSON:
{{
  "intent": "AGUARDANDO_EXTRATO|RECUSA_COTA_VENDIDA|RECUSA_SEM_INTERESSE|REDIRECIONAR|OUTRO",
  "response": "mensagem para enviar ao lead"
}}

INTENTS:
- AGUARDANDO_EXTRATO: lead está engajado, conversa seguindo — principal intent
- RECUSA_COTA_VENDIDA: já vendeu, transferiu ou cancelou a cota
- RECUSA_SEM_INTERESSE: não quer vender, pede para parar o contato
- REDIRECIONAR: quer falar com consultor humano ou tem dúvida fora dos dados acima
- OUTRO: mensagem ambígua, saudação — mantenha a conversa com naturalidade
""".strip()


# ---------------------------------------------------------------------------
# Ações de CRM por intent
# ---------------------------------------------------------------------------

async def _handle_intent(intent: str, card: dict) -> None:
    card_id = card.get("id", "")

    if intent in ("RECUSA_COTA_VENDIDA", "RECUSA_SEM_INTERESSE"):
        logger.info("Agente Bazar: %s → PERDIDO (card %s)", intent, card_id[:8])
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.PERDIDO)
            except FaroError as e:
                logger.error("Erro ao mover %s para PERDIDO: %s", card_id[:8], e)

    elif intent == "REDIRECIONAR":
        logger.info("Agente Bazar: REDIRECIONAR → FINALIZACAO_COMERCIAL (card %s)", card_id[:8])
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
            except FaroError as e:
                logger.error("Erro ao mover %s para FINALIZACAO_COMERCIAL: %s", card_id[:8], e)
        from webhooks.negociador import _build_handoff_notification
        notif_msg, notif_phones = _build_handoff_notification(card, "")
        if notif_phones:
            try:
                async with WhapiClient() as w:
                    for np in notif_phones:
                        await w.send_text(np, notif_msg)
            except WhapiError as e:
                logger.warning("Erro ao notificar consultor: %s", e)


# ---------------------------------------------------------------------------
# Fallbacks quando IA falha
# ---------------------------------------------------------------------------

_FALLBACKS_AGUARDANDO = [
    "Perfeito! Assim que você tiver o extrato, pode me enviar aqui mesmo. Qualquer dúvida estou aqui! 😊",
    "Ótimo! Pode me mandar o extrato quando tiver — analiso rapidinho. 🙏",
    "Entendi! Quando tiver o extrato em mãos, é só enviar aqui. Estou à disposição! 😊",
]

_FALLBACKS_OUTRO = [
    "Pode me contar mais? Assim consigo te ajudar melhor. 😊",
    "Entendido! O que você precisar, pode falar. 🙏",
    "Claro! Me fala um pouco mais pra eu entender como posso ajudar.",
]


def _fallback_response(intent: str, nome: str) -> str:
    primeiro = nome.split()[0] if nome else ""
    prefix = f"{primeiro}! " if primeiro else ""

    if intent in ("RECUSA_COTA_VENDIDA", "RECUSA_SEM_INTERESSE"):
        return (
            f"Entendido, {primeiro}! Sem problemas. "
            f"Se quiser acompanhar o mercado no futuro: {_GROUP_LINK} 😊"
        )
    if intent == "REDIRECIONAR":
        return f"Claro{', ' + primeiro if primeiro else ''}! Vou acionar o consultor responsável agora. 🙏"
    if intent == "AGUARDANDO_EXTRATO":
        return random.choice(_FALLBACKS_AGUARDANDO)
    return random.choice(_FALLBACKS_OUTRO)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

async def _respond(card: dict, texto: str) -> None:
    import json
    import re

    card_id = card.get("id", "")
    nome    = get_name(card)
    phone   = get_phone(card)
    adm     = get_adm(card)

    if not phone:
        logger.warning("Agente Bazar: card %s sem telefone.", card_id[:8])
        return

    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)

    history = load_history(card_fresh)
    history = history_append(history, "user", texto)

    system = SYSTEM_PROMPT.format(
        dados_card=build_card_context(card_fresh),
        group_link=_GROUP_LINK,
    )

    intent         = "OUTRO"
    texto_resposta = _fallback_response("OUTRO", nome)

    try:
        async with AIClient() as ai:
            resposta_raw = await ai.complete_with_history(
                history=history,
                system=system,
                max_tokens=350,
            )

        m = re.search(r"\{.*\}", resposta_raw, re.DOTALL)
        if m:
            data           = json.loads(m.group())
            intent         = data.get("intent", "OUTRO").upper()
            texto_resposta = (data.get("response") or "").strip() or _fallback_response(intent, nome)
        else:
            logger.warning("Agente Bazar: resposta sem JSON para card %s.", card_id[:8])

    except (AIError, json.JSONDecodeError, Exception) as e:
        logger.error("Agente Bazar: IA falhou para card %s: %s", card_id[:8], e)
        await slack_error(
            "Falha no Agente SDR Bazar",
            exception=e,
            context={"card": card_id[:12], "phone": phone},
        )
        texto_resposta = _fallback_response(intent, nome)

    # Envia via Z-API (Bazar/Site nunca usa Whapi)
    try:
        zapi = get_zapi_for_card(card_fresh)
        async with zapi as z:
            await z.send_text(phone, texto_resposta)
    except ZAPIError as e:
        logger.error("Agente Bazar: Z-API falhou para %s: %s", phone, e)
        return

    history = history_append(history, "assistant", texto_resposta)
    agora   = datetime.now(timezone.utc).isoformat()

    async with FaroClient() as faro:
        await save_history(faro, card_id, history)
        try:
            await faro.update_card(card_id, {
                "Ultima atividade":    agora,
                "Ultima resposta lead": texto[:500],
            })
        except FaroError:
            pass

    await _handle_intent(intent, card_fresh)

    logger.info(
        "Agente Bazar: card=%s | intent=%s | turns=%d",
        card_id[:8], intent, len(history) // 2,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def handle_message(card: dict, text: str) -> None:
    """Chamado pelo debounce central (via router) após acumulação de mensagens."""
    await _respond(card, text)
