"""
webhooks/router.py — Roteador central de mensagens WhatsApp recebidas

Recebe payloads do Whapi, normaliza para IncomingMessage e despacha
para o handler correto baseado no stage do lead no FARO.

Endpoint único: POST /webhook/whapi
(Z-API removido — todos os fluxos agora usam Whapi)
"""

import asyncio
import time
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config import Stage, TERMINAL_STAGES
from services.faro import FaroClient, FaroError, is_lista, get_name, get_canal, history_append
from services.transcriber import transcribe_audio
from services.session_store import load_history_smart, save_history_smart
from webhooks.negociador import handle_message
from webhooks.qualificador import handle_qualification, QUALIFICATION_STAGES
from webhooks.agente_contrato import handle_dados_pessoais, handle_extrato_recebido
from webhooks import debounce
import webhooks.agente_listas as agente_listas
import webhooks.agente_bazar as agente_bazar
import webhooks.agente_lp as agente_lp
import webhooks.agente_lp_lance as agente_lp_lance

logger = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    phone: str
    text: Optional[str]
    source: str  # "whapi"
    from_me: bool = False
    is_group: bool = False
    media_type: Optional[str] = None
    raw: dict = field(default_factory=dict)
    whapi_token: Optional[str] = None   # token do canal — usado para transcrição

    @property
    def is_processable(self) -> bool:
        if self.from_me or self.is_group:
            return False
        return bool(self.text and self.text.strip())

    @property
    def is_audio(self) -> bool:
        """True se for áudio/voz (ainda não transcrito)."""
        if self.from_me or self.is_group:
            return False
        return self.media_type in ("audio", "voice")

    @property
    def is_media_message(self) -> bool:
        if self.from_me or self.is_group:
            return False
        return self.media_type in ("image", "document", "video")


def _describe_media(msg: "IncomingMessage") -> str:
    """Retorna descrição legível da mídia para o #log-cs."""
    if not msg.media_type:
        return "[mídia desconhecida]"
    raw = msg.raw
    if msg.media_type == "document":
        doc = raw.get("document", {})
        fname = doc.get("file_name") or doc.get("filename") or ""
        mime  = doc.get("mime_type", "")
        label = fname or mime or "documento"
        return f"📄 [{label}]"
    if msg.media_type == "image":
        return "🖼️ [imagem]"
    if msg.media_type in ("audio", "voice"):
        return "🎤 [áudio]"
    if msg.media_type == "video":
        return "🎥 [vídeo]"
    return f"[{msg.media_type}]"


HANDLED_STAGES = {Stage.PRECIFICACAO, Stage.EM_NEGOCIACAO, Stage.FINALIZACAO_COMERCIAL}
ACTIVATION_STAGES = {
    Stage.PRIMEIRA_ATIVACAO, Stage.SEGUNDA_ATIVACAO,
    Stage.TERCEIRA_ATIVACAO, Stage.QUARTA_ATIVACAO,
}

# Se a proposta já foi enviada (Proposta Realizada preenchida), o negociador
# assume independente da stage — evita que agente_bazar encerre prematuramente
def _proposta_ja_enviada(card: dict) -> bool:
    p = str(card.get("Proposta Realizada") or "").strip()
    try:
        return float(p.replace("R$","").replace(".","").replace(",",".").strip()) > 0
    except (ValueError, TypeError):
        return False


def parse_whapi_payload(payload: dict, whapi_token: Optional[str] = None) -> list[IncomingMessage]:
    """Normaliza payload Whapi para lista de IncomingMessages."""
    messages_raw = []
    if "messages" in payload:
        messages_raw = payload["messages"] if isinstance(payload["messages"], list) else [payload["messages"]]
    elif "message" in payload:
        messages_raw = [payload["message"]]
    elif payload.get("event", {}).get("type") == "messages":
        messages_raw = payload.get("event", {}).get("data", {}).get("messages", [])

    result = []
    for msg in messages_raw:
        msg_type = msg.get("type", "")
        if msg_type in ("status", "reaction", "revoked"):
            continue

        chat_id = msg.get("chat_id", "") or msg.get("from", "")
        from_me = msg.get("from_me", False)
        is_group = "@g.us" in chat_id
        phone_raw = chat_id.replace("@s.whatsapp.net", "").replace("@g.us", "")
        phone = "".join(c for c in phone_raw if c.isdigit())
        if phone and not phone.startswith("55"):
            phone = "55" + phone

        text = None
        media_type = None

        if msg_type == "text":
            text = msg.get("body") or msg.get("text", {}).get("body", "")
        elif msg_type in ("image", "video", "audio", "document", "sticker", "voice"):
            media_type = msg_type
            text = msg.get("caption") or msg.get("body") or None
        elif msg_type == "reply":
            reply_obj = msg.get("reply", {})
            btn_reply = reply_obj.get("buttons_reply", {})
            text = btn_reply.get("title") or reply_obj.get("text") or None
        elif "body" in msg:
            text = msg["body"]

        if not text:
            interactive = msg.get("interactive", {})
            if interactive:
                btn_reply = interactive.get("button_reply", {})
                list_reply = interactive.get("list_reply", {})
                text = btn_reply.get("title") or list_reply.get("title") or None

        if not phone:
            continue

        result.append(IncomingMessage(
            phone=phone,
            text=text.strip() if text else None,
            source="whapi",
            from_me=from_me,
            is_group=is_group,
            media_type=media_type,
            raw=msg,
            whapi_token=whapi_token,
        ))

    return result


