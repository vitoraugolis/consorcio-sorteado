"""
tests/test_contemplacao_guard.py — Testes da guarda de contemplação

Verifica que cotas contempladas por LANCE nunca recebem proposta.
Cobre as duas camadas de defesa:
  1. qualificador.py — bloqueio na qualificação do extrato
  2. precificacao.py — bloqueio antes do envio da proposta
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import AsyncMock, patch, MagicMock, ANY

from config import Stage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_card(tipo_contemplacao="Sorteio", fonte="Bazar", stage_id=None, **overrides):
    base = {
        "id": "card-lance-test-001",
        "stage_id": stage_id or Stage.PRIMEIRA_ATIVACAO,
        "Nome do contato": "Carlos Teste",
        "Telefone": "5511999990002",
        "Adm": "Itaú",
        "Fonte": fonte,
        "Tipo contemplação": tipo_contemplacao,
        "Situação": "contemplada-sorteio",
        "Crédito": "200000",
        "Valor pago até o momento": "20000",
        "Proposta Realizada": "",
        "Aprovado Precificacao": "",
        "Link do Extrato": "",
        "Etiquetas": [],
        "ZapSign Token": "",
        "Classes de Proposta": "",
        "Notificado Precificacao": "",
    }
    base.update(overrides)
    return base


def _make_analise(tipo_contemplacao="Sorteio", resultado="QUALIFICADO"):
    """Cria um ExtratoAnalise mock."""
    from webhooks.qualificador import ExtratoAnalise, ExtratoResultado
    r = ExtratoResultado.QUALIFICADO if resultado == "QUALIFICADO" else ExtratoResultado.NAO_QUALIFICADO
    return ExtratoAnalise(
        resultado=r,
        administradora="Itaú",
        valor_credito=200_000.0,
        valor_pago=20_000.0,
        parcelas_pagas=12,
        total_parcelas=100,
        motivo="Elegível" if resultado == "QUALIFICADO" else "Não elegível",
        tipo_contemplacao=tipo_contemplacao,
        tipo_bem="Imóvel",
        grupo="0001",
        cota="0042",
    )


def _make_msg(media_type="image"):
    """Cria IncomingMessage mock com mídia."""
    from webhooks.router import IncomingMessage
    return IncomingMessage(
        phone="5511999990002",
        text=None,
        source="whapi",
        from_me=False,
        is_group=False,
        media_type=media_type,
        raw={"image": {"link": "https://exemplo.com/extrato.jpg"}},
    )


# ---------------------------------------------------------------------------
# CAMADA 1: qualificador.py
# ---------------------------------------------------------------------------

class TestQualificadorGuardaLance:

    @pytest.mark.asyncio
    async def test_lance_bloqueado_antes_de_precificacao(self):
        """
        Se extrato indica Lance, card NÃO deve ir para PRECIFICACAO.
        Deve ir para NAO_QUALIFICADO.
        """
        from webhooks.qualificador import handle_qualification

        card = _make_card(tipo_contemplacao="Lance")
        msg = _make_msg()
        analise = _make_analise(tipo_contemplacao="Lance")

        moved_to = []

        mock_faro = AsyncMock()
        mock_faro.__aenter__ = AsyncMock(return_value=mock_faro)
        mock_faro.__aexit__ = AsyncMock(return_value=False)
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock(side_effect=lambda cid, stage: moved_to.append(stage))

        with patch("webhooks.qualificador._analyze_extrato", AsyncMock(return_value=analise)), \
             patch("webhooks.qualificador.FaroClient", return_value=mock_faro), \
             patch("webhooks.qualificador._send_message", AsyncMock()), \
             patch("webhooks.qualificador.load_journey", return_value={}), \
             patch("webhooks.qualificador.save_journey", AsyncMock()), \
             patch("webhooks.qualificador.save_history_smart", AsyncMock()), \
             patch("webhooks.qualificador.load_history_smart", AsyncMock(return_value=[])), \
             patch("webhooks.qualificador.slack_warning", AsyncMock()):
            await handle_qualification(card=card, msg=msg)

        assert Stage.PRECIFICACAO not in moved_to, \
            "Card com lance NÃO deve ir para PRECIFICACAO"
        assert Stage.NAO_QUALIFICADO in moved_to, \
            "Card com lance deve ir para NAO_QUALIFICADO"

    @pytest.mark.asyncio
    async def test_contemplada_lance_variante_bloqueada(self):
        """Variante 'contemplada-lance' também deve ser bloqueada."""
        from webhooks.qualificador import handle_qualification

        card = _make_card(tipo_contemplacao="contemplada-lance")
        msg = _make_msg()
        analise = _make_analise(tipo_contemplacao="contemplada-lance")

        moved_to = []

        mock_faro = AsyncMock()
        mock_faro.__aenter__ = AsyncMock(return_value=mock_faro)
        mock_faro.__aexit__ = AsyncMock(return_value=False)
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock(side_effect=lambda cid, stage: moved_to.append(stage))

        with patch("webhooks.qualificador._analyze_extrato", AsyncMock(return_value=analise)), \
             patch("webhooks.qualificador.FaroClient", return_value=mock_faro), \
             patch("webhooks.qualificador._send_message", AsyncMock()), \
             patch("webhooks.qualificador.load_journey", return_value={}), \
             patch("webhooks.qualificador.save_journey", AsyncMock()), \
             patch("webhooks.qualificador.save_history_smart", AsyncMock()), \
             patch("webhooks.qualificador.load_history_smart", AsyncMock(return_value=[])), \
             patch("webhooks.qualificador.slack_warning", AsyncMock()):
            await handle_qualification(card=card, msg=msg)

        assert Stage.PRECIFICACAO not in moved_to
        assert Stage.NAO_QUALIFICADO in moved_to

    @pytest.mark.asyncio
    async def test_sorteio_passa_para_precificacao(self):
        """Cota de sorteio DEVE ir normalmente para PRECIFICACAO."""
        from webhooks.qualificador import handle_qualification

        card = _make_card(tipo_contemplacao="Sorteio")
        msg = _make_msg()
        analise = _make_analise(tipo_contemplacao="Sorteio")

        moved_to = []

        mock_faro = AsyncMock()
        mock_faro.__aenter__ = AsyncMock(return_value=mock_faro)
        mock_faro.__aexit__ = AsyncMock(return_value=False)
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock(side_effect=lambda cid, stage: moved_to.append(stage))
        mock_faro.save_history_smart = AsyncMock()

        with patch("webhooks.qualificador._analyze_extrato", AsyncMock(return_value=analise)), \
             patch("webhooks.qualificador.FaroClient", return_value=mock_faro), \
             patch("webhooks.qualificador._send_message", AsyncMock()), \
             patch("webhooks.qualificador.load_journey", return_value={}), \
             patch("webhooks.qualificador.save_journey", AsyncMock()), \
             patch("webhooks.qualificador.save_history_smart", AsyncMock()), \
             patch("webhooks.qualificador.load_history_smart", AsyncMock(return_value=[])):
            await handle_qualification(card=card, msg=msg)

        assert Stage.PRECIFICACAO in moved_to, \
            "Cota de sorteio DEVE ir para PRECIFICACAO"
        assert Stage.NAO_QUALIFICADO not in moved_to

    @pytest.mark.asyncio
    async def test_tipo_contemplacao_vazio_passa_para_precificacao(self):
        """
        tipo_contemplacao vazio/None = Gemini não identificou.
        Não deve bloquear — deixa passar para PRECIFICACAO.
        """
        from webhooks.qualificador import handle_qualification

        card = _make_card(tipo_contemplacao="")
        msg = _make_msg()
        analise = _make_analise(tipo_contemplacao="")

        moved_to = []

        mock_faro = AsyncMock()
        mock_faro.__aenter__ = AsyncMock(return_value=mock_faro)
        mock_faro.__aexit__ = AsyncMock(return_value=False)
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock(side_effect=lambda cid, stage: moved_to.append(stage))

        with patch("webhooks.qualificador._analyze_extrato", AsyncMock(return_value=analise)), \
             patch("webhooks.qualificador.FaroClient", return_value=mock_faro), \
             patch("webhooks.qualificador._send_message", AsyncMock()), \
             patch("webhooks.qualificador.load_journey", return_value={}), \
             patch("webhooks.qualificador.save_journey", AsyncMock()), \
             patch("webhooks.qualificador.save_history_smart", AsyncMock()), \
             patch("webhooks.qualificador.load_history_smart", AsyncMock(return_value=[])):
            await handle_qualification(card=card, msg=msg)

        # Vazio não deve bloquear — segue para PRECIFICACAO
        assert Stage.NAO_QUALIFICADO not in moved_to, \
            "tipo_contemplacao vazio não deve bloquear (Gemini incerto não é Lance confirmado)"

    @pytest.mark.asyncio
    async def test_slack_notificado_em_bloqueio_lance(self):
        """Slack deve ser alertado quando lance é bloqueado."""
        from webhooks.qualificador import handle_qualification

        card = _make_card(tipo_contemplacao="Lance")
        msg = _make_msg()
        analise = _make_analise(tipo_contemplacao="Lance")

        slack_calls = []

        mock_faro = AsyncMock()
        mock_faro.__aenter__ = AsyncMock(return_value=mock_faro)
        mock_faro.__aexit__ = AsyncMock(return_value=False)
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock()

        with patch("webhooks.qualificador._analyze_extrato", AsyncMock(return_value=analise)), \
             patch("webhooks.qualificador.FaroClient", return_value=mock_faro), \
             patch("webhooks.qualificador._send_message", AsyncMock()), \
             patch("webhooks.qualificador.load_journey", return_value={}), \
             patch("webhooks.qualificador.save_journey", AsyncMock()), \
             patch("webhooks.qualificador.save_history_smart", AsyncMock()), \
             patch("webhooks.qualificador.load_history_smart", AsyncMock(return_value=[])), \
             patch("webhooks.qualificador.slack_warning",
                   AsyncMock(side_effect=lambda *a, **kw: slack_calls.append(True))):
            await handle_qualification(card=card, msg=msg)

        assert len(slack_calls) > 0, "Slack deve ser notificado em bloqueio de lance"

    @pytest.mark.asyncio
    async def test_msg_nao_qualificado_enviada_para_lance(self):
        """Lead com lance deve receber MSG_NAO_QUALIFICADO, não mensagem de ativação."""
        from webhooks.qualificador import handle_qualification

        card = _make_card(tipo_contemplacao="Lance")
        msg = _make_msg()
        analise = _make_analise(tipo_contemplacao="Lance")

        msgs_enviadas = []

        mock_faro = AsyncMock()
        mock_faro.__aenter__ = AsyncMock(return_value=mock_faro)
        mock_faro.__aexit__ = AsyncMock(return_value=False)
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock()

        async def _capture_msg(card, phone, message, **kw):
            msgs_enviadas.append(message)

        with patch("webhooks.qualificador._analyze_extrato", AsyncMock(return_value=analise)), \
             patch("webhooks.qualificador.FaroClient", return_value=mock_faro), \
             patch("webhooks.qualificador._send_message",
                   AsyncMock(side_effect=_capture_msg)), \
             patch("webhooks.qualificador.load_journey", return_value={}), \
             patch("webhooks.qualificador.save_journey", AsyncMock()), \
             patch("webhooks.qualificador.save_history_smart", AsyncMock()), \
             patch("webhooks.qualificador.load_history_smart", AsyncMock(return_value=[])), \
             patch("webhooks.qualificador.slack_warning", AsyncMock()):
            await handle_qualification(card=card, msg=msg)

        assert len(msgs_enviadas) > 0, "Deve enviar mensagem ao lead"
        # Nenhuma mensagem deve conter "proposta" (seria MSG_QUALIFICADO)
        for m in msgs_enviadas:
            assert "proposta" not in m.lower() or "não" in m.lower(), \
                f"Lead lance não deve receber mensagem de proposta: {m[:80]}"


# ---------------------------------------------------------------------------
# CAMADA 2: precificacao.py (_process_card_locked)
# ---------------------------------------------------------------------------

class TestPrecificacaoGuardaLance:

    @pytest.mark.asyncio
    async def test_card_lance_bloqueado_antes_de_enviar_proposta(self):
        """
        Segunda camada: card com Tipo contemplação=Lance em PRECIFICACAO
        não deve ter proposta enviada.
        """
        from jobs.precificacao import _process_card_locked

        card = _make_card(
            tipo_contemplacao="Lance",
            stage_id=Stage.PRECIFICACAO,
            **{
                "Proposta Realizada": "40000",
                "Crédito": "200000",
                "Valor pago até o momento": "20000",
                "Link do Extrato": "https://exemplo.com/extrato.pdf",
            }
        )
        card["stage_id"] = Stage.PRECIFICACAO

        proposal_sent = []

        mock_faro = AsyncMock()
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock()

        with patch("jobs.precificacao._send_proposal",
                   AsyncMock(side_effect=lambda *a, **kw: proposal_sent.append(True) or True)), \
             patch("jobs.precificacao.slack_error", AsyncMock()):
            result = await _process_card_locked(mock_faro, card["id"])

        assert not proposal_sent, \
            "Proposta NÃO deve ser enviada para cota de lance (camada 2)"
        assert result is False

    @pytest.mark.asyncio
    async def test_card_lance_movido_para_nao_qualificado_na_precificacao(self):
        """Camada 2: card lance em PRECIFICACAO deve ir para NAO_QUALIFICADO."""
        from jobs.precificacao import _process_card_locked

        card = _make_card(
            tipo_contemplacao="Lance",
            stage_id=Stage.PRECIFICACAO,
            **{
                "Proposta Realizada": "40000",
                "Link do Extrato": "https://exemplo.com/extrato.pdf",
            }
        )
        card["stage_id"] = Stage.PRECIFICACAO

        moved_to = []

        mock_faro = AsyncMock()
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock(side_effect=lambda cid, stage: moved_to.append(stage))

        with patch("jobs.precificacao._send_proposal", AsyncMock()), \
             patch("jobs.precificacao.slack_error", AsyncMock()):
            await _process_card_locked(mock_faro, card["id"])

        assert Stage.NAO_QUALIFICADO in moved_to, \
            "Card lance deve ser movido para NAO_QUALIFICADO na camada 2"
        assert Stage.EM_NEGOCIACAO not in moved_to, \
            "Card lance não deve ir para EM_NEGOCIACAO"

    @pytest.mark.asyncio
    async def test_card_sorteio_passa_pela_precificacao(self):
        """Card de sorteio com proposta e extrato deve ter proposta enviada normalmente."""
        from jobs.precificacao import _process_card_locked

        card = _make_card(
            tipo_contemplacao="Sorteio",
            stage_id=Stage.PRECIFICACAO,
            **{
                "Proposta Realizada": "40000",
                "Crédito": "200000",
                "Valor pago até o momento": "20000",
                "Link do Extrato": "https://exemplo.com/extrato.pdf",
                "Aprovado Precificacao": "",
                "Notificado Precificacao": "",
            }
        )
        card["stage_id"] = Stage.PRECIFICACAO

        proposal_sent = []

        mock_faro = AsyncMock()
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock()

        with patch("jobs.precificacao._send_proposal",
                   AsyncMock(side_effect=lambda *a, **kw: proposal_sent.append(True) or True)), \
             patch("jobs.precificacao.slack_error", AsyncMock()), \
             patch("jobs.precificacao.load_history", return_value=[]), \
             patch("jobs.precificacao.history_append", side_effect=lambda h, r, c: h + [{}]), \
             patch("jobs.precificacao.save_history", AsyncMock()), \
             patch("jobs.precificacao.load_journey", return_value={}), \
             patch("jobs.precificacao.save_journey", AsyncMock()):
            await _process_card_locked(mock_faro, card["id"])

        assert len(proposal_sent) > 0, \
            "Cota de sorteio DEVE ter proposta enviada"

    @pytest.mark.asyncio
    async def test_contemplada_lance_variante_bloqueada_na_precificacao(self):
        """Variante 'contemplada-lance' também bloqueada na camada 2."""
        from jobs.precificacao import _process_card_locked

        card = _make_card(
            tipo_contemplacao="contemplada-lance",
            stage_id=Stage.PRECIFICACAO,
            **{
                "Proposta Realizada": "40000",
                "Link do Extrato": "https://exemplo.com/extrato.pdf",
            }
        )
        card["stage_id"] = Stage.PRECIFICACAO

        proposal_sent = []

        mock_faro = AsyncMock()
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock()

        with patch("jobs.precificacao._send_proposal",
                   AsyncMock(side_effect=lambda *a, **kw: proposal_sent.append(True) or True)), \
             patch("jobs.precificacao.slack_error", AsyncMock()):
            await _process_card_locked(mock_faro, card["id"])

        assert not proposal_sent, "Variante 'contemplada-lance' também deve ser bloqueada"

    @pytest.mark.asyncio
    async def test_slack_alertado_em_bloqueio_camada2(self):
        """Na camada 2, Slack deve ser alertado com severidade CRÍTICO."""
        from jobs.precificacao import _process_card_locked

        card = _make_card(
            tipo_contemplacao="Lance",
            stage_id=Stage.PRECIFICACAO,
            **{
                "Proposta Realizada": "40000",
                "Link do Extrato": "https://exemplo.com/extrato.pdf",
            }
        )
        card["stage_id"] = Stage.PRECIFICACAO

        slack_calls = []

        mock_faro = AsyncMock()
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock()

        # slack_error importado no topo de precificacao.py → patch no namespace do módulo
        with patch("jobs.precificacao._send_proposal", AsyncMock()), \
             patch("jobs.precificacao.slack_error",
                   AsyncMock(side_effect=lambda *a, **kw: slack_calls.append(True))):
            await _process_card_locked(mock_faro, card["id"])

        assert len(slack_calls) > 0, "Slack deve ser alertado na camada 2"


# ---------------------------------------------------------------------------
# Teste de integração: camada 1 garante que camada 2 nunca é atingida
# ---------------------------------------------------------------------------

class TestDefesaEmProfundidade:

    @pytest.mark.asyncio
    async def test_lance_nunca_chega_a_precificacao_via_qualificador(self):
        """
        Teste de integração: extrato com Lance no qualificador
        não deve resultar em nenhuma chamada a _process_card_locked.
        """
        from webhooks.qualificador import handle_qualification

        card = _make_card(tipo_contemplacao="Lance")
        msg = _make_msg()
        analise = _make_analise(tipo_contemplacao="Lance")

        precificacao_calls = []

        mock_faro = AsyncMock()
        mock_faro.__aenter__ = AsyncMock(return_value=mock_faro)
        mock_faro.__aexit__ = AsyncMock(return_value=False)
        mock_faro.get_card = AsyncMock(return_value=card)
        mock_faro.update_card = AsyncMock()
        mock_faro.move_card = AsyncMock()

        with patch("webhooks.qualificador._analyze_extrato", AsyncMock(return_value=analise)), \
             patch("webhooks.qualificador.FaroClient", return_value=mock_faro), \
             patch("webhooks.qualificador._send_message", AsyncMock()), \
             patch("webhooks.qualificador.load_journey", return_value={}), \
             patch("webhooks.qualificador.save_journey", AsyncMock()), \
             patch("webhooks.qualificador.save_history_smart", AsyncMock()), \
             patch("webhooks.qualificador.load_history_smart", AsyncMock(return_value=[])), \
             patch("webhooks.qualificador.slack_warning", AsyncMock()), \
             patch("jobs.precificacao.process_precificacao_card",
                   AsyncMock(side_effect=lambda *a: precificacao_calls.append(True))):
            await handle_qualification(card=card, msg=msg)

        assert not precificacao_calls, \
            "Lance bloqueado na camada 1 nunca deve chamar precificacao"
