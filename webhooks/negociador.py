"""
webhooks/negociador.py — Motor de negociação com IA

10 intents baseados nos blueprints CS NEGOCIADOR NOVO CRM:
  ACEITAR          → Lead aceita a proposta → ACEITO
  RECUSAR          → Recusa simples → escalada se viável, PERDIDO se não
  MELHORAR_VALOR   → Quer mais dinheiro → salta para máximo se < 27%, reconhece teto se ≥ 27%
  CONTRA_PROPOSTA  → Lead sugere valor específico → avalia contra sequência
  OFERECERAM_MAIS  → Concorrente ofereceu mais → tenta igualar
  NEGOCIAR         → Objeção genérica ao valor → escalada normal
  DUVIDA           → Pergunta respondível com dados do card → responde
  DESCONFIANCA     → "É golpe?" / "Como confio?" → argumentos de credibilidade
  AGENDAR          → Quer humano / pergunta fora dos dados → FINALIZACAO_COMERCIAL
  OUTRO            → Saudação, ambíguo → mantém conversa

Motor de preços:
  - Lê Sequencia_Proposta (lista CSV), Indice da Proposta e Proposta Realizada do FARO
  - Regra dos 27%: se última proposta < 27% do crédito → salta direto para o máximo
  - Se não há valor maior disponível → viavel=False → encerra com elegância
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from config import Stage, NOTIFY_PHONES, CONSULTANT_PHONES as _CONSULTANT_PHONES_CFG
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm, get_fonte, is_lista,
    load_history, history_append, build_card_context, history_to_text,
    load_journey, save_journey,
)
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card
from services.session_store import load_history_smart, save_history_smart

logger = logging.getLogger(__name__)

_GROUP_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

# Limites de precificação
_TETO_PCT    = 0.32   # 32% do crédito = máximo que o "diretor" autoriza
_ABSURDO_PCT = 0.40   # acima de 40% do crédito = proposta indecorosa, bot responde diretamente


# ---------------------------------------------------------------------------
# Mapeamento de consultores → telefone pessoal
# ---------------------------------------------------------------------------

_CONSULTANT_PHONES: dict[str, str] = _CONSULTANT_PHONES_CFG


def _get_consultant_phone(card: dict) -> str | None:
    responsavel = (
        card.get("Responsáveis") or card.get("Responsável") or card.get("Responsavel") or ""
    ).lower().strip()
    for key, phone in _CONSULTANT_PHONES.items():
        if key in responsavel:
            return phone
    return None


def _build_handoff_notification(card: dict, mensagem: str) -> tuple[str, list[str]]:
    nome     = get_name(card)
    adm      = get_adm(card)
    phone    = get_phone(card) or "não informado"
    proposta = card.get("Proposta Realizada") or "a consultar"
    credito  = card.get("Crédito") or "a consultar"
    fonte    = get_fonte(card)

    history = load_history(card)
    resumo_turns = []
    for turn in history[-6:]:
        role = "Lead" if turn.get("role") == "user" else "Manuela"
        resumo_turns.append(f"*{role}:* {turn.get('content', '')[:120]}")
    resumo = "\n".join(resumo_turns) if resumo_turns else f"*Lead:* {mensagem}"

    if "bazar" in fonte:
        canal = f"💬 O lead está no *número da Bazar do Consórcio*.\nNome: {nome} | Telefone: {phone}"
    elif "site" in fonte or "lp" in fonte:
        canal = f"💬 O lead está no *número do Site/LP*.\nNome: {nome} | Telefone: {phone}"
    else:
        canal = f"📞 Lead de *Lista fria* — entre em contato pelo *seu número próprio*.\nTelefone do lead: *{phone}*"

    msg = (
        f"👤 *Lead solicita falar com consultor*\n\n"
        f"*Cliente:* {nome}\n"
        f"*Administradora:* {adm}\n"
        f"*Crédito:* {credito} | *Proposta:* {proposta}\n\n"
        f"*Resumo da conversa:*\n{resumo}\n\n"
        f"*Última mensagem do lead:*\n_{mensagem}_\n\n"
        f"*O que responder:* Apresente-se como consultor(a) responsável, "
        f"confirme que está aqui para ajudar e retome de onde a conversa parou.\n\n"
        f"{canal}"
    )

    consultant_phone = _get_consultant_phone(card)
    targets = [consultant_phone] if consultant_phone else list(NOTIFY_PHONES)
    return msg, targets


# ---------------------------------------------------------------------------
# Motor de precificação
# ---------------------------------------------------------------------------

def _message_has_value(mensagem: str) -> bool:
    """Detecta se a mensagem contém um valor monetário explícito (número relevante)."""
    texto = mensagem.lower()
    # R$ 90.000 / R$350.000,00
    if re.search(r"r\$\s*[\d.,]+", texto):
        return True
    # Formato BR com separador de milhar: 500.000 / 500.000,00 / 1.000.000
    if re.search(r"\b\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?\b", texto):
        return True
    # Número com 4+ dígitos contíguos: 90000, 350000
    if re.search(r"\b\d{4,}\b", texto):
        return True
    # "90 mil" / "350 mil" / "1 milhão"
    if re.search(r"\b\d[\d.,]*\s*(mil|milh[aã]o|k|reais|reai)\b", texto):
        return True
    palavras_valor = ["cem mil", "duzentos mil", "trezentos mil", "quatrocentos mil",
                      "quinhentos mil", "seiscentos mil", "setecentos mil",
                      "oitocentos mil", "novecentos mil"]
    if any(p in texto for p in palavras_valor):
        return True
    return False


def _parse_br_number(raw: str) -> float:
    """
    Converte string numérica no formato BR (milhar=ponto, decimal=vírgula) para float.
    Casos:
      "350.000,00" → 350000.0
      "350.000"    → 350000.0  (ponto seguido de exatamente 3 dígitos = milhar)
      "350,00"     → 350.0
      "350000"     → 350000.0
    """
    raw = raw.strip()
    if "," in raw and "." in raw:
        # ex: "350.000,00" → BR completo
        return float(raw.replace(".", "").replace(",", "."))
    if "," in raw:
        # ex: "350,00" → decimal BR
        return float(raw.replace(",", "."))
    if "." in raw:
        parts = raw.split(".")
        # Ponto com 3 dígitos após = separador de milhar (ex: "350.000")
        if len(parts[-1]) == 3:
            return float(raw.replace(".", ""))
        # Caso contrário trata como decimal (ex: "350.50")
        return float(raw)
    return float(raw)


def _extract_lead_value(mensagem: str, proposta_atual: float = 0.0) -> float:
    """
    Extrai valor monetário mencionado pelo lead.

    Usa proposta_atual como âncora de contexto:
    se o número extraído for menor que 1% da proposta vigente,
    interpreta como estando na mesma ordem de grandeza (multiplica por 1000).

    Ex: proposta=200.000 + lead diz "320" → 320 < 2.000 → retorna 320.000
    """
    texto = mensagem.lower()

    # R$ 350.000 / R$350.000,00 / R$350000
    m = re.search(r"r\$\s*([\d.,]+)", texto)
    if m:
        try:
            return _parse_br_number(m.group(1))
        except ValueError:
            pass
    # Formato BR com separador de milhar: 500.000,00 / 500.000 / 1.000.000
    m = re.search(r"\b(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?)\b", texto)
    if m:
        try:
            return _parse_br_number(m.group(1))
        except ValueError:
            pass
    # "350 mil" / "350mil"
    m = re.search(r"(\d[\d.,]*)\s*mil\b", texto)
    if m:
        try:
            base = float(m.group(1).replace(".", "").replace(",", "."))
            return base * 1000
        except ValueError:
            pass
    # "31k" / "31 k" / "50k" — abreviação comum no WhatsApp
    m = re.search(r"(\d[\d.,]*)\s*k\b", texto)
    if m:
        try:
            base = float(m.group(1).replace(".", "").replace(",", "."))
            return base * 1000
        except ValueError:
            pass
    # número solto com 4+ dígitos (ex: "350000")
    m = re.search(r"\b(\d{4,})\b", texto)
    if m:
        return float(m.group(1))
    # número curto (ex: "320") — usa proposta_atual como âncora
    m = re.search(r"\b(\d{2,3})\b", texto)
    if m:
        val = float(m.group(1))
        if proposta_atual > 0 and val < proposta_atual * 0.01:
            # "320" com proposta de 200k → 320 < 2.000 → interpreta como 320.000
            val = val * 1000
        if val > 0:
            return val
    return 0.0


def _parse_sequencia(card: dict) -> list[float]:
    """Retorna a lista de valores da Sequencia_Proposta do card."""
    raw = (card.get("Sequencia_Proposta") or "").strip()
    result: list[float] = []
    if not raw:
        return result
    for item in re.split(r"[;|\n]", raw):
        for sub in item.split(","):
            sub = sub.strip()
            if sub:
                try:
                    result.append(float(sub.replace(".", "").replace(",", ".")))
                except ValueError:
                    try:
                        result.append(float(sub))
                    except ValueError:
                        pass
    return result


def _build_contraproposta_notification(card: dict, mensagem: str) -> tuple[str, list[str]]:
    """Monta notificação específica para handoff de contraproposta fora do nosso alcance."""
    nome     = get_name(card)
    adm      = get_adm(card)
    phone    = get_phone(card) or "não informado"
    credito  = card.get("Crédito") or "a consultar"
    proposta = card.get("Proposta Realizada") or "a consultar"
    lead_val = _extract_lead_value(mensagem, _parse_currency_value(card.get("Proposta Realizada") or "0"))
    lead_val_fmt = _fmt_currency(lead_val) if lead_val else mensagem[:80]

    history = load_history(card)
    resumo_turns = []
    for turn in history[-6:]:
        role = "Lead" if turn.get("role") == "user" else "Manuela"
        resumo_turns.append(f"*{role}:* {turn.get('content', '')[:120]}")
    resumo = "\n".join(resumo_turns) if resumo_turns else f"*Lead:* {mensagem}"

    msg = (
        f"💰 *Contraproposta acima do nosso teto!*\n\n"
        f"*Cliente:* {nome}\n"
        f"*Administradora:* {adm}\n"
        f"*Crédito da cota:* {credito}\n"
        f"*Nossa última proposta:* {proposta}\n"
        f"*Contraproposta do lead:* *{lead_val_fmt}*\n\n"
        f"*Resumo da conversa:*\n{resumo}\n\n"
        f"*O que fazer:* Avalie se é possível aceitar ou negociar esse valor com o diretor. "
        f"Se sim, entre em contato com o lead ({phone}) e feche o negócio. "
        f"Se não, informe o lead do teto máximo com suas melhores palavras. 🤝"
    )

    consultant_phone = _get_consultant_phone(card)
    targets = [consultant_phone] if consultant_phone else list(NOTIFY_PHONES)
    return msg, targets


def _parse_currency_value(value: str) -> float:
    """Converte string de moeda BR/US para float."""
    if not value:
        return 0.0
    val = str(value).strip().replace("R$", "").strip()
    comma_pos = val.find(",")
    period_pos = val.find(".")
    if "," in val and "." in val:
        if comma_pos < period_pos:   # US: 300,000.00
            val = val.replace(",", "")
        else:                         # BR: 300.000,00
            val = val.replace(".", "").replace(",", ".")
    elif "," in val:
        val = val.replace(".", "").replace(",", ".")
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _fmt_currency(value: float) -> str:
    """Formata float para moeda BR. Ex: 95000.0 → 'R$ 95.000,00'"""
    inteiro = int(value)
    centavos = round((value - inteiro) * 100)
    inteiro_str = f"{inteiro:,}".replace(",", ".")
    return f"R$ {inteiro_str},{centavos:02d}"


def _get_next_proposal(card: dict) -> dict:
    """
    Calcula a próxima proposta com base na Sequencia_Proposta do FARO.

    Returns:
        nova_proposta  float  — valor a oferecer
        indice         int    — novo índice 1-based para gravar no FARO
        viavel         bool   — ainda há propostas maiores depois desta
        pode_escalar   bool   — existe ao menos um valor maior que a proposta atual
        is_max_jump    bool   — saltou para o máximo (regra < 27% do crédito)
    """
    sequencia_raw   = (card.get("Sequencia_Proposta") or "").strip()
    ultima_proposta = _parse_currency_value(card.get("Proposta Realizada") or "0")
    credito         = _parse_currency_value(card.get("Crédito") or "0")

    # Parse da sequência (itens separados por vírgula, ponto como decimal)
    sequencia: list[float] = []
    if sequencia_raw:
        for item in re.split(r"[;|]", sequencia_raw.replace("\n", ",")):
            for sub in item.split(","):
                sub = sub.strip()
                if sub:
                    try:
                        sequencia.append(float(sub.replace(".", "").replace(",", ".")))
                    except ValueError:
                        try:
                            sequencia.append(float(sub))
                        except ValueError:
                            pass

    _no_escalation = {
        "nova_proposta": ultima_proposta,
        "indice": 1,
        "viavel": False,
        "pode_escalar": False,
        "is_max_jump": False,
    }

    if not sequencia:
        return _no_escalation

    # Candidatos: valores estritamente maiores que a última proposta
    candidatos = [(i, v) for i, v in enumerate(sequencia) if v > ultima_proposta]

    if not candidatos:
        return {**_no_escalation, "indice": len(sequencia)}

    # Regra dos 27%: se última < 27% do crédito → salta direto para o máximo disponível
    pct_atual  = (ultima_proposta / credito * 100) if credito > 0 else 100.0
    is_max_jump = pct_atual < 27.0

    if is_max_jump:
        novo_i, nova = max(candidatos, key=lambda x: x[1])
        viavel = any(v > nova for v in sequencia)
        return {
            "nova_proposta": nova,
            "indice": novo_i + 1,
            "viavel": viavel,
            "pode_escalar": True,
            "is_max_jump": True,
        }

    # Escalada normal: próximo valor imediatamente acima
    novo_i, nova = candidatos[0]
    viavel = len(candidatos) > 1
    return {
        "nova_proposta": nova,
        "indice": novo_i + 1,
        "viavel": viavel,
        "pode_escalar": True,
        "is_max_jump": False,
    }


# ---------------------------------------------------------------------------
# Tipos e estruturas
# ---------------------------------------------------------------------------

class Intent(str, Enum):
    ACEITAR         = "ACEITAR"
    RECUSAR         = "RECUSAR"
    MELHORAR_VALOR  = "MELHORAR_VALOR"
    CONTRA_PROPOSTA = "CONTRA_PROPOSTA"
    OFERECERAM_MAIS = "OFERECERAM_MAIS"
    NEGOCIAR        = "NEGOCIAR"
    DUVIDA          = "DUVIDA"
    DESCONFIANCA    = "DESCONFIANCA"
    AGENDAR         = "AGENDAR"
    OUTRO           = "OUTRO"


ACTIVE_STAGES  = {Stage.PRECIFICACAO, Stage.EM_NEGOCIACAO}
SUPPORT_STAGES = {Stage.ASSINATURA}


@dataclass
class NegotiationResult:
    intent:                   Intent
    response_message:         str
    next_stage:               Optional[str]  = None
    notify_team:              bool           = False
    notify_message:           str            = ""
    notify_phones:            Optional[list] = None
    extra_fields:             Optional[dict] = None
    # Resposta atrasada (simula "fui verificar com o diretor")
    delayed_followup:         Optional[str]  = None
    delayed_followup_seconds: int            = 0


# ---------------------------------------------------------------------------
# System Prompt e Classify Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
Você é Manuela, consultora da Consórcio Sorteado — empresa que COMPRA cotas contempladas
de consórcio diretamente dos proprietários há mais de 18 anos.

CONTEXTO DO NEGÓCIO:
- O lead TEM uma cota contemplada e nós estamos OFERECENDO DINHEIRO para comprá-la.
- O lead recebe o pagamento À VISTA na conta dele, ANTES de qualquer transferência.
- A proposta atual está em "Proposta Realizada" — pode haver valores maiores disponíveis.

COMO NEGOCIAR COM INTELIGÊNCIA EMOCIONAL:
- Leia o tom do lead no histórico. Se está animado, combine essa energia. Se está
  hesitante, seja mais cuidadosa e empática. Se está cético, seja mais factual.
- Nunca comece com "Poxa", "Lamento", "Que pena", "Infelizmente" — seja sempre
  confiante e proativa, mesmo ao lidar com objeção de valor.
- Quando o lead pede mais dinheiro, reconheça o pedido com naturalidade (não defensivamente)
  e apresente a melhora como algo que você foi buscar — cria sensação de esforço real.
- Quando há desconfiança, não despeje argumentos — valide o cuidado do lead primeiro,
  depois apresente as evidências de segurança.
- "Vou pensar": entenda o que está por trás. Às vezes é medo, às vezes é real interesse
  em negociar. Pergunte gentilmente ao invés de só pressionar com urgência.

REGRAS DE COMUNICAÇÃO:
- Português brasileiro natural, tom próximo e direto
- Máximo 3 parágrafos. Frases curtas. WhatsApp não é e-mail.
- Emojis com parcimônia — máximo 1 ou 2, só quando encaixam naturalmente
- Nunca revele ser IA. Se perguntada, desvie com leveza.
- Nunca invente dados além do que está no card. Se não souber, classifique como AGENDAR.

INFORMAÇÕES DE CREDIBILIDADE (use quando relevante, não de forma mecânica):
- CNPJ 07.931.205/0001-30 | Rua Irmã Carolina 45, Belenzinho-SP
- Pagamento à vista ANTES da transferência — lead não corre nenhum risco
- Empresa sólida há mais de 18 anos no mercado de cotas contempladas

DESPEDIDA QUANDO ENCERRAR (RECUSAR definitivo):
Seja gentil. Convide para o grupo de informações: {group_link}
""".strip().format(group_link=_GROUP_LINK)


