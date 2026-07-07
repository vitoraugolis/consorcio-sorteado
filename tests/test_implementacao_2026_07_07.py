"""
tests/test_implementacao_2026_07_07.py
Bateria de testes para as mudanças implementadas em 2026-07-07:

  1. Pool de tokens Listas (5 canais após migração DEADPL + DAREDL)
  2. notify_team(): ordem de canais corrigida (lista → bazar)
  3. _build_lp_pool() / _build_bazar_pool(): fallback silencioso sem token
  4. TOKEN_GAP_MIN_S = 720s (cadência 130–150 disparos/dia)
  5. agente_listas: INTERESSE dispara send_proposal_now via create_task
  6. precificacao: mensagem SDR após proposta
  7. precificacao: notify_team após proposta
  8. send_proposal_now: sem guard de janela horária
"""

import asyncio
import os
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

# ---------------------------------------------------------------------------
# Helpers de card/mock
# ---------------------------------------------------------------------------

def _make_card(
    card_id="card-abc12345",
    nome="João Silva",
    phone="5511999990001",
    adm="Porto Seguro",
    stage=None,
    fonte="lista",
    credito="200000",
    valor_pago="20000",
    proposta="40000",
    pct_pago="10",
):
    from config import Stage
    return {
        "id": card_id,
        "title": nome,
        "Nome do contato": nome,
        "Telefone": phone,
        "Adm": adm,                   # campo correto para get_adm()
        "Administradora": adm,        # campo alternativo
        "stage_id": stage or Stage.PRECIFICACAO,
        "Fonte": fonte,
        "Crédito": credito,
        "Valor pago até o momento": valor_pago,
        "Proposta Realizada": proposta,
        "Porcentagem paga até o momento": pct_pago,
        "Grupo": "12345",
        "Cota": "001",
        "Tipo contemplação": "Sorteio",
        "Tipo de bem": "imovel",
    }


# ===========================================================================
# 1. Pool de tokens — configuração do .env
# ===========================================================================

class TestPoolTokensListas(unittest.TestCase):
    """Garante que o pool de Listas lê os 5 tokens do .env."""

    def test_pool_tem_5_tokens(self):
        from config import WHAPI_LISTA_TOKENS
        self.assertEqual(len(WHAPI_LISTA_TOKENS), 5,
                         f"Esperado 5 tokens, encontrados {len(WHAPI_LISTA_TOKENS)}: {WHAPI_LISTA_TOKENS}")

    def test_deadpl_no_pool(self):
        """O token DEADPL (LISTA_4) deve estar no pool."""
        from config import WHAPI_LISTA_TOKENS
        token_lp = os.getenv("WHAPI_TOKEN_LISTA_4", "")
        self.assertIn(token_lp, WHAPI_LISTA_TOKENS)

    def test_daredl_no_pool(self):
        """O token DAREDL (LISTA_5, ex-Bazar) deve estar no pool."""
        from config import WHAPI_LISTA_TOKENS
        token_bazar = os.getenv("WHAPI_TOKEN_LISTA_5", "")
        self.assertIn(token_bazar, WHAPI_LISTA_TOKENS)

    def test_bazar_token_vazio(self):
        """WHAPI_TOKEN_BAZAR deve estar vazio após migração."""
        from config import WHAPI_BAZAR_TOKEN
        self.assertEqual(WHAPI_BAZAR_TOKEN, "",
                         "WHAPI_TOKEN_BAZAR deveria estar vazio após migração para Listas")

    def test_lp_token_vazio(self):
        """WHAPI_TOKEN_LP deve estar vazio após migração."""
        from config import WHAPI_LP_TOKEN
        self.assertEqual(WHAPI_LP_TOKEN, "",
                         "WHAPI_TOKEN_LP deveria estar vazio após migração para Listas")


# ===========================================================================
# 2. _build_bazar_pool / _build_lp_pool — fallback silencioso
# ===========================================================================

