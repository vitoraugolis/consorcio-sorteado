"""
jobs/follow_up.py — Follow-up após envio de proposta
Provider: Whapi (get_whapi_for_card — substitui Z-API)

Sequência de 5 tentativas com progressão de convencimento:
  #1 → 30 min  — curiosidade / reforço de valor
  #2 → 1h30    — prova social / urgência
  #3 → 3h      — objeção / segurança
  #4 → 4h      — escassez / última chance antes do humano
  #5 → escalar — handoff para agente comercial humano
"""

import logging
import time
from datetime import datetime, timezone

from config import (
    Stage, SEND_WINDOW_START, SEND_WINDOW_END,
    JOB_BATCH_LIMIT, NOTIFY_PHONES, TEST_MODE, TZ_BRASILIA, filter_test_cards,
    CONSULTANT_PHONES,
)
from services.faro import (
    FaroClient, FaroError,
    get_phone, get_name, get_adm, is_lista,
    load_history, history_to_text, build_card_context,
    load_journey, journey_to_text,
)
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card, notify_team
from services.ai import AIClient, AIError
from services.safety_car import audit_response

logger = logging.getLogger(__name__)

MAX_FOLLOW_UPS = 5          # 5 mensagens automáticas; na 6ª → escala para humano
ESCALATION_AT  = 6          # num_fups == ESCALATION_AT → handoff

# Intervalos mínimos entre cada tentativa (em horas — usados por _next_send_time)
# U2: #1 passou de 45min para 4h — evita mensagem enviada antes do lead processar a proposta
_INTERVAL_HOURS = {
    1: 4,    # #1 → 4h após proposta  (antes: 45min — muito agressivo)
    2: 24,   # #2 → 1 dia após #1     (antes: 3h)
    3: 48,   # #3 → 2 dias após #2    (antes: 8h)
    4: 96,   # #4 → 4 dias após #3    (antes: 1 dia)
    5: 168,  # #5 → 7 dias após #4    (antes: 2 dias) — cadência 14 dias total
}
# Backward compat em segundos para código que ainda usa _INTERVALS
_INTERVALS = {k: v * 3600 for k, v in _INTERVAL_HOURS.items()}


def _is_within_send_window() -> bool:
    """Verifica se agora está dentro da janela de envio (08h–20h BRT)."""
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _next_send_time(last_activity_ts: float, num_fups: int) -> float:
    """
    Calcula o timestamp Unix do próximo envio permitido para este lead.

    Regras:
      1. Soma o intervalo em horas ao último timestamp de atividade
      2. Se o resultado cair fora da janela 08h–20h BRT, avança para
         as 08h do próximo dia útil dentro da janela
      3. Garante que o lead nunca receba mensagem fora de 08h–20h BRT

    Exemplo: proposta às 18h + intervalo 4h = 22h → avança para 08h amanhã
    """
    from datetime import timedelta
    import math

    interval_h = _INTERVAL_HOURS.get(num_fups + 1, _INTERVAL_HOURS[5])
    candidate_ts = last_activity_ts + interval_h * 3600

    candidate_dt = datetime.fromtimestamp(candidate_ts, tz=TZ_BRASILIA)
    hora = candidate_dt.hour

    if SEND_WINDOW_START <= hora < SEND_WINDOW_END:
        return candidate_ts  # já está na janela — ok

    # Fora da janela: avança para 08h do dia seguinte (ou mesmo dia se ainda não chegou)
    if hora >= SEND_WINDOW_END:
        # Passou das 20h → amanhã às 08h
        next_day = candidate_dt.date() + timedelta(days=1)
    else:
        # Antes das 08h → hoje às 08h
        next_day = candidate_dt.date()

    next_window = datetime(
        year=next_day.year, month=next_day.month, day=next_day.day,
        hour=SEND_WINDOW_START, minute=0, second=0,
        tzinfo=TZ_BRASILIA,
    )
    return next_window.timestamp()


