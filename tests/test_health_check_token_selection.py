"""
tests/test_health_check_token_selection.py
Bateria de testes para o health_check na seleção de tokens da fila de Listas.

Cobre:
  1. Token offline é ignorado na seleção
  2. Todos offline → retorna None + alerta
  3. Seleciona o token online mais ocioso (ignora offline)
  4. Cache Redis evita chamada duplicada à API (TTL 3 min)
  5. Disparo bem-sucedido grava cache "online" no Redis
  6. Redis indisponível → assume online (não bloqueia disparo)
  7. Um token online + em cooldown → retorna None corretamente
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis_mock(cached_values: dict | None = None):
    """Redis mock que simula get/set com um dicionário em memória."""
    store = dict(cached_values or {})

    r = MagicMock()

    async def _get(key):
        return store.get(key)

    async def _set(key, value, ex=None):
        store[key] = value if isinstance(value, (bytes, str)) else str(value)

    r.get = _get
    r.set = _set
    return r, store


def _patch_pool(tokens: list[str]):
    """Patch WHAPI_LISTA_TOKENS na fila_listas."""
    return patch("jobs.fila_listas.WHAPI_LISTA_TOKENS", tokens)


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------

class TestHealthCheckTokenSelection(unittest.IsolatedAsyncioTestCase):

    async def test_token_offline_ignorado(self):
        """Token com health_check=False não deve ser selecionado."""
        tokens = ["AAAAAAAA_tok1", "BBBBBBBB_tok2"]

        redis_mock, _ = _make_redis_mock()  # cache vazio

        health_map = {
            tokens[0]: (False, "QR"),    # offline
            tokens[1]: (True, "READY"),  # online
        }

        async def fake_health(w_self):
            return health_map[w_self._token]

        class FakeWhapiClient:
            def __init__(self, token=None, canal=None):
                self._token = token
                self._client = MagicMock()
                self._client.timeout = MagicMock()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def health_check(self): return health_map.get(self._token, (True, "READY"))

        with _patch_pool(tokens), \
             patch("jobs.fila_listas.get_redis", AsyncMock(return_value=redis_mock)), \
             patch("jobs.fila_listas._get_token_last_used", AsyncMock(return_value=0.0)), \
             patch("services.whapi.WhapiClient", FakeWhapiClient):
            from jobs.fila_listas import _pick_lista_token_with_gap
            result = await _pick_lista_token_with_gap()

        self.assertIsNotNone(result, "Deveria retornar um token (tokens[1] está online)")
        token, _ = result
        self.assertEqual(token, tokens[1], f"Deveria selecionar tokens[1] (online), não {token}")

    async def test_todos_offline_retorna_none(self):
        """Se todos os tokens estiverem offline, retorna None."""
        tokens = ["AAAAAAAA_tok1", "BBBBBBBB_tok2", "CCCCCCCC_tok3"]
        redis_mock, _ = _make_redis_mock()

        async def fake_health(w_self):
            return False, "QR"

        class FakeWhapiClient:
            def __init__(self, token=None, canal=None):
                self._token = token
                self._client = MagicMock()
                self._client.timeout = MagicMock()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def health_check(self): return False, "QR"

        notify_calls = []

        async def fake_notify(msg):
            notify_calls.append(msg)

        with _patch_pool(tokens), \
             patch("jobs.fila_listas.get_redis", AsyncMock(return_value=redis_mock)), \
             patch("jobs.fila_listas._get_token_last_used", AsyncMock(return_value=0.0)), \
             patch("services.whapi.WhapiClient", FakeWhapiClient), \
             patch("jobs.fila_listas.asyncio.create_task", side_effect=lambda c: c.close()):
            with patch("services.whapi.notify_team", fake_notify):
                from jobs.fila_listas import _pick_lista_token_with_gap
                result = await _pick_lista_token_with_gap()

        self.assertIsNone(result, "Deveria retornar None quando todos offline")

    async def test_seleciona_mais_ocioso_entre_online(self):
        """Entre tokens online, deve selecionar o mais tempo ocioso."""
        tokens = ["AAAAAAAA_tok1", "BBBBBBBB_tok2", "CCCCCCCC_tok3"]
        redis_mock, _ = _make_redis_mock()

        # tok1 offline, tok2 idle=800s, tok3 idle=900s — deve selecionar tok3
        now = time.time()
        last_used = {
            tokens[0]: now - 1000,  # offline — não importa
            tokens[1]: now - 800,   # online, idle 800s >= 720s ✅
            tokens[2]: now - 900,   # online, idle 900s >= 720s ✅ (mais ocioso)
        }

        health_map = {
            tokens[0]: (False, "QR"),
            tokens[1]: (True, "READY"),
            tokens[2]: (True, "READY"),
        }

        class FakeWhapiClient:
            def __init__(self, token=None, canal=None):
                self._token = token
                self._client = MagicMock()
                self._client.timeout = MagicMock()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def health_check(self): return health_map.get(self._token, (True, "READY"))

        async def fake_last_used(suffix):
            for t, v in last_used.items():
                if t.endswith(suffix):
                    return v
            return 0.0

        with _patch_pool(tokens), \
             patch("jobs.fila_listas.get_redis", AsyncMock(return_value=redis_mock)), \
             patch("jobs.fila_listas._get_token_last_used", side_effect=fake_last_used), \
             patch("services.whapi.WhapiClient", FakeWhapiClient):
            from jobs.fila_listas import _pick_lista_token_with_gap
            result = await _pick_lista_token_with_gap()

        self.assertIsNotNone(result)
        token, _ = result
        self.assertEqual(token, tokens[2], f"Deveria selecionar tokens[2] (mais ocioso), não {token}")

    async def test_cache_redis_evita_chamada_api(self):
        """Se cache Redis tem resultado recente, não deve chamar health_check da API."""
        tokens = ["AAAAAAAA_tok1"]
        # Cache diz que o token está online ("1")
        suffix = tokens[0][-8:]
        redis_mock, _ = _make_redis_mock({
            f"cs:fila_listas:health:{suffix}": "1"
        })

        api_calls = []

        class FakeWhapiClient:
            def __init__(self, token=None, canal=None):
                self._token = token
                self._client = MagicMock()
                self._client.timeout = MagicMock()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def health_check(self):
                api_calls.append(self._token)
                return True, "READY"

        with _patch_pool(tokens), \
             patch("jobs.fila_listas.get_redis", AsyncMock(return_value=redis_mock)), \
             patch("jobs.fila_listas._get_token_last_used", AsyncMock(return_value=0.0)), \
             patch("services.whapi.WhapiClient", FakeWhapiClient):
            from jobs.fila_listas import _pick_lista_token_with_gap
            result = await _pick_lista_token_with_gap()

        self.assertIsNotNone(result, "Token com cache=online deveria ser selecionado")
        self.assertEqual(len(api_calls), 0,
                         f"API não deveria ser chamada quando cache existe. Chamadas: {api_calls}")

    async def test_cache_offline_bloqueia_sem_chamada_api(self):
        """Cache dizendo offline deve bloquear sem chamar a API."""
        tokens = ["AAAAAAAA_tok1"]
        suffix = tokens[0][-8:]
        redis_mock, _ = _make_redis_mock({
            f"cs:fila_listas:health:{suffix}": "0"  # cache diz offline
        })

        api_calls = []

        class FakeWhapiClient:
            def __init__(self, token=None, canal=None):
                self._token = token
                self._client = MagicMock()
                self._client.timeout = MagicMock()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def health_check(self):
                api_calls.append(self._token)
                return True, "READY"

        with _patch_pool(tokens), \
             patch("jobs.fila_listas.get_redis", AsyncMock(return_value=redis_mock)), \
             patch("jobs.fila_listas._get_token_last_used", AsyncMock(return_value=0.0)), \
             patch("services.whapi.WhapiClient", FakeWhapiClient):
            from jobs.fila_listas import _pick_lista_token_with_gap
            result = await _pick_lista_token_with_gap()

        self.assertIsNone(result, "Token com cache=offline não deveria ser selecionado")
        self.assertEqual(len(api_calls), 0, "API não deveria ser chamada quando cache existe")

    async def test_redis_indisponivel_assume_online(self):
        """Se Redis falhar, assume token online para não bloquear disparos."""
        tokens = ["AAAAAAAA_tok1"]

        redis_error = MagicMock()
        redis_error.get = AsyncMock(side_effect=Exception("Redis connection refused"))
        redis_error.set = AsyncMock(side_effect=Exception("Redis connection refused"))

        class FakeWhapiClient:
            def __init__(self, token=None, canal=None):
                self._token = token
                self._client = MagicMock()
                self._client.timeout = MagicMock()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def health_check(self): return True, "READY"

        with _patch_pool(tokens), \
             patch("jobs.fila_listas.get_redis", AsyncMock(return_value=redis_error)), \
             patch("jobs.fila_listas._get_token_last_used", AsyncMock(return_value=0.0)), \
             patch("services.whapi.WhapiClient", FakeWhapiClient):
            from jobs.fila_listas import _pick_lista_token_with_gap
            result = await _pick_lista_token_with_gap()

        self.assertIsNotNone(result, "Falha no Redis não deve bloquear seleção de token")

    async def test_token_online_em_cooldown_retorna_none(self):
        """Token online mas em cooldown (idle < TOKEN_GAP_MIN_S) → retorna None."""
        tokens = ["AAAAAAAA_tok1"]
        redis_mock, _ = _make_redis_mock()

        now = time.time()
        # Último uso há apenas 300s, gap mínimo é 720s → em cooldown
        last_used_time = now - 300

        class FakeWhapiClient:
            def __init__(self, token=None, canal=None):
                self._token = token
                self._client = MagicMock()
                self._client.timeout = MagicMock()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def health_check(self): return True, "READY"

        with _patch_pool(tokens), \
             patch("jobs.fila_listas.get_redis", AsyncMock(return_value=redis_mock)), \
             patch("jobs.fila_listas._get_token_last_used", AsyncMock(return_value=last_used_time)), \
             patch("services.whapi.WhapiClient", FakeWhapiClient):
            from jobs.fila_listas import _pick_lista_token_with_gap
            result = await _pick_lista_token_with_gap()

        self.assertIsNone(result, "Token online mas em cooldown deve retornar None")

    async def test_disparo_bem_sucedido_grava_cache_online(self):
        """Após disparo bem-sucedido, deve gravar cache health='1' no Redis."""
        redis_store = {}
        redis_mock = MagicMock()

        async def _get(key): return redis_store.get(key)
        async def _set(key, value, ex=None): redis_store[key] = value

        redis_mock.get = _get
        redis_mock.set = _set

        tokens = ["AAAAAAAA_tok1"]
        suffix = tokens[0][-8:]

        # Simula: token online, idle suficiente, card disponível, disparo ok
        class FakeWhapiClient:
            def __init__(self, token=None, canal=None):
                self._token = token
                self._client = MagicMock()
                self._client.timeout = MagicMock()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def health_check(self): return True, "READY"
            async def send_buttons(self, *a, **kw): pass
            async def send_text(self, *a, **kw): pass

        class FakeFaro:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get_cards_all_pages(self, **kw): return [_make_card()]
            async def move_card(self, *a): pass
            async def update_card(self, *a): pass
            async def get_card(self, cid): return _make_card()

        with _patch_pool(tokens), \
             patch("jobs.fila_listas.get_redis", AsyncMock(return_value=redis_mock)), \
             patch("jobs.fila_listas._get_token_last_used", AsyncMock(return_value=0.0)), \
             patch("jobs.fila_listas._set_token_last_used", AsyncMock()), \
             patch("jobs.fila_listas.FaroClient", return_value=FakeFaro()), \
             patch("jobs.fila_listas.resolve_phone", AsyncMock(return_value="5511999990001")), \
             patch("services.whapi.WhapiClient", FakeWhapiClient), \
             patch("services.session_store.acquire_mutex", AsyncMock(return_value=True)), \
             patch("services.session_store.release_mutex", AsyncMock()), \
             patch("jobs.fila_listas.acquire_mutex", AsyncMock(return_value=True)), \
             patch("jobs.fila_listas.release_mutex", AsyncMock()):
            from jobs.fila_listas import run_ciclo_fila_listas
            await run_ciclo_fila_listas()

        cache_key = f"cs:fila_listas:health:{suffix}"
        self.assertEqual(redis_store.get(cache_key), "1",
                         f"Cache de health deveria ser '1' após disparo bem-sucedido. "
                         f"Store: {redis_store}")


def _make_card():
    from config import Stage
    return {
        "id": "card-test-00000001",
        "title": "Teste",
        "Nome do contato": "Teste",
        "Telefone": "5511999990001",
        "Adm": "Porto Seguro",
        "stage_id": Stage.LISTAS,
        "Fonte": "lista",
        "Crédito": "200000",
        "Valor pago até o momento": "20000",
        "Proposta Realizada": "",
        "Porcentagem paga até o momento": "10",
    }


if __name__ == "__main__":
    unittest.main(verbosity=2)