CLASSIFY_PROMPT_TEMPLATE = """
Você é Manuela, consultora da Consórcio Sorteado. Analise a mensagem do lead e retorne JSON.

DADOS DO LEAD:
{dados_card}
Stage atual: {stage_nome}

HISTÓRICO DA CONVERSA (use para calibrar tom e não repetir perguntas já feitas):
{historico}

MENSAGEM ATUAL DO LEAD: "{mensagem}"

REGRA ANTI-ALUCINAÇÃO: use apenas os dados acima. Se não souber, classifique como AGENDAR.

INTENTS — escolha o que melhor descreve a INTENÇÃO real por trás da mensagem:
- ACEITAR:          aceitação INCONDICIONAL ("aceito", "pode fechar", "topei", "bora")
                    ATENÇÃO: "aceito por R$ X" ou "fecho se você me der X" → CONTRA_PROPOSTA
- RECUSAR:          recusa a vender ou pedido para parar o contato
- MELHORAR_VALOR:   quer mais dinheiro mas sem citar valor específico
- CONTRA_PROPOSTA:  cita um VALOR NUMÉRICO como condição ("fecho por 90 mil", "aceito por R$ X")
                    Se apenas pergunta SE pode fazer contraproposta → DUVIDA
- OFERECERAM_MAIS:  outro comprador ou empresa ofereceu valor maior (pode ou não ter citado o valor)
- NEGOCIAR:         objeção ao valor sem especificar quanto quer; quer "negociar" sem dizer o número
- DUVIDA:           pergunta sobre processo, documentação, prazo — respondível com os dados acima
- DESCONFIANCA:     medo de golpe, dúvida sobre idoneidade, pedido de CNPJ/comprovação
- AGENDAR:          quer falar com consultor humano, ligar, ou pergunta fora dos dados disponíveis
- OUTRO:            saudação, agradecimento, "ok", mensagem sem conteúdo decisório

COMO CONSTRUIR A RESPOSTA (campo "response"):
- Escreva como uma pessoa real escreveria no WhatsApp — frases curtas, natural.
- Para intents de valor (RECUSAR / MELHORAR_VALOR / NEGOCIAR / CONTRA_PROPOSTA):
  NÃO cite valores na resposta — o sistema insere a nova proposta depois.
  Apenas prepare uma abertura que reconheça o que o lead disse e sinalize movimento.
  Ex.: "Entendo você, [nome]! Deixa eu ver aqui o que consigo fazer..." (curto, empático)
- Para OFERECERAM_MAIS sem valor: pergunte o valor de forma direta e confiante.
  NÃO diga "que bom" ou "que ótimo" — não celebre o concorrente.
  Tom: "Entendo. Que valor foi esse? Quero ver o que consigo fazer por você."
  Se o valor já foi informado em mensagem anterior (está no histórico), não pergunte de novo —
- Para ACEITAR: seja genuinamente entusiasmada, curta, direta.
- Para DESCONFIANCA: valide o cuidado do lead antes de dar os dados concretos.
- Para OUTRO: mantenha a conversa com naturalidade, não force o tema da proposta.
- NUNCA comece a resposta com "Que pena", "Infelizmente", "Lamento" ou similares.

RETORNE EXCLUSIVAMENTE JSON VÁLIDO (sem markdown, sem texto fora do JSON):
{{
  "intent": "ACEITAR|RECUSAR|MELHORAR_VALOR|CONTRA_PROPOSTA|OFERECERAM_MAIS|NEGOCIAR|DUVIDA|DESCONFIANCA|AGENDAR|OUTRO",
  "reasoning": "1 frase explicando por que esse intent",
  "response": "mensagem para o lead"
}}
"""

