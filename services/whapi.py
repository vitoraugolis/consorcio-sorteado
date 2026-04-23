"""
services/whapi.py — Cliente assíncrono para Whapi Cloud
Usado no fluxo de Listas (leads frios em lote).
Documentação: https://whapi.cloud/docs

Suporte a dois canais com rotação aleatória:
  - Canal primário:    WHAPI_TOKEN   (FALCON-9TE4X)
  - Canal secundário:  WHAPI_TOKEN_2 (DAREDL-F4375)

A rotação entre canais distribui os envios e reduz o risco de ban em
listas frias de alto volume. Se apenas WHAPI_TOKEN estiver configurado,
o sistema usa somente o canal primário.
"""

import logging
import random
from typing import Any

import httpx

from config import WHAPI_TOKEN, WHAPI_TOKEN_2, WHAPI_BASE_URL

logger = logging.getLogger(__name__)


# Pool de tokens disponíveis (primário sempre presente; secundário opcional)
_TOKEN_POOL: list[str] = [t for t in [WHAPI_TOKEN, WHAPI_TOKEN_2] if t]


def _pick_token() -> str:
    """Seleciona aleatoriamente um token do pool para distribuir a carga."""
    return random.choice(_TOKEN_POOL)


class WhapiError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class WhapiClient:
    """
    Cliente assíncrono para Whapi Cloud com rotação de canais.

    Por padrão seleciona aleatoriamente entre os canais disponíveis.
    Passe token= para forçar um canal específico.

    Uso:
        async with WhapiClient() as whapi:
            await whapi.send_text("5511999999999", "Olá!")
    """

    def __init__(self, token: str = None):
        chosen = token or _pick_token()
        logger.debug("WhapiClient: usando canal ...%s", chosen[-6:])
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
        """
        Garante que o número está no formato internacional sem '+'.
        Entrada: '11 99999-9999', '+5511999999999', '5511999999999'
        Saída:   '5511999999999@s.whatsapp.net'
        """
        digits = "".join(c for c in phone if c.isdigit())
        if not digits.startswith("55"):
            digits = "55" + digits
        # Whapi aceita número puro — o @s.whatsapp.net é adicionado internamente
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

    async def send_text(self, to: str, message: str) -> dict:
        """Envia mensagem de texto simples."""
        phone = self._normalize_phone(to)
        logger.info("Whapi send_text → %s", phone)
        return await self._post("/messages/text", {
            "to": phone,
            "body": message,
        })

    async def send_buttons(
        self,
        to: str,
        message: str,
        buttons: list[dict],
        header: str = None,
        footer: str = None,
    ) -> dict:
        """
        Envia mensagem interativa com botões de resposta rápida.

        buttons: lista de {"id": "btn_id", "title": "Label"}
        Máximo 3 botões pelo WhatsApp.

        Endpoint Whapi: POST /messages/interactive
        Formato: type=button, action.buttons com type=quick_reply
        """
        phone = self._normalize_phone(to)
        logger.info("Whapi send_buttons → %s (%d botões)", phone, len(buttons))

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
        """
        Envia mensagem com lista de opções.

        sections: [{"title": "Seção", "rows": [{"id": "...", "title": "...", "description": "..."}]}]
        """
        phone = self._normalize_phone(to)
        logger.info("Whapi send_list → %s", phone)

        body: dict[str, Any] = {
            "to": phone,
            "body": message,
            "action": {
                "button": button_label,
                "sections": sections,
            },
        }
        if header:
            body["header"] = {"type": "text", "text": header}
        if footer:
            body["footer"] = footer

        return await self._post("/messages/interactive/list", body)

    async def send_image(
        self,
        to: str,
        image_url: str,
        caption: str = "",
    ) -> dict:
        """Envia imagem com legenda opcional."""
        phone = self._normalize_phone(to)
        logger.info("Whapi send_image → %s", phone)
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
        logger.info("Whapi send_document → %s (%s)", phone, filename)
        return await self._post("/messages/document", {
            "to": phone,
            "media": document_url,
            "filename": filename,
            "caption": caption,
        })