async def _find_card(phone: str, canal_hint: str = "") -> Optional[dict]:
    digits = "".join(c for c in phone if c.isdigit())
    candidates = {digits}
    if digits.startswith("55"):
        candidates.add(digits[2:])
    else:
        candidates.add("55" + digits)
    if len(digits) == 10:
        candidates.add(digits[:2] + "9" + digits[2:])
    if len(digits) == 12 and digits.startswith("55"):
        candidates.add(digits[:4] + "9" + digits[4:])

    try:
        async with FaroClient() as faro:
            for candidate in candidates:
                card = await faro.find_card_by_phone(candidate, canal_hint=canal_hint)
                if card:
                    return card
    except FaroError as e:
        logger.error("Router: erro ao buscar card por telefone %s: %s", phone, e)
    return None


def _canal_hint_from_token(whapi_token: Optional[str]) -> str:
    """Deduz o canal ('bazar', 'lp', 'lista') a partir do token Whapi recebido no webhook."""
    if not whapi_token:
        return ""
    from config import WHAPI_BAZAR_TOKEN, WHAPI_LP_TOKEN, WHAPI_LISTA_TOKENS
    if whapi_token == WHAPI_BAZAR_TOKEN:
        return "bazar"
    if whapi_token == WHAPI_LP_TOKEN:
        return "lp"
    if whapi_token in WHAPI_LISTA_TOKENS:
        return "lista"
    return ""


# ---------------------------------------------------------------------------
# Stages que NÃO devem ser sobrescritos pelo handoff automático.
# Nesses casos apenas registramos o turno no histórico, sem mover o card.
# ---------------------------------------------------------------------------
_HANDOFF_SKIP_STAGES: frozenset = frozenset({
    Stage.FINALIZACAO_COMERCIAL,  # já está com comercial — só registra turno
    Stage.SUCESSO,                # negócio fechado
    Stage.ACEITO,                 # aceite confirmado — aguardando coleta de dados
    Stage.ASSINATURA,             # em processo de assinatura — não interromper
}) | TERMINAL_STAGES             # PERDIDO, NAO_QUALIFICADO, FLUXO_CADENCIA, etc.


def _build_handoff_description(nome: str, history: list[dict], ultima_msg: str) -> str:
    """
    Constrói o bloco de texto a ser appendado na descrição do card no momento
    em que o time comercial assume o atendimento manualmente.

    Inclui:
    - Timestamp do handoff
    - Última mensagem enviada pelo comercial
    - Últimas 20 trocas do histórico com o bot (legível para o consultor)
    """
    from datetime import datetime
    from config import TZ_BRASILIA

    agora = datetime.now(TZ_BRASILIA).strftime("%d/%m/%Y %H:%M")

    linhas = [
        f"🤝 Atendimento assumido pelo time comercial em {agora}",
        f'Mensagem de abertura: "{ultima_msg[:200]}"',
        "",
        "📋 Histórico da conversa automatizada:",
    ]

    if not history:
        linhas.append("  (sem histórico registrado no momento do handoff)")
    else:
        for turn in history[-20:]:
            role_raw = turn.get("role", "")
            content  = str(turn.get("content", ""))[:300]
            # Turnos de atendimento humano já registrados anteriormente
            if content.startswith("[COMERCIAL]:"):
                role_label = "Comercial"
                content    = content[len("[COMERCIAL]:"):].strip()
            elif role_raw == "user":
                role_label = "Lead"
            else:
                role_label = "Bot"
            linhas.append(f"  {role_label}: {content}")

    return "\n".join(linhas)