# ---------------------------------------------------------------------------
# Classificação por keywords (fallback)
# ---------------------------------------------------------------------------

_KEYWORD_MAP = {
    Intent.ACEITAR: [
        "aceito", "aceitar", "quero fechar", "fechado", "topei", "vamos fechar",
        "pode mandar contrato", "concordo", "combinado", "ok pode ser",
    ],
    Intent.RECUSAR: [
        "não quero", "nao quero", "sem interesse", "não tenho interesse",
        "me tire", "remove", "para de enviar", "parem", "não me contate",
        "bloquear", "cancelar",
    ],
    Intent.MELHORAR_VALOR: [
        "muito baixo", "valor baixo", "preciso de mais", "não compensa",
        "nao compensa", "quero mais", "aumenta", "melhora o valor", "consegue mais",
        "pouco dinheiro", "insuficiente",
    ],
    Intent.CONTRA_PROPOSTA: [
        "aceito por", "quero pelo menos", "me paga", "fecho por", "se pagar",
    ],
    Intent.OFERECERAM_MAIS: [
        "outro lugar", "outra empresa", "me ofereceram", "recebi proposta",
        "ofereceram mais", "concorrente", "fulano pagou", "me deram",
        "recebi uma proposta", "proposta melhor", "oferta melhor", "oferta maior",
        "pagaram mais", "proposta mais alta", "outro comprador",
    ],
    Intent.NEGOCIAR: [
        "negociar", "outro valor", "desconto", "condição melhor", "parcela menor",
        "reduzir", "entrada",
    ],
    Intent.DESCONFIANCA: [
        "golpe", "fraude", "estelionato", "fake", "não confio", "nao confio",
        "como sei", "como confio", "prove", "cnpj", "endereço", "idoneidade",
        "é verdade", "é real", "funciona mesmo",
    ],
    Intent.AGENDAR: [
        "falar com alguém", "falar com pessoa", "consultor", "humano",
        "me ligue", "ligar", "falar por telefone", "passa pra", "transfere",
    ],
    Intent.DUVIDA: [
        "como funciona", "o que é", "como é o processo", "prazo", "taxa",
        "quando recebo", "como recebo", "documentos", "o que precisa",
    ],
}


