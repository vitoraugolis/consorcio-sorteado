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

from config import Stage, NOTIFY_PHONES, CONSULTANT_PHONES as _CONSULTANT_PHONES_CFG, NEGOCIADOR_MODEL
from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm, get_fonte, is_lista, is_pj,
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


def _build_handoff_notification(card: dict, mensagem: str, history: list | None = None) -> tuple[str, list[str]]:
    nome     = get_name(card)
    adm      = get_adm(card)
    phone    = get_phone(card) or "não informado"
    proposta = card.get("Proposta Realizada") or "a consultar"
    credito  = card.get("Crédito") or "a consultar"
    fonte    = get_fonte(card)

    history = history or load_history(card)
    if not history:
        logger.warning(
            "Negociador _build_handoff_notification: histórico vazio para card %s — "
            "considere passar history=<Redis> para evitar contexto perdido.",
            card.get("id", "?")[:8],
        )
    resumo_turns = []
    for turn in history[-12:]:  # aumentado de 6 para 12 turnos
        role = "Lead" if turn.get("role") == "user" else "Manuela"
        resumo_turns.append(f"*{role}:* {turn.get('content', '')[:200]}")  # 200 chars (era 120)
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
    raw = (card.get("Classes de Proposta") or "").strip()
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


def _build_contraproposta_notification(card: dict, mensagem: str, history: list | None = None) -> tuple[str, list[str]]:
    """Monta notificação específica para handoff de contraproposta fora do nosso alcance."""
    nome     = get_name(card)
    adm      = get_adm(card)
    phone    = get_phone(card) or "não informado"
    credito  = card.get("Crédito") or "a consultar"
    proposta = card.get("Proposta Realizada") or "a consultar"
    lead_val = _extract_lead_value(mensagem, _parse_currency_value(card.get("Proposta Realizada") or "0"))
    lead_val_fmt = _fmt_currency(lead_val) if lead_val else mensagem[:80]

    history = history or load_history(card)
    if not history:
        logger.warning(
            "Negociador _build_contraproposta_notification: histórico vazio para card %s — "
            "considere passar history=<Redis> para evitar contexto perdido.",
            card.get("id", "?")[:8],
        )
    resumo_turns = []
    for turn in history[-12:]:  # aumentado de 6 para 12 turnos
        role = "Lead" if turn.get("role") == "user" else "Manuela"
        resumo_turns.append(f"*{role}:* {turn.get('content', '')[:200]}")  # 200 chars (era 120)
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


def _get_next_proposal(card: dict, lead_value: float = 0.0) -> dict:
    """
    Calcula a próxima proposta com base na Classes de Proposta do FARO.

    lead_value: se > 0, representa uma contraproposta específica do lead.
                Nesse caso, usamos o menor step da sequência que cubra o valor,
                em vez de pular ao máximo (regra < 27%).

    Returns:
        nova_proposta  float  — valor a oferecer
        indice         int    — novo índice 1-based para gravar no FARO
        viavel         bool   — ainda há propostas maiores depois desta
        pode_escalar   bool   — existe ao menos um valor maior que a proposta atual
        is_max_jump    bool   — saltou para o máximo (regra < 27%, sem valor específico)
    """
    sequencia_raw   = (card.get("Classes de Proposta") or "").strip()
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

    # Se o lead deu uma CONTRA_PROPOSTA com valor específico:
    # Verificar se o valor pedido está dentro do que conseguimos pagar.
    # Se sim → sinalizar aceitação do valor do lead (não subir para um step maior).
    # Se não → retornar o máximo disponível da sequência.
    if lead_value > 0:
        max_sequencia = max(v for _, v in candidatos) if candidatos else 0.0
        if lead_value <= max_sequencia:
            # Conseguimos pagar o valor pedido → aceitar o valor do lead diretamente
            logger.info(
                "_get_next_proposal: CONTRA_PROPOSTA lead=%.0f ≤ max=%.0f → aceitar valor do lead",
                lead_value, max_sequencia,
            )
            # Descobrir o índice correspondente ao step imediatamente >= lead_value
            # (usado para atualizar Indice da Proposta no FARO)
            step_idx, step_val = min(
                ((i, v) for i, v in enumerate(sequencia) if v >= lead_value),
                key=lambda x: x[1],
                default=(len(sequencia) - 1, max_sequencia),
            )
            viavel = any(v > lead_value for v in sequencia)
            return {
                "nova_proposta": lead_value,   # valor exato do lead, não o step
                "indice": step_idx + 1,
                "viavel": viavel,
                "pode_escalar": True,
                "is_max_jump": False,
                "aceitar_contraproposta": True,  # flag para _build_result tratar como aceite
            }
        # Lead quer mais que nosso máximo → retorna o máximo como última oferta
        novo_i, nova = max(candidatos, key=lambda x: x[1])
        logger.info(
            "_get_next_proposal: CONTRA_PROPOSTA lead=%.0f > max=%.0f → oferecendo máximo",
            lead_value, nova,
        )
        return {
            "nova_proposta": nova,
            "indice": novo_i + 1,
            "viavel": False,
            "pode_escalar": True,
            "is_max_jump": False,
            "aceitar_contraproposta": False,
        }

    # Sem valor específico: regra dos 27%
    # Se proposta atual < 27% do crédito → salta direto para o máximo disponível
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
    DADOS_PESSOAIS  = "DADOS_PESSOAIS"   # lead enviando dados para o contrato
    QUER_COMPRAR    = "QUER_COMPRAR"     # lead quer COMPRAR cota/imóvel, não vender
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
    lost_reason:              Optional[str]  = None   # preenchido quando next_stage == PERDIDO
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