class TestPoolFallbacks(unittest.TestCase):
    """Bazar e LP sem token dedicado devem usar pool de Listas como fallback."""

    def test_bazar_pool_usa_lista_como_fallback(self):
        with patch("config.WHAPI_BAZAR_TOKEN", ""), \
             patch("config.WHAPI_LISTA_TOKENS", ["tok1", "tok2"]):
            # Reimporta a função isolada
            import importlib
            import services.whapi as wm
            importlib.reload(wm)
            pool = wm._build_bazar_pool()
            self.assertEqual(pool, ["tok1", "tok2"])

    def test_lp_pool_usa_lista_como_fallback(self):
        with patch("config.WHAPI_LP_TOKEN", ""), \
             patch("config.WHAPI_LISTA_TOKENS", ["tok1", "tok2"]):
            import importlib
            import services.whapi as wm
            importlib.reload(wm)
            pool = wm._build_lp_pool()
            self.assertEqual(pool, ["tok1", "tok2"])

    def test_bazar_pool_sem_fallback_retorna_vazio(self):
        with patch("config.WHAPI_BAZAR_TOKEN", ""), \
             patch("config.WHAPI_LISTA_TOKENS", []):
            import importlib
            import services.whapi as wm
            importlib.reload(wm)
            pool = wm._build_bazar_pool()
            self.assertEqual(pool, [])

    def test_lp_pool_sem_fallback_retorna_vazio(self):
        with patch("config.WHAPI_LP_TOKEN", ""), \
             patch("config.WHAPI_LISTA_TOKENS", []):
            import importlib
            import services.whapi as wm
            importlib.reload(wm)
            pool = wm._build_lp_pool()
            self.assertEqual(pool, [])


# ===========================================================================
# 3. notify_team() — ordem correta de canais
# ===========================================================================

class TestNotifyTeamOrdemCanais(unittest.IsolatedAsyncioTestCase):
    """notify_team deve tentar 'lista' primeiro (DEADPL está no pool lista)."""

    async def test_tenta_lista_primeiro(self):
        """Primeira tentativa deve ser canal 'lista'."""
        canais_tentados = []

        class FakeWhapiClient:
            def __init__(self, canal=None, token=None):
                self._canal = canal
            async def __aenter__(self):
                canais_tentados.append(self._canal)
                return self
            async def __aexit__(self, *a): pass
            async def send_text(self, to, msg): pass

        with patch("services.whapi.WhapiClient", FakeWhapiClient), \
             patch("config.NOTIFY_GROUP", "120363406133061169@g.us"), \
             patch("config.NOTIFY_PHONES", []):
            from services.whapi import notify_team
            await notify_team("teste")

        self.assertEqual(canais_tentados[0], "lista",
                         f"Primeiro canal tentado: {canais_tentados[0]}, esperado 'lista'")

    async def test_fallback_para_bazar_se_lista_falhar(self):
        """Se lista falhar, deve tentar bazar."""
        canais_tentados = []

        class FakeWhapiClientFail:
            def __init__(self, canal=None, token=None):
                self._canal = canal
            async def __aenter__(self):
                canais_tentados.append(self._canal)
                return self
            async def __aexit__(self, *a): pass
            async def send_text(self, to, msg):
                from services.whapi import WhapiError
                if self._canal == "lista":
                    raise WhapiError("canal lista falhou", status_code=401)

        with patch("services.whapi.WhapiClient", FakeWhapiClientFail), \
             patch("config.NOTIFY_GROUP", "120363406133061169@g.us"), \
             patch("config.NOTIFY_PHONES", []):
            from services.whapi import notify_team
            await notify_team("teste")

        self.assertIn("bazar", canais_tentados,
                      f"Bazar não foi tentado após falha de lista. Canais: {canais_tentados}")

    async def test_nao_tenta_lp(self):
        """Canal 'lp' não deve ser tentado (pool vazio após migração)."""
        canais_tentados = []

        class FakeWhapiClient:
            def __init__(self, canal=None, token=None):
                self._canal = canal
            async def __aenter__(self):
                canais_tentados.append(self._canal)
                return self
            async def __aexit__(self, *a): pass
            async def send_text(self, to, msg): pass

        with patch("services.whapi.WhapiClient", FakeWhapiClient), \
             patch("config.NOTIFY_GROUP", "120363406133061169@g.us"), \
             patch("config.NOTIFY_PHONES", []):
            from services.whapi import notify_team
            await notify_team("teste")

        self.assertNotIn("lp", canais_tentados,
                         f"Canal 'lp' não deveria ser tentado. Canais tentados: {canais_tentados}")


