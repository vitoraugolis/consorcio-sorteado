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
    QUALIFICADOR_MODEL,
)
from services.ai import AIClient, AIError
from services.pdf_extractor import (
    extract_extrato, ExtratoEstruturado,
    PDFInvalido, PDFCorrompido, GeminiError, ExtratorError,
)
from services.faro import (
    FaroClient, FaroError, get_name, get_phone, get_adm, get_fonte,
    load_history, history_append,
    load_journey, save_journey,
)
from services.slack import slack_error, slack_warning
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card, notify_team
from services.session_store import load_history_smart, save_history_smart
from services.safety_car import audit_response

logger = logging.getLogger(__name__)

QUALIFICATION_STAGES = {
    # ── Ativações (Primeira → Quarta) ─────────────────────────────────────────
    # Lead respondeu durante cadência de ativação. Agente qualificador atende.
    Stage.PRIMEIRA_ATIVACAO,
    Stage.SEGUNDA_ATIVACAO,
    Stage.TERCEIRA_ATIVACAO,
    Stage.QUARTA_ATIVACAO,
    # ── Espera ────────────────────────────────────────────────────────────────
    # Lead LP aguardando envio de extrato. Só reage a mídia (texto = silêncio).
    # Incluída aqui para unificar o roteamento de mídia no qualificador.
    Stage.ESPERA,
    # ── Em Contato ────────────────────────────────────────────────────────────
    # Lead em conversa ativa, já enviou algo, aguardando extrato completo.
    Stage.EM_CONTATO,
    # ── TESTES ────────────────────────────────────────────────────────────────
    # Stage de testes — processa normalmente para validar fluxos.
    Stage.TESTES,
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
    TIPO_BEM_NAO_ACEITO = "TIPO_BEM_NAO_ACEITO"


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

MSG_EXTRATO_SEM_CONTEMPLACAO = (
    "Olá, {nome}! 😊\n\n"
    "Recebi o seu extrato e consegui analisar — mas vi que essa cota ainda "
    "*não está contemplada*.\n\n"
    "Aqui na Consórcio Sorteado trabalhamos somente com cotas já contempladas. 🏦\n\n"
    "Caso você tenha alguma outra cota que já esteja contemplada, pode me enviar "
    "o extrato dela que analiso na hora! 📄\n\n"
    "Se não tiver, fica à vontade para nos chamar no futuro caso haja uma nova "
    "contemplação — será um prazer te atender! 🙏"
)

MSG_EXTRATO_SEM_CONTEMPLACAO_SEM_IMAGEM = (
    "Olá, {nome}! 😊\n\n"
    "Recebi o seu extrato e consegui analisar direitinho — mas vi que essa cota ainda "
    "*não está contemplada*.\n\n"
    "Aqui na Consórcio Sorteado trabalhamos somente com cotas já contempladas. 🏦\n\n"
    "Você tem alguma outra cota de consórcio que já esteja contemplada? "
    "Se sim, pode me enviar o extrato dela que analiso na hora! 📄"
)

MSG_EXTRATO_SEM_CONTEMPLACAO_ENCERRAMENTO = (
    "Entendido, {nome}! 😊\n\n"
    "No momento trabalhamos somente com cotas já contempladas, então não conseguimos "
    "fazer uma proposta para essa cota agora.\n\n"
    "Mas fica à vontade para nos chamar no futuro caso haja uma nova contemplação — "
    "será um prazer te atender! 🙏"
)

MSG_EXTRATO_INCORRETO_ESCALADO = (
    "Olá, {nome}! 😊\n\n"
    "Recebi alguns documentos, mas ainda não consegui identificar o extrato "
    "correto da sua cota. Não se preocupe — vou passar seu contato para um "
    "consultor da nossa equipe que vai te ajudar pessoalmente.\n\n"
    "Em breve alguém entra em contato! 🙏"
)

MSG_TIPO_BEM_NAO_ACEITO = (
    "Olá, {nome}! Tudo bem?\n\n"
    "Obrigado por enviar o extrato da sua cota {adm}. 🙏\n\n"
    "Infelizmente, no momento trabalhamos apenas com cotas de *imóvel* — "
    "não fazemos aquisição de cotas de {tipo_bem}.\n\n"
    "{complemento}"
    "Se no futuro você tiver uma cota de imóvel para negociar, pode contar "
    "com a gente! 😊"
)

# Tipos de bem que o sistema NÃO opera — tudo que não for imóvel
BENS_NAO_ACEITOS = {"veículo", "veiculo", "moto", "caminhão", "caminhao", "serviço", "servico"}

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

# Mensagem para leads do fluxo LP/Espera — sem prometer resultado, apenas confirma recebimento
MSG_QUALIFICADO_LP = (
    "Recebemos o seu extrato, {nome}! 📄✅\n\n"
    "Vamos encaminhar para nossa equipe de precificação fazer uma análise completa da sua cota {adm} "
    "e te daremos uma proposta personalizada em breve.\n\n"
    "Qualquer dúvida, é só chamar! 😊"
)

MSG_ERRO_ANALISE = (
    "Olá, {nome}! Recebi seu documento, mas houve um pequeno problema "
    "técnico na análise automática. Nossa equipe vai revisar e entrar "
    "em contato em breve! 🙏"
)

MSG_LINK_EXTERNO = (
    "Obrigada por enviar, {nome}! 😊\n\n"
    "Recebi o link, mas infelizmente não consigo acessar documentos em links externos "
    "(Adobe Acrobat, Google Drive, Dropbox, etc.) — o sistema precisa que o arquivo "
    "seja enviado diretamente aqui no WhatsApp.\n\n"
    "É bem simples:\n"
    "• Abra o app do banco ou a plataforma da administradora\n"
    "• Exporte ou salve o extrato como *PDF ou imagem*\n"
    "• Envie o arquivo diretamente nesta conversa 📎\n\n"
    "Pode mandar que analiso na hora! 📄"
)

# Domínios de serviços que exigem autenticação e não permitem download direto
_LINK_EXTERNO_PATTERNS = re.compile(
    r"https?://(?:"
    r"acrobat\.adobe\.com|"
    r"documentcloud\.adobe\.com|"
    r"drive\.google\.com|"
    r"docs\.google\.com|"
    r"dropbox\.com|"
    r"1drv\.ms|"
    r"onedrive\.live\.com|"
    r"sharepoint\.com|"
    r"icloud\.com"
    r")",
    re.IGNORECASE,
)

def _has_external_link(text: str) -> bool:
    """Retorna True se o texto contém link de serviço externo que requer autenticação."""
    return bool(_LINK_EXTERNO_PATTERNS.search(text or ""))


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
    """
    Extrai a URL de download da mídia do payload do Whapi.
    O Whapi coloca a URL em `document.link`, `image.link`, etc. (não em `.url`).
    """
    # Campos diretos de cada tipo de mídia (Whapi usa .link, não .url)
    for mtype in ("document", "image", "video", "audio", "voice"):
        obj = raw.get(mtype, {})
        if isinstance(obj, dict):
            url = obj.get("link") or obj.get("url")
            if url:
                return url

    # Fallback: dentro de message/messageData
    message_obj = raw.get("message") or raw.get("messageData") or {}
    if isinstance(message_obj, dict):
        for mtype in ("document", "image", "video", "audio", "voice"):
            obj = message_obj.get(mtype, {})
            if isinstance(obj, dict):
                url = obj.get("link") or obj.get("url")
                if url:
                    return url

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
    """
    Analisa extrato de consórcio via pdf_extractor (Gemini 2.5 Flash inline PDF).
    Ponte de compatibilidade: retorna ExtratoAnalise para o fluxo existente.
    """
    from config import QUALIFICACAO_PERCENTUAL_MAXIMO, QUALIFICACAO_VALOR_PAGO_MAXIMO

    try:
        estruturado: ExtratoEstruturado = await asyncio.wait_for(
            extract_extrato(media_url),
            timeout=130.0,  # pdf_extractor já tem retry interno de 120s
        )
    except asyncio.TimeoutError:
        raise AIError("Timeout na análise de extrato (>130s)")
    except (PDFInvalido, PDFCorrompido) as e:
        # PDF ilegível — trata como extrato incorreto para pedir reenvio
        logger.warning("Qualificador: PDF inválido/corrompido: %s", e)
        return ExtratoAnalise(
            resultado=ExtratoResultado.EXTRATO_INCORRETO,
            motivo=str(e),
        )
    except GeminiError as e:
        raise AIError(f"Gemini falhou na análise: {e}")

    dp = estruturado.dados_plano
    rf = estruturado.resumo_financeiro
    co = estruturado.contemplacao

    # ── Detecta tipo de contemplação ──────────────────────────────────────────
    # Fonte 1: campo direto contemplacao.tipo (Gemini preenche quando achou seção)
    # Fonte 2: sit_cobranca — campo normalizado que indica a situação atual da cota
    # Fonte 3: inferência por palavras-chave no sit_cobranca
    tipo_contemplacao: Optional[str] = None

    raw_tipo = (co.tipo or "").strip().lower()
    raw_sit  = (dp.sit_cobranca or "").strip().lower()

    _LANCE_TOKENS  = ("lance", "lances", "por lance", "contemplad lance", "contemplada-lance",
                      "contemplado lance", "valor lance")
    _SORTEIO_TOKENS = ("sorteio", "assembleia", "contemplad sorteio", "contemplada-sorteio",
                       "contemplado sorteio", "por sorteio")
    _NAO_CONT_TOKENS = ("não contemplad", "nao contemplad", "ativa", "vigente",
                        "em dia", "regular", "normal", "nenhum")

    def _has_token(text: str, tokens: tuple) -> bool:
        return any(t in text for t in tokens)

    if _has_token(raw_tipo, _LANCE_TOKENS) or _has_token(raw_sit, _LANCE_TOKENS):
        tipo_contemplacao = "lance"
    elif _has_token(raw_tipo, _SORTEIO_TOKENS) or _has_token(raw_sit, _SORTEIO_TOKENS):
        tipo_contemplacao = "contemplada-sorteio"
    elif _has_token(raw_sit, _NAO_CONT_TOKENS):
        # Cota ainda não contemplada — marca explicitamente para não inferir errado
        tipo_contemplacao = "nao-contemplada"
    elif co.data_contemplacao:
        # Tem data de contemplação mas sem tipo → assume sorteio (mais comum)
        tipo_contemplacao = "contemplada-sorteio"
        logger.info("Qualificador: tipo_contemplacao inferido como sorteio pela data_contemplacao")
    # else: mantém None — sem informação suficiente

    logger.info(
        "Qualificador: tipo_contemplacao=%r (raw_tipo=%r, raw_sit=%r)",
        tipo_contemplacao, co.tipo, dp.sit_cobranca,
    )

    # ── Bloqueio precoce: LANCE retorna analise direta (sem calcular qualificação) ──
    if tipo_contemplacao == "lance":
        logger.warning(
            "Qualificador: _analyze_extrato detectou LANCE em adm=%s — retorna ExtratoAnalise(LANCE)",
            dp.administradora,
        )
        analise = ExtratoAnalise(
            resultado=ExtratoResultado.QUALIFICADO,  # qualificado em valor mas tipo=lance → tratado no _process_analise
            administradora=dp.administradora,
            valor_credito=dp.valor_credito or 0.0,
            valor_pago=0.0,
            motivo="Contemplação por lance",
            tipo_contemplacao="lance",
            grupo=dp.grupo,
            cota=dp.cota,
        )
        analise._estruturado = estruturado  # type: ignore[attr-defined]
        return analise

    # ── Bloqueio: cota não contemplada → EXTRATO_INCORRETO para LP, NAO_QUALIFICADO para demais ──
    if tipo_contemplacao == "nao-contemplada":
        logger.info(
            "Qualificador: cota não contemplada (adm=%s) — EXTRATO_INCORRETO para sinalizar reenvio correto",
            dp.administradora,
        )
        analise = ExtratoAnalise(
            resultado=ExtratoResultado.EXTRATO_INCORRETO,
            administradora=dp.administradora,
            motivo=(
                f"Extrato indica que a cota {dp.administradora or ''} ainda não foi contemplada "
                f"(situação: '{dp.sit_cobranca or 'sem informação'}'). "
                f"Precisamos do extrato de uma cota já contemplada."
            ),
            tipo_contemplacao="nao-contemplada",
            grupo=dp.grupo,
            cota=dp.cota,
        )
        analise._estruturado = estruturado  # type: ignore[attr-defined]
        return analise

    # Extrai valor pago do resumo_financeiro.valores_pagos.total_pago
    valor_pago: float = 0.0
    if rf.valores_pagos:
        valor_pago = float(rf.valores_pagos.get("total_pago") or 0)

    # Para cotas contempladas, usa crédito corrigido (atualizado na data de contemplação)
    # que é o valor real disponível para negociação. Fallback para valor_credito original.
    _credito_corrigido = (estruturado.contemplacao.credito_corrigido or 0.0) if estruturado.contemplacao else 0.0
    valor_credito: float = _credito_corrigido if _credito_corrigido > 0 else (dp.valor_credito or 0.0)
    if _credito_corrigido > 0 and _credito_corrigido != (dp.valor_credito or 0.0):
        logger.info(
            "Qualificador: usando crédito corrigido=%.0f (original=%.0f) para precificação",
            _credito_corrigido, dp.valor_credito or 0.0,
        )
    administradora: Optional[str] = dp.administradora
    meses_pagos: int = dp.meses_pagos or rf.parcelas_pagas or 0
    total_parcelas: int = (dp.prazo_grupo_meses
                          or (meses_pagos + (dp.meses_a_pagar or 0))
                          or 0)

    # Se o Gemini não retornou dados essenciais, o documento provavelmente não é um extrato
    if valor_credito == 0 and valor_pago == 0 and not administradora:
        logger.info("Qualificador: campos essenciais ausentes — extrato incorreto")
        return ExtratoAnalise(
            resultado=ExtratoResultado.EXTRATO_INCORRETO,
            motivo="Não foi possível identificar administradora, crédito ou valor pago",
        )

    # Regra de qualificação: pago ≤ X% do crédito E pago ≤ R$ Y
    qualificado = False
    motivo = ""
    if valor_credito > 0:
        pct_pago = (valor_pago / valor_credito) * 100
        dentro_percentual = pct_pago <= QUALIFICACAO_PERCENTUAL_MAXIMO
        dentro_valor = valor_pago <= QUALIFICACAO_VALOR_PAGO_MAXIMO
        qualificado = dentro_percentual and dentro_valor
        if not qualificado:
            if not dentro_percentual:
                motivo = (f"Valor pago ({pct_pago:.1f}%) excede o teto de "
                          f"{QUALIFICACAO_PERCENTUAL_MAXIMO:.0f}% do crédito")
            else:
                motivo = (f"Valor pago R${valor_pago:,.0f} excede o teto de "
                          f"R${QUALIFICACAO_VALOR_PAGO_MAXIMO:,.0f}")
        else:
            motivo = (f"Cota elegível: pago {pct_pago:.1f}% do crédito "
                      f"(R${valor_pago:,.0f} de R${valor_credito:,.0f})")
    else:
        # Sem crédito identificado — não qualifica mas marca como incorreto
        # para dar uma segunda chance ao lead
        return ExtratoAnalise(
            resultado=ExtratoResultado.EXTRATO_INCORRETO,
            motivo="Valor do crédito não identificado no extrato",
        )

    resultado = ExtratoResultado.QUALIFICADO if qualificado else ExtratoResultado.NAO_QUALIFICADO

    # Mapeamento de produto para tipo_bem legível
    produto_map = {
        "IMOVEL": "Imóvel", "AUTOMOVEL": "Veículo",
        "MOTO": "Moto", "CAMINHAO": "Caminhão", "SERVICO": "Serviço",
    }
    tipo_bem = produto_map.get((dp.produto or "").upper(), dp.produto)

    # ── Bloqueio por tipo de bem: só operamos imóvel ──────────────────────────
    if tipo_bem and tipo_bem.lower() in BENS_NAO_ACEITOS:
        logger.info(
            "Qualificador: tipo de bem '%s' não operado — bloqueando cota adm=%s",
            tipo_bem, administradora,
        )
        return ExtratoAnalise(
            resultado=ExtratoResultado.TIPO_BEM_NAO_ACEITO,
            administradora=administradora,
            valor_credito=valor_credito,
            valor_pago=valor_pago,
            motivo=f"Tipo de bem '{tipo_bem}' não operado — trabalhamos apenas com imóvel",
            tipo_bem=tipo_bem,
            grupo=dp.grupo,
            cota=dp.cota,
        )

    analise = ExtratoAnalise(
        resultado=resultado,
        administradora=administradora,
        valor_credito=valor_credito,
        valor_pago=valor_pago,
        parcelas_pagas=meses_pagos,
        total_parcelas=total_parcelas,
        motivo=motivo,
        tipo_contemplacao=tipo_contemplacao,
        tipo_bem=tipo_bem,
        grupo=dp.grupo,
        cota=dp.cota,
    )

    logger.info(
        "Qualificador: resultado=%s adm=%s credito=%.0f pago=%.0f confidence=%.2f tipo_cont=%s | %s",
        resultado.value, administradora, valor_credito, valor_pago,
        estruturado.confidence_score, tipo_contemplacao, motivo[:80],
    )

    # Guarda o ExtratoEstruturado completo no analise para uso no update_fields abaixo
    analise._estruturado = estruturado  # type: ignore[attr-defined]
    return analise


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
    await notify_team(message)


# ---------------------------------------------------------------------------
# Handler principal
# ---------------------------------------------------------------------------

async def _gerar_msg_pede_extrato(nome: str, adm: str, card: dict, history: list) -> str:
    """Gera pedido de extrato personalizado via IA. Fallback: MSG_PEDE_EXTRATO."""
    from services.faro import history_to_text as _htt, build_card_context as _bcc
    system = (
        "Voce e Manuela, consultora da Consorcio Sorteado. Precisa pedir ao lead o extrato "
        "atualizado da cota de consorcio contemplada. Tom: caloroso, pessoal, WhatsApp. "
        "Max 5 linhas. Mencione o nome da administradora na mensagem. "
        "Nunca revele ser IA. Retorne apenas o texto da mensagem, sem JSON."
    )
    prompt = (
        f"Lead: {nome} | Administradora: {adm}\n"
        f"Contexto:\n{_bcc(card)}\n"
        f"Historico:\n{_htt(history[-4:] if history else [], max_turns=4)}\n\n"
        f"Escreva a mensagem pedindo o extrato da cota {adm}."
    )
    try:
        async with AIClient() as ai:
            msg = await ai.complete(prompt=prompt, system=system, max_tokens=220)
        if msg and msg.strip():
            return msg.strip()
    except Exception as e:
        logger.warning("Qualificador: IA falhou em _gerar_msg_pede_extrato: %s", e)
    return MSG_PEDE_EXTRATO.format(nome=nome, adm=adm)


async def _gerar_msg_nao_qualificado(nome: str, adm: str, card: dict, history: list) -> str:
    """Gera mensagem de nao qualificacao com empatia via IA. Fallback: MSG_NAO_QUALIFICADO."""
    from services.faro import history_to_text as _htt
    system = (
        "Voce e Manuela, consultora da Consorcio Sorteado. O lead enviou o extrato mas a cota "
        "nao passou nos criterios de aquisicao da empresa. Comunique isso com empatia e respeito, "
        "sem revelar os criterios exatos, deixando a porta aberta para o futuro. "
        "Tom humano, WhatsApp, max 4 linhas. Nunca revele ser IA. Retorne apenas o texto, sem JSON."
    )
    prompt = (
        f"Lead: {nome} | Administradora: {adm}\n"
        f"Historico:\n{_htt(history[-4:] if history else [], max_turns=4)}\n\n"
        f"Escreva a mensagem informando que nao foi possivel prosseguir com a compra da cota."
    )
    try:
        async with AIClient() as ai:
            msg = await ai.complete(prompt=prompt, system=system, max_tokens=200)
        if msg and msg.strip():
            return msg.strip()
    except Exception as e:
        logger.warning("Qualificador: IA falhou em _gerar_msg_nao_qualificado: %s", e)
    return MSG_NAO_QUALIFICADO.format(nome=nome, adm=adm)


async def _gerar_msg_qualificado(nome: str, adm: str, card: dict, history: list) -> str:
    """Gera mensagem de qualificacao positiva com entusiasmo via IA. Fallback: MSG_QUALIFICADO."""
    from services.faro import history_to_text as _htt
    system = (
        "Voce e Manuela, consultora da Consorcio Sorteado. O lead tem uma cota que passou nos "
        "criterios de aquisicao. De a boa noticia com entusiasmo genuino e informe que a proposta "
        "personalizada chegara em breve. Tom: animado, caloroso, WhatsApp. Max 3 linhas. "
        "Nunca revele ser IA. Retorne apenas o texto, sem JSON."
    )
    prompt = (
        f"Lead: {nome} | Administradora: {adm}\n"
        f"Historico:\n{_htt(history[-4:] if history else [], max_turns=4)}\n\n"
        f"Escreva a mensagem comemorando que a cota foi aprovada e que a proposta chegara em breve."
    )
    try:
        async with AIClient() as ai:
            msg = await ai.complete(prompt=prompt, system=system, max_tokens=150)
        if msg and msg.strip():
            return msg.strip()
    except Exception as e:
        logger.warning("Qualificador: IA falhou em _gerar_msg_qualificado: %s", e)
    return MSG_QUALIFICADO.format(nome=nome, adm=adm)


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
                await faro.update_card(card_id, {
                    "Motivo de perda": "SEM_INTERESSE — recusa verbal antes de enviar extrato"
                })
                await faro.move_card(card_id, Stage.PERDIDO)
            except FaroError as e:
                logger.error("Qualificador: erro ao mover card para PERDIDO: %s", e)
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        return

    # ── Caso 2: Mídia → buffer 30s + analisa extrato(s) ──────────────────────
    if msg.media_type in ("image", "document", "video"):
        media_url = _extract_media_url(msg.raw, msg.media_type)

        if not media_url:
            logger.warning(
                "Qualificador: mídia sem URL no payload (card %s). raw[:200]=%s",
                card_id[:8], str(msg.raw)[:200],
            )
            # Registra no histórico que o lead tentou enviar algo (sem URL disponível)
            history = history_append(history, "user", f"[Enviou {msg.media_type or 'mídia'} — URL indisponível no payload]")
            # Conta como tentativa incorreta mesmo sem URL
            erros = int(journey.get("extrato_incorreto_count", 0)) + 1
            journey["extrato_incorreto_count"] = erros
            await _handle_extrato_incorreto(card, card_id, phone, nome, history, journey, erros)
            return

        # ── Buffer de 30s: aguarda possíveis imagens adicionais do mesmo lead ──
        from services.session_store import push_media_buffer, pop_media_buffer, media_buffer_ttl
        entry = {"url": media_url, "media_type": msg.media_type, "raw": msg.raw or {}}
        buf_size = await push_media_buffer(phone, entry)
        logger.info(
            "Qualificador: card %s — mídia #%d enfileirada (url=%s…)",
            card_id[:8], buf_size, media_url[:60],
        )

        if buf_size == 1:
            # Primeira imagem deste lote: aguarda 30s antes de processar
            logger.info("Qualificador: card %s — aguardando 30s por possíveis imagens adicionais.", card_id[:8])
            await asyncio.sleep(30)

            # Após a espera, drena tudo que chegou
            lote = await pop_media_buffer(phone)
            if not lote:
                # Buffer expirou por TTL sem nenhuma entrada — usa a url original
                lote = [entry]
        else:
            # Imagem adicional chegou durante a janela; a task original vai processar tudo
            logger.info(
                "Qualificador: card %s — imagem adicional (#%d) adicionada ao buffer; task original processará.",
                card_id[:8], buf_size,
            )
            return

        logger.info(
            "Qualificador: card %s — processando lote de %d imagem(ns).",
            card_id[:8], len(lote),
        )

        # Analisa cada imagem em paralelo
        async def _safe_analyze(e: dict) -> tuple[dict, ExtratoAnalise | None]:
            try:
                return e, await _analyze_extrato(e["url"])
            except Exception as exc:
                logger.error("Qualificador: erro na análise de %s: %s", e["url"][:60], exc)
                return e, None

        resultados = await asyncio.gather(*[_safe_analyze(e) for e in lote])

        # Agrupa por cota (adm + crédito similar = mesma cota, multi-página)
        grupos: list[list[tuple[dict, ExtratoAnalise]]] = []
        for entry_r, analise_r in resultados:
            if analise_r is None or analise_r.resultado == ExtratoResultado.EXTRATO_INCORRETO:
                continue  # trata incorretos separado abaixo
            colocado = False
            for grupo in grupos:
                ref_entry, ref_analise = grupo[0]
                mesma_adm = (
                    (analise_r.administradora or "").lower() ==
                    (ref_analise.administradora or "").lower()
                    and (analise_r.administradora or "") != ""
                )
                credito_similar = (
                    ref_analise.valor_credito > 0
                    and abs(analise_r.valor_credito - ref_analise.valor_credito)
                    / ref_analise.valor_credito < 0.05  # 5% de tolerância
                ) if ref_analise.valor_credito > 0 else analise_r.valor_credito == 0
                mesma_cota_grupo = (
                    analise_r.grupo and ref_analise.grupo
                    and analise_r.grupo == ref_analise.grupo
                    and analise_r.cota and ref_analise.cota
                    and analise_r.cota == ref_analise.cota
                )
                if mesma_cota_grupo or (mesma_adm and credito_similar):
                    grupo.append((entry_r, analise_r))
                    colocado = True
                    break
            if not colocado:
                grupos.append([(entry_r, analise_r)])

        # Incorretos / sem URL — conta erros
        incorretos = [
            (e, a) for e, a in resultados
            if a is None or a.resultado == ExtratoResultado.EXTRATO_INCORRETO
        ]

        total_cotas = len(grupos)
        logger.info(
            "Qualificador: card %s — lote=%d imagens | %d cota(s) distinta(s) | %d incorreta(s)",
            card_id[:8], len(lote), total_cotas, len(incorretos),
        )

        # Se nenhuma cota válida, trata como extrato incorreto
        if total_cotas == 0:
            erros = int(journey.get("extrato_incorreto_count", 0)) + len(lote)
            journey["extrato_incorreto_count"] = erros
            # Detecta se todos os incorretos são "nao-contemplada" (demonstrativo sem contemplação)
            motivos_incorretos = [
                (a.tipo_contemplacao or "") for _, a in incorretos if a is not None
            ]
            motivo_predominante = "nao-contemplada" if motivos_incorretos and all(
                "nao-contemplada" in m for m in motivos_incorretos
            ) else ""
            history = history_append(history, "user", "[Enviou documento(s) — não é extrato ou ilegível]")
            await _handle_extrato_incorreto(
                card, card_id, phone, nome, history, journey, erros,
                motivo=motivo_predominante,
            )
            return

        # Processa cada cota distinta
        for idx, grupo in enumerate(grupos):
            # Mescla dados de múltiplas páginas da mesma cota (pega a análise mais completa)
            analise = max(
                [a for _, a in grupo],
                key=lambda a: sum([
                    bool(a.administradora), bool(a.valor_credito), bool(a.valor_pago),
                    bool(a.parcelas_pagas), bool(a.tipo_contemplacao), bool(a.grupo), bool(a.cota),
                ]),
            )
            analise_url = grupo[0][0]["url"]  # URL da imagem mais completa (primeira do grupo)

            is_first = idx == 0
            if is_first:
                target_card_id = card_id
                target_card    = card
            else:
                # Cota adicional → cria novo card no FARO copiando dados do lead
                try:
                    async with FaroClient() as faro_new:
                        novo_card = await faro_new.create_card(
                            title=nome,
                            stage_id=Stage.PRIMEIRA_ATIVACAO,
                            fields={
                                "Telefone":        phone,
                                "Nome do contato": nome,
                                "Fonte":           get_fonte(card) or "",
                                "Adm":             analise.administradora or adm,
                            },
                        )
                    target_card_id = novo_card["id"]
                    target_card    = novo_card
                    logger.info(
                        "Qualificador: cota adicional #%d → novo card %s criado para %s",
                        idx + 1, target_card_id[:8], nome,
                    )
                    await slack_warning(
                        f"📋 Multi-cota detectado\n"
                        f"Lead: *{nome}* | Telefone: `{phone[-6:]}`\n"
                        f"Cota #{idx + 1}: {analise.administradora or '?'} "
                        f"| Crédito: R${analise.valor_credito:,.0f}\n"
                        f"Novo card criado: `{target_card_id[:8]}`",
                        context={"Lead": nome, "Phone": phone, "Adm": analise.administradora},
                    )
                except Exception as e_new:
                    logger.error(
                        "Qualificador: falha ao criar card para cota adicional #%d: %s", idx + 1, e_new
                    )
                    continue

            # Processa esta cota no card alvo
            await _process_analise(
                analise=analise,
                media_url=analise_url,
                card=target_card,
                card_id=target_card_id,
                phone=phone,
                nome=nome,
                adm=adm,
                history=history,
                journey=journey if is_first else load_journey(target_card),
                is_extra_cota=not is_first,
                total_cotas=total_cotas,
            )
        return

    # ── Caso 3: Link externo (Adobe, Drive, Dropbox, etc.) ───────────────────
    if msg.text and _has_external_link(msg.text):
        logger.info("Qualificador: lead %s enviou link externo — solicitando envio direto.", card_id[:8])
        bot_msg = MSG_LINK_EXTERNO.format(nome=nome)
        await _send_message(card, phone, bot_msg, history=history)
        history = history_append(history, "user", user_text)
        history = history_append(history, "assistant", bot_msg)
        async with FaroClient() as faro:
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
            if card.get("stage_id") != Stage.EM_CONTATO:
                try:
                    await faro.move_card(card_id, Stage.EM_CONTATO)
                except FaroError as e:
                    logger.warning("Qualificador: erro ao mover %s para EM_CONTATO: %s", card_id[:8], e)
        return

    # ── Caso 4: Texto sem extrato ─────────────────────────────────────────────
    logger.info("Qualificador: lead %s enviou texto sem extrato. Solicitando.", card_id[:8])
    bot_msg = await _gerar_msg_pede_extrato(nome, adm, card, history)
    await _send_message(card, phone, bot_msg, history=history)
    history = history_append(history, "user", user_text)
    history = history_append(history, "assistant", bot_msg)
    async with FaroClient() as faro:
        await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        # Mover para EM_CONTATO — lead está em conversa ativa aguardando extrato
        if card.get("stage_id") != Stage.EM_CONTATO:
            try:
                await faro.move_card(card_id, Stage.EM_CONTATO)
                logger.info("Qualificador: card %s → EM_CONTATO (aguardando extrato)", card_id[:8])
            except FaroError as e:
                logger.warning("Qualificador: erro ao mover %s para EM_CONTATO: %s", card_id[:8], e)


# ---------------------------------------------------------------------------
# Processamento de uma cota qualificada (card original ou card extra)
# ---------------------------------------------------------------------------

async def _process_analise(
    analise: ExtratoAnalise,
    media_url: str,
    card: dict,
    card_id: str,
    phone: str,
    nome: str,
    adm: str,
    history: list,
    journey: dict,
    is_extra_cota: bool = False,
    total_cotas: int = 1,
) -> None:
    """
    Processa o resultado de análise de extrato para um card específico.
    Chamado pelo handle_qualification para cada cota distinta detectada no lote.
    """
    # ── TIPO_BEM_NAO_ACEITO — cota de veículo/moto/caminhão/serviço ─────────
    if analise.resultado == ExtratoResultado.TIPO_BEM_NAO_ACEITO:
        tipo_bem_label = analise.tipo_bem or "veículo"
        pct_pago = (
            (analise.valor_pago / analise.valor_credito * 100)
            if analise.valor_credito and analise.valor_pago
            else None
        )
        # Complemento: se já pagou muito (>40%), menciona que a cota pode ser difícil de vender
        if pct_pago is not None and pct_pago > 40:
            complemento = (
                f"Notamos que você já pagou {pct_pago:.0f}% do crédito — "
                f"nesse percentual, pode ser mais difícil encontrar comprador no mercado. "
                f"Vale consultar uma administradora ou especialista. 💡\n\n"
            )
        else:
            complemento = ""

        bot_msg = MSG_TIPO_BEM_NAO_ACEITO.format(
            nome=nome,
            adm=analise.administradora or adm,
            tipo_bem=tipo_bem_label,
            complemento=complemento,
        )
        logger.info(
            "Qualificador: tipo_bem=%s não operado — card %s | adm=%s | pago=%.0f/%.0f (%.1f%%)",
            tipo_bem_label, card_id[:8], analise.administradora,
            analise.valor_pago, analise.valor_credito, pct_pago or 0,
        )
        if not is_extra_cota:
            await _send_message(card, phone, bot_msg, history=history)
        history = history_append(
            history, "user",
            f"[Extrato — cota {analise.administradora or adm}, tipo={tipo_bem_label}, "
            f"crédito R${analise.valor_credito:,.0f}, pago R${analise.valor_pago:,.0f}]",
        )
        if not is_extra_cota:
            history = history_append(history, "assistant", bot_msg)
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.PERDIDO)
                await faro.update_card(card_id, {
                    "Motivo dispensa": f"Tipo de bem não operado: {tipo_bem_label}",
                    "Tipo de bem": tipo_bem_label,
                    "Valor do crédito": str(analise.valor_credito) if analise.valor_credito else "",
                    "Valor pago até o momento": str(analise.valor_pago) if analise.valor_pago else "",
                })
            except FaroError as e:
                logger.error("Qualificador: erro ao mover card %s para PERDIDO (tipo_bem): %s", card_id[:8], e)
            if not is_extra_cota:
                await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        return

    # ── NAO_QUALIFICADO ───────────────────────────────────────────────────────
    if analise.resultado == ExtratoResultado.NAO_QUALIFICADO:
        logger.info(
            "Qualificador: cota NÃO qualificada — card %s | pago=%.0f | credito=%.0f | %s",
            card_id[:8], analise.valor_pago, analise.valor_credito, analise.motivo,
        )
        bot_msg = await _gerar_msg_nao_qualificado(nome, analise.administradora or adm, card, history)
        if not is_extra_cota:
            await _send_message(card, phone, bot_msg, history=history)
        history = history_append(
            history, "user",
            f"[Extrato — cota {analise.administradora or adm}, "
            f"crédito R${analise.valor_credito:,.0f}, pago R${analise.valor_pago:,.0f}]",
        )
        if not is_extra_cota:
            history = history_append(history, "assistant", bot_msg)
        async with FaroClient() as faro:
            try:
                await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
                await faro.update_card(card_id, {
                    "Motivo dispensa": analise.motivo,
                    "Valor do crédito": str(analise.valor_credito) if analise.valor_credito else "",
                    "Valor pago até o momento": str(analise.valor_pago) if analise.valor_pago else "",
                })
            except FaroError as e:
                logger.error("Qualificador: erro ao mover card %s para NAO_QUALIFICADO: %s", card_id[:8], e)
            if not is_extra_cota:
                await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        return

    # ── QUALIFICADO ───────────────────────────────────────────────────────────
    if analise.resultado == ExtratoResultado.QUALIFICADO:
        logger.info(
            "Qualificador: cota QUALIFICADA — card %s | pago=%.0f | credito=%.0f | adm=%s",
            card_id[:8], analise.valor_pago, analise.valor_credito, analise.administradora,
        )

        # Bloqueio de contemplação por LANCE
        # Fluxo LP: lead vai para LP_LANCE com sondagem de interesse
        # Demais fluxos: NAO_QUALIFICADO (sem compra de lance)
        _tipo_cont_extrato = (analise.tipo_contemplacao or "").strip().lower()
        if _tipo_cont_extrato in ("lance", "contemplada-lance"):
            logger.warning(
                "Qualificador: card %s — extrato indica LANCE.",
                card_id[:8],
            )
            _fonte_card = (get_fonte(card) or "").lower()
            _is_lp_fonte = "lp" in _fonte_card or "site" in _fonte_card or "landing" in _fonte_card

            if _is_lp_fonte:
                # Fluxo LP: move para LP_LANCE e envia mensagem de sondagem
                from webhooks.agente_lp_lance import MSG_LP_LANCE
                bot_msg = MSG_LP_LANCE.format(nome=nome, adm=analise.administradora or adm)
                await _send_message(card, phone, bot_msg, history=history)
                history = history_append(
                    history, "user",
                    f"[Extrato — contemplação LANCE, adm={analise.administradora or adm}]",
                )
                history = history_append(history, "assistant", bot_msg)
                async with FaroClient() as faro:
                    try:
                        await faro.update_card(card_id, {
                            "Tipo contemplação": analise.tipo_contemplacao or "Lance",
                            "Adm": analise.administradora or adm,
                        })
                        await faro.move_card(card_id, Stage.LP_LANCE)
                    except FaroError as e:
                        logger.error(
                            "Qualificador: erro ao mover card %s (lance LP) para LP_LANCE: %s",
                            card_id[:8], e,
                        )
                    await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
                await slack_warning(
                    f"🟡 *Cota de LANCE detectada — lead movido para LP_LANCE*\n"
                    f"Lead: *{nome}* | Adm: {analise.administradora or adm} | Card: `{card_id[:8]}`\n"
                    f"Fonte: {_fonte_card} | Crédito: R${analise.valor_credito:,.0f}\n"
                    f"Mensagem de sondagem enviada — aguardando resposta do lead.",
                    context={"Card": card_id[:12], "Telefone": phone, "Adm": adm},
                )
            else:
                # Demais fluxos (Bazar, Listas): NAO_QUALIFICADO sem contato adicional
                bot_msg = await _gerar_msg_nao_qualificado(nome, analise.administradora or adm, card, history)
                if not is_extra_cota:
                    await _send_message(card, phone, bot_msg, history=history)
                    history = history_append(
                        history, "user",
                        f"[Extrato — contemplação LANCE, adm={analise.administradora or adm}]",
                    )
                    history = history_append(history, "assistant", bot_msg)
                async with FaroClient() as faro:
                    try:
                        await faro.update_card(card_id, {
                            "Tipo contemplação": analise.tipo_contemplacao or "Lance",
                            "Motivo dispensa": "Cota contemplada por lance — fora do escopo de compra",
                        })
                        await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
                    except FaroError as e:
                        logger.error(
                            "Qualificador: erro ao mover card %s (lance) para NAO_QUALIFICADO: %s",
                            card_id[:8], e,
                        )
                    if not is_extra_cota:
                        await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
                await slack_warning(
                    f"⚠️ Cota de LANCE descartada\n"
                    f"Lead: {nome} | Adm: {analise.administradora or adm} | Card: `{card_id[:8]}`\n"
                    f"Extrato indicou contemplação por lance — movido para Não Qualificado.",
                    context={"Card": card_id[:12], "Telefone": phone, "Adm": adm},
                )
            return

        # Leads em ESPERA (fluxo LP retroativa)
        _stage_atual = card.get("stage_id") or ""
        _veio_de_espera = (_stage_atual == Stage.ESPERA)
        if _veio_de_espera:
            from jobs.ativacao_bazar_site import _qualifica_lp
            adm_extrato = analise.administradora or adm
            _adm_ok, _adm_motivo = _qualifica_lp({
                "Adm": adm_extrato,
                "Tipo contemplação": analise.tipo_contemplacao or card.get("Tipo contemplação") or "",
            })
            if not _adm_ok:
                logger.info(
                    "Qualificador: lead ESPERA — adm '%s' fora da lista LP (%s) → mantém em ESPERA",
                    adm_extrato, _adm_motivo,
                )
                bot_msg = MSG_QUALIFICADO_LP.format(nome=nome, adm=adm_extrato)
                if not is_extra_cota:
                    await _send_message(card, phone, bot_msg, history=history)
                    history = history_append(history, "assistant", bot_msg)
                async with FaroClient() as faro:
                    try:
                        _update: dict = {}
                        if analise.valor_pago:
                            _update["Valor pago até o momento"] = str(analise.valor_pago)
                        # Prefere crédito corrigido quando disponível (cota contemplada)
                        _estruturado_espera: ExtratoEstruturado | None = getattr(analise, "_estruturado", None)
                        _credito_espera = analise.valor_credito
                        if _estruturado_espera and _estruturado_espera.contemplacao.credito_corrigido:
                            _cc = _estruturado_espera.contemplacao.credito_corrigido
                            if _cc > 0:
                                _credito_espera = _cc
                                logger.info(
                                    "Qualificador ESPERA: usando crédito corrigido=%.0f para card %s",
                                    _cc, card_id[:8],
                                )
                        if _credito_espera:
                            _update["Crédito"] = str(_credito_espera)
                        if analise.administradora:
                            _update["Adm"] = analise.administradora
                        if analise.tipo_contemplacao:
                            _update["Tipo contemplação"] = analise.tipo_contemplacao
                        if media_url:
                            _update["Link do Extrato"] = media_url
                        if _update:
                            await faro.update_card(card_id, _update)
                    except FaroError as e:
                        logger.error("Qualificador: erro ao gravar dados ESPERA card %s: %s", card_id[:8], e)
                    if not is_extra_cota:
                        await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
                return

        # Mensagem de confirmação
        if _veio_de_espera:
            bot_msg = MSG_QUALIFICADO_LP.format(nome=nome, adm=analise.administradora or adm)
        elif is_extra_cota:
            # Cota adicional: mensagem específica, não duplica a confirmação
            bot_msg = (
                f"Perfeito, {nome}! Também recebi o extrato da sua cota *{analise.administradora or adm}* "
                f"(crédito R${analise.valor_credito:,.0f}). Vou analisar e retorno em breve! 📋"
            )
        else:
            bot_msg = await _gerar_msg_qualificado(nome, analise.administradora or adm, card, history)

        await _send_message(card if not is_extra_cota else card, phone, bot_msg, history=history)

        if not is_extra_cota:
            history = history_append(
                history, "user",
                f"[Extrato — cota {analise.administradora or adm}, "
                f"crédito R${analise.valor_credito:,.0f}, pago R${analise.valor_pago:,.0f}, "
                f"{analise.parcelas_pagas}/{analise.total_parcelas} parcelas]",
            )
            history = history_append(history, "assistant", bot_msg)

        update_fields: dict = {
            "Valor pago até o momento": str(analise.valor_pago) if analise.valor_pago else "",
            "Parcelas pagas": str(analise.parcelas_pagas) if analise.parcelas_pagas else "",
            "Quantidade total meses": str(analise.total_parcelas) if analise.total_parcelas else "",
        }
        if analise.valor_pago and analise.valor_credito:
            _pct = round(analise.valor_pago / analise.valor_credito * 100, 2)
            update_fields["Porcentagem paga até o momento"] = str(_pct)
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
        if media_url:
            update_fields["Link do Extrato"] = media_url

        # Enriquecimento extra com ExtratoEstruturado
        estruturado: ExtratoEstruturado | None = getattr(analise, "_estruturado", None)
        if estruturado:
            dp = estruturado.dados_plano
            dc = estruturado.dados_cadastrais
            if dp.contrato:
                update_fields["Contrato"] = dp.contrato
            if dp.data_adesao:
                update_fields["Data de adesão"] = dp.data_adesao
            if dp.prazo_grupo_meses:
                update_fields["Prazo do grupo"] = str(dp.prazo_grupo_meses)
            if dp.meses_a_pagar:
                update_fields["Quantidade meses a pagar"] = str(dp.meses_a_pagar)
            if dp.taxa_administracao:
                update_fields["Taxa administração"] = str(dp.taxa_administracao)
            if dp.valor_parcela_atual:
                update_fields["Valor parcela"] = str(dp.valor_parcela_atual)
            if dp.sit_cobranca:
                update_fields["Situação cobrança"] = dp.sit_cobranca
            if dp.bem:
                update_fields["Bem"] = dp.bem
            if dc.cpf:
                update_fields["CPF"] = dc.cpf
            if dc.tipo_pessoa:
                _tp = dc.tipo_pessoa.strip().upper()
                if _tp in ("PF", "CPF", "PESSOA FÍSICA", "PESSOA FISICA"):
                    update_fields["Tipo Pessoa"] = "PF"
                elif _tp in ("PJ", "CNPJ", "PESSOA JURÍDICA", "PESSOA JURIDICA"):
                    update_fields["Tipo Pessoa"] = "PJ"
            if dc.nome and not card.get("Nome do contato"):
                update_fields["Nome do contato"] = dc.nome

            # Crédito corrigido: para Bazar/LP (cotas contempladas), o valor corrigido
            # é o que importa para a negociação. Sobrescreve o crédito original do plano.
            co = estruturado.contemplacao
            if co.credito_corrigido and co.credito_corrigido > 0:
                update_fields["Crédito"] = str(co.credito_corrigido)
                analise.valor_credito = co.credito_corrigido
                logger.info(
                    "Qualificador: usando crédito corrigido=%.0f (original=%.0f) para card %s",
                    co.credito_corrigido, dp.valor_credito or 0, card_id[:8],
                )

            logger.info(
                "Qualificador: enriquecendo FARO com %d campos extras (confidence=%.2f)",
                len(update_fields), estruturado.confidence_score,
            )

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

        async with FaroClient() as faro:
            try:
                await faro.update_card(card_id, update_fields)
                await faro.move_card(card_id, Stage.PRECIFICACAO)
            except FaroError as e:
                logger.error("Qualificador: erro CRÍTICO ao mover card %s para PRECIFICACAO: %s", card_id[:8], e)
                await slack_error(
                    "Falha crítica: lead qualificado não moveu para PRECIFICACAO",
                    exception=e,
                    context={"Card": card_id[:12], "Cliente": nome, "Telefone": phone},
                )
                return
            if not is_extra_cota:
                await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
                await save_journey(faro, card_id, journey)
            else:
                await save_journey(faro, card_id, journey)


