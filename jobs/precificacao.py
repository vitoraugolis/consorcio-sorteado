"""
jobs/precificacao.py — Envio de proposta de precificação ao lead
Provider: Whapi (get_whapi_for_card — substitui Z-API para Bazar/Site)
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

from config import (
    Stage, JOB_BATCH_LIMIT, SEND_WINDOW_START, SEND_WINDOW_END,
    PUBLIC_URL, TEST_MODE, TZ_BRASILIA, filter_test_cards,
)
from services.html_image import render_to_file
from services.faro import (
    FaroClient, FaroError,
    get_phone, get_name, get_adm, is_lista,
    load_history, history_append, save_history,
    load_journey, save_journey,
)
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card
from services.slack import slack_error

logger = logging.getLogger(__name__)

_processing: dict[str, asyncio.Lock] = {}

# ---------------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------------

def _fmt_currency(value: str) -> str:
    if not value:
        return "a consultar"
    val = str(value).strip()
    if "R$" in val and "," in val:
        return val
    try:
        clean = val.replace("R$", "").strip()
        comma_pos = clean.find(",")
        period_pos = clean.find(".")
        if "," in clean and "." in clean:
            clean = clean.replace(".", "").replace(",", ".") if comma_pos > period_pos else clean.replace(",", "")
        elif "," in clean:
            clean = clean.replace(".", "").replace(",", ".")
        else:
            clean = clean.replace(",", "")
        num = float(clean)
        inteiro = int(num)
        centavos = round((num - inteiro) * 100)
        return f"R$ {inteiro:,}".replace(",", ".") + f",{centavos:02d}"
    except (ValueError, TypeError):
        return val


def _fmt_contemplacao(value: str) -> str:
    v = (value or "").lower().replace("-", " ").strip()
    if "sorteio" in v:
        return "Sorteio"
    if "lance" in v:
        return "Lance"
    return value.title() if value else "Sorteio/Lance"


def _get_consultor(card: dict) -> str:
    return (card.get("Responsáveis") or card.get("Responsável")
            or os.getenv("CONSULTOR_NOME", "Manuela"))


# ---------------------------------------------------------------------------
# Geração da imagem HTML
# ---------------------------------------------------------------------------

_MESES_BR = ["Janeiro","Fevereiro","Março","Abril","Maio","Junho",
             "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]


def _build_proposal_html(card: dict) -> str:
    title = card.get("title") or card.get("Nome do contato") or "Cliente"
    adm = get_adm(card)
    grupo = card.get("Grupo") or "—"
    cota = card.get("Cota") or "—"
    proposta = _fmt_currency(card.get("Proposta Realizada", ""))
    tipo_contemplacao = _fmt_contemplacao(card.get("Tipo contemplação", ""))
    tipo_bem = (card.get("Tipo de bem") or "bem").capitalize()
    now = datetime.now(TZ_BRASILIA)
    data_str = f"São Paulo, {now.day:02d} de {_MESES_BR[now.month - 1]} de {now.year}"
    logo_url = "https://www.consorciosorteado.com.br/templates/yootheme/cache/25/logotipo-consorcio-sorteado-2556d1fd.png"
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: Arial, sans-serif;
  background-color: #f0f2f5;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 40px;
  color: #333;
}}
.document-container {{
  background-color: #fff;
  width: 800px;
  padding: 50px;
  box-shadow: 0 4px 15px rgba(0,0,0,0.1);
  position: relative;
  border-top: 10px solid #0097a7;
}}
.logo {{
  margin-bottom: 30px;
}}
.logo img {{
  height: 55px;
}}
.title {{
  font-size: 28px;
  font-weight: 800;
  text-transform: uppercase;
  line-height: 1.2;
  margin-bottom: 40px;
  width: 350px;
}}
.info-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 30px;
}}
.info-box {{
  background-color: #f8f9fa;
  padding: 15px 20px;
  border-radius: 8px;
}}
.label {{
  color: #00bcd4;
  font-size: 12px;
  font-weight: bold;
  text-transform: uppercase;
  margin-bottom: 8px;
}}
.value {{
  font-size: 16px;
  font-weight: 500;
}}
.proposal-value-box {{
  background-color: #f8f9fa;
  padding: 20px;
  border-radius: 8px;
  margin-bottom: 30px;
}}
.highlight {{
  background-color: #ffff00;
  font-weight: bold;
  font-size: 22px;
  padding: 2px 5px;
}}
.body-text {{
  background-color: #f2f7f2;
  padding: 30px;
  border-radius: 8px;
  line-height: 1.6;
  font-size: 14px;
  margin-bottom: 40px;
}}
.signature {{
  font-size: 14px;
  line-height: 1.8;
  margin-top: 16px;
}}
.footer {{
  text-align: center;
  font-size: 14px;
  font-weight: bold;
  border-top: 1px solid #eee;
  padding-top: 20px;
}}
.checkmark-bg {{
  position: absolute;
  top: 20px;
  right: 40px;
  font-size: 150px;
  color: #8bc34a;
  opacity: 0.8;
  font-weight: bold;
  transform: rotate(10deg);
  pointer-events: none;
  line-height: 1;
}}
</style>
</head>
<body>
  <div class="document-container">
    <div class="checkmark-bg">✓</div>

    <div class="logo">
      <img src="{logo_url}" alt="Consórcio Sorteado">
    </div>

    <div class="title">Análise Departamento<br>de Precificação</div>

    <div class="info-grid">
      <div class="info-box">
        <div class="label">Nome do Cliente:</div>
        <div class="value">{title}</div>
      </div>
      <div class="info-box">
        <div class="label">Administradora:</div>
        <div class="value">{adm}</div>
      </div>
      <div class="info-box">
        <div class="label">Grupo:</div>
        <div class="value">{grupo}</div>
      </div>
      <div class="info-box">
        <div class="label">Cota:</div>
        <div class="value">{cota}</div>
      </div>
      <div class="info-box">
        <div class="label">Tipo de Bem:</div>
        <div class="value">{tipo_bem}</div>
      </div>
      <div class="info-box">
        <div class="label">Forma de Contemplação:</div>
        <div class="value">{tipo_contemplacao}</div>
      </div>
    </div>

    <div class="proposal-value-box">
      <div class="label">Valor da Proposta:</div>
      <div class="value"><span class="highlight">{proposta}</span></div>
    </div>

    <div class="body-text">
      Prezado(a),<br><br>
      Nós analisamos o consórcio e verificamos que a cota <strong>{cota}</strong> do grupo <strong>{grupo}</strong>
      foi contemplada por <strong>{tipo_contemplacao}</strong>. Nossa proposta de compra do consórcio de
      <strong>{tipo_bem}</strong> é no valor de <strong>{proposta}</strong>. Todas as despesas relativas à
      transferência e às parcelas futuras do consórcio são de nossa responsabilidade. Aceitando a proposta,
      solicitamos os dados pessoais para formalizar o contrato eletrônico de compra e venda — após a assinatura,
      efetuamos o pagamento imediato.
      <div class="signature">
        Atenciosamente,<br>
        <strong>Equipe Consórcio Sorteado</strong>
      </div>
    </div>

    <div class="footer">{data_str}</div>
  </div>
</body>
</html>"""