SOBRE TAXAS E PROCESSO (use sempre que lead perguntar sobre custos, taxas, parcelas, pagamento ou segurança):
- TODAS as despesas ficam por nossa conta — taxa de transferência, cartório, parcelas seguintes do consórcio: tudo nós assumimos.
- A ORDEM EXATA DO PROCESSO É (nunca inverta):
  1. Lead assina o contrato eletrônico (pelo celular, em minutos)
  2. Transferimos o valor À VISTA direto na conta do lead
  3. Só DEPOIS do dinheiro na conta do lead a cota é transferida para o nosso nome
- O lead NÃO corre nenhum risco: a cota sai do nome dele apenas após o pagamento estar confirmado.
- Nunca diga "costuma ser" ou "vou verificar" para essas informações — são fatos fixos do negócio, responda com convicção.

QUANDO O LEAD QUESTIONAR "POR QUE ASSINAR ANTES DE RECEBER?" (objeção comum de quem lida com imóveis):
Use este raciocínio (adapte o tom, não copie palavra por palavra):
- Valide a lógica do lead — ele está certo que em compra e venda normal se paga para assinar.
- Explique que aqui é diferente: a assinatura é o CONTRATO DE VENDA, não uma quitação.
  São três momentos distintos: 1) assinatura do compromisso, 2) pagamento à vista, 3) transferência.
- O motivo da assinatura primeiro: já aconteceu de fazermos o pagamento e o proprietário não honrar
  a transferência. A assinatura é a garantia dos dois lados.
- Reforce: enquanto a cota não é transferida, ela segue em posse do lead. Ele tem total controle.
- Convide para prosseguir: "Se estiver de acordo com o valor, já partimos para o contrato — você
  vê item por item, pode questionar qualquer cláusula, e só assina quando estiver 100% confortável."

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
- ACEITAR:          aceitação INCONDICIONAL ("aceito", "pode fechar", "topei", "bora",
                    "perfeito", "confirmado", "combinado", "pode enviar o contrato")
                    ATENÇÃO: "aceito por R$ X" ou "fecho se você me der X" → CONTRA_PROPOSTA
- RECUSAR:          recusa a vender ou pedido para parar o contato
- MELHORAR_VALOR:   quer mais dinheiro mas sem citar valor específico
- CONTRA_PROPOSTA:  cita um VALOR NUMÉRICO como condição ("fecho por 90 mil", "aceito por R$ X")
                    Se apenas pergunta SE pode fazer contraproposta → DUVIDA
- OFERECERAM_MAIS:  outro comprador ou empresa ofereceu valor maior (pode ou não ter citado o valor)
- NEGOCIAR:         objeção ao valor sem especificar quanto quer; quer "negociar" sem dizer o número
- DUVIDA:           pergunta sobre processo, documentação, prazo, contrato, pagamento, segurança —
                    qualquer pergunta operacional. Responda com clareza e segurança.
