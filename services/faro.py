"""
services/faro.py — Cliente assíncrono para a API do FARO CRM
Todos os acessos ao CRM passam por aqui. Nunca chame a API diretamente nos jobs.
"""

import asyncio
import logging
from typing import Any

import httpx

from config import FARO_API_KEY, FARO_BASE_URL, PIPELINE_ID, HISTORY_MAX_TURNS, TERMINAL_STAGES

logger = logging.getLogger(__name__)

_RETRY_DELAYS = (1.0, 3.0)


async def _with_retry(coro_fn, label: str):
    last_exc = None
    for attempt, delay in enumerate((_RETRY_DELAYS[0], _RETRY_DELAYS[1], None), start=1):
        try:
            return await coro_fn()
        except FaroError as e:
            if e.status_code and 400 <= e.status_code < 500:
                raise
            last_exc = e
            if delay is not None:
                logger.warning("FARO %s: tentativa %d falhou (%s). Retry em %.0fs...", label, attempt, e, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("FARO %s: todas as tentativas esgotadas. Ultimo erro: %s", label, e)
    try:
        from services.slack import slack_error as _slack_err
        asyncio.create_task(_slack_err(
            f"FARO indisponivel apos 3 tentativas - endpoint: {label}",
            context={"endpoint": label}
        ))
    except Exception:
        pass
    raise last_exc


def is_pj(card: dict) -> bool:
    """Retorna True se o card pertence a Pessoa Juridica. Single source of truth."""
    tipo = str(card.get("Tipo Pessoa") or "").strip().upper()
    return tipo in ("PJ", "CNPJ", "PESSOA JURIDICA")


class FaroError(Exception):
    def __init__(self, message: str, status_code: int = 0, endpoint: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint



class FaroClient:
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
        """PATCH com validacao de success=false (fix: versao anterior nao validava)."""
        async def _do():
            try:
                r = await self._client.patch(endpoint, json=body)
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
        return await _with_retry(_do, f"PATCH {endpoint}")

    # ------------------------------------------------------------------
    # Leitura de cards
    # ------------------------------------------------------------------

    async def get_card(self, card_id: str) -> dict:
        data = await self._get("/api-cards-get", {"card_id": card_id})
        return data.get("data") or data.get("card") or data

    async def find_card_by_phone(self, phone: str, canal_hint: str = "") -> dict | None:
        """
        Busca card pelo telefone. Quando ha duplicatas (mesmo numero em varios cards),
        usa desempate inteligente:
          1. Descarta stages terminais (Perdido, Nao Qualificado, etc.)
          2. Se canal_hint fornecido ("bazar", "lp", "lista"), prefere card cuja Fonte bate
          3. Entre restantes, prefere o mais recentemente atualizado
        """
        digits = "".join(c for c in phone if c.isdigit())
        variants = list(dict.fromkeys([phone, digits, f"+{digits}"]))

        all_cards: list[dict] = []
        seen_ids: set[str] = set()

        for variant in variants:
            try:
                data = await self._get("/api-cards-get", {
                    "pipeline_id": PIPELINE_ID,
                    "field_name": "Telefone",
                    "field_value": variant,
                })
                if data.get("id"):
                    cid = data["id"]
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        all_cards.append(data)
                else:
                    cards = data.get("cards") or data.get("data", {}).get("cards", [])
                    for c in (cards or []):
                        cid = c.get("id", "")
                        if cid and cid not in seen_ids:
                            seen_ids.add(cid)
                            all_cards.append(c)
            except FaroError:
                continue

        if not all_cards:
            return None
        if len(all_cards) == 1:
            return all_cards[0]

        # Desempate: descarta stages terminais (TERMINAL_STAGES centralizado em config.py)
        ativos = [c for c in all_cards if c.get("stage_id") not in TERMINAL_STAGES]
        candidatos = ativos if ativos else all_cards

        if canal_hint and len(candidatos) > 1:
            hint = canal_hint.lower()
            _FONTE_MAP = {
                "bazar": ["bazar", "bazar do consorcio"],
                "lp":    ["lp", "site", "landing page"],
                "lista": ["lista", "list"],
            }
            tokens = _FONTE_MAP.get(hint, [hint])
            match_canal = [
                c for c in candidatos
                if any(t in (c.get("Fonte") or "").lower() for t in tokens)
            ]
            if match_canal:
                candidatos = match_canal

        def _recency(c: dict) -> str:
            return c.get("updated_at") or c.get("created_at") or ""

        candidatos.sort(key=_recency, reverse=True)
        if len(all_cards) > 1:
            import logging as _log
            _log.getLogger(__name__).warning(
                "find_card_by_phone: %d cards para telefone %s - usando [%s] Fonte='%s' (hint='%s', descartados: %s)",
                len(all_cards), phone[-4:],
                candidatos[0].get("id", "")[:8],
                candidatos[0].get("Fonte", ""),
                canal_hint,
                [c.get("id", "")[:8] for c in all_cards if c.get("id") != candidatos[0].get("id")],
            )
        return candidatos[0]

    async def find_all_cards_by_phone(self, phone: str) -> list[dict]:
        """
        Busca TODOS os cards ativos associados ao telefone, em todos os canais.
        Retorna lista deduplicada por card id, excluindo stages terminais.
        """
        seen_ids: set[str] = set()
        all_cards: list[dict] = []
        digits = "".join(c for c in phone if c.isdigit())
        variants = list(dict.fromkeys([phone, digits, f"+{digits}"]))

        for variant in variants:
            try:
                data = await self._get("/api-cards-get", {
                    "pipeline_id": PIPELINE_ID,
                    "field_name": "Telefone",
                    "field_value": variant,
                })
                if data.get("id"):
                    cid = data["id"]
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        all_cards.append(data)
                else:
                    cards = data.get("cards") or data.get("data", {}).get("cards", [])
                    for c in (cards or []):
                        cid = c.get("id", "")
                        if cid and cid not in seen_ids:
                            seen_ids.add(cid)
                            all_cards.append(c)
            except FaroError:
                continue

        # Exclui stages terminais (TERMINAL_STAGES centralizado em config.py)
        return [c for c in all_cards if c.get("stage_id") not in TERMINAL_STAGES]

    async def get_cards_from_stage(
        self,
        stage_id: str = None,
        stage_name: str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
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

    async def watch_new(self, stage_id: str, minutes_ago: int = 10, limit: int = 50) -> list[dict]:
        data = await self._get("/api-cards-watch-new", {
            "pipeline_id": PIPELINE_ID,
            "stage_id": stage_id,
            "minutes_ago": minutes_ago,
            "limit": limit,
        })
        return data.get("data", {}).get("cards", []) or data.get("cards", [])

    async def watch_recent(self, stage_id: str, hours: int = 168, limit: int = 50) -> list[dict]:
        data = await self._get("/api-cards-watch-recent", {
            "pipeline_id": PIPELINE_ID,
            "stage_id": stage_id,
            "hours": hours,
            "limit": limit,
        })
        return data.get("data", {}).get("cards", []) or data.get("cards", [])

    async def watch_late(self, stage_id: str, became_late_minutes_ago: int = 60, limit: int = 50) -> list[dict]:
        data = await self._get("/api-cards-watch-late", {
            "pipeline_id": PIPELINE_ID,
            "stage_id": stage_id,
            "became_late_minutes_ago": became_late_minutes_ago,
            "limit": limit,
        })
        return data.get("cards", [])

    async def check_stage_time(self, stage_id: str, days_threshold: int = None, limit: int = 50) -> list[dict]:
        params: dict[str, Any] = {"pipeline_id": PIPELINE_ID, "stage_id": stage_id, "limit": limit}
        if days_threshold is not None:
            params["days_threshold"] = days_threshold
        data = await self._get("/api-cards-check-stage-time", params)
        return data.get("data", {}).get("cards", []) or data.get("cards", [])

    async def watch_done(self, stage_id: str, minutes_ago: int = 60, limit: int = 50) -> list[dict]:
        data = await self._get("/api-cards-watch-done", {
            "pipeline_id": PIPELINE_ID,
            "stage_id": stage_id,
            "minutes_ago": minutes_ago,
            "limit": limit,
        })
        return data.get("data", {}).get("cards", []) or data.get("cards", [])

    # ------------------------------------------------------------------
    # Mutacao de cards
    # ------------------------------------------------------------------

    async def move_card(self, card_id: str, to_stage_id: str) -> dict:
        logger.info("Movendo card %s para stage %s", card_id, to_stage_id)
        return await self._post("/api-cards-move", {
            "card_id": card_id,
            "stage_id": to_stage_id,
        })

    async def update_card(self, card_id: str, fields: dict) -> dict:
        logger.info("Atualizando card %s: %s", card_id, list(fields.keys()))
        return await self._patch("/api-cards-update", {
            "card_id": card_id,
            "fields": fields,
        })

    async def create_card(self, title: str, stage_id: str = None, fields: dict = None, description: str = None) -> dict:
        body: dict[str, Any] = {"pipeline_id": PIPELINE_ID, "title": title}
        if stage_id:
            body["stage_id"] = stage_id
        if description:
            body["description"] = description
        if fields:
            body["fields"] = fields
        return await self._post("/api-cards-create", body)


# ---------------------------------------------------------------------------
# Utilitarios de campo
# ---------------------------------------------------------------------------

_HISTORY_FIELD = "Historico Conversa"
_HISTORY_MAX_TURNS = HISTORY_MAX_TURNS


def load_history(card: dict) -> list[dict]:
    import json
    raw = card.get(_HISTORY_FIELD, "") or ""
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def history_append(history: list[dict], role: str, content: str) -> list[dict]:
    history = history + [{"role": role, "content": content}]
    max_items = _HISTORY_MAX_TURNS * 2
    return history[-max_items:]


async def save_history(faro: "FaroClient", card_id: str, history: list[dict]) -> None:
    """Persiste o historico. Deve ser chamado com o FaroClient ainda aberto."""
    import json
    try:
        await faro.update_card(card_id, {_HISTORY_FIELD: json.dumps(history, ensure_ascii=False)})
    except FaroError as e:
        logging.getLogger(__name__).warning("Erro ao salvar historico card %s: %s", card_id[:8], e)


def history_to_text(history: list[dict], max_turns: int = 10) -> str:
    if not history:
        return "(sem historico anterior)"
    recent = history[-(max_turns * 2):]
    lines = []
    for turn in recent:
        role = "Lead" if turn.get("role") == "user" else "Manuela"
        content = str(turn.get("content", ""))[:300]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def get_phone(card: dict) -> str | None:
    raw = card.get("Telefone") or card.get("Telefone alternativo") or ""
    return "".join(c for c in str(raw) if c.isdigit()) or None


def get_name(card: dict) -> str:
    full = card.get("Nome do contato") or card.get("title") or "cliente"
    return full.strip().split()[0].capitalize()


def get_adm(card: dict) -> str:
    return card.get("Adm") or "sua administradora"


def get_fonte(card: dict) -> str:
    return (card.get("Fonte") or card.get("Etiquetas") or "").lower()


def is_lista(card: dict) -> bool:
    """
    Retorna True se o lead veio de uma lista fria (usa Whapi pool Lista).
    Retorna False para leads organicos Bazar/Site (usa Whapi pool Bazar).
    """
    fonte = str(card.get("Fonte") or "").strip().lower()
    etiqueta = str(card.get("Etiquetas") or "").strip().lower()
    return "lista" in fonte or "lista" in etiqueta


def is_bazar(card: dict) -> bool:
    fonte = str(card.get("Fonte") or "").strip().lower()
    return "bazar" in fonte


def get_canal(card: dict) -> str:
    """Retorna 'lista', 'bazar', 'lp' ou 'desconhecido'. Usado para roteamento e logging."""
    fonte = str(card.get("Fonte") or "").strip().lower()
    if "lista" in fonte:
        return "lista"
    if "bazar" in fonte:
        return "bazar"
    if "site" in fonte or "lp" in fonte:
        return "lp"
    return "desconhecido"


def get_etiqueta(card: dict) -> str:
    """Retorna a etiqueta normalizada para fins de roteamento/logging."""
    etiqueta = (card.get("Etiquetas") or "").lower()
    for key in ["itau", "santander", "bradesco", "porto", "caixa"]:
        if key in etiqueta:
            return key
    return "default"


# ---------------------------------------------------------------------------
# Contexto Jornada
# ---------------------------------------------------------------------------

_JOURNEY_FIELD = "Contexto Jornada"


def load_journey(card: dict) -> dict:
    import json
    raw = card.get(_JOURNEY_FIELD, "") or ""
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def save_journey(faro: "FaroClient", card_id: str, journey: dict) -> None:
    """Persiste o contexto de jornada. Deve ser chamado com o FaroClient ainda aberto."""
    import json
    try:
        await faro.update_card(card_id, {_JOURNEY_FIELD: json.dumps(journey, ensure_ascii=False)})
    except FaroError as e:
        logging.getLogger(__name__).warning("Erro ao salvar jornada card %s: %s", card_id[:8], e)


def journey_to_text(journey: dict) -> str:
    if not journey:
        return "(sem contexto de jornada registrado)"
    _labels = {
        "origem": "Origem",
        "adm": "Administradora",
        "credito": "Credito",
        "pago_pct": "Ja pago (%)",
        "qualificado_em": "Qualificado em",
        "proposta_inicial": "Proposta inicial",
        "proposta_final": "Proposta aceita",
        "num_negociacoes": "Negociacoes",
        "ultima_intencao": "Ultima intencao",
        "observacoes": "Observacoes",
        "tom": "Tom do lead",
    }
    lines = []
    for key, label in _labels.items():
        val = journey.get(key)
        if val is None or val == "" or val == 0:
            continue
        if key in ("credito", "proposta_inicial", "proposta_final") and isinstance(val, (int, float)):
            lines.append(f"* {label}: R$ {val:,.0f}")
        elif key == "pago_pct" and isinstance(val, (int, float)):
            lines.append(f"* {label}: {val:.0f}%")
        else:
            lines.append(f"* {label}: {val}")
    return "\n".join(lines) if lines else "(sem contexto de jornada registrado)"


# ---------------------------------------------------------------------------
# Contexto do card para prompts de IA
# ---------------------------------------------------------------------------

_CARD_SKIP_FIELDS = {
    "id", "stage_id", "stageId", "pipeline_id", "created_at", "updated_at",
    "days_in_stage", "is_late", "days_late", "is_done", "stage",
    "Historico Conversa", "Dados Pessoais Texto", "Contexto Jornada",
}


def build_card_context(card: dict) -> str:
    lines = []
    for key, val in card.items():
        if key in _CARD_SKIP_FIELDS:
            continue
        if val is None or val == "" or val == [] or val == {}:
            continue
        lines.append(f"- {key}: {str(val)[:200]}")
    return "\n".join(lines) if lines else "- (sem dados disponíveis)"


import logging  # noqa: E402 - necessario para uso nos helpers acima
