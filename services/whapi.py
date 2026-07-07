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
    """Retorna pool Bazar. Se WHAPI_TOKEN_BAZAR não configurado, usa pool de Listas como fallback."""
    if WHAPI_BAZAR_TOKEN:
        return [WHAPI_BAZAR_TOKEN]
    if WHAPI_LISTA_TOKENS:
        logger.info(
            "WHAPI_TOKEN_BAZAR não configurado — usando pool de Listas como fallback para Bazar."
        )
        return WHAPI_LISTA_TOKENS
    logger.warning("WHAPI_TOKEN_BAZAR não configurado e pool de Listas vazio — leads Bazar sem canal!")
    return []


def _build_lp_pool() -> list[str]:
    """Retorna pool LP. Se WHAPI_TOKEN_LP não configurado, usa pool de Listas como fallback."""
    if WHAPI_LP_TOKEN:
        return [WHAPI_LP_TOKEN]
    if WHAPI_LISTA_TOKENS:
        logger.info(
            "WHAPI_TOKEN_LP não configurado — usando pool de Listas como fallback para LP."
        )
        return WHAPI_LISTA_TOKENS
    logger.warning("WHAPI_TOKEN_LP não configurado e pool de Listas vazio — leads LP sem canal!")
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


async def _register_bot_message_async(msg_id: str) -> None:
    """Grava o message_id no Redis de forma assíncrona com retry."""
    from services.session_store import mark_bot_message
    for attempt in range(3):
        try:
            await mark_bot_message(msg_id)
            logger.debug("_register_bot_message: marcado %s (tentativa %d)", msg_id[:16], attempt + 1)
            return
        except Exception as _e:
            if attempt == 2:
                logger.warning("_register_bot_message: falhou após 3 tentativas para %s: %s", msg_id[:16], _e)
            else:
                import asyncio as _asyncio
                await _asyncio.sleep(0.1 * (attempt + 1))


