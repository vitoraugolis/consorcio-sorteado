"""
tests/conftest.py — Fixtures reutilizáveis para a suite de testes
"""
import pytest
from unittest.mock import AsyncMock


def _make_card(**overrides) -> dict:
    base = {
        "id": "aaaabbbb-0000-0000-0000-000000000001",
        "stage_id": "stage-listas-uuid",
        "Nome do contato": "Vitor Teste",
        "Telefone": "5519999990001",
        "Fonte": "Lista",
        "Etiquetas": [],
        "Administradora": "Itaú",
        "Crédito": "200000",
        "Proposta Realizada": "160000",
        "Sequencia_Proposta": "160000,170000,180000,190000",
        "Indice da Proposta": "1",
        "Grupo": "12345",
        "Cota": "001",
        "Tipo contemplação": "Sorteio",
        "Tipo de bem": "Imóvel",
        "Historico Conversa": "",
        "Dados Pessoais Texto": "",
        "Num Follow Ups": "0",
        "Situacao Negociacao": "",
        "ZapSign Token": "",
        "Ultima atividade": "",
    }
    base.update(overrides)
    return base


@pytest.fixture
def card_lista():
    return _make_card(Fonte="Lista")


@pytest.fixture
def card_bazar():
    return _make_card(Fonte="Bazar", stage_id="stage-precificacao-uuid")


@pytest.fixture
def card_sem_fonte():
    return _make_card(Fonte=None)


@pytest.fixture
def card_negociacao():
    return _make_card(stage_id="stage-em-negociacao-uuid")


@pytest.fixture
def card_sem_sequencia():
    base = _make_card()
    base["Sequencia_Proposta"] = ""
    return base


@pytest.fixture
def card_sem_telefone():
    return _make_card(Telefone="")


@pytest.fixture
def card_sem_credito():
    base = _make_card()
    base["Crédito"] = ""
    base["Proposta Realizada"] = "0"
    return base


class MockFaroClient:
    def __init__(self, card=None):
        self._card = card or _make_card()
        self.moved_to = []
        self.updated_fields = []

        self.get_card = AsyncMock(return_value=self._card)
        self.move_card = AsyncMock(side_effect=self._record_move)
        self.update_card = AsyncMock(side_effect=self._record_update)
        self.find_card_by_phone = AsyncMock(return_value=self._card)
        self.get_cards_all_pages = AsyncMock(return_value=[self._card])
        self.watch_new = AsyncMock(return_value=[self._card])

    async def _record_move(self, card_id, stage_id):
        self.moved_to.append(stage_id)
        self._card["stage_id"] = stage_id

    async def _record_update(self, card_id, fields):
        self.updated_fields.append(fields)
        self._card.update(fields)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockWhapiClient:
    def __init__(self):
        self.sent_texts = []
        self.sent_buttons = []
        self.sent_images = []
        self.send_text = AsyncMock(side_effect=lambda to, msg: self.sent_texts.append((to, msg)))
        self.send_buttons = AsyncMock(side_effect=lambda **kw: self.sent_buttons.append(kw))
        self.send_image = AsyncMock(side_effect=lambda to, url, **kw: self.sent_images.append((to, url)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockAIClient:
    def __init__(self, response=""):
        self._response = response
        self.complete = AsyncMock(return_value=response)
        self.complete_with_history = AsyncMock(return_value=response)
        self.complete_with_image = AsyncMock(return_value=response)

    def set_response(self, r):
        self.complete = AsyncMock(return_value=r)
        self.complete_with_history = AsyncMock(return_value=r)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def mock_faro(card_negociacao):
    return MockFaroClient(card=card_negociacao)


@pytest.fixture
def mock_whapi():
    return MockWhapiClient()


@pytest.fixture
def mock_ai():
    return MockAIClient()