# ===========================================================================
# 4. TOKEN_GAP_MIN_S = 720s
# ===========================================================================

class TestTokenGapCadencia(unittest.TestCase):
    """Verifica que o gap de token garante a cadência de 130–150/dia."""

    def test_gap_configurado_corretamente(self):
        gap = int(os.getenv("FILA_LISTAS_TOKEN_GAP_S", "1500"))
        self.assertEqual(gap, 720,
                         f"FILA_LISTAS_TOKEN_GAP_S={gap}, esperado 720s")

    def test_cadencia_diaria_dentro_da_meta(self):
        """Com 5 tokens e gap de 720s, capacidade dentro da janela 08h-20h."""
        gap_s = int(os.getenv("FILA_LISTAS_TOKEN_GAP_S", "720"))
        n_tokens = 5
        janela_h = 12  # 08h–20h
        janela_s = janela_h * 3600
        # Disparos por token por dia
        disparos_por_token = janela_s // gap_s
        # Total do pool
        total = disparos_por_token * n_tokens
        self.assertGreaterEqual(total, 130, f"Capacidade total {total} < 130")
        self.assertLessEqual(total, 300,
                             f"Capacidade total {total} muito alta — risco de ban")

    def test_fila_listas_le_do_env(self):
        """fila_listas.py deve ler TOKEN_GAP_MIN_S do .env."""
        with patch.dict(os.environ, {"FILA_LISTAS_TOKEN_GAP_S": "720"}):
            import importlib
            import jobs.fila_listas as fl
            importlib.reload(fl)
            self.assertEqual(fl.TOKEN_GAP_MIN_S, 720)


# ===========================================================================
# 5. agente_listas — INTERESSE dispara send_proposal_now via create_task
# ===========================================================================

class TestAgentListasInteresse(unittest.IsolatedAsyncioTestCase):
    """Garante que intent INTERESSE dispara send_proposal_now como task."""

    async def test_interesse_cria_task_precificacao(self):
        from config import Stage
        card = _make_card(stage=Stage.PRECIFICACAO)
        tasks_criadas = []

        async def fake_move_card(card_id, stage): pass
        async def fake_update_card(card_id, fields): pass
        async def fake_send_proposal_now(c): pass

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def move_card(self, cid, stage): pass
            async def update_card(self, cid, fields): pass

        def fake_create_task(coro):
            tasks_criadas.append(coro)
            # Cancela a coroutine para não vazar
            coro.close()
            return MagicMock()

        with patch("webhooks.agente_listas.FaroClient", return_value=FakeFaro()), \
             patch("jobs.precificacao.send_proposal_now", fake_send_proposal_now):
            import asyncio as _asyncio
            with patch.object(_asyncio, "create_task", side_effect=fake_create_task):
                from webhooks.agente_listas import _handle_intent
                await _handle_intent("INTERESSE", card)

        self.assertEqual(len(tasks_criadas), 1,
                         f"Esperado 1 create_task, encontrado {len(tasks_criadas)}")

    async def test_interesse_move_para_precificacao(self):
        """Intent INTERESSE deve mover o card para PRECIFICACAO antes do task."""
        from config import Stage
        card = _make_card()
        stages_movidos = []

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def move_card(self, cid, stage):
                stages_movidos.append(stage)
            async def update_card(self, cid, fields): pass

        async def fake_send(*a): pass

        with patch("webhooks.agente_listas.FaroClient", return_value=FakeFaro()), \
             patch("jobs.precificacao.send_proposal_now", fake_send):
            import asyncio as _asyncio
            with patch.object(_asyncio, "create_task", side_effect=lambda c: c.close() or MagicMock()):
                from webhooks.agente_listas import _handle_intent
                await _handle_intent("INTERESSE", card)

        self.assertIn(Stage.PRECIFICACAO, stages_movidos,
                      f"Card não foi movido para PRECIFICACAO. Stages: {stages_movidos}")

    async def test_recusa_nao_dispara_proposta(self):
        """Intent RECUSA não deve chamar send_proposal_now."""
        from config import Stage
        card = _make_card()
        tasks_criadas = []

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def move_card(self, cid, stage): pass
            async def update_card(self, cid, fields): pass

        with patch("webhooks.agente_listas.FaroClient", return_value=FakeFaro()):
            import asyncio as _asyncio
            with patch.object(_asyncio, "create_task", side_effect=lambda c: tasks_criadas.append(c) or MagicMock()):
                from webhooks.agente_listas import _handle_intent
                await _handle_intent("RECUSA_SEM_INTERESSE", card)

        self.assertEqual(len(tasks_criadas), 0,
                         f"Nenhum task deveria ser criado para RECUSA. Tasks: {len(tasks_criadas)}")


