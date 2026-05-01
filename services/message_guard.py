"""
services/message_guard.py — Porteiro de Deduplicação de Mensagens

Garante que nenhuma mensagem seja enviada mais de uma vez para o mesmo lead
dentro de uma janela de tempo, independente do agente ou fluxo que a originou.

Duas camadas de verificação:
  1. HASH EXATO   — bloqueia reenvio de mensagem idêntica (mesmo texto) por 12h
  2. PREFIXO      — bloqueia mensagens com os primeiros 80 chars iguais por 4h
                    (evita variações mínimas da mesma mensagem, ex: saudação ligeiramente diferente)

Fail-open: se Redis não estiver disponível, libera o envio e loga aviso.
Nunca bloqueia o fluxo principal por falha interna.

Namespaces Redis:
  cs:guard:exact:{phone}:{sha256[:16]}  → TTL 12h — hash exato da mensagem
  cs:guard:prefix:{phone}:{prefix_hash} → TTL 4h  — hash do prefixo (80 chars)
"""

import hashlib
import logging
import time

logger = logging.getLogger(__name__)

# Janelas de bloqueio
_TTL_EXACT_H  = 12   # horas para mensagem idêntica
_TTL_PREFIX_H = 4    # horas para prefixo similar

# Comprimento do prefixo para detecção de similaridade
_PREFIX_LEN = 80

# Mensagens muito curtas (< N chars) só usam hash exato — evitar falso positivo
# em respostas como "Ok!", "Sim, pode ser.", etc.
_MIN_LEN_FOR_PREFIX = 120


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _prefix_hash(text: str) -> str:
    prefix = text[:_PREFIX_LEN].strip().lower()
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:16]


async def check_and_register(
    phone: str,
    message: str,
    canal: str = "",
) -> tuple[bool, str]:
    """
    Verifica se a mensagem pode ser enviada para o telefone.

    Retorna (bloqueado: bool, motivo: str).
      bloqueado=False → pode enviar; registra os hashes no Redis
      bloqueado=True  → não enviar; motivo descreve o porquê

    Fail-open: qualquer exceção de Redis retorna (False, "redis_error").
    """
    try:
        from services.session_store import get_redis
        redis = await get_redis()
        if redis is None:
            return False, "redis_unavailable"
    except Exception as e:
        logger.warning("message_guard: Redis indisponível (%s) — liberando envio", e)
        return False, "redis_error"

    msg_hash    = _hash(message)
    key_exact   = f"cs:guard:exact:{phone}:{msg_hash}"
    ttl_exact_s = _TTL_EXACT_H * 3600

    try:
        # ── Camada 1: hash exato ──────────────────────────────────────────────
        existing_exact = await redis.get(key_exact)
        if existing_exact:
            sent_at = existing_exact.decode() if isinstance(existing_exact, bytes) else existing_exact
            logger.info(
                "message_guard[exact]: BLOQUEADO para %s (hash=%s, enviado em %s)",
                phone[-4:], msg_hash, sent_at,
            )
            return True, f"duplicata_exata (hash={msg_hash}, enviado={sent_at})"

        # ── Camada 2: prefixo similar (apenas mensagens longas) ───────────────
        if len(message) >= _MIN_LEN_FOR_PREFIX:
            pfx_hash  = _prefix_hash(message)
            key_pfx   = f"cs:guard:prefix:{phone}:{pfx_hash}"
            ttl_pfx_s = _TTL_PREFIX_H * 3600

            existing_pfx = await redis.get(key_pfx)
            if existing_pfx:
                sent_at = existing_pfx.decode() if isinstance(existing_pfx, bytes) else existing_pfx
                logger.info(
                    "message_guard[prefix]: BLOQUEADO para %s (pfx=%s, enviado em %s)",
                    phone[-4:], pfx_hash, sent_at,
                )
                return True, f"prefixo_similar (pfx={pfx_hash}, enviado={sent_at})"

        # ── Registra os hashes (mensagem liberada) ────────────────────────────
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await redis.setex(key_exact, ttl_exact_s, now_iso)

        if len(message) >= _MIN_LEN_FOR_PREFIX:
            await redis.setex(key_pfx, ttl_pfx_s, now_iso)

        return False, ""

    except Exception as e:
        logger.warning("message_guard: erro ao verificar/registrar (%s) — liberando envio", e)
        return False, "redis_error"


async def clear_guard(phone: str) -> int:
    """
    Remove todas as entradas do porteiro para um telefone.
    Útil quando um lead muda de estágio e precisa receber nova comunicação
    (ex: após aceitar proposta, limpa guard para mensagem de coleta de dados).

    Retorna o número de chaves removidas.
    """
    try:
        from services.session_store import get_redis
        redis = await get_redis()
        if redis is None:
            return 0

        # Varrer e deletar todas as chaves deste telefone
        keys_exact  = await redis.keys(f"cs:guard:exact:{phone}:*")
        keys_prefix = await redis.keys(f"cs:guard:prefix:{phone}:*")
        all_keys = keys_exact + keys_prefix

        if all_keys:
            await redis.delete(*all_keys)
            logger.info("message_guard: cleared %d chaves para %s", len(all_keys), phone[-4:])

        return len(all_keys)

    except Exception as e:
        logger.warning("message_guard: falha ao limpar guard para %s: %s", phone[-4:], e)
        return 0


async def clear_guard_prefix_only(phone: str) -> int:
    """
    Remove apenas as entradas de prefixo para um telefone.
    Útil quando queremos bloquear mensagem exata mas liberar variações
    (ex: nova proposta com valor diferente tem prefixo similar mas conteúdo diferente).
    """
    try:
        from services.session_store import get_redis
        redis = await get_redis()
        if redis is None:
            return 0

        keys = await redis.keys(f"cs:guard:prefix:{phone}:*")
        if keys:
            await redis.delete(*keys)
            logger.info("message_guard: cleared %d prefix-keys para %s", len(keys), phone[-4:])

        return len(keys)

    except Exception as e:
        logger.warning("message_guard: falha ao limpar prefix guard para %s: %s", phone[-4:], e)
        return 0
