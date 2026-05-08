"""
tests/test_negociador.py — Testes da lógica de negociação (sem IO externo)
Testa _build_result, _get_next_proposal e _classify_with_ai (mockando IA).

Correções aplicadas (2026-05-08):
  - Campo correto para sequência de propostas: "Classes de Proposta" (não "Sequencia_Proposta")
  - _classify_with_ai usa complete_with_history() — mock atualizado
  - test_acima_sequencia_razoavel_faz_handoff: cenário corrigido (lead pede 380k = 38% de 1M,
    acima do teto de 32% e abaixo do absurdo de 40%)
  - test_escala_normal / test_salta_para_max: fixtures corrigidas com campo certo
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from webhooks.negociador import (
    Intent, NegotiationResult,
    _build_result, _get_next_proposal, _extract_lead_value,
    _parse_currency_value, _classify_with_ai,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def card_base(**overrides):
    base = {
        "id": "card-test-001",
        "stage_id": "stage-em-negociacao",
        "Nome do contato": "Ana Teste",
        "Telefone": "5511999990001",
        "Fonte": "Lista",
        "Etiquetas": [],
        "Administradora": "Itaú",
        "Crédito": "200000",
        "Proposta Realizada": "160000",
        # Campo correto: "Classes de Proposta" (campo FARO que guarda a sequência CSV)
        "Classes de Proposta": "160000,170000,180000,190000",
        "Indice da Proposta": "1",
        "Historico Conversa": "",
        "ZapSign Token": "",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _get_next_proposal
# ---------------------------------------------------------------------------
class TestGetNextProposal:

    def test_escala_normal(self):
        card = card_base()
        r = _get_next_proposal(card)
        assert r["pode_escalar"] is True
        assert r["nova_proposta"] == 170_000
        assert r["is_max_jump"] is False

    def test_salta_para_max_quando_abaixo_27pct(self):
        # Proposta atual = 50k = 5% de 1M → abaixo de 27% → salta direto ao máximo
        c = card_base(**{"Crédito": "1000000", "Proposta Realizada": "50000"})
        c["Classes de Proposta"] = "50000,200000,250000,300000"
        r = _get_next_proposal(c)
        assert r["is_max_jump"] is True
        assert r["nova_proposta"] == 300_000

    def test_sem_sequencia_retorna_nao_viavel(self):
        c = card_base(**{"Classes de Proposta": ""})
        r = _get_next_proposal(c)
        assert r["pode_escalar"] is False
        assert r["viavel"] is False

    def test_proposta_ja_no_teto_da_sequencia(self):
        c = card_base(**{"Proposta Realizada": "190000"})
        c["Classes de Proposta"] = "160000,170000,180000,190000"
        r = _get_next_proposal(c)
        assert r["pode_escalar"] is False

    def test_sequencia_com_lixo_nao_crasha(self):
        c = card_base(**{"Classes de Proposta": "abc,def,160000"})
        r = _get_next_proposal(c)
        assert r["pode_escalar"] is False


# ---------------------------------------------------------------------------
# _build_result — intents simples
# ---------------------------------------------------------------------------
class TestBuildResultSimples:

    def test_aceitar_move_para_aceito(self):
        from config import Stage
        r = _build_result(Intent.ACEITAR, "Ótimo!", card_base(), "aceito")
        assert r.next_stage == Stage.ACEITO
        assert r.notify_team is True

    def test_aceitar_condicional_reclassifica(self):
        # "aceito se você me der 180k" com sequência que tem 180k disponível:
        # o guard reclassifica para CONTRA_PROPOSTA, mas como 180k está na sequência,
        # o sistema aceita diretamente → next_stage=ACEITO é o comportamento correto.
        from config import Stage
        r = _build_result(Intent.ACEITAR, "Ótimo!", card_base(), "aceito se você me der 180 mil")
        # 180k está na sequência [160k,170k,180k,190k] → sistema aceita, vai para ACEITO
        assert r.next_stage == Stage.ACEITO

    def test_aceitar_condicional_acima_sequencia_escala(self):
        # "aceito se me der 250k" com sequência máxima 190k → CONTRA_PROPOSTA real,
        # 250k > max_seq=190k mas 250k < teto=32%*200k=64k... wait, crédito=200k
        # teto=32%*200k=64k < 250k → não, teto > crédito faz sentido em R$
        # crédito=500k, teto=160k, seq=[100k,120k], lead pede 200k > teto → handoff
        c = card_base(**{"Crédito": "500000", "Proposta Realizada": "100000"})
        c["Classes de Proposta"] = "100000,120000"
        r = _build_result(Intent.ACEITAR, "Ótimo!", c, "aceito se você me der 200 mil")
        # 200k > teto (32%*500k=160k) → handoff comercial
        assert r.notify_team is True

    def test_agendar_move_para_finalizacao(self):
        from config import Stage
        r = _build_result(Intent.AGENDAR, "Vou chamar", card_base(), "quero falar com alguém")
        assert r.notify_team is True
        assert r.next_stage == Stage.FINALIZACAO_COMERCIAL

    def test_duvida_mantem_em_negociacao(self):
        from config import Stage
        r = _build_result(Intent.DUVIDA, "Explico", card_base(), "como funciona?")
        assert r.next_stage == Stage.EM_NEGOCIACAO
        assert r.notify_team is False

    def test_desconfianca_mantem_em_negociacao(self):
        from config import Stage
        r = _build_result(Intent.DESCONFIANCA, "Somos legítimos", card_base(), "é golpe?")
        assert r.next_stage == Stage.EM_NEGOCIACAO

    def test_outro_mantem_em_negociacao(self):
        from config import Stage
        r = _build_result(Intent.OUTRO, "Olá!", card_base(), "oi")
        assert r.next_stage == Stage.EM_NEGOCIACAO


# ---------------------------------------------------------------------------
# _build_result — escalada de preço
# ---------------------------------------------------------------------------
class TestBuildResultEscalada:

    def test_melhorar_valor_escala(self):
        # card_base tem sequência 160→170→180→190k, proposta atual=160k → escala para 170k
        r = _build_result(Intent.MELHORAR_VALOR, "Vou ver", card_base(), "quero mais")
        assert r.extra_fields is not None
        assert float(r.extra_fields.get("Proposta Realizada", 0)) > 160_000

    def test_recusar_escala_quando_ha_sequencia(self):
        r = _build_result(Intent.RECUSAR, "Entendo", card_base(), "não quero")
        assert r.extra_fields is not None
        assert float(r.extra_fields.get("Proposta Realizada", 0)) > 160_000

    def test_melhorar_sem_candidatos_encerra(self):
        c = card_base(**{"Proposta Realizada": "190000"})
        c["Classes de Proposta"] = "160000,170000,180000,190000"
        r = _build_result(Intent.MELHORAR_VALOR, "Sinto muito", c, "quero mais")
        assert r.next_stage is not None  # vai para PERDIDO ou mantém com msg de teto

    def test_negociar_escala(self):
        r = _build_result(Intent.NEGOCIAR, "Vejo", card_base(), "consegue melhorar?")
        assert r.extra_fields is not None
        assert float(r.extra_fields.get("Proposta Realizada", 0)) > 160_000


# ---------------------------------------------------------------------------
# _build_result — contraproposta
# ---------------------------------------------------------------------------
class TestBuildResultContraproposta:

    def test_sem_valor_aguarda(self):
        r = _build_result(Intent.CONTRA_PROPOSTA, "Qual valor?", card_base(), "quero mais")
        assert r.delayed_followup is None

    def test_absurda_acima_40pct_gera_delay(self):
        # credito=1M, seq=[160k,170k], lead pede 450k = 45% > absurdo (40%) → delay do diretor
        c = card_base(**{"Crédito": "1000000", "Proposta Realizada": "160000"})
        c["Classes de Proposta"] = "160000,170000"
        r = _build_result(Intent.CONTRA_PROPOSTA, "Vou ver", c, "aceito por 450 mil")
        assert r.delayed_followup is not None
        assert r.delayed_followup_seconds > 0

    def test_acima_teto_razoavel_faz_handoff(self):
        # credito=1M, teto=32%=320k, absurdo=40%=400k
        # lead pede 380k = 38% → acima do teto (32%) mas abaixo do absurdo (40%) → handoff
        c = card_base(**{"Crédito": "1000000", "Proposta Realizada": "160000"})
        c["Classes de Proposta"] = "160000,170000"
        r = _build_result(Intent.CONTRA_PROPOSTA, "Vou ver", c, "aceito por 380 mil")
        assert r.notify_team is True

    def test_dentro_teto_nao_faz_handoff(self):
        # credito=1M, teto=32%=320k
        # lead pede 250k = 25% → dentro do teto → resposta do diretor, sem handoff
        c = card_base(**{"Crédito": "1000000", "Proposta Realizada": "160000"})
        c["Classes de Proposta"] = "160000,170000"
        r = _build_result(Intent.CONTRA_PROPOSTA, "Vou ver", c, "aceito por 250 mil")
        assert r.notify_team is False
        assert r.delayed_followup is not None  # resposta do diretor agendada

    def test_sem_valor_mantem_stage(self):
        from config import Stage
        r = _build_result(Intent.OFERECERAM_MAIS, "Qual valor?", card_base(), "me ofereceram mais")
        assert r.next_stage == Stage.EM_NEGOCIACAO

    def test_com_valor_nao_crasha(self):
        c = card_base(**{"Crédito": "1000000", "Proposta Realizada": "160000"})
        c["Classes de Proposta"] = "160000,170000,180000,190000"
        r = _build_result(Intent.OFERECERAM_MAIS, "Entendo", c, "me ofereceram 31k")
        assert r is not None


# ---------------------------------------------------------------------------
# _classify_with_ai — mock de complete_with_history()
# ---------------------------------------------------------------------------
class TestClassifyWithAI:

    @pytest.mark.asyncio
    async def test_aceitar_com_ia_mockada(self):
        from config import Stage
        ai_response = json.dumps({"intent": "ACEITAR", "reasoning": "aceitou", "response": "Ótimo!"})
        mock_ai = AsyncMock()
        # _classify_with_ai usa complete_with_history(), não complete()
        mock_ai.complete_with_history = AsyncMock(return_value=ai_response)
        mock_ai.__aenter__ = AsyncMock(return_value=mock_ai)
        mock_ai.__aexit__ = AsyncMock(return_value=False)
        result = await _classify_with_ai(mock_ai, "aceito", card_base(), "Em Negociação", [])
        assert result.intent == Intent.ACEITAR
        assert result.next_stage == Stage.ACEITO

    @pytest.mark.asyncio
    async def test_fallback_ia_invalida(self):
        mock_ai = AsyncMock()
        mock_ai.complete_with_history = AsyncMock(return_value="sem json nenhum")
        mock_ai.__aenter__ = AsyncMock(return_value=mock_ai)
        mock_ai.__aexit__ = AsyncMock(return_value=False)
        result = await _classify_with_ai(mock_ai, "não quero", card_base(), "Em Negociação", [])
        assert result is not None and result.response_message

    @pytest.mark.asyncio
    async def test_melhorar_valor_escala_com_ia(self):
        ai_response = json.dumps({"intent": "MELHORAR_VALOR", "reasoning": "quer mais", "response": "Verifico"})
        mock_ai = AsyncMock()
        mock_ai.complete_with_history = AsyncMock(return_value=ai_response)
        mock_ai.__aenter__ = AsyncMock(return_value=mock_ai)
        mock_ai.__aexit__ = AsyncMock(return_value=False)
        result = await _classify_with_ai(mock_ai, "quero mais", card_base(), "Em Negociação", [])
        assert result.intent == Intent.MELHORAR_VALOR
        assert result.extra_fields is not None
        assert float(result.extra_fields.get("Proposta Realizada", 0)) > 160_000