# ===========================================================================
# 6 & 7. precificacao — mensagem SDR + notify_team após proposta
# ===========================================================================

class TestPrecificacaoSDRNotify(unittest.IsolatedAsyncioTestCase):
    """Verifica mensagem SDR e notify_team após envio bem-sucedido de proposta."""

    def _make_full_card(self):
        from config import Stage
        return _make_card(
            stage=Stage.PRECIFICACAO,
            credito="200000",
            valor_pago="20000",
            pct_pago="10",
            proposta="40000",
        )

    async def test_mensagem_sdr_enviada_apos_proposta(self):
        """Após proposta enviada, deve enviar mensagem SDR ao lead."""
        from config import Stage
        card = self._make_full_card()
        mensagens_enviadas = []

        class FakeWhapi:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def send_image(self, *a, **kw): pass
            async def send_text(self, phone, msg):
                mensagens_enviadas.append(msg)

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get_card(self, cid): return card
            async def move_card(self, cid, stage): pass
            async def update_card(self, cid, fields): pass
            async def append_description(self, cid, txt): pass

        with patch("jobs.precificacao.FaroClient", return_value=FakeFaro()), \
             patch("jobs.precificacao.get_whapi_for_card", return_value=FakeWhapi()), \
             patch("jobs.precificacao._generate_proposal_image", AsyncMock(return_value=None)), \
             patch("jobs.precificacao.save_history", AsyncMock()), \
             patch("jobs.precificacao.save_journey", AsyncMock()), \
             patch("jobs.precificacao.notify_team", AsyncMock()) as mock_notify, \
             patch("services.session_store.acquire_mutex", AsyncMock(return_value=True)), \
             patch("services.session_store.release_mutex", AsyncMock()):
            from jobs.precificacao import _process_card_locked
            await _process_card_locked(FakeFaro(), card["id"])

        sdr_msgs = [m for m in mensagens_enviadas if "consultor" in m.lower()]
        self.assertTrue(len(sdr_msgs) >= 1,
                        f"Mensagem SDR não encontrada. Mensagens enviadas: {mensagens_enviadas}")

    async def test_notify_team_chamado_apos_proposta(self):
        """notify_team deve ser chamado com dados do lead após envio da proposta."""
        from config import Stage
        card = self._make_full_card()

        class FakeWhapi:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def send_image(self, *a, **kw): pass
            async def send_text(self, *a, **kw): pass

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get_card(self, cid): return card
            async def move_card(self, cid, stage): pass
            async def update_card(self, cid, fields): pass
            async def append_description(self, cid, txt): pass

        mock_notify = AsyncMock()

        # O módulo reimporta notify_team localmente: "from services.whapi import notify_team as _notify_team"
        # Portanto o patch correto é em services.whapi.notify_team
        with patch("jobs.precificacao.FaroClient", return_value=FakeFaro()), \
             patch("jobs.precificacao.get_whapi_for_card", return_value=FakeWhapi()), \
             patch("jobs.precificacao._generate_proposal_image", AsyncMock(return_value=None)), \
             patch("jobs.precificacao.save_history", AsyncMock()), \
             patch("jobs.precificacao.save_journey", AsyncMock()), \
             patch("services.whapi.notify_team", mock_notify), \
             patch("services.message_guard.check_reactivation_rate", AsyncMock(return_value=False)), \
             patch("services.session_store.acquire_mutex", AsyncMock(return_value=True)), \
             patch("services.session_store.release_mutex", AsyncMock()):
            from jobs.precificacao import _process_card_locked
            await _process_card_locked(FakeFaro(), card["id"])

        mock_notify.assert_called_once()
        msg_notif = mock_notify.call_args[0][0]
        self.assertIn("proposta", msg_notif.lower(),
                      f"Mensagem de notificação não menciona proposta: {msg_notif[:200]}")

    async def test_notify_team_contem_dados_lead(self):
        """Notificação deve conter nome, telefone e ADM do lead."""
        from config import Stage
        card = self._make_full_card()

        class FakeWhapi:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def send_image(self, *a, **kw): pass
            async def send_text(self, *a, **kw): pass

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get_card(self, cid): return card
            async def move_card(self, cid, stage): pass
            async def update_card(self, cid, fields): pass
            async def append_description(self, cid, txt): pass

        mock_notify = AsyncMock()

        with patch("jobs.precificacao.FaroClient", return_value=FakeFaro()), \
             patch("jobs.precificacao.get_whapi_for_card", return_value=FakeWhapi()), \
             patch("jobs.precificacao._generate_proposal_image", AsyncMock(return_value=None)), \
             patch("jobs.precificacao.save_history", AsyncMock()), \
             patch("jobs.precificacao.save_journey", AsyncMock()), \
             patch("services.whapi.notify_team", mock_notify), \
             patch("services.message_guard.check_reactivation_rate", AsyncMock(return_value=False)), \
             patch("services.session_store.acquire_mutex", AsyncMock(return_value=True)), \
             patch("services.session_store.release_mutex", AsyncMock()):
            from jobs.precificacao import _process_card_locked
            await _process_card_locked(FakeFaro(), card["id"])

        self.assertTrue(mock_notify.called, "notify_team não foi chamado")
        msg = mock_notify.call_args[0][0]
        # get_name() retorna o primeiro nome; get_adm() usa campo "Adm"
        self.assertIn("João", msg, "Nome não encontrado na notificação")
        self.assertIn("Porto Seguro", msg, "ADM não encontrada na notificação")
        self.assertIn("5511999990001", msg, "Telefone não encontrado na notificação")

    async def test_notify_team_nao_chamado_em_falha(self):
        """Se a proposta falhar, notify_team não deve ser chamado."""
        from config import Stage
        card = self._make_full_card()

        class FakeWhapiError:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def send_image(self, *a, **kw): pass
            async def send_text(self, *a, **kw):
                from services.whapi import WhapiError
                raise WhapiError("falha simulada", status_code=500)

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get_card(self, cid): return card
            async def move_card(self, cid, stage): pass
            async def update_card(self, cid, fields): pass
            async def append_description(self, cid, txt): pass

        mock_notify = AsyncMock()

        with patch("jobs.precificacao.FaroClient", return_value=FakeFaro()), \
             patch("jobs.precificacao.get_whapi_for_card", return_value=FakeWhapiError()), \
             patch("jobs.precificacao._generate_proposal_image", AsyncMock(return_value=None)), \
             patch("jobs.precificacao.notify_team", mock_notify), \
             patch("services.session_store.acquire_mutex", AsyncMock(return_value=True)), \
             patch("services.session_store.release_mutex", AsyncMock()):
            from jobs.precificacao import _process_card_locked
            await _process_card_locked(FakeFaro(), card["id"])

        mock_notify.assert_not_called()


