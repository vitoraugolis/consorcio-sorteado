"""
webhooks/qualificador.py — Qualificação de leads Bazar/Site via análise de extrato

Fluxo completo:
  1. Lead é ativado pelo job ativacao_bazar_site → stage PRIMEIRA_ATIVACAO
  2. Lead responde (texto ou documento/imagem) → router encaminha para cá
  3. Este módulo analisa:
       a) Se recebeu mídia → analisa o extrato via IA com visão
       b) Se recebeu só texto → orienta a enviar o extrato
  4. Resultado da análise de extrato:
       QUALIFICADO       → move para PRECIFICACAO (proposta disparada automaticamente)
       NAO_QUALIFICADO   → envia mensagem gentil de dispensa, move para NAO_QUALIFICADO
       EXTRATO_INCORRETO → orienta o lead sobre como obter o extrato correto (mantém stage)
       RECUSAR_TEXTO     → lead indicou verbalmente que não tem mais cota → PERDIDO
  5. Em caso de erro técnico na análise, notifica equipe e mantém stage atual.

Stages atendidos: PRIMEIRA_ATIVACAO, SEGUNDA_ATIVACAO, TERCEIRA_ATIVACAO, QUARTA_ATIVACAO
Apenas para leads Bazar/Site (is_lista(card) == False).
"""

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config import (
    Stage,
    NOTIFY_PHONES,
    QUALIFICACAO_PERCENTUAL_MAXIMO,
    QUALIFICACAO_VALOR_PAGO_MAXIMO,
)
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError, get_name, get_phone, get_adm, get_fonte,
    load_history, history_append, save_history,
    load_journey, save_journey,
)
from services.slack import slack_error, slack_warning
from services.whapi import WhapiClient, WhapiError
from services.zapi import ZAPIError, get_zapi_for_card

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stages atendidos pelo qualificador
# ---------------------------------------------------------------------------

QUALIFICATION_STAGES = {
    Stage.PRIMEIRA_ATIVACAO,
    Stage.SEGUNDA_ATIVACAO,
    Stage.TERCEIRA_ATIVACAO,
    Stage.QUARTA_ATIVACAO,
}


# ---------------------------------------------------------------------------
# Resultado da análise
# ---------------------------------------------------------------------------

class ExtratoResultado(str, Enum):
    QUALIFICADO        = "QUALIFICADO"
    NAO_QUALIFICADO    = "NAO_QUALIFICADO"
    EXTRATO_INCORRETO  = "EXTRATO_INCORRETO"


@dataclass
class ExtratoAnalise:
    resultado:          ExtratoResultado
    administradora:     Optional[str] = None
    valor_credito:      float = 0.0
    valor_pago:         float = 0.0
    parcelas_pagas:     int = 0
    total_parcelas:     int = 0
    motivo:             str = ""
    tipo_contemplacao:  Optional[str] = None   # "Lance" ou "Sorteio"
    tipo_bem:           Optional[str] = None   # "Imóvel", "Veículo", "Moto", etc.
    grupo:              Optional[str] = None   # ex: "A123"
    cota:               Optional[str] = None   # ex: "042"


# ---------------------------------------------------------------------------
# Prompt de análise de extrato
# ---------------------------------------------------------------------------

EXTRATO_SYSTEM_PROMPT = """
Você é um agente especializado em análise de extratos de consórcio brasileiro.
Sua tarefa é analisar o documento ou imagem enviado e extrair informações-chave
para determinar se a cota é elegível para compra.
""".strip()