async def handle_outgoing_manual(msg: IncomingMessage) -> None:
    """
    Intercepta mensagens enviadas manualmente pelo time comercial via números
    conectados ao Whapi (from_me=True).

    Fluxo:
    1. Ignora mensagens sem conteúdo (status, reações, etc.)
    2. Ignora mensagens cujo destinatário é número interno (NOTIFY_PHONES / CONSULTANT_PHONES)
    3. Verifica fingerprint Redis (message_id marcado pelo bot → descarta)
       — Camada 2: aguarda até 500ms para dar tempo ao create_task do _register_bot_message
    4. Localiza o card do lead pelo telefone destino
    5. Aplica flag de handoff (deduplicação atômica via Redis SET NX)
    6. Se for o primeiro handoff:
       a. Move o card para FINALIZACAO_COMERCIAL (exceto stages protegidos)
       b. Appenda o histórico na descrição do card
       c. Envia alerta no Slack
    7. Sempre: registra o turno do comercial no histórico Redis
    """
    import asyncio as _asyncio

    # ── Filtra mensagens sem conteúdo relevante ───────────────────────────────
    if not msg.text and not msg.media_type:
        return

    # ── Filtra destinatários internos (nunca são leads) ───────────────────────
    # Evita handoff acidental quando o time envia msg para si mesmo ou para
    # o grupo de alertas via o mesmo número Whapi.
    from config import NOTIFY_PHONES, CONSULTANT_PHONES
    _dest_digits = "".join(c for c in msg.phone if c.isdigit())
    _internos = set()
    for v in NOTIFY_PHONES:
        _internos.add("".join(c for c in str(v) if c.isdigit()))
    for v in CONSULTANT_PHONES.values():
        _internos.add("".join(c for c in str(v) if c.isdigit()))
    if any(_dest_digits.endswith(i) or i.endswith(_dest_digits) for i in _internos if i):
        logger.debug("handle_outgoing_manual: destinatário interno %s — ignorando", _dest_digits[-6:])
        return

    # ── Verifica fingerprint: é mensagem do bot? ──────────────────────────────
    # Camada A (conteúdo — pré-send): verifica hash do texto contra o que o bot
    #   registrou ANTES de fazer o POST HTTP. Funciona mesmo quando o webhook
    #   chega antes da resposta HTTP (race condition Whapi confirmada nos logs).
    # Camada B (msg_id — pós-send): fallback para quando a Camada A não captura
    #   (ex: send_image, send_document onde não há texto para hash).
    from services.session_store import is_bot_message, is_bot_text, set_handoff_flag
    msg_id   = msg.raw.get("id") or msg.raw.get("message_id") or ""
    msg_text = msg.text or ""
    # Para mídia sem texto, normaliza para marcador genérico usado no mark_bot_text
    if not msg_text and msg.media_type:
        msg_text = f"[{msg.media_type}]"

    # Camada A — verifica pelo conteúdo (não depende de timing)
    if msg_text and await is_bot_text(msg.phone, msg_text):
        logger.debug(
            "handle_outgoing_manual: msg para %s identificada como bot (camada A — conteúdo) — ignorando",
            msg.phone[-6:],
        )
        return

    # Camada B — verifica pelo msg_id (pós-send, pode chegar tarde)
    if msg_id and await is_bot_message(msg_id):
        logger.debug(
            "handle_outgoing_manual: msg_id=%s identificado como bot (camada B — msg_id) — ignorando",
            msg_id[:16],
        )
        return

    # Camada B com espera: aguarda 300ms e tenta de novo (absorve latência mínima)
    if msg_id:
        import asyncio as _asyncio
        await _asyncio.sleep(0.3)
        if await is_bot_message(msg_id):
            logger.debug(
                "handle_outgoing_manual: msg_id=%s identificado como bot (camada B +300ms) — ignorando",
                msg_id[:16],
            )
            return

    # ── Localiza o card pelo telefone destino ─────────────────────────────────
    card = await _find_card(msg.phone, canal_hint=_canal_hint_from_token(msg.whapi_token))
    if not card:
        logger.debug(
            "handle_outgoing_manual: %s não encontrado no CRM — ignorando mensagem manual",
            msg.phone,
        )
        return

    card_id       = card.get("id", "")
    current_stage = card.get("stage_id") or ""
    phone         = msg.phone
    nome          = get_name(card)
    canal_hint    = _canal_hint_from_token(msg.whapi_token) or "desconhecido"
    texto         = msg.text or f"[{msg.media_type or 'mídia'}]"

    logger.info(
        "handle_outgoing_manual: card=%s (%s) | stage=%s... | canal=%s | msg='%s'",
        card_id[:8], nome, current_stage[:8], canal_hint, texto[:60],
    )

    # ── Stage protegido: apenas registra o turno, não move o card ────────────
    stage_protegido = current_stage in _HANDOFF_SKIP_STAGES

    # ── Flag de handoff — atômica (SET NX) ───────────────────────────────────
    # Garante que múltiplas mensagens em sequência não movam o card N vezes.
    primeiro_handoff = False
    if not stage_protegido:
        primeiro_handoff = await set_handoff_flag(phone)

    async with FaroClient() as faro:
        if not stage_protegido and primeiro_handoff:
            # ── Move o card para FINALIZACAO_COMERCIAL ────────────────────────
            try:
                await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
                logger.info(
                    "handle_outgoing_manual: card %s → FINALIZACAO_COMERCIAL (handoff manual)",
                    card_id[:8],
                )
            except FaroError as e:
                logger.error(
                    "handle_outgoing_manual: erro ao mover card %s para FINALIZACAO_COMERCIAL: %s",
                    card_id[:8], e,
                )

            # ── Appenda o histórico completo na descrição do card ─────────────
            try:
                history_snapshot = await load_history_smart(phone, card)
                descricao = _build_handoff_description(nome, history_snapshot, texto)
                await faro.append_description(card_id, descricao)
                logger.info(
                    "handle_outgoing_manual: descrição appendada no card %s (%d turns no snapshot)",
                    card_id[:8], len(history_snapshot),
                )
            except FaroError as e:
                logger.warning(
                    "handle_outgoing_manual: erro ao gravar descrição card %s: %s",
                    card_id[:8], e,
                )

            # ── Alerta no Slack ───────────────────────────────────────────────
            try:
                from services.slack import slack_warning
                asyncio.create_task(slack_warning(
                    f"👤 *Atendimento humano detectado*\n"
                    f"Lead: *{nome}* | Número: `...{phone[-6:]}`\n"
                    f"Card movido para *FINALIZAÇÃO COM AGENTE COMERCIAL*\n"
                    f"Canal: `{canal_hint}` | Stage anterior: `{current_stage[:8]}`\n"
                    f"Mensagem de abertura: _{texto[:120]}_",
                    context={"Card": card_id[:12], "Lead": nome, "Phone": phone},
                ))
            except Exception as _slack_err:
                logger.debug("handle_outgoing_manual: slack_warning falhou: %s", _slack_err)

        elif stage_protegido:
            logger.debug(
                "handle_outgoing_manual: card %s em stage protegido (%s) — "
                "apenas registrando turno no histórico",
                card_id[:8], current_stage[:8],
            )

        # ── Sempre: registra o turno do comercial no histórico Redis ──────────
        try:
            history = await load_history_smart(phone, card)
            history = history_append(history, "assistant", f"[COMERCIAL]: {texto}")
            await save_history_smart(phone, history, faro_client=faro, card_id=card_id)
        except Exception as e:
            logger.warning(
                "handle_outgoing_manual: erro ao salvar turno no histórico card %s: %s",
                card_id[:8], e,
            )