def _classify_by_keywords(mensagem: str) -> Optional[Intent]:
    texto = mensagem.lower().strip()
    for intent, keywords in _KEYWORD_MAP.items():
        if any(kw in texto for kw in keywords):
            return intent
    return None


# ---------------------------------------------------------------------------
# Construção de resultado com escalada de preço
# ---------------------------------------------------------------------------

def _build_director_response(nome: str, teto_val: float, credito_val: float) -> str:
    """
    Mensagem enviada após delay simulando consulta ao diretor comercial.
    Usada quando a contraproposta do lead é absurda (> 40% do crédito).
    Oferece o teto de 32%, reforça segurança e alerta sobre fraudes de mercado.
    """
    teto_fmt   = _fmt_currency(teto_val)
    credito_fmt = _fmt_currency(credito_val)
    return (
        f"Consegui falar agora com o nosso diretor comercial, {nome}! 💪\n\n"
        f"Para uma cota de {credito_fmt}, o máximo que ele autorizou foi *{teto_fmt}* — "
        f"e é uma concessão especial, já acima do que normalmente praticamos.\n\n"
        f"Um ponto importante que quero reforçar: na Consórcio Sorteado o pagamento é feito "
        f"*à vista, direto na sua conta, ANTES de qualquer transferência da cota*. Você não "
        f"assume nenhum risco. Se outra empresa está oferecendo um valor muito acima disso, "
        f"recomendo desconfiar — o mercado sério não costuma fugir muito desse patamar, e "
        f"propostas tentadoras podem esconder armadilhas. A Consórcio Sorteado tem mais de "
        f"20 anos de mercado exatamente pela nossa seriedade e transparência. 🏆\n\n"
        f"O que você acha de fecharmos por *{teto_fmt}*?"
    )


