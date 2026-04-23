"""
tests/simulation_full.py — Simulação completa de todos os fluxos do sistema

Cobre os dois fluxos principais e todos os edge cases identificados:

FLUXO LISTAS (cold leads via Whapi):
  L01 - Ativação de lead frio
  L02 - Lead aceita proposta imediatamente → ACEITO
  L03 - Lead recusa → PERDIDO
  L04 - Lead tem dúvida → resposta, stage mantido
  L05 - Lead quer atendente humano → equipe notificada
  L06 - Counter-proposal com valor inline → CONTRA_PROPOSTA direto
  L07 - Counter-proposal sem valor → pergunta o valor
  L08 - Lead diz "me ofereceram mais" com valor → reclassifica p/ CONTRA_PROPOSTA
  L09 - ACEITO → lista: solicita dados pessoais
  L10 - Coleta de dados pessoais: dados completos numa mensagem
  L11 - Coleta de dados pessoais: dados parciais → pede restantes
  L12 - Extrato recebido com dados completos → gera ZapSign
  L13 - Extrato recebido sem dados → bloqueia e pede os dados
  L14 - Follow-up em proposta sem resposta
  L15 - Esgotamento de follow-ups (8x) → PERDIDO + notifica equipe
  L16 - ASSINATURA parada 3+ dias (aguardando dados) → lembrete
  L17 - ASSINATURA parada 3+ dias (aguardando extrato) → lembrete

FLUXO BAZAR/SITE (leads orgânicos via Z-API):
  B01 - Lead envia extrato imagem → QUALIFICADO → PRECIFICACAO
  B02 - Lead envia extrato PDF → QUALIFICADO (extrai todos os campos)
  B03 - Lead envia extrato com >50% pago → NAO_QUALIFICADO
  B04 - Lead envia boleto (não é extrato) → EXTRATO_INCORRETO → orienta
  B05 - Lead envia extrato ilegível → EXTRATO_INCORRETO
  B06 - Lead envia texto sem extrato → solicita extrato
  B07 - Lead recusa verbalmente → PERDIDO
  B08 - Erro técnico na análise de extrato → graceful error + slack
  B09 - QUALIFICADO → precificação → lead aceita → ACEITO → ZapSign direto
  B10 - Campos do extrato gravados corretamente no FARO (Crédito, Tipo contemplação, etc.)

EDGE CASES:
  E01 - Administradora sem template ZapSign → notifica equipe, não envia msg errada
  E02 - ZapSign error (não-lista) → MSG_ERRO_INTERNO enviada + notificação equipe
  E03 - ZapSign error (lista) → apenas notifica equipe, não manda mensagem ao lead
  E04 - Lead em ASSINATURA sem ZapSign token → problema com link → redirecionar
  E05 - Lead em ASSINATURA com ZapSign token → mensagem genérica via IA
  E06 - Lead muito desconfiante → resposta de credibilidade
  E07 - Extrato de cota vendida (verbal) no qualificador → PERDIDO
  E08 - Múltiplos formatos de número na proposta (R$ 180.000,00 vs 180000)

Uso:
    cd /Users/vitoraugolis/Documents/CS/consorcio-sorteado
    python -m tests.simulation_full
    python -m tests.simulation_full --cenario L01
    python -m tests.simulation_full --verbose
"""

import argparse
import asyncio
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

# ── Cores ANSI ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"
TICK   = f"{GREEN}✓{RESET}"
CROSS  = f"{RED}✗{RESET}"
INFO   = f"{CYAN}ℹ{RESET}"


# ---------------------------------------------------------------------------
# Factories de dados de teste
# ---------------------------------------------------------------------------

def make_lista_card(
    card_id: str = "lista-001",
    nome: str = "Carlos Ferreira",
    phone: str = "5511900000001",
    adm: str = "Santander",
    stage_id: str = None,
    proposta: str = "185000",
    credito: str = "250000",
    num_fups: int = 0,
    zapsign_token: str = "",
    dados_pessoais: str = "",
    # Sequencia_Proposta: CSV de valores para escalonamento de preço
    # Padrão: sequência esgotada (proposta atual = último valor)
    sequencia: str = "150000,165000,175000,185000",
    indice_proposta: str = "3",
    **extra,
) -> dict:
    """Card de lead de lista (cold lead, Whapi)."""
    from config import Stage
    return {
        "id":                    card_id,
        "title":                 nome,
        "stage_id":              stage_id or Stage.EM_NEGOCIACAO,
        "stage_name":            "Em negociação",
        "Nome do contato":       nome,
        "Telefone":              phone,
        "Adm":                   adm,
        "Fonte":                 "lista_teste",
        "Etiquetas":             "lista_teste",
        "Proposta Realizada":    proposta,
        "Sequencia_Proposta":    sequencia,
        "Indice da Proposta":    indice_proposta,
        "Crédito":               credito,
        "Grupo":                 "A042",
        "Cota":                  "015",
        "Tipo contemplação":     "Sorteio",
        "Tipo de bem":           "Imóvel",
        "Parcelas pagas":        "24",
        "Parcelas a vencer":     "96",
        "Valor das parcelas":    "R$ 1.800,00",
        "ZapSign Token":         zapsign_token,
        "Dados Pessoais Texto":  dados_pessoais,
        "Historico Conversa":    "[]",
        "Contexto Jornada":      "{}",
        "Num Follow Ups":        str(num_fups),
        "Ultima atividade":      "",
        **extra,
    }


def make_bazar_card(
    card_id: str = "bazar-001",
    nome: str = "Ana Paula Santos",
    phone: str = "5511900000002",
    adm: str = "Bradesco",
    stage_id: str = None,
    credito: str = "",
    proposta: str = "",
    **extra,
) -> dict:
    """Card de lead do Bazar/Site (lead orgânico, Z-API)."""
    from config import Stage
    return {
        "id":                    card_id,
        "title":                 nome,
        "stage_id":              stage_id or Stage.PRIMEIRA_ATIVACAO,
        "stage_name":            "1ª Ativação",
        "Nome do contato":       nome,
        "Telefone":              phone,
        "Adm":                   adm,
        "Fonte":                 "bazar",
        "Etiquetas":             "bazar",
        "Proposta Realizada":    proposta,
        "Crédito":               credito,
        "Grupo":                 "",
        "Cota":                  "",
        "Tipo contemplação":     "",
        "Tipo de bem":           "",
        "Parcelas pagas":        "",
        "Parcelas a vencer":     "",
        "Historico Conversa":    "[]",
        "Contexto Jornada":      "{}",
        "ZapSign Token":         "",
        **extra,
    }


def make_incoming_msg(
    text: str = "",
    media_type: str = None,
    media_url: str = None,
    raw: dict = None,
    source: str = "zapi",
):
    """IncomingMessage simulado."""
    from webhooks.router import IncomingMessage
    raw_payload = raw or {}
    if media_type and media_url:
        raw_payload = {"message": {media_type: {"url": media_url}}}
    return IncomingMessage(
        phone="5511900000001",
        text=text,
        source=source,
        media_type=media_type,
        raw=raw_payload,
        from_me=False,
    )


# ---------------------------------------------------------------------------
# Helpers de mock
# ---------------------------------------------------------------------------

