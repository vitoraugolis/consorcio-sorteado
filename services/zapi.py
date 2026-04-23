"""
services/zapi.py — Cliente assíncrono para Z-API
Usado nos fluxos Bazar e Site (leads orgânicos, volume intermitente).
Documentação: https://developer.z-api.io/
"""

import logging
from typing import Any

import httpx

from config import ZAPI_BASE_URL, ZAPI_INSTANCES

logger = logging.getLogger(__name__)


class ZAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class ZAPIClient:
    """
    Cliente assíncrono para Z-API.

    Cada instância Z-API corresponde a um número WhatsApp conectado.
    O cliente é instanciado com uma chave de instância configurada no config.py.

    Uso:
        zapi = ZAPIClient(instance_key="bazar")
        await zapi.send_text("5511999999999", "Olá!")
        await zapi.aclose()

    Ou via context manager:
        async with ZAPIClient("bazar") as zapi:
            await zapi.send_text(...)
    """

    def __init__(self, instance_key: str = "default"):
        instance = ZAPI_INSTANCES.get(instance_key) or ZAPI_INSTANCES.get("default")
        if not instance:
            raise ZAPIError(
                f"Instância Z-API '{instance_key}' não configurada. "
                f"Adicione ZAPI_INSTANCE_{instance_key.upper()}=<id>:<token> no .env"
            )
        self.instance_id = instance["instance_id"]
        self.token = instance["token"]
        self._base = f"{ZAPI_BASE_URL}/{self.instance_id}/token/{self.token}"
        self._client = httpx.AsyncClient(
            headers={"Content-Type": "application/json"},
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
        """Remove tudo exceto dígitos, garante prefixo 55."""
        digits = "".join(c for c in phone if c.isdigit())
        if not digits.startswith("55"):
            digits = "55" + digits
        return digits

    async def _post(self, endpoint: str, body: dict) -> dict:
        url = f"{self._base}/{endpoint}"
        try:
            r = await self._client.post(url, json=body)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ZAPIError(
                f"HTTP {e.response.status_code} em {endpoint}: {e.response.text[:300]}",
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            raise ZAPIError(f"Erro de rede em {endpoint}: {e}") from e

    async def _get(self, endpoint: str) -> dict:
        url = f"{self._base}/{endpoint}"
        try:
            r = await self._client.get(url)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ZAPIError(
                f"HTTP {e.response.status_code} em {endpoint}: {e.response.text[:300]}",
                status_code=e.response.status_code,
            ) from e

    # ------------------------------------------------------------------
    # Status da instância
    # ------------------------------------------------------------------

    async def get_status(self) -> dict:
        """Retorna o status de conexão da instância Z-API."""
        return await self._get("status")

    async def is_connected(self) -> bool:
        """Retorna True se o número está conectado."""
        try:
            status = await self.get_status()
            return status.get("connected", False)
        except ZAPIError:
            return False

    # ------------------------------------------------------------------
    # Envio de mensagens
    # ------------------------------------------------------------------

    async def send_text(self, to: str, message: str) -> dict:
        """Envia mensagem de texto simples."""
        phone = self._normalize_phone(to)
        logger.info("Z-API [%s] send_text → %s", self.instance_id[:8], phone)
        return await self._post("send-text", {
            "phone": phone,
            "message": message,
        })

    async def send_button_list(
        self,
        to: str,
        message: str,
        buttons: list[dict],
        title: str = "",
        footer: str = "",
    ) -> dict:
        """
        Envia mensagem com lista de botões (button list).

        buttons: lista de {"id": "btn_id", "label": "Texto do botão"}
        Máximo 3 botões pelo WhatsApp.

        Exemplo:
            buttons = [
                {"id": "saber_mais", "label": "Quero saber mais"},
                {"id": "sem_interesse", "label": "Sem interesse"},
            ]
        """
        phone = self._normalize_phone(to)
        logger.info("Z-API [%s] send_button_list → %s (%d botões)", self.instance_id[:8], phone, len(buttons))
        return await self._post("send-button-list", {
            "phone": phone,
            "message": message,
            "title": title,
            "footer": footer,
            "buttonList": {
                "buttons": [
                    {"id": b["id"], "label": b.get("label") or b.get("title", "")}
                    for b in buttons
                ]
            },
        })

    async def send_image(
        self,
        to: str,
        image_url: str,
        caption: str = "",
    ) -> dict:
        """Envia imagem com legenda opcional."""
        phone = self._normalize_phone(to)
        logger.info("Z-API [%s] send_image → %s", self.instance_id[:8], phone)
        return await self._post("send-image", {
            "phone": phone,
            "image": image_url,
            "caption": caption,
        })

    async def send_document(
        self,
        to: str,
        document_url: str,
        filename: str = "documento.pdf",
        caption: str = "",
    ) -> dict:
        """Envia documento."""
        phone = self._normalize_phone(to)
        logger.info("Z-API [%s] send_document → %s", self.instance_id[:8], phone)
        return await self._post("send-document/pdf", {
            "phone": phone,
            "document": document_url,
            "fileName": filename,
            "caption": caption,
        })


# ---------------------------------------------------------------------------
# Factory: seleciona a instância Z-API correta pelo card
# ---------------------------------------------------------------------------

def get_zapi_for_card(card: dict) -> ZAPIClient:
    """
    Retorna o ZAPIClient correto baseado na etiqueta/administradora do lead.
    Itaú → instância Itaú, Santander → instância Santander, etc.
    """
    from services.faro import get_etiqueta, is_lista
    if is_lista(card):
        raise ZAPIError("Lead de Lista deve usar Whapi, não Z-API")
    etiqueta = get_etiqueta(card)
    return ZAPIClient(instance_key=etiqueta)
