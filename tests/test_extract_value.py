"""
tests/test_extract_value.py — Testes de extração de valores monetários
Suite síncrona — não precisa de pytest-asyncio
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from webhooks.negociador import _extract_lead_value, _message_has_value


class TestExtractLeadValue:

    def test_formato_k_minusculo(self):
        assert _extract_lead_value("Recebi uma proposta de 31k") == 31_000

    def test_formato_k_sem_espaco(self):
        assert _extract_lead_value("me ofereceram 50k") == 50_000

    def test_formato_k_maiusculo(self):
        assert _extract_lead_value("proposta de 90K") == 90_000

    def test_formato_mil(self):
        assert _extract_lead_value("Me ofereceram 320 mil") == 320_000

    def test_formato_mil_sem_espaco(self):
        assert _extract_lead_value("Me ofereceram 320mil") == 320_000

    def test_formato_reais_br(self):
        assert _extract_lead_value("R$ 320.000,00") == 320_000

    def test_formato_reais_sem_espaco(self):
        assert _extract_lead_value("R$320.000") == 320_000

    def test_formato_milhar_br(self):
        assert _extract_lead_value("Recebi 320.000") == 320_000

    def test_formato_milhar_grande(self):
        assert _extract_lead_value("Me deram 1.200.000") == 1_200_000

    def test_numero_curto_sem_ancora_retorna_literal(self):
        # "320" sem âncora — retornado como literal (320)
        assert _extract_lead_value("proposta de 320") == 320

    def test_numero_curto_com_ancora_interpreta_como_mil(self):
        # "320" com proposta de 200k → 320 < 2000 (1% de 200k) → 320.000
        assert _extract_lead_value("proposta de 320", proposta_atual=200_000) == 320_000

    def test_numero_curto_com_ancora_pequena_nao_amplia(self):
        # "320" com proposta de 100 reais → 320 > 1 (1% de 100) → não amplia
        result = _extract_lead_value("proposta de 320", proposta_atual=100)
        assert result == 320

    def test_valor_quatro_digitos_sem_ancora(self):
        assert _extract_lead_value("me pagaram 9500") == 9_500

    def test_recebi_280_mil(self):
        assert _extract_lead_value("recebi 280 mil") == 280_000

    def test_string_vazia(self):
        assert _extract_lead_value("") == 0

    def test_sem_valor(self):
        assert _extract_lead_value("oi tudo bem?") == 0

    def test_so_emoji(self):
        assert _extract_lead_value("😊👍") == 0


class TestMessageHasValue:

    def test_detecta_k(self):
        assert _message_has_value("proposta de 31k") is True

    def test_detecta_mil(self):
        assert _message_has_value("320 mil reais") is True

    def test_detecta_reais(self):
        assert _message_has_value("R$ 200.000") is True

    def test_detecta_numero_longo(self):
        assert _message_has_value("quero 320000") is True

    def test_nao_detecta_sem_valor(self):
        assert _message_has_value("quero negociar") is False

    def test_nao_detecta_numero_curto(self):
        assert _message_has_value("sim") is False