def _build_result(intent: Intent, ai_response: str, card: dict, mensagem: str = "") -> NegotiationResult:
    """
    Monta o NegotiationResult com ações de CRM e injeta nova proposta quando aplicável.
    """
    nome = get_name(card)
    adm  = get_adm(card)

    # ── ACEITAR ────────────────────────────────────────────────────────────────
    # Guarda: aceitação condicional com valor ("eu fecho se você me der X") → CONTRA_PROPOSTA
    if intent == Intent.ACEITAR and _message_has_value(mensagem):
        _texto = mensagem.lower()
        _condicionais = ["se ", "se você", "caso ", "desde que", "se me ", "se der", "se oferecer"]
        if any(c in _texto for c in _condicionais):
            intent = Intent.CONTRA_PROPOSTA
            # cai nos blocos de CONTRA_PROPOSTA abaixo

    if intent == Intent.ACEITAR:
        return NegotiationResult(
            intent=intent,
            response_message=ai_response,
            next_stage=Stage.ACEITO,
            notify_team=True,
            notify_message=(
                f"🎯 *Lead aceitou a proposta!*\n\n"
                f"*Cliente:* {nome}\n"
                f"*Administradora:* {adm}\n"
                f"*Telefone:* {get_phone(card) or 'não informado'}\n\n"
                f"O sistema vai gerar o contrato automaticamente. ✅"
            ),
        )

    # ── AGENDAR ────────────────────────────────────────────────────────────────
    if intent == Intent.AGENDAR:
        notif_msg, notif_phones = _build_handoff_notification(card, mensagem)
        return NegotiationResult(
            intent=intent,
            response_message=ai_response,
            next_stage=Stage.FINALIZACAO_COMERCIAL,
            notify_team=True,
            notify_message=notif_msg,
            notify_phones=notif_phones,
        )

    # ── DUVIDA / DESCONFIANCA / OUTRO ─────────────────────────────────────────
    if intent in (Intent.DUVIDA, Intent.DESCONFIANCA, Intent.OUTRO):
        return NegotiationResult(
            intent=intent,
            response_message=ai_response,
            next_stage=Stage.EM_NEGOCIACAO,
        )

    # ── OFERECERAM_MAIS — extrai valor do concorrente se já informado ─────────
    if intent == Intent.OFERECERAM_MAIS:
        proposta_val = _parse_currency_value(card.get("Proposta Realizada") or "0")
        competitor_value = _extract_lead_value(mensagem, proposta_val)
        if competitor_value > 0:
            # Lead já informou o valor — trata como contraproposta diretamente
            logger.info(
                "Negociador: OFERECERAM_MAIS com valor=%.0f — tratando como CONTRA_PROPOSTA",
                competitor_value,
            )
            # Reclassifica e passa pelo fluxo de contraproposta
            intent = Intent.CONTRA_PROPOSTA
            # Reusa a resposta da IA mas continua para o bloco CONTRA_PROPOSTA abaixo
        else:
            # Sem valor: IA vai perguntar — mantém
            return NegotiationResult(
                intent=intent,
                response_message=ai_response,
                next_stage=Stage.EM_NEGOCIACAO,
                extra_fields={"Situacao Negociacao": intent.value},
            )

    # ── CONTRA_PROPOSTA sem valor numérico — pede o valor antes de escalar ────
    if intent == Intent.CONTRA_PROPOSTA and not _message_has_value(mensagem):
        return NegotiationResult(
            intent=intent,
            response_message=ai_response,
            next_stage=Stage.EM_NEGOCIACAO,
            extra_fields={"Situacao Negociacao": intent.value},
        )

    # ── CONTRA_PROPOSTA com valor — árvore de decisão completa ───────────────
    if intent == Intent.CONTRA_PROPOSTA and _message_has_value(mensagem):
        import random as _random
        proposta_ctx = _parse_currency_value(card.get("Proposta Realizada") or "0")
        lead_value   = _extract_lead_value(mensagem, proposta_ctx)
        credito_val  = _parse_currency_value(card.get("Crédito") or "0")
        teto_val     = credito_val * _TETO_PCT    if credito_val > 0 else 0.0
        absurdo_val  = credito_val * _ABSURDO_PCT if credito_val > 0 else 0.0
        max_sequencia = max((_parse_sequencia(card) or [0.0]))

        # 1️⃣ Conseguimos cobrir com a sequência → escalada automática
        if max_sequencia > 0 and lead_value <= max_sequencia:
            pass  # cai no bloco de escalada abaixo
        # 2️⃣ Dentro do nosso teto (≤ 32%) mas sem sequência calculada → responde com o teto
        elif teto_val > 0 and lead_value <= teto_val:
            delay = _random.randint(35, 65)
            director_msg = _build_director_response(nome, teto_val, credito_val)
            logger.info(
                "Negociador: CONTRA_PROPOSTA dentro do teto (%.0f%% ≤ 32%%) para %s — "
                "resposta do diretor com teto=%.0f em %ds.",
                (lead_value / credito_val * 100) if credito_val > 0 else 0,
                card.get("id", "")[:8], teto_val, delay,
            )
            return NegotiationResult(
                intent=intent,
                response_message=ai_response,
                next_stage=Stage.EM_NEGOCIACAO,
                extra_fields={"Situacao Negociacao": intent.value},
                delayed_followup=director_msg,
                delayed_followup_seconds=delay,
            )
        # 3️⃣ Proposta indecorosa (> 40% do crédito) → bot responde com 32% após delay
        elif absurdo_val > 0 and lead_value > absurdo_val:
            delay = _random.randint(35, 65)
            director_msg = _build_director_response(nome, teto_val or lead_value * 0.64, credito_val)
            logger.info(
                "Negociador: CONTRA_PROPOSTA absurda (%.0f%% do crédito) para %s — "
                "resposta do diretor em %ds.",
                (lead_value / credito_val * 100) if credito_val > 0 else 0,
                card.get("id", "")[:8], delay,
            )
            return NegotiationResult(
                intent=intent,
                response_message=ai_response,
                next_stage=Stage.EM_NEGOCIACAO,
                extra_fields={"Situacao Negociacao": intent.value},
                delayed_followup=director_msg,
                delayed_followup_seconds=delay,
            )
        # 4️⃣ Acima do teto mas razoável (32-40%) → handoff ao consultor
        else:
            notif_msg, notif_phones = _build_contraproposta_notification(card, mensagem)
            return NegotiationResult(
                intent=intent,
                response_message=ai_response,
                next_stage=Stage.FINALIZACAO_COMERCIAL,
                notify_team=True,
                notify_message=notif_msg,
                notify_phones=notif_phones,
                extra_fields={"Situacao Negociacao": intent.value},
            )

    # ── Intents que envolvem escalada de preço ────────────────────────────────
    # RECUSAR · MELHORAR_VALOR · NEGOCIAR  (e CONTRA_PROPOSTA que passou pelo caso 1️⃣)
    prox = _get_next_proposal(card)

    if not prox["pode_escalar"]:
        # Sem Sequencia_Proposta → não encerra, mantém negociação
        if not (card.get("Sequencia_Proposta") or "").strip():
            logger.warning("Negociador: Sequencia_Proposta vazia para card %s — escalada ignorada.", card.get("id","")[:8])
            return NegotiationResult(
                intent=intent,
                response_message=ai_response,
                next_stage=Stage.EM_NEGOCIACAO,
                extra_fields={"Situacao Negociacao": intent.value},
            )
        # Teto real da sequência atingido — encerra com elegância
        response = (
            f"{ai_response}\n\n"
            f"Esse é o valor máximo que conseguimos oferecer pelo mercado atual. "
            f"Respeito sua decisão e fico à disposição caso mude de ideia. 😊\n\n"
            f"Se quiser acompanhar o mercado: {_GROUP_LINK}"
        )
        return NegotiationResult(
            intent=intent,
            response_message=response,
            next_stage=Stage.PERDIDO,
        )

    # Formata nova proposta e injeta na resposta da IA
    nova_fmt = _fmt_currency(prox["nova_proposta"])
    nome_curto = get_name(card).split()[0] if get_name(card) else ""

    import random as _rand
    if prox["is_max_jump"]:
        _opcoes_max = [
            f"Aqui entre nós: fui direto ao máximo que consigo autorizar — *{nova_fmt}*. "
            f"Pagamento à vista na sua conta, antes de qualquer transferência. "
            f"O que você acha?",
            f"Consultei aqui e consegui ir ao nosso teto: *{nova_fmt}*. "
            f"Essa é a oferta mais alta que temos para essa cota, com pagamento à vista. "
            f"Fechamos?",
            f"Fui buscar o máximo disponível pra você: *{nova_fmt}*. "
            f"Tudo à vista, seguro, direto na sua conta. O que acha, {nome_curto}?",
        ]
        complemento = _rand.choice(_opcoes_max)
    else:
        _opcoes_escala = [
            f"Consegui melhorar pra *{nova_fmt}*. Pagamento à vista, total segurança. Fechamos?",
            f"Fui verificar aqui e consigo chegar a *{nova_fmt}*. O que você acha?",
            f"Boa notícia: consigo ir até *{nova_fmt}*. "
            f"À vista, na sua conta, antes de qualquer transferência. Topamos?",
        ]
        complemento = _rand.choice(_opcoes_escala)

    response = f"{ai_response}\n\n{complemento}"

    extra = {
        "Proposta Realizada": f"{prox['nova_proposta']:.2f}",
        "Indice da Proposta": str(prox["indice"]),
        "Situacao Negociacao": intent.value,
    }

    return NegotiationResult(
        intent=intent,
        response_message=response,
        next_stage=Stage.EM_NEGOCIACAO,
        extra_fields=extra,
    )


