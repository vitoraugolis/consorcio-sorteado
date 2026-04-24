"""
tests/test_edge_cases.py — Casos extremos e cenários de falha gracioso
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from webhooks.negociador import (
    _get_next_proposal, _extract_lead_value, _message_has_value, _build_result, Intent
)
from services.faro import is_lista, get_name, get_phone, get_adm


def card(**overrides):
    base = {
        "id": "edge-001",
        "stage_id": "stage-test",
        "Nome do contato": "Test Lead",
        "Telefone": "5511999990001",
        "Fonte": "Lista",
        "Etiquetas": [],
        "Crédito": "200000",
        "Proposta Realizada": "160000",
        "Sequencia_Proposta": "160000,170000,180000",
        "Historico Conversa": "",
        "ZapSign Token": "",
    }
    base.update(overrides)
    return base


class TestEdgeCasesNegociador:

    def test_card_sem_credito_nao_crasha(self):
        c = card(**{"Crédito": "", "Proposta Realizada": "0", "Sequencia_Proposta": ""})
        result = _get_next_proposal(c)
        assert result is not None
        assert result["pode_escalar"] is False

    def test_card_sem_sequencia_nao_crasha(self):
        c = card(**{"Sequencia_Proposta": ""})
        result = _get_next_proposal(c)
        assert result["pode_escalar"] is False

    def test_sequencia_com_lixo_nao_crasha(self):
        c = card(**{"Sequencia_Proposta": "abc,None,,,160000,def"})
        result = _get_next_proposal(c)
        assert result is not None

    def test_build_result_mensagem_vazia_nao_crasha(self):
        c = card()
        result = _build_result(Intent.OUTRO, "Olá!", c, "")
        assert result is not None
        assert result.response_message

    def test_build_result_so_emoji_nao_crasha(self):
        c = card()
        result = _build_result(Intent.OUTRO, "😊", c, "👍🎉😊")
        assert result is not None

    def test_extract_value_mensagem_muito_longa(self):
        msg = "a" * 10_000 + " 200 mil " + "b" * 10_000
        result = _extract_lead_value(msg)
        assert result == 200_000

    def test_extract_value_sem_numero(self):
        assert _extract_lead_value("oi tudo bem com você?") == 0

    def test_message_has_value_string_vazia(self):
        assert _message_has_value("") is False


class TestFaroHelpers:

    # get_name: retorna primeiro nome capitalizado, fallback "Cliente"
    def test_get_name_retorna_primeiro_nome(self):
        assert get_name({"Nome do contato": "Ana Silva"}) == "Ana"

    def test_get_name_nome_unico(self):
        assert get_name({"Nome do contato": "vitor"}) == "Vitor"

    def test_get_name_ausente_retorna_fallback(self):
        result = get_name({})
        assert isinstance(result, str)
        assert len(result) > 0  # retorna "Cliente" ou similar

    # get_phone: retorna str com dígitos ou None
    def test_get_phone_normal(self):
        assert get_phone({"Telefone": "5511999990001"}) == "5511999990001"

    def test_get_phone_com_formatacao(self):
        result = get_phone({"Telefone": "+55 (11) 99999-0001"})
        assert result == "5511999990001"

    def test_get_phone_ausente_retorna_none(self):
        assert get_phone({}) is None

    def test_get_phone_vazio_retorna_none(self):
        assert get_phone({"Telefone": ""}) is None

    # get_adm: usa campo "Adm", fallback "sua administradora"
    def test_get_adm_com_campo_adm(self):
        assert get_adm({"Adm": "Itaú"}) == "Itaú"

    def test_get_adm_ausente_retorna_fallback(self):
        result = get_adm({})
        assert isinstance(result, str)
        assert len(result) > 0


class TestIsLista:

    def test_fonte_lista(self):
        assert is_lista({"Fonte": "Lista", "Etiquetas": []}) is True

    def test_fonte_lista_case_insensitive(self):
        assert is_lista({"Fonte": "LISTA", "Etiquetas": []}) is True

    def test_fonte_bazar(self):
        assert is_lista({"Fonte": "Bazar", "Etiquetas": []}) is False

    def test_fonte_site(self):
        assert is_lista({"Fonte": "Site", "Etiquetas": []}) is False

    def test_fonte_none(self):
        assert is_lista({"Fonte": None, "Etiquetas": []}) is False

    def test_etiqueta_lista(self):
        assert is_lista({"Fonte": None, "Etiquetas": ["lista", "fria"]}) is True

    def test_campos_ausentes(self):
        assert is_lista({}) is False

    def test_etiquetas_none(self):
        assert is_lista({"Fonte": None, "Etiquetas": None}) is False


class TestRouterRegras:

    def test_fonte_vazia_assume_lista(self):
        """Nova regra: card sem Fonte em stage de ativação = lista fria."""
        card_sem_fonte = {"Fonte": None, "Etiquetas": []}
        fonte = str(card_sem_fonte.get("Fonte") or "").strip().lower()
        is_lista_card = is_lista(card_sem_fonte) or (not fonte)
        assert is_lista_card is True

    def test_bazar_com_fonte_nao_assume_lista(self):
        card_bazar = {"Fonte": "Bazar", "Etiquetas": []}
        fonte = str(card_bazar.get("Fonte") or "").strip().lower()
        is_lista_card = is_lista(card_bazar) or (not fonte)
        assert is_lista_card is False

    def test_lista_com_fonte_e_nova_regra_consistentes(self):
        card_lista = {"Fonte": "Lista", "Etiquetas": []}
        fonte = str(card_lista.get("Fonte") or "").strip().lower()
        is_lista_card = is_lista(card_lista) or (not fonte)
        assert is_lista_card is True


class TestFaroHistory:

    def test_load_history_vazio(self):
        from services.faro import load_history
        history = load_history({"Historico Conversa": ""})
        assert isinstance(history, list)
        assert len(history) == 0

    def test_load_history_com_dados(self):
        import json
        from services.faro import load_history, history_append
        h = []
        h = history_append(h, "user", "oi")
        h = history_append(h, "assistant", "olá!")
        loaded = load_history({"Historico Conversa": json.dumps(h)})
        assert len(loaded) == 2
        assert loaded[0]["role"] == "user"

    def test_history_append_preserva_ordem(self):
        from services.faro import history_append
        h = []
        h = history_append(h, "user", "msg1")
        h = history_append(h, "assistant", "resp1")
        h = history_append(h, "user", "msg2")
        assert h[0]["content"] == "msg1"
        assert h[2]["content"] == "msg2"

    def test_load_history_json_invalido_retorna_vazio(self):
        from services.faro import load_history
        assert load_history({"Historico Conversa": "isso nao é json {{{"}) == []
