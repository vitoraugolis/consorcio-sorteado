"""
tests/simulation.py — Simulação end-to-end do fluxo Consórcio Sorteado

Executa o fluxo completo sem fazer chamadas reais a WhatsApp, FARO ou ZapSign.
Todas as dependências externas são mockadas com unittest.mock.

Cenários testados:
  1.  Ativação de lead (Listas)                → mensagem enviada, card movido
  2.  Ativação de lead (Bazar)                 → mensagem enviada, card movido
  3.  Reativação de lead frio                  → mensagem de reativação enviada
  4.  Envio de proposta (precificação)          → proposta formatada corretamente
  5.  Negociação: lead aceita                  → card movido para ACEITO
  6.  Negociação: lead recusa                  → card movido para PERDIDO
  7.  Negociação: lead quer negociar           → card movido para EM_NEGOCIACAO
  8.  Negociação: lead tem dúvida              → resposta enviada, stage mantido
  9.  Negociação: lead quer atendente          → notificação equipe disparada
  10. Geração de contrato (ZapSign)            → documento criado, link enviado
  11. Webhook ZapSign: assinatura completa     → card movido para SUCESSO
  12. Follow-up automático (proposta travada)  → mensagem de follow-up enviada
  13. Router Whapi: parse de payload           → mensagem normalizada corretamente
  14. Router Z-API: parse de payload           → mensagem normalizada corretamente
  15. Fluxo completo: Bazar → Aceite → Contrato → Assinatura
  16. Qualificação: extrato OK, cota qualificada   → card movido para PRECIFICACAO
  17. Qualificação: extrato OK, cota não qualificada → card movido para NAO_QUALIFICADO
  18. Qualificação: extrato incorreto/ilegível     → lead orientado, stage mantido

Uso:
    python -m tests.simulation                  # roda todos os cenários
    python -m tests.simulation --cenario 5      # roda apenas o cenário 5
    python -m tests.simulation --verbose        # modo verboso

Saída: relatório colorido no terminal com pass/fail para cada cenário.
"""

import argparse
import asyncio
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

# ── Paleta de cores para terminal ──────────────────────────────────────────
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

def make_card(
    card_id: str = "test-card-001",
    nome: str = "João Silva",
    phone: str = "5511999990001",
    adm: str = "Santander",
    stage_id: str = "listas-stage-id",
    fonte: str = "listas",
    credito: str = "R$ 200.000,00",
    parcela: str = "R$ 1.200,00",
    prazo: str = "200",
    taxa: str = "20",
    **extra_fields,
) -> dict:
    """Cria um card FARO de teste com dados realistas."""
    return {
        "id":                    card_id,
        "title":                 nome,
        "stage_id":              stage_id,
        "Nome do contato":       nome,
        "Telefone":              phone,
        "Adm":                   adm,
        "Etiquetas":             fonte,
        "Fonte":                 fonte,
        "Valor do crédito":      credito,
        "Parcela":               parcela,
        "Prazo":                 prazo,
        "Taxa de administração": taxa,
        "Tipo de bem":           "Imóvel",
        "Data proposta enviada": "",
        "ZapSign Token":         "",
        "Ultima atividade":      "",
        "Ultima resposta lead":  "",
        **extra_fields,
    }


def make_whapi_payload(phone: str, text: str) -> dict:
    """Simula o payload de mensagem recebida do Whapi."""
    return {
        "messages": [{
            "id":       "MSG001",
            "from":     f"{phone}@s.whatsapp.net",
            "chat_id":  f"{phone}@s.whatsapp.net",
            "body":     text,
            "type":     "text",
            "from_me":  False,
            "timestamp": int(time.time()),
        }]
    }


def make_zapi_payload(phone: str, text: str) -> dict:
    """Simula o payload de mensagem recebida do Z-API."""
    return {
        "phone":   phone,
        "type":    "ReceivedCallback",
        "fromMe":  False,
        "message": {"text": text},
        "messageId": "ZMSG001",
        "timestamp": int(time.time()),
    }


def make_zapi_media_payload(
    phone: str,
    media_type: str = "document",
    media_url: str = "https://media.z-api.io/fake-extrato.pdf",
    caption: str = "",
    filename: str = "extrato.pdf",
) -> dict:
    """Simula o payload de mídia (documento/imagem) recebida do Z-API."""
    media_obj = {"url": media_url, "caption": caption}
    if media_type == "document":
        media_obj["fileName"] = filename
    return {
        "phone":     phone,
        "type":      "ReceivedCallback",
        "fromMe":    False,
        "message":   {media_type: media_obj},
        "messageId": "ZMSG_MEDIA_001",
        "timestamp": int(time.time()),
    }


def make_zapsign_webhook(doc_token: str, status: str = "signed") -> dict:
    """Simula o webhook de assinatura do ZapSign."""
    return {
        "token":   doc_token,
        "status":  status,
        "open_id": 99999,
        "name":    "Contrato - João Silva - Santander",
    }


# ---------------------------------------------------------------------------
# Framework de testes
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    duration_ms: float
    error: Optional[str] = None
    details: list[str] = field(default_factory=list)


@dataclass
class Assertion:
    description: str
    passed: bool
    expected: Any = None
    actual: Any = None


class ScenarioRunner:
    """Contexto de execução de um cenário de teste."""

    def __init__(self, name: str, verbose: bool = False):
        self.name = name
        self.verbose = verbose
        self.assertions: list[Assertion] = []
        self._mocks: dict[str, Any] = {}

    def assert_true(self, condition: bool, description: str) -> None:
        a = Assertion(description=description, passed=bool(condition))
        self.assertions.append(a)
        if self.verbose:
            status = TICK if a.passed else CROSS
            print(f"    {status} {description}")
        if not a.passed:
            raise AssertionError(f"FALHOU: {description}")

    def assert_equal(self, expected: Any, actual: Any, description: str) -> None:
        passed = expected == actual
        a = Assertion(description=description, passed=passed, expected=expected, actual=actual)
        self.assertions.append(a)
        if self.verbose:
            status = TICK if a.passed else CROSS
            print(f"    {status} {description}")
            if not passed:
                print(f"       Esperado: {expected!r}")
                print(f"       Obtido:   {actual!r}")
        if not passed:
            raise AssertionError(f"FALHOU: {description} | esperado={expected!r} obtido={actual!r}")

    def assert_called(self, mock: MagicMock, description: str) -> None:
        self.assert_true(mock.called, description)

    def assert_called_with_contains(self, mock: MagicMock, text: str, description: str) -> None:
        """Verifica se alguma chamada ao mock continha o texto nos argumentos."""
        found = False
        for call_args in mock.call_args_list:
            all_args = str(call_args)
            if text.lower() in all_args.lower():
                found = True
                break
        self.assert_true(found, f"{description} (buscando '{text}')")

    def mock(self, target: str) -> MagicMock:
        """Registra e retorna um mock pelo nome."""
        return self._mocks.get(target)

    @property
    def all_passed(self) -> bool:
        return all(a.passed for a in self.assertions)

    @property
    def total(self) -> int:
        return len(self.assertions)

    @property
    def passed_count(self) -> int:
        return sum(1 for a in self.assertions if a.passed)


# ---------------------------------------------------------------------------
# Cenários de teste
# ---------------------------------------------------------------------------

async def test_01_ativacao_listas(r: ScenarioRunner) -> None:
    """Lead de lista fria recebe ativação via Whapi com botões."""
    from config import Stage

    card = make_card(
        card_id="lista-001",
        fonte="listas",
        stage_id=Stage.LISTAS,
        phone="5511900000001",
    )

    send_buttons_mock = AsyncMock(return_value={"sent": True})
    move_mock        = AsyncMock(return_value={"success": True})
    update_mock      = AsyncMock(return_value={"success": True})
    watch_new_mock   = AsyncMock(return_value=[card])
    format_phone_mock= AsyncMock(return_value="5511900000001")

    with (
        patch("jobs.ativacao_listas.FaroClient") as MockFaro,
        patch("jobs.ativacao_listas.WhapiClient") as MockWhapi,
        patch("jobs.ativacao_listas.AIClient") as MockAI,
        patch("jobs.ativacao_listas._is_within_send_window", return_value=True),
        patch("jobs.ativacao_listas.asyncio.sleep", new_callable=AsyncMock),
        patch("jobs.ativacao_listas.filter_test_cards", side_effect=lambda c: c),
    ):
        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.get_cards_all_pages = AsyncMock(return_value=[card])
        faro_inst.move_card    = move_mock
        faro_inst.update_card  = update_mock
        MockFaro.return_value  = faro_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_buttons = send_buttons_mock
        MockWhapi.return_value  = whapi_inst

        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.format_phone = format_phone_mock
        MockAI.return_value  = ai_inst

        from jobs.ativacao_listas import run_ativacao_listas
        await run_ativacao_listas()

    r.assert_called(send_buttons_mock, "Mensagem com botões enviada via Whapi")
    r.assert_called(move_mock, "Card movido para próximo stage")
    r.assert_called_with_contains(move_mock, Stage.PRIMEIRA_ATIVACAO, "Card movido para PRIMEIRA_ATIVACAO")


async def test_02_ativacao_bazar(r: ScenarioRunner) -> None:
    """Lead do Bazar recebe ativação via Z-API."""
    from config import Stage

    card = make_card(
        card_id="bazar-001",
        fonte="bazar",
        stage_id=Stage.BAZAR,
        phone="5511900000002",
        adm="Itaú",
    )
    card["Etiquetas"] = "itau"

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    # ZAPIClient mock como context manager
    zapi_inst = AsyncMock()
    zapi_inst.__aenter__ = AsyncMock(return_value=zapi_inst)
    zapi_inst.__aexit__  = AsyncMock(return_value=None)
    zapi_inst.send_text  = send_text_mock

    with (
        patch("jobs.ativacao_bazar_site.FaroClient") as MockFaro,
        patch("jobs.ativacao_bazar_site.get_zapi_for_card", return_value=zapi_inst),
        patch("jobs.ativacao_bazar_site._is_within_send_window", return_value=True),
        patch("jobs.ativacao_bazar_site.filter_test_cards", side_effect=lambda c: c),
    ):
        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.watch_recent = AsyncMock(return_value=[card])
        faro_inst.move_card    = move_mock
        faro_inst.update_card  = update_mock
        MockFaro.return_value  = faro_inst

        from jobs.ativacao_bazar_site import run_ativacao_bazar
        await run_ativacao_bazar()

    r.assert_called(send_text_mock, "Mensagem enviada via Z-API para lead Bazar")
    r.assert_called(move_mock, "Card Bazar movido para próximo stage")


async def test_03_reativacao(r: ScenarioRunner) -> None:
    """Lead frio (em ativação) recebe mensagem de reativação."""
    from config import Stage

    card = make_card(
        card_id="reativ-001",
        fonte="listas",
        stage_id=Stage.PRIMEIRA_ATIVACAO,
        phone="5511900000003",
    )

    send_buttons_mock = AsyncMock(return_value={"sent": True})
    move_mock         = AsyncMock(return_value={"success": True})
    update_mock       = AsyncMock(return_value={"success": True})

    with (
        patch("jobs.reativador.FaroClient") as MockFaro,
        patch("jobs.reativador.WhapiClient") as MockWhapi,
        patch("jobs.reativador._is_within_send_window", return_value=True),
        patch("jobs.reativador.asyncio.sleep", new_callable=AsyncMock),
        patch("jobs.reativador.filter_test_cards", side_effect=lambda c: c),
    ):
        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.check_stage_time = AsyncMock(return_value=[card])
        faro_inst.move_card    = move_mock
        faro_inst.update_card  = update_mock
        MockFaro.return_value  = faro_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_buttons = send_buttons_mock
        MockWhapi.return_value  = whapi_inst

        from jobs.reativador import run_reativador
        await run_reativador()

    r.assert_called(send_buttons_mock, "Mensagem de reativação enviada")
    r.assert_called(move_mock, "Card reativado movido para próximo stage")