# ---------------------------------------------------------------------------
# Fallback de classificação sem IA
# ---------------------------------------------------------------------------

def _fallback_classify(mensagem: str, card: dict) -> NegotiationResult:
    nome   = get_name(card)
    intent = _classify_by_keywords(mensagem) or Intent.OUTRO

    import random as _r
    primeiro = nome.split()[0] if nome else ""
    _pn = f"{primeiro}! " if primeiro else ""

    fallback_responses = {
        Intent.ACEITAR:         _r.choice([
            f"Boa, {_pn}Ótima decisão! 🎉 Já estou encaminhando pra finalizar.",
            f"Que ótimo, {primeiro}! Vou cuidar disso agora mesmo pra você. 🎉",
        ]),
        Intent.RECUSAR:         f"{_pn}Entendo. Deixa eu ver o que ainda consigo fazer antes de encerrarmos...",
        Intent.MELHORAR_VALOR:  _r.choice([
            f"Entendido, {primeiro}. Deixa eu verificar aqui o que consigo...",
            f"Faz sentido, {primeiro}. Vou dar uma olhada no que é possível. Um segundo!",
        ]),
        Intent.CONTRA_PROPOSTA: f"Anotei, {primeiro}. Vou verificar se consigo chegar aí pra você.",
        Intent.OFERECERAM_MAIS: (
            f"Entendo, {primeiro}. Que valor foi esse? "
            f"Quero levar pro nosso diretor e ver o que consigo fazer por você. 💪"
        ),
        Intent.NEGOCIAR:        f"Entendo, {primeiro}. Deixa eu verificar o que consigo melhorar pra você.",
        Intent.DUVIDA:          f"Boa pergunta! Vou te explicar direitinho.",
        Intent.DESCONFIANCA:    (
            f"Faz todo sentido ter cuidado, {primeiro}! "
            f"Somos a Consórcio Sorteado — CNPJ 07.931.205/0001-30, "
            f"Rua Irmã Carolina 45, Belenzinho-SP, mais de 18 anos de mercado. "
            f"O pagamento é feito ANTES da transferência — você não assume risco nenhum. 😊"
        ),
        Intent.AGENDAR:         f"Claro, {primeiro}! Vou acionar um consultor pra falar com você pessoalmente. 🙏",
        Intent.OUTRO:           _r.choice([
            f"Estou aqui, {primeiro}! Como posso te ajudar? 😊",
            f"Pode falar, {primeiro}! O que você precisar.",
        ]),
    }

    ai_response = fallback_responses.get(intent, fallback_responses[Intent.OUTRO])
    return _build_result(intent, ai_response, card, mensagem)