- DESCONFIANCA:     medo de golpe, dúvida sobre idoneidade, pedido de CNPJ/comprovação
- AGENDAR:          quer falar com consultor humano, ligar, ou pergunta completamente fora do escopo
- DADOS_PESSOAIS:   o lead está enviando dados para o contrato (nome, CPF, RG, endereço, e-mail,
                    dados bancários, CNPJ, nome da empresa). Detectar por padrões como
                    "nome:", "cpf:", "rg:", "endereço:", "cnpj:", "email:" ou lista de dados pessoais.
- QUER_COMPRAR:     o lead quer COMPRAR uma cota contemplada ou imóvel — não vender.
                    Ex: "o que vocês têm de imóvel?", "quero comprar uma carta", "tem cota à venda?",
                    "vocês vendem consórcio?", "quero adquirir um imóvel contemplado".
                    IMPORTANTE: o negócio da CS é COMPRAR cotas. Esse lead está fora do escopo
                    mas deve ser redirecionado com cordialidade para o departamento de venda de cotas.
                    Responda informando o redirecionamento — não tente converter para venda.
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
- Para DUVIDA sobre taxas/custos/processo: responda com convicção total — nunca "vou verificar" ou "costuma ser".
  Modelo geral: "Boa tarde, [nome]! Sobre as taxas — aqui na CS você não arca com nada. A taxa de transferência, o cartório e até as parcelas seguintes do consórcio ficam todos por nossa conta. Você recebe o valor combinado à vista após assinar o contrato, e a cota só sai do seu nome depois que o dinheiro já está na sua conta. Segurança total pra você! 😊"
- Para DUVIDA sobre "por que assinar antes de receber" ou estranheza com a ordem do processo:
  Valide o raciocínio do lead (faz sentido para quem negocia imóveis), depois explique os três momentos:
  1) assinatura do compromisso de venda, 2) pagamento à vista na conta, 3) transferência da cota.
  Reforce que são etapas separadas por segurança dos dois lados — já ocorreu de pagar e o proprietário
  não honrar a transferência. Enquanto a cota não é transferida, segue nos poderes do lead.
  Convide para prosseguir vendo o contrato item a item.
  Modelo: "Entendo sua lógica, [nome] — faz sentido! Mas aqui funciona assim: são três momentos separados. Primeiro você assina o contrato de venda (o compromisso). Depois a gente faz o pagamento à vista direto na sua conta. E só então, com o dinheiro já na sua mão, a cota é transferida para o nosso nome. Isso protege os dois lados — já tivemos casos em que pagamos e o proprietário não deu continuidade à transferência. Enquanto a cota não sair do seu nome, ela segue sob seus poderes, tudo certo? Se estiver de acordo com o valor, já partimos pro contrato — você vê cada item e pode questionar o que quiser antes de assinar. 😊"
- Para OUTRO: mantenha a conversa com naturalidade, não force o tema da proposta.
- NUNCA comece a resposta com "Que pena", "Infelizmente", "Lamento" ou similares.

