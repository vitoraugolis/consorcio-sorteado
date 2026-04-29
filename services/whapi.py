"""
services/whapi.py — Cliente assíncrono para Whapi Cloud
Único provider WhatsApp do sistema (substitui Whapi Listas + Z-API Bazar/Site).

Três pools independentes:
  LISTA : tokens WHAPI_TOKEN_LISTA_1..5 — rotação aleatória anti-ban
  BAZAR : token WHAPI_TOKEN_BAZAR       — canal dedicado leads empresa parceira
  LP    : token WHAPI_TOKEN_LP          — canal dedicado leads site próprio / tráfego pago

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

from config import WHAPI_BASE_URL, WHAPI_LISTA_TOKENS, WHAPI_BAZAR_TOKEN, WHAPI_LP_TOKEN

logger = logging.getLogger(__name__)

Canal = Literal["lista", "bazar", "lp"]


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


def _build_lp_pool() -> list[str]:
    """Retorna pool LP; usa Bazar como fallback se token LP não configurado."""
    if WHAPI_LP_TOKEN:
        return [WHAPI_LP_TOKEN]
    if WHAPI_BAZAR_TOKEN:
        return [WHAPI_BAZAR_TOKEN]  # fallback para bazar (aviso já emitido no config.py)
    if WHAPI_LISTA_TOKENS:
        return WHAPI_LISTA_TOKENS
    return []


_LISTA_POOL: list[str] = WHAPI_LISTA_TOKENS
_BAZAR_POOL: list[str] = _build_bazar_pool()
_LP_POOL:    list[str] = _build_lp_pool()

# Contadores round-robin por pool (thread-safe para asyncio single-thread)
_LISTA_RR_IDX: int = 0
_BAZAR_RR_IDX: int = 0
_LP_RR_IDX:    int = 0


def _pick_token(canal: Canal) -> str:
    global _LISTA_RR_IDX, _BAZAR_RR_IDX, _LP_RR_IDX
    if canal == "lista":
        pool = _LISTA_POOL
    elif canal == "bazar":
        pool = _BAZAR_POOL
    else:
        pool = _LP_POOL
    if not pool:
        raise WhapiError(
            f"Nenhum token Whapi configurado para o canal '{canal}'. "
            f"Verifique WHAPI_TOKEN_LISTA_1 / WHAPI_TOKEN_BAZAR / WHAPI_TOKEN_LP no .env."
        )
    if canal == "lista":
        token = pool[_LISTA_RR_IDX % len(pool)]
        _LISTA_RR_IDX += 1
        idx = _LISTA_RR_IDX
    elif canal == "bazar":
        token = pool[_BAZAR_RR_IDX % len(pool)]
        _BAZAR_RR_IDX += 1
        idx = _BAZAR_RR_IDX
    else:
        token = pool[_LP_RR_IDX % len(pool)]
        _LP_RR_IDX += 1
        idx = _LP_RR_IDX
    logger.debug("WhapiClient[%s]: canal #%d token ...%s", canal, idx - 1, token[-6:])
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

    async def check_phone(self, phone: str) -> bool:
        """
        Verifica se um número tem WhatsApp ativo.
        Retorna True se existir, False se 404 (sem WA) ou erro.
        Usa GET /contacts/{phone}@s.whatsapp.net
        """
        normalized = self._normalize_phone(phone)
        jid = f"{normalized}@s.whatsapp.net"
        try:
            r = await self._client.get(f"/contacts/{jid}", timeout=10.0)
            if r.status_code == 404:
                return False
            if r.status_code == 200:
                data = r.json()
                # Se API retornar exists=false explicitamente, número não tem WA
                if isinstance(data, dict) and data.get("exists") is False:
                    return False
                return True
            # Outros erros: assumir que tem WA (fail-open, não bloqueia o lead)
            logger.warning("check_phone(%s): status inesperado %d — assumindo True", normalized[-4:], r.status_code)
            return True
        except Exception as e:
            logger.warning("check_phone(%s): erro de rede — assumindo True: %s", normalized[-4:], e)
            return True

    async def send_text(self, to: str, message: str, _log_nome: str = "", _log_card_id: str = "") -> dict:
        """Envia mensagem de texto simples."""
        phone = self._normalize_phone(to)
        logger.info("Whapi[%s] send_text → %s", self._canal, phone)
        result = await self._post("/messages/text", {"to": phone, "body": message})
        # Log no #log-cs (fire-and-forget, nunca bloqueia o fluxo)
        try:
            from services.slack import log_cs
            import asyncio
            asyncio.ensure_future(log_cs(
                direcao="enviado", canal=self._canal, phone=phone,
                nome=_log_nome, card_id=_log_card_id, mensagem=message,
            ))
        except Exception:
            pass
        return result

    async def send_buttons(
        self,
        to: str,
        message: str,
        buttons: list[dict],
        header: str = None,
        footer: str = None,
        _log_nome: str = "",
        _log_card_id: str = "",
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
        result = await self._post("/messages/interactive", body)
        try:
            from services.slack import log_cs
            import asyncio
            asyncio.ensure_future(log_cs(
                direcao="enviado", canal=self._canal, phone=phone,
                nome=_log_nome, card_id=_log_card_id, mensagem=f"[botões] {message}",
            ))
        except Exception:
            pass
        return result

    async def send_list(
        self,
        to: str,
        message: str,
        button_label: str,
        sections: list[dict],
        header: str = None,
        footer: str = None,
        _log_nome: str = "",
        _log_card_id: str = "",
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
        result = await self._post("/messages/interactive/list", body)
        try:
            from services.slack import log_cs
            import asyncio
            asyncio.ensure_future(log_cs(
                direcao="enviado", canal=self._canal, phone=phone,
                nome=_log_nome, card_id=_log_card_id, mensagem=f"[lista] {message}",
            ))
        except Exception:
            pass
        return result

    async def send_image(self, to: str, image_url: str, caption: str = "", _log_nome: str = "", _log_card_id: str = "") -> dict:
        """Envia imagem com legenda opcional."""
        phone = self._normalize_phone(to)
        logger.info("Whapi[%s] send_image → %s", self._canal, phone)
        result = await self._post("/messages/image", {
            "to": phone,
            "media": image_url,
            "caption": caption,
        })
        try:
            from services.slack import log_cs
            import asyncio
            asyncio.ensure_future(log_cs(
                direcao="enviado", canal=self._canal, phone=phone,
                nome=_log_nome, card_id=_log_card_id, mensagem=f"[imagem] {caption}",
            ))
        except Exception:
            pass
        return result

    async def send_document(
        self,
        to: str,
        document_url: str,
        filename: str = "documento.pdf",
        caption: str = "",
        _log_nome: str = "",
        _log_card_id: str = "",
    ) -> dict:
        """Envia documento (PDF, etc.)."""
        phone = self._normalize_phone(to)
        logger.info("Whapi[%s] send_document → %s (%s)", self._canal, phone, filename)
        result = await self._post("/messages/document", {
            "to": phone,
            "media": document_url,
            "filename": filename,
            "caption": caption,
        })
        try:
            from services.slack import log_cs
            import asyncio
            asyncio.ensure_future(log_cs(
                direcao="enviado", canal=self._canal, phone=phone,
                nome=_log_nome, card_id=_log_card_id, mensagem=f"[doc] {filename}",
            ))
        except Exception:
            pass
        return result


# ---------------------------------------------------------------------------
# Função de roteamento automático por card
# ---------------------------------------------------------------------------

def get_whapi_for_card(card: dict) -> WhapiClient:
    """
    Retorna WhapiClient com o canal correto baseado na origem do lead.
    - Lead de Lista  → canal "lista" (pool anti-ban)
    - Lead Bazar     → canal "bazar" (token dedicado empresa parceira)
    - Lead LP/Site   → canal "lp"    (token dedicado site próprio / tráfego pago)

    Uso:
        async with get_whapi_for_card(card) as w:
            await w.send_text(phone, mensagem)
    """
    from services.faro import get_canal
    origem = get_canal(card)
    if origem == "lista":
        canal: Canal = "lista"
    elif origem == "lp":
        canal = "lp"
    else:
        canal = "bazar"
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
