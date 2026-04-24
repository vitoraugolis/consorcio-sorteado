"""
tests/test_agente_contrato.py — Testes de coleta de dados para contrato
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def card_assinatura(**overrides):
    base = {
        "id": "card-assinatura-001",
        "stage_id": "stage-assinatura",
        "Nome do contato": "Ana Lead",
        "Telefone": "5511988880001",
        "Fonte": "Lista",
        "Etiquetas": [],
        "Administradora": "Itaú",
        "Historico Conversa": "",
        "Dados Pessoais Texto": "",
        "ZapSign Token": "",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _extract_fields_with_ai
# ---------------------------------------------------------------------------

class TestExtractFieldsWithAI:

    @pytest.mark.asyncio
    async def test_extrai_dados_completos(self):
        from webhooks.agente_contrato import _extract_fields_with_ai

        ai_response = json.dumps({
            "CPF": "123.456.789-00",
            "RG": "12.345.678",
            "Endereco": "Rua das Flores 123, São Paulo - SP, 01000-000",
            "Email": "ana@email.com"
        })
        mock_ai = AsyncMock()
        mock_ai.complete = AsyncMock(return_value=ai_response)
        mock_ai.__aenter__ = AsyncMock(return_value=mock_ai)
        mock_ai.__aexit__ = AsyncMock(return_value=False)

        with patch("webhooks.agente_contrato.AIClient", return_value=mock_ai):
            result = await _extract_fields_with_ai(
                "CPF 123.456.789-00, RG 12.345.678, Rua das Flores 123 SP, ana@email.com"
            )

        assert result.get("CPF") == "123.456.789-00"
        assert result.get("Email") == "ana@email.com"

    @pytest.mark.asyncio
    async def test_fallback_extrai_cpf_e_email_via_regex(self):
        from webhooks.agente_contrato import _extract_fields_with_ai

        mock_ai = AsyncMock()
        mock_ai.complete = AsyncMock(side_effect=Exception("IA indisponível"))
        mock_ai.__aenter__ = AsyncMock(return_value=mock_ai)
        mock_ai.__aexit__ = AsyncMock(return_value=False)

        with patch("webhooks.agente_contrato.AIClient", return_value=mock_ai):
            result = await _extract_fields_with_ai(
                "meu cpf é 123.456.789-00 e email ana@email.com"
            )

        assert result.get("CPF") is not None
        assert result.get("Email") is not None

    @pytest.mark.asyncio
    async def test_ignora_email_malformado(self):
        from webhooks.agente_contrato import _extract_fields_with_ai

        mock_ai = AsyncMock()
        mock_ai.complete = AsyncMock(return_value=json.dumps({
            "CPF": "123.456.789-00", "RG": None,
            "Endereco": None, "Email": None
        }))
        mock_ai.__aenter__ = AsyncMock(return_value=mock_ai)
        mock_ai.__aexit__ = AsyncMock(return_value=False)

        with patch("webhooks.agente_contrato.AIClient", return_value=mock_ai):
            result = await _extract_fields_with_ai(
                "cpf 123.456.789-00 email: vitoremail.com"  # sem @
            )

        assert result.get("Email") is None  # email inválido ignorado

    @pytest.mark.asyncio
    async def test_aceita_cpf_sem_formatacao(self):
        from webhooks.agente_contrato import _extract_fields_with_ai

        mock_ai = AsyncMock()
        mock_ai.complete = AsyncMock(return_value=json.dumps({
            "CPF": "12345678900", "RG": None, "Endereco": None, "Email": None
        }))
        mock_ai.__aenter__ = AsyncMock(return_value=mock_ai)
        mock_ai.__aexit__ = AsyncMock(return_value=False)

        with patch("webhooks.agente_contrato.AIClient", return_value=mock_ai):
            result = await _extract_fields_with_ai("meu cpf 12345678900")

        assert result.get("CPF") == "12345678900"


# ---------------------------------------------------------------------------
# _load_collected / _save_collected
# ---------------------------------------------------------------------------

class TestLoadCollected:

    def test_card_sem_dados_retorna_vazio(self):
        from webhooks.agente_contrato import _load_collected
        card = card_assinatura()
        assert _load_collected(card) == {}

    def test_card_com_json_valido(self):
        from webhooks.agente_contrato import _load_collected
        dados = {"CPF": "123.456.789-00", "Email": "a@b.com"}
        card = card_assinatura(**{"Dados Pessoais Texto": json.dumps(dados)})
        result = _load_collected(card)
        assert result["CPF"] == "123.456.789-00"

    def test_card_com_json_invalido_retorna_vazio(self):
        from webhooks.agente_contrato import _load_collected
        card = card_assinatura(**{"Dados Pessoais Texto": "isso nao é json {{{}"})
        assert _load_collected(card) == {}


# ---------------------------------------------------------------------------
# handle_dados_pessoais — fluxo de coleta
# ---------------------------------------------------------------------------

class TestHandleDadosPessoais:

    def _make_mocks(self, card, ai_fields=None, ai_response="Preciso de mais dados"):
        """Monta mocks de FaroClient, WhapiClient e AIClient."""
        faro = MagicMock()
        faro.get_card = AsyncMock(return_value=card)
        faro.update_card = AsyncMock()
        faro.__aenter__ = AsyncMock(return_value=faro)
        faro.__aexit__ = AsyncMock(return_value=False)

        whapi = MagicMock()
        whapi.send_text = AsyncMock()
        whapi.__aenter__ = AsyncMock(return_value=whapi)
        whapi.__aexit__ = AsyncMock(return_value=False)

        ai_extract_resp = json.dumps(ai_fields or {
            "CPF": None, "RG": None, "Endereco": None, "Email": None
        })
        ai = MagicMock()
        ai.complete = AsyncMock(side_effect=[ai_extract_resp, ai_response])
        ai.__aenter__ = AsyncMock(return_value=ai)
        ai.__aexit__ = AsyncMock(return_value=False)

        return faro, whapi, ai

    @pytest.mark.asyncio
    async def test_dados_completos_de_uma_vez(self):
        from webhooks.agente_contrato import handle_dados_pessoais

        card = card_assinatura()
        dados_completos = {
            "CPF": "123.456.789-00",
            "RG": "12.345.678",
            "Endereco": "Rua A 1, SP",
            "Email": "a@b.com"
        }
        faro, whapi, ai = self._make_mocks(
            card,
            ai_fields=dados_completos,
            ai_response="Dados completos! Envie o extrato."
        )

        with patch("webhooks.agente_contrato.FaroClient", return_value=faro), \
             patch("webhooks.agente_contrato.WhapiClient", return_value=whapi), \
             patch("webhooks.agente_contrato.AIClient", return_value=ai):
            await handle_dados_pessoais(
                card,
                "CPF 123.456.789-00, RG 12.345.678, Rua A 1 SP, a@b.com"
            )

        # Deve ter atualizado o card com os dados
        assert faro.update_card.called
        # Deve ter enviado mensagem
        assert whapi.send_text.called

    @pytest.mark.asyncio
    async def test_dados_fragmentados_acumula(self):
        from webhooks.agente_contrato import handle_dados_pessoais, _load_collected
        import json as _json

        card = card_assinatura()

        # Mensagem 1: só CPF
        faro1, whapi1, ai1 = self._make_mocks(
            card, ai_fields={"CPF": "111.222.333-44", "RG": None, "Endereco": None, "Email": None}
        )
        with patch("webhooks.agente_contrato.FaroClient", return_value=faro1), \
             patch("webhooks.agente_contrato.WhapiClient", return_value=whapi1), \
             patch("webhooks.agente_contrato.AIClient", return_value=ai1):
            await handle_dados_pessoais(card, "meu cpf 111.222.333-44")

        # Simular que o card foi atualizado com CPF
        if faro1.update_card.called:
            for call_args in faro1.update_card.call_args_list:
                fields = call_args[0][1] if call_args[0] else call_args[1].get("fields", {})
                if "Dados Pessoais Texto" in fields:
                    card["Dados Pessoais Texto"] = fields["Dados Pessoais Texto"]

        collected = _load_collected(card)
        assert "CPF" in collected

    @pytest.mark.asyncio
    async def test_extrato_antes_dos_dados_pede_dados_primeiro(self):
        from webhooks.agente_contrato import handle_extrato_recebido

        card = card_assinatura()  # Dados Pessoais Texto vazio

        whapi = MagicMock()
        whapi.send_text = AsyncMock()
        whapi.__aenter__ = AsyncMock(return_value=whapi)
        whapi.__aexit__ = AsyncMock(return_value=False)

        faro = MagicMock()
        faro.update_card = AsyncMock()
        faro.__aenter__ = AsyncMock(return_value=faro)
        faro.__aexit__ = AsyncMock(return_value=False)

        msg = MagicMock()
        msg.media_type = "image"

        with patch("webhooks.agente_contrato.WhapiClient", return_value=whapi), \
             patch("webhooks.agente_contrato.FaroClient", return_value=faro):
            await handle_extrato_recebido(card, msg)

        # Deve pedir os dados — não chamar generate_and_send_contract
        assert whapi.send_text.called
        texto_enviado = whapi.send_text.call_args[0][1]
        assert any(w in texto_enviado.lower() for w in ["cpf", "rg", "endereço", "dados"])

    @pytest.mark.asyncio
    async def test_extrato_com_dados_completos_gera_contrato(self):
        from webhooks.agente_contrato import handle_extrato_recebido

        dados = {"CPF": "111.222.333-44", "RG": "12345", "Endereco": "Rua A 1", "Email": "a@b.com"}
        card = card_assinatura(**{"Dados Pessoais Texto": json.dumps(dados)})

        whapi = MagicMock()
        whapi.send_text = AsyncMock()
        whapi.__aenter__ = AsyncMock(return_value=whapi)
        whapi.__aexit__ = AsyncMock(return_value=False)

        faro = MagicMock()
        faro.update_card = AsyncMock()
        faro.__aenter__ = AsyncMock(return_value=faro)
        faro.__aexit__ = AsyncMock(return_value=False)

        msg = MagicMock()
        msg.media_type = "image"

        contract_mock = AsyncMock()

        with patch("webhooks.agente_contrato.WhapiClient", return_value=whapi), \
             patch("webhooks.agente_contrato.FaroClient", return_value=faro), \
             patch("webhooks.agente_contrato.FaroClient", return_value=faro), \
             patch("jobs.contrato.generate_and_send_contract", contract_mock), \
             patch("asyncio.create_task") as mock_task:
            await handle_extrato_recebido(card, msg)

        # Deve ter disparado confirmação ao lead
        assert whapi.send_text.called
        # Deve ter criado task para gerar contrato
        assert mock_task.called