def _count_followups(card: dict) -> int:
    """
    Conta follow-ups já realizados para a proposta ATUAL.

    Lógica de reset: se Proposta Realizada mudou desde o último ciclo
    (nova proposta escalada pelo negociador), o contador zera — o lead
    recebe a sequência completa de follow-ups para a nova proposta.

    Compara 'Follow Up Proposta Base' (gravado no primeiro FU do ciclo)
    com 'Proposta Realizada' atual. Se divergirem → contador = 0.
    """
    proposta_atual = str(card.get("Proposta Realizada") or "").strip()
    proposta_base  = str(card.get("Follow Up Proposta Base") or "").strip()

    # Ignora base inválida (vazio, "0" ou "0.0" — campo recém-criado sem valor real)
    _base_invalida = not proposta_base or proposta_base in ("0", "0.0", "0,0")

    # Nova proposta desde o último ciclo → zera a sequência
    if proposta_atual and not _base_invalida and proposta_atual != proposta_base:
        logger.info(
            "Follow-up: card %s — nova proposta detectada (%.0s → %.0s), resetando contador.",
            str(card.get("id", ""))[:8], proposta_base, proposta_atual,
        )
        return 0

    from_field = card.get("Num Follow Ups")
    if from_field is not None:
        try:
            return int(from_field)
        except (ValueError, TypeError):
            pass
    # Fallback: contar mensagens do assistente no histórico
    from services.faro import load_history
    history = load_history(card)
    assistant_turns = sum(1 for t in history if t.get("role") == "assistant")
    return max(0, assistant_turns - 1)  # -1 para não contar a mensagem de proposta


def _get_interval(num_fups: int) -> int:
    """Retorna o intervalo mínimo em segundos para a próxima tentativa."""
    return _INTERVALS.get(num_fups + 1, _INTERVALS[4])


def _should_followup(card: dict) -> bool:
    """
    Retorna True se este card está pronto para receber o próximo follow-up.

    Usa _next_send_time para garantir que:
      - O intervalo configurado já passou
      - O horário calculado está dentro da janela 08h–20h BRT
      - Nenhum follow-up é enviado fora da janela, mesmo que o intervalo tenha passado
    """
    num_fups = _count_followups(card)
    if num_fups >= ESCALATION_AT:
        return False
    ultima = card.get("Ultima atividade") or ""
    if not ultima:
        return True
    try:
        ts = int(ultima) if str(ultima).isdigit() else int(
            datetime.fromisoformat(ultima.replace("Z", "+00:00")).timestamp()
        )
        next_ts = _next_send_time(ts, num_fups)
        return time.time() >= next_ts
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Prompts e mensagens por tentativa
# ---------------------------------------------------------------------------

_FOLLOWUP_SYSTEM = """
Você é Manuela, consultora da Consórcio Sorteado.
Tom: {tom}
Máximo 3 linhas. Nunca revele ser IA. Nunca mencione concorrentes.
Foque no benefício específico desta tentativa.
""".strip()

_FOLLOWUP_PROMPT = """
DADOS DO LEAD: {dados_card}
JORNADA: {jornada}
PROPOSTA ENVIADA: {proposta}
ADMINISTRADORA: {adm}
TENTATIVA: {num} de {max_fups}
SITUAÇÃO: {situacao}
HISTÓRICO RECENTE:
{historico}

FOCO DESTA MENSAGEM: {foco}

Gere UMA mensagem de follow-up natural e persuasiva (máx 3 linhas).
Não use saudações formais. Vá direto ao ponto com o foco indicado.
Apenas o texto, sem aspas, sem formatação markdown.
""".strip()