async def route_message(msg: IncomingMessage) -> None:
    if msg.is_group:
        return

    # ── Mensagens enviadas manualmente pelo time comercial ────────────────────
    # from_me=True pode ser: (a) bot enviou via API, ou (b) comercial digitou
    # no WhatsApp. O handler distingue via fingerprint de message_id no Redis.
    if msg.from_me:
        asyncio.create_task(handle_outgoing_manual(msg))
        return

    # ── Transcrição de áudio — acontece antes de qualquer dispatch ────────────
    if msg.is_audio and msg.whapi_token:
        transcricao = await transcribe_audio(msg.raw, msg.whapi_token)
        if transcricao:
            logger.info("Router: áudio transcrito para %s: '%s'", msg.phone, transcricao[:80])
            msg.text = transcricao
            msg.media_type = None  # trata como texto a partir daqui
        else:
            logger.warning("Router: falha ao transcrever áudio de %s — ignorando", msg.phone)
            return

    if not msg.is_processable and not msg.is_media_message:
        return

    logger.info("Router [%s]: %s → media=%s texto='%s'",
                msg.source, msg.phone, msg.media_type or "none", (msg.text or "")[:60])

    card = await _find_card(msg.phone, canal_hint=_canal_hint_from_token(msg.whapi_token))

    # Log no #log-cs independente de ter card no FARO
    try:
        from services.slack import log_cs
        if card:
            canal_lead = get_canal(card)
            nome       = card.get("Nome do contato") or card.get("title") or "?"
            card_id    = card.get("id", "")
        else:
            canal_lead = "desconhecido"
            nome       = ""
            card_id    = ""
        asyncio.create_task(log_cs(
            direcao="recebido", canal=canal_lead, phone=msg.phone,
            nome=nome, card_id=card_id,
            mensagem=msg.text or _describe_media(msg),
            extra={"FARO": "✅ encontrado" if card else "❌ sem cadastro"},
        ))
    except Exception:
        pass

    if not card:
        logger.info("Router: %s não encontrado no CRM.", msg.phone)
        return

    card_id = card.get("id", "")
    current_stage = card.get("stage_id") or card.get("stageId") or ""
    nome = card.get("Nome do contato") or card.get("title") or "?"
    logger.info("Router: %s (%s) | stage=%s...", nome, card_id[:8], current_stage[:8])

    # Stage TESTES: silêncio total — nenhum agente responde
    if current_stage == Stage.TESTES:
        logger.info("Router: %s em stage TESTES — mensagem ignorada.", nome)
        return

    # Se proposta já foi enviada, negociador assume independente da stage
    if _proposta_ja_enviada(card) and msg.is_processable:
        async def _dispatch_neg(c: dict, texto: str) -> None:
            await handle_message(card=c, mensagem=texto, current_stage_id=current_stage)
        debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                          dispatch=_dispatch_neg)
        return

    # Listas em stages de ativação → agente SDR Listas
    # Regra: is_lista()==True OU Fonte não definida (sem origem = lista fria)
    _fonte = str(card.get("Fonte") or "").strip().lower()
    _is_lista_card = is_lista(card) or (not _fonte)
    if current_stage in ACTIVATION_STAGES and _is_lista_card:
        if msg.is_processable:
            debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                              dispatch=agente_listas.handle_message)
        return

    # LP Lance: leads contemplados por lance — agente específico responde.
    # Se o lead enviar mídia (extrato) → qualificador processa.
    if current_stage == Stage.LP_LANCE:
        if msg.is_media_message:
            asyncio.create_task(agente_lp_lance.handle_extrato_recebido(card, msg))
        elif msg.is_processable:
            debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                              dispatch=agente_lp_lance.handle_message)
        return

    # Qualificação: stages de ativação, apenas Bazar/Site (Fonte definida)
    # LP usa agente_lp (prompt correto para leads de site/landing page)
    # Bazar usa agente_bazar
    if current_stage in QUALIFICATION_STAGES and not _is_lista_card:
        if msg.is_media_message:
            await handle_qualification(card=card, msg=msg)
        elif msg.is_processable:
            fonte = str(card.get("Fonte") or "").lower()
            _dispatch = agente_lp.handle_message if "lp" in fonte or "site" in fonte else agente_bazar.handle_message
            debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                              dispatch=_dispatch)
        return

    # ESPERA: lead aguardando envio de extrato (LP retroativa)
    # Mídia → qualifica extrato; texto → silêncio total (não respondemos até receber o extrato)
    if current_stage == Stage.ESPERA:
        if msg.is_media_message:
            await handle_qualification(card=card, msg=msg)
        else:
            logger.info("Router: %s em ESPERA enviou texto — ignorando (aguardando extrato)", nome)
        return

    # ASSINATURA: qualquer mensagem nessa stage vai para agente_contrato — sem exceção
    # Inclui leads com ZapSign Token já gerado (ex: template inativo, aguardando reenvio)
    if current_stage == Stage.ASSINATURA:
        if msg.is_media_message:
            asyncio.create_task(handle_extrato_recebido(card, msg))
        elif msg.is_processable:
            debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                              dispatch=handle_dados_pessoais)
        return

    # Negociação / suporte
    if current_stage in HANDLED_STAGES:
        if not msg.is_processable:
            return

        async def _dispatch_negociador(c: dict, texto: str) -> None:
            await handle_message(card=c, mensagem=texto, current_stage_id=current_stage)

        debounce.schedule(phone=msg.phone, text=msg.text, card=card,
                          dispatch=_dispatch_negociador)
        return

    logger.info("Router: stage %s não tratado para %s.", current_stage[:8], nome)