# ---------------------------------------------------------------------------
# Detecção de tom do lead (leve — baseada em padrões textuais)
# ---------------------------------------------------------------------------

def _detect_tom(texto: str) -> str:
    """
    Detecta o tom predominante do lead na mensagem.
    Retorna: "informal", "formal", "ansioso", "desconfiante" ou "" se inconclusivo.
    Usado apenas uma vez (na primeira mensagem) para preencher journey["tom"].
    """
    t = texto.lower()

    # Sinais de informalidade
    informal_signals = ["oi", "oii", "opa", "vlw", "valeu", "blz", "beleza",
                        "tá bom", "ta bom", "show", "top", "boa", "massa",
                        "kk", "haha", "hehe", "rsrs", "kkk"]
    # Sinais de formalidade
    formal_signals = ["prezado", "boa tarde", "bom dia", "boa noite", "agradeço",
                      "solicito", "gostaria", "venho por meio", "conforme"]
    # Ansiedade / urgência
    anxious_signals = ["urgente", "rápido", "preciso logo", "quando", "quanto tempo",
                       "demora", "hoje", "amanhã", "espero", "esperando"]
    # Desconfiança
    skeptic_signals = ["golpe", "fraude", "seguro", "confiável", "garantia", "prova",
                       "como funciona", "não acredito", "tenho medo", "desconfio"]

    scores = {
        "informal":     sum(1 for s in informal_signals if s in t),
        "formal":       sum(1 for s in formal_signals if s in t),
        "ansioso":      sum(1 for s in anxious_signals if s in t),
        "desconfiante": sum(1 for s in skeptic_signals if s in t),
    }

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else ""


# ---------------------------------------------------------------------------
# Classificação com IA
# ---------------------------------------------------------------------------

def _history_to_text(history: list[dict], exclude_last: bool = True) -> str:
    """Converte histórico de conversa para texto para incluir em prompts."""
    turns = history[:-1] if exclude_last and history else history
    if not turns:
        return "(sem histórico anterior)"
    recent = turns[-8:]  # máximo de 8 turnos de contexto
    lines = []
    for t in recent:
        role = "Lead" if t.get("role") == "user" else "Manuela"
        lines.append(f"{role}: {t.get('content', '')[:200]}")
    return "\n".join(lines)


async def _classify_with_ai(
    ai: AIClient,
    mensagem: str,
    card: dict,
    stage_nome: str,
    history: list[dict] | None = None,
) -> NegotiationResult:
    """
    Classifica a mensagem e gera resposta via IA.
    Usa sempre ai.complete() com histórico embutido como texto — garante retorno JSON.
    """
    historico_txt = _history_to_text(history or [], exclude_last=True)

    prompt = CLASSIFY_PROMPT_TEMPLATE.format(
        stage_nome=stage_nome,
        dados_card=build_card_context(card),
        mensagem=mensagem,
        historico=historico_txt,
    )

    try:
        raw = await ai.complete(
            prompt=prompt,
            system=SYSTEM_PROMPT,
            max_tokens=500,
        )

        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            raise AIError(f"Resposta sem JSON: {raw[:100]}")

        data        = json.loads(json_match.group())
        intent      = Intent(data.get("intent", "OUTRO"))
        ai_response = data.get("response", "").strip()

        if not ai_response:
            raise AIError("Resposta vazia da IA")

        logger.info(
            "Negociador IA: intent=%s | reasoning=%s",
            intent, data.get("reasoning", "")[:80]
        )
        return _build_result(intent, ai_response, card, mensagem)

    except (AIError, json.JSONDecodeError, ValueError, KeyError) as e:
        logger.warning("IA falhou na classificação: %s. Usando keywords.", e)
        return _fallback_classify(mensagem, card)


# ---------------------------------------------------------------------------
# Suporte ao stage ASSINATURA
# ---------------------------------------------------------------------------

async def _handle_assinatura_message(card: dict, mensagem: str) -> str:
    """
    Suporte para leads em ASSINATURA que enviam mensagem.
    Usa histórico e contexto para gerar resposta relevante.
    """
    nome        = get_name(card)
    adm         = get_adm(card)
    texto_lower = mensagem.lower()
    tem_token   = bool(card.get("ZapSign Token"))

    # Palavras-chave que indicam problema com o link de assinatura
    problemas_link = ["link", "assinar", "assinatura", "contrato", "não consigo",
                      "nao consigo", "abrir", "erro", "não abre", "nao abre",
                      "expirou", "venceu", "inválido"]

    if not tem_token:
        # Contrato ainda não foi gerado — aguarda dados/extrato
        return (
            f"Oi, {nome}! 😊 Ainda estou finalizando os detalhes do seu contrato {adm}. "
            f"Assim que tiver pronto te mando o link! Se precisar de algo enquanto isso, "
            f"é só chamar. 🙏"
        )

    if any(w in texto_lower for w in problemas_link):
        return (
            f"Oi, {nome}! Seu contrato {adm} está esperando só pela sua assinatura. 😊\n\n"
            f"Se o link não estiver abrindo me fala que reenvio agora mesmo! "
            f"É bem rápido pelo celular. 📱"
        )

    # Gera resposta contextual com IA para dúvidas genéricas em ASSINATURA
    try:
        history_ctx = history_to_text(load_history(card), max_turns=4)
        from services.ai import AIClient, AIError
        system = (
            "Você é Manuela, consultora da Consórcio Sorteado. "
            "O lead está na etapa de assinatura eletrônica do contrato — a última etapa antes de receber o pagamento. "
            "Seja prestativa, calorosa e direta. Máximo 3 linhas. "
            "Encoraje a assinar mas sem pressão — o lead já decidiu vender, só precisa de suporte."
        )
        prompt = (
            f"Lead: {nome} | Adm: {adm} | Contrato gerado: Sim\n"
            f"Histórico recente:\n{history_ctx}\n\n"
            f"Mensagem do lead: \"{mensagem}\"\n\n"
            f"Responda de forma natural e útil. Se for dúvida operacional, explique brevemente. "
            f"Se for algo fora do escopo, diga que vai acionar o consultor."
        )
        async with AIClient() as ai:
            return (await ai.complete(prompt=prompt, system=system, max_tokens=120)).strip()
    except Exception:
        pass

    return (
        f"Olá, {nome}! 😊 Seu contrato {adm} está pronto para assinatura. "
        f"Qualquer dúvida, é só me chamar!"
    )