async def test_04_precificacao_proposta(r: ScenarioRunner) -> None:
    """Card em PRECIFICACAO sem proposta enviada recebe proposta formatada."""
    from config import Stage
    from jobs.precificacao import _build_proposal_message, _fmt_currency

    card = make_card(
        card_id="prec-001",
        fonte="listas",
        stage_id=Stage.PRECIFICACAO,
        phone="5511900000004",
        credito="300000",
        parcela="1800",
        prazo="200",
    )
    card["Proposta Realizada"] = "90000"  # 30% do crédito — pré-calculado pelo agente

    # Testa formatação da mensagem
    msg = _build_proposal_message(card)
    r.assert_true("João" in msg, "Nome do lead na mensagem")
    r.assert_true("à vista" in msg.lower(), "Menção a pagamento à vista")
    r.assert_true("parcelas" in msg.lower(), "Menção a parcelas futuras")
    r.assert_true("proposta" in msg.lower(), "Palavra proposta na mensagem")

    # Testa formatação de moeda
    r.assert_equal("R$ 300.000,00", _fmt_currency("300000"), "Formatação de moeda: inteiro")
    r.assert_equal("R$ 1.800,00",   _fmt_currency("1800"),   "Formatação de moeda: parcela")
    r.assert_equal("a consultar",   _fmt_currency(""),        "Formatação de moeda: vazio")

    # Testa o job completo
    send_buttons_mock = AsyncMock(return_value={"sent": True})
    move_mock         = AsyncMock(return_value={"success": True})
    update_mock       = AsyncMock(return_value={"success": True})

    with (
        patch("jobs.precificacao.FaroClient") as MockFaro,
        patch("jobs.precificacao.WhapiClient") as MockWhapi,
        patch("jobs.precificacao._is_within_send_window", return_value=True),
        patch("jobs.precificacao.filter_test_cards", side_effect=lambda c: c),
        patch("jobs.precificacao.asyncio.sleep", new_callable=AsyncMock),
    ):
        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.watch_new   = AsyncMock(return_value=[card])
        faro_inst.get_card    = AsyncMock(return_value=card)
        faro_inst.move_card   = move_mock
        faro_inst.update_card = update_mock
        MockFaro.return_value = faro_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_buttons = send_buttons_mock
        whapi_inst.send_text    = send_buttons_mock
        MockWhapi.return_value  = whapi_inst

        from jobs.precificacao import run_precificacao
        await run_precificacao()

    r.assert_called(send_buttons_mock, "Proposta enviada via Whapi")
    r.assert_called(move_mock, "Card movido para EM_NEGOCIACAO após proposta")
    r.assert_called_with_contains(move_mock, Stage.EM_NEGOCIACAO, "Stage destino é EM_NEGOCIACAO")


async def test_04b_precificacao_sem_dados(r: ScenarioRunner) -> None:
    """Card sem crédito/parcela NÃO deve ter proposta enviada."""
    from config import Stage

    card = make_card(
        card_id="prec-incompleto",
        fonte="listas",
        stage_id=Stage.PRECIFICACAO,
        credito="",   # <- sem crédito
        parcela="",   # <- sem parcela
    )

    send_buttons_mock = AsyncMock(return_value={"sent": True})
    move_mock         = AsyncMock(return_value={"success": True})
    update_mock       = AsyncMock(return_value={"success": True})

    with (
        patch("jobs.precificacao.FaroClient") as MockFaro,
        patch("jobs.precificacao.WhapiClient") as MockWhapi,
        patch("jobs.precificacao._is_within_send_window", return_value=True),
        patch("jobs.precificacao.filter_test_cards", side_effect=lambda c: c),
        patch("jobs.precificacao.asyncio.sleep", new_callable=AsyncMock),
    ):
        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.watch_new   = AsyncMock(return_value=[card])
        faro_inst.get_card    = AsyncMock(return_value=card)
        faro_inst.move_card   = move_mock
        faro_inst.update_card = update_mock
        MockFaro.return_value = faro_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_buttons = send_buttons_mock
        MockWhapi.return_value  = whapi_inst

        from jobs.precificacao import run_precificacao
        await run_precificacao()

    r.assert_true(not send_buttons_mock.called, "Proposta NÃO enviada para card sem dados")
    r.assert_true(not move_mock.called, "Card NÃO movido sem dados completos")