# ===========================================================================
# 8. send_proposal_now — sem guard de janela horária
# ===========================================================================

class TestSendProposalNow(unittest.IsolatedAsyncioTestCase):
    """send_proposal_now deve disparar independente da janela horária."""

    async def test_dispara_fora_da_janela(self):
        """Mesmo com _is_within_send_window() retornando False, deve processar."""
        from config import Stage
        card = _make_card(stage=Stage.PRECIFICACAO)
        process_chamado = []

        async def fake_process(faro, c):
            process_chamado.append(True)
            return True

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get_card(self, cid): return card

        with patch("jobs.precificacao.FaroClient", return_value=FakeFaro()), \
             patch("jobs.precificacao._process_card", side_effect=fake_process), \
             patch("jobs.precificacao._is_within_send_window", return_value=False):
            from jobs.precificacao import send_proposal_now
            await send_proposal_now(card)

        self.assertEqual(len(process_chamado), 1,
                         "send_proposal_now não chamou _process_card fora da janela")

    async def test_dispara_dentro_da_janela(self):
        """Dentro da janela também deve processar normalmente."""
        from config import Stage
        card = _make_card(stage=Stage.PRECIFICACAO)
        process_chamado = []

        async def fake_process(faro, c):
            process_chamado.append(True)
            return True

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get_card(self, cid): return card

        with patch("jobs.precificacao.FaroClient", return_value=FakeFaro()), \
             patch("jobs.precificacao._process_card", side_effect=fake_process), \
             patch("jobs.precificacao._is_within_send_window", return_value=True):
            from jobs.precificacao import send_proposal_now
            await send_proposal_now(card)

        self.assertEqual(len(process_chamado), 1,
                         "send_proposal_now não chamou _process_card dentro da janela")


