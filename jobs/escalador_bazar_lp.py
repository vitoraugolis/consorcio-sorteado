"""
jobs/escalador_bazar_lp.py — Escalamento automático Bazar/LP → Finalização com Agente Comercial

Regra:
  Cards em PRIMEIRA_ATIVACAO com fonte Bazar ou LP e tipo contemplação = sorteio
  que ficaram 3h sem qualquer resposta do lead (nenhuma mensagem recebida após a ativação)
  são movidos para FINALIZACAO_COMERCIAL com notificação para a equipe comercial.

Frequência: a cada 30 min via APScheduler.
Janela de execução: 08h–20h BRT (mesma janela dos demais jobs).

O job NÃO envia nenhuma mensagem ao lead — move silenciosamente e notifica equipe.
O campo "Situacao Negociacao" é atualizado para "sem-resposta-3h" para rastreabilidade.
"""

import asyncio
import logging
import time
import unicodedata
import re
from datetime import datetime

from config import Stage, TZ_BRASILIA, SEND_WINDOW_START, SEND_WINDOW_END, TEST_MODE, filter_test_cards
from services.faro import FaroClient, FaroError, get_name, get_phone, get_adm, get_canal, load_history
from services.whapi import notify_team
from jobs.ativacao_bazar_site import _adm_matches, ADM_BAZAR_TOKENS, ADM_LP_TOKENS, _LP_EXACT_SIGLAS

logger = logging.getLogger(__name__)

# Tempo mínimo sem resposta para escalar (segundos)
ESCALAMENTO_THRESHOLD_S = 3 * 3600  # 3 horas

# Idade máxima do card para ser elegível ao escalamento (segundos)
# Leads retroativos (> 5 dias) ficam nas colunas normais e seguem pelo reativador
ESCALAMENTO_MAX_IDADE_S = 5 * 24 * 3600  # 5 dias

