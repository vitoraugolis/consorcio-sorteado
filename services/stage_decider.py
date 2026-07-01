"""
services/stage_decider.py — Decisor de Mudança de Fase (Stage Decider)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALMA DO MÓDULO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
O StageDecider é o árbitro de intenção da Consórcio Sorteado.

Ele não classifica palavras — ele lê contexto. Tem acesso à consciência
coletiva do time (agent_knowledge), ao histórico completo da conversa e
ao estado atual do card no CRM.

Sua responsabilidade é única e inegociável:
  → Decidir se uma mudança de fase está JUSTIFICADA pelo que o lead
    REALMENTE expressou — não pelo que o sistema quer ouvir.

Princípios invioláveis:
  1. CONSERVADORISMO — em caso de dúvida, NÃO avança. Manter é mais seguro.
  2. EVIDÊNCIA EXPLÍCITA — aceite só se o lead disse claramente que quer fechar.
     Menção a valor de terceiros ≠ aceite. Pergunta sobre negociação ≠ aceite.
  3. CONTEXTO > TEXTO — a mensagem atual é lida à luz de toda a conversa anterior.
  4. ANTI-ALUCINAÇÃO — retorna apenas os campos definidos no schema. Se não tem
     certeza do campo, usa o valor padrão seguro (manter stage).
  5. RASTREABILIDADE — toda decisão tem reasoning auditável no log.

Uso:
    from services.stage_decider import StageDecider, StageDecision

    decision = await StageDecider.decide(
        mensagem=mensagem,
        card=card,
        history=history,
        current_stage_id=current_stage_id,
    )
    if decision.should_change:
        await faro.move_card(card_id, decision.next_stage)
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from config import Stage, NEGOCIADOR_MODEL
from services.ai import AIClient, AIError
from services.faro import get_name, get_adm, get_phone, is_lista, build_card_context
from services.agent_knowledge import get_knowledge_for_agent, STAGES_MAP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stages que o decider é autorizado a transicionar FROM
# ---------------------------------------------------------------------------
_ALLOWED_SOURCE_STAGES = {
    Stage.EM_NEGOCIACAO,
    Stage.PRECIFICACAO,
}

# ---------------------------------------------------------------------------
# Transições permitidas — o decider só pode sugerir esses destinos
# ---------------------------------------------------------------------------
_ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    Stage.EM_NEGOCIACAO: [
        Stage.ACEITO,
        Stage.PERDIDO,
        Stage.FINALIZACAO_COMERCIAL,
        Stage.EM_NEGOCIACAO,   # manter
    ],
    Stage.PRECIFICACAO: [
        Stage.EM_NEGOCIACAO,   # proposta recebida, lead reagiu antes de ser movido
        Stage.FINALIZACAO_COMERCIAL,
        Stage.PRECIFICACAO,    # manter
    ],
}

# ---------------------------------------------------------------------------
# Schema de decisão
# ---------------------------------------------------------------------------

@dataclass
class StageDecision:
    """Resultado imutável do StageDecider."""
    should_change:   bool
    next_stage:      Optional[str]  # None se should_change=False
    reasoning:       str            # auditável, gravado em log
    confidence:      str            # "high" | "medium" | "low"
    raw_intent:      str            # intent identificado pelo decider
    evidence:        str            # trecho textual que justifica a decisão
    source:          str            # "ai" | "fallback_conservative"


# ---------------------------------------------------------------------------
# System Prompt do StageDecider
# ---------------------------------------------------------------------------

_DECIDER_SYSTEM = (
    get_knowledge_for_agent("stage_decider")
    + """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 QUEM VOCÊ É — STAGE DECIDER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Você é o Decisor de Fase da Consórcio Sorteado — um árbitro de intenção,
não um classificador de palavras.

Sua única função: avaliar se a mensagem mais recente do lead, lida no contexto
de toda a conversa, justifica uma mudança de fase no CRM.

VOCÊ NÃO ESCREVE RESPOSTAS AO LEAD. Você apenas decide fases.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧠 PRINCÍPIOS INVIOLÁVEIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. CONSERVADORISMO — quando em dúvida, mantenha a fase atual.
   Uma mudança prematura pode custar uma venda. Manter é o comportamento padrão.

