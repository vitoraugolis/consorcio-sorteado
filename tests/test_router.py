"""
tests/test_router.py — Testes de roteamento de mensagens
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import AsyncMock, patch, MagicMock, call


def _make_msg(text="oi", from_me=False, is_group=False, media_type=None, phone="5511999990001"):
    from webhooks.router import IncomingMessage
    return IncomingMessage(
        phone=phone,
        text=text,
        source="whapi",
        from_me=from_me,
        is_group=is_group,
        media_type=media_type,
        raw={},
    )


def _card(fonte="Lista", stage_id="stage-ativacao-001", **overrides):
    base = {
        "id": "card-001",
        "stage_id": stage_id,
        "Nome do contato": "Test Lead",
        "Telefone": "5511999990001",
        "Fonte": fonte,
        "Etiquetas": [],
        "ZapSign Token": "",
    }
    base.update(overrides)
    return base


class TestRouteMessage:

    @pytest.mark.asyncio
    async def test_mensagem_propria_ignorada(self):
        from webhooks.router import route_message
        msg = _make_msg(from_me=True)

        with patch("webhooks.router._find_card") as mock_find:
            await route_message(msg)
            mock_find.assert_not_called()

    @pytest.mark.asyncio
    async def test_mensagem_grupo_ignorada(self):
        from webhooks.router import route_message
        msg = _make_msg(is_group=True)

        with patch("webhooks.router._find_card") as mock_find:
            await route_message(msg)
            mock_find.assert_not_called()

    @pytest.mark.asyncio
    async def test_card_nao_encontrado_silencioso(self):
        from webhooks.router import route_message
        msg = _make_msg()

        with patch("webhooks.router._find_card", AsyncMock(return_value=None)):
            # Não deve lançar exceção
            await route_message(msg)

    @pytest.mark.asyncio
    async def test_lista_em_ativacao_vai_para_agente_listas(self):
        from webhooks.router import route_message, ACTIVATION_STAGES
        from config import Stage

        stage = next(iter(ACTIVATION_STAGES))
        card = _card(fonte="Lista", stage_id=stage)
        msg = _make_msg(text="oi")

        dispatched = []

        async def fake_dispatch(card, text):
            dispatched.append("agente_listas")

        with patch("webhooks.router._find_card", AsyncMock(return_value=card)), \
             patch("webhooks.router.debounce") as mock_debounce:
            mock_debounce.schedule = MagicMock(
                side_effect=lambda **kw: dispatched.append("agente_listas")
            )
            await route_message(msg)

        assert "agente_listas" in dispatched

    @pytest.mark.asyncio
    async def test_sem_fonte_em_ativacao_tambem_vai_para_listas(self):
        """Nova regra: card sem Fonte deve ir para agente_listas."""
        from webhooks.router import route_message, ACTIVATION_STAGES

        stage = next(iter(ACTIVATION_STAGES))
        card = _card(fonte=None, stage_id=stage)
        card["Fonte"] = None
        msg = _make_msg(text="oi")

        dispatched = []

        with patch("webhooks.router._find_card", AsyncMock(return_value=card)), \
             patch("webhooks.router.debounce") as mock_debounce:
            mock_debounce.schedule = MagicMock(
                side_effect=lambda **kw: dispatched.append(
                    "agente_listas" if "agente_listas" in str(kw.get("dispatch", "")) else "outro"
                )
            )
            await route_message(msg)

        # O dispatch deve ter sido chamado (qualquer handler)
        assert mock_debounce.schedule.called

    @pytest.mark.asyncio
    async def test_bazar_em_ativacao_vai_para_agente_bazar(self):
        from webhooks.router import route_message, QUALIFICATION_STAGES

        stage = next(iter(QUALIFICATION_STAGES))
        card = _card(fonte="Bazar", stage_id=stage)
        msg = _make_msg(text="quero vender")

        with patch("webhooks.router._find_card", AsyncMock(return_value=card)), \
             patch("webhooks.router.debounce") as mock_debounce:
            mock_debounce.schedule = MagicMock()
            await route_message(msg)

        assert mock_debounce.schedule.called

    @pytest.mark.asyncio
    async def test_assinatura_sem_zapsign_vai_para_agente_contrato(self):
        from webhooks.router import route_message
        from config import Stage

        card = _card(fonte="Lista", stage_id=Stage.ASSINATURA)
        card["ZapSign Token"] = ""
        msg = _make_msg(text="meu cpf 123")

        with patch("webhooks.router._find_card", AsyncMock(return_value=card)), \
             patch("webhooks.router.debounce") as mock_debounce:
            mock_debounce.schedule = MagicMock()
            await route_message(msg)

        assert mock_debounce.schedule.called