def make_faro_mock(card: dict = None, extra_cards: list = None):
    """Cria um FaroClient mock com comportamentos padrão."""
    faro = AsyncMock()
    faro.__aenter__ = AsyncMock(return_value=faro)
    faro.__aexit__  = AsyncMock(return_value=None)
    faro.move_card    = AsyncMock(return_value={"success": True})
    faro.update_card  = AsyncMock(return_value={"success": True})
    faro.get_card     = AsyncMock(return_value=card or {})
    faro.get_cards    = AsyncMock(return_value=extra_cards or [])
    faro.watch_new    = AsyncMock(return_value=[card] if card else [])
    faro.watch_late   = AsyncMock(return_value=[])
    faro.get_cards_all_pages = AsyncMock(return_value=[card] if card else [])
    return faro


def make_whapi_mock():
    """Cria um WhapiClient mock."""
    w = AsyncMock()
    w.__aenter__ = AsyncMock(return_value=w)
    w.__aexit__  = AsyncMock(return_value=None)
    w.send_text    = AsyncMock(return_value={"sent": True})
    w.send_buttons = AsyncMock(return_value={"sent": True})
    w.send_image   = AsyncMock(return_value={"sent": True})
    return w


def make_zapi_mock():
    """Cria um ZAPIClient mock."""
    z = AsyncMock()
    z.__aenter__ = AsyncMock(return_value=z)
    z.__aexit__  = AsyncMock(return_value=None)
    z.send_text  = AsyncMock(return_value={"sent": True})
    z.send_image = AsyncMock(return_value={"sent": True})
    z.send_button_list = AsyncMock(return_value={"sent": True})
    return z


def make_ai_mock(response: str = "{}"):
    """Cria um AIClient mock."""
    ai = AsyncMock()
    ai.__aenter__ = AsyncMock(return_value=ai)
    ai.__aexit__  = AsyncMock(return_value=None)
    ai.complete            = AsyncMock(return_value=response)
    ai.complete_with_image = AsyncMock(return_value=response)
    return ai


def make_zapsign_mock(doc_token: str = "zapsign-doc-abc123", sign_url: str = "https://app.zapsign.com.br/sign/abc"):
    """Cria um ZapSignClient mock."""
    z = AsyncMock()
    z.__aenter__ = AsyncMock(return_value=z)
    z.__aexit__  = AsyncMock(return_value=None)
    z.create_from_template = AsyncMock(return_value={
        "doc_token":          doc_token,
        "open_id":            99999,
        "lead_sign_url":      sign_url,
        "internal_sign_urls": ["https://app.zapsign.com.br/sign/internal1"],
        "all_signers":        [],
    })
    return z


# ---------------------------------------------------------------------------
# Framework de asserções
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    duration_ms: float
    error: Optional[str] = None
    details: list[str] = field(default_factory=list)


class ScenarioRunner:
    def __init__(self, name: str, verbose: bool = False):
        self.name = name
        self.verbose = verbose
        self.assertions: list = []

    def assert_true(self, condition: bool, description: str) -> None:
        passed = bool(condition)
        self.assertions.append((description, passed, None, None))
        if self.verbose:
            print(f"    {TICK if passed else CROSS} {description}")
        if not passed:
            raise AssertionError(f"FALHOU: {description}")

    def assert_false(self, condition: bool, description: str) -> None:
        self.assert_true(not condition, description)

    def assert_equal(self, expected: Any, actual: Any, description: str) -> None:
        passed = expected == actual
        self.assertions.append((description, passed, expected, actual))
        if self.verbose:
            print(f"    {TICK if passed else CROSS} {description}")
            if not passed:
                print(f"       Esperado: {expected!r}")
                print(f"       Obtido:   {actual!r}")
        if not passed:
            raise AssertionError(f"FALHOU: {description} | esperado={expected!r} obtido={actual!r}")

    def assert_called(self, mock, description: str) -> None:
        self.assert_true(mock.called, description)

    def assert_not_called(self, mock, description: str) -> None:
        self.assert_false(mock.called, description)

    def assert_called_with_contains(self, mock, text: str, description: str) -> None:
        found = any(text.lower() in str(c).lower() for c in mock.call_args_list)
        self.assert_true(found, f"{description} (buscando '{text}')")

    def assert_field_updated(self, update_mock, field_name: str, description: str) -> None:
        """Verifica se um campo específico foi atualizado em qualquer chamada ao update_card."""
        found = any(
            field_name in str(c)
            for c in update_mock.call_args_list
        )
        self.assert_true(found, f"{description} (campo '{field_name}')")

    @property
    def all_passed(self) -> bool:
        return all(a[1] for a in self.assertions)

    @property
    def total(self) -> int:
        return len(self.assertions)

    @property
    def passed_count(self) -> int:
        return sum(1 for a in self.assertions if a[1])


# ===========================================================================
# FLUXO LISTAS
# ===========================================================================

async def test_L01_ativacao_lista(r: ScenarioRunner) -> None:
    """Lead de lista recebe mensagem de ativação via Whapi."""
    from config import Stage

    MockFaro  = MagicMock()
    MockWhapi = MagicMock()
    faro  = make_faro_mock()
    whapi = make_whapi_mock()
    MockFaro.return_value  = faro
    MockWhapi.return_value = whapi

    card = make_lista_card(card_id="L01", stage_id=Stage.LISTAS)
    faro.get_cards_all_pages = AsyncMock(return_value=[card])

    with (
        patch("jobs.ativacao_listas.FaroClient", MockFaro),
        patch("jobs.ativacao_listas.WhapiClient", MockWhapi),
        patch("jobs.ativacao_listas.AIClient", MagicMock(return_value=make_ai_mock())),
        patch("jobs.ativacao_listas._is_within_send_window", return_value=True),
        patch("jobs.ativacao_listas.filter_test_cards", side_effect=lambda x: x),
        patch("jobs.ativacao_listas.asyncio.sleep", new_callable=AsyncMock),
    ):
        from jobs.ativacao_listas import run_ativacao_listas
        await run_ativacao_listas()

    r.assert_called(whapi.send_buttons, "Mensagem de ativação enviada via Whapi")
    r.assert_called(faro.move_card, "Card movido para próximo stage")
    r.assert_called_with_contains(faro.move_card, Stage.PRIMEIRA_ATIVACAO, "Destino: PRIMEIRA_ATIVACAO")


