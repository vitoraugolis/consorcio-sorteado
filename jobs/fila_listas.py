"""
jobs/fila_listas.py — Fila unificada de ativação de Listas (1ª → 4ª ativação)

Substitui ativacao_listas.py + reativador.py para o fluxo de Listas.

Lógica:
  1. A cada ciclo (5 min + jitter aleatório de ±90s) busca 1 card para disparar
  2. Prioridade decrescente: Listas → 1ª → 2ª → 3ª → 4ª Ativação
  3. Round-robin entre tokens do pool "lista" com gap mínimo de 25 min por token
  4. Canal Whapi preservado por origem do lead:
       - origem lista/lp → pool de tokens lista (round-robin)
       - origem bazar    → canal bazar (sem alteração)
  5. Respeita janela de envio (SEND_WINDOW_START–SEND_WINDOW_END BRT)
  6. Todas as proteções do sistema original preservadas:
       - mutex por card (evita disparo duplo)
       - mutex listas_sent por telefone (TTL 24h, só para etapa Listas)
       - porteiro de stage (re-verifica antes de enviar)
       - cutoff de data (04/05/2026)
       - filter_test_cards em TEST_MODE
"""

import asyncio
import logging
import os
import random
import time
from datetime import datetime, timezone

from config import (
    Stage, ACTIVATION_SEQUENCE, REATIVACAO_DIAS,
    SEND_WINDOW_START, SEND_WINDOW_END,
    TEST_MODE, TZ_BRASILIA, filter_test_cards,
)
from services.faro import FaroClient, FaroError, get_name, get_adm, get_phone
from services.whapi import WhapiClient, WhapiError, resolve_phone, WHAPI_LISTA_TOKENS
from services.session_store import acquire_mutex, release_mutex, get_redis
from services.slack import slack_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

# Intervalo base entre disparos (segundos) — jitter aplicado em cima
CICLO_BASE_S      = int(os.getenv("FILA_LISTAS_CICLO_BASE_S",  "300"))   # 5 min
CICLO_JITTER_S    = int(os.getenv("FILA_LISTAS_CICLO_JITTER_S", "90"))   # ±90s

# Gap mínimo entre dois disparos pelo mesmo token (segundos)
TOKEN_GAP_MIN_S   = int(os.getenv("FILA_LISTAS_TOKEN_GAP_S", "1500"))    # 25 min

# Cutoff de data — leads ativados antes disso não recebem follow-up
REATIVACAO_CUTOFF = "04/05/2026"  # DD/MM/AAAA

# Link do grupo (quarta ativação)
_GRUPO_LINK = "https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t"

# Prioridade de etapas — ordem de busca
PRIORITY_STAGES = [
    Stage.LISTAS,
    Stage.PRIMEIRA_ATIVACAO,
    Stage.SEGUNDA_ATIVACAO,
    Stage.TERCEIRA_ATIVACAO,
    Stage.QUARTA_ATIVACAO,
]

# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------

