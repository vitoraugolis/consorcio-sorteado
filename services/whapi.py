"""
services/whapi.py — Cliente assíncrono para Whapi Cloud
Único provider WhatsApp do sistema (substitui Whapi Listas + Z-API Bazar/Site).

Dois pools independentes:
  LISTA : tokens WHAPI_TOKEN_LISTA_1..5 — rotação aleatória anti-ban
  BAZAR : token WHAPI_TOKEN_BAZAR       — canal dedicado leads orgânicos

Uso:
    # Roteamento automático pelo card (recomendado):
    async with get_whapi_for_card(card) as w:
        await w.send_text(phone, "Olá!")

    # Pool explícito:
    async with WhapiClient(canal="lista") as w:
        await w.send_text(phone, "Mensagem de lista")
"""

import logging
import random
from typing import Any, Literal

import httpx

from config import WHAPI_BASE_URL, WHAPI_LISTA_TOKENS, WHAPI_BAZAR_TOKEN

logger = logging.getLogger(__name__)

Canal = Literal["lista", "bazar"]


# ---------------------------------------------------------------------------
# Pools de tokens
# ---------------------------------------------------------------------------

def _build_bazar_pool() -> list[str]:
    """Retorna pool Bazar; usa lista como fallback se token Bazar não configurado."""
    if WHAPI_BAZAR_TOKEN:
        return [WHAPI_BAZAR_TOKEN]
    if WHAPI_LISTA_TOKENS:
        return WHAPI_LISTA_TOKENS  # fallback silencioso (aviso já emitido no config.py)
    return []


_LISTA_POOL: list[str] = WHAPI_LISTA_TOKENS
_BAZAR_POOL: list[str] = _build_bazar_pool()

# Contadores round-robin por pool (thread-safe para asyncio single-thread)
_LISTA_RR_IDX: int = 0
_BAZAR_RR_IDX: int = 0


def _pick_token(canal: Canal) -> str:
    global _LISTA_RR_IDX, _BAZAR_RR_IDX
    pool = _LISTA_POOL if canal == "lista" else _BAZAR_POOL
    if not pool:
        raise WhapiError(
            f"Nenhum token Whapi configurado para o canal '{canal}'. "
            f"Verifique WHAPI_TOKEN_LISTA_1 / WHAPI_TOKEN_BAZAR no .env."
        )
    if canal == "lista":
        token = pool[_LISTA_RR_IDX % len(pool)]
        _LISTA_RR_IDX += 1
    else:
        token = pool[_BAZAR_RR_IDX % len(pool)]
        _BAZAR_RR_IDX += 1
    logger.debug("WhapiClient[%s]: canal #%d token ...%s",
                 canal, (_LISTA_RR_IDX if canal == "lista" else _BAZAR_RR_IDX) - 1, token[-6:])
    return token


# ---------------------------------------------------------------------------
# Exceção
# ---------------------------------------------------------------------------

class WhapiError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