2. ACEITE REQUER EVIDÊNCIA EXPLÍCITA E INEQUÍVOCA.
   O lead deve ter dito claramente que quer fechar — não implicitamente.
   Exemplos que NÃO são aceite:
     ✗ "Recebi uma proposta de R$ 30 mil de outra empresa"
     ✗ "Isso é negociável?"
     ✗ "Gostei, mas quero pensar"
     ✗ "Que proposta vocês fariam?"
     ✗ "Me manda mais detalhes"
   Exemplos que SÃO aceite:
     ✓ "Aceito"
     ✓ "Pode fechar"
     ✓ "Fechado"
     ✓ "Topo"
     ✓ "Pode mandar o contrato"
     ✓ "Combinado, pode seguir"
     ✓ "Aceito por R$ X" (aceite condicional com valor)
     ✓ "Fechado, me manda os próximos passos"

3. INFORMAÇÃO COMPETITIVA ≠ PROPOSTA DO LEAD.
   "Recebi proposta de R$ X de outra empresa" é informação de mercado.
   Não é uma contraproposta. Não é um aceite. É contexto competitivo.
   A resposta certa é investigar e contra-oferecer — não aceitar nem avançar.

4. PERGUNTA SOBRE NEGOCIAÇÃO ≠ RECUSA ≠ ACEITE.
   "Isso é negociável?" → lead está sondando, não aceitando nem recusando.
   Manter em EM NEGOCIAÇÃO.

5. PARA PERDIDO: exija recusa clara e definitiva.
   "Vou pensar" → NÃO é PERDIDO.
   "Não tenho interesse" / "Já vendi para outra empresa" / "Não quero mais" → PERDIDO.

6. PARA FINALIZACAO_COMERCIAL: apenas quando o lead explicitamente pede humano
   ou quando a situação está demonstravelmente fora do alcance do sistema
   (proposta acima do teto de 30%, situação jurídica complexa, desconforto grave).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 INTENTS QUE MAPEIAM PARA MUDANÇA DE FASE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ACEITE_DEFINITIVO → ACEITO
  Condição: lead expressou aceite claro, sem condicionais de valor não atendidas.
  Aceite condicional com valor dentro da sequência de propostas: também ACEITO
  (o agente negociador já terá atualizado o valor antes).

RECUSA_DEFINITIVA → PERDIDO
  Condição: lead disse explicitamente que não quer mais negociar.
  "Vou pensar", "me liga amanhã", silêncio → NÃO é recusa definitiva.

ESCALAR_HUMANO → FINALIZACAO_COMERCIAL
  Condição: lead pediu humano explicitamente, OU situação está fora do alcance
  do sistema (jurídico, proposta > 30% do crédito, desconforto severo).

CONTINUAR_NEGOCIACAO → EM_NEGOCIACAO (manter)
  Tudo que não se encaixa acima. É o estado padrão e o mais frequente.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚫 ANTI-ALUCINAÇÃO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Retorne EXCLUSIVAMENTE o JSON definido. Nenhum texto fora do JSON.
- Se tiver qualquer dúvida sobre o intent → use CONTINUAR_NEGOCIACAO.
- Não invente campos. Não adicione campos extras.
- O campo "evidence" deve ser uma citação literal da mensagem do lead que
  justifica a decisão. Se não houver evidência clara → confidence = "low"
  e intent = CONTINUAR_NEGOCIACAO.
- Se o histórico indicar que o lead já estava negociando valor e a mensagem
  atual parece aceite mas é ambígua → CONTINUAR_NEGOCIACAO.
""".strip()
)

# ---------------------------------------------------------------------------
# Prompt de decisão
# ---------------------------------------------------------------------------

_DECIDER_PROMPT_TEMPLATE = """
Você é o Stage Decider da Consórcio Sorteado. Avalie se a mensagem do lead
justifica uma mudança de fase no CRM.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DADOS DO CARD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{dados_card}
Fase atual: {stage_nome}
Proposta atual: {proposta}
Crédito: {credito}
Sequência de propostas disponível: {sequencia}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HISTÓRICO DA CONVERSA (contexto completo)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{historico}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MENSAGEM ATUAL DO LEAD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"{mensagem}"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTENTS DISPONÍVEIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- ACEITE_DEFINITIVO      → lead aceitou de forma clara e inequívoca
- RECUSA_DEFINITIVA      → lead recusou explicitamente e de forma final
- ESCALAR_HUMANO         → lead pediu humano OU situação requer intervenção
- CONTINUAR_NEGOCIACAO   → tudo o mais (padrão conservador)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRANSIÇÕES PERMITIDAS DA FASE ATUAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{transicoes_permitidas}