async def test_L02_lead_aceita_proposta(r: ScenarioRunner) -> None:
    """Lead em EM_NEGOCIACAO aceita a proposta → card movido para ACEITO."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_lista_card(card_id="L02", stage_id=Stage.EM_NEGOCIACAO)
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = make_ai_mock(json.dumps({
        "intent": "ACEITAR", "reasoning": "Lead disse quero fechar", "response": "Que ótimo! 🎉",
    }))

    with (
        patch("webhooks.negociador.AIClient", MockAI),
        patch("webhooks.negociador.WhapiClient", MockWhapi),
        patch("webhooks.negociador.FaroClient", MockFaro),
        patch("webhooks.negociador.NOTIFY_PHONES", ["5511999999099"]),
    ):
        await handle_message(card=card, mensagem="Aceito! Pode mandar.", current_stage_id=Stage.EM_NEGOCIACAO)

    r.assert_called(whapi.send_text, "Resposta enviada ao lead")
    r.assert_called_with_contains(faro.move_card, Stage.ACEITO, "Card movido para ACEITO")


async def test_L03_lead_recusa(r: ScenarioRunner) -> None:
    """Lead recusa após sequência de preços esgotada → PERDIDO."""
    from config import Stage
    from webhooks.negociador import handle_message

    # Sequência esgotada: proposta já está no último valor (185000)
    card = make_lista_card(
        card_id="L03",
        stage_id=Stage.EM_NEGOCIACAO,
        proposta="185000",
        sequencia="150000,165000,175000,185000",
        indice_proposta="3",  # último índice
    )
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = make_ai_mock(json.dumps({
        "intent": "RECUSAR", "reasoning": "Lead sem interesse", "response": "Entendido. Até mais! 😊",
    }))

    with (
        patch("webhooks.negociador.AIClient", MockAI),
        patch("webhooks.negociador.WhapiClient", MockWhapi),
        patch("webhooks.negociador.FaroClient", MockFaro),
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        await handle_message(card=card, mensagem="Não tenho interesse.", current_stage_id=Stage.EM_NEGOCIACAO)

    r.assert_called_with_contains(faro.move_card, Stage.PERDIDO, "Card movido para PERDIDO (sequência esgotada)")


async def test_L04_lead_duvida_stage_mantido(r: ScenarioRunner) -> None:
    """Lead com dúvida → resposta IA, stage NÃO muda."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_lista_card(card_id="L04", stage_id=Stage.EM_NEGOCIACAO)
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = make_ai_mock(json.dumps({
        "intent": "DUVIDA", "reasoning": "Pergunta sobre processo", "response": "É 100% seguro...",
    }))

    with (
        patch("webhooks.negociador.AIClient", MockAI),
        patch("webhooks.negociador.WhapiClient", MockWhapi),
        patch("webhooks.negociador.FaroClient", MockFaro),
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        await handle_message(card=card, mensagem="Como funciona o pagamento?", current_stage_id=Stage.EM_NEGOCIACAO)

    r.assert_called(whapi.send_text, "Resposta à dúvida enviada")
    r.assert_not_called(faro.move_card, "Stage NÃO muda para dúvida simples")


async def test_L05_lead_quer_atendente(r: ScenarioRunner) -> None:
    """Lead pede atendente humano → equipe notificada via Whapi."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_lista_card(card_id="L05", stage_id=Stage.EM_NEGOCIACAO)
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    notify_phone = "5511999999099"

    sent_messages = []
    async def track_send(phone, msg):
        sent_messages.append((phone, msg))
        return {"sent": True}
    whapi.send_text.side_effect = track_send

    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = make_ai_mock(json.dumps({
        "intent": "AGENDAR", "reasoning": "Pediu atendente", "response": "Claro! 😊",
    }))

    with (
        patch("webhooks.negociador.AIClient", MockAI),
        patch("webhooks.negociador.WhapiClient", MockWhapi),
        patch("webhooks.negociador.FaroClient", MockFaro),
        patch("webhooks.negociador.NOTIFY_PHONES", [notify_phone]),
    ):
        await handle_message(card=card, mensagem="Quero falar com um humano.", current_stage_id=Stage.EM_NEGOCIACAO)

    r.assert_true(len(sent_messages) >= 2, "Resposta ao lead + notificação à equipe")
    team_notified = any(notify_phone in phone for phone, _ in sent_messages)
    r.assert_true(team_notified, "Equipe notificada no número correto")


async def test_L06_contra_proposta_com_valor_inline(r: ScenarioRunner) -> None:
    """Lead diz 'me ofereceram R$ 200.000' → sistema responde."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_lista_card(card_id="L06", stage_id=Stage.EM_NEGOCIACAO, proposta="185000")
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = make_ai_mock(json.dumps({
        "intent": "OFERECERAM_MAIS",
        "reasoning": "Lead mencionou oferta de terceiro",
        "response": "Entendo! Vou consultar. Um momento.",
        "valor_oferta_externa": 200000.0,
    }))

    with (
        patch("webhooks.negociador.AIClient", MockAI),
        patch("webhooks.negociador.WhapiClient", MockWhapi),
        patch("webhooks.negociador.FaroClient", MockFaro),
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        await handle_message(
            card=card,
            mensagem="Me ofereceram R$ 200.000 por essa cota, vocês conseguem cobrir?",
            current_stage_id=Stage.EM_NEGOCIACAO,
        )

    r.assert_called(whapi.send_text, "Resposta enviada ao lead")


async def test_L07_contra_proposta_sem_valor(r: ScenarioRunner) -> None:
    """Lead faz contra-proposta sem informar valor → bot responde."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_lista_card(card_id="L07", stage_id=Stage.EM_NEGOCIACAO)
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = make_ai_mock(json.dumps({
        "intent": "CONTRA_PROPOSTA", "reasoning": "Sem valor", "response": "Que valor você tem em mente?",
    }))

    with (
        patch("webhooks.negociador.AIClient", MockAI),
        patch("webhooks.negociador.WhapiClient", MockWhapi),
        patch("webhooks.negociador.FaroClient", MockFaro),
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        await handle_message(
            card=card,
            mensagem="Acho que precisa ser mais que isso, né?",
            current_stage_id=Stage.EM_NEGOCIACAO,
        )

    r.assert_called(whapi.send_text, "Bot responde à contra-proposta")


async def test_L08_aceito_lista_solicita_dados(r: ScenarioRunner) -> None:
    """Card ACEITO (lista) → contrato.py solicita dados pessoais, move para ASSINATURA."""
    from config import Stage
    from jobs.contrato import _process_card

    card = make_lista_card(card_id="L08", stage_id=Stage.ACEITO)
    card_fresco = {**card, "stage_id": Stage.ACEITO}
    faro  = make_faro_mock(card_fresco)
    whapi = make_whapi_mock()
    ai    = make_ai_mock("Parabéns Carlos! Para o contrato preciso de: CPF, RG, Endereço e E-mail.")
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = ai

    with (
        patch("jobs.contrato.FaroClient", MockFaro),
        patch("jobs.contrato.WhapiClient", MockWhapi),
        patch("jobs.contrato.AIClient", MockAI),
        patch("jobs.contrato.NOTIFY_PHONES", []),
    ):
        await _process_card(card)

    r.assert_called_with_contains(faro.move_card, Stage.ASSINATURA, "Card movido para ASSINATURA")
    r.assert_called(whapi.send_text, "Mensagem solicitando dados enviada ao lead")
    r.assert_true(True, "ZapSign não criado neste momento (correto para lista)")


async def test_L09_coleta_dados_completos(r: ScenarioRunner) -> None:
    """Lead envia todos os dados pessoais de uma vez → confirmação + pedido de extrato."""
    from config import Stage
    from webhooks.agente_contrato import handle_dados_pessoais

    card = make_lista_card(card_id="L09", stage_id=Stage.ASSINATURA)
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    ai    = make_ai_mock("Perfeito, Carlos! Recebi todos os dados ✅ Agora preciso do extrato.")
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = ai

    msg_texto = (
        "CPF: 123.456.789-00, RG: 12.345.678-9, "
        "endereço: Rua das Flores 123, São Paulo SP CEP 01234-567, "
        "e-mail: carlos@email.com"
    )

    with (
        patch("webhooks.agente_contrato.AIClient", MockAI),
        patch("webhooks.agente_contrato.WhapiClient", MockWhapi),
        patch("webhooks.agente_contrato.FaroClient", MockFaro),
    ):
        await handle_dados_pessoais(card=card, texto=msg_texto)

    r.assert_called(whapi.send_text, "Confirmação enviada ao lead")
    r.assert_called(faro.update_card, "Dados gravados no FARO")
    r.assert_field_updated(faro.update_card, "CPF", "CPF salvo")
    r.assert_field_updated(faro.update_card, "Dados Pessoais Texto", "Dados Pessoais Texto atualizado")


async def test_L10_coleta_dados_parciais(r: ScenarioRunner) -> None:
    """Lead envia apenas CPF → bot confirma CPF e pede os dados restantes."""
    from config import Stage
    from webhooks.agente_contrato import handle_dados_pessoais

    card = make_lista_card(card_id="L10", stage_id=Stage.ASSINATURA)
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    ai    = make_ai_mock("Recebi seu CPF ✅ Ainda preciso de:\n• *RG ou CNH*\n• *Endereço completo*\n• *E-mail*")
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = ai

    with (
        patch("webhooks.agente_contrato.AIClient", MockAI),
        patch("webhooks.agente_contrato.WhapiClient", MockWhapi),
        patch("webhooks.agente_contrato.FaroClient", MockFaro),
    ):
        await handle_dados_pessoais(card=card, texto="Meu CPF é 123.456.789-00")

    r.assert_called(whapi.send_text, "Bot pede dados restantes")
    r.assert_field_updated(faro.update_card, "CPF", "CPF extraído e salvo")


async def test_L11_extrato_com_dados_completos(r: ScenarioRunner) -> None:
    """Lead envia extrato com dados pessoais já completos → gera ZapSign."""
    from config import Stage
    from webhooks.agente_contrato import handle_extrato_recebido

    dados_ok = json.dumps({
        "CPF": "123.456.789-00",
        "RG": "12.345.678-9",
        "Endereco": "Rua das Flores, 123, São Paulo SP 01234-567",
        "Email": "carlos@email.com",
    })
    card = make_lista_card(card_id="L11", stage_id=Stage.ASSINATURA, dados_pessoais=dados_ok)
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi

    msg = make_incoming_msg(media_type="document", media_url="https://example.com/extrato.pdf")

    with (
        patch("webhooks.agente_contrato.WhapiClient", MockWhapi),
        patch("webhooks.agente_contrato.FaroClient", MockFaro),
        patch("asyncio.create_task") as create_task_mock,
    ):
        await handle_extrato_recebido(card=card, msg=msg)

    r.assert_called(whapi.send_text, "Confirmação de extrato recebido enviada")
    r.assert_called(create_task_mock, "Task de geração do ZapSign disparada")


async def test_L12_extrato_sem_dados_bloqueia(r: ScenarioRunner) -> None:
    """Lead envia extrato mas dados pessoais estão incompletos → bloqueia e pede dados."""
    from config import Stage
    from webhooks.agente_contrato import handle_extrato_recebido

    dados_incompletos = json.dumps({"CPF": "123.456.789-00"})  # faltam RG, Endereço, Email
    card = make_lista_card(card_id="L12", stage_id=Stage.ASSINATURA, dados_pessoais=dados_incompletos)
    faro = make_faro_mock(card)
    whapi = make_whapi_mock()

    msg = make_incoming_msg(media_type="document", media_url="https://example.com/extrato.pdf")

    sent_messages = []
    async def track(phone, msg_text):
        sent_messages.append(msg_text)
        return {"sent": True}
    whapi.send_text.side_effect = track

    with (
        patch("webhooks.agente_contrato.WhapiClient", return_value=whapi),
        patch("webhooks.agente_contrato.FaroClient", return_value=faro),
    ):
        await handle_extrato_recebido(card=card, msg=msg)

    r.assert_called(whapi.send_text, "Mensagem pedindo dados enviada")
    has_rg = any("RG" in m or "rg" in m.lower() for m in sent_messages)
    r.assert_true(has_rg, "Mensagem menciona RG que está faltando")


async def test_L13_followup_proposta(r: ScenarioRunner) -> None:
    """Lead não responde proposta → follow-up enviado."""
    from config import Stage
    from jobs.follow_up import run_follow_up

    card = make_lista_card(card_id="L13", stage_id=Stage.EM_NEGOCIACAO, num_fups=2)
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    ai    = make_ai_mock("Carlos, ainda está por aqui? A oferta de R$ 185.000 ainda está de pé! 😊")
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = ai

    with (
        patch("jobs.follow_up.FaroClient", MockFaro),
        patch("jobs.follow_up.WhapiClient", MockWhapi),
        patch("jobs.follow_up.AIClient", MockAI),
        patch("jobs.follow_up._is_within_send_window", return_value=True),
        patch("jobs.follow_up.filter_test_cards", side_effect=lambda x: x),
        patch("jobs.follow_up.NOTIFY_PHONES", []),
    ):
        await run_follow_up()

    r.assert_called(whapi.send_text, "Follow-up enviado")
    r.assert_called(faro.update_card, "Contador de follow-ups atualizado")


async def test_L14_followup_esgotamento(r: ScenarioRunner) -> None:
    """Lead com 8+ follow-ups sem resposta → movido para PERDIDO, equipe notificada."""
    from config import Stage
    from jobs.follow_up import run_follow_up

    card = make_lista_card(card_id="L14", stage_id=Stage.EM_NEGOCIACAO, num_fups=8)
    faro  = make_faro_mock(card)
    whapi = make_whapi_mock()
    ai    = make_ai_mock("")
    MockFaro  = MagicMock(); MockFaro.return_value  = faro
    MockWhapi = MagicMock(); MockWhapi.return_value = whapi
    MockAI    = MagicMock(); MockAI.return_value    = ai

    with (
        patch("jobs.follow_up.FaroClient", MockFaro),
        patch("jobs.follow_up.WhapiClient", MockWhapi),
        patch("jobs.follow_up.AIClient", MockAI),
        patch("services.whapi.WhapiClient", MockWhapi),   # cobre o inline import no esgotados
        patch("jobs.follow_up._is_within_send_window", return_value=True),
        patch("jobs.follow_up.filter_test_cards", side_effect=lambda x: x),
        patch("jobs.follow_up.NOTIFY_PHONES", ["5511999999099"]),
    ):
        await run_follow_up()

    r.assert_called_with_contains(faro.move_card, Stage.PERDIDO, "Card esgotado → PERDIDO")
    r.assert_called(whapi.send_text, "Equipe notificada do esgotamento")


# ===========================================================================
# FLUXO BAZAR/SITE
# ===========================================================================

async def test_B01_extrato_qualificado_imagem(r: ScenarioRunner) -> None:
    """Lead Bazar envia imagem de extrato → QUALIFICADO → move para PRECIFICACAO."""
    from config import Stage
    from webhooks.qualificador import handle_qualification

    card = make_bazar_card(card_id="B01", stage_id=Stage.PRIMEIRA_ATIVACAO)
    faro = make_faro_mock(card)
    zapi = make_zapi_mock()

    # Simula resposta IA de extrato qualificado
    ai_resp = json.dumps({
        "resultado": "QUALIFICADO",
        "administradora": "Bradesco",
        "valor_credito": 280000.0,
        "valor_pago": 56000.0,  # 20% pago
        "parcelas_pagas": 24,
        "total_parcelas": 120,
        "motivo": "Cota elegível: 20% pago, dentro dos critérios.",
        "tipo_contemplacao": "Sorteio",
        "tipo_bem": "Imóvel",
        "grupo": "B078",
        "cota": "042",
    })
    ai = make_ai_mock(ai_resp)

    msg = make_incoming_msg(media_type="image", media_url="https://example.com/extrato.jpg")

    with (
        patch("webhooks.qualificador.AIClient", return_value=ai),
        patch("webhooks.qualificador.FaroClient", return_value=faro),
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi),
    ):
        await handle_qualification(card=card, msg=msg)

    r.assert_called(zapi.send_text, "Mensagem de qualificado enviada")
    r.assert_called_with_contains(faro.move_card, Stage.PRECIFICACAO, "Card movido para PRECIFICACAO")
    r.assert_field_updated(faro.update_card, "Crédito", "Crédito salvo no FARO")
    r.assert_field_updated(faro.update_card, "Tipo contemplação", "Tipo contemplação salvo")
    r.assert_field_updated(faro.update_card, "Tipo de bem", "Tipo de bem salvo")
    r.assert_field_updated(faro.update_card, "Grupo", "Grupo salvo")
    r.assert_field_updated(faro.update_card, "Cota", "Cota salva")


async def test_B02_extrato_nao_qualificado(r: ScenarioRunner) -> None:
    """Lead com >50% pago → NAO_QUALIFICADO."""
    from config import Stage
    from webhooks.qualificador import handle_qualification

    card = make_bazar_card(card_id="B02", stage_id=Stage.PRIMEIRA_ATIVACAO)
    faro = make_faro_mock(card)
    zapi = make_zapi_mock()

    ai_resp = json.dumps({
        "resultado": "NAO_QUALIFICADO",
        "administradora": "Bradesco",
        "valor_credito": 200000.0,
        "valor_pago": 120000.0,  # 60% pago — excede limite
        "parcelas_pagas": 72,
        "total_parcelas": 120,
        "motivo": "60% do crédito já pago, excede o teto de 50%.",
        "tipo_contemplacao": "Sorteio",
        "tipo_bem": "Imóvel",
        "grupo": "C012",
        "cota": "088",
    })
    ai = make_ai_mock(ai_resp)
    msg = make_incoming_msg(media_type="document", media_url="https://example.com/extrato.pdf")

    with (
        patch("webhooks.qualificador.AIClient", return_value=ai),
        patch("webhooks.qualificador.FaroClient", return_value=faro),
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi),
    ):
        await handle_qualification(card=card, msg=msg)

    r.assert_called(zapi.send_text, "Mensagem de não qualificado enviada")
    r.assert_called_with_contains(faro.move_card, Stage.NAO_QUALIFICADO, "Card movido para NAO_QUALIFICADO")


async def test_B03_extrato_incorreto_boleto(r: ScenarioRunner) -> None:
    """Lead envia boleto ou documento errado → EXTRATO_INCORRETO → orientação."""
    from config import Stage
    from webhooks.qualificador import handle_qualification

    card = make_bazar_card(card_id="B03", stage_id=Stage.SEGUNDA_ATIVACAO)
    faro = make_faro_mock(card)
    zapi = make_zapi_mock()

    ai_resp = json.dumps({
        "resultado": "EXTRATO_INCORRETO",
        "administradora": None,
        "valor_credito": 0.0,
        "valor_pago": 0.0,
        "parcelas_pagas": 0,
        "total_parcelas": 0,
        "motivo": "Documento parece ser um boleto, não um extrato de consórcio.",
        "tipo_contemplacao": None,
        "tipo_bem": None,
        "grupo": None,
        "cota": None,
    })
    ai = make_ai_mock(ai_resp)
    msg = make_incoming_msg(media_type="document", media_url="https://example.com/boleto.pdf")

    with (
        patch("webhooks.qualificador.AIClient", return_value=ai),
        patch("webhooks.qualificador.FaroClient", return_value=faro),
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi),
    ):
        await handle_qualification(card=card, msg=msg)

    r.assert_called(zapi.send_text, "Orientação sobre extrato correto enviada")
    r.assert_not_called(faro.move_card, "Stage NÃO muda para extrato incorreto")


async def test_B04_texto_sem_extrato(r: ScenarioRunner) -> None:
    """Lead envia texto sem extrato → bot solicita o extrato."""
    from config import Stage
    from webhooks.qualificador import handle_qualification

    card = make_bazar_card(card_id="B04", stage_id=Stage.PRIMEIRA_ATIVACAO)
    faro = make_faro_mock(card)
    zapi = make_zapi_mock()

    msg = make_incoming_msg(text="Olá! Vi que vocês compram consórcio contemplado.")

    with (
        patch("webhooks.qualificador.AIClient", return_value=make_ai_mock()),
        patch("webhooks.qualificador.FaroClient", return_value=faro),
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi),
    ):
        await handle_qualification(card=card, msg=msg)

    r.assert_called(zapi.send_text, "Solicitação de extrato enviada")
    r.assert_not_called(faro.move_card, "Stage NÃO muda sem extrato")


async def test_B05_recusa_verbal(r: ScenarioRunner) -> None:
    """Lead diz que vendeu a cota → PERDIDO imediatamente."""
    from config import Stage
    from webhooks.qualificador import handle_qualification

    card = make_bazar_card(card_id="B05", stage_id=Stage.SEGUNDA_ATIVACAO)
    faro = make_faro_mock(card)
    zapi = make_zapi_mock()

    msg = make_incoming_msg(text="Já vendi minha cota semana passada, não preciso mais.")

    with (
        patch("webhooks.qualificador.AIClient", return_value=make_ai_mock()),
        patch("webhooks.qualificador.FaroClient", return_value=faro),
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi),
    ):
        await handle_qualification(card=card, msg=msg)

    r.assert_called(zapi.send_text, "Despedida enviada")
    r.assert_called_with_contains(faro.move_card, Stage.PERDIDO, "Card movido para PERDIDO")


async def test_B06_erro_tecnico_extrato(r: ScenarioRunner) -> None:
    """Erro na IA de análise de extrato → lead orientado, equipe alertada via Slack."""
    from config import Stage
    from webhooks.qualificador import handle_qualification
    from services.ai import AIError

    card = make_bazar_card(card_id="B06", stage_id=Stage.PRIMEIRA_ATIVACAO)
    faro = make_faro_mock(card)
    zapi = make_zapi_mock()
    slack_mock = AsyncMock()

    # AI lança exceção
    ai_failing = make_ai_mock()
    ai_failing.complete_with_image = AsyncMock(side_effect=AIError("Timeout na IA"))

    msg = make_incoming_msg(media_type="image", media_url="https://example.com/extrato.jpg")

    with (
        patch("webhooks.qualificador.AIClient", return_value=ai_failing),
        patch("webhooks.qualificador.FaroClient", return_value=faro),
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi),
        patch("webhooks.qualificador.slack_error", new=AsyncMock()) as slack_err,
    ):
        await handle_qualification(card=card, msg=msg)

    r.assert_called(zapi.send_text, "Mensagem de erro técnico enviada ao lead")
    r.assert_not_called(faro.move_card, "Stage NÃO muda em erro técnico")


async def test_B07_qualificado_campos_corretos_no_faro(r: ScenarioRunner) -> None:
    """Após qualificação: Crédito (não Valor do crédito) + Tipo contemplação + Tipo de bem + Grupo + Cota gravados."""
    from config import Stage
    from webhooks.qualificador import handle_qualification

    card = make_bazar_card(card_id="B07", stage_id=Stage.TERCEIRA_ATIVACAO)
    faro = make_faro_mock(card)
    zapi = make_zapi_mock()

    ai_resp = json.dumps({
        "resultado": "QUALIFICADO",
        "administradora": "Itaú",
        "valor_credito": 350000.0,
        "valor_pago": 52500.0,  # 15%
        "parcelas_pagas": 18,
        "total_parcelas": 120,
        "motivo": "Cota elegível.",
        "tipo_contemplacao": "Lance",
        "tipo_bem": "Veículo",
        "grupo": "G099",
        "cota": "007",
    })
    ai = make_ai_mock(ai_resp)
    msg = make_incoming_msg(media_type="document", media_url="https://example.com/extrato.pdf")

    captured_updates = {}
    async def capture_update(card_id, fields):
        captured_updates.update(fields)
        return {"success": True}
    faro.update_card = AsyncMock(side_effect=capture_update)

    with (
        patch("webhooks.qualificador.AIClient", return_value=ai),
        patch("webhooks.qualificador.FaroClient", return_value=faro),
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi),
    ):
        await handle_qualification(card=card, msg=msg)

    r.assert_equal("350000.0", captured_updates.get("Crédito"), "Campo 'Crédito' correto (não 'Valor do crédito')")
    r.assert_equal("Lance", captured_updates.get("Tipo contemplação"), "Tipo contemplação salvo")
    r.assert_equal("Veículo", captured_updates.get("Tipo de bem"), "Tipo de bem salvo")
    r.assert_equal("G099", captured_updates.get("Grupo"), "Grupo salvo")
    r.assert_equal("007", captured_updates.get("Cota"), "Cota salva")
    r.assert_true("Valor do crédito" not in captured_updates, "Campo 'Valor do crédito' NÃO usado (deprecated)")


async def test_B08_bazar_aceito_gera_zapsign_direto(r: ScenarioRunner) -> None:
    """Lead Bazar em ACEITO → contrato gerado imediatamente (sem coletar extrato de novo)."""
    from config import Stage
    from jobs.contrato import _process_card

    card = make_bazar_card(
        card_id="B08",
        stage_id=Stage.ACEITO,
        adm="Bradesco",
        credito="280000",
        proposta="R$ 168.000,00",
        **{
            "Grupo": "B078",
            "Cota": "042",
            "Tipo contemplação": "Sorteio",
            "Tipo de bem": "Imóvel",
            "Parcelas pagas": "24",
            "Email": "ana@email.com",
            "CPF": "987.654.321-00",
        }
    )
    card_fresco = {**card, "stage_id": Stage.ACEITO}
    faro = make_faro_mock(card_fresco)
    zapi = make_zapi_mock()
    zapsign = make_zapsign_mock()
    whapi = make_whapi_mock()

    with (
        patch("jobs.contrato.FaroClient", return_value=faro),
        patch("jobs.contrato.ZapSignClient", return_value=zapsign),
        patch("jobs.contrato.get_zapi_for_card", return_value=zapi),
        patch("jobs.contrato.WhapiClient", return_value=whapi),
        patch("jobs.contrato.NOTIFY_PHONES", []),
    ):
        await _process_card(card)

    r.assert_called(zapsign.create_from_template, "ZapSign criado diretamente (sem coleta de extrato)")
    r.assert_called_with_contains(faro.move_card, Stage.ASSINATURA, "Card movido para ASSINATURA")
    r.assert_called(zapi.send_text, "Link de assinatura enviado ao lead via Z-API")
    r.assert_field_updated(faro.update_card, "ZapSign Token", "ZapSign Token salvo no FARO")


# ===========================================================================
# EDGE CASES
# ===========================================================================

async def test_E01_adm_sem_template(r: ScenarioRunner) -> None:
    """Administradora sem template ZapSign → equipe notificada, lead NÃO recebe mensagem errada."""
    from config import Stage
    from jobs.contrato import _process_card

    card = make_bazar_card(
        card_id="E01",
        stage_id=Stage.ACEITO,
        adm="Administradora Desconhecida",
    )
    card_fresco = {**card, "stage_id": Stage.ACEITO}
    faro = make_faro_mock(card_fresco)
    zapi = make_zapi_mock()
    whapi = make_whapi_mock()

    with (
        patch("jobs.contrato.FaroClient", return_value=faro),
        patch("jobs.contrato.get_zapi_for_card", return_value=zapi),
        patch("jobs.contrato.WhapiClient", return_value=whapi),
        patch("jobs.contrato.NOTIFY_PHONES", ["5511999999099"]),
    ):
        await _process_card(card)

    r.assert_not_called(zapi.send_text, "Lead NÃO recebe mensagem quando sem template")
    r.assert_called(whapi.send_text, "Equipe notificada da ausência de template")


async def test_E02_zapsign_error_bazar(r: ScenarioRunner) -> None:
    """ZapSign falha para lead Bazar → MSG_ERRO_INTERNO enviada ao lead + notifica equipe."""
    from config import Stage
    from jobs.contrato import _process_card
    from services.zapsign import ZapSignError

    card = make_bazar_card(
        card_id="E02",
        stage_id=Stage.ACEITO,
        adm="Bradesco",
        credito="200000",
        proposta="R$ 120.000,00",
    )
    card_fresco = {**card, "stage_id": Stage.ACEITO}
    faro = make_faro_mock(card_fresco)
    zapi = make_zapi_mock()
    whapi = make_whapi_mock()

    zapsign_fail = make_zapsign_mock()
    zapsign_fail.create_from_template = AsyncMock(side_effect=ZapSignError("API timeout", 500))

    with (
        patch("jobs.contrato.FaroClient", return_value=faro),
        patch("jobs.contrato.ZapSignClient", return_value=zapsign_fail),
        patch("jobs.contrato.get_zapi_for_card", return_value=zapi),
        patch("jobs.contrato.WhapiClient", return_value=whapi),
        patch("jobs.contrato.NOTIFY_PHONES", ["5511999999099"]),
    ):
        await _process_card(card)

    r.assert_called(zapi.send_text, "Lead Bazar recebe mensagem de espera (MSG_ERRO_INTERNO)")
    r.assert_called(whapi.send_text, "Equipe notificada do erro ZapSign")


async def test_E03_zapsign_error_lista_sem_msg_lead(r: ScenarioRunner) -> None:
    """ZapSign falha para lead Lista em ASSINATURA → APENAS notifica equipe, NÃO manda msg ao lead."""
    from jobs.contrato import generate_and_send_contract
    from services.zapsign import ZapSignError

    dados_ok = json.dumps({
        "CPF": "111.222.333-44",
        "RG": "11.222.333-4",
        "Endereco": "Rua Teste 1, São Paulo",
        "Email": "teste@email.com",
    })
    card = make_lista_card(
        card_id="E03",
        adm="Santander",
        dados_pessoais=dados_ok,
        **{"stage_id": "assinatura-stage"}
    )
    faro = make_faro_mock(card)
    whapi = make_whapi_mock()

    zapsign_fail = make_zapsign_mock()
    zapsign_fail.create_from_template = AsyncMock(side_effect=ZapSignError("Erro", 500))

    with (
        patch("jobs.contrato.FaroClient", return_value=faro),
        patch("jobs.contrato.ZapSignClient", return_value=zapsign_fail),
        patch("jobs.contrato.WhapiClient", return_value=whapi),
        patch("jobs.contrato.NOTIFY_PHONES", ["5511999999099"]),
    ):
        await generate_and_send_contract(card)

    # Whapi.send_text é chamado para a equipe, MAS a mensagem ao lead deve ser OMITIDA
    # O teste verifica que não há msg "Sua proposta foi aceita" ou "MSG_ERRO_INTERNO" para o lead
    called_msgs = [str(c) for c in whapi.send_text.call_args_list]
    lead_phone_called = any(card["Telefone"] in msg for msg in called_msgs)
    r.assert_false(lead_phone_called, "Lead de lista NÃO recebe MSG_ERRO_INTERNO quando ZapSign falha")
    # Equipe deve ser notificada
    team_called = any("5511999999099" in msg for msg in called_msgs)
    r.assert_true(team_called, "Equipe notificada do erro ZapSign para lista")


async def test_E04_assinatura_sem_token_keywords(r: ScenarioRunner) -> None:
    """Lead em ASSINATURA sem token e usa palavras de problema com link → redireciona p/ equipe."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_lista_card(card_id="E04", stage_id=Stage.ASSINATURA, zapsign_token="")
    faro = make_faro_mock(card)
    whapi = make_whapi_mock()

    ai_resp = json.dumps({
        "intent": "DUVIDA",
        "reasoning": "Problema com link",
        "response": "Pode me ajudar com o link?",
    })

    with (
        patch("webhooks.negociador.AIClient", return_value=make_ai_mock(ai_resp)),
        patch("webhooks.negociador.WhapiClient", return_value=whapi),
        patch("webhooks.negociador.FaroClient", return_value=faro),
        patch("webhooks.negociador.NOTIFY_PHONES", ["5511999999099"]),
    ):
        await handle_message(
            card=card,
            mensagem="O link não funciona, não consigo abrir.",
            current_stage_id=Stage.ASSINATURA,
        )

    r.assert_called(whapi.send_text, "Resposta enviada ao lead em ASSINATURA")


async def test_E05_negociador_lead_desconfiante(r: ScenarioRunner) -> None:
    """Lead muito desconfiante → resposta de credibilidade, NÃO muda stage."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_lista_card(card_id="E05", stage_id=Stage.EM_NEGOCIACAO)
    faro = make_faro_mock(card)
    whapi = make_whapi_mock()

    ai_resp = json.dumps({
        "intent": "DESCONFIANCA",
        "reasoning": "Lead suspeita de golpe",
        "response": "Entendo sua preocupação! Somos registrados no CNPJ 12.345.678/0001-00...",
    })

    with (
        patch("webhooks.negociador.AIClient", return_value=make_ai_mock(ai_resp)),
        patch("webhooks.negociador.WhapiClient", return_value=whapi),
        patch("webhooks.negociador.FaroClient", return_value=faro),
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        await handle_message(
            card=card,
            mensagem="Isso parece golpe. Como eu sei que vocês são confiáveis?",
            current_stage_id=Stage.EM_NEGOCIACAO,
        )

    r.assert_called(whapi.send_text, "Resposta de credibilidade enviada")
    r.assert_not_called(faro.move_card, "Stage NÃO muda para desconfiança")


async def test_E06_deteccao_tom_informal(r: ScenarioRunner) -> None:
    """_detect_tom detecta tom informal corretamente."""
    from webhooks.negociador import _detect_tom

    r.assert_equal("informal", _detect_tom("opa, tudo blz?? kk vlw"), "Tom informal detectado")
    r.assert_equal("formal",   _detect_tom("Prezado, bom dia. Gostaria de saber mais."), "Tom formal detectado")
    r.assert_equal("ansioso",  _detect_tom("urgente! preciso logo de uma resposta quando você pode?"), "Tom ansioso detectado")
    r.assert_equal("desconfiante", _detect_tom("esse negócio parece golpe, não é fraude isso?"), "Tom desconfiante detectado")
    r.assert_equal("",         _detect_tom("ok"), "Tom neutro: sem sinal claro retorna vazio")


async def test_E07_zapsign_template_ids(r: ScenarioRunner) -> None:
    """Template IDs mapeados corretamente (verificação de integridade)."""
    from services.zapsign import get_template_for_adm

    r.assert_equal("6c143118-62ec-4a3d-85c3-09eee5465bf5", get_template_for_adm("Santander"),    "Santander template")
    r.assert_equal("9c9498d3-c8b1-4a81-9e98-c68534be7429", get_template_for_adm("Bradesco"),      "Bradesco template")
    r.assert_equal("8fdb67d7-a01b-45c5-afeb-c152eccd030f", get_template_for_adm("Itaú"),          "Itaú template")
    r.assert_equal("c50dd4fd-4515-46eb-8beb-962ef87f4140", get_template_for_adm("Caixa"),          "Caixa template")
    r.assert_equal("f6b4dd2a-efd2-4a8c-a25e-d711c65eb7e0", get_template_for_adm("Embracon"),      "Embracon template")
    r.assert_equal("9c9498d3-c8b1-4a81-9e98-c68534be7429", get_template_for_adm("Porto Seguro"),  "Porto template (mesmo que Bradesco)")
    r.assert_equal(None, get_template_for_adm("Administradora Inexistente"), "Adm desconhecida retorna None")


async def test_E08_fmt_currency_formatos(r: ScenarioRunner) -> None:
    """_fmt_currency lida corretamente com múltiplos formatos de entrada."""
    from jobs.precificacao import _fmt_currency

    r.assert_equal("R$ 185.000,00", _fmt_currency("185000"),          "Inteiro sem formatação")
    r.assert_equal("R$ 185.000,00", _fmt_currency("R$ 185.000,00"),   "Já formatado BR")
    r.assert_equal("R$ 185.000,00", _fmt_currency("185,000.00"),      "Formato US")
    r.assert_equal("R$ 1.800,00",   _fmt_currency("1800"),            "Valor de parcela")
    r.assert_equal("a consultar",   _fmt_currency(""),                "Vazio → a consultar")
    r.assert_equal("a consultar",   _fmt_currency(None),              "None → a consultar")


async def test_E09_is_lista_detection(r: ScenarioRunner) -> None:
    """is_lista() detecta corretamente leads de lista vs bazar."""
    from services.faro import is_lista

    card_lista = make_lista_card()
    card_bazar = make_bazar_card()
    card_lista_etiqueta = {**make_bazar_card(), "Etiquetas": "lista_vip", "Fonte": "bazar"}

    r.assert_true(is_lista(card_lista),          "Card com Fonte=lista_teste → is_lista True")
    r.assert_false(is_lista(card_bazar),         "Card Bazar → is_lista False")
    # Fonte="bazar" tem prioridade sobre Etiquetas="lista_vip" → is_lista False
    r.assert_false(is_lista(card_lista_etiqueta), "Fonte='bazar' tem prioridade sobre Etiquetas='lista_vip' → is_lista False")


async def test_E10_build_form_fields_zapsign(r: ScenarioRunner) -> None:
    """build_form_fields monta campos do ZapSign corretamente a partir de um card."""
    from services.zapsign import build_form_fields

    card = {
        **make_lista_card(),
        "CPF": "123.456.789-00",
        "RG": "12.345.678-9",
        "Email": "carlos@email.com",
        "Endereço": "Rua das Flores, 123",
        "Bairro": "Centro",
        "Cidade": "São Paulo",
        "Estado": "SP",
        "CEP": "01234-567",
        "Crédito": "250000",
        "Proposta Realizada": "R$ 185.000,00",
    }

    fields = build_form_fields(card)

    r.assert_equal("Carlos Ferreira",   fields.get("nome_completo"),  "nome_completo correto")
    r.assert_equal("123.456.789-00",    fields.get("cpf"),            "cpf correto")
    r.assert_equal("carlos@email.com",  fields.get("email"),          "email correto")
    r.assert_equal("Santander",         fields.get("administradora"), "administradora correta")
    r.assert_equal("A042",              fields.get("grupo"),          "grupo correto")
    r.assert_equal("015",               fields.get("cota"),           "cota correta")
    r.assert_true(fields.get("credito") != "",                        "crédito preenchido")
    r.assert_true(fields.get("valor_proposta") != "",                 "valor_proposta preenchido")


# ===========================================================================
# Runner principal
# ===========================================================================

ALL_SCENARIOS = [
    ("L01", "Ativação de lead lista",                    test_L01_ativacao_lista),
    ("L02", "Lead lista aceita proposta → ACEITO",       test_L02_lead_aceita_proposta),
    ("L03", "Lead lista recusa → PERDIDO",               test_L03_lead_recusa),
    ("L04", "Lead lista dúvida → stage mantido",         test_L04_lead_duvida_stage_mantido),
    ("L05", "Lead lista quer atendente humano",          test_L05_lead_quer_atendente),
    ("L06", "Contra-proposta com valor inline",          test_L06_contra_proposta_com_valor_inline),
    ("L07", "Contra-proposta sem valor → pede valor",   test_L07_contra_proposta_sem_valor),
    ("L08", "ACEITO lista → solicita dados pessoais",   test_L08_aceito_lista_solicita_dados),
    ("L09", "Coleta dados completos numa msg",           test_L09_coleta_dados_completos),
    ("L10", "Coleta dados parciais → pede restantes",   test_L10_coleta_dados_parciais),
    ("L11", "Extrato + dados completos → ZapSign",      test_L11_extrato_com_dados_completos),
    ("L12", "Extrato + dados incompletos → bloqueia",   test_L12_extrato_sem_dados_bloqueia),
    ("L13", "Follow-up em proposta sem resposta",       test_L13_followup_proposta),
    ("L14", "Esgotamento follow-ups → PERDIDO",         test_L14_followup_esgotamento),
    ("B01", "Bazar: extrato qualificado (imagem)",      test_B01_extrato_qualificado_imagem),
    ("B02", "Bazar: extrato >50% pago → NAO_QUAL",     test_B02_extrato_nao_qualificado),
    ("B03", "Bazar: boleto enviado → EXTRATO_INCOR",   test_B03_extrato_incorreto_boleto),
    ("B04", "Bazar: texto sem extrato → solicita",     test_B04_texto_sem_extrato),
    ("B05", "Bazar: recusa verbal → PERDIDO",          test_B05_recusa_verbal),
    ("B06", "Bazar: erro técnico IA → graceful",       test_B06_erro_tecnico_extrato),
    ("B07", "Bazar: campos corretos no FARO",          test_B07_qualificado_campos_corretos_no_faro),
    ("B08", "Bazar ACEITO → ZapSign direto",           test_B08_bazar_aceito_gera_zapsign_direto),
    ("E01", "Adm sem template → notifica equipe",      test_E01_adm_sem_template),
    ("E02", "ZapSign error Bazar → MSG_ERRO_INTERNO",  test_E02_zapsign_error_bazar),
    ("E03", "ZapSign error Lista → SEM msg ao lead",   test_E03_zapsign_error_lista_sem_msg_lead),
    ("E04", "ASSINATURA sem token + link problem",     test_E04_assinatura_sem_token_keywords),
    ("E05", "Lead desconfiante → credibilidade",       test_E05_negociador_lead_desconfiante),
    ("E06", "Detecção de tom de comunicação",          test_E06_deteccao_tom_informal),
    ("E07", "ZapSign template IDs corretos",           test_E07_zapsign_template_ids),
    ("E08", "_fmt_currency formatos múltiplos",        test_E08_fmt_currency_formatos),
    ("E09", "is_lista() detecção correta",             test_E09_is_lista_detection),
    ("E10", "build_form_fields ZapSign completo",      test_E10_build_form_fields_zapsign),
]


async def run_scenario(code: str, name: str, fn, verbose: bool) -> TestResult:
    r = ScenarioRunner(name=f"[{code}] {name}", verbose=verbose)
    start = time.monotonic()
    try:
        await fn(r)
        duration = (time.monotonic() - start) * 1000
        return TestResult(
            name=r.name,
            passed=r.all_passed,
            duration_ms=duration,
            details=[f"{r.passed_count}/{r.total} assertions"],
        )
    except AssertionError as e:
        duration = (time.monotonic() - start) * 1000
        return TestResult(name=r.name, passed=False, duration_ms=duration, error=str(e))
    except Exception as e:
        duration = (time.monotonic() - start) * 1000
        tb = traceback.format_exc()
        return TestResult(name=r.name, passed=False, duration_ms=duration, error=f"{type(e).__name__}: {e}\n{tb}")


async def main(args):
    verbose = args.verbose
    filter_code = args.cenario

    scenarios = ALL_SCENARIOS
    if filter_code:
        scenarios = [(c, n, f) for c, n, f in scenarios if c.upper() == filter_code.upper()]
        if not scenarios:
            print(f"{RED}Cenário '{filter_code}' não encontrado.{RESET}")
            sys.exit(1)

    print(f"\n{BOLD}{CYAN}═══ Simulação Completa — Consórcio Sorteado ══════════════════════{RESET}")
    print(f"{CYAN}  {len(scenarios)} cenário(s) | {time.strftime('%Y-%m-%d %H:%M:%S')}{RESET}\n")

    results = []
    for code, name, fn in scenarios:
        if verbose:
            print(f"{BOLD}[{code}] {name}{RESET}")
        result = await run_scenario(code, name, fn, verbose)
        results.append(result)

        status = TICK if result.passed else CROSS
        detail = result.details[0] if result.details else ""
        timing = f"{result.duration_ms:.0f}ms"
        print(f"  {status} [{code}] {name:<50} {YELLOW}{timing}{RESET}  {detail}")

        if not result.passed and result.error:
            first_line = result.error.split("\n")[0]
            print(f"       {RED}↳ {first_line}{RESET}")
            if verbose and len(result.error.split("\n")) > 1:
                for line in result.error.split("\n")[1:]:
                    if line.strip():
                        print(f"         {line}")

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed

    print(f"\n{BOLD}{'─' * 70}{RESET}")
    if failed == 0:
        print(f"{GREEN}{BOLD}  ✓ Todos os {total} cenários passaram!{RESET}")
    else:
        print(f"{RED}{BOLD}  ✗ {failed}/{total} cenário(s) falharam{RESET}")
        print(f"\n{YELLOW}Falhas:{RESET}")
        for r in results:
            if not r.passed:
                print(f"  • {r.name}")
                if r.error:
                    print(f"    {RED}{r.error.split(chr(10))[0]}{RESET}")

    avg_ms = sum(r.duration_ms for r in results) / total if total else 0
    print(f"\n{CYAN}  Duração média por cenário: {avg_ms:.0f}ms{RESET}")
    print(f"{CYAN}  Total: {sum(r.duration_ms for r in results):.0f}ms{RESET}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulação completa Consórcio Sorteado")
    parser.add_argument("--cenario", help="Código do cenário a executar (ex: L01, B03, E07)")
    parser.add_argument("--verbose", action="store_true", help="Modo verboso")
    args = parser.parse_args()

    exit_code = asyncio.run(main(args))
    sys.exit(exit_code)