EXTRATO_PROMPT_TEMPLATE = """
Analise o documento/imagem de consórcio e extraia as seguintes informações.

REGRAS DE QUALIFICAÇÃO:
- A cota é QUALIFICADA se: valor pago ≤ {percentual_max:.0f}% do crédito
  E valor pago ≤ R$ {valor_max:,.0f}
- A cota é NAO_QUALIFICADA se o valor pago exceder qualquer um desses limites
- O extrato é INCORRETO se:
    • O documento não é um extrato de consórcio (é boleto, contrato ou outro)
    • O extrato está ilegível, cortado ou com informações essenciais ausentes
    • Não é possível identificar o valor do crédito ou o valor pago

NORMALIZAÇÃO DE CAMPOS:
- administradora: "Santander", "Bradesco", "Itaú", "Caixa", "Porto Seguro", "Embracon", "Sicoob", etc.
- tipo_contemplacao: APENAS "Lance" ou "Sorteio"
- tipo_bem: APENAS "Imóvel", "Veículo", "Moto", "Caminhão" ou "Serviço"
- grupo: código alfanumérico do grupo (ex: "A123", "0456")
- cota: número da cota (ex: "042", "1234")

Retorne EXCLUSIVAMENTE um JSON válido (sem markdown, sem texto extra):
{{
  "resultado": "QUALIFICADO|NAO_QUALIFICADO|EXTRATO_INCORRETO",
  "administradora": "nome da administradora ou null",
  "valor_credito": 0.0,
  "valor_pago": 0.0,
  "parcelas_pagas": 0,
  "total_parcelas": 0,
  "motivo": "explicação objetiva em 1 frase",
  "tipo_contemplacao": "Lance|Sorteio|null",
  "tipo_bem": "Imóvel|Veículo|Moto|Caminhão|Serviço|null",
  "grupo": "código do grupo ou null",
  "cota": "número da cota ou null"
}}

Campos numéricos devem ser números (não strings com R$).
""".format(
    percentual_max=QUALIFICACAO_PERCENTUAL_MAXIMO,
    valor_max=QUALIFICACAO_VALOR_PAGO_MAXIMO,
)


# ---------------------------------------------------------------------------
# Mensagens enviadas ao lead
# ---------------------------------------------------------------------------

MSG_PEDE_EXTRATO = (
    "Olá, {nome}! 😊\n\n"
    "Para prosseguirmos com a avaliação da sua cota {adm}, precisamos do "
    "extrato atualizado do seu consórcio.\n\n"
    "Como obter o extrato:\n"
    "• *Santander/Bradesco/Itaú*: pelo app ou internet banking do banco, "
    "em Produtos → Consórcio → Extrato\n"
    "• *Porto Seguro*: no app Porto Seguro, em Consórcio → Extrato de Cota\n"
    "• *Caixa*: no app Caixa, em Meus Produtos → Consórcio\n\n"
    "Pode me enviar uma *foto* ou *PDF* do extrato que eu analiso na hora! 📄"
)

MSG_EXTRATO_INCORRETO = (
    "Obrigada por enviar, {nome}! 😊\n\n"
    "Mas parece que o documento que recebi não é o extrato de consórcio "
    "que preciso. Pode ser um boleto, contrato ou a imagem ficou um pouco "
    "ilegível.\n\n"
    "O que preciso é o *extrato atualizado da cota*, que mostra:\n"
    "• O valor do crédito\n"
    "• Quanto já foi pago\n"
    "• Quantas parcelas faltam\n\n"
    "Tente tirar uma foto clara do documento ou exportar como PDF pelo "
    "aplicativo do banco. Pode me mandar que analiso na hora! 📄"
)

MSG_NAO_QUALIFICADO = (
    "Olá, {nome}! Tudo bem?\n\n"
    "Agradeço por enviar as informações da sua cota {adm} e pelo seu "
    "interesse em negociar conosco.\n\n"
    "Após uma análise criteriosa, infelizmente não conseguimos prosseguir "
    "com a compra dessa cota no momento. O valor já pago excede o nosso "
    "teto de aquisição para este tipo de operação.\n\n"
    "Caso sua situação mude ou queira tentar novamente no futuro, é só "
    "nos chamar. Boa sorte! 😊"
)

MSG_QUALIFICADO = (
    "Ótima notícia, {nome}! ✅\n\n"
    "Analisei o extrato e a sua cota {adm} está dentro dos nossos critérios "
    "de aquisição. Vou preparar uma proposta personalizada para você e "
    "envio em breve!\n\n"
    "Um momento... 😊"
)

MSG_ERRO_ANALISE = (
    "Olá, {nome}! Recebi seu documento, mas houve um pequeno problema "
    "técnico na análise automática. Nossa equipe vai revisar e entrar "
    "em contato em breve! 🙏"
)

# Palavras-chave que indicam recusa verbal (lead não tem mais a cota)
_RECUSA_KEYWORDS = [
    "vendi", "vender", "já vendi", "ja vendi",
    "não tenho mais", "nao tenho mais",
    "transferi", "cancelei", "cancelou", "encerrei",
    "sem interesse", "não quero", "nao quero",
    "me remova", "me tire", "para de enviar", "parem",
]


# ---------------------------------------------------------------------------
# Extração de URL de mídia do payload raw
# ---------------------------------------------------------------------------