RETORNE EXCLUSIVAMENTE JSON VÁLIDO (sem markdown, sem texto fora do JSON):
{{
  "intent": "ACEITAR|RECUSAR|MELHORAR_VALOR|CONTRA_PROPOSTA|OFERECERAM_MAIS|NEGOCIAR|DUVIDA|DESCONFIANCA|AGENDAR|DADOS_PESSOAIS|QUER_COMPRAR|OUTRO",
  "reasoning": "1 frase explicando por que esse intent",
  "response": "mensagem para o lead"
}}
"""

# ---------------------------------------------------------------------------
# Classificação por keywords (fallback)
# ---------------------------------------------------------------------------

_KEYWORD_MAP = {
    Intent.ACEITAR: [
        # Aceites explícitos
        "aceito", "aceitar", "quero fechar", "fechado", "topei", "vamos fechar",
        "pode mandar contrato", "concordo", "combinado", "ok pode ser",
        # Aceites implícitos confirmados nas jornadas reais (Marta: "Perfeito!", "Confirmado", "Combinado")
        "perfeito", "confirmado", "confirmo", "isso mesmo", "pode ser",
        "tá bom", "ta bom", "tá ótimo", "ta otimo", "ótimo", "otimo",
        "fechamos", "fechar", "bora", "vamos nessa", "pode ir", "pode fazer",
        "tá certo", "ta certo", "correto", "certo", "pode", "sim pode",
        "ok fechado", "ok combinado", "pode enviar o contrato", "manda o contrato",
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
    Intent.DADOS_PESSOAIS: [
        "nome:", "cpf:", "rg:", "cnpj:", "endereço:", "endereco:", "e-mail:",
        "email:", "profissão:", "profissao:", "estado civil:", "cep:",
        "nome da empresa:", "nome completo:", "dados bancários:", "dados bancarios:",
        "agência:", "agencia:", "conta:", "pix:",
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
    Intent.QUER_COMPRAR: [
        "quero comprar", "quero adquirir", "tem para comprar", "tem pra comprar",
        "o que vocês têm", "o que voces tem", "tem imóvel", "tem imovel",
        "tem apartamento", "tem carro", "tem veículo", "tem veiculo",
        "cota à venda", "cota a venda", "carta disponível", "carta disponivel",
        "vocês vendem", "voces vendem", "para me oferecer",
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
        # response_message vazio: _iniciar_coleta_dados_contrato envia a única mensagem
        # ao lead após o aceite — evita duplicação (IA + coleta de dados).
        return NegotiationResult(
            intent=intent,
            response_message="",   # silencia a IA; coleta de dados fala sozinha
            next_stage=Stage.ACEITO,
            notify_team=True,
            notify_message=(
                f"🎯 *Lead aceitou a proposta!*\n\n"
                f"*Cliente:* {nome}\n"
                f"*Administradora:* {adm}\n"
                f"*Telefone:* {get_phone(card) or 'não informado'}\n\n"
                f"O sistema vai iniciar a coleta de dados para contrato. ✅"
            ),
        )

    # ── DADOS_PESSOAIS ────────────────────────────────────────────────────────
    # Lead enviou dados para contrato (CPF, nome, endereço, etc.) sem ter sido solicitado
    # formalmente — acontece quando lead aceita e já manda tudo junto.
    # Mover para ASSINATURA e deixar agente_contrato processar.
    if intent == Intent.DADOS_PESSOAIS:
        return NegotiationResult(
            intent=intent,
            response_message="",   # agente_contrato assumirá e confirmará os dados
            next_stage=Stage.ACEITO,  # aciona _iniciar_coleta que move para ASSINATURA
            notify_team=True,
            notify_message=(
                f"📋 *Lead enviou dados para contrato espontaneamente!*\n\n"
                f"*Cliente:* {nome} | *Adm:* {adm}\n"
                f"O sistema vai processar os dados e iniciar a coleta via agente_contrato. ✅"
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

    # ── QUER_COMPRAR ───────────────────────────────────────────────────────────
    # Lead quer COMPRAR cota/imóvel — fora do escopo de venda da CS.
    # Registra o interesse na descrição, move para FINALIZACAO_COMERCIAL e notifica equipe.
    if intent == Intent.QUER_COMPRAR:
        notif_msg, notif_phones = _build_handoff_notification(card, mensagem)
        notif_msg = (
            f"🏠 *Lead quer COMPRAR cota/imóvel*\n"
            f"Redirecionado automaticamente para FINALIZAÇÃO COMERCIAL.\n\n"
        ) + notif_msg
        return NegotiationResult(
            intent=intent,
            response_message=ai_response,
            next_stage=Stage.FINALIZACAO_COMERCIAL,
            notify_team=True,
            notify_message=notif_msg,
            notify_phones=notif_phones,
            extra_fields={"_quer_comprar_mensagem": mensagem},  # usado para append_description
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
    # Para CONTRA_PROPOSTA com valor específico: passa lead_value para _get_next_proposal
    # evita o salto ao máximo quando o lead pediu apenas um pouco mais
    _lead_val_for_escalada = 0.0
    if intent == Intent.CONTRA_PROPOSTA and _message_has_value(mensagem):
        _proposta_ctx = _parse_currency_value(card.get("Proposta Realizada") or "0")
        _lead_val_for_escalada = _extract_lead_value(mensagem, _proposta_ctx)

    prox = _get_next_proposal(card, lead_value=_lead_val_for_escalada)

    if not prox["pode_escalar"]:
        # Sem Sequencia_Proposta → não encerra, mantém negociação
        if not (card.get("Classes de Proposta") or "").strip():
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
            lost_reason="VALOR_INSUFICIENTE — teto da sequência atingido sem aceite",
        )

    # Formata nova proposta e injeta na resposta da IA
    nova_fmt   = _fmt_currency(prox["nova_proposta"])
    nome_curto = get_name(card).split()[0] if get_name(card) else ""

    # Contraproposta do lead dentro do nosso alcance → aceitar o valor dele diretamente
    if prox.get("aceitar_contraproposta"):
        import random as _rand
        _opcoes_aceite_cp = [
            f"Fechado, {nome_curto}! *{nova_fmt}* combinado. 🤝 "
            f"Vou agora passar para nosso time finalizar — eles entram em contato para os próximos passos.",
            f"Ótimo, {nome_curto}! Aceito *{nova_fmt}*. 🎉 "
            f"Nosso time vai entrar em contato para organizar tudo. Pagamento à vista na sua conta, antes de qualquer transferência.",
            f"Combinado! *{nova_fmt}* está aprovado. 🤝 "
            f"Vou acionar nosso consultor agora para dar sequência.",
        ]
        response = _rand.choice(_opcoes_aceite_cp)
        nome    = get_name(card)
        adm     = get_adm(card)
        phone   = get_phone(card) or "não informado"
        notif = (
            f"🎯 *Lead aceitou contraproposta!*\n\n"
            f"*Cliente:* {nome}\n"
            f"*Administradora:* {adm}\n"
            f"*Telefone:* {phone}\n"
            f"*Valor aceito:* *{nova_fmt}* (contraproposta do lead)\n\n"
            f"O sistema vai iniciar a coleta de dados para contrato. ✅"
        )
        return NegotiationResult(
            intent=Intent.ACEITAR,
            response_message=response,
            next_stage=Stage.ACEITO,
            notify_team=True,
            notify_message=notif,
            extra_fields={
                "Proposta Realizada": f"{prox['nova_proposta']:.2f}",
                "Indice da Proposta": str(prox["indice"]),
            },
        )

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
        Intent.QUER_COMPRAR:    (
            f"Certo{', ' + primeiro if primeiro else ''}! Vou redirecionar seu contato para um "
            f"representante comercial do departamento de venda de cotas, tudo bem? "
            f"Ele poderá te mostrar as melhores oportunidades. 😊"
        ),
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
    """Converte histórico de conversa para texto para incluir em prompts de classificação."""
    turns = history[:-1] if exclude_last and history else history
    if not turns:
        return "(sem histórico anterior)"
    recent = turns[-20:]  # últimos 20 turnos de contexto (aumentado de 8)
    lines = []
    for t in recent:
        role = "Lead" if t.get("role") == "user" else "Manuela"
        lines.append(f"{role}: {t.get('content', '')[:500]}")  # 500 chars por turn (era 200)
    return "\n".join(lines)


def _split_history_for_classify(
    history: list[dict],
    mensagem: str,
) -> tuple[list[dict], str]:
    """
    Divide o histórico para o prompt de classificação do negociador.

    Estratégia:
    - O histórico completo (sem a última mensagem do usuário) vai como mensagens reais
      para o complete_with_history — o modelo vê a conversa real, não texto achatado.
    - O CLASSIFY_PROMPT é enviado como última mensagem do usuário, com o histórico
      textual mais antigo embutido apenas quando o histórico total é muito longo.

    Retorna:
        messages: lista de mensagens para o modelo (sem a última do usuário, que vai no prompt)
        historico_txt: histórico como texto para embutir no prompt de classificação
    """
    # Exclui a última entrada (que é a mensagem atual do lead, adicionada antes da chamada)
    turns_sem_atual = history[:-1] if history else []

    # Últimos 30 turnos como mensagens reais — o modelo processa como conversa autêntica
    recent_as_messages = turns_sem_atual[-30:]

    # Para o campo {historico} no CLASSIFY_PROMPT usamos os mesmos 20 últimos como texto
    # (serve de âncora textual para o modelo identificar o contexto no prompt)
    historico_txt = _history_to_text(turns_sem_atual, exclude_last=False)

    return recent_as_messages, historico_txt


async def _classify_with_ai(
    ai: AIClient,
    mensagem: str,
    card: dict,
    stage_nome: str,
    history: list[dict] | None = None,
    extra_system: str = "",
) -> NegotiationResult:
    """
    Classifica a mensagem e gera resposta via IA.

    Usa complete_with_history() — o histórico da conversa passa como mensagens reais
    ao modelo (não como texto achatado no prompt). Isso garante que o modelo "vive"
    a conversa completa em vez de ler um resumo truncado, eliminando respostas
    fora de contexto, repetitivas e sem humanização.

    O prompt de classificação (intent + resposta) é a última mensagem do usuário.
    """
    _history = history or []
    conv_messages, historico_txt = _split_history_for_classify(_history, mensagem)

    _system = SYSTEM_PROMPT + extra_system if extra_system else SYSTEM_PROMPT

    # Prompt de classificação como última mensagem do usuário
    classify_prompt = CLASSIFY_PROMPT_TEMPLATE.format(
        stage_nome=stage_nome,
        dados_card=build_card_context(card),
        mensagem=mensagem,
        historico=historico_txt,
    )

    # Monta histórico + prompt de classificação como mensagens reais
    # O modelo recebe: [histórico real] + [mensagem de classificação]
    messages_for_model = conv_messages + [{"role": "user", "content": classify_prompt}]

    try:
        raw = await ai.complete_with_history(
            history=messages_for_model,
            system=_system,
            max_tokens=600,
            model=NEGOCIADOR_MODEL,
            fallback_model="gpt-4o-mini",
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
            "Negociador IA: intent=%s | reasoning=%s | history_turns=%d",
            intent, data.get("reasoning", "")[:80], len(conv_messages),
        )
        return _build_result(intent, ai_response, card, mensagem)

    except (AIError, json.JSONDecodeError, ValueError, KeyError) as e:
        logger.warning("IA falhou na classificação: %s. Usando keywords.", e)
        return _fallback_classify(mensagem, card)


# ---------------------------------------------------------------------------
# Suporte ao stage ASSINATURA
# ---------------------------------------------------------------------------

async def _handle_assinatura_message(card: dict, mensagem: str, history: list | None = None) -> str:
    """
    Suporte para leads em ASSINATURA que enviam mensagem.
    Usa histórico e contexto para gerar resposta relevante.
    Recebe history já carregado via Redis (load_history_smart) para evitar leitura stale do FARO.
    """
    from services.faro import history_to_text as _history_to_text
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
        # Usa history passado pelo caller (Redis) para evitar leitura stale do campo FARO
        history_ctx = _history_to_text(history or [], max_turns=10)  # aumentado de 4 para 10
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


def _count_followups_from_history(history: list) -> int:
    """Conta turns do assistente no histórico como proxy de num_negociacoes."""
    return max(0, sum(1 for t in history if t.get("role") == "assistant") - 1)


async def _iniciar_coleta_dados_contrato(card: dict, phone: str, history: list) -> None:
    """
    Disparado imediatamente após o lead aceitar a proposta.
    Esta é a ÚNICA mensagem enviada ao lead no aceite — a IA fica em silêncio
    (response_message vazio no NegotiationResult ACEITAR).
    Move o card para ASSINATURA para que agente_contrato assuma a coleta.

    Dois conjuntos de dados conforme titularidade:
      PF  → CPF (padrão)
      PJ  → CNPJ (quando campo "Tipo Pessoa" = "PJ" / "CNPJ")
    """
    nome     = get_name(card).split()[0] if get_name(card) else "você"
    card_id  = card.get("id", "")
    proposta = card.get("Proposta Realizada") or "o valor combinado"
    adm      = get_adm(card)

    # Limpa o guard para este lead — ele aceitou, pode receber novas mensagens
    # (coleta de dados tem prefixo diferente da ativação, mas por segurança zeramos)
    try:
        from services.message_guard import clear_guard
        await clear_guard(phone)
    except Exception:
        pass


    if is_pj(card):
        msg = (
            f"Ótimo, {nome}! 🎉 Vou iniciar o contrato da cota *{adm}* pelo valor de *R$ {proposta}*.\n\n"
            f"Como a cota está em nome de pessoa jurídica, precisarei dos seguintes dados:\n\n"
            f"🏢 *Nome da empresa:*\n"
            f"📄 *CNPJ:*\n"
            f"👤 *Nome completo do sócio:*\n"
            f"🪪 *CPF do sócio:*\n"
            f"🪪 *RG do sócio:*\n"
            f"🏠 *Endereço completo:*\n"
            f"📮 *CEP:*\n"
            f"💼 *Profissão:*\n"
            f"💍 *Estado civil:*\n"
            f"📧 *E-mail:*\n"
            f"💳 *Dados para pagamento — Conta/Agência/PIX em nome do CNPJ:*\n\n"
            f"Pode me enviar tudo de uma vez ou em partes, como preferir! 😊"
        )
    else:
        msg = (
            f"Ótimo, {nome}! 🎉 Vou iniciar o contrato da cota *{adm}* pelo valor de *R$ {proposta}*.\n\n"
            f"Para agilizar, preciso dos seguintes dados:\n\n"
            f"👤 *Nome completo:*\n"
            f"🪪 *CPF:*\n"
            f"🪪 *RG:*\n"
            f"🏠 *Endereço completo:*\n"
            f"📮 *CEP:*\n"
            f"💼 *Profissão / ocupação:*\n"
            f"💍 *Estado civil:*\n"
            f"🌍 *Nacionalidade:*\n"
            f"📧 *E-mail:*\n"
            f"💳 *Dados para pagamento — Conta/Agência/PIX em nome do CPF:*\n\n"
            f"Pode me enviar tudo de uma vez ou em partes, como preferir! 😊"
        )

    try:
        await _send_response(card, phone, msg)
        new_history = history_append(history, "assistant", msg)
        async with FaroClient() as faro:
            await save_history_smart(phone, new_history, faro_client=faro, card_id=card_id)
            await faro.move_card(card_id, Stage.ASSINATURA)
            await faro.update_card(card_id, {
                "Ultima atividade": datetime.now(timezone.utc).isoformat(),
            })
        logger.info("Negociador: coleta de dados iniciada para card %s → ASSINATURA (tipo=%s)",
                    card_id[:8], "PJ" if is_pj(card) else "PF")
    except Exception as e:
        logger.error("Negociador: erro ao iniciar coleta dados card %s: %s", card_id[:8], e)


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

    if current_stage_id not in ACTIVE_STAGES and current_stage_id not in SUPPORT_STAGES:
        logger.info("Negociador: stage %s fora do escopo.", current_stage_id[:8])
        return

    # Carrega card fresco + histórico (necessário para SUPPORT_STAGES e ACTIVE_STAGES)
    async with FaroClient() as faro:
        card_fresh = await faro.get_card(card_id)
    history = await load_history_smart(phone, card_fresh)

    # Stage ASSINATURA: suporte simples com histórico atualizado via Redis
    if current_stage_id in SUPPORT_STAGES:
        response = await _handle_assinatura_message(card_fresh, mensagem, history=history)
        await _send_response(card_fresh, phone, response)
        # Persiste a troca no histórico
        new_history = history_append(history, "user", mensagem)
        new_history = history_append(new_history, "assistant", response)
        async with FaroClient() as faro_s:
            await save_history_smart(phone, new_history, faro_client=faro_s, card_id=card_id)
        return
    history = history_append(history, "user", mensagem)

    # ── Consciência cross-fluxo: verifica outros cards do mesmo lead ────────────
    _cross_context = ""
    try:
        async with FaroClient() as _faro_all:
            _all_cards = await _faro_all.find_all_cards_by_phone(phone)
        _outros = [c for c in _all_cards if c.get("id") != card_id]
        if _outros:
            from services.faro import get_fonte as _get_fonte
            _linhas = []
            for _oc in _outros:
                _fluxo = _get_fonte(_oc) or "fluxo desconhecido"
                _stage_oc = _oc.get("stage_id", "")[:8]
                _prop_oc = _oc.get("Proposta Realizada") or "nenhuma"
                _linhas.append(f"fluxo {_fluxo} (stage {_stage_oc}, proposta: {_prop_oc})")
            _cross_context = "\n\nCONTEXTO CROSS-FLUXO: Este lead também aparece em " + "; ".join(_linhas) + ". Não duplicar propostas já feitas."
            logger.info("Negociador: cross-fluxo detectado para %s — %d outros cards", card_id[:8], len(_outros))
    except Exception as _cfe:
        logger.debug("Negociador: erro ao buscar cross-fluxo: %s", _cfe)

    stage_nome = "Precificação" if current_stage_id == Stage.PRECIFICACAO else "Em Negociação"

    async with AIClient() as ai:
        result = await _classify_with_ai(ai, mensagem, card_fresh, stage_nome, history, extra_system=_cross_context)

    logger.info(
        "Negociador: %s (%s) → intent=%s | next_stage=%s",
        nome, card_id[:8], result.intent.value,
        result.next_stage[:8] if result.next_stage else "mantém",
    )

    # ── Audita resposta (sem Safety Car — responde autonomamente) ─────────────
    from services.safety_car import audit_response
    from services.faro import history_to_text
    historico_txt = history_to_text(history[:-1], max_turns=15)  # aumentado de 6 para 15

    if result.response_message:
        audit = await audit_response(result.response_message, card_fresh, historico_txt, agente="negociador")
        mensagem_auditada = audit.mensagem_final
        await _send_response(card, phone, mensagem_auditada)
        history = history_append(history, "assistant", mensagem_auditada)
        logger.info("Negociador: resposta enviada autonomamente para card %s (intent=%s)",
                    card_id[:8], result.intent.value)
    else:
        mensagem_auditada = ""
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
                # Filtra campos internos (prefixo _) — não devem ir para o FARO
                faro_fields = {k: v for k, v in result.extra_fields.items() if not k.startswith("_")}
                update_fields.update(faro_fields)
            # Registra motivo de perda automaticamente ao mover para PERDIDO
            if result.next_stage == Stage.PERDIDO and result.lost_reason:
                update_fields["Motivo de perda"] = result.lost_reason
            await faro.update_card(card_id, update_fields)

            if result.next_stage and result.next_stage != current_stage_id:
                await faro.move_card(card_id, result.next_stage)
                logger.info("Negociador: card %s → %s", card_id[:8], result.next_stage[:8])

                # Marco 2 — enviado para agente comercial
                if result.next_stage == Stage.FINALIZACAO_COMERCIAL:
                    try:
                        # QUER_COMPRAR: descrição rica com histórico e interesse de compra
                        if result.intent == Intent.QUER_COMPRAR:
                            _msg_compra = (result.extra_fields or {}).get("_quer_comprar_mensagem", mensagem)
                            _historico_txt = history_to_text(history, max_turns=10)
                            await faro.append_description(
                                card_id,
                                f"🏠 Lead com interesse em COMPRAR cota/imóvel (fora do escopo de venda)\n\n"
                                f"Mensagem original: \"{_msg_compra[:300]}\"\n\n"
                                f"📋 Histórico até o redirecionamento:\n{_historico_txt}"
                            )
                        else:
                            proposta_str = card_fresh.get("Proposta Realizada") or "?"
                            adm_str      = card_fresh.get("Adm") or "?"
                            intent_str   = result.intent.value
                            await faro.append_description(
                                card_id,
                                f"🔵 Enviado para agente comercial — Proposta: R$ {proposta_str} | {adm_str} | Motivo: {intent_str}"
                            )
                    except Exception as _de:
                        logger.warning("Negociador: erro ao gravar marco comercial %s: %s", card_id[:8], _de)

                # Marco 3 — lead aceitou a proposta
                if result.next_stage == Stage.ACEITO:
                    try:
                        proposta_str = card_fresh.get("Proposta Realizada") or "?"
                        adm_str      = card_fresh.get("Adm") or "?"
                        await faro.append_description(
                            card_id,
                            f"✅ Proposta aceita — R$ {proposta_str} | {adm_str}"
                        )
                    except Exception as _de:
                        logger.warning("Negociador: erro ao gravar marco aceite %s: %s", card_id[:8], _de)

                # Ao aceitar: coleta dados para contrato e registra snapshot na jornada
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
                        num_neg = _count_followups_from_history(history)
                        journey = load_journey(card_fresh)
                        journey.update({
                            "proposta_final":  proposta_num,
                            "num_negociacoes": num_neg,
                            "ultima_intencao": result.intent.value,
                        })
                        await save_journey(faro, card_id, journey)
                    except Exception as _je:
                        logger.warning("Negociador: erro ao salvar jornada card %s: %s", card_id[:8], _je)

                    # Inicia coleta de dados para contrato (nome, endereço, estado civil etc.)
                    import asyncio as _asyncio
                    _asyncio.create_task(_iniciar_coleta_dados_contrato(card_fresh, phone, history))

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
