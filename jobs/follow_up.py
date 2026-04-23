"""
jobs/follow_up.py — Follow-up após envio de proposta

Lógica:
  1. Busca todos os cards em EM_NEGOCIACAO
  2. Filtra: última atividade há mais de 25 min E menos de 8 follow-ups enviados
  3. Gera mensagem personalizada com IA (tom profissional, saudação por horário)
  4. Envia via provider correto (Whapi para Listas, Z-API para demais)
  5. Incrementa campo "Num Follow Ups" e atualiza "Ultima atividade"
  6. Para quando "Num Follow Ups" >= 8 (garante mínimo de 7 seguros)

Frequência sugerida: a cada 30 minutos
"""

import logging
import time
from datetime import datetime, timezone

from config import (
    Stage,
    SEND_WINDOW_START,
    SEND_WINDOW_END,
    JOB_BATCH_LIMIT,
    NOTIFY_PHONES,
    TEST_MODE,
    TZ_BRASILIA,
    filter_test_cards,
)
from services.faro import (
    FaroClient, FaroError,
    get_phone, get_name, get_adm, is_lista,
    load_history, build_card_context,
    load_journey, journey_to_text,
)
from services.whapi import WhapiClient, WhapiError
from services.zapi import ZAPIClient, ZAPIError, get_zapi_for_card
from services.ai import AIClient, AIError

logger = logging.getLogger(__name__)

# Máximo de follow-ups por lead (para após o 8º → garante mínimo de 7)
MAX_FOLLOW_UPS = 8

# Tempo mínimo entre follow-ups (segundos) — evita spam
MIN_INTERVAL_S = 25 * 60  # 25 minutos


def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


def _should_followup(card: dict) -> bool:
    """
    Verifica se o card precisa de follow-up:
    - Menos de MAX_FOLLOW_UPS já enviados
    - Última atividade há mais de MIN_INTERVAL_S segundos
    """
    num_fups = int(card.get("Num Follow Ups") or "0")
    if num_fups >= MAX_FOLLOW_UPS:
        return False

    ultima = card.get("Ultima atividade") or ""
    if not ultima:
        return True

    # "Ultima atividade" pode ser ISO string ou timestamp unix
    try:
        if ultima.isdigit():
            ts = int(ultima)
        else:
            # ISO 8601 → timestamp
            dt = datetime.fromisoformat(ultima.replace("Z", "+00:00"))
            ts = int(dt.timestamp())
        elapsed = time.time() - ts
        return elapsed >= MIN_INTERVAL_S
    except (ValueError, TypeError):
        return True


_FOLLOWUP_SYSTEM = """
Você é Manuela, consultora da Consórcio Sorteado — empresa que COMPRA cotas contempladas.
Você está fazendo follow-up com um lead que parou de responder em algum momento da negociação.

REGRAS GERAIS:
- Tom caloroso e natural, nunca robótico ou repetitivo
- Máximo 3 linhas — mensagem curta e direta
- Varie a abordagem conforme o número de tentativa (informado no prompt)
- Use o nome do lead
- Nunca revele ser IA
- Se for a última tentativa (8ª), deixe uma despedida gentil sem pressão

ADAPTE O TOM CONFORME A SITUAÇÃO DA NEGOCIAÇÃO:
- Sem situação / proposta inicial: desperte curiosidade sobre a proposta enviada
- MELHORAR_VALOR / NEGOCIAR / CONTRA_PROPOSTA / OFERECERAM_MAIS / RECUSAR:
    Mencione que há uma nova proposta melhorada aguardando resposta — crie senso de oportunidade
- DUVIDA / DESCONFIANCA: pergunte se surgiu mais alguma dúvida
- OUTRO / AGENDAR: retome a conversa de forma natural e calorosa
""".strip()