_FOLLOWUP_CONFIG = {
    1: {
        "tom": "leve e curioso — checagem natural, sem pressão",
        "foco": "Verificar se o lead viu a proposta. Tom de continuidade da conversa. Reforçar que a oferta é personalizada e que está disponível para tirar dúvidas.",
        "fallbacks": [
            "{nome}, vi que você recebeu nossa proposta. 😊 Teve chance de dar uma olhada? Qualquer dúvida, estou aqui!",
            "Oi, {nome}! Passando para saber se a proposta chegou certo. Alguma pergunta antes de darmos o próximo passo?",
        ],
    },
    2: {
        "tom": "entusiasmado com FOMO — oportunidade de mercado, escassez",
        "foco": (
            "Criar urgência real: cotas {adm} contempladas estão valorizadas AGORA, "
            "o mercado de compra está aquecido e essa janela pode fechar. "
            "Quem vende hoje recebe mais do que quem espera. Sem pressão falsa — urgência genuína."
        ),
        "fallbacks": [
            "Oi, {nome}! 🔥 Essa semana a procura por cotas {adm} explodiu por aqui. "
            "O valor que consigo te oferecer hoje pode não ser o mesmo daqui a alguns dias. Ainda tem interesse?",
            "{nome}, só um aviso rápido: temos muita demanda por cotas {adm} contempladas agora. "
            "Quem fecha primeiro garante o melhor valor. A sua proposta ainda está no ar — vamos aproveitar? 📈",
        ],
    },
    3: {
        "tom": "educativo e comparativo — posiciona venda da cota vs outras modalidades",
        "foco": (
            "Comparar venda da cota com financiamento bancário: "
            "financiar imóvel custa de 10% a 14% ao ano em juros — isso dobra o preço final. "
            "Vender a cota é receber o dinheiro hoje, limpo, na conta, sem dívida e sem burocracia. "
            "Posicionar como decisão financeiramente inteligente, não uma perda."
        ),
        "fallbacks": [
            "{nome}, um pensamento rápido: financiar hoje custa entre 10% e 14% ao ano. "
            "Vender sua cota te dá o dinheiro à vista, agora, sem juros e sem dívida. "
            "Financeiramente, faz muito mais sentido. 💡 O que você acha?",
            "Enquanto um financiamento bancário cobra juros que podem dobrar o valor pago, "
            "a venda da sua cota {adm} te coloca dinheiro na conta hoje — zero burocracia, zero dívida. "
            "Isso tem valor, {nome}. 😊",
        ],
    },
    4: {
        "tom": "exclusividade e confiança — empresa sólida, processo seguro",
        "foco": (
            "Reforçar exclusividade e segurança: "
            "não somos uma plataforma genérica — somos especialistas em cotas {adm} há mais de 18 anos. "
            "Pagamento ANTES da transferência — zero risco para o lead. "
            "CNPJ público, endereço físico. Transmitir que poucas empresas oferecem essa garantia."
        ),
        "fallbacks": [
            "{nome}, só para reforçar: somos especializados em cotas {adm} há mais de 18 anos. "
            "O pagamento vai direto na sua conta ANTES de qualquer transferência. "
            "Você não corre nenhum risco. 🔒 Posso dar andamento?",
            "Diferente de plataformas genéricas, aqui você fala com especialistas que conhecem "
            "cada detalhe de cotas {adm}. E o mais importante: pagamos antes. "
            "Sem risco nenhum pra você, {nome}. 🤝",
        ],
    },
    5: {
        "tom": "despedida respeitosa com última janela — FOMO leve, porta aberta",
        "foco": (
            "Última mensagem automática. Informar que o contato automático encerra aqui. "
            "Deixar a porta aberta com leveza. "
            "Leve senso de exclusividade: a proposta não fica disponível indefinidamente. "
            "Tom humano, sem pressão — respeitar a decisão do lead."
        ),
        "fallbacks": [
            "{nome}, vou ser direta: essa é minha última mensagem por este canal. 😊 "
            "A proposta para sua cota {adm} ainda está na mesa, mas não por muito tempo. "
            "Se mudar de ideia, é só responder — estarei esperando!",
            "Oi, {nome}. Não quero ser insistente, então encerro por aqui. "
            "Nossa proposta para a cota {adm} segue válida por mais alguns dias. "
            "Qualquer coisa, é só me chamar. Foi um prazer! 🤝",
        ],
    },
}

