"""
tests/test_negociador.py — Testes da lógica de negociação (sem IO externo)
Testa _build_result, _get_next_proposal e _classify_with_ai (mockando IA).

Correções aplicadas (2026-05-08):
  - Campo correto para sequência de propostas: "Classes de Proposta" (não "Sequencia_Proposta")
  - _classify_with_ai usa complete_with_history() — mock atualizado
  - test_acima_sequencia_razoavel_faz_handoff: cenário corrigido (lead pede 380k = 38% de 1M,
    acima do teto de 32% e abaixo do absurdo de 40%)
  - test_escala_normal / test_salta_para_max: fixtures corrigidas com campo certo

Correções aplicadas (2026-05-15) — Bug lead 1c55c3d4:
  - _extract_lead_value agora retorna o MAIOR valor encontrado (não o primeiro)
  - _get_next_proposal bloqueia aceite quando lead_value ≤ ultima_proposta
  - Novos testes: test_extract_retorna_maior_valor, test_nao_aceita_valor_abaixo_proposta_atual,
    test_nao_aceita_valor_incremental_ambiguo
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


# ---------------------------------------------------------------------------
# Testes dos fixes do bug lead 1c55c3d4 (2026-05-15)
# ---------------------------------------------------------------------------

class TestExtractLeadValueMaximo:
    """
    Fix #1: _extract_lead_value deve retornar o MAIOR valor, não o primeiro.
    Cenário do bug: mensagem continha "17 mil" antes de "43" → retornava 17000.
    Correto: retornar max([17000, 43000]) = 43000.
    """

    def test_retorna_maior_valor_quando_ha_dois_numeros(self):
        # "17 mil a mais além dos 43" → deve retornar 43000, não 17000
        msg = "Desculpa, mais que o dobro, quero 17 mil a mais Desculpa, mas a DM como ofereceu 43, tá bom?"
        val = _extract_lead_value(msg, proposta_atual=26000)
        assert val == 43_000, f"esperado 43000, got {val}"

    def test_retorna_maior_quando_r_cifrao_e_numero_solto(self):
        # "R$ 36.000... mais 43" → deve retornar 43000 (maior)
        msg = "me ofereça R$ 36.000 ou melhor ainda uns 43 mil"
        val = _extract_lead_value(msg, proposta_atual=26000)
        assert val == 43_000

    def test_mensagem_simples_43_mil(self):
        val = _extract_lead_value("aceito por 43 mil", proposta_atual=26000)
        assert val == 43_000

    def test_mensagem_com_apenas_17_retorna_17000(self):
        # Sem concorrência, "17 mil" deve retornar 17000
        val = _extract_lead_value("quero 17 mil", proposta_atual=26000)
        assert val == 17_000

    def test_mensagem_r_cifrao_43000(self):
        val = _extract_lead_value("aceito por R$ 43.000", proposta_atual=26000)
        assert val == 43_000

    def test_cenario_exato_do_bug(self):
        """
        Reproduz o cenário do lead 1c55c3d4:
        Áudio transcrito (125 chars) + texto concatenados pelo debounce.
        A transcrição continha '17 mil' antes do '43'.
        Com o fix, deve retornar 43000 (o maior valor).
        """
        msg_combinada = (
            "Desculpa, mas a DM como ofereceu 43, tá bom? Muito obrigado. "
            "Mais que o dobro, quero 17 mil a mais "
            "Desculpa, mas a DM como ofereceu 43, tá bom? Muito obrigado."
        )
        val = _extract_lead_value(msg_combinada, proposta_atual=26000)
        assert val == 43_000, (
            f"Fix regressão: esperado 43000 (maior valor na msg), got {val}. "
            f"Cenário do bug lead 1c55c3d4."
        )


class TestGetNextProposalGuardaValorAbaixo:
    """
    Fix #2: _get_next_proposal não deve aceitar lead_value ≤ ultima_proposta.
    Evita que valores incrementais ("mais 17 mil") sejam registrados como proposta.
    """

    def test_nao_aceita_lead_value_abaixo_da_proposta_atual(self):
        # Card com proposta=26000; lead_value=17000 (< 26000) → NÃO deve aceitar
        c = card_base(**{
            "Crédito": "114831",
            "Proposta Realizada": "26000",
            "Classes de Proposta": "26000,31000,34000,36000",
            "Indice da Proposta": "1",
        })
        r = _get_next_proposal(c, lead_value=17_000)
        assert r.get("aceitar_contraproposta") is not True, (
            "Bug regressão: aceitar_contraproposta=True com lead_value=17000 < proposta_atual=26000"
        )
        # Deve ter escalado normalmente (não aceite automático)
        assert r.get("nova_proposta", 0) >= 26_000

    def test_nao_aceita_lead_value_igual_a_proposta_atual(self):
        # lead_value == ultima_proposta → também não deve aceitar automaticamente
        c = card_base(**{
            "Crédito": "114831",
            "Proposta Realizada": "26000",
            "Classes de Proposta": "26000,31000,34000,36000",
        })
        r = _get_next_proposal(c, lead_value=26_000)
        assert r.get("aceitar_contraproposta") is not True

    def test_aceita_lead_value_dentro_da_sequencia_e_acima_do_atual(self):
        # lead_value=31000 > ultima_proposta=26000 E ≤ max_seq=36000 → deve aceitar
        c = card_base(**{
            "Crédito": "114831",
            "Proposta Realizada": "26000",
            "Classes de Proposta": "26000,31000,34000,36000",
        })
        r = _get_next_proposal(c, lead_value=31_000)
        assert r.get("aceitar_contraproposta") is True
        assert r.get("nova_proposta") == 31_000

    def test_cenario_exato_do_bug_17000_nao_aceito(self):
        """
        Reproduz _get_next_proposal com os valores reais do lead 1c55c3d4.
        Proposta=26000, sequência=26000,31000,34000,36000, lead_value=17000.
        Com o fix, NÃO deve gerar aceite automático.
        """
        c = card_base(**{
            "Crédito": "114831",
            "Proposta Realizada": "26000",
            "Classes de Proposta": "26000,31000,34000,36000",
            "Indice da Proposta": "1",
        })
        r = _get_next_proposal(c, lead_value=17_000)
        assert r.get("aceitar_contraproposta") is not True, (
            "Regressão crítica: sistema aceitou R$ 17.000 como proposta sendo proposta_atual=R$ 26.000"
        )
        nova = r.get("nova_proposta", 0)
        assert nova >= 26_000, f"Nova proposta {nova} deve ser ≥ 26000"