_FOLLOWUP_PROMPT = """
DADOS DO LEAD:
{dados_card}

JORNADA DO LEAD ATÉ AQUI:
{jornada}

SITUAÇÃO ATUAL DA NEGOCIAÇÃO: {situacao}
NÚMERO DA TENTATIVA: {num} de {max_fups}
HORÁRIO: {saudacao}
ÚLTIMA TENTATIVA: {ultima_tentativa}

HISTÓRICO RECENTE DA CONVERSA (últimas mensagens trocadas):
{historico}

Gere UMA mensagem de follow-up curta (máx 3 linhas) adequada para esta tentativa e situação.
Adapte o tom ao perfil do lead (campo "Tom do lead" na jornada, se disponível).
Retorne apenas o texto da mensagem, sem aspas, sem explicações.
""".strip()


_SITUACAO_LABEL: dict[str, str] = {
    "MELHORAR_VALOR":   "lead pediu melhora de valor — nova proposta maior foi enviada",
    "CONTRA_PROPOSTA":  "lead fez contraproposta — nova proposta foi enviada em resposta",
    "OFERECERAM_MAIS":  "concorrente ofereceu mais — nova proposta foi enviada para igualar",
    "NEGOCIAR":         "lead pediu negociação — nova proposta melhorada foi enviada",
    "RECUSAR":          "lead recusou — nova proposta escalada foi enviada",
    "DUVIDA":           "lead tinha dúvida sobre o processo — foi respondido",
    "DESCONFIANCA":     "lead demonstrou desconfiança — credenciais da empresa foram apresentadas",
    "AGENDAR":          "lead pediu falar com consultor — handoff foi iniciado",
    "ACEITAR":          "lead aceitou — processo em andamento",
    "OUTRO":            "conversa em andamento",
}

# Intents em que houve escalada de valor (follow-up deve mencionar nova proposta)
_PRICE_ESCALATION_INTENTS = {"MELHORAR_VALOR", "CONTRA_PROPOSTA", "OFERECERAM_MAIS", "NEGOCIAR", "RECUSAR"}


async def _generate_followup_message(ai: AIClient, card: dict, hora: int) -> str:
    """Gera follow-up personalizado para o lead com base no histórico e situação da negociação."""
    nome      = get_name(card)
    num_fups  = int(card.get("Num Follow Ups") or "0") + 1  # próximo número
    saudacao  = "Bom dia" if hora < 12 else ("Boa tarde" if hora < 18 else "Boa noite")
    is_last   = num_fups >= MAX_FOLLOW_UPS
    history   = load_history(card)
    situacao_raw  = (card.get("Situacao Negociacao") or "").strip().upper()
    situacao_desc = _SITUACAO_LABEL.get(situacao_raw, "proposta enviada, aguardando resposta do lead")

    # Resume as últimas 4 mensagens do histórico para contexto
    if history:
        turns = history[-4:]
        historico_txt = "\n".join(
            f"{'Lead' if t['role'] == 'user' else 'Manuela'}: {t['content'][:120]}"
            for t in turns
        )
    else:
        historico_txt = "(sem histórico — proposta enviada, lead não respondeu ainda)"

    # Contexto estruturado da jornada (tom, proposta, origem…)
    journey = load_journey(card)
    jornada_txt = journey_to_text(journey)

    prompt = _FOLLOWUP_PROMPT.format(
        dados_card=build_card_context(card),
        jornada=jornada_txt,
        situacao=situacao_desc,
        num=num_fups,
        max_fups=MAX_FOLLOW_UPS,
        saudacao=saudacao,
        ultima_tentativa="Sim — seja gentil na despedida, sem pressão" if is_last else "Não",
        historico=historico_txt,
    )

    try:
        msg = await ai.complete(prompt=prompt, system=_FOLLOWUP_SYSTEM, max_tokens=120)
        return msg.strip()
    except AIError as e:
        logger.warning("Follow-up IA falhou para %s: %s. Usando fallback.", card.get("id","")[:8], e)

        if is_last:
            return (
                f"{saudacao}, {nome}! Passando para dar um último alô. "
                f"Se mudar de ideia ou precisar de qualquer coisa, é só me chamar. Abraço! 😊"
            )

        # Fallbacks contextuais conforme situação
        if situacao_raw in _PRICE_ESCALATION_INTENTS:
            textos = [
                f"{saudacao}, {nome}! Passou pela nova proposta que enviei? Quero muito fechar com você! 😊",
                f"{nome}, ainda está disponível a proposta melhorada que mandei — qualquer dúvida é só chamar!",
                f"{saudacao}, {nome}! A oferta que melhorei para você ainda está valendo. O que acha? 😊",
                f"{nome}, não quero deixar essa oportunidade passar por você. A proposta continua na mesa! 😊",
            ]
        else:
            textos = [
                f"{saudacao}, {nome}! Passando para saber se você já teve chance de ver a proposta. 😊",
                f"{saudacao}, {nome}! Alguma dúvida sobre a proposta? Estou aqui para ajudar!",
                f"{nome}, ainda tem interesse? Posso tirar qualquer dúvida agora mesmo. 😊",
                f"{saudacao}, {nome}! A proposta continua válida — qualquer coisa é só me chamar!",
            ]
        return textos[(num_fups - 1) % len(textos)]