_token_cache: dict[str, tuple[str, float]] = {}  # channel_id -> (token, timestamp)
_TOKEN_CACHE_TTL = 300  # 5 minutos


async def _resolve_whapi_token_cached(payload: dict) -> Optional[str]:
    """Versão cacheada de _resolve_whapi_token — TTL 5 min por channel_id."""
    channel_id = (
        payload.get("channel_id")
        or payload.get("channelId")
        or (payload.get("event") or {}).get("channel_id")
        or ""
    )
    if channel_id:
        now = time.time()
        if channel_id in _token_cache:
            token, ts = _token_cache[channel_id]
            if now - ts < _TOKEN_CACHE_TTL:
                return token
        token = await _resolve_whapi_token(payload)
        if token:
            _token_cache[channel_id] = (token, now)
        return token
    return await _resolve_whapi_token(payload)

async def _resolve_whapi_token(payload: dict) -> Optional[str]:
    """
    Identifica o token do canal Whapi a partir do payload do webhook.

    Estratégia (em ordem de prioridade):
      1. Mapeamento estático via variáveis de ambiente WHAPI_CHANNEL_ID_BAZAR/LP/LISTA_N
         — custo zero, determinístico, sem chamada HTTP
      2. Cache em memória do resultado anterior (TTL 5 min)
      3. Fallback: chamada à API Whapi para descoberta dinâmica (apenas se não mapeado)
      4. Fallback final: token Bazar
    """
    from config import WHAPI_LISTA_TOKENS, WHAPI_BAZAR_TOKEN, WHAPI_LP_TOKEN
    import os

    channel_id = (
        payload.get("channel_id")
        or payload.get("channelId")
        or (payload.get("event") or {}).get("channel_id")
        or ""
    )

    if not channel_id:
        return WHAPI_BAZAR_TOKEN or (WHAPI_LISTA_TOKENS[0] if WHAPI_LISTA_TOKENS else None)

    # ── Mapeamento estático via env (I2 — elimina lookup dinâmico) ────────────
    # Configure no .env: WHAPI_CHANNEL_ID_BAZAR=abc123, WHAPI_CHANNEL_ID_LP=def456
    # WHAPI_CHANNEL_ID_LISTA_1=ghi789, WHAPI_CHANNEL_ID_LISTA_2=...
    _static_map: dict[str, Optional[str]] = {
        os.getenv("WHAPI_CHANNEL_ID_BAZAR", ""): WHAPI_BAZAR_TOKEN,
        os.getenv("WHAPI_CHANNEL_ID_LP", ""):    WHAPI_LP_TOKEN,
    }
    for i, tok in enumerate(WHAPI_LISTA_TOKENS, 1):
        cid_env = os.getenv(f"WHAPI_CHANNEL_ID_LISTA_{i}", "")
        if cid_env:
            _static_map[cid_env] = tok

    if channel_id in _static_map and _static_map[channel_id]:
        return _static_map[channel_id]
    # ─────────────────────────────────────────────────────────────────────────

    # Fallback: descoberta dinâmica via API Whapi (cold start ou env não configurado)
    all_tokens = list({t for t in [WHAPI_BAZAR_TOKEN, WHAPI_LP_TOKEN] + WHAPI_LISTA_TOKENS if t})
    for token in all_tokens:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(
                    "https://gate.whapi.cloud/settings",
                    headers={"Authorization": f"Bearer {token}"}
                )
                if r.status_code == 200:
                    data = r.json()
                    cid = data.get("channel_id") or data.get("id") or ""
                    if cid == channel_id:
                        logger.info(
                            "router: channel_id '%s' resolvido dinamicamente — configure "
                            "WHAPI_CHANNEL_ID_BAZAR/LP/LISTA_N no .env para eliminar este lookup",
                            channel_id,
                        )
                        return token
        except Exception:
            pass

    return WHAPI_BAZAR_TOKEN or (WHAPI_LISTA_TOKENS[0] if WHAPI_LISTA_TOKENS else None)


async def handle_whapi_webhook(payload: dict) -> dict:
    """Entry point para POST /webhook/whapi."""
    # Resolve token do canal para transcrição de áudio
    token = await _resolve_whapi_token_cached(payload)
    messages = parse_whapi_payload(payload, whapi_token=token)
    if not messages:
        return {"status": "ok", "processed": 0}
    logger.info("Whapi webhook: %d mensagem(ns)", len(messages))
    for msg in messages:
        asyncio.create_task(route_message(msg))
    return {"status": "ok", "processed": len(messages)}