class WhapiClient:
    """
    Cliente assíncrono Whapi Cloud com seleção de pool por canal.

    Parâmetros:
        canal  : "lista" (padrão) ou "bazar" — determina qual pool usar
        token  : força um token específico (ignora canal e pool)
    """

    def __init__(self, canal: Canal = "lista", token: str = None):
        chosen = token or _pick_token(canal)
        self._canal = canal
        self._client = httpx.AsyncClient(
            base_url=WHAPI_BASE_URL,
            headers={
                "Authorization": f"Bearer {chosen}",
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
    # Helpers
    # ------------------------------------------------------------------

    def _normalize_phone(self, phone: str) -> str:
        digits = "".join(c for c in phone if c.isdigit())
        if not digits.startswith("55"):
            digits = "55" + digits
        return digits

    async def _post(self, endpoint: str, body: dict) -> dict:
        try:
            r = await self._client.post(endpoint, json=body)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise WhapiError(
                f"HTTP {e.response.status_code} em {endpoint}: {e.response.text[:300]}",
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            raise WhapiError(f"Erro de rede em {endpoint}: {e}") from e

    # ------------------------------------------------------------------
    # Envio de mensagens
    # ------------------------------------------------------------------

    async def health_check(self) -> tuple[bool, str]:
        """
        Verifica se o canal está respondendo e conectado.
        Retorna (online, status_text).
        - online=True apenas se HTTP 200 E status não indica desconexão (QR, unpaired, loading)
        """
        _OFFLINE_STATUSES = {"qr", "unpaired", "loading", "unknown", "init"}
        try:
            r = await self._client.get("/health", timeout=10.0)
            data = r.json()
            status_text = (data.get("status", {}).get("text") or "UNKNOWN").lower()
            if r.status_code != 200:
                return False, status_text.upper()
            # Canal conectado = não está em estado de desconexão
            online = status_text not in _OFFLINE_STATUSES
            return online, status_text.upper()
        except Exception as e:
            return False, f"ERRO: {e}"

    async def send_text(self, to: str, message: str) -> dict:
        """Envia mensagem de texto simples."""
        phone = self._normalize_phone(to)
        logger.info("Whapi[%s] send_text → %s", self._canal, phone)
        return await self._post("/messages/text", {"to": phone, "body": message})

    async def send_buttons(
        self,
        to: str,
        message: str,
        buttons: list[dict],
        header: str = None,
        footer: str = None,
    ) -> dict:
        """Envia mensagem interativa com botões de resposta rápida (máx 3)."""
        phone = self._normalize_phone(to)
        logger.info("Whapi[%s] send_buttons → %s (%d botões)", self._canal, phone, len(buttons))
        body: dict[str, Any] = {
            "to": phone,
            "type": "button",
            "body": {"text": message},
            "action": {
                "buttons": [
                    {
                        "type": "quick_reply",
                        "id": b["id"],
                        "title": b.get("title") or b.get("label"),
                    }
                    for b in buttons
                ]
            },
        }
        if header:
            body["header"] = {"type": "text", "text": header}
        if footer:
            body["footer"] = footer
        return await self._post("/messages/interactive", body)

    async def send_list(
        self,
        to: str,
        message: str,
        button_label: str,
        sections: list[dict],
        header: str = None,
        footer: str = None,
    ) -> dict:
        """Envia mensagem com lista de opções."""
        phone = self._normalize_phone(to)
        logger.info("Whapi[%s] send_list → %s", self._canal, phone)
        body: dict[str, Any] = {
            "to": phone,
            "body": message,
            "action": {"button": button_label, "sections": sections},
        }
        if header:
            body["header"] = {"type": "text", "text": header}
        if footer:
            body["footer"] = footer
        return await self._post("/messages/interactive/list", body)

    async def send_image(self, to: str, image_url: str, caption: str = "") -> dict:
        """Envia imagem com legenda opcional."""
        phone = self._normalize_phone(to)
        logger.info("Whapi[%s] send_image → %s", self._canal, phone)
        return await self._post("/messages/image", {
            "to": phone,
            "media": image_url,
            "caption": caption,
        })

    async def send_document(
        self,
        to: str,
        document_url: str,
        filename: str = "documento.pdf",
        caption: str = "",
    ) -> dict:
        """Envia documento (PDF, etc.)."""
        phone = self._normalize_phone(to)
        logger.info("Whapi[%s] send_document → %s (%s)", self._canal, phone, filename)
        return await self._post("/messages/document", {
            "to": phone,
            "media": document_url,
            "filename": filename,
            "caption": caption,
        })


# ---------------------------------------------------------------------------
# Função de roteamento automático por card
# ---------------------------------------------------------------------------

def get_whapi_for_card(card: dict) -> WhapiClient:
    """
    Retorna WhapiClient com o canal correto baseado na origem do lead.
    - Lead de Lista  → canal "lista" (pool anti-ban)
    - Lead Bazar/Site → canal "bazar" (token dedicado)

    Uso:
        async with get_whapi_for_card(card) as w:
            await w.send_text(phone, mensagem)
    """
    from services.faro import is_lista
    canal: Canal = "lista" if is_lista(card) else "bazar"
    return WhapiClient(canal=canal)


# ---------------------------------------------------------------------------
# Notificação centralizada para equipe (grupo Alarmes Sistemas CS)
# ---------------------------------------------------------------------------

async def notify_team(message: str) -> None:
    """
    Envia notificação para o grupo de alarmes/equipe comercial.
    Usa canal Bazar (número 8087) para enviar ao grupo.
    Fallback: envia para NOTIFY_PHONES se grupo não configurado.
    """
    from config import NOTIFY_GROUP, NOTIFY_PHONES
    import logging
    _log = logging.getLogger(__name__)

    if NOTIFY_GROUP:
        try:
            async with WhapiClient(canal="bazar") as w:
                await w.send_text(NOTIFY_GROUP, message)
            return
        except WhapiError as e:
            _log.warning("notify_team: falha ao enviar para grupo (%s), tentando NOTIFY_PHONES: %s", NOTIFY_GROUP, e)

    # Fallback: NOTIFY_PHONES
    if NOTIFY_PHONES:
        try:
            async with WhapiClient(canal="lista") as w:
                for ph in NOTIFY_PHONES:
                    await w.send_text(ph, message)
        except WhapiError as e:
            _log.error("notify_team: falha total ao notificar equipe: %s", e)