async def _send_followup(card: dict, message: str) -> bool:
    """Envia o follow-up pelo provider correto. Retorna True se OK."""
    phone = get_phone(card)
    if not phone:
        return False

    try:
        if is_lista(card):
            async with WhapiClient() as whapi:
                await whapi.send_text(phone, message)
        else:
            zapi = get_zapi_for_card(card)
            async with zapi:
                await zapi.send_text(phone, message)
        return True
    except (WhapiError, ZAPIError) as e:
        logger.error("Erro WhatsApp follow-up card %s: %s", card["id"][:8], e)
        return False


_ASSINATURA_PARADO_DIAS = 3
_ASSINATURA_MAX_LEMBRETES = 3  # Máximo de lembretes antes de notificar só a equipe


async def _followup_assinatura_parados(faro: FaroClient) -> None:
    """
    Verifica leads de lista em ASSINATURA (sem ZapSign Token) parados por 3+ dias.
    Envia lembrete de dados pessoais e, após 3 lembretes sem resposta, notifica a equipe.
    """
    try:
        cards = await faro.get_cards_all_pages(
            stage_id=Stage.ASSINATURA,
            page_size=50,
        )
    except FaroError as e:
        logger.error("Follow-up ASSINATURA: erro ao buscar cards: %s", e)
        return

    if not cards:
        return

    limiar_s = _ASSINATURA_PARADO_DIAS * 24 * 3600
    agora    = time.time()

    for card in cards:
        # Só leads de lista sem ZapSign Token (aguardando dados pessoais)
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
        nome    = get_name(card)
        phone   = get_phone(card)
        adm     = get_adm(card)

        num_lembretes = int(card.get("Num Follow Ups Assinatura") or "0")

        if num_lembretes >= _ASSINATURA_MAX_LEMBRETES:
            # Notifica equipe após esgotar lembretes automáticos
            if NOTIFY_PHONES and num_lembretes == _ASSINATURA_MAX_LEMBRETES:
                from services.whapi import WhapiClient, WhapiError
                notif = (
                    f"⏸️ *Lead parado em ASSINATURA*\n"
                    f"Nome: {nome} | Adm: {adm}\n"
                    f"Sem resposta há {_ASSINATURA_PARADO_DIAS}+ dias após {num_lembretes} lembretes.\n"
                    f"Intervenção manual recomendada."
                )
                try:
                    async with WhapiClient() as w:
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
        missing   = [f for f in _REQUIRED_FIELDS if not collected.get(f)]

        if not missing:
            # Dados completos mas extrato não enviado
            bot_msg = (
                f"Oi, {nome}! 😊 Só passando para lembrar que já tenho seus dados pessoais, "
                f"mas ainda aguardo o *extrato detalhado* da sua cota {adm} para gerar o contrato.\n\n"
                f"Pode enviar uma foto ou PDF por aqui mesmo! 📄"
            )
        else:
            missing_labels = [_FIELD_LABELS[f] for f in missing]
            bot_msg = (
                f"Oi, {nome}! 😊 Notei que ainda precisamos de algumas informações para gerar seu contrato:\n\n"
                + "\n".join(f"• *{l}*" for l in missing_labels)
                + f"\n\nAssim que me enviar, dou andamento imediato! 📋"
            )

        try:
            from services.whapi import WhapiClient, WhapiError
            async with WhapiClient() as w:
                await w.send_text(phone, bot_msg)
            await faro.update_card(card_id, {
                "Num Follow Ups Assinatura": str(num_lembretes + 1),
                "Ultima atividade": str(int(agora)),
            })
            logger.info(
                "Follow-up ASSINATURA #%d: card=%s (%s)",
                num_lembretes + 1, card_id[:8], nome,
            )
        except Exception as e:
            logger.error("Follow-up ASSINATURA: erro para card %s: %s", card_id[:8], e)