def _extract_media_url(raw: dict, media_type: str) -> Optional[str]:
    """
    Extrai a URL de download da mídia do payload bruto do Z-API.

    Z-API retorna a URL no campo:
      raw.message.document.url  (para PDF)
      raw.message.image.url     (para imagem)
      raw.image.url             (formato alternativo)
      raw.document.url          (formato alternativo)
    """
    # Tenta via message object
    message_obj = raw.get("message") or raw.get("messageData") or {}
    if isinstance(message_obj, dict):
        for mtype in ("document", "image", "video"):
            obj = message_obj.get(mtype, {})
            if isinstance(obj, dict) and obj.get("url"):
                return obj["url"]

    # Tenta direto no payload (alguns formatos do Z-API)
    for mtype in ("document", "image"):
        obj = raw.get(mtype, {})
        if isinstance(obj, dict) and obj.get("url"):
            return obj["url"]

    # Tenta campo genérico "mediaUrl" ou "fileUrl"
    return raw.get("mediaUrl") or raw.get("fileUrl") or None


# ---------------------------------------------------------------------------
# Análise de extrato via IA com visão
# ---------------------------------------------------------------------------

async def _analyze_extrato(media_url: str) -> ExtratoAnalise:
    """
    Chama a IA com visão para analisar o extrato.
    Faz fallback para EXTRATO_INCORRETO em caso de erro técnico da IA.
    """
    async with AIClient() as ai:
        try:
            raw_response = await ai.complete_with_image(
                prompt=EXTRATO_PROMPT_TEMPLATE,
                media_url=media_url,
                system=EXTRATO_SYSTEM_PROMPT,
                max_tokens=500,
            )

            # Extrai JSON da resposta
            json_match = re.search(r"\{.*\}", raw_response, re.DOTALL)
            if not json_match:
                raise AIError("Resposta sem JSON válido")

            data = json.loads(json_match.group())
            resultado = ExtratoResultado(data.get("resultado", "EXTRATO_INCORRETO"))

            def _nullable_str(val) -> Optional[str]:
                if not val or str(val).strip().lower() in ("null", "none", ""):
                    return None
                return str(val).strip()

            analise = ExtratoAnalise(
                resultado=resultado,
                administradora=_nullable_str(data.get("administradora")),
                valor_credito=float(data.get("valor_credito") or 0),
                valor_pago=float(data.get("valor_pago") or 0),
                parcelas_pagas=int(data.get("parcelas_pagas") or 0),
                total_parcelas=int(data.get("total_parcelas") or 0),
                motivo=data.get("motivo", ""),
                tipo_contemplacao=_nullable_str(data.get("tipo_contemplacao")),
                tipo_bem=_nullable_str(data.get("tipo_bem")),
                grupo=_nullable_str(data.get("grupo")),
                cota=_nullable_str(data.get("cota")),
            )

            logger.info(
                "Qualificador IA: resultado=%s adm=%s credito=%.0f pago=%.0f | %s",
                resultado.value,
                analise.administradora,
                analise.valor_credito,
                analise.valor_pago,
                analise.motivo[:80],
            )
            return analise

        except (AIError, json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error("Qualificador: erro ao analisar extrato via IA: %s", e)
            raise  # Repassa para o caller decidir o fallback


# ---------------------------------------------------------------------------
# Detecção de recusa verbal por texto
# ---------------------------------------------------------------------------

def _is_verbal_refusal(text: str) -> bool:
    """Retorna True se o texto indica que o lead não tem mais a cota."""
    lower = text.lower()
    return any(kw in lower for kw in _RECUSA_KEYWORDS)


# ---------------------------------------------------------------------------
# Envio de mensagem
# ---------------------------------------------------------------------------

async def _send_message(card: dict, phone: str, message: str) -> None:
    """Envia mensagem ao lead via Z-API (leads Bazar/Site nunca usam Whapi)."""
    try:
        zapi = get_zapi_for_card(card)
        async with zapi as z:
            await z.send_text(phone, message)
    except ZAPIError as e:
        logger.error("Qualificador: erro Z-API ao enviar para %s: %s", phone, e)


async def _notify_team(message: str) -> None:
    """Notifica a equipe interna via Whapi."""
    if not NOTIFY_PHONES:
        return
    try:
        async with WhapiClient() as w:
            for phone in NOTIFY_PHONES:
                await w.send_text(phone, message)
    except WhapiError as e:
        logger.warning("Qualificador: falha ao notificar equipe: %s", e)


# ---------------------------------------------------------------------------
# Handler principal
# ---------------------------------------------------------------------------

async def handle_qualification(card: dict, msg) -> None:
    """
    Entry point do qualificador. Chamado pelo router para leads Bazar/Site
    em stages de ativação (PRIMEIRA → QUARTA_ATIVACAO).

    Args:
        card: Dict completo do card FARO.
        msg:  IncomingMessage normalizado pelo router.
    """
    card_id = card.get("id", "")
    nome    = get_name(card)
    phone   = get_phone(card)
    adm     = get_adm(card)

    if not phone:
        logger.warning("Qualificador: card %s sem telefone, ignorando.", card_id[:8])
        return

    logger.info(
        "Qualificador: card=%s | has_media=%s | media_type=%s | text='%s'",
        card_id[:8],
        msg.media_type is not None,
        msg.media_type,
        (msg.text or "")[:60],
    )

    # Carrega histórico uma vez — todos os branches gravam ao final
    history = load_history(card)
    user_text = msg.text or f"[Enviou {msg.media_type or 'mídia'}]"

    # ── Caso 1: Recusa verbal por texto ──────────────────────────────────────
    if msg.text and _is_verbal_refusal(msg.text):
        logger.info("Qualificador: recusa verbal detectada para card %s", card_id[:8])
        bot_msg = (
            f"Tudo bem, {nome}! Entendido. Caso mude de ideia ou queira "
            f"negociar outra cota no futuro, é só nos chamar. Até mais! 😊"
        )
        await _send_message(card, phone, bot_msg)
        history = history_append(history, "user", msg.text)
        history = history_append(history, "assistant", bot_msg)
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.PERDIDO)
            except FaroError as e:
                logger.error("Qualificador: erro ao mover card para PERDIDO: %s", e)
            await save_history(faro, card_id, history)
        return

    # ── Caso 2: Lead enviou mídia → analisa extrato ───────────────────────────
    if msg.media_type in ("image", "document", "video"):
        media_url = _extract_media_url(msg.raw, msg.media_type)

        if not media_url:
            logger.warning(
                "Qualificador: mídia sem URL no payload (card %s). "
                "Solicitando extrato novamente.",
                card_id[:8],
            )
            bot_msg = MSG_EXTRATO_INCORRETO.format(nome=nome)
            await _send_message(card, phone, bot_msg)
            history = history_append(history, "user", "[Enviou documento — URL não disponível]")
            history = history_append(history, "assistant", bot_msg)
            async with FaroClient() as faro:
                await save_history(faro, card_id, history)
            return

        # Analisa via IA
        try:
            analise = await _analyze_extrato(media_url)
        except Exception as e:
            # Erro técnico: alerta Slack (Guará Lab) e pede paciência ao lead
            logger.error("Qualificador: erro técnico na análise: %s", e)
            bot_msg = MSG_ERRO_ANALISE.format(nome=nome)
            await _send_message(card, phone, bot_msg)
            history = history_append(history, "user", "[Enviou extrato — erro técnico na análise]")
            history = history_append(history, "assistant", bot_msg)
            async with FaroClient() as faro:
                await save_history(faro, card_id, history)
            await slack_error(
                "Falha na análise de extrato (IA Visão)",
                exception=e,
                context={
                    "Cliente": nome,
                    "Telefone": phone,
                    "Administradora": get_adm(card),
                    "Card ID": card.get("id", "")[:12],
                    "Ação": "Analise manualmente o extrato enviado pelo lead.",
                },
            )
            return

        # ── EXTRATO_INCORRETO ────────────────────────────────────────────────
        if analise.resultado == ExtratoResultado.EXTRATO_INCORRETO:
            logger.info("Qualificador: extrato incorreto para card %s — %s", card_id[:8], analise.motivo)
            bot_msg = MSG_EXTRATO_INCORRETO.format(nome=nome)
            await _send_message(card, phone, bot_msg)
            history = history_append(history, "user", "[Enviou documento — não é extrato de consórcio ou ilegível]")
            history = history_append(history, "assistant", bot_msg)
            async with FaroClient() as faro:
                await save_history(faro, card_id, history)
            return

        # ── NAO_QUALIFICADO ──────────────────────────────────────────────────
        if analise.resultado == ExtratoResultado.NAO_QUALIFICADO:
            logger.info(
                "Qualificador: cota NÃO qualificada — card %s | pago=%.0f | credito=%.0f | %s",
                card_id[:8], analise.valor_pago, analise.valor_credito, analise.motivo,
            )
            bot_msg = MSG_NAO_QUALIFICADO.format(nome=nome, adm=adm)
            await _send_message(card, phone, bot_msg)
            history = history_append(
                history, "user",
                f"[Enviou extrato — cota {analise.administradora or adm}, "
                f"crédito R${analise.valor_credito:,.0f}, pago R${analise.valor_pago:,.0f}]",
            )
            history = history_append(history, "assistant", bot_msg)
            async with FaroClient() as faro:
                try:
                    await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
                    await faro.update_card(card_id, {
                        "Motivo dispensa": analise.motivo,
                        "Valor do crédito": str(analise.valor_credito) if analise.valor_credito else "",
                        "Valor pago extrato": str(analise.valor_pago) if analise.valor_pago else "",
                    })
                except FaroError as e:
                    logger.error("Qualificador: erro ao mover card para NAO_QUALIFICADO: %s", e)
                await save_history(faro, card_id, history)
            return

        # ── QUALIFICADO ──────────────────────────────────────────────────────
        if analise.resultado == ExtratoResultado.QUALIFICADO:
            logger.info(
                "Qualificador: cota QUALIFICADA — card %s | pago=%.0f | credito=%.0f | adm=%s",
                card_id[:8], analise.valor_pago, analise.valor_credito, analise.administradora,
            )
            bot_msg = MSG_QUALIFICADO.format(nome=nome, adm=analise.administradora or adm)
            await _send_message(card, phone, bot_msg)
            history = history_append(
                history, "user",
                f"[Enviou extrato — cota {analise.administradora or adm}, "
                f"crédito R${analise.valor_credito:,.0f}, pago R${analise.valor_pago:,.0f}, "
                f"{analise.parcelas_pagas}/{analise.total_parcelas} parcelas]",
            )
            history = history_append(history, "assistant", bot_msg)

            # Atualiza CRM com dados extraídos e move para PRECIFICACAO
            update_fields: dict = {
                "Valor pago extrato": str(analise.valor_pago) if analise.valor_pago else "",
                "Parcelas pagas":     str(analise.parcelas_pagas) if analise.parcelas_pagas else "",
                "Total parcelas":     str(analise.total_parcelas) if analise.total_parcelas else "",
            }
            if analise.valor_credito:
                # "Crédito" é o campo correto no FARO (usado por build_form_fields no ZapSign)
                update_fields["Crédito"] = str(analise.valor_credito)
            if analise.administradora:
                update_fields["Adm"] = analise.administradora
            if analise.tipo_contemplacao:
                update_fields["Tipo contemplação"] = analise.tipo_contemplacao
            if analise.tipo_bem:
                update_fields["Tipo de bem"] = analise.tipo_bem
            if analise.grupo:
                update_fields["Grupo"] = analise.grupo
            if analise.cota:
                update_fields["Cota"] = analise.cota

            # Registra snapshot da jornada na transição para PRECIFICACAO
            journey = load_journey(card)
            journey.update({
                "origem":         get_fonte(card) or "desconhecida",
                "adm":            analise.administradora or adm,
                "credito":        analise.valor_credito,
                "pago_pct":       round(analise.valor_pago / analise.valor_credito * 100, 1)
                                  if analise.valor_credito else 0,
                "qualificado_em": __import__("datetime").date.today().isoformat(),
            })
            if analise.tipo_contemplacao:
                journey["tipo_contemplacao"] = analise.tipo_contemplacao
            if analise.tipo_bem:
                journey["tipo_bem"] = analise.tipo_bem

            async with FaroClient() as faro:
                try:
                    await faro.update_card(card_id, update_fields)
                    await faro.move_card(card_id, Stage.PRECIFICACAO)
                except FaroError as e:
                    logger.error("Qualificador: erro ao mover card para PRECIFICACAO: %s", e)
                await save_history(faro, card_id, history)
                await save_journey(faro, card_id, journey)
            return

    # ── Caso 3: Lead enviou texto sem extrato → solicita ─────────────────────
    logger.info("Qualificador: lead %s enviou texto sem extrato. Solicitando.", card_id[:8])
    bot_msg = MSG_PEDE_EXTRATO.format(nome=nome, adm=adm)
    await _send_message(card, phone, bot_msg)
    history = history_append(history, "user", user_text)
    history = history_append(history, "assistant", bot_msg)
    async with FaroClient() as faro:
        await save_history(faro, card_id, history)