_SITUACAO_LABEL: dict[str, str] = {
    "MELHORAR_VALOR":  "lead pediu melhora de valor — nova proposta enviada",
    "CONTRA_PROPOSTA": "lead fez contraproposta — nova proposta enviada",
    "OFERECERAM_MAIS": "concorrente ofereceu mais — nova proposta enviada",
    "NEGOCIAR":        "lead pediu negociação — nova proposta melhorada enviada",
    "RECUSAR":         "lead recusou — nova proposta escalada enviada",
    "DUVIDA":          "lead tinha dúvida — foi respondido",
    "DESCONFIANCA":    "lead demonstrou desconfiança — credenciais apresentadas",
    "AGENDAR":         "lead pediu falar com consultor — handoff iniciado",
    "ACEITAR":         "lead aceitou — processo em andamento",
    "OUTRO":           "proposta enviada, aguardando resposta",
}


async def _generate_followup_message(ai: AIClient, card: dict, hora: int) -> str:
    import random as _r
    nome = get_name(card)
    adm = get_adm(card)
    num_fups = int(card.get("Num Follow Ups") or "0") + 1
    config = _FOLLOWUP_CONFIG.get(num_fups, _FOLLOWUP_CONFIG[4])

    situacao_raw = (card.get("Situacao Negociacao") or "").strip().upper()
    situacao_desc = _SITUACAO_LABEL.get(situacao_raw, _SITUACAO_LABEL["OUTRO"])
    history = load_history(card)
    historico_txt = "\n".join(
        f"{'Lead' if t['role'] == 'user' else 'Manuela'}: {t['content'][:120]}"
        for t in history[-4:]
    ) if history else "(sem histórico)"
    journey = load_journey(card)

    prompt = _FOLLOWUP_PROMPT.format(
        dados_card=build_card_context(card),
        jornada=journey_to_text(journey),
        proposta=card.get("Proposta Realizada", "a consultar"),
        adm=adm,
        num=num_fups,
        max_fups=MAX_FOLLOW_UPS,
        situacao=situacao_desc,
        historico=historico_txt,
        foco=config["foco"].format(nome=nome, adm=adm),
    )
    system = _FOLLOWUP_SYSTEM.format(tom=config["tom"])

    try:
        msg = await ai.complete(prompt=prompt, system=system, max_tokens=120, model="gpt-4o-mini")
        return msg.strip()
    except AIError as e:
        logger.warning("Follow-up IA falhou para %s: %s", card.get("id", "")[:8], e)

    # Fallback estático com variação
    tmpl = _r.choice(config["fallbacks"])
    return tmpl.format(nome=nome, adm=adm)


# ---------------------------------------------------------------------------
# Escalada para humano
# ---------------------------------------------------------------------------

async def _escalate_to_human(faro: FaroClient, card: dict) -> None:
    """Move card para FINALIZACAO_COMERCIAL e notifica equipe."""
    card_id = card["id"]
    nome = get_name(card)
    adm = get_adm(card)
    phone = get_phone(card)

    try:
        await faro.move_card(card_id, Stage.FINALIZACAO_COMERCIAL)
        logger.info("Follow-up: card %s escalado para FINALIZACAO_COMERCIAL", card_id[:8])
    except FaroError as e:
        logger.error("Follow-up: erro ao escalar card %s: %s", card_id[:8], e)
        return

    # Resumo da jornada para o consultor
    journey = load_journey(card)
    jornada_txt = journey_to_text(journey)
    proposta = card.get("Proposta Realizada", "—")

    notif = (
        f"🔔 *Lead para atendimento humano*\n"
        f"Nome: {nome}\n"
        f"Adm: {adm}\n"
        f"Telefone: {phone}\n"
        f"Proposta enviada: {proposta}\n"
        f"Follow-ups realizados: {MAX_FOLLOW_UPS}\n\n"
        f"{jornada_txt}"
    )

    await notify_team(notif)
    logger.info("Follow-up: equipe notificada para card %s", card_id[:8])


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------