async def run_follow_up():
    """
    Job de follow-up.
    Busca leads em EM_NEGOCIACAO que não responderam há 25+ min e envia lembrete.
    Garante no mínimo 7 follow-ups por lead (para no 8º).
    """
    if not _is_within_send_window():
        logger.info("Follow-up: fora da janela de envio, pulando.")
        return

    logger.info("=== Iniciando Follow-up ===")
    hora_atual = (datetime.now(timezone.utc).hour - 3) % 24

    async with FaroClient() as faro, AIClient() as ai:
        try:
            cards = await faro.get_cards_all_pages(
                stage_id=Stage.EM_NEGOCIACAO,
                page_size=100,
            )
        except FaroError as e:
            logger.error("Erro buscando cards EM_NEGOCIACAO: %s", e)
            return

        if not cards:
            logger.info("Nenhum card em EM_NEGOCIACAO.")
            return

        # Em TEST_MODE, processa apenas o card de teste
        cards = filter_test_cards(cards)

        # Leads que já esgotaram todos os follow-ups — mover para PERDIDO (sempre, antes do early return)
        esgotados = [c for c in cards if int(c.get("Num Follow Ups") or "0") >= MAX_FOLLOW_UPS]
        for card in esgotados:
            card_id = card["id"]
            nome    = get_name(card)
            adm     = get_adm(card)
            try:
                await faro.move_card(card_id, Stage.PERDIDO)
                logger.info("Follow-up: card %s (%s) esgotou %d tentativas → PERDIDO", card_id[:8], nome, MAX_FOLLOW_UPS)
                # Notifica equipe para eventual intervenção manual
                if NOTIFY_PHONES:
                    from services.whapi import WhapiClient, WhapiError
                    notif = (
                        f"📭 *Lead esgotou follow-ups*\n"
                        f"Nome: {nome}\nAdm: {adm}\n"
                        f"Tentativas: {MAX_FOLLOW_UPS} — movido para PERDIDO.\n"
                        f"Considere intervenção manual se for lead quente."
                    )
                    try:
                        async with WhapiClient() as w:
                            for ph in NOTIFY_PHONES:
                                await w.send_text(ph, notif)
                    except WhapiError as _we:
                        logger.warning("Follow-up: erro ao notificar equipe: %s", _we)
            except FaroError as e:
                logger.error("Follow-up: erro ao mover card esgotado %s: %s", card_id[:8], e)

        # Filtra apenas os que precisam de follow-up agora
        pendentes = [c for c in cards if _should_followup(c)]
        pendentes = pendentes[:JOB_BATCH_LIMIT]

        if not pendentes:
            logger.info("Follow-up: nenhum card elegível no momento.")
            return

        logger.info("%d cards para follow-up (de %d em EM_NEGOCIACAO)", len(pendentes), len(cards))

        total_ok = 0
        for card in pendentes:
            followup_msg = await _generate_followup_message(ai, card, hora_atual)
            success = await _send_followup(card, followup_msg)
            if success:
                total_ok += 1
                num_fups = int(card.get("Num Follow Ups") or "0") + 1
                try:
                    await faro.update_card(card["id"], {
                        "Num Follow Ups": str(num_fups),
                        "Ultima atividade": str(int(time.time())),
                    })
                except FaroError:
                    pass
                logger.info(
                    "Follow-up #%d OK: card=%s",
                    num_fups, card["id"][:8],
                )

    logger.info("=== Follow-up concluído: %d/%d enviados ===", total_ok, len(pendentes))

    # ── Leads em ASSINATURA sem dados há 3+ dias → lembrete ──────────────────
    await _followup_assinatura_parados(faro)