# Mutex Redis — evita processamento duplicado do mesmo card em runs paralelas
_MUTEX_TTL_S = 300  # 5 min


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _normalize(s: str) -> str:
    """Remove acentos, converte para lowercase."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower().strip())


def _is_sorteio(card: dict) -> bool:
    """
    Retorna True se o tipo de contemplação é sorteio ou está vazio (Bazar aceita vazio como sorteio).
    Exclui explicitamente lance, nao-contemplada, etc.
    """
    tipo = _normalize(card.get("Tipo contemplação") or "")
    if not tipo:
        # Vazio: só aceita como sorteio se for Bazar (mesma lógica de _qualifica_bazar)
        canal = get_canal(card)
        return canal == "bazar"
    return "sorteio" in tipo


def _lead_respondeu(card: dict) -> bool:
    """
    Retorna True se o lead enviou ao menos UMA mensagem após a ativação.
    Usa o histórico de conversa gravado no FARO (campo Historico Conversa).
    Qualquer mensagem com role='user' conta — não exige extrato.
    """
    history = load_history(card)
    return any(t.get("role") == "user" for t in history)


def _ts_ultima_atividade(card: dict) -> float:
    """
    Retorna o timestamp Unix da última atividade registrada.
    Usa 'Ultima atividade' → 'Data de primeira ativação' → created_at como fallback.
    """
    # Campo principal: timestamp Unix gravado em cada ativação/atualização
    ultima = card.get("Ultima atividade") or ""
    if ultima:
        try:
            if str(ultima).isdigit():
                return float(ultima)
            return float(datetime.fromisoformat(
                str(ultima).replace("Z", "+00:00")
            ).timestamp())
        except (ValueError, TypeError):
            pass

    # Fallback: data de primeira ativação (formato DD/MM/YYYY)
    data_str = (card.get("Data de primeira ativação") or "").strip()
    if data_str:
        try:
            d, m, y = data_str.split("/")
            from datetime import timezone
            dt = datetime(int(y), int(m), int(d), 8, 0, 0, tzinfo=TZ_BRASILIA)
            return dt.timestamp()
        except Exception:
            pass

    # Último fallback: created_at do card
    created = card.get("created_at") or ""
    if created:
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass

    return 0.0


def _ts_criacao(card: dict) -> float:
    """
    Retorna o timestamp Unix de criação do card.
    Usa created_at → Data de primeira ativação como fallback.
    Aceita created_at como ISO string, Unix string ou Unix float/int.
    """
    created = card.get("created_at") or ""
    if created:
        try:
            # Float/int direto (timestamp Unix)
            if isinstance(created, (int, float)):
                return float(created)
            s = str(created).strip()
            if s.replace(".", "", 1).isdigit():
                return float(s)
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass

    # Fallback: data de primeira ativação (DD/MM/YYYY)
    data_str = (card.get("Data de primeira ativação") or "").strip()
    if data_str:
        try:
            d, m, y = data_str.split("/")
            dt = datetime(int(y), int(m), int(d), 8, 0, 0, tzinfo=TZ_BRASILIA)
            return dt.timestamp()
        except Exception:
            pass

    return 0.0


def _adm_qualificada(card: dict, canal: str) -> bool:
    """
    Retorna True se a ADM do card está na lista de compra para o canal informado.

    Bazar → ADM_BAZAR_TOKENS
    LP    → ADM_LP_TOKENS (Bazar + extras: BB, Rodobens, Disal, Mapfre, HS)

    ADM vazia → False (sem informação suficiente para escalar)
    """
    adm_raw = (card.get("Adm") or card.get("adm") or "").strip()
    if not adm_raw:
        logger.debug(
            "Escalador: card %s sem ADM preenchida — não escala",
            card.get("id", "")[:8],
        )
        return False

    if canal == "lp":
        ok = _adm_matches(adm_raw, ADM_LP_TOKENS, exact_siglas=_LP_EXACT_SIGLAS)
    else:
        ok = _adm_matches(adm_raw, ADM_BAZAR_TOKENS)

    if not ok:
        logger.debug(
            "Escalador: card %s — ADM '%s' não qualificada para canal '%s' — não escala",
            card.get("id", "")[:8], adm_raw, canal,
        )
    return ok


def _elegivel(card: dict) -> bool:
    """
    Retorna True se o card deve ser escalado.

    Critérios (ordem de custo crescente):
      1. Canal = bazar ou lp
      2. Tipo contemplação = sorteio (ou vazio para Bazar)
      3. ADM qualificada para compra no canal correto
      4. Card criado há no máximo 5 dias (retroativos ficam no fluxo normal)
      5. Tempo sem atividade >= 3h
      6. Lead NÃO respondeu (nenhuma mensagem role=user no histórico)
    """
    canal = get_canal(card)
    if canal not in ("bazar", "lp"):
        return False

    if not _is_sorteio(card):
        return False

    # Filtro de ADM qualificada — segunda camada de segurança
    if not _adm_qualificada(card, canal):
        return False

    # Filtro de idade: só escala leads criados nos últimos 5 dias
    # Retroativos (> 5 dias) ficam nas colunas normais e seguem pelo reativador
    ts_criacao = _ts_criacao(card)
    if ts_criacao > 0.0:
        idade_s = time.time() - ts_criacao
        if idade_s > ESCALAMENTO_MAX_IDADE_S:
            logger.debug(
                "Escalador: card %s criado há %.1f dias — acima do limite de 5 dias, ignorando",
                card.get("id", "")[:8], idade_s / 86400,
            )
            return False

    ts = _ts_ultima_atividade(card)
    if ts == 0.0:
        logger.debug("Escalador: card %s sem timestamp de atividade — ignorando", card.get("id", "")[:8])
        return False

    if (time.time() - ts) < ESCALAMENTO_THRESHOLD_S:
        return False

    if _lead_respondeu(card):
        return False

    return True


async def _escalar_card(faro: FaroClient, card: dict) -> bool:
    """
    Escala um card para FINALIZACAO_COMERCIAL.
    Retorna True se o escalamento foi realizado com sucesso.
    """
    card_id = card["id"]
    nome = get_name(card)
    adm = get_adm(card)
    phone = get_phone(card)
    canal = get_canal(card)

    # ── Mutex Redis: evita processar o mesmo card em runs simultâneas ────────
    try:
        from services.session_store import acquire_mutex, release_mutex
        mutex_key = f"job:escalador:{card_id}"
        if not await acquire_mutex(mutex_key, ttl=_MUTEX_TTL_S):
            logger.debug("Escalador: card %s já em processamento — pulando", card_id[:8])
            return False
    except Exception as e:
        logger.warning("Escalador: mutex falhou para %s (%s) — prosseguindo", card_id[:8], e)
        mutex_key = None

    try:
        # ── Porteiro de stage: re-verifica no FARO antes de mover ────────────
        # Evita escalar lead que respondeu ou avançou entre o fetch e agora
        try:
            card_atual = await faro.get_card(card_id)
            stage_atual = (card_atual or {}).get("stage_id", "")

            if stage_atual != Stage.PRIMEIRA_ATIVACAO:
                logger.info(
                    "Escalador: card %s já saiu de PRIMEIRA_ATIVACAO (stage=%s) — ignorando",
                    card_id[:8], stage_atual[:8] if stage_atual else "?",
                )
                return False

            # Re-verifica histórico com dados frescos do FARO
            if _lead_respondeu(card_atual):
                logger.info(
                    "Escalador: card %s respondeu entre fetch e escalamento — ignorando",
                    card_id[:8],
                )
                return False

        except FaroError as e:
            logger.warning("Escalador: falha ao re-verificar card %s (%s) — abortando por segurança", card_id[:8], e)
            return False

        # ── Move para FINALIZACAO_COMERCIAL ──────────────────────────────────
        tempo_parado_h = round((time.time() - _ts_ultima_atividade(card)) / 3600, 1)

        try:
            await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
            await faro.update_card(card_id, {
                "Situacao Negociacao": "sem-resposta-3h",
                "Ultima atividade": str(int(time.time())),
            })
        except FaroError as e:
            logger.error("Escalador: erro ao mover card %s: %s", card_id[:8], e)
            return False

        logger.info(
            "Escalador ✅: card=%s | %s (%s) | canal=%s | %.1fh sem resposta → FINALIZACAO_COMERCIAL",
            card_id[:8], nome, adm, canal, tempo_parado_h,
        )

        # ── Notifica equipe ───────────────────────────────────────────────────
        data_ativacao = card.get("Data de primeira ativação") or "—"
        hora_brt = datetime.now(TZ_BRASILIA).strftime("%d/%m %H:%M")

        notif = (
            f"🔔 *Lead encaminhado para Agente Comercial*\n\n"
            f"👤 *{nome}* | {adm}\n"
            f"📱 Telefone: `{phone or 'não informado'}`\n"
            f"🌐 Fonte: {canal.upper()}\n"
            f"📅 Ativado em: {data_ativacao}\n"
            f"⏱️ Sem resposta há: *{tempo_parado_h}h*\n"
            f"🕐 Escalado às: {hora_brt} BRT"
        )

        try:
            await notify_team(notif)
        except Exception as e:
            logger.warning("Escalador: falha ao notificar equipe para card %s: %s", card_id[:8], e)
            # Não aborta — card já foi movido com sucesso

        return True

    finally:
        # Libera mutex independente do resultado
        try:
            if mutex_key:
                await release_mutex(mutex_key)
        except Exception:
            pass


async def run_escalador_bazar_lp() -> None:
    """
    Job principal: busca todos os cards em PRIMEIRA_ATIVACAO,
    filtra os elegíveis e escala para FINALIZACAO_COMERCIAL.
    """
    if not _is_within_send_window():
        logger.debug("Escalador: fora da janela 08h–20h BRT — pulando")
        return

    logger.info("=== Iniciando Escalador Bazar/LP (3h sem resposta) ===")

    async with FaroClient() as faro:
        try:
            cards = await faro.get_cards_all_pages(
                stage_id=Stage.PRIMEIRA_ATIVACAO,
                page_size=100,
            )
        except FaroError as e:
            logger.error("Escalador: erro ao buscar cards PRIMEIRA_ATIVACAO: %s", e)
            return

    if not cards:
        logger.info("Escalador: nenhum card em PRIMEIRA_ATIVACAO")
        return

    if TEST_MODE:
        cards = filter_test_cards(cards)
        if not cards:
            return

    # Filtra elegíveis localmente (rápido, sem I/O)
    elegíveis = [c for c in cards if _elegivel(c)]

    if not elegíveis:
        logger.info("Escalador: %d cards verificados, nenhum elegível para escalamento", len(cards))
        return

    logger.info(
        "Escalador: %d/%d cards elegíveis para escalamento (3h sem resposta, Bazar/LP sorteio)",
        len(elegíveis), len(cards),
    )

    # Processa em paralelo com semáforo — máx 3 simultâneos
    ok = 0
    sem = asyncio.Semaphore(3)

    async def _process(card: dict) -> None:
        nonlocal ok
        async with sem:
            async with FaroClient() as faro:
                result = await _escalar_card(faro, card)
            if result:
                ok += 1

    await asyncio.gather(*[_process(c) for c in elegíveis], return_exceptions=True)

    logger.info("=== Escalador concluído: %d/%d escalados ===", ok, len(elegíveis))


async def run_escalador_bazar_lp_safe() -> None:
    """Wrapper resiliente para o scheduler — captura exceções sem derrubar o APScheduler."""
    try:
        await run_escalador_bazar_lp()
    except Exception as e:
        logger.exception("run_escalador_bazar_lp: erro inesperado: %s", e)
        try:
            from services.slack import slack_error
            await slack_error("Job escalador_bazar_lp falhou inesperadamente", exception=e)
        except Exception:
            pass