RETORNE EXCLUSIVAMENTE JSON VÁLIDO:
{{
  "intent": "ACEITE_DEFINITIVO|RECUSA_DEFINITIVA|ESCALAR_HUMANO|CONTINUAR_NEGOCIACAO",
  "should_change": true|false,
  "next_stage_name": "nome legível da fase destino ou null",
  "confidence": "high|medium|low",
  "reasoning": "1-2 frases explicando a decisão com base no contexto",
  "evidence": "citação literal da mensagem que justifica a decisão, ou string vazia se inconclusivo"
}}
"""

# ---------------------------------------------------------------------------
# Mapa intent → stage destino
# ---------------------------------------------------------------------------

_INTENT_TO_STAGE: dict[str, dict[str, str]] = {
    "ACEITE_DEFINITIVO": {
        Stage.EM_NEGOCIACAO: Stage.ACEITO,
        Stage.PRECIFICACAO:  Stage.ACEITO,
    },
    "RECUSA_DEFINITIVA": {
        Stage.EM_NEGOCIACAO: Stage.PERDIDO,
        Stage.PRECIFICACAO:  Stage.PERDIDO,
    },
    "ESCALAR_HUMANO": {
        Stage.EM_NEGOCIACAO: Stage.FINALIZACAO_COMERCIAL,
        Stage.PRECIFICACAO:  Stage.FINALIZACAO_COMERCIAL,
    },
    "CONTINUAR_NEGOCIACAO": {},  # sem mudança de stage
}

# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------

def _history_to_text(history: list[dict], max_turns: int = 20) -> str:
    if not history:
        return "(sem histórico anterior)"
    turns = history[-max_turns:]
    lines = []
    for t in turns:
        role = "Lead" if t.get("role") == "user" else "Manuela"
        lines.append(f"{role}: {t.get('content', '')[:400]}")
    return "\n".join(lines)


def _stage_name(stage_id: str) -> str:
    return STAGES_MAP.get(stage_id, (stage_id[:8], ""))[0]


def _transicoes_texto(current_stage_id: str) -> str:
    allowed = _ALLOWED_TRANSITIONS.get(current_stage_id, [])
    if not allowed:
        return "Nenhuma transição definida — mantenha fase atual."
    lines = []
    for s in allowed:
        name = _stage_name(s)
        lines.append(f"  • {name} ({s[:8]})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fallback conservador (sem IA)
# ---------------------------------------------------------------------------

def _conservative_fallback(
    mensagem: str,
    current_stage_id: str,
    reasoning: str = "Fallback conservador: IA indisponível.",
) -> StageDecision:
    """
    Em caso de falha da IA, o comportamento padrão é NUNCA avançar.
    Manter o stage atual é sempre o comportamento mais seguro.
    """
    logger.warning("StageDecider: usando fallback conservador — %s", reasoning)
    return StageDecision(
        should_change=False,
        next_stage=None,
        reasoning=reasoning,
        confidence="low",
        raw_intent="CONTINUAR_NEGOCIACAO",
        evidence="",
        source="fallback_conservative",
    )


# ---------------------------------------------------------------------------
# StageDecider — classe principal
# ---------------------------------------------------------------------------

class StageDecider:
    """
    Árbitro de mudança de fase. Stateless — todos os métodos são @staticmethod.
    """

    @staticmethod
    async def decide(
        mensagem: str,
        card: dict,
        history: list[dict],
        current_stage_id: str,
        ai_client: Optional["AIClient"] = None,
    ) -> StageDecision:
        """
        Ponto de entrada principal.

        Args:
            mensagem:         Última mensagem do lead (já devidamente concatenada
                              se houve debounce).
            card:             Card fresco do FARO.
            history:          Histórico completo da conversa (incluindo mensagem atual).
            current_stage_id: Stage ID atual do card.
            ai_client:        AIClient já instanciado (opcional — cria um interno se None).

        Returns:
            StageDecision com a decisão e rastreabilidade completa.
        """
        # Guard: só atua nos stages autorizados
        if current_stage_id not in _ALLOWED_SOURCE_STAGES:
            return StageDecision(
                should_change=False,
                next_stage=None,
                reasoning=f"Stage {_stage_name(current_stage_id)} fora do escopo do StageDecider.",
                confidence="high",
                raw_intent="CONTINUAR_NEGOCIACAO",
                evidence="",
                source="ai",
            )

        if ai_client is not None:
            return await StageDecider._decide_with_ai(
                mensagem, card, history, current_stage_id, ai_client
            )

        # Cria client interno
        try:
            async with AIClient() as ai:
                return await StageDecider._decide_with_ai(
                    mensagem, card, history, current_stage_id, ai
                )
        except Exception as e:
            return _conservative_fallback(
                mensagem, current_stage_id,
                reasoning=f"Exceção ao criar AIClient: {e}",
            )

    @staticmethod
    async def _decide_with_ai(
        mensagem: str,
        card: dict,
        history: list[dict],
        current_stage_id: str,
        ai: "AIClient",
    ) -> StageDecision:
        """Core da decisão via IA."""
        card_id    = card.get("id", "")
        nome       = get_name(card)
        stage_nome = _stage_name(current_stage_id)
        proposta   = card.get("Proposta Realizada") or "não definida"
        credito    = card.get("Crédito") or "não informado"
        sequencia  = card.get("Classes de Proposta") or "não definida"

        historico_txt = _history_to_text(history, max_turns=20)
        transicoes    = _transicoes_texto(current_stage_id)

        prompt = _DECIDER_PROMPT_TEMPLATE.format(
            dados_card=build_card_context(card),
            stage_nome=stage_nome,
            proposta=proposta,
            credito=credito,
            sequencia=sequencia,
            historico=historico_txt,
            mensagem=mensagem,
            transicoes_permitidas=transicoes,
        )

        try:
            raw = await ai.complete(
                prompt=prompt,
                system=_DECIDER_SYSTEM,
                max_tokens=400,
                model=NEGOCIADOR_MODEL,
            )

            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not json_match:
                return _conservative_fallback(
                    mensagem, current_stage_id,
                    reasoning=f"IA retornou resposta sem JSON válido: {raw[:120]}",
                )

            data = json.loads(json_match.group())

            intent        = str(data.get("intent", "CONTINUAR_NEGOCIACAO")).upper()
            should_change = bool(data.get("should_change", False))
            confidence    = str(data.get("confidence", "low"))
            reasoning_txt = str(data.get("reasoning", ""))
            evidence_txt  = str(data.get("evidence", ""))

            # Validação de segurança: se IA diz should_change mas confidence=low → não avança
            if should_change and confidence == "low":
                logger.warning(
                    "StageDecider: card %s — IA sugeriu mudança com confidence=low. "
                    "Ignorando por conservadorismo. reasoning=%s",
                    card_id[:8], reasoning_txt,
                )
                return StageDecision(
                    should_change=False,
                    next_stage=None,
                    reasoning=f"[CONSERVADOR] IA sugeriu mudança mas confidence=low. Original: {reasoning_txt}",
                    confidence="low",
                    raw_intent=intent,
                    evidence=evidence_txt,
                    source="ai",
                )

            # Resolve o stage destino
            next_stage = None
            if should_change and intent in _INTENT_TO_STAGE:
                stage_map = _INTENT_TO_STAGE[intent]
                next_stage = stage_map.get(current_stage_id)

                # Valida que o destino é uma transição permitida
                allowed = _ALLOWED_TRANSITIONS.get(current_stage_id, [])
                if next_stage not in allowed:
                    logger.error(
                        "StageDecider: card %s — IA sugeriu transição não permitida "
                        "%s → %s. Ignorando.",
                        card_id[:8], _stage_name(current_stage_id),
                        _stage_name(next_stage or ""),
                    )
                    return _conservative_fallback(
                        mensagem, current_stage_id,
                        reasoning=f"Transição não permitida: {_stage_name(current_stage_id)} → {_stage_name(next_stage or '')}",
                    )

            logger.info(
                "StageDecider: card=%s | stage=%s | intent=%s | should_change=%s "
                "| next=%s | confidence=%s | reasoning=%s",
                card_id[:8],
                stage_nome,
                intent,
                should_change,
                _stage_name(next_stage) if next_stage else "manter",
                confidence,
                reasoning_txt[:120],
            )

            return StageDecision(
                should_change=should_change and next_stage is not None,
                next_stage=next_stage,
                reasoning=reasoning_txt,
                confidence=confidence,
                raw_intent=intent,
                evidence=evidence_txt,
                source="ai",
            )

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return _conservative_fallback(
                mensagem, current_stage_id,
                reasoning=f"Erro ao parsear JSON da IA: {e}",
            )
        except AIError as e:
            return _conservative_fallback(
                mensagem, current_stage_id,
                reasoning=f"AIError: {e}",
            )