async def _generate_proposal_image(card: dict) -> str | None:
    if not PUBLIC_URL:
        return None
    html = _build_proposal_html(card)
    filename = f"proposta_{card.get('id','x')[:8]}_{uuid.uuid4().hex[:6]}.png"
    path = await render_to_file(html, filename)
    if not path:
        return None
    return f"{PUBLIC_URL}/images/{filename}"


# ---------------------------------------------------------------------------
# Mensagem de texto
# ---------------------------------------------------------------------------

def _build_proposal_message(card: dict) -> str:
    nome = get_name(card)
    proposta = _fmt_currency(card.get("Proposta Realizada", ""))
    consultor = _get_consultor(card)
    return (
        f"Olá, {nome}! Tudo bem?\n\n"
        f"Meu nome é {consultor}, sou o consultor responsável pela negociação da sua cota contemplada.\n\n"
        f"💰 *NOSSA PROPOSTA*\n"
        f"Consegui estruturar uma oferta no valor de *{proposta}*.\n\n"
        f"📅 Você elimina as parcelas futuras e transforma em dinheiro imediato.\n"
        f"💳 Pagamento à vista, na sua conta, ANTES de qualquer transferência.\n\n"
        f"Se confirmar agora, já agilizo tudo para pagamento imediato.\n\nO que acha?"
    ).strip()


def _build_proposal_buttons(card: dict) -> tuple[str, list[dict]]:
    return _build_proposal_message(card), [
        {"id": "proposta_aceitar", "title": "✅ Quero vender!"},
        {"id": "proposta_duvida", "title": "💬 Tenho dúvidas"},
        {"id": "proposta_nao", "title": "❌ Não tenho interesse"},
    ]


# ---------------------------------------------------------------------------
# Envio unificado via Whapi
# ---------------------------------------------------------------------------

async def _send_proposal(phone: str, card: dict) -> bool:
    """Envia imagem (se disponível) + mensagem com botões via Whapi (canal correto)."""
    image_url = await _generate_proposal_image(card)
    if image_url:
        try:
            import base64
            from pathlib import Path
            img_path = Path(os.getenv("IMAGES_DIR", "/tmp/cs_images")) / Path(image_url).name
            b64 = base64.b64encode(img_path.read_bytes()).decode() if img_path.exists() else None
            data_uri = f"data:image/png;base64,{b64}" if b64 else image_url
            async with get_whapi_for_card(card) as w:
                await w.send_image(phone, data_uri)
            await asyncio.sleep(2)
        except WhapiError as e:
            logger.warning("Falha ao enviar imagem da proposta: %s", e)

    mensagem, botoes = _build_proposal_buttons(card)
    try:
        async with get_whapi_for_card(card) as w:
            await w.send_buttons(to=phone, message=mensagem, buttons=botoes)
        logger.info("Proposta Whapi enviada → %s", phone)
        return True
    except WhapiError as e:
        logger.warning("Botões falharam, tentando texto simples: %s", e)
        try:
            async with get_whapi_for_card(card) as w:
                await w.send_text(phone, _build_proposal_message(card))
            return True
        except WhapiError as e2:
            logger.error("Falha total Whapi para %s: %s", phone, e2)
            return False