# ===========================================================================
# 9. Cálculo de cadência — matemática da meta
# ===========================================================================

class TestCadenciaMath(unittest.TestCase):
    """Testes matemáticos da cadência garantida."""

    def test_5_tokens_720s_gap_alcanca_meta(self):
        """
        Com 5 tokens e gap de 720s, a CAPACIDADE máxima do pool é 300/dia.
        O limitador real é o ciclo de 5 min: 144 ciclos/dia = 144 disparos/dia.
        Ambos os números devem estar dentro da faixa operacional.
        """
        gap = 720       # segundos
        tokens = 5
        janela = 12 * 3600  # 12 horas em segundos

        # Capacidade máxima do pool (sem limitação de ciclo)
        por_token = janela // gap
        capacidade_pool = por_token * tokens
        self.assertGreaterEqual(capacidade_pool, 130,
                                "Pool não tem capacidade mínima de 130/dia")

        # Limitador real: ciclo de 5 min (1 disparo por ciclo)
        ciclos_por_dia = (janela // 60) // 5
        self.assertGreaterEqual(ciclos_por_dia, 130,
                                f"Ciclos/dia {ciclos_por_dia} < 130")
        self.assertLessEqual(ciclos_por_dia, 150,
                             f"Ciclos/dia {ciclos_por_dia} > 150 — recalcular ciclo")

    def test_ciclo_5min_suficiente(self):
        """Job rodando a cada 5 min na janela de 12h gera 144 ciclos — suficiente."""
        ciclos = (12 * 60) // 5
        self.assertGreaterEqual(ciclos, 130)
        self.assertLessEqual(ciclos, 150)

    def test_gap_720s_igual_12min(self):
        self.assertEqual(720 / 60, 12.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