async def _send_followup(card: dict, message: str) -> bool:
    phone = get_phone(card)
    if not phone:
        return False
    # ── Safety Car: audita antes de enviar ──────────────────────────────────
    history = load_history(card)
    historico_txt = history_to_text(history, max_turns=6)
    audit = await audit_response(message, card, historico_txt, agente="follow_up")
    message = audit.mensagem_final
    try:
        async with get_whapi_for_card(card) as w:
            await w.send_text(phone, message)
        return True
    except WhapiError as e:
        logger.error("Erro Whapi follow-up card %s: %s", card["id"][:8], e)
        return False


# ---------------------------------------------------------------------------
# Follow-up de ASSINATURA parada
# ---------------------------------------------------------------------------

async def _followup_assinatura_parados(faro: FaroClient) -> None:
    """Verifica leads de lista em ASSINATURA (sem ZapSign Token) parados por 3+ dias."""
    try:
        cards = await faro.get_cards_all_pages(stage_id=Stage.ASSINATURA, page_size=50)
    except FaroError as e:
        logger.error("Follow-up ASSINATURA: erro ao buscar cards: %s", e)
        return
    if not cards:
        return

    _ASSINATURA_PARADO_DIAS = 3
    _ASSINATURA_MAX_LEMBRETES = 3
    limiar_s = _ASSINATURA_PARADO_DIAS * 24 * 3600
    agora = time.time()

    for card in cards:
        if not is_lista(card) or card.get("ZapSign Token"):
            continue
        ultima = card.get("Ultima atividade") or ""
        if not ultima:
            continue
        try:
            ts = int(ultima) if str(ultima).isdigit() else int(
                datetime.fromisoformat(ultima.replace("Z", "+00:00")).timestamp()
            )
        except (ValueError, TypeError):
            continue
        if (agora - ts) < limiar_s:
            continue

        card_id = card["id"]
        nome = get_name(card)
        phone = get_phone(card)
        adm = get_adm(card)
        num_lembretes = int(card.get("Num Follow Ups Assinatura") or "0")

        if num_lembretes >= _ASSINATURA_MAX_LEMBRETES:
            if NOTIFY_PHONES and num_lembretes == _ASSINATURA_MAX_LEMBRETES:
                notif = (
                    f"⏸️ *Lead parado em ASSINATURA*\n"
                    f"Nome: {nome} | Adm: {adm}\n"
                    f"Sem resposta após {num_lembretes} lembretes. Intervenção manual recomendada."
                )
                await notify_team(notif)
            try:
                await faro.update_card(card_id, {"Num Follow Ups Assinatura": str(num_lembretes + 1)})
            except FaroError:
                pass
            continue

        if not phone:
            continue

        from webhooks.agente_contrato import _REQUIRED_FIELDS, _load_collected, _FIELD_LABELS
        collected = _load_collected(card)
        missing = [f for f in _REQUIRED_FIELDS if not collected.get(f)]
        if not missing:
            bot_msg = (
                f"Oi, {nome}! 😊 Só passando para lembrar que já tenho seus dados, "
                f"mas ainda aguardo o *extrato detalhado* da cota {adm}. "
                f"Pode enviar uma foto ou PDF por aqui mesmo! 📄"
            )
        else:
            bot_msg = (
                f"Oi, {nome}! 😊 Ainda precisamos de:\n\n"
                + "\n".join(f"• *{_FIELD_LABELS[f]}*" for f in missing)
                + f"\n\nAssim que me enviar, dou andamento imediato! 📋"
            )
        try:
            async with WhapiClient(canal="lista") as w:
                await w.send_text(phone, bot_msg)
            await faro.update_card(card_id, {
                "Num Follow Ups Assinatura": str(num_lembretes + 1),
                "Ultima atividade": str(int(agora)),
            })
            logger.info("Follow-up ASSINATURA #%d: card=%s", num_lembretes + 1, card_id[:8])
        except Exception as e:
            logger.error("Follow-up ASSINATURA: erro card %s: %s", card_id[:8], e)