def _register_bot_message(result: dict) -> None:
    """
    Agenda gravação do message_id no Redis como coroutine rastreável.
    Usa create_task (não ensure_future) para garantir execução antes do
    próximo webhook from_me chegar — evita falsos handoffs.

    Cobre: send_text, send_image, send_document, send_buttons, send_list.
    """
    import asyncio
    msg_id = (result or {}).get("id") or (result or {}).get("message_id") or ""
    # Whapi às vezes aninha o id dentro de "message"
    if not msg_id:
        msg_obj = (result or {}).get("message") or {}
        msg_id = msg_obj.get("id") or ""
    if not msg_id:
        logger.debug("_register_bot_message: resultado sem id — %s", str(result)[:80])
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_register_bot_message_async(msg_id))
    except RuntimeError:
        # Sem event loop rodando (testes, scripts CLI) — ignora silenciosamente
        pass


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
        # Grupos WhatsApp (@g.us) e outros JIDs especiais não devem ser normalizados
        if "@" in str(phone):
            return str(phone)
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
        Verifica se um número tem WhatsApp ativo via POST /contacts.
        Retorna True se válido, False se inválido ou sem WA.
        O endpoint também normaliza o número (remove 9 extra se necessário).
        """
        normalized = self._normalize_phone(phone)
        try:
            r = await self._client.post("/contacts", json={"contacts": [normalized]}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                contacts = data.get("contacts", [])
                if contacts:
                    status = contacts[0].get("status", "")
                    return status == "valid"
                return False
            logger.warning("check_phone(%s): status inesperado %d — assumindo True", normalized[-4:], r.status_code)
            return True
        except Exception as e:
            logger.warning("check_phone(%s): erro de rede — assumindo True: %s", normalized[-4:], e)
            return True

    async def resolve_wa_id(self, phone: str) -> str | None:
        """
        Retorna o wa_id real do número (pode diferir do input quando há 9 extra).
        Retorna None se o número não tem WhatsApp.
        """
        normalized = self._normalize_phone(phone)
        try:
            r = await self._client.post("/contacts", json={"contacts": [normalized]}, timeout=10.0)
            if r.status_code == 200:
                contacts = r.json().get("contacts", [])
                if contacts and contacts[0].get("status") == "valid":
                    wa_id = contacts[0].get("wa_id", "")
                    # wa_id vem como "554791295576@s.whatsapp.net" ou só dígitos
                    return "".join(c for c in wa_id if c.isdigit()) or None
            return None
        except Exception:
            return None

    async def _validate_lead_phone(self, phone: str) -> bool:
        """
        Verifica se o número tem WhatsApp ativo antes de disparos proativos.
        Só aplica para números de leads (_is_lead_recipient). Grupos e equipe passam direto.
        """
        if not _is_lead_recipient(phone):
            return True
        ok = await self.check_phone(phone)
        if not ok:
            logger.warning(
                "Whapi[%s] BLOQUEADO: %s não tem WA ativo — disparo cancelado",
                self._canal, phone[-4:],
            )
        return ok

    async def send_text(self, to: str, message: str, _log_nome: str = "", _log_card_id: str = "") -> dict:
        """Envia mensagem de texto simples."""
        phone = self._normalize_phone(to)
        if not await self._validate_lead_phone(phone):
            return {"sent": False, "blocked": True, "reason": "no_whatsapp"}

        # ── PORTEIRO DE DEDUPLICAÇÃO ──────────────────────────────────────────
        # Bloqueia reenvio de mensagem idêntica (ou muito similar) para o mesmo
        # número dentro de uma janela de tempo. Fail-open: se Redis falhar, envia.
        try:
            from services.message_guard import check_and_register
            bloqueado, motivo = await check_and_register(phone, message, canal=self._canal)
            if bloqueado:
                logger.warning(
                    "Whapi[%s] PORTEIRO bloqueou envio → %s | motivo: %s",
                    self._canal, phone[-4:], motivo,
                )
                return {"sent": False, "blocked": True, "reason": f"porteiro:{motivo}"}
        except Exception as _pg_err:
            logger.warning("Whapi: porteiro falhou (%s) — prosseguindo por segurança", _pg_err)
        # ─────────────────────────────────────────────────────────────────────

        logger.info("Whapi[%s] send_text → %s", self._canal, phone)
        # Camada A: registra o texto NO REDIS ANTES do POST HTTP — garante que
        # o webhook from_me (que chega antes da resposta) já encontra o fingerprint.
        try:
            from services.session_store import mark_bot_text
            await mark_bot_text(phone, message)
        except Exception as _mbt_err:
            logger.debug("mark_bot_text falhou (%s) — prosseguindo", _mbt_err)
        result = await self._post("/messages/text", {"to": phone, "body": message})
        # Registra message_id no Redis para distinguir mensagens do bot de mensagens manuais
        _register_bot_message(result)
        # Limpa marcador de pendente — sistema respondeu ao lead
        if result.get("sent") or result.get("id"):
            try:
                from jobs.watchdog_extratos import clear_message_pending
                import asyncio
                asyncio.ensure_future(clear_message_pending(phone))
            except Exception:
                pass
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
        if not await self._validate_lead_phone(phone):
            return {"sent": False, "blocked": True, "reason": "no_whatsapp"}
        logger.info("Whapi[%s] send_buttons → %s (%d botões)", self._canal, phone, len(buttons))
        # Camada A: registra conteúdo ANTES do POST (mesmo padrão do send_text)
        try:
            from services.session_store import mark_bot_text
            await mark_bot_text(phone, message)
        except Exception as _mbt_err:
            logger.debug("mark_bot_text (buttons) falhou (%s) — prosseguindo", _mbt_err)
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
        _register_bot_message(result)
        # Limpa marcador de pendente — sistema respondeu ao lead
        if result.get("sent") or result.get("id"):
            try:
                from jobs.watchdog_extratos import clear_message_pending
                import asyncio
                asyncio.ensure_future(clear_message_pending(phone))
            except Exception:
                pass
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
        # Camada A: registra conteúdo ANTES do POST
        try:
            from services.session_store import mark_bot_text
            await mark_bot_text(phone, message)
        except Exception as _mbt_err:
            logger.debug("mark_bot_text (list) falhou (%s) — prosseguindo", _mbt_err)
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
        _register_bot_message(result)
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
        if not await self._validate_lead_phone(phone):
            return {"sent": False, "blocked": True, "reason": "no_whatsapp"}
        logger.info("Whapi[%s] send_image → %s", self._canal, phone)
        # Camada A: para imagens sem texto, registra marcador genérico "[image]"
        # que bate com o msg.media_type do webhook from_me correspondente.
        try:
            from services.session_store import mark_bot_text
            await mark_bot_text(phone, caption or "[image]")
            await mark_bot_text(phone, "[image]")
        except Exception as _mbt_err:
            logger.debug("mark_bot_text (image) falhou (%s) — prosseguindo", _mbt_err)
        result = await self._post("/messages/image", {
            "to": phone,
            "media": image_url,
            "caption": caption,
        })
        _register_bot_message(result)
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
        # Camada A: registra marcador genérico para documento
        try:
            from services.session_store import mark_bot_text
            await mark_bot_text(phone, caption or "[document]")
            await mark_bot_text(phone, "[document]")
        except Exception as _mbt_err:
            logger.debug("mark_bot_text (document) falhou (%s) — prosseguindo", _mbt_err)
        result = await self._post("/messages/document", {
            "to": phone,
            "media": document_url,
            "filename": filename,
            "caption": caption,
        })
        _register_bot_message(result)
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
    Resolve o número WhatsApp válido para um card, com fallback automático.

    Usa POST /contacts que já normaliza o 9 extra do Brasil automaticamente
    e retorna o wa_id correto. Corrige o FARO se necessário.
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
            wa_id = await w.resolve_wa_id(phone_principal)
            if wa_id:
                if wa_id != phone_principal:
                    _logger.info(
                        "resolve_phone: %s normalizado %s → %s — corrigindo FARO",
                        card_id[:8], phone_principal[-6:], wa_id[-6:],
                    )
                    try:
                        from services.faro import FaroClient
                        async with FaroClient() as faro:
                            await faro.update_card(card_id, {
                                "Telefone":             wa_id,
                                "Telefone alternativo": phone_principal,
                            })
                        card["Telefone"] = wa_id
                        card["Telefone alternativo"] = phone_principal
                    except Exception as _e:
                        _logger.warning("resolve_phone: erro ao corrigir FARO %s: %s", card_id[:8], _e)
                return wa_id

            _logger.info(
                "resolve_phone: %s sem WA em principal (%s) — tentando alternativo (%s)",
                card_id[:8], phone_principal[-4:], phone_alt[-4:] if phone_alt else "N/A",
            )

            if phone_alt:
                wa_id_alt = await w.resolve_wa_id(phone_alt)
                if wa_id_alt:
                    _logger.info(
                        "resolve_phone: alternativo válido (%s) — corrigindo FARO", wa_id_alt[-4:],
                    )
                    try:
                        from services.faro import FaroClient
                        async with FaroClient() as faro:
                            await faro.update_card(card_id, {
                                "Telefone":             wa_id_alt,
                                "Telefone alternativo": phone_principal,
                            })
                        card["Telefone"] = wa_id_alt
                        card["Telefone alternativo"] = phone_principal
                    except Exception as _e:
                        _logger.warning("resolve_phone: erro FARO alt %s: %s", card_id[:8], _e)
                    return wa_id_alt

        elif phone_alt:
            wa_id_alt = await w.resolve_wa_id(phone_alt)
            if wa_id_alt:
                return wa_id_alt

    _logger.warning(
        "resolve_phone: card %s — nenhum número com WA (principal=%s, alt=%s)",
        card_id[:8], phone_principal[-4:] if phone_principal else "N/A",
        phone_alt[-4:] if phone_alt else "N/A",
    )

    # Procedimento padrão: mover para PROBLEMA_CONTATO e registrar motivo de perda
    if card_id:
        try:
            from services.faro import FaroClient
            from config import Stage
            async with FaroClient() as faro:
                await faro.update_card(card_id, {
                    "Motivo de perda": (
                        "Número sem WhatsApp — sem resposta em ambas as variantes "
                        "(com e sem nono dígito)"
                    ),
                })
                await faro.move_card(card_id, Stage.PROBLEMA_CONTATO)
            _logger.info(
                "resolve_phone: card %s movido para PROBLEMA_CONTATO (sem WA)",
                card_id[:8],
            )
        except Exception as _e:
            _logger.error(
                "resolve_phone: erro ao mover card %s para PROBLEMA_CONTATO: %s",
                card_id[:8], _e,
            )

    return None


# ---------------------------------------------------------------------------
# Notificação centralizada para equipe (grupo Alarmes Sistemas CS)
# ---------------------------------------------------------------------------

async def notify_team(message: str) -> None:
    """
    Envia notificação para o grupo de alarmes/equipe comercial.
    Tenta canal lista (inclui DEADPL-V592K que está no grupo) primeiro.
    Fallback: bazar. Fallback final: NOTIFY_PHONES individuais.

    ATENÇÃO: Para o grupo funcionar, o número do canal utilizado deve
    ser participante do grupo no WhatsApp e estar autorizado no Whapi.
    O DEADPL-V592K foi movido para o pool de Listas (LISTA_4).
    """
    from config import NOTIFY_GROUP, NOTIFY_PHONES
    import logging
    _log = logging.getLogger(__name__)

    if NOTIFY_GROUP:
        # DEADPL-V592K (número no grupo) agora é LISTA_4 — tenta lista primeiro
        for _canal_grupo in ("lista", "bazar"):
            try:
                async with WhapiClient(canal=_canal_grupo) as w:
                    await w.send_text(NOTIFY_GROUP, message)
                return  # Sucesso — não precisa de fallback
            except WhapiError as e:
                _log.warning(
                    "notify_team: canal '%s' falhou ao enviar para grupo (%s): %s — tentando próximo",
                    _canal_grupo, NOTIFY_GROUP, e,
                )
        _log.error("notify_team: todos os canais falharam para grupo %s — usando NOTIFY_PHONES", NOTIFY_GROUP)

    # Fallback: NOTIFY_PHONES
    if NOTIFY_PHONES:
        try:
            async with WhapiClient(canal="lista") as w:
                for ph in NOTIFY_PHONES:
                    await w.send_text(ph, message)
        except WhapiError as e:
            _log.error("notify_team: falha total ao notificar equipe: %s", e)