# ---------------------------------------------------------------------------
# Envio e notificação
# ---------------------------------------------------------------------------

async def _send_response(card: dict, phone: str, message: str) -> bool:
    try:
        async with get_whapi_for_card(card) as w:
            await w.send_text(phone, message)
        return True
    except WhapiError as e:
        logger.error("Erro ao enviar resposta para %s: %s", phone, e)
        return False


async def _notify_team(message: str, target_phones: list[str] | None = None) -> None:
    """Notifica equipe. Se target_phones especificado, envia direto a eles; caso contrário usa grupo central."""
    if target_phones:
        try:
            async with WhapiClient(canal="lista") as w:
                for phone in target_phones:
                    await w.send_text(phone, message)
        except WhapiError as e:
            logger.warning("Falha ao notificar consultor direto: %s", e)
    else:
        from services.whapi import notify_team as _nt
        await _nt(message)


# ---------------------------------------------------------------------------
# Handler principal
# ---------------------------------------------------------------------------

async def handle_message(card: dict, mensagem: str, current_stage_id: str) -> None:
    card_id = card.get("id", "")
    nome    = get_name(card)
    phone   = get_phone(card)

    if not phone:
        logger.warning("Negociador: card %s sem telefone, ignorando.", card_id[:8])
        return

    # Negociador pausado manualmente — consultor humano está negociando
    if str(card.get("Negociador Pausado") or "").strip().lower() == "sim":
        logger.info("Negociador: card %s pausado (Negociador Pausado=sim) — ignorando msg", card_id[:8])
        return

    logger.info(
        "Negociador: card=%s | stage=%s... | msg='%s'",
        card_id[:8], current_stage_id[:8], mensagem[:60]
    )

    # Stage ASSINATURA: suporte simples
    if current_stage_id in SUPPORT_STAGES:
        response = await _handle_assinatura_message(card, mensagem)
        await _send_response(card, phone, response)
        return

    if current_stage_id not in ACTIVE_STAGES:
        logger.info("Negociador: stage %s fora do escopo.", current_stage_id[:8])
        return

    # Carrega card fresco + histórico
    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)
    history = await load_history_smart(phone, card_fresh)
    history = history_append(history, "user", mensagem)

    stage_nome = "Precificação" if current_stage_id == Stage.PRECIFICACAO else "Em Negociação"

    async with AIClient() as ai:
        result = await _classify_with_ai(ai, mensagem, card_fresh, stage_nome, history)

    logger.info(
        "Negociador: %s (%s) → intent=%s | next_stage=%s",
        nome, card_id[:8], result.intent.value,
        result.next_stage[:8] if result.next_stage else "mantém",
    )

    # ── Safety Car: audita resposta antes de enviar ──────────────────────────
    from services.safety_car import audit_response
    from services.faro import history_to_text
    historico_txt = history_to_text(history[:-1], max_turns=6)
    audit = await audit_response(result.response_message, card_fresh, historico_txt, agente="negociador")
    mensagem_auditada = audit.mensagem_final

    await _send_response(card, phone, mensagem_auditada)

    history = history_append(history, "assistant", mensagem_auditada)
    agora   = datetime.now(timezone.utc).isoformat()

    async with FaroClient() as faro:
        await save_history_smart(phone, history, faro_client=faro, card_id=card_id)

        # Detecta tom do lead na primeira troca e registra na jornada
        try:
            journey = load_journey(card_fresh)
            if not journey.get("tom"):
                tom = _detect_tom(mensagem)
                if tom:
                    journey["tom"] = tom
                    await save_journey(faro, card_id, journey)
        except Exception as _te:
            logger.debug("Negociador: erro ao detectar tom: %s", _te)
        try:
            update_fields: dict = {
                "Ultima atividade":      agora,
                "Ultima resposta lead":  mensagem[:500],
                "Situacao Negociacao":   result.intent.value,
            }
            if result.extra_fields:
                update_fields.update(result.extra_fields)
            await faro.update_card(card_id, update_fields)

            if result.next_stage and result.next_stage != current_stage_id:
                await faro.move_card(card_id, result.next_stage)
                logger.info("Negociador: card %s → %s", card_id[:8], result.next_stage[:8])

                # Ao aceitar: registra snapshot da negociação na jornada
                if result.next_stage == Stage.ACEITO:
                    try:
                        proposta_str = card_fresh.get("Proposta Realizada") or ""
                        try:
                            import re as _re
                            nums = _re.sub(r"[^\d,.]", "", proposta_str)
                            nums = nums.replace(".", "").replace(",", ".")
                            proposta_num = float(nums) if nums else 0.0
                        except (ValueError, TypeError):
                            proposta_num = 0.0

                        num_neg = int(card_fresh.get("Num Follow Ups") or 0)
                        journey = load_journey(card_fresh)
                        journey.update({
                            "proposta_final":    proposta_num,
                            "num_negociacoes":   num_neg,
                            "ultima_intencao":   result.intent.value,
                        })
                        await save_journey(faro, card_id, journey)
                    except Exception as _je:
                        logger.warning("Negociador: erro ao salvar jornada card %s: %s", card_id[:8], _je)

        except FaroError as e:
            logger.error("Negociador: erro ao atualizar card %s: %s", card_id[:8], e)

    if result.notify_team and result.notify_message:
        await _notify_team(result.notify_message, result.notify_phones)

    # Resposta atrasada (simula consulta ao diretor) — dispara em background
    if result.delayed_followup and result.delayed_followup_seconds > 0:
        import asyncio as _asyncio

        async def _send_delayed(
            _card: dict, _phone: str, _card_id: str,
            _msg: str, _seconds: int, _history: list,
        ) -> None:
            await _asyncio.sleep(_seconds)
            sent = await _send_response(_card, _phone, _msg)
            if sent:
                new_history = history_append(_history, "assistant", _msg)
                agora_delayed = datetime.now(timezone.utc).isoformat()
                async with FaroClient() as _faro:
                    await save_history_smart(_phone, new_history, faro_client=_faro, card_id=_card_id)
                    try:
                        await _faro.update_card(_card_id, {"Ultima atividade": agora_delayed})
                    except FaroError:
                        pass
                logger.info("Negociador: resposta do diretor enviada para %s", _card_id[:8])

        _asyncio.create_task(_send_delayed(
            card, phone, card_id,
            result.delayed_followup,
            result.delayed_followup_seconds,
            history,
        ))