# ---------------------------------------------------------------------------
# Job principal
# ---------------------------------------------------------------------------

async def run_follow_up():
    if not _is_within_send_window():
        logger.info("Follow-up: fora da janela de envio, pulando.")
        return
    logger.info("=== Iniciando Follow-up ===")
    hora_atual = (datetime.now(timezone.utc).hour - 3) % 24

    async with FaroClient() as faro, AIClient() as ai:
        try:
            cards = await faro.get_cards_all_pages(stage_id=Stage.EM_NEGOCIACAO, page_size=100)
        except FaroError as e:
            logger.error("Erro buscando cards EM_NEGOCIACAO: %s", e)
            return
        if not cards:
            logger.info("Nenhum card em EM_NEGOCIACAO.")
            return

        cards = filter_test_cards(cards)

        # Escalar cards que atingiram o limite
        para_escalar = [c for c in cards if _count_followups(c) >= ESCALATION_AT]
        for card in para_escalar:
            await _escalate_to_human(faro, card)

        # Follow-ups automáticos
        pendentes = [
            c for c in cards
            if _count_followups(c) < ESCALATION_AT and _should_followup(c)
        ][:JOB_BATCH_LIMIT]

        if not pendentes:
            logger.info("Follow-up: nenhum card elegível.")
        else:
            logger.info("%d cards para follow-up", len(pendentes))
            total_ok = 0

            # Processar em paralelo com semáforo (máx 4 simultâneos — respeita rate limit IA + Whapi)
            import asyncio as _aio
            sem = _aio.Semaphore(4)

            async def _process_one_fu(card: dict) -> None:
                nonlocal total_ok
                async with sem:
                    from services.session_store import acquire_mutex, release_mutex
                    _card_id_fu = card.get("id", "")
                    _mutex_key_fu = f"job:followup:{_card_id_fu}"
                    if not await acquire_mutex(_mutex_key_fu, ttl=180):
                        logger.info("Follow-up: card %s ja em processamento - pulando", _card_id_fu[:8])
                        return
                    try:
                        num_atual = _count_followups(card)
                        followup_msg = await _generate_followup_message(ai, card, hora_atual)
                        success = await _send_followup(card, followup_msg)
                        if success:
                            total_ok += 1
                            try:
                                proposta_atual = str(card.get("Proposta Realizada") or "").strip()
                                proposta_base  = str(card.get("Follow Up Proposta Base") or "").strip()
                                houve_reset    = bool(proposta_atual and proposta_base and proposta_atual != proposta_base)
                                update = {"Ultima atividade": str(int(time.time()))}
                                if proposta_atual:
                                    update["Follow Up Proposta Base"] = proposta_atual
                                if houve_reset:
                                    update["Num Follow Ups"] = "1"
                                elif card.get("Num Follow Ups") is not None:
                                    update["Num Follow Ups"] = str(num_atual + 1)
                                await faro.update_card(card["id"], update)
                            except FaroError:
                                pass
                            logger.info(
                                "Follow-up #%d OK: card=%s | proximo=%s",
                                num_atual + 1, card["id"][:8],
                                f"{_get_interval(num_atual + 1) // 60}min" if num_atual + 1 < MAX_FOLLOW_UPS else "escalar"
                            )
                    finally:
                        await release_mutex(_mutex_key_fu)
            await _aio.gather(*[_process_one_fu(c) for c in pendentes], return_exceptions=True)
            logger.info("=== Follow-up concluído: %d/%d ===", total_ok, len(pendentes))

        await _followup_assinatura_parados(faro)


async def run_follow_up_safe():
    """Wrapper resiliente — garante que exceções não derrubam o scheduler."""
    try:
        await run_follow_up()
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("run_follow_up: erro inesperado: %s", e)
        try:
            from services.slack import slack_error
            await slack_error("Job follow_up falhou inesperadamente", exception=e)
        except Exception:
            pass
