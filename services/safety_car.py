"""
services/safety_car.py — Supervisor de Processos e Auditor de Respostas

Duas responsabilidades:

1. PIPELINE MONITOR — job periódico que verifica se todos os leads estão
   avançando conforme esperado. Detecta travamentos, silêncios prolongados
   e anomalias de volume. Alerta no Slack com contexto acionável.

2. RESPONSE AUDITOR — interceptor síncrono que valida cada mensagem antes
   de ser enviada ao lead. Garante que o tom é humano, os dados estão
   corretos e a resposta é coerente com o histórico.

Filosofia: fail-safe. Se o Safety Car falhar por qualquer motivo,
o sistema principal continua operando (nunca bloqueia o fluxo por erro
interno do supervisor).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from config import (
    Stage, REATIVACAO_DIAS, SEND_WINDOW_START, SEND_WINDOW_END, TZ_BRASILIA,
)
from services.faro import FaroClient, FaroError, get_name, get_phone, get_adm, load_journey
from services.slack import slack_error
from services.ai import AIClient, AIError

logger = logging.getLogger(__name__)

# ─── Constantes de SLA ────────────────────────────────────────────────────────

# Tempo máximo em cada stage antes de alertar (em segundos)
_SLA = {
    Stage.PRECIFICACAO:   2  * 3600,   # 2h sem proposta → alerta
    Stage.EM_NEGOCIACAO:  24 * 3600,   # 24h sem atividade → alerta
    Stage.ACEITO:         4  * 3600,   # 4h sem contrato → alerta
    Stage.ASSINATURA:     48 * 3600,   # 48h sem assinatura → alerta
}

# Janela de silêncio aceitável para reativações (multiplica o SLA configurado)
_REATIVACAO_TOLERANCIA = 1.5

# Prompt de auditoria de resposta
_AUDIT_SYSTEM = """
Você é um auditor de qualidade de atendimento ao cliente.

Sua tarefa é analisar uma resposta gerada por IA que será enviada a um lead de vendas.
O lead NÃO PODE saber que está sendo atendido por IA — o atendimento deve parecer 100% humano.

Avalie a resposta nos seguintes critérios:
1. TOM HUMANO: A resposta soa natural, como uma consultora brasileira real escreveria no WhatsApp?
2. COERÊNCIA: A resposta está alinhada com o contexto da conversa e os dados do lead?
3. DADOS CORRETOS: Nomes, valores, administradora estão corretos e consistentes?
4. SEM PROMESSAS IMPOSSÍVEIS: A resposta não promete algo que o sistema não pode cumprir?
5. SEM LINGUAGEM ROBÓTICA: Sem frases genéricas como "Claro! Posso te ajudar com isso."?
6. PROPORCIONAL: O tamanho da resposta é adequado ao contexto? (WhatsApp = mensagens curtas)

Responda em JSON puro:
{
  "aprovado": true/false,
  "score": 0-100,
  "problemas": ["lista de problemas encontrados, vazia se aprovado"],
  "sugestao": "versão corrigida da mensagem (somente se não aprovado, senão null)"
}

IMPORTANTE: Seja criterioso mas não perfeccionista. Score >= 70 = aprovado.
Erros graves (dados errados, promessas impossíveis, tom robótico severo) = reprovar.
""".strip()

# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class AuditResult:
    aprovado: bool
    score: int
    problemas: list[str]
    sugestao: str | None
    mensagem_final: str  # a mensagem que deve ser enviada (original ou sugestão)


@dataclass
class PipelineAnomaly:
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    tipo: str
    card_id: str
    card_nome: str
    stage: str
    detalhes: str
    phone: str | None = None


# ─── Response Auditor ─────────────────────────────────────────────────────────

async def audit_response(
    mensagem: str,
    card: dict,
    historico_txt: str = "",
    agente: str = "agente",
) -> AuditResult:
    """
    Valida uma mensagem antes de enviá-la ao lead.
    Retorna AuditResult com aprovado=True e a mensagem final a enviar.

    Fail-safe: se a auditoria falhar por qualquer motivo, aprova a mensagem
    original para não bloquear o fluxo.
    """
    nome = get_name(card)
    adm = get_adm(card)
    phone = get_phone(card) or "?"
    journey = load_journey(card)

    contexto = f"""
AGENTE: {agente}
LEAD: {nome} | ADM: {adm} | Telefone: {phone[-4:]}
DADOS DA JORNADA: {journey}

HISTÓRICO RECENTE:
{historico_txt or "(sem histórico)"}

