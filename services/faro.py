"""
services/faro.py — Cliente assíncrono para a API do FARO CRM
Todos os acessos ao CRM passam por aqui. Nunca chame a API diretamente nos jobs.
"""

import asyncio
import logging
from typing import Any

import httpx

from config import FARO_API_KEY, FARO_BASE_URL, PIPELINE_ID, HISTORY_MAX_TURNS

logger = logging.getLogger(__name__)

_RETRY_DELAYS = (1.0, 3.0)  # pausas entre tentativas (segundos)


async def _with_retry(coro_fn, label: str):
    """
    Executa coro_fn() com até 3 tentativas.
    Retentar apenas em erros de rede (RequestError) ou 5xx.
    Erros 4xx são repassados imediatamente.
    """
    last_exc = None
    for attempt, delay in enumerate((_RETRY_DELAYS[0], _RETRY_DELAYS[1], None), start=1):
        try:
            return await coro_fn()
        except FaroError as e:
            if e.status_code and 400 <= e.status_code < 500:
                raise  # erro do cliente: não retentar
            last_exc = e
            if delay is not None:
                logger.warning("FARO %s: tentativa %d falhou (%s). Retry em %.0fs…", label, attempt, e, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("FARO %s: todas as tentativas esgotadas. Último erro: %s", label, e)
    raise last_exc


class FaroError(Exception):
    """Erro da API FARO com contexto adicional."""
    def __init__(self, message: str, status_code: int = 0, endpoint: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint


class FaroClient:
    """
    Cliente assíncrono para a API do FARO CRM.

    Uso:
        async with FaroClient() as faro:
            cards = await faro.get_cards_from_stage(stage_id="...")

    Ou reutilize uma instância compartilhada (recomendado em produção):
        faro = FaroClient()
        cards = await faro.get_cards_from_stage(stage_id="...")
        await faro.aclose()
    """

    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=FARO_BASE_URL,
            headers={
                "Authorization": f"Bearer {FARO_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    async def _get(self, endpoint: str, params: dict = None) -> dict:
        async def _do():
            try:
                r = await self._client.get(endpoint, params=params or {})
                r.raise_for_status()
                data = r.json()
                if not data.get("success", True):
                    raise FaroError(
                        f"API retornou success=false: {data}",
                        status_code=r.status_code,
                        endpoint=endpoint,
                    )
                return data
            except httpx.HTTPStatusError as e:
                raise FaroError(
                    f"HTTP {e.response.status_code} em {endpoint}: {e.response.text[:200]}",
                    status_code=e.response.status_code,
                    endpoint=endpoint,
                ) from e
            except httpx.RequestError as e:
                raise FaroError(f"Erro de rede em {endpoint}: {e}", endpoint=endpoint) from e
        return await _with_retry(_do, f"GET {endpoint}")

    async def _post(self, endpoint: str, body: dict) -> dict:
        async def _do():
            try:
                r = await self._client.post(endpoint, json=body)
                r.raise_for_status()
                data = r.json()
                if not data.get("success", True):
                    raise FaroError(
                        f"API retornou success=false: {data}",
                        status_code=r.status_code,
                        endpoint=endpoint,
                    )
                return data
            except httpx.HTTPStatusError as e:
                raise FaroError(
                    f"HTTP {e.response.status_code} em {endpoint}: {e.response.text[:200]}",
                    status_code=e.response.status_code,
                    endpoint=endpoint,
                ) from e
            except httpx.RequestError as e:
                raise FaroError(f"Erro de rede em {endpoint}: {e}", endpoint=endpoint) from e
        return await _with_retry(_do, f"POST {endpoint}")

    async def _patch(self, endpoint: str, body: dict) -> dict:
        async def _do():
            try:
                r = await self._client.patch(endpoint, json=body)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                raise FaroError(
                    f"HTTP {e.response.status_code} em {endpoint}: {e.response.text[:200]}",
                    status_code=e.response.status_code,
                    endpoint=endpoint,
                ) from e
            except httpx.RequestError as e:
                raise FaroError(f"Erro de rede em {endpoint}: {e}", endpoint=endpoint) from e
        return await _with_retry(_do, f"PATCH {endpoint}")

    # ------------------------------------------------------------------
    # Leitura de cards
    # ------------------------------------------------------------------

    async def get_card(self, card_id: str) -> dict:
        """Busca um card pelo ID. Retorna o dict do card."""
        data = await self._get("/api-cards-get", {"card_id": card_id})
        return data.get("data") or data.get("card") or data

    async def find_card_by_phone(self, phone: str) -> dict | None:
        """Busca o card pelo número de telefone. Retorna None se não encontrado."""
        try:
            data = await self._get("/api-cards-get", {
                "pipeline_id": PIPELINE_ID,
                "field_name": "Telefone",
                "field_value": phone,
            })
            cards = data.get("cards") or data.get("data", {}).get("cards", [])
            return cards[0] if cards else None
        except FaroError:
            return None

    async def get_cards_from_stage(
        self,
        stage_id: str = None,
        stage_name: str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Retorna cards de uma etapa específica (por ID ou nome)."""
        params = {"pipeline_id": PIPELINE_ID, "limit": limit, "offset": offset}
        if stage_id:
            params["stage_id"] = stage_id
        if stage_name:
            params["stage_name"] = stage_name
        data = await self._get("/api-cards-from-stage", params)
        return data.get("cards", [])

    async def get_cards_all_pages(
        self,
        stage_id: str = None,
        stage_name: str = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Busca todos os cards de uma etapa paginando automaticamente."""
        all_cards, offset = [], 0
        while True:
            batch = await self.get_cards_from_stage(
                stage_id=stage_id,
                stage_name=stage_name,
                limit=page_size,
                offset=offset,
            )
            all_cards.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return all_cards

    async def watch_new(
        self,
        stage_id: str,
        minutes_ago: int = 10,
        limit: int = 50,
    ) -> list[dict]:
        """Cards criados/movidos para a etapa nos últimos N minutos."""
        data = await self._get("/api-cards-watch-new", {
            "pipeline_id": PIPELINE_ID,
            "stage_id": stage_id,
            "minutes_ago": minutes_ago,
            "limit": limit,
        })
        return data.get("data", {}).get("cards", []) or data.get("cards", [])

    async def watch_recent(
        self,
        stage_id: str,
        hours: int = 168,
        limit: int = 50,
    ) -> list[dict]:
        """Cards com atividade recente na etapa (criações e movimentações)."""
        data = await self._get("/api-cards-watch-recent", {
            "pipeline_id": PIPELINE_ID,
            "stage_id": stage_id,
            "hours": hours,
            "limit": limit,
        })
        return data.get("data", {}).get("cards", []) or data.get("cards", [])

    async def watch_late(
        self,
        stage_id: str,
        became_late_minutes_ago: int = 60,
        limit: int = 50,
    ) -> list[dict]:
        """Cards que ficaram 'atrasados' (excederam max_days_in_stage) recentemente."""
        data = await self._get("/api-cards-watch-late", {
            "pipeline_id": PIPELINE_ID,
            "stage_id": stage_id,
            "became_late_minutes_ago": became_late_minutes_ago,
            "limit": limit,
        })
        return data.get("cards", [])

    async def check_stage_time(
        self,
        stage_id: str,
        days_threshold: int = None,
        limit: int = 50,
    ) -> list[dict]:
        """Cards que estão na etapa há mais de N dias."""
        params: dict[str, Any] = {
            "pipeline_id": PIPELINE_ID,
            "stage_id": stage_id,
            "limit": limit,
        }
        if days_threshold is not None:
            params["days_threshold"] = days_threshold
        data = await self._get("/api-cards-check-stage-time", params)
        return data.get("data", {}).get("cards", []) or data.get("cards", [])

    async def watch_done(
        self,
        stage_id: str,
        minutes_ago: int = 60,
        limit: int = 50,
    ) -> list[dict]:
        """Cards finalizados/movidos para stage final recentemente."""
        data = await self._get("/api-cards-watch-done", {
            "pipeline_id": PIPELINE_ID,
            "stage_id": stage_id,
            "minutes_ago": minutes_ago,
            "limit": limit,
        })
        return data.get("data", {}).get("cards", []) or data.get("cards", [])

    # ------------------------------------------------------------------
    # Mutação de cards
    # ------------------------------------------------------------------

    async def move_card(self, card_id: str, to_stage_id: str) -> dict:
        """Move o card para outra etapa."""
        logger.info("Movendo card %s → stage %s", card_id, to_stage_id)
        return await self._post("/api-cards-move", {
            "card_id": card_id,
            "stage_id": to_stage_id,
        })

    async def update_card(self, card_id: str, fields: dict) -> dict:
        """
        Atualiza campos do card.
        Exemplo: await faro.update_card(card_id, {"Situação": "em contato", "Ultima atividade": "123"})
        """
        logger.info("Atualizando card %s: %s", card_id, list(fields.keys()))
        return await self._patch("/api-cards-update", {
            "card_id": card_id,
            "fields": fields,
        })

    async def create_card(
        self,
        title: str,
        stage_id: str = None,
        fields: dict = None,
        description: str = None,
    ) -> dict:
        """Cria um novo card no pipeline."""
        body: dict[str, Any] = {
            "pipeline_id": PIPELINE_ID,
            "title": title,
        }
        if stage_id:
            body["stage_id"] = stage_id
        if description:
            body["description"] = description
        if fields:
            body["fields"] = fields
        return await self._post("/api-cards-create", body)


# ---------------------------------------------------------------------------
# Utilitários de campo
# ---------------------------------------------------------------------------

_HISTORY_FIELD = "Historico Conversa"
_HISTORY_MAX_TURNS = HISTORY_MAX_TURNS


def load_history(card: dict) -> list[dict]:
    """Carrega histórico de conversa do card. Retorna lista vazia se ausente."""
    import json
    raw = card.get(_HISTORY_FIELD, "") or ""
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def history_append(history: list[dict], role: str, content: str) -> list[dict]:
    """Adiciona mensagem ao histórico e limita ao máximo de turns."""
    history = history + [{"role": role, "content": content}]
    # Mantém apenas os últimos _HISTORY_MAX_TURNS * 2 itens (user + assistant)
    max_items = _HISTORY_MAX_TURNS * 2
    return history[-max_items:]


async def save_history(faro: "FaroClient", card_id: str, history: list[dict]) -> None:
    """Persiste o histórico de conversa no FARO."""
    import json
    try:
        await faro.update_card(card_id, {_HISTORY_FIELD: json.dumps(history, ensure_ascii=False)})
    except FaroError as e:
        import logging
        logging.getLogger(__name__).warning("Erro ao salvar histórico card %s: %s", card_id[:8], e)


def history_to_text(history: list[dict], max_turns: int = 10) -> str:
    """
    Converte histórico de conversa em texto legível para uso em prompts de IA.
    Usado por qualquer agente que precise de contexto da conversa anterior.
    """
    if not history:
        return "(sem histórico anterior)"
    recent = history[-(max_turns * 2):]
    lines = []
    for turn in recent:
        role = "Lead" if turn.get("role") == "user" else "Manuela"
        content = str(turn.get("content", ""))[:300]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def get_phone(card: dict) -> str | None:
    """Retorna o telefone principal do card normalizado (apenas dígitos)."""
    raw = card.get("Telefone") or card.get("Telefone alternativo") or ""
    return "".join(c for c in str(raw) if c.isdigit()) or None


def get_name(card: dict) -> str:
    """Retorna o primeiro nome do contato."""
    full = card.get("Nome do contato") or card.get("title") or "cliente"
    return full.strip().split()[0].capitalize()


def get_adm(card: dict) -> str:
    """Retorna a administradora da cota."""
    return card.get("Adm") or "sua administradora"


def get_fonte(card: dict) -> str:
    """Retorna a fonte do lead normalizada em minúsculas."""
    return (card.get("Fonte") or card.get("Etiquetas") or "").lower()


def is_lista(card: dict) -> bool:
    """Retorna True se o lead veio de uma lista fria."""
    fonte = get_fonte(card)
    return any(x in fonte for x in ["lista", "listas"])


def is_bazar(card: dict) -> bool:
    fonte = get_fonte(card)
    return "bazar" in fonte


# ---------------------------------------------------------------------------
# Contexto Jornada — snapshot acumulativo da jornada do lead
# ---------------------------------------------------------------------------

_JOURNEY_FIELD = "Contexto Jornada"


def load_journey(card: dict) -> dict:
    """Carrega o contexto de jornada do card. Retorna dict vazio se ausente."""
    import json
    raw = card.get(_JOURNEY_FIELD, "") or ""
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def save_journey(faro: "FaroClient", card_id: str, journey: dict) -> None:
    """Persiste o contexto de jornada no FARO."""
    import json
    try:
        await faro.update_card(card_id, {_JOURNEY_FIELD: json.dumps(journey, ensure_ascii=False)})
    except FaroError as e:
        import logging
        logging.getLogger(__name__).warning("Erro ao salvar jornada card %s: %s", card_id[:8], e)


def journey_to_text(journey: dict) -> str:
    """
    Formata o contexto de jornada como texto legível para prompts de IA.
    Omite campos vazios ou None.
    """
    if not journey:
        return "(sem contexto de jornada registrado)"

    _labels = {
        "origem":             "Origem",
        "adm":                "Administradora",
        "credito":            "Crédito",
        "pago_pct":           "Já pago (%)",
        "qualificado_em":     "Qualificado em",
        "proposta_inicial":   "Proposta inicial",
        "proposta_final":     "Proposta aceita",
        "num_negociacoes":    "Negociações",
        "ultima_intencao":    "Última intenção",
        "observacoes":        "Observações",
        "tom":                "Tom do lead",
    }

    lines = []
    for key, label in _labels.items():
        val = journey.get(key)
        if val is None or val == "" or val == 0:
            continue
        if key in ("credito", "proposta_inicial", "proposta_final") and isinstance(val, (int, float)):
            lines.append(f"• {label}: R$ {val:,.0f}")
        elif key == "pago_pct" and isinstance(val, (int, float)):
            lines.append(f"• {label}: {val:.0f}%")
        else:
            lines.append(f"• {label}: {val}")

    return "\n".join(lines) if lines else "(sem contexto de jornada registrado)"


# ---------------------------------------------------------------------------

_CARD_SKIP_FIELDS = {
    "id", "stage_id", "stageId", "pipeline_id", "created_at", "updated_at",
    "days_in_stage", "is_late", "days_late", "is_done", "stage",
    "Historico Conversa", "Dados Pessoais Texto", "Contexto Jornada",
}


def build_card_context(card: dict) -> str:
    """
    Retorna uma string com todos os campos não-vazios do card formatados para
    uso em prompts de IA. Campos técnicos/internos são omitidos.
    """
    lines = []
    for key, val in card.items():
        if key in _CARD_SKIP_FIELDS:
            continue
        if val is None or val == "" or val == [] or val == {}:
            continue
        lines.append(f"- {key}: {str(val)[:200]}")
    return "\n".join(lines) if lines else "- (sem dados disponíveis)"


def get_etiqueta(card: dict) -> str:
    """Retorna a etiqueta (administradora) para roteamento de número Z-API."""
    etiqueta = (card.get("Etiquetas") or "").lower()
    for key in ["itau", "itaú", "santander", "bradesco", "porto", "caixa"]:
        if key in etiqueta:
            return key.replace("itaú", "itau")
    return "default"
