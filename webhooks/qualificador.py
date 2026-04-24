"""
webhooks/qualificador.py — Qualificação de leads Bazar/Site via análise de extrato
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config import (
    Stage,
    NOTIFY_PHONES,
    PUBLIC_URL,
    QUALIFICACAO_PERCENTUAL_MAXIMO,
    QUALIFICACAO_VALOR_PAGO_MAXIMO,
)
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError, get_name, get_phone, get_adm, get_fonte,
    load_history, history_append,
    load_journey, save_journey,
)
from services.slack import slack_error, slack_warning
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card
from services.session_store import load_history_smart, save_history_smart
from services.safety_car import audit_response

logger = logging.getLogger(__name__)

QUALIFICATION_STAGES = {
    Stage.PRIMEIRA_ATIVACAO,
    Stage.SEGUNDA_ATIVACAO,
    Stage.TERCEIRA_ATIVACAO,
    Stage.QUARTA_ATIVACAO,
}

# Máximo de extratos incorretos antes de escalar para humano
MAX_EXTRATO_INCORRETO = 3

# ---------------------------------------------------------------------------
# Resultado da análise
# ---------------------------------------------------------------------------

class ExtratoResultado(str, Enum):
    QUALIFICADO = "QUALIFICADO"
    NAO_QUALIFICADO = "NAO_QUALIFICADO"
    EXTRATO_INCORRETO = "EXTRATO_INCORRETO"


@dataclass
class ExtratoAnalise:
    resultado: ExtratoResultado
    administradora: Optional[str] = None
    valor_credito: float = 0.0
    valor_pago: float = 0.0
    parcelas_pagas: int = 0
    total_parcelas: int = 0
    motivo: str = ""
    tipo_contemplacao: Optional[str] = None
    tipo_bem: Optional[str] = None
    grupo: Optional[str] = None
    cota: Optional[str] = None


# ---------------------------------------------------------------------------
# Prompts
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
  • O documento não é um extrato de consórcio
  • O extrato está ilegível, cortado ou com informações essenciais ausentes
  • Não é possível identificar o valor do crédito ou o valor pago

NORMALIZAÇÃO DE CAMPOS:
- administradora: "Santander", "Bradesco", "Itaú", "Caixa", "Porto Seguro", etc.
- tipo_contemplacao: APENAS "Lance" ou "Sorteio"
- tipo_bem: APENAS "Imóvel", "Veículo", "Moto", "Caminhão" ou "Serviço"

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
# Mensagens
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
    "Veja abaixo um exemplo do extrato correto 👇"
)

MSG_EXTRATO_INCORRETO_SEM_IMAGEM = (
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

MSG_EXTRATO_INCORRETO_ESCALADO = (
    "Olá, {nome}! 😊\n\n"
    "Recebi alguns documentos, mas ainda não consegui identificar o extrato "
    "correto da sua cota. Não se preocupe — vou passar seu contato para um "
    "consultor da nossa equipe que vai te ajudar pessoalmente.\n\n"
    "Em breve alguém entra em contato! 🙏"
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

_RECUSA_KEYWORDS = [
    "vendi", "vender", "já vendi", "ja vendi",
    "não tenho mais", "nao tenho mais",
    "transferi", "cancelei", "cancelou", "encerrei",
    "sem interesse", "não quero", "nao quero",
    "me remova", "me tire", "para de enviar", "parem",
]

# Usa re.UNICODE para tratar acentos corretamente com \b
_RECUSA_PATTERNS = [
    re.compile(r'(?<!\w)' + re.escape(kw) + r'(?!\w)', re.IGNORECASE | re.UNICODE)
    for kw in _RECUSA_KEYWORDS
]

# Caminho local da imagem de exemplo de extrato
_EXTRATO_EXEMPLO_PATH = os.path.join(os.getenv("IMAGES_DIR", "/tmp/cs_images"), "extrato_exemplo.png")


# ---------------------------------------------------------------------------
# Extração de URL de mídia
# ---------------------------------------------------------------------------

def _extract_media_url(raw: dict, media_type: str) -> Optional[str]:
    message_obj = raw.get("message") or raw.get("messageData") or {}
    if isinstance(message_obj, dict):
        for mtype in ("document", "image", "video"):
            obj = message_obj.get(mtype, {})
            if isinstance(obj, dict) and obj.get("url"):
                return obj["url"]
    for mtype in ("document", "image"):
        obj = raw.get(mtype, {})
        if isinstance(obj, dict) and obj.get("url"):
            return obj["url"]
    return raw.get("mediaUrl") or raw.get("fileUrl") or None


# ---------------------------------------------------------------------------
# Imagem de exemplo de extrato
# ---------------------------------------------------------------------------

def _get_extrato_exemplo_url() -> Optional[str]:
    """Retorna URL pública da imagem de exemplo, se disponível."""
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/images/extrato_exemplo.png"
    return None


async def _send_extrato_exemplo(card: dict, phone: str) -> bool:
    """
    Envia imagem de exemplo do extrato correto via Whapi.
    Retorna True se enviou, False se imagem não disponível ou falhou.
    """
    import base64
    from pathlib import Path

    img_path = Path(_EXTRATO_EXEMPLO_PATH)
    if not img_path.exists():
        logger.info("Qualificador: imagem de exemplo não encontrada em %s", _EXTRATO_EXEMPLO_PATH)
        return False

    try:
        b64 = base64.b64encode(img_path.read_bytes()).decode()
        data_uri = f"data:image/png;base64,{b64}"
        async with get_whapi_for_card(card) as w:
            await w.send_image(phone, data_uri, caption="Exemplo de extrato correto 👆")
        return True
    except Exception as e:
        logger.warning("Qualificador: falha ao enviar imagem de exemplo: %s", e)
        return False


# ---------------------------------------------------------------------------
# Análise via IA — com timeout
# ---------------------------------------------------------------------------

async def _analyze_extrato(media_url: str) -> ExtratoAnalise:
    """Analisa extrato via IA com timeout de 90s para evitar travar o event loop."""
    async with AIClient() as ai:
        try:
            raw_response = await asyncio.wait_for(
                ai.complete_with_image(
                    prompt=EXTRATO_PROMPT_TEMPLATE,
                    media_url=media_url,
                    system=EXTRATO_SYSTEM_PROMPT,
                    max_tokens=500,
                ),
                timeout=90.0,
            )

            json_match = re.search(r"\{.*\}", raw_response, re.DOTALL)
            if not json_match:
                raise AIError("Resposta sem JSON válido")

            data = json.loads(json_match.group())
            resultado = ExtratoResultado(data.get("resultado", "EXTRATO_INCORRETO"))

            def _nullable_str(val) -> Optional[str]:
                if not val or str(val).strip().lower() in ("null", "none", ""):
                    return None
                return str(val).strip()

            # Normalização defensiva de tipo_bem
            tipo_bem_raw = _nullable_str(data.get("tipo_bem"))
            tipo_bem_map = {
                "imovel": "Imóvel", "imóvel": "Imóvel",
                "veiculo": "Veículo", "veículo": "Veículo",
                "moto": "Moto",
                "caminhao": "Caminhão", "caminhão": "Caminhão",
                "servico": "Serviço", "serviço": "Serviço",
            }
            tipo_bem_norm = tipo_bem_map.get(
                tipo_bem_raw.lower() if tipo_bem_raw else "",
                tipo_bem_raw,
            )

            analise = ExtratoAnalise(
                resultado=resultado,
                administradora=_nullable_str(data.get("administradora")),
                valor_credito=float(data.get("valor_credito") or 0),
                valor_pago=float(data.get("valor_pago") or 0),
                parcelas_pagas=int(data.get("parcelas_pagas") or 0),
                total_parcelas=int(data.get("total_parcelas") or 0),
                motivo=data.get("motivo", ""),
                tipo_contemplacao=_nullable_str(data.get("tipo_contemplacao")),
                tipo_bem=tipo_bem_norm,
                grupo=_nullable_str(data.get("grupo")),
                cota=_nullable_str(data.get("cota")),
            )

            logger.info(
                "Qualificador IA: resultado=%s adm=%s credito=%.0f pago=%.0f | %s",
                resultado.value, analise.administradora,
                analise.valor_credito, analise.valor_pago, analise.motivo[:80],
            )
            return analise

        except asyncio.TimeoutError:
            raise AIError("Timeout na análise de extrato (>90s)")
        except (AIError, json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error("Qualificador: erro ao analisar extrato via IA: %s", e)
            raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_verbal_refusal(text: str) -> bool:
    lower = text.lower()
    return any(p.search(lower) for p in _RECUSA_PATTERNS)


async def _send_message(card: dict, phone: str, message: str, history: list | None = None) -> None:
    """Envia mensagem ao lead via Whapi com auditoria Safety Car."""
    from services.faro import history_to_text
    historico_txt = history_to_text(history or [], max_turns=6)
    audit = await audit_response(message, card, historico_txt, agente="qualificador")
    message = audit.mensagem_final
    try:
        async with get_whapi_for_card(card) as w:
            await w.send_text(phone, message)
    except WhapiError as e:
        logger.error("Qualificador: erro Whapi ao enviar para %s: %s", phone, e)


async def _notify_team(message: str) -> None:
    if not NOTIFY_PHONES:
        return
    try:
        async with WhapiClient(canal="lista") as w:
            for phone in NOTIFY_PHONES:
                await w.send_text(phone, message)
    except WhapiError as e:
        logger.warning("Qualificador: falha ao notificar equipe: %s", e)


# ---------------------------------------------------------------------------
# Handler principal
# ---------------------------------------------------------------------------

async def handle_qualification(card: dict, msg) -> None:
    card_id = card.get("id", "")
    nome = get_name(card)
    phone = get_phone(card)
    adm = get_adm(card)

    if not phone:
        logger.warning("Qualificador: card %s sem telefone, ignorando.", card_id[:8])
        return

    logger.info(
        "Qualificador: card=%s | has_media=%s | media_type=%s | text='%s'",
        card_id[:8], msg.media_type is not None, msg.media_type, (msg.text or "")[:60],
    )

    history = await load_history_smart(phone, card)
    journey = load_journey(card)
    user_text = msg.text or f"[Enviou {msg.media_type or 'mídia'}]"

    # ── Caso 1: Recusa verbal ────────────────────────────────────────────────
    if msg.text and _is_verbal_refusal(msg.text):
        logger.info("Qualificador: recusa verbal detectada para card %s", card_id[:8])
        bot_msg = (
            f"Tudo bem, {nome}! Entendido. Caso mude de ideia ou queira "
            f"negociar outra cota no futuro, é só nos chamar. Até mais! 😊"
        )
        await _send_message(card, phone, bot_msg, history=history)
        history = history_append(history, "user", msg.text)
        history = history_append(history, "assistant", bot_msg)
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.PERDIDO)
            except FaroError as e:
                logger.error("Qualificador: erro ao mover card para PERDIDO: %s", e)
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        return

    # ── Caso 2: Mídia → analisa extrato ──────────────────────────────────────
    if msg.media_type in ("image", "document", "video"):
        media_url = _extract_media_url(msg.raw, msg.media_type)

        if not media_url:
            logger.warning(
                "Qualificador: mídia sem URL no payload (card %s). raw[:200]=%s",
                card_id[:8], str(msg.raw)[:200],
            )
            # Conta como tentativa incorreta mesmo sem URL
            erros = int(journey.get("extrato_incorreto_count", 0)) + 1
            journey["extrato_incorreto_count"] = erros
            await _handle_extrato_incorreto(card, card_id, phone, nome, history, journey, erros)
            return

        # Analisa via IA
        try:
            analise = await _analyze_extrato(media_url)
        except Exception as e:
            logger.error("Qualificador: erro técnico na análise: %s", e)
            bot_msg = MSG_ERRO_ANALISE.format(nome=nome)
            await _send_message(card, phone, bot_msg, history=history)
            history = history_append(history, "user", "[Enviou extrato — erro técnico na análise]")
            history = history_append(history, "assistant", bot_msg)
            async with FaroClient() as faro:
                await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
            await slack_error(
                "Falha na análise de extrato (IA Visão)",
                exception=e,
                context={
                    "Cliente": nome, "Telefone": phone,
                    "Administradora": adm, "Card ID": card_id[:12],
                    "Ação": "Analise manualmente o extrato enviado pelo lead.",
                },
            )
            return

        # ── EXTRATO_INCORRETO ─────────────────────────────────────────────
        if analise.resultado == ExtratoResultado.EXTRATO_INCORRETO:
            logger.info("Qualificador: extrato incorreto para card %s — %s", card_id[:8], analise.motivo)
            erros = int(journey.get("extrato_incorreto_count", 0)) + 1
            journey["extrato_incorreto_count"] = erros
            history = history_append(history, "user", "[Enviou documento — não é extrato ou ilegível]")
            await _handle_extrato_incorreto(card, card_id, phone, nome, history, journey, erros)
            return

        # ── NAO_QUALIFICADO ───────────────────────────────────────────────
        if analise.resultado == ExtratoResultado.NAO_QUALIFICADO:
            logger.info(
                "Qualificador: cota NÃO qualificada — card %s | pago=%.0f | credito=%.0f | %s",
                card_id[:8], analise.valor_pago, analise.valor_credito, analise.motivo,
            )
            bot_msg = MSG_NAO_QUALIFICADO.format(nome=nome, adm=adm)
            await _send_message(card, phone, bot_msg, history=history)
            history = history_append(
                history, "user",
                f"[Extrato — cota {analise.administradora or adm}, "
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
                await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
            return

        # ── QUALIFICADO ───────────────────────────────────────────────────
        if analise.resultado == ExtratoResultado.QUALIFICADO:
            logger.info(
                "Qualificador: cota QUALIFICADA — card %s | pago=%.0f | credito=%.0f | adm=%s",
                card_id[:8], analise.valor_pago, analise.valor_credito, analise.administradora,
            )
            bot_msg = MSG_QUALIFICADO.format(nome=nome, adm=analise.administradora or adm)
            await _send_message(card, phone, bot_msg, history=history)
            history = history_append(
                history, "user",
                f"[Extrato — cota {analise.administradora or adm}, "
                f"crédito R${analise.valor_credito:,.0f}, pago R${analise.valor_pago:,.0f}, "
                f"{analise.parcelas_pagas}/{analise.total_parcelas} parcelas]",
            )
            history = history_append(history, "assistant", bot_msg)

            update_fields: dict = {
                "Valor pago extrato": str(analise.valor_pago) if analise.valor_pago else "",
                "Parcelas pagas": str(analise.parcelas_pagas) if analise.parcelas_pagas else "",
                "Total parcelas": str(analise.total_parcelas) if analise.total_parcelas else "",
            }
            if analise.valor_credito:
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

            journey.update({
                "origem": get_fonte(card) or "desconhecida",
                "adm": analise.administradora or adm,
                "credito": analise.valor_credito,
                "pago_pct": round(analise.valor_pago / analise.valor_credito * 100, 1)
                if analise.valor_credito else 0,
                "qualificado_em": __import__("datetime").date.today().isoformat(),
            })
            if analise.tipo_contemplacao:
                journey["tipo_contemplacao"] = analise.tipo_contemplacao
            if analise.tipo_bem:
                journey["tipo_bem"] = analise.tipo_bem

            # Tudo num único FaroClient — fix do bug de cliente fechado
            async with FaroClient() as faro:
                try:
                    await faro.update_card(card_id, update_fields)
                    await faro.move_card(card_id, Stage.PRECIFICACAO)
                except FaroError as e:
                    logger.error("Qualificador: erro CRÍTICO ao mover para PRECIFICACAO: %s", e)
                    await slack_error(
                        "Falha crítica: lead qualificado não moveu para PRECIFICACAO",
                        exception=e,
                        context={"Card": card_id[:12], "Cliente": nome, "Telefone": phone},
                    )
                    return
                await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
                await save_journey(faro, card_id, journey)
            return

    # ── Caso 3: Texto sem extrato ─────────────────────────────────────────────
    logger.info("Qualificador: lead %s enviou texto sem extrato. Solicitando.", card_id[:8])
    bot_msg = MSG_PEDE_EXTRATO.format(nome=nome, adm=adm)
    await _send_message(card, phone, bot_msg, history=history)
    history = history_append(history, "user", user_text)
    history = history_append(history, "assistant", bot_msg)
    async with FaroClient() as faro:
        await save_history_smart(phone, history, faro_client=faro, card_id=card_id)


# ---------------------------------------------------------------------------
# Handler de extrato incorreto com contador + escalada
# ---------------------------------------------------------------------------

async def _handle_extrato_incorreto(
    card: dict,
    card_id: str,
    phone: str,
    nome: str,
    history: list,
    journey: dict,
    erros: int,
) -> None:
    """
    Gerencia resposta a extratos incorretos.
    - Até MAX_EXTRATO_INCORRETO tentativas: orienta + envia imagem de exemplo
    - Acima do limite: escala para humano e move para ON_HOLD
    """
    if erros >= MAX_EXTRATO_INCORRETO:
        # Escalada para humano
        logger.warning(
            "Qualificador: card %s atingiu %d extratos incorretos — escalando para humano.",
            card_id[:8], erros,
        )
        bot_msg = MSG_EXTRATO_INCORRETO_ESCALADO.format(nome=nome)
        await _send_message(card, phone, bot_msg, history=history)
        history = history_append(history, "assistant", bot_msg)
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.ON_HOLD)
                await faro.update_card(card_id, {
                    "Motivo dispensa": f"Extrato incorreto após {erros} tentativas — aguarda atendimento humano",
                })
            except FaroError as e:
                logger.error("Qualificador: erro ao mover card %s para ON_HOLD: %s", card_id[:8], e)
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
            await save_journey(faro, card_id, journey)
        await slack_warning(
            f"Lead {nome} enviou extrato incorreto {erros}x — movido para ON_HOLD",
            context={"Card": card_id[:12], "Telefone": phone, "Tentativas": str(erros)},
        )
    else:
        # Orienta + envia imagem de exemplo
        bot_msg = MSG_EXTRATO_INCORRETO.format(nome=nome)
        await _send_message(card, phone, bot_msg, history=history)
        enviou_imagem = await _send_extrato_exemplo(card, phone)
        if not enviou_imagem:
            # Sem imagem: usa mensagem alternativa sem a deixa "veja abaixo"
            await _send_message(card, phone, MSG_EXTRATO_INCORRETO_SEM_IMAGEM.format(nome=nome), history=history)
        history = history_append(history, "assistant", bot_msg)
        async with FaroClient() as faro:
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
            await save_journey(faro, card_id, journey)

    logger.info(
        "Qualificador: extrato incorreto card %s — tentativa %d/%d",
        card_id[:8], erros, MAX_EXTRATO_INCORRETO,
    )