# ---------------------------------------------------------------------------
# Processamento de um card
# ---------------------------------------------------------------------------


async def _process_card(faro: FaroClient, card: dict) -> bool:
    card_id = card.get("id", "")
    if card_id not in _processing:
        _processing[card_id] = asyncio.Lock()
    lock = _processing[card_id]
    if lock.locked():
        logger.info("Precificacao: card %s ja em processamento -- ignorado.", card_id[:8])
        return False
    async with lock:
        return await _process_card_locked(faro, card_id)


async def _process_card_locked(faro: FaroClient, card_id: str) -> bool:
    try:
        card = await faro.get_card(card_id)
    except FaroError as e:
        logger.error("Precificação: erro ao buscar card %s: %s", card_id[:8], e)
        return False

    current_stage = card.get("stage_id") or card.get("stageId") or ""
    if current_stage != Stage.PRECIFICACAO:
        return False

    nome = get_name(card)
    phone = get_phone(card)
    if not phone:
        return False

    proposta = card.get("Proposta Realizada", "")
    if not proposta:
        logger.warning("Precificação: card %s sem Proposta Realizada.", card_id[:8])
        return False

    agora = datetime.now(timezone.utc).isoformat()

    # Move para EM_NEGOCIACAO ANTES de enviar (stage-as-mutex)
    try:
        await faro.move_card(card_id, Stage.EM_NEGOCIACAO)
    except FaroError as e:
        logger.error("Precificação: erro ao reservar card %s: %s", card_id[:8], e)
        return False

    sucesso = await _send_proposal(phone, card)

    if sucesso:
        try:
            await faro.update_card(card_id, {"Ultima atividade": agora})
        except FaroError:
            pass
        try:
            history = load_history(card)
            history = history_append(history, "assistant", _build_proposal_message(card))
            await save_history(faro, card_id, history)
            import re as _re
            proposta_str = card.get("Proposta Realizada", "") or ""
            nums = _re.sub(r"[^\d,.]", "", proposta_str).replace(".", "").replace(",", ".")
            proposta_num = float(nums) if nums else 0.0
            journey = load_journey(card)
            journey["proposta_inicial"] = proposta_num
            await save_journey(faro, card_id, journey)
        except Exception as e:
            logger.warning("Precificação: erro ao salvar histórico/jornada %s: %s", card_id[:8], e)
        logger.info("Precificação: proposta enviada para card %s", card_id[:8])
    else:
        # Rollback — devolve para PRECIFICACAO
        try:
            await faro.move_card(card_id, Stage.PRECIFICACAO)
            logger.warning("Precificação: envio falhou, card %s devolvido a PRECIFICACAO", card_id[:8])
        except FaroError as rollback_err:
            logger.error("Precificação: CRÍTICO — rollback falhou card %s", card_id[:8])
            await slack_error(
                f"Card {card_id[:8]} preso em EM_NEGOCIACAO sem proposta",
                exception=rollback_err,
                context={"card_id": card_id, "nome": nome, "phone": phone},
            )

    return sucesso


# ---------------------------------------------------------------------------
# Job principal
# ---------------------------------------------------------------------------

def _is_within_send_window() -> bool:
    return SEND_WINDOW_START <= datetime.now(TZ_BRASILIA).hour < SEND_WINDOW_END


async def run_precificacao() -> None:
    if not _is_within_send_window():
        return
    logger.info("=== Iniciando Job Precificação ===")
    try:
        async with FaroClient() as faro:
            cards = await faro.watch_new(stage_id=Stage.PRECIFICACAO, minutes_ago=15, limit=JOB_BATCH_LIMIT)
            if not cards:
                return
            cards = filter_test_cards(cards)
            if not cards:
                return
            logger.info("Precificação: %d card(s) para processar", len(cards))
            total_ok = 0
            for card in cards:
                try:
                    ok = await _process_card(faro, card)
                    if ok:
                        total_ok += 1
                except Exception as e:
                    logger.exception("Precificação: erro inesperado card %s: %s", card.get("id", "?")[:8], e)
                await asyncio.sleep(3)
    except FaroError as e:
        logger.error("Precificação: erro ao buscar cards: %s", e)
        return
    logger.info("=== Precificação concluída: %d proposta(s) ===", total_ok)


async def send_proposal_now(card: dict) -> None:
    """Dispara proposta imediatamente para um card específico."""
    if not _is_within_send_window():
        return
    try:
        async with FaroClient() as faro:
            fresh = await faro.get_card(card.get("id", ""))
            await _process_card(faro, fresh)
    except Exception as e:
        logger.error("Precificação imediata: erro card %s: %s", card.get("id", "")[:8], e)


async def process_precificacao_card(card: dict) -> bool:
    """Ponto de entrada público para o webhook FARO — processa um card específico."""
    async with FaroClient() as faro:
        return await _process_card(faro, card)