MENSAGEM A AUDITAR:
{mensagem}
""".strip()

    try:
        async with AIClient() as ai:
            resposta_raw = await ai.complete(
                prompt=contexto,
                system=_AUDIT_SYSTEM,
                max_tokens=400,
                model="gpt-4o-mini",
            )

        import json, re
        m = re.search(r"\{.*\}", resposta_raw, re.DOTALL)
        if not m:
            raise ValueError("Resposta da IA sem JSON")

        data = json.loads(m.group())
        aprovado = bool(data.get("aprovado", True))
        score = int(data.get("score", 80))
        problemas = data.get("problemas") or []
        sugestao = data.get("sugestao")

        mensagem_final = mensagem
        if not aprovado and sugestao:
            mensagem_final = sugestao
            logger.warning(
                "SafetyCar[audit]: %s reprovada (score=%d) para %s | problemas=%s → usando sugestão",
                agente, score, phone[-6:], problemas,
            )
            # Alerta não-bloqueante no Slack
            asyncio.create_task(_alert_audit_failure(agente, card, mensagem, problemas, score, sugestao))
        elif not aprovado:
            logger.warning(
                "SafetyCar[audit]: %s reprovada (score=%d) para %s | sem sugestão → mantém original",
                agente, score, phone[-6:],
            )

        return AuditResult(
            aprovado=aprovado,
            score=score,
            problemas=problemas,
            sugestao=sugestao,
            mensagem_final=mensagem_final,
        )

    except Exception as e:
        # FAIL-SAFE: qualquer falha aprova a mensagem original
        logger.warning("SafetyCar[audit]: falhou para %s (%s) — aprovando original", phone[-6:], e)
        return AuditResult(
            aprovado=True,
            score=-1,
            problemas=[],
            sugestao=None,
            mensagem_final=mensagem,
        )


async def _alert_audit_failure(
    agente: str,
    card: dict,
    mensagem_original: str,
    problemas: list[str],
    score: int,
    sugestao: str,
) -> None:
    """Envia alerta de auditoria reprovada para o Slack."""
    try:
        import httpx
        from config import SLACK_WEBHOOK_URL
        if not SLACK_WEBHOOK_URL:
            return

        nome = get_name(card)
        card_id = card.get("id", "")[:12]
        problemas_txt = "\n".join(f"• {p}" for p in problemas) or "não especificado"

        payload = {
            "text": f"⚠️ Safety Car: resposta reprovada [{agente}]",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "⚠️ Safety Car — Auditoria Reprovada"}},
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    f"*Agente:* `{agente}` | *Lead:* {nome} (`{card_id}`) | *Score:* {score}/100\n\n"
                    f"*Problemas encontrados:*\n{problemas_txt}\n\n"
                    f"*Mensagem original:*\n> {mensagem_original[:300]}\n\n"
                    f"*Substituída por:*\n> {sugestao[:300]}"
                }},
            ],
        }
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(SLACK_WEBHOOK_URL, json=payload)
    except Exception as e:
        logger.warning("SafetyCar: falha ao alertar Slack sobre auditoria: %s", e)


# ─── Pipeline Monitor ─────────────────────────────────────────────────────────

async def run_pipeline_monitor() -> None:
    """
    Job periódico: varre todos os leads ativos e detecta anomalias.
    Envia relatório consolidado no Slack apenas se houver problemas.
    """
    logger.info("SafetyCar[monitor]: iniciando varredura de pipeline...")
    anomalias: list[PipelineAnomaly] = []

    try:
        async with FaroClient() as faro:
            # Verifica cada stage com SLA definido
            for stage_id, sla_seconds in _SLA.items():
                try:
                    cards = await faro.get_cards_from_stage(stage_id=stage_id)
                    for card in cards:
                        anomalia = _check_card_sla(card, stage_id, sla_seconds)
                        if anomalia:
                            anomalias.append(anomalia)
                except FaroError as e:
                    logger.warning("SafetyCar[monitor]: erro ao buscar stage %s: %s", stage_id[:8], e)

            # Verifica leads em reativação com SLA ultrapassado
            for stage_id, dias in REATIVACAO_DIAS.items():
                try:
                    cards = await faro.get_cards_from_stage(stage_id=stage_id)
                    sla_segundos = int(dias * 86400 * _REATIVACAO_TOLERANCIA)
                    for card in cards:
                        anomalia = _check_card_sla(card, stage_id, sla_segundos)
                        if anomalia:
                            anomalia.tipo = "reativacao_atrasada"
                            anomalia.severity = "WARNING"
                            anomalias.append(anomalia)
                except FaroError as e:
                    logger.warning("SafetyCar[monitor]: erro ao buscar reativação %s: %s", stage_id[:8], e)

    except Exception as e:
        logger.error("SafetyCar[monitor]: falha geral na varredura: %s", e)
        await slack_error("Safety Car: falha na varredura de pipeline", exception=e)
        return

    if not anomalias:
        logger.info("SafetyCar[monitor]: pipeline saudável — nenhuma anomalia detectada.")
        return

    # Agrupa por severidade
    criticas  = [a for a in anomalias if a.severity == "CRITICAL"]
    warnings  = [a for a in anomalias if a.severity == "WARNING"]
    infos     = [a for a in anomalias if a.severity == "INFO"]

    logger.warning(
        "SafetyCar[monitor]: %d anomalias detectadas (CRITICAL=%d, WARNING=%d, INFO=%d)",
        len(anomalias), len(criticas), len(warnings), len(infos),
    )

    await _send_pipeline_report(criticas, warnings, infos)


def _check_card_sla(card: dict, stage_id: str, sla_seconds: int) -> PipelineAnomaly | None:
    """
    Verifica se um card está dentro do SLA.
    Retorna PipelineAnomaly se violado, None se ok.
    """
    card_id = card.get("id", "")
    nome = get_name(card)
    phone = get_phone(card)

    ultima_raw = card.get("Ultima atividade") or card.get("updated_at") or ""
    if not ultima_raw:
        # Sem timestamp → alerta INFO
        return PipelineAnomaly(
            severity="INFO",
            tipo="sem_timestamp",
            card_id=card_id,
            card_nome=nome,
            stage=stage_id[:8],
            detalhes="Sem registro de última atividade",
            phone=phone,
        )

    try:
        if str(ultima_raw).isdigit():
            ts = int(ultima_raw)
        else:
            ts = int(datetime.fromisoformat(str(ultima_raw).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None

    elapsed = time.time() - ts
    if elapsed < sla_seconds:
        return None  # dentro do SLA

    horas_atraso = int((elapsed - sla_seconds) / 3600)
    severity: Literal["INFO", "WARNING", "CRITICAL"] = (
        "CRITICAL" if elapsed > sla_seconds * 3
        else "WARNING" if elapsed > sla_seconds * 1.5
        else "INFO"
    )

    return PipelineAnomaly(
        severity=severity,
        tipo="sla_violado",
        card_id=card_id,
        card_nome=nome,
        stage=stage_id[:8],
        detalhes=f"SLA excedido em {horas_atraso}h (limite: {sla_seconds // 3600}h)",
        phone=phone,
    )


async def _send_pipeline_report(
    criticas: list[PipelineAnomaly],
    warnings: list[PipelineAnomaly],
    infos: list[PipelineAnomaly],
) -> None:
    """Envia relatório consolidado de anomalias para o Slack."""
    try:
        import httpx
        from config import SLACK_WEBHOOK_URL
        if not SLACK_WEBHOOK_URL:
            return

        agora_br = datetime.now(TZ_BRASILIA).strftime("%d/%m %H:%M")
        total = len(criticas) + len(warnings) + len(infos)

        emoji = "🚨" if criticas else "⚠️" if warnings else "ℹ️"
        header = f"{emoji} Safety Car — {total} anomalia(s) detectada(s) · {agora_br}"

        blocks: list[dict] = [
            {"type": "header", "text": {"type": "plain_text", "text": header}},
        ]

        def _anomalia_lines(anomalias: list[PipelineAnomaly], emoji: str) -> str:
            lines = []
            for a in anomalias[:10]:  # máx 10 por severidade
                phone_suffix = f" · `{a.phone[-6:]}`" if a.phone else ""
                lines.append(
                    f"{emoji} *{a.card_nome}* (`{a.card_id[:8]}`){phone_suffix}\n"
                    f"   Stage: `{a.stage}` | {a.detalhes}"
                )
            if len(anomalias) > 10:
                lines.append(f"_...e mais {len(anomalias) - 10} ocorrências_")
            return "\n".join(lines)

        if criticas:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": f"*🚨 CRÍTICO ({len(criticas)})*\n{_anomalia_lines(criticas, '🚨')}"}})

        if warnings:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": f"*⚠️ ATENÇÃO ({len(warnings)})*\n{_anomalia_lines(warnings, '⚠️')}"}})

        if infos:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": f"*ℹ️ INFO ({len(infos)})*\n{_anomalia_lines(infos, 'ℹ️')}"}})

        blocks.append({"type": "divider"})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"🏎️ Safety Car · Varredura automática · {agora_br}"}]})

        payload = {"text": header, "blocks": blocks}

        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(SLACK_WEBHOOK_URL, json=payload)

    except Exception as e:
        logger.error("SafetyCar: falha ao enviar relatório Slack: %s", e)