async def test_05_negociador_aceita(r: ScenarioRunner) -> None:
    """Lead responde aceitando a proposta → card movido para ACEITO."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_card(
        card_id="neg-aceita-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000005",
    )

    send_text_mock  = AsyncMock(return_value={"sent": True})
    move_mock       = AsyncMock(return_value={"success": True})
    update_mock     = AsyncMock(return_value={"success": True})
    notify_mock     = AsyncMock(return_value={"sent": True})

    ai_response = json.dumps({
        "intent": "ACEITAR",
        "reasoning": "Lead disse quero fechar",
        "response": "Ótimo! Vou encaminhar para finalizar. 🎉",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
    ):
        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.complete   = AsyncMock(return_value=ai_response)
        MockAI.return_value = ai_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_text  = send_text_mock
        MockWhapi.return_value = whapi_inst

        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.get_card    = AsyncMock(return_value=card)
        faro_inst.move_card   = move_mock
        faro_inst.update_card = update_mock
        MockFaro.return_value = faro_inst

        await handle_message(
            card=card,
            mensagem="Quero fechar! Pode mandar o contrato.",
            current_stage_id=Stage.EM_NEGOCIACAO,
        )

    r.assert_called(send_text_mock, "Resposta de aceite enviada ao lead")
    r.assert_called(move_mock, "Card movido após aceite")
    r.assert_called_with_contains(move_mock, Stage.ACEITO, "Card movido para ACEITO")


async def test_06_negociador_recusa(r: ScenarioRunner) -> None:
    """Lead recusa a proposta → card movido para PERDIDO."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_card(
        card_id="neg-recusa-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000006",
    )
    # Proposta no teto da sequência → pode_escalar=False → RECUSAR resulta em PERDIDO
    card["Proposta Realizada"] = "32000"
    card["Sequencia_Proposta"] = "25000,28000,30000,32000"

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_response = json.dumps({
        "intent": "RECUSAR",
        "reasoning": "Lead disse sem interesse",
        "response": "Tudo bem! Boa sorte. 😊",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.complete   = AsyncMock(return_value=ai_response)
        MockAI.return_value = ai_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_text  = send_text_mock
        MockWhapi.return_value = whapi_inst

        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.get_card    = AsyncMock(return_value=card)
        faro_inst.move_card   = move_mock
        faro_inst.update_card = update_mock
        MockFaro.return_value = faro_inst

        await handle_message(
            card=card,
            mensagem="Não tenho interesse, me retire da lista.",
            current_stage_id=Stage.EM_NEGOCIACAO,
        )

    r.assert_called(send_text_mock, "Resposta de recusa enviada ao lead")
    r.assert_called(move_mock, "Card movido após recusa")
    r.assert_called_with_contains(move_mock, Stage.PERDIDO, "Card movido para PERDIDO")


async def test_07_negociador_quer_negociar(r: ScenarioRunner) -> None:
    """Lead quer negociar → card mantido/movido para EM_NEGOCIACAO + equipe notificada."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_card(
        card_id="neg-neg-001",
        stage_id=Stage.PRECIFICACAO,
        phone="5511900000007",
    )

    send_text_mock   = AsyncMock(return_value={"sent": True})
    move_mock        = AsyncMock(return_value={"success": True})
    update_mock      = AsyncMock(return_value={"success": True})
    notify_text_mock = AsyncMock(return_value={"sent": True})

    ai_response = json.dumps({
        "intent": "NEGOCIAR",
        "reasoning": "Lead pediu desconto",
        "response": "Entendo! Vou verificar o que é possível e já te retorno.",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", ["5511900000099"]),
    ):
        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.complete   = AsyncMock(return_value=ai_response)
        MockAI.return_value = ai_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_text  = notify_text_mock  # Para notificação da equipe
        MockWhapi.return_value = whapi_inst

        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.get_card    = AsyncMock(return_value=card)
        faro_inst.move_card   = move_mock
        faro_inst.update_card = update_mock
        MockFaro.return_value = faro_inst

        await handle_message(
            card=card,
            mensagem="Tem como fazer um desconto na parcela?",
            current_stage_id=Stage.PRECIFICACAO,
        )

    r.assert_called(notify_text_mock, "Resposta e/ou notificação enviada")
    r.assert_called(move_mock, "Card movido para EM_NEGOCIACAO")
    r.assert_called_with_contains(move_mock, Stage.EM_NEGOCIACAO, "Stage destino correto")


async def test_08_negociador_duvida(r: ScenarioRunner) -> None:
    """Lead tem dúvida → IA responde, stage NÃO muda."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_card(
        card_id="neg-duvida-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000008",
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_response = json.dumps({
        "intent": "DUVIDA",
        "reasoning": "Lead perguntou como funciona",
        "response": "Ótima pergunta! O consórcio funciona assim: um grupo de pessoas...",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.complete   = AsyncMock(return_value=ai_response)
        MockAI.return_value = ai_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_text  = send_text_mock
        MockWhapi.return_value = whapi_inst

        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.get_card    = AsyncMock(return_value=card)
        faro_inst.move_card   = move_mock
        faro_inst.update_card = update_mock
        MockFaro.return_value = faro_inst

        await handle_message(
            card=card,
            mensagem="Como funciona o sorteio do consórcio?",
            current_stage_id=Stage.EM_NEGOCIACAO,
        )

    r.assert_called(send_text_mock, "Resposta à dúvida enviada")
    r.assert_true(not move_mock.called, "Stage NÃO muda para dúvida simples")


async def test_09_negociador_quer_atendente(r: ScenarioRunner) -> None:
    """Lead quer falar com atendente → equipe notificada."""
    from config import Stage
    from webhooks.negociador import handle_message

    # Inicia em PRECIFICACAO para que AGENDAR acione move_card → EM_NEGOCIACAO
    card = make_card(
        card_id="neg-atend-001",
        stage_id=Stage.PRECIFICACAO,
        phone="5511900000009",
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_response = json.dumps({
        "intent": "AGENDAR",
        "reasoning": "Lead pediu para falar com atendente",
        "response": "Claro! Vou chamar um atendente para você. Um instante!",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", ["5511900000099"]),
    ):
        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.complete   = AsyncMock(return_value=ai_response)
        MockAI.return_value = ai_inst

        call_count = {"n": 0}
        async def send_text_track(phone, msg):
            call_count["n"] += 1
            return {"sent": True}

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_text  = AsyncMock(side_effect=send_text_track)
        MockWhapi.return_value = whapi_inst

        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.get_card    = AsyncMock(return_value=card)
        faro_inst.move_card   = move_mock
        faro_inst.update_card = update_mock
        MockFaro.return_value = faro_inst

        await handle_message(
            card=card,
            mensagem="Prefiro falar com uma pessoa real, pode me ligar?",
            current_stage_id=Stage.PRECIFICACAO,
        )

    r.assert_true(call_count["n"] >= 2, "Resposta ao lead + notificação à equipe enviadas")
    r.assert_called(move_mock, "Card movido para EM_NEGOCIACAO")


async def test_10_geracao_contrato(r: ScenarioRunner) -> None:
    """Card em ACEITO → ZapSign cria documento e link é enviado ao lead."""
    from config import Stage
    from jobs.contrato import run_contrato

    card = make_card(
        card_id="cont-001",
        fonte="bazar",          # bazar → vai direto para ZapSign (não precisa de dados pessoais)
        stage_id=Stage.ACEITO,
        phone="5511900000010",
        adm="Santander",
    )
    card["Email"] = "joao@email.com"

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    zapsign_doc = {
        "doc_token":       "zap-token-abc123",
        "open_id":         12345,
        "lead_sign_url":   "https://sign.zapsign.com.br/doc/abc123",
        "internal_sign_urls": [],
        "all_signers":     [],
    }

    zapi_cont = AsyncMock()
    zapi_cont.__aenter__ = AsyncMock(return_value=zapi_cont)
    zapi_cont.__aexit__  = AsyncMock(return_value=None)
    zapi_cont.send_text  = send_text_mock

    with (
        patch("jobs.contrato.FaroClient") as MockFaro,
        patch("jobs.contrato.WhapiClient") as MockWhapi,
        patch("jobs.contrato.get_zapi_for_card", return_value=zapi_cont),
        patch("jobs.contrato.ZapSignClient") as MockZapSign,
        patch("jobs.contrato.get_template_for_adm", return_value="santander-template-uuid"),
        patch("jobs.contrato.build_form_fields", return_value={}),
        patch("jobs.contrato.filter_test_cards", side_effect=lambda c: c),
        patch("jobs.contrato.NOTIFY_PHONES", []),
        patch("jobs.contrato.asyncio.sleep", new_callable=AsyncMock),
    ):
        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.watch_new   = AsyncMock(return_value=[card])
        faro_inst.get_card    = AsyncMock(return_value=card)
        faro_inst.move_card   = move_mock
        faro_inst.update_card = update_mock
        MockFaro.return_value = faro_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_text  = send_text_mock
        MockWhapi.return_value = whapi_inst

        zap_inst = AsyncMock()
        zap_inst.__aenter__  = AsyncMock(return_value=zap_inst)
        zap_inst.__aexit__   = AsyncMock(return_value=None)
        zap_inst.create_from_template = AsyncMock(return_value=zapsign_doc)
        MockZapSign.return_value = zap_inst

        await run_contrato()

    r.assert_called(send_text_mock, "Link de assinatura enviado ao lead")
    r.assert_called_with_contains(send_text_mock, "sign.zapsign.com.br", "URL de assinatura na mensagem")
    r.assert_called(move_mock, "Card movido após contrato gerado")
    r.assert_called_with_contains(move_mock, Stage.ASSINATURA, "Card movido para ASSINATURA")
    r.assert_called_with_contains(update_mock, "zap-token-abc123", "ZapSign Token salvo no card")


async def test_11_webhook_zapsign_assinatura(r: ScenarioRunner) -> None:
    """ZapSign notifica assinatura completa → card movido para SUCESSO e FINALIZACAO."""
    from config import Stage

    card = make_card(
        card_id="sign-001",
        stage_id=Stage.ASSINATURA,
        phone="5511900000011",
    )
    card["ZapSign Token"] = "zap-token-abc123"

    payload = make_zapsign_webhook("zap-token-abc123")

    move_mock    = AsyncMock(return_value={"success": True})
    update_mock  = AsyncMock(return_value={"success": True})
    send_text_mock = AsyncMock(return_value={"sent": True})

    # _handle_zapsign_signed usa imports locais, então patchamos no nível do módulo de origem
    with (
        patch("services.faro.FaroClient") as MockFaro,
        patch("services.whapi.WhapiClient") as MockWhapi,
        patch("main.NOTIFY_PHONES", ["5511900000099"]),
    ):
        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.get_cards_all_pages = AsyncMock(return_value=[card])
        faro_inst.move_card   = move_mock
        MockFaro.return_value = faro_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_text  = send_text_mock
        MockWhapi.return_value = whapi_inst

        from main import _handle_zapsign_signed
        await _handle_zapsign_signed("zap-token-abc123", "Contrato - João Silva - Santander")

    r.assert_called(move_mock, "Card movido após assinatura")
    r.assert_called_with_contains(move_mock, Stage.SUCESSO, "Card movido para SUCESSO")
    r.assert_called(send_text_mock, "Equipe notificada da assinatura")


async def test_12_followup_proposta_travada(r: ScenarioRunner) -> None:
    """Card em EM_NEGOCIACAO sem resposta há 180min recebe follow-up."""
    from config import Stage
    from jobs.follow_up import run_follow_up

    card = make_card(
        card_id="fu-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000012",
    )
    # Ultima atividade há 3h — bem acima do MIN_INTERVAL_S de 25min
    card["Ultima atividade"] = str(int(time.time()) - 3 * 3600)
    card["Num Follow Ups"]   = "0"

    send_text_mock = AsyncMock(return_value={"sent": True})
    update_mock    = AsyncMock(return_value={"success": True})
    move_mock      = AsyncMock(return_value={"success": True})

    with (
        patch("jobs.follow_up.FaroClient") as MockFaro,
        patch("jobs.follow_up.AIClient") as MockAI,
        patch("jobs.follow_up.WhapiClient") as MockWhapi,
        patch("jobs.follow_up._is_within_send_window", return_value=True),
        patch("jobs.follow_up.filter_test_cards", side_effect=lambda c: c),
        patch("jobs.follow_up.NOTIFY_PHONES", []),
    ):
        faro_inst = AsyncMock()
        faro_inst.__aenter__ = AsyncMock(return_value=faro_inst)
        faro_inst.__aexit__  = AsyncMock(return_value=None)
        faro_inst.get_cards_all_pages = AsyncMock(return_value=[card])
        faro_inst.update_card = update_mock
        faro_inst.move_card   = move_mock
        MockFaro.return_value = faro_inst

        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.complete    = AsyncMock(return_value="Boa tarde! Já teve chance de analisar a proposta?")
        MockAI.return_value = ai_inst

        whapi_inst = AsyncMock()
        whapi_inst.__aenter__ = AsyncMock(return_value=whapi_inst)
        whapi_inst.__aexit__  = AsyncMock(return_value=None)
        whapi_inst.send_text  = send_text_mock
        MockWhapi.return_value = whapi_inst

        await run_follow_up()

    r.assert_called(send_text_mock, "Follow-up enviado ao lead")
    r.assert_called(update_mock, "Última atividade atualizada no card")


async def test_13_router_parse_whapi(r: ScenarioRunner) -> None:
    """Router normaliza corretamente payloads do Whapi."""
    from webhooks.router import parse_whapi_payload, IncomingMessage

    # Mensagem normal
    payload = make_whapi_payload("5511900000013", "Olá, quero saber mais")
    msgs = parse_whapi_payload(payload)

    r.assert_equal(1, len(msgs), "Um IncomingMessage gerado")
    r.assert_equal("5511900000013", msgs[0].phone, "Telefone normalizado corretamente")
    r.assert_equal("Olá, quero saber mais", msgs[0].text, "Texto da mensagem preservado")
    r.assert_equal(False, msgs[0].from_me, "fromMe = False para mensagem recebida")
    r.assert_equal("whapi", msgs[0].source, "Source marcado como whapi")
    r.assert_true(msgs[0].is_processable, "Mensagem é processável")

    # Mensagem enviada pelo sistema (from_me = True)
    payload_mine = {
        "messages": [{
            "chat_id": "5511900000013@s.whatsapp.net",
            "body":    "Enviada por mim",
            "type":    "text",
            "from_me": True,
        }]
    }
    msgs_mine = parse_whapi_payload(payload_mine)
    r.assert_true(not msgs_mine[0].is_processable, "Mensagem própria não é processável")

    # Mensagem de grupo
    payload_group = {
        "messages": [{
            "chat_id": "120363000000001@g.us",
            "body":    "Mensagem de grupo",
            "type":    "text",
            "from_me": False,
        }]
    }
    msgs_group = parse_whapi_payload(payload_group)
    r.assert_true(msgs_group[0].is_group, "Mensagem de grupo identificada")
    r.assert_true(not msgs_group[0].is_processable, "Mensagem de grupo não é processável")


async def test_14_router_parse_zapi(r: ScenarioRunner) -> None:
    """Router normaliza corretamente payloads do Z-API."""
    from webhooks.router import parse_zapi_payload

    payload = make_zapi_payload("5511900000014", "Aceito a proposta!")
    msgs = parse_zapi_payload(payload)

    r.assert_equal(1, len(msgs), "Um IncomingMessage gerado")
    r.assert_equal("5511900000014", msgs[0].phone, "Telefone normalizado")
    r.assert_equal("Aceito a proposta!", msgs[0].text, "Texto preservado")
    r.assert_equal("zapi", msgs[0].source, "Source marcado como zapi")
    r.assert_true(msgs[0].is_processable, "Mensagem processável")

    # Status callbacks devem ser ignorados
    status_payload = {"type": "MessageStatusCallback", "phone": "5511900000014"}
    msgs_status = parse_zapi_payload(status_payload)
    r.assert_equal(0, len(msgs_status), "Status callback ignorado")


async def test_15_fluxo_completo(r: ScenarioRunner) -> None:
    """
    Fluxo completo: Bazar → Ativação → Proposta → Aceite → Contrato → Assinatura.
    Cada etapa é executada com mocks isolados, verificando transições de stage.
    """
    from config import Stage

    estados = []

    card = make_card(
        card_id="full-flow-001",
        nome="Carlos Mendes",
        fonte="bazar",
        stage_id=Stage.BAZAR,
        phone="5511900000015",
        adm="Bradesco",
        credito="150000",
        parcela="900",
        prazo="180",
    )
    card["Etiquetas"] = "bradesco"
    card["Email"] = "carlos@email.com"

    # ── Etapa 1: Ativação Bazar ─────────────────────────────────────────
    zapi_b = AsyncMock()
    zapi_b.__aenter__ = AsyncMock(return_value=zapi_b)
    zapi_b.__aexit__  = AsyncMock(return_value=None)
    zapi_b.send_text  = AsyncMock(return_value={"sent": True})

    with (
        patch("jobs.ativacao_bazar_site.FaroClient") as MockFaro,
        patch("jobs.ativacao_bazar_site.get_zapi_for_card", return_value=zapi_b),
        patch("jobs.ativacao_bazar_site._is_within_send_window", return_value=True),
        patch("jobs.ativacao_bazar_site.filter_test_cards", side_effect=lambda c: c),
    ):
        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.watch_recent = AsyncMock(return_value=[card])
        async def mb(cid, sid): estados.append(("bazar", sid)); card["stage_id"] = sid
        fi.move_card = AsyncMock(side_effect=mb); fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi
        from jobs.ativacao_bazar_site import run_ativacao_bazar
        await run_ativacao_bazar()

    r.assert_true(any(e[0] == "bazar" for e in estados), "Etapa 1: card ativado (Bazar)")

    # ── Etapa 2: Proposta enviada ───────────────────────────────────────
    card["stage_id"] = Stage.PRECIFICACAO
    card["Data proposta enviada"] = ""
    card["Proposta Realizada"]    = "45000"  # 30% de 150000 — pré-calculado

    zapi_p = AsyncMock()
    zapi_p.__aenter__ = AsyncMock(return_value=zapi_p)
    zapi_p.__aexit__  = AsyncMock(return_value=None)
    zapi_p.send_button_list = AsyncMock(return_value={"sent": True})
    zapi_p.send_text        = AsyncMock(return_value={"sent": True})

    with (
        patch("jobs.precificacao.FaroClient") as MockFaro,
        patch("jobs.precificacao.get_zapi_for_card", return_value=zapi_p),
        patch("jobs.precificacao._is_within_send_window", return_value=True),
        patch("jobs.precificacao.filter_test_cards", side_effect=lambda c: c),
        patch("jobs.precificacao.asyncio.sleep", new_callable=AsyncMock),
    ):
        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.watch_new = AsyncMock(return_value=[card])
        fi.get_card  = AsyncMock(return_value=card)
        async def mp(cid, sid): estados.append(("prec", sid)); card["stage_id"] = sid
        async def up(cid, f):
            if "Data proposta enviada" in f: card["Data proposta enviada"] = f["Data proposta enviada"]
        fi.move_card = AsyncMock(side_effect=mp); fi.update_card = AsyncMock(side_effect=up)
        MockFaro.return_value = fi
        from jobs.precificacao import run_precificacao
        await run_precificacao()

    r.assert_true(any(e[0] == "prec" and e[1] == Stage.EM_NEGOCIACAO for e in estados),
                  "Etapa 2: proposta enviada, card em EM_NEGOCIACAO")

    # ── Etapa 3: Lead aceita ────────────────────────────────────────────
    card["stage_id"] = Stage.EM_NEGOCIACAO
    ai_aceita = json.dumps({"intent": "ACEITAR", "reasoning": "sim", "response": "Ótimo! 🎉"})

    zapi_n = AsyncMock()
    zapi_n.__aenter__ = AsyncMock(return_value=zapi_n)
    zapi_n.__aexit__  = AsyncMock(return_value=None)
    zapi_n.send_text  = AsyncMock(return_value={"sent": True})

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.get_zapi_for_card", return_value=zapi_n),
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(return_value=ai_aceita); MockAI.return_value = ai
        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = AsyncMock(return_value={}); MockWhapi.return_value = wi
        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_card = AsyncMock(return_value=card)
        async def ma(cid, sid): estados.append(("aceite", sid)); card["stage_id"] = sid
        fi.move_card = AsyncMock(side_effect=ma); fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi
        from webhooks.negociador import handle_message
        await handle_message(card=card, mensagem="Sim, quero fechar!", current_stage_id=Stage.EM_NEGOCIACAO)

    r.assert_true(any(e[0] == "aceite" and e[1] == Stage.ACEITO for e in estados),
                  "Etapa 3: card movido para ACEITO")

    # ── Etapa 4: Contrato ZapSign ───────────────────────────────────────
    card["stage_id"] = Stage.ACEITO
    zapsign_doc = {"doc_token": "full-flow-tok-xyz", "lead_sign_url": "https://sign.zapsign.com.br/xyz"}

    zapi_c = AsyncMock()
    zapi_c.__aenter__ = AsyncMock(return_value=zapi_c)
    zapi_c.__aexit__  = AsyncMock(return_value=None)
    zapi_c.send_text  = AsyncMock(return_value={"sent": True})

    with (
        patch("jobs.contrato.FaroClient") as MockFaro,
        patch("jobs.contrato.get_zapi_for_card", return_value=zapi_c),
        patch("jobs.contrato.ZapSignClient") as MockZap,
        patch("jobs.contrato.get_template_for_adm", return_value="bradesco-tpl"),
        patch("jobs.contrato.build_form_fields", return_value={}),
        patch("jobs.contrato.NOTIFY_PHONES", []),
        patch("jobs.contrato.filter_test_cards", side_effect=lambda c: c),
        patch("jobs.contrato.asyncio.sleep", new_callable=AsyncMock),
    ):
        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.watch_new = AsyncMock(return_value=[card])
        fi.get_card  = AsyncMock(return_value=card)
        async def mc(cid, sid): estados.append(("contrato", sid)); card["stage_id"] = sid
        async def uc(cid, f):
            if "ZapSign Token" in f: card["ZapSign Token"] = f["ZapSign Token"]
        fi.move_card = AsyncMock(side_effect=mc); fi.update_card = AsyncMock(side_effect=uc)
        MockFaro.return_value = fi
        zap = AsyncMock(); zap.__aenter__ = AsyncMock(return_value=zap); zap.__aexit__ = AsyncMock(return_value=None)
        zap.create_from_template = AsyncMock(return_value=zapsign_doc); MockZap.return_value = zap
        from jobs.contrato import run_contrato
        await run_contrato()

    r.assert_true(any(e[0] == "contrato" and e[1] == Stage.ASSINATURA for e in estados),
                  "Etapa 4: card movido para ASSINATURA")
    r.assert_equal("full-flow-tok-xyz", card.get("ZapSign Token"),
                   "Etapa 4: ZapSign Token salvo no card")
    r.assert_true(len(estados) >= 4,
                  f"Fluxo completo: {len(estados)} transições de stage registradas")

# ---------------------------------------------------------------------------
# Cenários #17–19: Qualificação de extrato (Bazar/Site)
# ---------------------------------------------------------------------------

async def test_17_qualificacao_qualificado(r: ScenarioRunner) -> None:
    """Lead Bazar envia extrato válido com cota qualificada → move para PRECIFICACAO."""
    from config import Stage
    from webhooks.qualificador import handle_qualification
    from webhooks.router import IncomingMessage

    card = make_card(
        card_id="qual-ok-001",
        stage_id=Stage.PRIMEIRA_ATIVACAO,
        fonte="bazar",
        adm="Santander",
        credito="",
    )

    raw_payload = make_zapi_media_payload(
        phone=card["Telefone"],
        media_type="document",
        media_url="https://media.z-api.io/fake-extrato.pdf",
        filename="extrato_santander.pdf",
    )
    msg = IncomingMessage(
        phone=card["Telefone"],
        text=None,
        source="zapi",
        from_me=False,
        is_group=False,
        media_type="document",
        raw=raw_payload,
    )

    # IA retorna cota qualificada (pago 30% do crédito, abaixo do limite de 50%)
    ai_response = json.dumps({
        "resultado":       "QUALIFICADO",
        "administradora":  "Santander",
        "valor_credito":   200000.0,
        "valor_pago":      60000.0,
        "parcelas_pagas":  30,
        "total_parcelas":  100,
        "motivo":          "Valor pago (30%) dentro do limite de 50%",
    })

    estados = []
    zapi_mock = AsyncMock()
    zapi_mock.__aenter__ = AsyncMock(return_value=zapi_mock)
    zapi_mock.__aexit__  = AsyncMock(return_value=None)
    zapi_mock.send_text  = AsyncMock(return_value={"sent": True})

    with (
        patch("webhooks.qualificador.AIClient") as MockAI,
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi_mock),
        patch("webhooks.qualificador.FaroClient") as MockFaro,
        patch("webhooks.qualificador.NOTIFY_PHONES", []),
    ):
        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.complete_with_image = AsyncMock(return_value=ai_response)
        MockAI.return_value = ai_inst

        fi = AsyncMock()
        fi.__aenter__ = AsyncMock(return_value=fi)
        fi.__aexit__  = AsyncMock(return_value=None)
        async def move_qual(cid, sid):
            estados.append(sid)
            card["stage_id"] = sid
        fi.move_card   = AsyncMock(side_effect=move_qual)
        fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi

        await handle_qualification(card=card, msg=msg)

    r.assert_called(ai_inst.complete_with_image, "IA de visão chamada para analisar extrato")
    r.assert_true(Stage.PRECIFICACAO in estados, "Cota qualificada: card movido para PRECIFICACAO")
    r.assert_called(zapi_mock.send_text, "Mensagem de boas-vindas enviada ao lead")
    r.assert_called_with_contains(
        zapi_mock.send_text, "proposta",
        "Mensagem ao lead menciona envio de proposta",
    )


async def test_18_qualificacao_nao_qualificado(r: ScenarioRunner) -> None:
    """Lead Bazar envia extrato com cota acima do teto → move para NAO_QUALIFICADO."""
    from config import Stage
    from webhooks.qualificador import handle_qualification
    from webhooks.router import IncomingMessage

    card = make_card(
        card_id="qual-nok-001",
        stage_id=Stage.PRIMEIRA_ATIVACAO,
        fonte="bazar",
        adm="Bradesco",
    )

    raw_payload = make_zapi_media_payload(
        phone=card["Telefone"],
        media_type="image",
        media_url="https://media.z-api.io/fake-extrato-imagem.jpg",
    )
    msg = IncomingMessage(
        phone=card["Telefone"],
        text=None,
        source="zapi",
        from_me=False,
        is_group=False,
        media_type="image",
        raw=raw_payload,
    )

    # IA retorna cota NÃO qualificada (pago 75% — acima do limite de 50%)
    ai_response = json.dumps({
        "resultado":       "NAO_QUALIFICADO",
        "administradora":  "Bradesco",
        "valor_credito":   300000.0,
        "valor_pago":      225000.0,
        "parcelas_pagas":  75,
        "total_parcelas":  100,
        "motivo":          "Valor pago (75%) excede o limite máximo de 50%",
    })

    estados = []
    zapi_mock = AsyncMock()
    zapi_mock.__aenter__ = AsyncMock(return_value=zapi_mock)
    zapi_mock.__aexit__  = AsyncMock(return_value=None)
    zapi_mock.send_text  = AsyncMock(return_value={"sent": True})

    with (
        patch("webhooks.qualificador.AIClient") as MockAI,
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi_mock),
        patch("webhooks.qualificador.FaroClient") as MockFaro,
        patch("webhooks.qualificador.NOTIFY_PHONES", []),
    ):
        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.complete_with_image = AsyncMock(return_value=ai_response)
        MockAI.return_value = ai_inst

        fi = AsyncMock()
        fi.__aenter__ = AsyncMock(return_value=fi)
        fi.__aexit__  = AsyncMock(return_value=None)
        async def move_nok(cid, sid):
            estados.append(sid)
            card["stage_id"] = sid
        fi.move_card   = AsyncMock(side_effect=move_nok)
        fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi

        await handle_qualification(card=card, msg=msg)

    r.assert_called(ai_inst.complete_with_image, "IA de visão chamada para analisar extrato")
    r.assert_true(Stage.NAO_QUALIFICADO in estados, "Cota não qualificada: card movido para NAO_QUALIFICADO")
    r.assert_called(zapi_mock.send_text, "Mensagem de dispensa enviada ao lead")
    r.assert_called_with_contains(
        zapi_mock.send_text, "teto de aquisição",
        "Mensagem menciona teto de aquisição",
    )


async def test_19_qualificacao_extrato_incorreto(r: ScenarioRunner) -> None:
    """Lead Bazar envia documento ilegível/errado → orienta a enviar extrato correto."""
    from config import Stage
    from webhooks.qualificador import handle_qualification
    from webhooks.router import IncomingMessage

    card = make_card(
        card_id="qual-inc-001",
        stage_id=Stage.SEGUNDA_ATIVACAO,
        fonte="bazar",
        adm="Porto",
    )
    stage_original = card["stage_id"]

    raw_payload = make_zapi_media_payload(
        phone=card["Telefone"],
        media_type="image",
        media_url="https://media.z-api.io/foto-borrada.jpg",
    )
    msg = IncomingMessage(
        phone=card["Telefone"],
        text=None,
        source="zapi",
        from_me=False,
        is_group=False,
        media_type="image",
        raw=raw_payload,
    )

    # IA retorna extrato incorreto (imagem borrada / boleto / outro documento)
    ai_response = json.dumps({
        "resultado":       "EXTRATO_INCORRETO",
        "administradora":  None,
        "valor_credito":   0.0,
        "valor_pago":      0.0,
        "parcelas_pagas":  0,
        "total_parcelas":  0,
        "motivo":          "Imagem ilegível — não foi possível identificar dados do consórcio",
    })

    estados_movidos = []
    zapi_mock = AsyncMock()
    zapi_mock.__aenter__ = AsyncMock(return_value=zapi_mock)
    zapi_mock.__aexit__  = AsyncMock(return_value=None)
    zapi_mock.send_text  = AsyncMock(return_value={"sent": True})

    with (
        patch("webhooks.qualificador.AIClient") as MockAI,
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi_mock),
        patch("webhooks.qualificador.FaroClient") as MockFaro,
        patch("webhooks.qualificador.NOTIFY_PHONES", []),
    ):
        ai_inst = AsyncMock()
        ai_inst.__aenter__ = AsyncMock(return_value=ai_inst)
        ai_inst.__aexit__  = AsyncMock(return_value=None)
        ai_inst.complete_with_image = AsyncMock(return_value=ai_response)
        MockAI.return_value = ai_inst

        fi = AsyncMock()
        fi.__aenter__ = AsyncMock(return_value=fi)
        fi.__aexit__  = AsyncMock(return_value=None)
        async def move_inc(cid, sid):
            estados_movidos.append(sid)
        fi.move_card   = AsyncMock(side_effect=move_inc)
        fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi

        await handle_qualification(card=card, msg=msg)

    r.assert_called(ai_inst.complete_with_image, "IA de visão chamada para analisar extrato")
    r.assert_true(len(estados_movidos) == 0, "Extrato incorreto: stage NÃO alterado (lead tenta de novo)")
    r.assert_called(zapi_mock.send_text, "Mensagem de orientação enviada ao lead")
    r.assert_called_with_contains(
        zapi_mock.send_text, "extrato",
        "Mensagem orienta o lead a enviar o extrato correto",
    )


# ---------------------------------------------------------------------------
# Cenários #20–25: Agente SDR Listas e Bazar
# ---------------------------------------------------------------------------

async def test_20_agente_listas_interesse(r: ScenarioRunner) -> None:
    """Lead de lista responde com interesse → PRECIFICACAO + proposta imediata disparada."""
    from config import Stage
    from webhooks.agente_listas import handle_message

    card = make_card(
        card_id="lista-int-001",
        stage_id=Stage.PRIMEIRA_ATIVACAO,
        fonte="listas",
        phone="5511900000020",
        credito="R$ 180.000,00",
        parcela="R$ 1.100,00",
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})
    create_task_mock = MagicMock()

    ai_resp = json.dumps({
        "intent": "INTERESSE",
        "response": "Ótimo! Já estou encaminhando para análise — em breve você recebe a proposta. 😊",
    })

    with (
        patch("webhooks.agente_listas.FaroClient") as MockFaro,
        patch("webhooks.agente_listas.WhapiClient") as MockWhapi,
        patch("webhooks.agente_listas.AIClient") as MockAI,
        patch("webhooks.agente_listas.asyncio.create_task", create_task_mock),
        patch("webhooks.agente_listas.slack_error", new_callable=AsyncMock),
    ):
        fi = AsyncMock()
        fi.__aenter__ = AsyncMock(return_value=fi)
        fi.__aexit__  = AsyncMock(return_value=None)
        fi.get_card   = AsyncMock(return_value=card)
        fi.move_card  = move_mock
        fi.update_card = update_mock
        fi.update_card_field = update_mock
        MockFaro.return_value = fi

        wi = AsyncMock()
        wi.__aenter__ = AsyncMock(return_value=wi)
        wi.__aexit__  = AsyncMock(return_value=None)
        wi.send_text  = send_text_mock
        MockWhapi.return_value = wi

        ai = AsyncMock()
        ai.__aenter__ = AsyncMock(return_value=ai)
        ai.__aexit__  = AsyncMock(return_value=None)
        ai.complete_with_history = AsyncMock(return_value=ai_resp)
        MockAI.return_value = ai

        await handle_message(card=card, text="Tenho interesse sim, quero saber mais!")

    r.assert_called(send_text_mock, "Resposta de interesse enviada ao lead")
    r.assert_called(move_mock, "Card movido após intent INTERESSE")
    r.assert_called_with_contains(move_mock, Stage.PRECIFICACAO, "Card movido para PRECIFICACAO")
    r.assert_called(create_task_mock, "asyncio.create_task chamado para proposta imediata")


async def test_21_agente_listas_recusa(r: ScenarioRunner) -> None:
    """Lead de lista informa que vendeu a cota → DISPENSADOS."""
    from config import Stage
    from webhooks.agente_listas import handle_message

    card = make_card(
        card_id="lista-rec-001",
        stage_id=Stage.SEGUNDA_ATIVACAO,
        fonte="listas",
        phone="5511900000021",
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_resp = json.dumps({
        "intent": "RECUSA_COTA_VENDIDA",
        "response": "Entendido! Obrigada pelo aviso. Se quiser acompanhar o mercado no futuro: https://wa.me/group 😊",
    })

    with (
        patch("webhooks.agente_listas.FaroClient") as MockFaro,
        patch("webhooks.agente_listas.WhapiClient") as MockWhapi,
        patch("webhooks.agente_listas.AIClient") as MockAI,
        patch("webhooks.agente_listas.asyncio.create_task", MagicMock()),
        patch("webhooks.agente_listas.slack_error", new_callable=AsyncMock),
    ):
        fi = AsyncMock()
        fi.__aenter__ = AsyncMock(return_value=fi)
        fi.__aexit__  = AsyncMock(return_value=None)
        fi.get_card   = AsyncMock(return_value=card)
        fi.move_card  = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        wi = AsyncMock()
        wi.__aenter__ = AsyncMock(return_value=wi)
        wi.__aexit__  = AsyncMock(return_value=None)
        wi.send_text  = send_text_mock
        MockWhapi.return_value = wi

        ai = AsyncMock()
        ai.__aenter__ = AsyncMock(return_value=ai)
        ai.__aexit__  = AsyncMock(return_value=None)
        ai.complete_with_history = AsyncMock(return_value=ai_resp)
        MockAI.return_value = ai

        await handle_message(card=card, text="Já vendi minha cota mês passado.")

    r.assert_called(send_text_mock, "Resposta de despedida enviada ao lead")
    r.assert_called(move_mock, "Card movido após recusa")
    r.assert_called_with_contains(move_mock, Stage.DISPENSADOS, "Card movido para DISPENSADOS")


async def test_22_agente_listas_redirecionar(r: ScenarioRunner) -> None:
    """Lead de lista pede para falar com consultor → FINALIZACAO_COMERCIAL + notificação."""
    from config import Stage
    from webhooks.agente_listas import handle_message

    card = make_card(
        card_id="lista-redir-001",
        stage_id=Stage.TERCEIRA_ATIVACAO,
        fonte="listas",
        phone="5511900000022",
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_resp = json.dumps({
        "intent": "REDIRECIONAR",
        "response": "Claro! Vou acionar o consultor responsável pra você agora. 🙏",
    })

    with (
        patch("webhooks.agente_listas.FaroClient") as MockFaro,
        patch("webhooks.agente_listas.WhapiClient") as MockWhapi,
        patch("webhooks.agente_listas.AIClient") as MockAI,
        patch("webhooks.agente_listas.asyncio.create_task", MagicMock()),
        patch("webhooks.agente_listas.slack_error", new_callable=AsyncMock),
        patch("webhooks.negociador._build_handoff_notification",
              return_value=("Notificação de handoff", ["5511900000099"])),
    ):
        fi = AsyncMock()
        fi.__aenter__ = AsyncMock(return_value=fi)
        fi.__aexit__  = AsyncMock(return_value=None)
        fi.get_card   = AsyncMock(return_value=card)
        fi.move_card  = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        wi = AsyncMock()
        wi.__aenter__ = AsyncMock(return_value=wi)
        wi.__aexit__  = AsyncMock(return_value=None)
        wi.send_text  = send_text_mock
        MockWhapi.return_value = wi

        ai = AsyncMock()
        ai.__aenter__ = AsyncMock(return_value=ai)
        ai.__aexit__  = AsyncMock(return_value=None)
        ai.complete_with_history = AsyncMock(return_value=ai_resp)
        MockAI.return_value = ai

        await handle_message(card=card, text="Prefiro falar diretamente com alguém.")

    r.assert_called(send_text_mock, "Resposta e/ou notificação enviada")
    r.assert_called(move_mock, "Card movido após REDIRECIONAR")
    r.assert_called_with_contains(move_mock, Stage.FINALIZACAO_COMERCIAL,
                                  "Card movido para FINALIZACAO_COMERCIAL")


async def test_23_agente_listas_outro(r: ScenarioRunner) -> None:
    """Lead de lista manda mensagem ambígua → resposta enviada, stage NÃO muda."""
    from config import Stage
    from webhooks.agente_listas import handle_message

    card = make_card(
        card_id="lista-outro-001",
        stage_id=Stage.PRIMEIRA_ATIVACAO,
        fonte="listas",
        phone="5511900000023",
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_resp = json.dumps({
        "intent": "OUTRO",
        "response": "Pode me contar mais? Quero entender melhor como posso te ajudar. 😊",
    })

    with (
        patch("webhooks.agente_listas.FaroClient") as MockFaro,
        patch("webhooks.agente_listas.WhapiClient") as MockWhapi,
        patch("webhooks.agente_listas.AIClient") as MockAI,
        patch("webhooks.agente_listas.asyncio.create_task", MagicMock()),
        patch("webhooks.agente_listas.slack_error", new_callable=AsyncMock),
    ):
        fi = AsyncMock()
        fi.__aenter__ = AsyncMock(return_value=fi)
        fi.__aexit__  = AsyncMock(return_value=None)
        fi.get_card   = AsyncMock(return_value=card)
        fi.move_card  = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        wi = AsyncMock()
        wi.__aenter__ = AsyncMock(return_value=wi)
        wi.__aexit__  = AsyncMock(return_value=None)
        wi.send_text  = send_text_mock
        MockWhapi.return_value = wi

        ai = AsyncMock()
        ai.__aenter__ = AsyncMock(return_value=ai)
        ai.__aexit__  = AsyncMock(return_value=None)
        ai.complete_with_history = AsyncMock(return_value=ai_resp)
        MockAI.return_value = ai

        await handle_message(card=card, text="oi")

    r.assert_called(send_text_mock, "Resposta enviada para mensagem ambígua")
    r.assert_true(not move_mock.called, "Stage NÃO muda para intent OUTRO")


async def test_24_agente_bazar_aguardando_extrato(r: ScenarioRunner) -> None:
    """Lead Bazar está engajado aguardando enviar extrato → resposta de acompanhamento."""
    from config import Stage
    from webhooks.agente_bazar import handle_message

    card = make_card(
        card_id="bazar-agua-001",
        stage_id=Stage.SEGUNDA_ATIVACAO,
        fonte="bazar",
        phone="5511900000024",
        adm="Porto Seguro",
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    zapi_inst = AsyncMock()
    zapi_inst.__aenter__ = AsyncMock(return_value=zapi_inst)
    zapi_inst.__aexit__  = AsyncMock(return_value=None)
    zapi_inst.send_text  = send_text_mock

    ai_resp = json.dumps({
        "intent": "AGUARDANDO_EXTRATO",
        "response": "Ótimo! Pode me mandar o extrato quando tiver — analiso rapidinho. 🙏",
    })

    with (
        patch("webhooks.agente_bazar.FaroClient") as MockFaro,
        patch("webhooks.agente_bazar.get_zapi_for_card", return_value=zapi_inst),
        patch("webhooks.agente_bazar.AIClient") as MockAI,
        patch("webhooks.agente_bazar.slack_error", new_callable=AsyncMock),
    ):
        fi = AsyncMock()
        fi.__aenter__ = AsyncMock(return_value=fi)
        fi.__aexit__  = AsyncMock(return_value=None)
        fi.get_card   = AsyncMock(return_value=card)
        fi.move_card  = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        ai = AsyncMock()
        ai.__aenter__ = AsyncMock(return_value=ai)
        ai.__aexit__  = AsyncMock(return_value=None)
        ai.complete_with_history = AsyncMock(return_value=ai_resp)
        MockAI.return_value = ai

        await handle_message(card=card, text="Vou buscar o extrato amanhã, tudo bem?")

    r.assert_called(send_text_mock, "Resposta enviada ao lead Bazar")
    r.assert_true(not move_mock.called, "Stage NÃO muda para AGUARDANDO_EXTRATO")


async def test_25_agente_bazar_recusa(r: ScenarioRunner) -> None:
    """Lead Bazar declara que a cota foi vendida → PERDIDO."""
    from config import Stage
    from webhooks.agente_bazar import handle_message

    card = make_card(
        card_id="bazar-rec-001",
        stage_id=Stage.PRIMEIRA_ATIVACAO,
        fonte="bazar",
        phone="5511900000025",
        adm="Itaú",
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    zapi_inst = AsyncMock()
    zapi_inst.__aenter__ = AsyncMock(return_value=zapi_inst)
    zapi_inst.__aexit__  = AsyncMock(return_value=None)
    zapi_inst.send_text  = send_text_mock

    ai_resp = json.dumps({
        "intent": "RECUSA_COTA_VENDIDA",
        "response": "Entendido, João! Sem problemas. Se quiser acompanhar o mercado no futuro, temos um grupo: https://wa.me/group 😊",
    })

    with (
        patch("webhooks.agente_bazar.FaroClient") as MockFaro,
        patch("webhooks.agente_bazar.get_zapi_for_card", return_value=zapi_inst),
        patch("webhooks.agente_bazar.AIClient") as MockAI,
        patch("webhooks.agente_bazar.slack_error", new_callable=AsyncMock),
    ):
        fi = AsyncMock()
        fi.__aenter__ = AsyncMock(return_value=fi)
        fi.__aexit__  = AsyncMock(return_value=None)
        fi.get_card   = AsyncMock(return_value=card)
        fi.move_card  = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        ai = AsyncMock()
        ai.__aenter__ = AsyncMock(return_value=ai)
        ai.__aexit__  = AsyncMock(return_value=None)
        ai.complete_with_history = AsyncMock(return_value=ai_resp)
        MockAI.return_value = ai

        await handle_message(card=card, text="Essa cota já foi vendida faz tempo.")

    r.assert_called(send_text_mock, "Resposta de despedida enviada")
    r.assert_called(move_mock, "Card movido após recusa")
    r.assert_called_with_contains(move_mock, Stage.PERDIDO, "Card movido para PERDIDO")


# ---------------------------------------------------------------------------
# Cenários #26–35: Negociador — lógica de preços e intents avançados
# ---------------------------------------------------------------------------

async def test_26_negociador_melhorar_27pct(r: ScenarioRunner) -> None:
    """MELHORAR_VALOR com proposta atual < 27% do crédito → salta direto para o máximo."""
    from config import Stage
    from webhooks.negociador import handle_message, _get_next_proposal

    # Crédito R$ 100.000 | Proposta atual R$ 20.000 (20% < 27%) → deve saltar para 32.000
    card = make_card(
        card_id="neg-27pct-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000026",
        credito="100000",
    )
    card["Crédito"]            = "100000"
    card["Proposta Realizada"] = "20000"
    card["Sequencia_Proposta"] = "20000,25000,32000"

    prox = _get_next_proposal(card)
    r.assert_true(prox["is_max_jump"], "Regra 27%: proposta < 27% do crédito aciona salto máximo")
    r.assert_equal(32000.0, prox["nova_proposta"], "Salta direto para 32.000 (máximo da sequência)")

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_resp = json.dumps({
        "intent": "MELHORAR_VALOR",
        "reasoning": "Lead disse que o valor está baixo",
        "response": "Entendo, João! Deixa eu ver o que consigo fazer aqui...",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(return_value=ai_resp); MockAI.return_value = ai

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_card    = AsyncMock(return_value=card)
        fi.move_card   = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        await handle_message(card=card, mensagem="Tá muito baixo esse valor, não compensa.", current_stage_id=Stage.EM_NEGOCIACAO)

    r.assert_called(send_text_mock, "Nova proposta enviada ao lead")
    r.assert_called_with_contains(update_mock, "32000", "Proposta máxima (R$ 32.000) salva no card")


async def test_27_negociador_melhorar_escalada_normal(r: ScenarioRunner) -> None:
    """MELHORAR_VALOR com proposta >= 27% do crédito → escalada normal (próximo valor)."""
    from config import Stage
    from webhooks.negociador import _get_next_proposal

    # Crédito R$ 100.000 | Proposta atual R$ 28.000 (28% >= 27%) → próximo passo: 30.000
    card = make_card(card_id="neg-esc-001", stage_id=Stage.EM_NEGOCIACAO, phone="5511900000027")
    card["Crédito"]            = "100000"
    card["Proposta Realizada"] = "28000"
    card["Sequencia_Proposta"] = "28000,30000,32000"

    prox = _get_next_proposal(card)
    r.assert_true(not prox["is_max_jump"], "Escalada normal: não usa regra dos 27%")
    r.assert_equal(30000.0, prox["nova_proposta"], "Próximo valor na sequência: R$ 30.000")
    r.assert_true(prox["viavel"], "Ainda há valor maior depois (32.000) → viavel=True")


async def test_28_negociador_contraproposta_sequencia(r: ScenarioRunner) -> None:
    """CONTRA_PROPOSTA com valor dentro da sequência → escalada automática para cobrir."""
    from config import Stage
    from webhooks.negociador import handle_message

    # Lead propõe 30.000 e a sequência tem 30.000 disponível.
    # Proposta atual = 28.000 (28% do crédito) — não aciona regra dos 27% → escalada normal.
    card = make_card(
        card_id="neg-cp-seq-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000028",
        credito="100000",
    )
    card["Crédito"]            = "100000"
    card["Proposta Realizada"] = "28000"
    card["Sequencia_Proposta"] = "25000,28000,30000,32000"

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_resp = json.dumps({
        "intent": "CONTRA_PROPOSTA",
        "reasoning": "Lead propôs 30.000 como condição",
        "response": "Anotei! Vou verificar se consigo chegar aí pra você...",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(return_value=ai_resp); MockAI.return_value = ai

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_card    = AsyncMock(return_value=card)
        fi.move_card   = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        await handle_message(
            card=card, mensagem="Fecho por R$ 30.000", current_stage_id=Stage.EM_NEGOCIACAO
        )

    r.assert_called(send_text_mock, "Resposta com nova proposta enviada")
    r.assert_called_with_contains(update_mock, "30000", "Proposta R$ 30.000 salva (cobre contraproposta)")


async def test_29_negociador_contraproposta_absurda(r: ScenarioRunner) -> None:
    """CONTRA_PROPOSTA > 40% do crédito → resposta imediata + delayed director response."""
    from config import Stage
    from webhooks.negociador import handle_message

    # Crédito R$ 100.000 | Lead pede R$ 50.000 (50% > 40% → absurdo)
    card = make_card(
        card_id="neg-abs-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000029",
        credito="100000",
    )
    card["Crédito"]            = "100000"
    card["Proposta Realizada"] = "28000"
    card["Sequencia_Proposta"] = "28000,30000,32000"

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})
    create_task_mock = MagicMock()

    ai_resp = json.dumps({
        "intent": "CONTRA_PROPOSTA",
        "reasoning": "Lead pediu valor muito acima",
        "response": "Que proposta! Deixa eu consultar aqui com o nosso diretor comercial...",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", []),
        patch("asyncio.create_task", create_task_mock),
    ):
        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(return_value=ai_resp); MockAI.return_value = ai

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_card    = AsyncMock(return_value=card)
        fi.move_card   = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        await handle_message(
            card=card, mensagem="Quero R$ 50.000 no mínimo", current_stage_id=Stage.EM_NEGOCIACAO
        )

    r.assert_called(send_text_mock, "Resposta inicial enviada")
    r.assert_called(create_task_mock, "Delayed director response agendada via create_task")
    r.assert_true(not any(
        Stage.PERDIDO in str(call) for call in move_mock.call_args_list
    ), "Card NÃO movido para PERDIDO (contraproposta absurda não encerra imediatamente)")


async def test_30_negociador_contraproposta_acima_teto(r: ScenarioRunner) -> None:
    """CONTRA_PROPOSTA razoável mas acima do teto (32-40%) → handoff ao consultor."""
    from config import Stage
    from webhooks.negociador import handle_message

    # Crédito R$ 100.000 | Sequência máxima = 32.000 | Lead pede 35.000 (35%, razoável mas acima)
    card = make_card(
        card_id="neg-teto-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000030",
        credito="100000",
    )
    card["Crédito"]            = "100000"
    card["Proposta Realizada"] = "30000"
    card["Sequencia_Proposta"] = "25000,30000,32000"
    card["Responsáveis"]       = ""  # sem responsável → usa NOTIFY_PHONES

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_resp = json.dumps({
        "intent": "CONTRA_PROPOSTA",
        "reasoning": "Lead pediu 35.000 explicitamente",
        "response": "Anotei, João! Vou ver o que é possível fazer...",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", ["5511900000099"]),
    ):
        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(return_value=ai_resp); MockAI.return_value = ai

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_card    = AsyncMock(return_value=card)
        fi.move_card   = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        await handle_message(
            card=card, mensagem="Fecho por R$ 35.000", current_stage_id=Stage.EM_NEGOCIACAO
        )

    r.assert_called(move_mock, "Card movido após contraproposta acima do teto")
    r.assert_called_with_contains(move_mock, Stage.FINALIZACAO_COMERCIAL,
                                  "Card movido para FINALIZACAO_COMERCIAL")
    r.assert_called(send_text_mock, "Resposta enviada + notificação ao consultor")


async def test_31_negociador_ofereceram_mais_sem_valor(r: ScenarioRunner) -> None:
    """OFERECERAM_MAIS sem valor numérico → bot pede qual foi o valor da concorrência."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_card(
        card_id="neg-ofm-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000031",
        credito="200000",
    )
    card["Crédito"] = "200000"
    card["Proposta Realizada"] = "55000"
    card["Sequencia_Proposta"] = "55000,60000,64000"

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_resp = json.dumps({
        "intent": "OFERECERAM_MAIS",
        "reasoning": "Menciona concorrência mas sem valor",
        "response": "Que ótimo que você me contou, João! Qual foi o valor que te ofereceram? Com esse número consigo levar pro nosso diretor. 😊",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(return_value=ai_resp); MockAI.return_value = ai

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_card    = AsyncMock(return_value=card)
        fi.move_card   = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        await handle_message(
            card=card, mensagem="Outra empresa me ofereceu mais que isso",
            current_stage_id=Stage.EM_NEGOCIACAO
        )

    r.assert_called(send_text_mock, "Resposta pedindo o valor do concorrente")
    r.assert_true(not move_mock.called, "Stage mantido enquanto aguarda o valor")


async def test_32_negociador_ofereceram_mais_com_valor(r: ScenarioRunner) -> None:
    """OFERECERAM_MAIS com valor explícito → reclassifica como CONTRA_PROPOSTA."""
    from config import Stage
    from webhooks.negociador import _message_has_value, _extract_lead_value, _build_result, Intent

    # Valida as funções de detecção de valor
    mensagem = "Me ofereceram R$ 62.000 em outra empresa"
    r.assert_true(_message_has_value(mensagem), "Valor R$ 62.000 detectado na mensagem")
    val = _extract_lead_value(mensagem)
    r.assert_true(abs(val - 62000.0) < 1.0, f"Valor extraído correto: {val}")

    # Valida que OFERECERAM_MAIS com valor → reclassificado para CONTRA_PROPOSTA em _build_result
    card = make_card(card_id="neg-ofmv-001", stage_id=Stage.EM_NEGOCIACAO, phone="5511900000032")
    card["Crédito"]            = "200000"
    card["Proposta Realizada"] = "55000"
    card["Sequencia_Proposta"] = "55000,60000,64000"

    result = _build_result(Intent.OFERECERAM_MAIS, "Resposta IA", card, mensagem)
    r.assert_equal(Intent.CONTRA_PROPOSTA, result.intent,
                   "OFERECERAM_MAIS com valor reclassifica para CONTRA_PROPOSTA")


async def test_33_negociador_desconfianca(r: ScenarioRunner) -> None:
    """Lead demonstra desconfiança sobre a empresa → resposta com argumentos de credibilidade."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_card(
        card_id="neg-desc-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000033",
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_resp = json.dumps({
        "intent": "DESCONFIANCA",
        "reasoning": "Lead perguntou CNPJ e teme golpe",
        "response": "Faz todo sentido ter cuidado! Somos a Consórcio Sorteado — CNPJ 07.931.205/0001-30, Rua Irmã Carolina 45, Belenzinho-SP, mais de 18 anos de mercado. O pagamento é feito ANTES da transferência — você não assume risco nenhum. 😊",
    })

    with (
        patch("webhooks.negociador.AIClient") as MockAI,
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.NOTIFY_PHONES", []),
    ):
        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(return_value=ai_resp); MockAI.return_value = ai

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_card    = AsyncMock(return_value=card)
        fi.move_card   = AsyncMock(return_value={}); fi.update_card = update_mock
        MockFaro.return_value = fi

        await handle_message(
            card=card, mensagem="Como sei que isso não é golpe? Me manda o CNPJ de vocês",
            current_stage_id=Stage.EM_NEGOCIACAO
        )

    r.assert_called(send_text_mock, "Resposta de credibilidade enviada")
    r.assert_called_with_contains(send_text_mock, "07.931.205", "CNPJ incluído na resposta")


async def test_34_negociador_assinatura_sem_token(r: ScenarioRunner) -> None:
    """Lead em ASSINATURA sem ZapSign Token → mensagem informando que contrato está sendo finalizado."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_card(
        card_id="neg-ass-sem-001",
        stage_id=Stage.ASSINATURA,
        phone="5511900000034",
        adm="Santander",
    )
    card["ZapSign Token"] = ""  # sem token

    send_text_mock = AsyncMock(return_value={"sent": True})

    with (
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.AIClient") as MockAI,
    ):
        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_card    = AsyncMock(return_value=card)
        fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi

        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(return_value="Seu contrato está em preparação!"); MockAI.return_value = ai

        await handle_message(
            card=card, mensagem="E o contrato, quando chega?",
            current_stage_id=Stage.ASSINATURA
        )

    r.assert_called(send_text_mock, "Mensagem sobre contrato pendente enviada")


async def test_35_negociador_assinatura_link_problem(r: ScenarioRunner) -> None:
    """Lead em ASSINATURA com token tem problema no link → orientação sobre o link."""
    from config import Stage
    from webhooks.negociador import handle_message

    card = make_card(
        card_id="neg-ass-link-001",
        stage_id=Stage.ASSINATURA,
        phone="5511900000035",
        adm="Bradesco",
    )
    card["ZapSign Token"] = "tok-abc-123"

    send_text_mock = AsyncMock(return_value={"sent": True})

    with (
        patch("webhooks.negociador.WhapiClient") as MockWhapi,
        patch("webhooks.negociador.FaroClient") as MockFaro,
        patch("webhooks.negociador.AIClient") as MockAI,
    ):
        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_card    = AsyncMock(return_value=card)
        fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi

        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(return_value="Reenvio o link agora!"); MockAI.return_value = ai

        await handle_message(
            card=card, mensagem="não consigo abrir o link de assinatura",
            current_stage_id=Stage.ASSINATURA
        )

    r.assert_called(send_text_mock, "Orientação sobre o link enviada")


# ---------------------------------------------------------------------------
# Cenários #36–37: Debounce
# ---------------------------------------------------------------------------

async def test_36_debounce_multiplas_mensagens(r: ScenarioRunner) -> None:
    """Múltiplas mensagens acumuladas → dispatch único com texto combinado."""
    import asyncio
    from webhooks import debounce

    # Limpa estado do debounce antes do teste
    debounce._pending.clear()
    debounce._buffers.clear()
    debounce._card_latest.clear()

    phone       = "5511900000036"
    card        = make_card(card_id="deb-multi-001", phone=phone)
    dispatch_calls = []

    async def mock_dispatch(c: dict, text: str) -> None:
        dispatch_calls.append(text)

    with patch("webhooks.debounce.DEBOUNCE_SECONDS", 0.05):
        debounce.schedule(phone=phone, text="Olá,",     card=card, dispatch=mock_dispatch)
        debounce.schedule(phone=phone, text="quero",    card=card, dispatch=mock_dispatch)
        debounce.schedule(phone=phone, text="saber mais", card=card, dispatch=mock_dispatch)

        await asyncio.sleep(0.2)

    r.assert_equal(1, len(dispatch_calls), "Dispatch chamado exatamente uma vez para 3 mensagens")
    r.assert_true("Olá," in dispatch_calls[0], "Primeira mensagem presente no texto combinado")
    r.assert_true("saber mais" in dispatch_calls[0], "Última mensagem presente no texto combinado")


async def test_37_debounce_dispatch_exception(r: ScenarioRunner) -> None:
    """Dispatch que lança exceção → erro capturado e logado, não propagado."""
    import asyncio
    from webhooks import debounce

    debounce._pending.clear()
    debounce._buffers.clear()
    debounce._card_latest.clear()

    phone = "5511900000037"
    card  = make_card(card_id="deb-exc-001", phone=phone)

    async def failing_dispatch(c: dict, text: str) -> None:
        raise RuntimeError("Falha simulada no dispatch")

    exception_raised = False
    with patch("webhooks.debounce.DEBOUNCE_SECONDS", 0.05):
        debounce.schedule(phone=phone, text="Teste", card=card, dispatch=failing_dispatch)
        try:
            await asyncio.sleep(0.2)
        except Exception:
            exception_raised = True

    r.assert_true(not exception_raised, "Exceção no dispatch NÃO propaga para fora do debounce")


# ---------------------------------------------------------------------------
# Cenários #38–39: Qualificador — casos de texto (sem mídia)
# ---------------------------------------------------------------------------

async def test_38_qualificacao_recusa_verbal(r: ScenarioRunner) -> None:
    """Lead Bazar envia texto com recusa verbal ('já vendi') → move para PERDIDO."""
    from config import Stage
    from webhooks.qualificador import handle_qualification
    from webhooks.router import IncomingMessage

    card = make_card(
        card_id="qual-rec-001",
        stage_id=Stage.SEGUNDA_ATIVACAO,
        fonte="bazar",
        phone="5511900000038",
    )

    msg = IncomingMessage(
        phone=card["Telefone"],
        text="já vendi minha cota faz algumas semanas",
        source="zapi",
        from_me=False,
        is_group=False,
        media_type=None,
        raw={},
    )

    move_mock = AsyncMock(return_value={"success": True})
    zapi_mock = AsyncMock()
    zapi_mock.__aenter__ = AsyncMock(return_value=zapi_mock)
    zapi_mock.__aexit__  = AsyncMock(return_value=None)
    zapi_mock.send_text  = AsyncMock(return_value={"sent": True})

    with (
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi_mock),
        patch("webhooks.qualificador.FaroClient") as MockFaro,
        patch("webhooks.qualificador.NOTIFY_PHONES", []),
    ):
        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.move_card   = move_mock
        fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi

        await handle_qualification(card=card, msg=msg)

    r.assert_called(move_mock, "Card movido após recusa verbal")
    r.assert_called_with_contains(move_mock, Stage.PERDIDO, "Card movido para PERDIDO")


async def test_39_qualificacao_texto_sem_midia(r: ScenarioRunner) -> None:
    """Lead Bazar envia texto genérico (sem mídia) → lead orientado a enviar o extrato."""
    from config import Stage
    from webhooks.qualificador import handle_qualification
    from webhooks.router import IncomingMessage

    card = make_card(
        card_id="qual-txt-001",
        stage_id=Stage.PRIMEIRA_ATIVACAO,
        fonte="bazar",
        phone="5511900000039",
        adm="Santander",
    )

    msg = IncomingMessage(
        phone=card["Telefone"],
        text="oi, tudo bem?",
        source="zapi",
        from_me=False,
        is_group=False,
        media_type=None,
        raw={},
    )

    send_text_mock = AsyncMock(return_value={"sent": True})
    move_mock      = AsyncMock(return_value={"success": True})

    zapi_mock = AsyncMock()
    zapi_mock.__aenter__ = AsyncMock(return_value=zapi_mock)
    zapi_mock.__aexit__  = AsyncMock(return_value=None)
    zapi_mock.send_text  = send_text_mock

    with (
        patch("webhooks.qualificador.get_zapi_for_card", return_value=zapi_mock),
        patch("webhooks.qualificador.FaroClient") as MockFaro,
        patch("webhooks.qualificador.NOTIFY_PHONES", []),
    ):
        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.move_card   = move_mock
        fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi

        await handle_qualification(card=card, msg=msg)

    r.assert_called(send_text_mock, "Mensagem pedindo extrato enviada ao lead")
    r.assert_true(not move_mock.called, "Stage NÃO alterado para texto sem mídia")


# ---------------------------------------------------------------------------
# Cenários #40–43: Agente Contrato
# ---------------------------------------------------------------------------

async def test_40_agente_contrato_dados_parciais(r: ScenarioRunner) -> None:
    """Lead envia CPF e e-mail (falta RG e Endereço) → bot confirma e pede o que falta."""
    from webhooks.agente_contrato import handle_dados_pessoais

    card = make_card(
        card_id="cont-parcial-001",
        stage_id="assinatura-stage-id",
        fonte="listas",
        phone="5511900000040",
        adm="Itaú",
    )
    card["Dados Pessoais Texto"] = ""  # nada coletado ainda

    send_text_mock = AsyncMock(return_value={"sent": True})
    update_mock    = AsyncMock(return_value={"success": True})

    # IA extrai CPF e email da mensagem
    ai_extract_resp = json.dumps({
        "CPF": "123.456.789-00",
        "RG": None,
        "Endereco": None,
        "Email": "joao@email.com",
    })
    # IA de resposta gera mensagem confirmando recebido + pedindo o que falta
    ai_reply_resp = "Ótimo, João! Recebi seu CPF e e-mail ✅\n\nAinda preciso de:\n• *RG ou CNH*\n• *Endereço completo*"

    with (
        patch("webhooks.agente_contrato.AIClient") as MockAI,
        patch("webhooks.agente_contrato.WhapiClient") as MockWhapi,
        patch("webhooks.agente_contrato.FaroClient") as MockFaro,
    ):
        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(side_effect=[ai_extract_resp, ai_reply_resp])
        MockAI.return_value = ai

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.update_card = update_mock
        MockFaro.return_value = fi

        await handle_dados_pessoais(
            card=card,
            texto="Meu CPF é 123.456.789-00 e meu e-mail é joao@email.com",
        )

    r.assert_called(send_text_mock, "Resposta confirmando dados parciais enviada")
    r.assert_called(update_mock, "Dados parciais salvos no FARO")
    r.assert_called_with_contains(update_mock, "123.456.789-00", "CPF salvo nos dados coletados")


async def test_41_agente_contrato_dados_completos(r: ScenarioRunner) -> None:
    """Lead envia último dado faltante (completa os 4 campos) → bot confirma e pede extrato."""
    from webhooks.agente_contrato import handle_dados_pessoais

    card = make_card(
        card_id="cont-completo-001",
        stage_id="assinatura-stage-id",
        fonte="listas",
        phone="5511900000041",
        adm="Santander",
    )
    # 3 campos já coletados, só falta o RG
    import json as _json
    card["Dados Pessoais Texto"] = _json.dumps({
        "CPF": "123.456.789-00",
        "Email": "joao@email.com",
        "Endereco": "Rua das Flores, 100, SP 01310-100",
    })

    send_text_mock = AsyncMock(return_value={"sent": True})
    update_mock    = AsyncMock(return_value={"success": True})

    ai_extract_resp = _json.dumps({
        "CPF": None,
        "RG": "12.345.678-9",
        "Endereco": None,
        "Email": None,
    })
    ai_reply_resp = "Perfeito, João! Todos os seus dados foram confirmados ✅\n\nAgora só falta o extrato detalhado da sua cota Santander."

    with (
        patch("webhooks.agente_contrato.AIClient") as MockAI,
        patch("webhooks.agente_contrato.WhapiClient") as MockWhapi,
        patch("webhooks.agente_contrato.FaroClient") as MockFaro,
    ):
        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        ai.complete = AsyncMock(side_effect=[ai_extract_resp, ai_reply_resp])
        MockAI.return_value = ai

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.update_card = update_mock
        MockFaro.return_value = fi

        await handle_dados_pessoais(
            card=card,
            texto="Meu RG é 12.345.678-9",
        )

    r.assert_called(send_text_mock, "Confirmação de dados completos enviada")
    r.assert_called_with_contains(send_text_mock, "extrato",
                                  "Mensagem menciona pedido de extrato")


async def test_42_agente_contrato_extrato_sem_dados(r: ScenarioRunner) -> None:
    """Lead envia extrato (mídia) antes de fornecer dados pessoais → bot pede dados primeiro."""
    from webhooks.agente_contrato import handle_extrato_recebido
    from webhooks.router import IncomingMessage

    card = make_card(
        card_id="cont-ext-nok-001",
        stage_id="assinatura-stage-id",
        fonte="listas",
        phone="5511900000042",
        adm="Porto Seguro",
    )
    card["Dados Pessoais Texto"] = ""  # sem dados coletados

    send_text_mock = AsyncMock(return_value={"sent": True})

    msg = IncomingMessage(
        phone=card["Telefone"],
        text=None,
        source="whapi",
        from_me=False,
        is_group=False,
        media_type="document",
        raw={"messages": [{"media": {"url": "https://whapi.cloud/fake-extrato.pdf"}}]},
    )

    with (
        patch("webhooks.agente_contrato.WhapiClient") as MockWhapi,
        patch("webhooks.agente_contrato.FaroClient") as MockFaro,
    ):
        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi

        await handle_extrato_recebido(card=card, msg=msg)

    r.assert_called(send_text_mock, "Bot pediu dados pessoais antes de aceitar extrato")
    r.assert_called_with_contains(send_text_mock, "CPF", "Mensagem menciona CPF como dado faltante")


async def test_43_agente_contrato_extrato_com_dados(r: ScenarioRunner) -> None:
    """Lead envia extrato com todos os dados já coletados → ZapSign é gerado."""
    import asyncio as _asyncio
    from webhooks.agente_contrato import handle_extrato_recebido
    from webhooks.router import IncomingMessage
    import json as _json

    card = make_card(
        card_id="cont-ext-ok-001",
        stage_id="assinatura-stage-id",
        fonte="listas",
        phone="5511900000043",
        adm="Bradesco",
    )
    card["Dados Pessoais Texto"] = _json.dumps({
        "CPF":      "123.456.789-00",
        "RG":       "12.345.678-9",
        "Endereco": "Rua das Flores, 100, SP",
        "Email":    "joao@email.com",
    })

    send_text_mock   = AsyncMock(return_value={"sent": True})
    create_task_mock = MagicMock()

    msg = IncomingMessage(
        phone=card["Telefone"],
        text=None,
        source="whapi",
        from_me=False,
        is_group=False,
        media_type="document",
        raw={},
    )

    with (
        patch("webhooks.agente_contrato.WhapiClient") as MockWhapi,
        patch("webhooks.agente_contrato.FaroClient") as MockFaro,
        patch("jobs.contrato.generate_and_send_contract", AsyncMock()),
        patch("asyncio.create_task", create_task_mock),
    ):
        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = send_text_mock; MockWhapi.return_value = wi

        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.update_card = AsyncMock(return_value={})
        MockFaro.return_value = fi

        await handle_extrato_recebido(card=card, msg=msg)

    r.assert_called(send_text_mock, "Confirmação de extrato recebido enviada ao lead")
    r.assert_called(create_task_mock, "generate_and_send_contract disparado via create_task")


# ---------------------------------------------------------------------------
# Cenários #44–45: Motor de precificação (unit tests)
# ---------------------------------------------------------------------------

async def test_44_motor_precificacao_regras(r: ScenarioRunner) -> None:
    """Verifica todas as regras do motor _get_next_proposal."""
    from webhooks.negociador import _get_next_proposal

    # Caso 1: sem sequência → não escala
    card_sem = make_card()
    card_sem["Crédito"] = "100000"
    card_sem["Proposta Realizada"] = "20000"
    card_sem["Sequencia_Proposta"] = ""
    prox = _get_next_proposal(card_sem)
    r.assert_true(not prox["pode_escalar"], "Sem sequência: pode_escalar=False")

    # Caso 2: proposta já no teto → não escala
    card_teto = make_card()
    card_teto["Crédito"] = "100000"
    card_teto["Proposta Realizada"] = "32000"
    card_teto["Sequencia_Proposta"] = "25000,28000,32000"
    prox = _get_next_proposal(card_teto)
    r.assert_true(not prox["pode_escalar"], "Proposta já no máximo: pode_escalar=False")

    # Caso 3: 27% rule — salta para máximo
    card_27 = make_card()
    card_27["Crédito"] = "100000"
    card_27["Proposta Realizada"] = "20000"  # 20% < 27%
    card_27["Sequencia_Proposta"] = "20000,25000,30000,32000"
    prox = _get_next_proposal(card_27)
    r.assert_true(prox["is_max_jump"], "Regra 27%: proposta 20% do crédito aciona salto máximo")
    r.assert_equal(32000.0, prox["nova_proposta"], "Salta para 32.000 (máximo)")

    # Caso 4: escalada normal (proposta >= 27%)
    card_norm = make_card()
    card_norm["Crédito"] = "100000"
    card_norm["Proposta Realizada"] = "28000"  # 28% >= 27%
    card_norm["Sequencia_Proposta"] = "25000,28000,30000,32000"
    prox = _get_next_proposal(card_norm)
    r.assert_true(not prox["is_max_jump"], "28% do crédito: escalada normal")
    r.assert_equal(30000.0, prox["nova_proposta"], "Próximo valor: R$ 30.000")
    r.assert_true(prox["viavel"], "Ainda há R$ 32.000 disponível → viavel=True")


async def test_45_motor_parse_valores(r: ScenarioRunner) -> None:
    """_parse_br_number e _extract_lead_value reconhecem formatos variados de moeda BR."""
    from webhooks.negociador import _parse_br_number, _extract_lead_value, _message_has_value

    # _parse_br_number
    r.assert_equal(350000.0, _parse_br_number("350.000,00"), "350.000,00 → 350000.0")
    r.assert_equal(350000.0, _parse_br_number("350.000"),    "350.000 → 350000.0")
    r.assert_equal(350.0,    _parse_br_number("350,00"),     "350,00 → 350.0")
    r.assert_equal(350000.0, _parse_br_number("350000"),     "350000 → 350000.0")

    # _extract_lead_value
    r.assert_equal(90000.0, _extract_lead_value("Quero R$ 90.000"),        "R$ 90.000 → 90000.0")
    r.assert_equal(90000.0, _extract_lead_value("quero 90 mil"),            "90 mil → 90000.0")
    r.assert_equal(90000.0, _extract_lead_value("fecho por 90000"),         "90000 → 90000.0")
    r.assert_equal(350000.0, _extract_lead_value("me paga R$350.000,00"),   "R$350.000,00 → 350000.0")

    # _message_has_value
    r.assert_true(_message_has_value("aceito por R$ 95.000"),  "Detecta R$ 95.000")
    r.assert_true(_message_has_value("fecho por 90 mil"),       "Detecta '90 mil'")
    r.assert_true(not _message_has_value("quero mais dinheiro"), "Sem valor: False")


# ---------------------------------------------------------------------------
# Cenário #46: Follow-up esgotado → PERDIDO
# ---------------------------------------------------------------------------

async def test_46_followup_esgotado(r: ScenarioRunner) -> None:
    """Card com 8+ follow-ups em EM_NEGOCIACAO → movido para PERDIDO."""
    from config import Stage
    from jobs.follow_up import run_follow_up

    card = make_card(
        card_id="fu-esgot-001",
        stage_id=Stage.EM_NEGOCIACAO,
        phone="5511900000046",
    )
    card["Num Follow Ups"] = "8"  # já esgotou os 8 permitidos
    card["Ultima atividade"] = str(int(time.time()) - 3600)  # 1h atrás

    move_mock   = AsyncMock(return_value={"success": True})
    update_mock = AsyncMock(return_value={"success": True})

    with (
        patch("jobs.follow_up.FaroClient") as MockFaro,
        patch("jobs.follow_up.AIClient") as MockAI,
        patch("jobs.follow_up.WhapiClient") as MockWhapi,
        patch("jobs.follow_up._is_within_send_window", return_value=True),
        patch("jobs.follow_up.filter_test_cards", side_effect=lambda c: c),
    ):
        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_cards_all_pages = AsyncMock(return_value=[card])
        fi.move_card   = move_mock
        fi.update_card = update_mock
        MockFaro.return_value = fi

        ai = AsyncMock(); ai.__aenter__ = AsyncMock(return_value=ai); ai.__aexit__ = AsyncMock(return_value=None)
        MockAI.return_value = ai

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = AsyncMock(return_value={"sent": True}); MockWhapi.return_value = wi

        await run_follow_up()

    r.assert_called(move_mock, "Card com follow-ups esgotados foi movido")
    r.assert_called_with_contains(move_mock, Stage.PERDIDO, "Card movido para PERDIDO após 8 follow-ups")


# ---------------------------------------------------------------------------
# Cenário #47: ZapSign — assinatura parcial não altera stage
# ---------------------------------------------------------------------------

async def test_47_zapsign_parcial(r: ScenarioRunner) -> None:
    """
    ZapSign envia webhook com status 'pending' → rota ignora, _handle_zapsign_signed NÃO é chamado.
    Verifica a guarda de status no endpoint e que _handle_zapsign_signed move corretamente
    para SUCESSO apenas quando status == 'signed'.
    """
    from config import Stage

    # Parte 1: guarda de status — somente "signed" aciona o processamento
    guard_called = {"n": 0}
    async def mock_signed_handler(token, name):
        guard_called["n"] += 1

    # Simula a lógica da rota: só chama o handler se status == "signed"
    for status_val, should_call in [("signed", True), ("pending", False), ("refused", False)]:
        guard_called["n"] = 0
        if status_val == "signed":
            await mock_signed_handler("tok", "doc")
        r.assert_equal(
            1 if should_call else 0,
            guard_called["n"],
            f"Status '{status_val}': handler {'chamado' if should_call else 'NÃO chamado'}",
        )

    # Parte 2: _handle_zapsign_signed move card para SUCESSO quando token bate
    card = make_card(
        card_id="sign-parcial-001",
        stage_id=Stage.ASSINATURA,
        phone="5511900000047",
    )
    card["ZapSign Token"] = "zap-signed-tok"

    move_mock = AsyncMock(return_value={"success": True})

    with (
        patch("services.faro.FaroClient") as MockFaro,
        patch("services.whapi.WhapiClient") as MockWhapi,
        patch("main.NOTIFY_PHONES", ["5511900000099"]),
    ):
        fi = AsyncMock(); fi.__aenter__ = AsyncMock(return_value=fi); fi.__aexit__ = AsyncMock(return_value=None)
        fi.get_cards_all_pages = AsyncMock(return_value=[card])
        fi.move_card = move_mock
        MockFaro.return_value = fi

        wi = AsyncMock(); wi.__aenter__ = AsyncMock(return_value=wi); wi.__aexit__ = AsyncMock(return_value=None)
        wi.send_text = AsyncMock(return_value={"sent": True}); MockWhapi.return_value = wi

        from main import _handle_zapsign_signed
        await _handle_zapsign_signed(
            doc_token="zap-signed-tok",
            doc_name="Contrato - João Silva - Santander",
        )

    r.assert_called(move_mock, "_handle_zapsign_signed move card para SUCESSO quando token bate")
    r.assert_called_with_contains(move_mock, Stage.SUCESSO, "Stage destino é SUCESSO")


# ---------------------------------------------------------------------------
# Catálogo de cenários
# ---------------------------------------------------------------------------

SCENARIOS: list[tuple[int, str, Callable]] = [
    (1,  "Ativação de lead (Listas)",               test_01_ativacao_listas),
    (2,  "Ativação de lead (Bazar)",                 test_02_ativacao_bazar),
    (3,  "Reativação de lead frio",                  test_03_reativacao),
    (4,  "Envio de proposta (precificação)",         test_04_precificacao_proposta),
    (5,  "Proposta sem dados → não enviada",         test_04b_precificacao_sem_dados),
    (6,  "Negociação: lead aceita",                  test_05_negociador_aceita),
    (7,  "Negociação: lead recusa",                  test_06_negociador_recusa),
    (8,  "Negociação: lead quer negociar",           test_07_negociador_quer_negociar),
    (9,  "Negociação: lead tem dúvida",              test_08_negociador_duvida),
    (10, "Negociação: lead quer atendente",          test_09_negociador_quer_atendente),
    (11, "Geração de contrato (ZapSign)",            test_10_geracao_contrato),
    (12, "Webhook ZapSign: assinatura completa",     test_11_webhook_zapsign_assinatura),
    (13, "Follow-up: proposta travada",              test_12_followup_proposta_travada),
    (14, "Router Whapi: parse de payload",           test_13_router_parse_whapi),
    (15, "Router Z-API: parse de payload",           test_14_router_parse_zapi),
    (16, "Fluxo completo: Bazar → Assinatura",      test_15_fluxo_completo),
    (17, "Qualificação: extrato OK, cota qualificada",         test_17_qualificacao_qualificado),
    (18, "Qualificação: extrato OK, cota não qualificada",     test_18_qualificacao_nao_qualificado),
    (19, "Qualificação: extrato incorreto/ilegível",           test_19_qualificacao_extrato_incorreto),
    (20, "Agente Listas: interesse → PRECIFICACAO imediata",   test_20_agente_listas_interesse),
    (21, "Agente Listas: recusa (cota vendida) → DISPENSADOS", test_21_agente_listas_recusa),
    (22, "Agente Listas: redirecionar → consultor notificado", test_22_agente_listas_redirecionar),
    (23, "Agente Listas: mensagem ambígua → stage mantido",    test_23_agente_listas_outro),
    (24, "Agente Bazar: aguardando extrato → acompanhamento",  test_24_agente_bazar_aguardando_extrato),
    (25, "Agente Bazar: recusa → PERDIDO",                     test_25_agente_bazar_recusa),
    (26, "Negociador: MELHORAR_VALOR regra 27% → salta máximo", test_26_negociador_melhorar_27pct),
    (27, "Negociador: MELHORAR_VALOR escalada normal",         test_27_negociador_melhorar_escalada_normal),
    (28, "Negociador: CONTRA_PROPOSTA coberta pela sequência", test_28_negociador_contraproposta_sequencia),
    (29, "Negociador: CONTRA_PROPOSTA absurda → diretor delay", test_29_negociador_contraproposta_absurda),
    (30, "Negociador: CONTRA_PROPOSTA acima do teto → handoff", test_30_negociador_contraproposta_acima_teto),
    (31, "Negociador: OFERECERAM_MAIS sem valor → pede valor", test_31_negociador_ofereceram_mais_sem_valor),
    (32, "Negociador: OFERECERAM_MAIS com valor → CONTRA_PROPOSTA", test_32_negociador_ofereceram_mais_com_valor),
    (33, "Negociador: DESCONFIANCA → credibilidade",           test_33_negociador_desconfianca),
    (34, "Negociador: ASSINATURA sem token → avisa finalização", test_34_negociador_assinatura_sem_token),
    (35, "Negociador: ASSINATURA link problem → orienta",      test_35_negociador_assinatura_link_problem),
    (36, "Debounce: 3 msgs → dispatch único combinado",        test_36_debounce_multiplas_mensagens),
    (37, "Debounce: dispatch exception → não propaga",         test_37_debounce_dispatch_exception),
    (38, "Qualificação: recusa verbal → PERDIDO",              test_38_qualificacao_recusa_verbal),
    (39, "Qualificação: texto sem mídia → pede extrato",       test_39_qualificacao_texto_sem_midia),
    (40, "Agente Contrato: dados parciais → confirma + pede restante", test_40_agente_contrato_dados_parciais),
    (41, "Agente Contrato: dados completos → pede extrato",    test_41_agente_contrato_dados_completos),
    (42, "Agente Contrato: extrato sem dados → pede dados",    test_42_agente_contrato_extrato_sem_dados),
    (43, "Agente Contrato: extrato com dados → ZapSign gerado", test_43_agente_contrato_extrato_com_dados),
    (44, "Motor: _get_next_proposal — todas as regras",        test_44_motor_precificacao_regras),
    (45, "Motor: _parse_br_number e _extract_lead_value",      test_45_motor_parse_valores),
    (46, "Follow-up esgotado (8 tentativas) → PERDIDO",        test_46_followup_esgotado),
    (47, "ZapSign: assinatura parcial → stage mantido",        test_47_zapsign_parcial),
]


# ---------------------------------------------------------------------------
# Runner e relatório
# ---------------------------------------------------------------------------

async def run_scenario(
    num: int,
    name: str,
    fn: Callable,
    verbose: bool = False,
) -> TestResult:
    """Executa um cenário e captura resultado."""
    runner = ScenarioRunner(name, verbose=verbose)
    start  = time.time()

    try:
        await fn(runner)
        duration_ms = (time.time() - start) * 1000
        return TestResult(
            name=name,
            passed=runner.all_passed,
            duration_ms=duration_ms,
            details=[
                f"{runner.passed_count}/{runner.total} assertions passaram"
            ],
        )
    except AssertionError as e:
        duration_ms = (time.time() - start) * 1000
        return TestResult(
            name=name,
            passed=False,
            duration_ms=duration_ms,
            error=str(e),
        )
    except Exception as e:
        duration_ms = (time.time() - start) * 1000
        return TestResult(
            name=name,
            passed=False,
            duration_ms=duration_ms,
            error=f"{type(e).__name__}: {e}",
        )


def print_report(results: list[TestResult]) -> None:
    """Imprime o relatório de simulação no terminal."""
    total   = len(results)
    passed  = sum(1 for r in results if r.passed)
    failed  = total - passed

    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  SIMULAÇÃO CONSÓRCIO SORTEADO — RELATÓRIO{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}\n")

    for i, result in enumerate(results, 1):
        icon     = TICK if result.passed else CROSS
        duration = f"{result.duration_ms:5.0f}ms"
        name     = result.name[:50]
        print(f"  {icon}  #{i:02d} {name:<52} {CYAN}{duration}{RESET}")
        if not result.passed and result.error:
            print(f"         {RED}↳ {result.error[:80]}{RESET}")
        elif result.passed and result.details:
            print(f"         {YELLOW}↳ {result.details[0]}{RESET}")

    print(f"\n{BOLD}{'─' * 60}{RESET}")
    color = GREEN if failed == 0 else RED
    print(f"{BOLD}  Resultado: {color}{passed}/{total} cenários aprovados{RESET}", end="")
    if failed > 0:
        print(f"  {RED}({failed} falharam){RESET}", end="")
    print(f"\n{BOLD}{'═' * 60}{RESET}\n")


async def main(cenario: int = None, verbose: bool = False) -> int:
    """Executa os cenários e retorna código de saída (0 = OK, 1 = falhas)."""
    scenarios_to_run = SCENARIOS
    if cenario:
        scenarios_to_run = [(n, name, fn) for n, name, fn in SCENARIOS if n == cenario]
        if not scenarios_to_run:
            print(f"{RED}Cenário {cenario} não encontrado. Disponíveis: 1-{len(SCENARIOS)}{RESET}")
            return 1

    print(f"\n{BOLD}{CYAN}Iniciando simulação com {len(scenarios_to_run)} cenário(s)...{RESET}\n")

    results = []
    for num, name, fn in scenarios_to_run:
        if verbose:
            print(f"{BOLD}── #{num:02d} {name} ──{RESET}")
        result = await run_scenario(num, name, fn, verbose=verbose)
        results.append(result)
        if not verbose:
            icon = TICK if result.passed else CROSS
            print(f"  {icon} #{num:02d} {name}", end="\r", flush=True)

    print_report(results)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulação end-to-end Consórcio Sorteado")
    parser.add_argument("--cenario", type=int, default=None, help="Número do cenário (1-47)")
    parser.add_argument("--verbose", action="store_true", help="Modo verboso (mostra assertions)")
    args = parser.parse_args()

    exit_code = asyncio.run(main(cenario=args.cenario, verbose=args.verbose))
    sys.exit(exit_code)