# Mensagem inicial (etapa Listas — primeiro contato)
_ACTIVATION_HEADER = (
    "Meu nome é Manuela, da Consórcio Sorteado, empresa que está há 30 anos "
    "no mercado de cotas contempladas."
)
_ACTIVATION_MESSAGE = (
    "⚡️ {nome}, identificamos em um dos grupos em que somos consorciados que você "
    "tem uma cota contemplada {adm}! E por isso, gostaríamos de lembrar que sua cota "
    "pode ser vendida com ótima valorização. 🎉\n\n"
    "Por isso, gostaria de saber: você teria interesse em receber uma proposta "
    "personalizada pela sua cota, sem compromisso?"
)
_ACTIVATION_BUTTONS = [
    {"id": "quero_proposta", "title": "Quero receber proposta"},
    {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
]

# Follow-up para leads lista/lp (botões)
_FOLLOWUP_LISTA = {
    Stage.PRIMEIRA_ATIVACAO: {
        "text": (
            "Sei que você pode estar pensando sobre nossa proposta para a sua cota {adm}. 😊\n\n"
            "💡 Alguns pontos que vale considerar:\n"
            "• Cotas contempladas estão valorizadas — mas o valor oscila com o tempo\n"
            "• O mercado atual está favorável para quem quer vender\n"
            "• Nossa avaliação é gratuita e sem compromisso\n\n"
            "Ainda tem interesse em receber uma proposta personalizada?"
        ),
        "buttons": [
            {"id": "quero_proposta", "title": "Quero receber proposta"},
            {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
        ],
    },
    Stage.SEGUNDA_ATIVACAO: {
        "text": (
            "Esta semana ajudamos 3 pessoas a vender suas cotas contempladas "
            "— e todas ficaram surpresas com a simplicidade do processo! 🙌✨\n\n"
            "Sua cota {adm} pode ter um valor muito interessante no mercado atual.\n\n"
            "Posso preparar uma proposta personalizada para você?"
        ),
        "buttons": [
            {"id": "quero_proposta", "title": "Quero receber proposta"},
            {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
        ],
    },
    Stage.TERCEIRA_ATIVACAO: {
        "text": (
            "Não quero ser insistente, {nome}, mas o mercado de cotas contempladas "
            "está realmente aquecido agora! 📈\n\n"
            "🎯 Alta demanda por cotas {adm} — o processo é simples e rápido.\n\n"
            "Essa pode ser a última vez que entro em contato. "
            "Você toparia receber uma proposta sem compromisso?"
        ),
        "buttons": [
            {"id": "quero_proposta", "title": "Quero receber proposta"},
            {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
        ],
    },
    Stage.QUARTA_ATIVACAO: {
        "text": (
            "Entendo que a venda da sua cota {adm} não faz sentido agora — tudo bem! 😊\n\n"
            "Se um dia mudar de ideia, é só nos chamar. A Consórcio Sorteado estará aqui.\n\n"
            "Aproveitamos para te convidar para o nosso grupo especial:\n"
            f"{_GRUPO_LINK}\n\n"
            "💛 Obrigada pela atenção, {{nome}}!"
        ),
        "buttons": [
            {"id": "quero_proposta", "title": "Quero receber proposta"},
            {"id": "nao_tenho_interesse", "title": "Não tenho interesse"},
        ],
    },
}

# Follow-up para leads bazar (texto simples)
_FOLLOWUP_BAZAR = {
    Stage.PRIMEIRA_ATIVACAO: (
        "Oi, {nome}! 😊 Vi que você demonstrou interesse em vender sua cota {adm}, "
        "mas ainda não conseguimos conversar!\n\n"
        "Só preciso do extrato atualizado da sua cota para fazer a análise. "
        "Tem o extrato em mãos?"
    ),
    Stage.SEGUNDA_ATIVACAO: (
        "{nome}, tudo bem?\n\n"
        "Ontem mesmo fechamos a compra de uma cota {adm} similar à sua — "
        "e o processo foi super rápido! 🎉\n\n"
        "É literalmente só enviar o extrato e nossa equipe já cuida do resto. "
        "Posso esperar você enviar agora?"
    ),
    Stage.TERCEIRA_ATIVACAO: (
        "{nome}, é a Manuela! 😊\n\n"
        "Estou preocupada em não ter conseguido te ajudar ainda...\n\n"
        "Se tiver um 'sim' guardado aí, me manda o extrato da cota {adm} agora "
        "e eu garanto uma análise rápida pra você!"
    ),
    Stage.QUARTA_ATIVACAO: (
        "{nome}, uma mensagem final! 📝\n\n"
        "Entendo que o momento pode não ser ideal. Não tem problema! 😊\n\n"
        "Seu cadastro fica salvo aqui e, quando quiser, é só me chamar.\n\n"
        "Um abraço da Manuela! 💛"
    ),
}

# ---------------------------------------------------------------------------
# Controle de gap por token (Redis)
# ---------------------------------------------------------------------------

_REDIS_TOKEN_GAP_PREFIX = "cs:fila_listas:token_last:"


async def _get_token_last_used(token_suffix: str) -> float:
    """Retorna timestamp Unix da última vez que o token foi usado (0 se nunca)."""
    try:
        r = await get_redis()
        val = await r.get(f"{_REDIS_TOKEN_GAP_PREFIX}{token_suffix}")
        return float(val) if val else 0.0
    except Exception:
        return 0.0


async def _set_token_last_used(token_suffix: str) -> None:
    """Grava timestamp atual como último uso do token."""
    try:
        r = await get_redis()
        await r.set(f"{_REDIS_TOKEN_GAP_PREFIX}{token_suffix}", str(time.time()), ex=3600)
    except Exception:
        pass


def _get_canal(card: dict) -> str:
    """Retorna canal Whapi correto pelo campo Fonte do card."""
    fonte = str(card.get("Fonte") or "").lower()
    if "bazar" in fonte:
        return "bazar"
    if "lp" in fonte or "site" in fonte:
        return "lp"
    return "lista"


def _is_bazar(card: dict) -> bool:
    return _get_canal(card) == "bazar"


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _passou_cutoff(card: dict) -> bool:
    """Retorna True se o lead deve receber follow-up (data >= cutoff ou campo vazio)."""
    data_str = (card.get("Data de primeira ativação") or "").strip()
    if not data_str:
        return True  # campo vazio = assume válido
    try:
        from datetime import date
        d, m, y = data_str.split("/")
        data_card = date(int(y), int(m), int(d))
        dc, mc, yc = REATIVACAO_CUTOFF.split("/")
        cutoff = date(int(yc), int(mc), int(dc))
        return data_card >= cutoff
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Seleção de token com gap mínimo
# ---------------------------------------------------------------------------

async def _pick_lista_token_with_gap() -> tuple[str, int] | None:
    """
    Seleciona o próximo token do pool lista que respeita o gap mínimo de TOKEN_GAP_MIN_S
    E está online (health_check com cache Redis de 3 min).

    Estratégia:
    1. Faz health_check em paralelo em todos os tokens do pool (cache 3 min)
    2. Filtra tokens offline
    3. Entre os tokens online + respeitando gap, escolhe o mais ocioso
    4. Se todos offline → loga erro crítico, retorna None
    5. Se todos em cooldown → loga info, retorna None
    """
    pool = WHAPI_LISTA_TOKENS
    if not pool:
        return None

    # ── Passo 1: health_check em paralelo com cache Redis (TTL 3 min) ──────
    _HEALTH_CACHE_PREFIX = "cs:fila_listas:health:"
    _HEALTH_CACHE_TTL    = 180  # segundos — reusa resultado por 3 min

    async def _check_token_health(token: str) -> tuple[str, bool]:
        """Retorna (token, online). Usa cache Redis; só checa API se cache expirado."""
        suffix = token[-8:]
        cache_key = f"{_HEALTH_CACHE_PREFIX}{suffix}"
        try:
            r = await get_redis()
            cached = await r.get(cache_key)
            if cached is not None:
                return token, cached == b"1" or cached == "1"
        except Exception:
            pass  # Redis indisponível → assume online (não bloqueia disparo)

        # Cache expirado ou ausente — checa a API com timeout curto
        try:
            from services.whapi import WhapiClient
            async with WhapiClient(token=token) as w:
                # Timeout reduzido para não atrasar o ciclo
                import httpx as _httpx
                w._client.timeout = _httpx.Timeout(4.0)
                online, status_text = await w.health_check()
        except Exception as e:
            logger.warning("Fila Listas: health_check token ...%s falhou: %s — assumindo offline", suffix, e)
            online = False
            status_text = "ERRO"

        # Grava no cache (1 = online, 0 = offline)
        try:
            r = await get_redis()
            await r.set(cache_key, "1" if online else "0", ex=_HEALTH_CACHE_TTL)
        except Exception:
            pass

        if not online:
            logger.warning(
                "Fila Listas: token ...%s OFFLINE (status=%s) — excluído da seleção neste ciclo",
                suffix, status_text,
            )
        return token, online

    # Checa todos em paralelo
    health_results = await asyncio.gather(*[_check_token_health(t) for t in pool])
    tokens_online = {token for token, online in health_results if online}

    if not tokens_online:
        logger.error(
            "Fila Listas: TODOS os %d tokens offline — nenhum disparo possível. "
            "Verifique os canais no painel Whapi.",
            len(pool),
        )
        return None

    offline_count = len(pool) - len(tokens_online)
    if offline_count > 0:
        logger.info(
            "Fila Listas: %d/%d tokens online — %d offline ignorados neste ciclo",
            len(tokens_online), len(pool), offline_count,
        )

    # ── Passo 2: entre tokens online, escolhe o mais ocioso com gap respeitado ──
    now = time.time()
    best_token = None
    best_idx = -1
    best_idle = -1.0

    for idx, token in enumerate(pool):
        if token not in tokens_online:
            continue  # pula tokens offline
        suffix = token[-8:]
        last = await _get_token_last_used(suffix)
        idle = now - last
        if idle >= TOKEN_GAP_MIN_S and idle > best_idle:
            best_idle = idle
            best_token = token
            best_idx = idx

    if best_token is None:
        # Tokens online existem mas todos em cooldown
        min_wait = TOKEN_GAP_MIN_S + 1
        for token in pool:
            if token not in tokens_online:
                continue
            last = await _get_token_last_used(token[-8:])
            wait = TOKEN_GAP_MIN_S - (now - last)
            if wait < min_wait:
                min_wait = wait
        logger.info(
            "Fila Listas: %d token(s) online mas todos em cooldown — próximo disponível em ~%ds",
            len(tokens_online), max(0, int(min_wait))
        )
        return None

    return best_token, best_idx


# ---------------------------------------------------------------------------
# Busca de card candidato por prioridade de stage
# ---------------------------------------------------------------------------

async def _fetch_candidate(faro: FaroClient) -> tuple[dict, str] | None:
    """
    Busca 1 card candidato percorrendo PRIORITY_STAGES em ordem.
    Para etapas de follow-up: usa check_stage_time com limiar de dias.
    Para etapa Listas: usa get_cards_all_pages.

    Retorna (card, stage_id) ou None se nada elegível.
    """
    for stage_id in PRIORITY_STAGES:

        if stage_id == Stage.LISTAS:
            # Etapa inicial — busca em lotes, para no primeiro card com telefone válido
            # Campo "Data de primeira ativação" ignorado (07/2026)
            # Pula cards já disparados hoje via mutex listas_sent
            try:
                r = await get_redis()
                offset = 0
                candidato = None
                while True:
                    batch = await faro.get_cards_from_stage(stage_id=stage_id, limit=50, offset=offset)
                    if not batch:
                        break
                    batch = filter_test_cards(batch) if TEST_MODE else batch
                    for c in batch:
                        phone_raw = c.get("Telefone") or c.get("Telefone alternativo") or ""
                        phone_digits = "".join(ch for ch in str(phone_raw) if ch.isdigit())
                        if not phone_digits:
                            continue  # sem telefone — pula
                        # Pula se já disparado hoje
                        already = await r.get(f"cs:mutex:listas_sent:{phone_digits}")
                        if already:
                            continue
                        candidato = c
                        break
                    if candidato or len(batch) < 50:
                        break
                    offset += 50
                if candidato:
                    logger.debug("Fila Listas: candidato Listas encontrado após offset=%d", offset)
                    return candidato, stage_id
            except FaroError as e:
                logger.warning("Fila Listas: erro ao buscar etapa Listas: %s", e)
            continue

        # Etapas de follow-up — respeita dias de espera
        dias = REATIVACAO_DIAS.get(stage_id, 2)
        try:
            meta_cards = await faro.check_stage_time(
                stage_id=stage_id,
                days_threshold=dias,
                limit=20,
            )
        except FaroError as e:
            logger.warning("Fila Listas: erro check_stage_time %s: %s", stage_id[:8], e)
            continue

        if not meta_cards:
            continue

        # Busca campos completos — precisamos de Telefone, Fonte, etc.
        sem = asyncio.Semaphore(5)
        card_ids = [c.get("card_id") or c.get("id", "") for c in meta_cards if c.get("card_id") or c.get("id")]

        async def _fetch_one(cid: str) -> dict | None:
            async with sem:
                try:
                    return await faro.get_card(cid)
                except FaroError:
                    return None

        full_cards = [c for c in await asyncio.gather(*[_fetch_one(cid) for cid in card_ids]) if c]
        full_cards = filter_test_cards(full_cards) if TEST_MODE else full_cards

        # Filtra cutoff de data e canal correto (só lista/lp — bazar tem seu próprio reativador)
        # NOTA: bazar continua sendo tratado pelo reativador original para não misturar canais
        elegíveis = [
            c for c in full_cards
            if _passou_cutoff(c) and not _is_bazar(c)
        ]

        if elegíveis:
            logger.debug(
                "Fila Listas: candidato encontrado em %s (%d elegíveis)",
                stage_id[:8], len(elegíveis)
            )
            return elegíveis[0], stage_id

    return None


# ---------------------------------------------------------------------------
# Envio por etapa
# ---------------------------------------------------------------------------

async def _send_listas(card: dict, whapi_token: str) -> bool:
    """Envia mensagem inicial (etapa Listas) com botões interativos."""
    card_id = card["id"]
    nome = get_name(card)
    adm = get_adm(card)
    message = _ACTIVATION_MESSAGE.format(nome=nome, adm=adm)

    # resolve_phone já testa WhatsApp + alternativo + move PROBLEMA_CONTATO
    phone = await resolve_phone(card, canal="lista")
    if not phone:
        logger.warning("Fila Listas: card %s sem número WhatsApp — movido para PROBLEMA_CONTATO", card_id[:8])
        return False

    # Mutex por telefone — evita enviar 2x no dia para o mesmo número
    raw_phone = card.get("Telefone") or card.get("Telefone alternativo") or ""
    phone_digits = "".join(c for c in str(raw_phone) if c.isdigit())
    if phone_digits:
        r = await get_redis()
        already = await r.get(f"cs:mutex:listas_sent:{phone_digits}")
        if already:
            logger.info("Fila Listas: telefone ...%s já recebeu ativação hoje — pulando %s", phone_digits[-4:], card_id[:8])
            return False

    sent = False
    async with WhapiClient(token=whapi_token) as w:
        try:
            await w.send_buttons(
                to=phone,
                message=message,
                buttons=_ACTIVATION_BUTTONS,
                header=_ACTIVATION_HEADER,
            )
            sent = True
        except WhapiError as e:
            if "not found" in str(e).lower() and e.status_code == 404:
                logger.warning("Fila Listas: botões indisponíveis para %s — fallback texto", card_id[:8])
                try:
                    await w.send_text(phone, message)
                    sent = True
                except WhapiError as e2:
                    logger.error("Fila Listas: fallback texto falhou %s: %s", card_id[:8], e2)
            else:
                logger.error("Fila Listas: WhapiError %s: %s", card_id[:8], e)

    if sent:
        if phone_digits:
            await acquire_mutex(f"listas_sent:{phone_digits}", ttl=86400)
        async with FaroClient() as faro:
            await faro.move_card(card_id, Stage.PRIMEIRA_ATIVACAO)
            await faro.update_card(card_id, {
                "Data de primeira ativação": datetime.now(timezone.utc).strftime("%d/%m/%Y"),
                "Ultima atividade": str(int(datetime.now(timezone.utc).timestamp())),
            })
        logger.info("✅ Fila Listas [NOVO]: card=%s phone=...%s", card_id[:8], phone[-4:])
        from services.stats import increment_stat
        await increment_stat("listas")
        from services.slack import log_cs
        asyncio.create_task(log_cs(
            direcao="enviado", canal="lista", phone=phone,
            nome=get_name(card), card_id=card_id[:8], mensagem="1ª ativação (novo lead)",
        ))

    return sent


async def _send_followup(card: dict, from_stage: str, whapi_token: str) -> bool:
    """Envia mensagem de follow-up (1ª → 4ª ativação) para leads lista/lp."""
    card_id = card["id"]
    phone = get_phone(card)
    if not phone:
        logger.warning("Fila Listas: card %s sem telefone — pulando follow-up", card_id[:8])
        return False

    to_stage = ACTIVATION_SEQUENCE.get(from_stage)
    if not to_stage:
        logger.error("Fila Listas: sem próxima etapa para %s", from_stage)
        return False

    # Porteiro de stage — re-verifica antes de enviar
    try:
        async with FaroClient() as faro_check:
            card_atual = await faro_check.get_card(card_id)
        stage_atual = (card_atual or {}).get("stage_id", "")
        if stage_atual and stage_atual != from_stage:
            logger.info(
                "Fila Listas: porteiro stage %s saiu para %s — pulando",
                card_id[:8], stage_atual[:8]
            )
            return False
    except Exception as e:
        logger.warning("Fila Listas: porteiro stage falhou para %s — prosseguindo: %s", card_id[:8], e)

    # Guard de rate-limit (15 min)
    canal = _get_canal(card)
    from services.message_guard import check_reactivation_rate, register_reactivation_rate
    if await check_reactivation_rate(phone, canal):
        logger.info("Fila Listas: rate limit 15min phone=...%s — adiando", phone[-4:])
        return False

    nome = get_name(card)
    adm = get_adm(card)
    msg_data = _FOLLOWUP_LISTA[from_stage]
    text = msg_data["text"].format(nome=nome, adm=adm)

    sent = False
    async with WhapiClient(token=whapi_token) as w:
        try:
            await w.send_buttons(phone, text, msg_data["buttons"])
            sent = True
        except WhapiError as e:
            # Fallback para texto simples quando botões retornam 401 ou 404
            # 401 = "need channel authorization" (número não aceita interativos)
            # 404 = endpoint de botões indisponível no plano/canal
            is_fallback_error = (
                (e.status_code == 401 and "channel authorization" in str(e).lower()) or
                (e.status_code == 404 and "not found" in str(e).lower())
            )
            if is_fallback_error:
                logger.warning(
                    "Fila Listas: botões recusados (HTTP %s) para follow-up %s — fallback texto simples",
                    e.status_code, card_id[:8],
                )
                try:
                    await w.send_text(phone, text)
                    sent = True
                except WhapiError as e2:
                    logger.error("Fila Listas: fallback texto follow-up falhou %s: %s", card_id[:8], e2)
            else:
                logger.error("Fila Listas: WhapiError follow-up %s: %s", card_id[:8], e)

    if sent:
        async with FaroClient() as faro:
            await faro.move_card(card_id, to_stage)
            await faro.update_card(card_id, {
                "Ultima atividade": str(int(datetime.now(timezone.utc).timestamp())),
            })
        await register_reactivation_rate(phone, canal)
        logger.info(
            "✅ Fila Listas [FOLLOW-UP]: card=%s %s→%s phone=...%s",
            card_id[:8], from_stage[:8], to_stage[:8], phone[-4:]
        )
        # Contador de ativação: determina qual número é pelo stage de origem
        from services.stats import increment_stat
        _STAGE_STAT = {
            Stage.LISTAS:           "ativacao_1",
            Stage.LP:               "ativacao_1",
            Stage.PRIMEIRA_ATIVACAO: "ativacao_2",
            Stage.SEGUNDA_ATIVACAO:  "ativacao_3",
            Stage.TERCEIRA_ATIVACAO: "ativacao_4",
        }
        _stat_key = _STAGE_STAT.get(from_stage, "ativacao_1")
        await increment_stat(_stat_key)
        _num_ativacao = {"ativacao_1":"1ª","ativacao_2":"2ª","ativacao_3":"3ª","ativacao_4":"4ª"}.get(_stat_key, "")
        from services.slack import log_cs
        asyncio.create_task(log_cs(
            direcao="enviado", canal="lista", phone=phone,
            nome=get_name(card), card_id=card_id[:8],
            mensagem=f"{_num_ativacao} ativação (follow-up)",
        ))

    return sent


# ---------------------------------------------------------------------------
# Ciclo principal
# ---------------------------------------------------------------------------

async def run_ciclo_fila_listas() -> bool:
    """
    Executa 1 ciclo: busca candidato → seleciona token → dispara.
    Retorna True se disparou, False se pulou (sem candidato ou tokens em cooldown).
    """
    if not _is_within_send_window():
        logger.debug("Fila Listas: fora da janela de envio — pulando ciclo")
        return False

    if os.getenv("LISTAS_ATIVACAO_ENABLED", "true").lower() != "true":
        logger.info("Fila Listas: LISTAS_ATIVACAO_ENABLED=false — pulando ciclo")
        return False

    # Seleciona token disponível ANTES de buscar o candidato
    # (evita trabalho desnecessário se todos em cooldown)
    token_result = await _pick_lista_token_with_gap()
    if token_result is None:
        return False
    token, token_idx = token_result

    # Mutex por token — evita disparo duplo se job for chamado em paralelo
    token_mutex_key = f"fila_listas:token_lock:{token[-8:]}"
    acquired = await acquire_mutex(token_mutex_key, ttl=60)
    if not acquired:
        logger.debug("Fila Listas: token ...%s já em uso — pulando ciclo", token[-8:])
        return False

    try:
        async with FaroClient() as faro:
            result = await _fetch_candidate(faro)

        if result is None:
            logger.debug("Fila Listas: nenhum candidato elegível em nenhuma etapa")
            return False

        card, stage_id = result
        card_id = card["id"]

        # Mutex por card — evita disparo duplo após restart
        card_mutex_key = f"ativacao:{card_id}"
        card_acquired = await acquire_mutex(card_mutex_key)
        if not card_acquired:
            logger.debug("Fila Listas: card %s já em processamento — pulando", card_id[:8])
            return False

        try:
            if stage_id == Stage.LISTAS:
                success = await _send_listas(card, token)
            else:
                success = await _send_followup(card, stage_id, token)

            if success:
                await _set_token_last_used(token[-8:])
                # Confirma token online no cache de health (evita check desnecessário
                # na próxima execução quando o token acabou de disparar com sucesso)
                try:
                    r = await get_redis()
                    await r.set(f"cs:fila_listas:health:{token[-8:]}", "1", ex=180)
                except Exception:
                    pass

            return success
        finally:
            await release_mutex(card_mutex_key)

    except Exception as e:
        logger.exception("Fila Listas: erro inesperado no ciclo: %s", e)
        return False
    finally:
        await release_mutex(token_mutex_key)


async def run_fila_listas_safe():
    """
    Wrapper chamado pelo scheduler a cada ~5 min.
    Executa 1 ciclo e loga o resultado.
    """
    try:
        disparou = await run_ciclo_fila_listas()
        if disparou:
            logger.info("Fila Listas: ciclo concluído — 1 disparo realizado")
        else:
            logger.debug("Fila Listas: ciclo concluído — nenhum disparo")
    except Exception as e:
        logger.exception("run_fila_listas_safe: erro inesperado: %s", e)
        try:
            await slack_error("Job fila_listas_safe falhou inesperadamente", exception=e)
        except Exception:
            pass
