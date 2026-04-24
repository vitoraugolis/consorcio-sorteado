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
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card
from services.ai import AIClient, AIError
from services.safety_car import audit_response

logger = logging.getLogger(__name__)

MAX_FOLLOW_UPS = 4          # 4 mensagens automáticas; na 5ª → escala para humano
ESCALATION_AT  = 5          # num_fups == ESCALATION_AT → handoff

# Intervalos mínimos entre cada tentativa (em segundos)
_INTERVALS = {
    1: 30  * 60,    # #1 → 30 min após proposta
    2: 90  * 60,    # #2 → 1h30 após #1
    3: 3   * 3600,  # #3 → 3h após #2
    4: 4   * 3600,  # #4 → 4h após #3
}


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _get_interval(num_fups: int) -> int:
    """Retorna o intervalo mínimo em segundos para a próxima tentativa."""
    return _INTERVALS.get(num_fups + 1, _INTERVALS[4])


def _should_followup(card: dict) -> bool:
    num_fups = int(card.get("Num Follow Ups") or "0")
    if num_fups >= ESCALATION_AT:
        return False
    ultima = card.get("Ultima atividade") or ""
    if not ultima:
        return True
    try:
        ts = int(ultima) if str(ultima).isdigit() else int(
            datetime.fromisoformat(ultima.replace("Z", "+00:00")).timestamp()
        )
        intervalo = _get_interval(num_fups)
        return (time.time() - ts) >= intervalo
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
        "tom": "caloroso e curioso — reforça o valor da proposta",
        "foco": "Perguntar naturalmente se o lead teve chance de ver a proposta. Reforçar que a oferta é personalizada e está disponível.",
        "fallbacks": [
            "Oi, {nome}! 😊 Passando para saber se você teve chance de ver a proposta que enviei. Qualquer dúvida, estou aqui!",
            "{nome}, tudo bem? Vi que você recebeu nossa proposta — alguma dúvida antes de darmos o próximo passo? 😊",
        ],
    },
    2: {
        "tom": "entusiasmado com prova social — cria senso de oportunidade",
        "foco": "Compartilhar que o mercado está aquecido e que outras cotas {adm} estão sendo negociadas rapidamente. Criar senso de oportunidade sem pressão excessiva.",
        "fallbacks": [
            "Oi, {nome}! Só para você saber: essa semana já fechamos 3 cotas {adm} similares. O mercado está muito favorável agora! 🔥",
            "{nome}, lembrei de você hoje! Fechamos uma cota {adm} ontem por um valor ótimo — a sua tem perfil muito parecido. 😊",
        ],
    },
    3: {
        "tom": "empático e seguro — quebra objeções",
        "foco": "Endereçar possíveis objeções: processo seguro, pagamento antes de qualquer transferência, empresa com CNPJ (07.931.205/0001-30). Transmitir segurança.",
        "fallbacks": [
            "{nome}, entendo que vender uma cota gera dúvidas. 😊 Só lembrando: o pagamento é à vista na sua conta, ANTES de qualquer transferência. 100% seguro.",
            "Oi, {nome}! Se a preocupação for segurança — somos CNPJ 07.931.205/0001-30, Rua Irmã Carolina 45, SP. Pagamos antes de qualquer transferência. 🤝",
        ],
    },
    4: {
        "tom": "urgente mas respeitoso — última mensagem automática",
        "foco": "Informar que essa é a última mensagem automática antes de encerrar o contato por esse canal. Deixar a porta aberta. Tom de despedida gentil mas com leve senso de escassez.",
        "fallbacks": [
            "{nome}, vou ser sincera: essa é minha última mensagem por aqui. Se quiser conversar ainda, é só responder — estarei esperando! 😊",
            "Oi, {nome}. Não quero ser insistente, então essa é minha última tentativa. A proposta segue válida. Qualquer coisa, é só me chamar! 🤝",
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

    if not NOTIFY_PHONES:
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

    try:
        canal = "lista" if is_lista(card) else "bazar"
        async with WhapiClient(canal=canal) as w:
            for ph in NOTIFY_PHONES:
                await w.send_text(ph, notif)
        logger.info("Follow-up: equipe notificada para card %s", card_id[:8])
    except WhapiError as e:
        logger.error("Follow-up: erro ao notificar equipe card %s: %s", card_id[:8], e)


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
                try:
                    async with WhapiClient(canal="lista") as w:
                        for ph in NOTIFY_PHONES:
                            await w.send_text(ph, notif)
                except WhapiError:
                    pass
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
        para_escalar = [c for c in cards if int(c.get("Num Follow Ups") or "0") >= ESCALATION_AT]
        for card in para_escalar:
            await _escalate_to_human(faro, card)

        # Follow-ups automáticos
        pendentes = [
            c for c in cards
            if int(c.get("Num Follow Ups") or "0") < ESCALATION_AT and _should_followup(c)
        ][:JOB_BATCH_LIMIT]

        if not pendentes:
            logger.info("Follow-up: nenhum card elegível.")
        else:
            logger.info("%d cards para follow-up", len(pendentes))
            total_ok = 0
            for card in pendentes:
                num_atual = int(card.get("Num Follow Ups") or "0")
                followup_msg = await _generate_followup_message(ai, card, hora_atual)
                success = await _send_followup(card, followup_msg)
                if success:
                    total_ok += 1
                    try:
                        await faro.update_card(card["id"], {
                            "Num Follow Ups": str(num_atual + 1),
                            "Ultima atividade": str(int(time.time())),
                        })
                    except FaroError:
                        pass
                    logger.info(
                        "Follow-up #%d OK: card=%s | intervalo_próximo=%s",
                        num_atual + 1, card["id"][:8],
                        f"{_get_interval(num_atual + 1) // 60}min" if num_atual + 1 < MAX_FOLLOW_UPS else "escalar"
                    )
            logger.info("=== Follow-up concluído: %d/%d ===", total_ok, len(pendentes))

        await _followup_assinatura_parados(faro)
