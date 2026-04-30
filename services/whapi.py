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
    """Retorna pool LP (DEADPL-V592K). Sem fallback — token LP é obrigatório."""
    if WHAPI_LP_TOKEN:
        return [WHAPI_LP_TOKEN]
    logger.warning("WHAPI_TOKEN_LP não configurado — leads LP sem canal de envio!")
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
# Helpers
# ---------------------------------------------------------------------------

def _is_lead_recipient(to: str) -> bool:
    """
    Retorna True se o destinatário é um lead (deve aparecer no #log-cs).
    Filtra grupos (@g.us) e números internos da equipe (NOTIFY_PHONES).
    """
    if "@g.us" in str(to):
        return False
    try:
        from config import NOTIFY_PHONES
        digits = "".join(c for c in str(to) if c.isdigit())
        for internal in NOTIFY_PHONES.values():
            if digits.endswith(internal) or internal.endswith(digits):
                return False
    except Exception:
        pass
    return True


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
        Usa GET /contacts/{phone} (só dígitos, sem @s.whatsapp.net)
        """
        normalized = self._normalize_phone(phone)
        try:
            r = await self._client.get(f"/contacts/{normalized}", timeout=10.0)
            if r.status_code == 404:
                return False
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("exists") is False:
                    return False
                return True
            # Outros erros inesperados: fail-open (não bloqueia o lead)
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
        # Log no #log-cs — ignora grupos (@g.us) e números internos da equipe
        if _is_lead_recipient(to):
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
        if _is_lead_recipient(to):
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
        if _is_lead_recipient(to):
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
        if _is_lead_recipient(to):
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
        if _is_lead_recipient(to):
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
# Resolução de telefone com fallback automático
# ---------------------------------------------------------------------------

async def resolve_phone(card: dict, canal: "Canal" = "lp") -> str | None:
    """
    Verifica qual número do card tem WhatsApp ativo e retorna o válido.

    Lógica:
      1. Testa `Telefone` principal
      2. Se não tiver WA → testa `Telefone alternativo`
      3. Se o alternativo funcionar → corrige `Telefone` no FARO automaticamente
      4. Se nenhum funcionar → retorna None

    Fail-open: se check_phone falhar por erro de rede, retorna o telefone principal
    sem corrigir (não bloqueia o fluxo).
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    def _digits(v: str) -> str:
        return "".join(c for c in str(v or "") if c.isdigit())

    phone_principal = _digits(card.get("Telefone") or "")
    phone_alt       = _digits(card.get("Telefone alternativo") or "")
    card_id         = card.get("id", "")

    if not phone_principal and not phone_alt:
        return None

    async with WhapiClient(canal=canal) as w:
        if phone_principal:
            ok = await w.check_phone(phone_principal)
            if ok:
                return phone_principal

            _logger.info(
                "resolve_phone: %s sem WA em principal (%s) — tentando alternativo (%s)",
                card_id[:8], phone_principal[-4:], phone_alt[-4:] if phone_alt else "N/A",
            )

            if phone_alt:
                ok_alt = await w.check_phone(phone_alt)
                if ok_alt:
                    _logger.info(
                        "resolve_phone: alternativo (%s) tem WA — corrigindo Telefone no FARO",
                        phone_alt[-4:],
                    )
                    # Corrige no FARO: swap principal ↔ alternativo
                    try:
                        from services.faro import FaroClient, FaroError
                        async with FaroClient() as faro:
                            await faro.update_card(card_id, {
                                "Telefone":              phone_alt,
                                "Telefone alternativo":  phone_principal,
                            })
                        card["Telefone"] = phone_alt
                        card["Telefone alternativo"] = phone_principal
                    except Exception as _e:
                        _logger.warning("resolve_phone: erro ao corrigir FARO card %s: %s", card_id[:8], _e)
                    return phone_alt

            _logger.warning(
                "resolve_phone: card %s — nenhum número tem WA (principal=%s, alt=%s)",
                card_id[:8], phone_principal[-4:], phone_alt[-4:] if phone_alt else "N/A",
            )
            return None

        # Só tem alternativo
        ok_alt = await w.check_phone(phone_alt)
        return phone_alt if ok_alt else None


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