# ---------------------------------------------------------------------------
# Gerador de mensagem contextual para cota não contemplada (IA + fallback)
# ---------------------------------------------------------------------------

async def _gerar_msg_nao_contemplada(nome: str, card: dict, history: list) -> str:
    """
    Gera via IA uma mensagem personalizada para o caso em que a cota
    analisada não está contemplada.

    A mensagem deve:
    - Informar que leu o extrato e a cota não está contemplada
    - Explicar que a CS só compra cotas contempladas
    - Perguntar se o lead tem outra cota contemplada
    - Se não tiver, encerrar cordialmente deixando a porta aberta

    Tom: empático, direto, natural — como a Manuela escreveria no WhatsApp.
    Fallback: MSG_EXTRATO_SEM_CONTEMPLACAO se a IA falhar.
    """
    from services.faro import history_to_text, build_card_context

    adm     = card.get("Adm") or "sua administradora"
    credito = card.get("Crédito") or ""
    credito_str = f"R$ {credito}" if credito else "valor a consultar"

    historico_txt = history_to_text(history[:-1] if history else [], max_turns=6)

    system = (
        "Você é Manuela, consultora da Consórcio Sorteado — empresa que COMPRA cotas "
        "contempladas de consórcio diretamente dos proprietários.\n\n"
        "SITUAÇÃO: O lead enviou o extrato da cota e o sistema identificou que ela "
        "NÃO está contemplada. Você precisa comunicar isso com clareza e empatia.\n\n"
        "A mensagem deve:\n"
        "1. Confirmar que recebeu e analisou o extrato\n"
        "2. Informar que a cota ainda não está contemplada\n"
        "3. Explicar brevemente que a CS trabalha somente com cotas já contempladas\n"
        "4. Perguntar se o lead tem alguma outra cota que já esteja contemplada\n"
        "5. Se não tiver, deixar a porta aberta para contato futuro quando houver contemplação\n\n"
        "REGRAS:\n"
        "- Tom empático e natural, como no WhatsApp — sem formalidade excessiva\n"
        "- Máximo 5 linhas curtas. Sem parágrafos longos.\n"
        "- Nunca revele ser IA\n"
        "- Retorne APENAS o texto da mensagem, sem JSON, sem aspas externas"
    )

    prompt = (
        f"Lead: {nome}\n"
        f"Administradora: {adm} | Crédito: {credito_str}\n"
        f"Histórico recente:\n{historico_txt or '(sem histórico)'}\n\n"
        f"Escreva a mensagem informando que a cota não está contemplada."
    )

    try:
        async with AIClient() as ai:
            resposta = await ai.complete(
                prompt=prompt,
                system=system,
                max_tokens=200,
            )
        msg = resposta.strip()
        if msg:
            logger.info("Qualificador: mensagem nao-contemplada gerada pela IA para card %s", card.get("id", "")[:8])
            return msg
    except (AIError, Exception) as e:
        logger.warning("Qualificador: IA falhou para msg nao-contemplada (%s) — usando fallback", e)

    return MSG_EXTRATO_SEM_CONTEMPLACAO.format(nome=nome)


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
    motivo: str = "",
) -> None:
    """
    Gerencia resposta a extratos incorretos ou não contemplados.

    Dois fluxos distintos:
    A) nao-contemplada: cota lida com sucesso mas não está contemplada.
       - Informa claramente que a CS só compra cotas contempladas.
       - Pergunta se o lead tem outra cota contemplada.
       - Se esgotar tentativas (todas nao-contemplada): encerra cordialmente
         e move para PERDIDO (não há humano que resolva — é critério de negócio).
    B) extrato incorreto/ilegível: documento não é extrato ou não pôde ser lido.
       - Até MAX_EXTRATO_INCORRETO tentativas: orienta com imagem de exemplo.
       - Acima do limite: escala para humano (ON_HOLD).
    """
    _sem_contemplacao = "nao-contemplada" in (motivo or "").lower()

    # ── Fluxo A: cota não contemplada ────────────────────────────────────────
    # Independente do número de tentativas: uma única mensagem que informa a
    # situação, deixa a porta aberta para outra cota e encerra cordialmente.
    #
    # Destino por fluxo:
    #   - Lead LP (site/landing page): COTAS_NAO_CONTEMPLADAS (se stage configurado)
    #     → stage especial para acompanhamento; quando a cota for contemplada, reativar.
    #   - Demais fluxos (Bazar, etc.): PERDIDO — não há atendimento humano que resolva.
    if _sem_contemplacao:
        logger.info(
            "Qualificador: card %s — cota não contemplada — encerrando com mensagem única.",
            card_id[:8],
        )
        bot_msg = await _gerar_msg_nao_contemplada(nome, card, history)
        await _send_message(card, phone, bot_msg, history=history)
        history = history_append(history, "assistant", bot_msg)

        # Determina stage de destino
        _fonte_card = (get_fonte(card) or "").lower()
        _is_lp = "lp" in _fonte_card or "site" in _fonte_card or "landing" in _fonte_card
        _stage_dest = (
            Stage.COTAS_NAO_CONTEMPLADAS
            if _is_lp and Stage.COTAS_NAO_CONTEMPLADAS
            else Stage.PERDIDO
        )
        _motivo = (
            "Cota não contemplada (LP) — aguardando contemplação futura"
            if _stage_dest == Stage.COTAS_NAO_CONTEMPLADAS
            else "Cota não contemplada — sem cota elegível no momento"
        )
        logger.info(
            "Qualificador: card %s cota não contemplada → %s (lp=%s)",
            card_id[:8], _stage_dest[:8] if _stage_dest else "PERDIDO", _is_lp,
        )

        async with FaroClient() as faro:
            try:
                if _stage_dest:
                    await faro.move_card(card_id, _stage_dest)
                else:
                    await faro.move_card(card_id, Stage.PERDIDO)
                await faro.update_card(card_id, {
                    "Motivo de perda": _motivo,
                })
            except FaroError as e:
                logger.error("Qualificador: erro ao mover card %s: %s", card_id[:8], e)
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
            await save_journey(faro, card_id, journey)

    # ── Fluxo B: extrato incorreto / ilegível ────────────────────────────────
    elif erros >= MAX_EXTRATO_INCORRETO:
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
        # Orienta com imagem de exemplo
        tem_imagem = os.path.exists(_EXTRATO_EXEMPLO_PATH)
        if tem_imagem:
            bot_msg = MSG_EXTRATO_INCORRETO.format(nome=nome)
            await _send_message(card, phone, bot_msg, history=history)
            await _send_extrato_exemplo(card, phone)
        else:
            bot_msg = MSG_EXTRATO_INCORRETO_SEM_IMAGEM.format(nome=nome)
            await _send_message(card, phone, bot_msg, history=history)
        history = history_append(history, "assistant", bot_msg)
        async with FaroClient() as faro:
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
            await save_journey(faro, card_id, journey)
            if card.get("stage_id") != Stage.EM_CONTATO:
                try:
                    await faro.move_card(card_id, Stage.EM_CONTATO)
                    logger.info("Qualificador: card %s → EM_CONTATO (extrato incorreto, tentativa %d)", card_id[:8], erros)
                except FaroError as e:
                    logger.warning("Qualificador: erro ao mover %s para EM_CONTATO: %s", card_id[:8], e)

    logger.info(
        "Qualificador: extrato incorreto card %s — tentativa %d/%d",
        card_id[:8], erros, MAX_EXTRATO_INCORRETO,
    )
