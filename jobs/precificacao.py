"""
jobs/precificacao.py — Envio de proposta de precificação ao lead

Acionado quando um card entra no stage PRECIFICACAO (geralmente após o agente
qualificar o lead e preencher os dados da cota no CRM).

Fluxo:
  1. watch_new() retorna cards que chegaram recentemente em PRECIFICACAO
  2. Verifica se a proposta já foi enviada (campo "Data proposta enviada")
  3. Gera imagem da proposta via HTML/CSS to Image (hcti.io)
  4. Envia a imagem via WhatsApp (Whapi para Listas, Z-API para demais)
  5. Envia mensagem de texto estruturada com botões
  6. Registra data/hora de envio e move o card para EM_NEGOCIACAO

Campos do card usados:
  - "Proposta Realizada"       → valor da oferta de compra (ex: "200,000.00")
  - "Adm"                      → nome da administradora
  - "Cota"                     → número da cota
  - "Tipo contemplação"        → forma de contemplação (sorteio/lance)
  - "Tipo de bem"              → tipo de bem (imóvel, veículo, etc.)
  - "Nome do contato" / title  → nome completo do lead
  - "Data proposta enviada"    → flag: se preenchida, proposta já foi enviada
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import uuid

from config import Stage, JOB_BATCH_LIMIT, SEND_WINDOW_START, SEND_WINDOW_END, PUBLIC_URL, TEST_MODE, TZ_BRASILIA, filter_test_cards
from services.html_image import render_to_file
from services.faro import (
    FaroClient, FaroError,
    get_phone, get_name, get_adm, is_lista,
    load_history, history_append, save_history,
    load_journey, save_journey,
)
from services.whapi import WhapiClient, WhapiError
from services.zapi import ZAPIClient, ZAPIError, get_zapi_for_card

logger = logging.getLogger(__name__)

# Lock in-process por card_id — impede execuções concorrentes do mesmo card
_processing: set[str] = set()


# ---------------------------------------------------------------------------
# Formatação da proposta
# ---------------------------------------------------------------------------

def _fmt_currency(value: str) -> str:
    """Garante que o valor está formatado como moeda brasileira. Ex: '300000' → 'R$ 300.000,00'"""
    if not value:
        return "a consultar"
    val = str(value).strip()
    # Se já tem formatação completa, retorna direto
    if "R$" in val and "," in val:
        return val
    if "R$" in val:
        # Tem R$ mas não vírgula — reformata o número
        val = val.replace("R$", "").strip()
    # Normaliza separadores: aceita 300.000,00 ou 300000 ou 300000.00 ou 300,000.00
    try:
        # Remove R$, espaços
        clean = val.replace("R$", "").strip()
        comma_pos = clean.find(",")
        period_pos = clean.find(".")
        if "," in clean and "." in clean:
            # Ambos presentes: determina qual é separador de milhar e qual é decimal
            if comma_pos < period_pos:
                # Formato US: 300,000.00 → vírgula é milhar, ponto é decimal
                clean = clean.replace(",", "")
            else:
                # Formato BR: 300.000,00 → ponto é milhar, vírgula é decimal
                clean = clean.replace(".", "").replace(",", ".")
        elif "," in clean:
            # Só vírgula: formato BR com decimal ex: 1.800,50 ou 300.000,00
            clean = clean.replace(".", "").replace(",", ".")
        else:
            # Sem vírgula: pode ser 300000 ou 300000.00
            clean = clean.replace(",", "")
        num = float(clean)
        # Formata manualmente no padrão BR: 300.000,00
        inteiro = int(num)
        centavos = round((num - inteiro) * 100)
        # Formata inteiro com separador de milhar
        inteiro_str = f"{inteiro:,}".replace(",", ".")
        return f"R$ {inteiro_str},{centavos:02d}"
    except (ValueError, TypeError):
        return val


def _fmt_prazo(value: str) -> str:
    """Formata prazo: '200' → '200 meses'"""
    if not value:
        return "a consultar"
    val = str(value).strip()
    if "mes" in val.lower():
        return val
    return f"{val} meses"


def _get_consultor(card: dict) -> str:
    """Retorna o nome do consultor responsável pelo card."""
    import os
    return (
        card.get("Responsáveis")
        or card.get("Responsável")
        or os.getenv("CONSULTOR_NOME", "Manuela")
    )


# ---------------------------------------------------------------------------
# Geração da imagem da proposta (HTML/CSS to Image)
# ---------------------------------------------------------------------------

_MESES_BR = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


def _fmt_contemplacao(value: str) -> str:
    """Formata o tipo de contemplação para exibição amigável."""
    v = (value or "").lower().replace("-", " ").replace("_", " ").strip()
    if "sorteio" in v:
        return "Sorteio"
    if "lance" in v:
        return "Lance"
    return value.title() if value else "Sorteio/Lance"


def _build_proposal_html(card: dict) -> str:
    """Monta o HTML da imagem 'ANÁLISE DEPARTAMENTO DE PRECIFICAÇÃO'."""
    title = card.get("title") or card.get("Nome do contato") or "Cliente"
    adm = get_adm(card)
    grupo = card.get("Grupo") or "—"
    cota  = card.get("Cota")  or "—"
    proposta = _fmt_currency(card.get("Proposta Realizada", ""))
    tipo_contemplacao = _fmt_contemplacao(card.get("Tipo contemplação", ""))
    tipo_bem = (card.get("Tipo de bem") or "bem").capitalize()

    now = datetime.now()
    data_str = f", {now.day:02d} de {_MESES_BR[now.month - 1]} de {now.year}"

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Análise Departamento de Precificação</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Arial', sans-serif; background: white; padding: 0; margin: 0; }}
        .container {{ max-width: 100%; margin: 0 auto; background: white; position: relative; overflow: hidden; }}
        .triangle-top-right {{ position: absolute; top: 0; right: 0; width: 0; height: 0; border-style: solid; border-width: 0 200px 100px 0; border-color: transparent #5fb3d9 transparent transparent; opacity: 0.8; }}
        .triangle-small {{ position: absolute; top: 20px; right: 120px; width: 0; height: 0; border-style: solid; border-width: 0 30px 30px 0; border-color: transparent #5fb3d9 transparent transparent; opacity: 0.6; }}
        .checkmark {{ position: absolute; top: 60px; right: 40px; width: 180px; height: 180px; background: linear-gradient(135deg, #7ed321 0%, #5fb848 100%); clip-path: polygon(0% 50%, 35% 85%, 100% 10%, 90% 0%, 35% 65%, 10% 40%); opacity: 0.9; }}
        .triangle-bottom-left {{ position: absolute; bottom: 0; left: 0; width: 0; height: 0; border-style: solid; border-width: 0 0 150px 150px; border-color: transparent transparent #5fb3d9 transparent; opacity: 0.3; }}
        .header {{ padding: 40px 50px 30px; position: relative; z-index: 1; }}
        .logo {{ margin-bottom: 40px; }}
        .logo img {{ height: 65px; width: auto; }}
        .content {{ padding: 0 50px 40px; position: relative; z-index: 1; }}
        h1 {{ font-size: 28px; font-weight: bold; color: #000; margin-bottom: 20px; line-height: 1.3; }}
        .underline {{ display: flex; gap: 10px; margin-bottom: 30px; }}
        .underline-green {{ width: 60px; height: 4px; background: #7ed321; }}
        .underline-black {{ width: 180px; height: 4px; background: #000; }}
        .info-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 30px; margin-bottom: 30px; }}
        .info-block {{ background: #f9f9f9; padding: 20px; border-radius: 8px; }}
        .info-label {{ color: #5fb3d9; font-size: 12px; font-weight: bold; margin-bottom: 8px; text-transform: uppercase; }}
        .info-value {{ color: #333; font-size: 15px; line-height: 1.6; }}
        .highlight {{ background: #ffeb3b; padding: 2px 4px; font-weight: bold; }}
        .text-content {{ background: #f0f8f0; padding: 25px; border-radius: 8px; margin-bottom: 30px; line-height: 1.8; color: #333; font-size: 14px; }}
        .signature {{ margin-top: 40px; margin-bottom: 20px; }}
        .signature p {{ margin: 5px 0; color: #333; }}
        .footer {{ text-align: center; padding: 30px; border-top: 2px solid #f0f0f0; margin-top: 40px; position: relative; z-index: 1; }}
        .footer-date {{ font-size: 14px; font-weight: bold; color: #000; letter-spacing: 1px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="triangle-top-right"></div>
        <div class="triangle-small"></div>
        <div class="checkmark"></div>
        <div class="triangle-bottom-left"></div>

        <div class="header">
            <div class="logo">
                <img src="https://www.consorciosorteado.com.br/templates/yootheme/cache/25/logotipo-consorcio-sorteado-2556d1fd.png" alt="Consórcio Sorteado">
            </div>
        </div>

        <div class="content">
            <h1>ANÁLISE DEPARTAMENTO<br>DE PRECIFICAÇÃO</h1>

            <div class="underline">
                <div class="underline-green"></div>
                <div class="underline-black"></div>
            </div>

            <div class="info-section">
                <div class="info-block">
                    <div class="info-label">Nome do Cliente:</div>
                    <div class="info-value">{title}</div>
                </div>
                <div class="info-block">
                    <div class="info-label">Administradora:</div>
                    <div class="info-value">{adm}</div>
                </div>
            </div>

            <div class="info-section">
                <div class="info-block">
                    <div class="info-label">Grupo:</div>
                    <div class="info-value">{grupo}</div>
                </div>
                <div class="info-block">
                    <div class="info-label">Cota:</div>
                    <div class="info-value">{cota}</div>
                </div>
            </div>

            <div class="info-section">
                <div class="info-block">
                    <div class="info-label">Tipo de Bem:</div>
                    <div class="info-value">{tipo_bem}</div>
                </div>
                <div class="info-block">
                    <div class="info-label">Forma de Contemplação:</div>
                    <div class="info-value">{tipo_contemplacao}</div>
                </div>
            </div>

            <div class="info-section" style="grid-template-columns: 1fr;">
                <div class="info-block">
                    <div class="info-label">Valor da Proposta:</div>
                    <div class="info-value" style="font-size:20px;font-weight:bold;"><span class="highlight">{proposta}</span></div>
                </div>
            </div>

            <div class="text-content">
                Prezado(a),<br><br>

                Nós analisamos o consórcio e verificamos que a cota <span class="highlight">{cota}</span>
                do grupo <span class="highlight">{grupo}</span> foi contemplada por
                <span class="highlight">{tipo_contemplacao}</span>.
                Nossa proposta de compra do consórcio de
                <span class="highlight">{tipo_bem}</span> é no valor de
                <span class="highlight">{proposta}</span>.
                Todas as despesas relativas à transferência e às parcelas futuras do
                consórcio são de nossa responsabilidade. Aceitando a proposta,
                solicitamos os dados pessoais para formalizar o contrato eletrônico de
                compra e venda — após a assinatura, efetuamos o pagamento de imediato.

                <div class="signature">
                    <p>Atenciosamente,</p>
                    <p><strong>Equipe Consórcio Sorteado</strong></p>
                </div>
            </div>
        </div>

        <div class="footer">
            <div class="footer-date">São Paulo{data_str}</div>
        </div>
    </div>
</body>
</html>"""


async def _generate_proposal_image(card: dict) -> str | None:
    """
    Gera a imagem da proposta com Playwright e retorna a URL pública.
    Retorna None se PUBLIC_URL não estiver configurado ou se o render falhar.
    """
    if not PUBLIC_URL:
        logger.debug("PUBLIC_URL não configurado — imagem da proposta desativada.")
        return None

    html = _build_proposal_html(card)
    card_id = card.get("id", "unknown")
    filename = f"proposta_{card_id[:8]}_{uuid.uuid4().hex[:6]}.png"

    path = await render_to_file(html, filename)
    if not path:
        return None

    url = f"{PUBLIC_URL}/images/{filename}"
    logger.info("Imagem da proposta disponível em: %s", url)
    return url


# ---------------------------------------------------------------------------
# Construção da mensagem de texto
# ---------------------------------------------------------------------------

def _build_proposal_message(card: dict) -> str:
    """
    Monta a mensagem de oferta de compra da cota.
    A Consórcio Sorteado COMPRA a cota contemplada do lead.
    """
    nome      = get_name(card)
    proposta  = _fmt_currency(card.get("Proposta Realizada", ""))
    consultor = _get_consultor(card)

    mensagem = (
        f"Olá, {nome}! Tudo bem?\n\n"
        f"Meu nome é {consultor}, sou o consultor responsável pela negociação da sua cota contemplada.\n\n"
        f"💰 *NOSSA PROPOSTA*\n"
        f"O mercado está aquecido e consegui estruturar uma oferta muito interessante "
        f"para você no valor de *{proposta}*.\n\n"
        f"Veja por que essa proposta pode fazer sentido para você:\n"
        f"📅 Você elimina as parcelas futuras e transforma um compromisso de longo prazo "
        f"em dinheiro imediato.\n"
        f"💳 O pagamento é feito à vista, com total segurança. A transferência da cota "
        f"só acontece após o valor estar na sua conta.\n\n"
        f"Essa é uma excelente oportunidade para antecipar recursos e ganhar mais liberdade financeira.\n\n"
        f"Se você me confirmar agora, já posso agilizar tudo para pagamento imediato.\n\n"
        f"O que acha?"
    )
    return mensagem.strip()


def _build_proposal_buttons(card: dict) -> tuple[str, list[dict]]:
    """
    Retorna (mensagem, lista_de_botões) para envio com botões interativos.
    """
    mensagem = _build_proposal_message(card)
    botoes = [
        {"id": "proposta_aceitar", "title": "✅ Quero vender!"},
        {"id": "proposta_duvida",  "title": "💬 Tenho dúvidas"},
        {"id": "proposta_nao",     "title": "❌ Não tenho interesse"},
    ]
    return mensagem, botoes


# ---------------------------------------------------------------------------
# Helpers de envio
# ---------------------------------------------------------------------------

async def _send_proposal_whapi(phone: str, card: dict) -> bool:
    """Envia imagem da proposta (se disponível) + mensagem com botões via Whapi."""
    # 1. Tenta gerar e enviar a imagem como base64 (evita problemas com URLs ngrok/redirect)
    image_url = await _generate_proposal_image(card)
    if image_url:
        try:
            import base64
            from pathlib import Path
            # Converte arquivo para data URI base64
            img_path = Path(os.getenv("IMAGES_DIR", "/tmp/cs_images")) / Path(image_url).name
            if img_path.exists():
                b64 = base64.b64encode(img_path.read_bytes()).decode()
                data_uri = f"data:image/png;base64,{b64}"
            else:
                data_uri = image_url  # fallback para URL
            async with WhapiClient() as w:
                await w.send_image(phone, data_uri)
            logger.info("Imagem da proposta enviada via Whapi → %s", phone)
            await asyncio.sleep(2)
        except WhapiError as e:
            logger.warning("Falha ao enviar imagem da proposta via Whapi: %s", e)

    # 2. Envia mensagem de texto com botões
    mensagem, botoes = _build_proposal_buttons(card)
    try:
        async with WhapiClient() as w:
            await w.send_buttons(
                to=phone,
                message=mensagem,
                buttons=botoes,
            )
        logger.info("Proposta Whapi enviada → %s", phone)
        return True
    except WhapiError as e:
        # Fallback: tenta texto simples se botões falharem
        logger.warning("Botões Whapi falharam, tentando texto simples: %s", e)
        try:
            async with WhapiClient() as w:
                await w.send_text(phone, _build_proposal_message(card))
            return True
        except WhapiError as e2:
            logger.error("Falha total Whapi para %s: %s", phone, e2)
            return False


async def _send_proposal_zapi(phone: str, card: dict) -> bool:
    """Envia imagem da proposta (se disponível) + mensagem com botões via Z-API."""
    # 1. Tenta gerar e enviar a imagem
    image_url = await _generate_proposal_image(card)
    if image_url:
        try:
            zapi = get_zapi_for_card(card)
            async with zapi as z:
                await z.send_image(phone, image_url)
            logger.info("Imagem da proposta enviada via Z-API → %s", phone)
            await asyncio.sleep(2)
        except ZAPIError as e:
            logger.warning("Falha ao enviar imagem da proposta via Z-API: %s", e)

    # 2. Envia mensagem de texto com botões
    mensagem, botoes = _build_proposal_buttons(card)
    zapi = get_zapi_for_card(card)
    try:
        async with zapi as z:
            await z.send_button_list(
                to=phone,
                message=mensagem,
                buttons=botoes,
                title="Proposta de Consórcio",
                footer="Guará Lab • Consórcio Sorteado",
            )
        logger.info("Proposta Z-API enviada → %s", phone)
        return True
    except ZAPIError as e:
        # Fallback: tenta texto simples (nova instância para evitar estado inconsistente)
        logger.warning("Botões Z-API falharam, tentando texto simples: %s", e)
        try:
            zapi2 = get_zapi_for_card(card)
            async with zapi2 as z:
                await z.send_text(phone, _build_proposal_message(card))
            return True
        except ZAPIError as e2:
            logger.error("Falha total Z-API para %s: %s", phone, e2)
            return False


# ---------------------------------------------------------------------------
# Processamento de um card
# ---------------------------------------------------------------------------

async def _process_card(faro: FaroClient, card: dict) -> bool:
    """
    Processa um card do stage PRECIFICACAO:
    - Valida dados mínimos
    - Envia imagem + proposta
    - Atualiza campos de controle
    Retorna True se proposta foi enviada com sucesso.
    """
    card_id = card.get("id", "")

    # Lock in-process: impede dois loops/tasks processando o mesmo card simultaneamente
    if card_id in _processing:
        logger.info("Precificação: card %s já em processamento, pulando.", card_id[:8])
        return False
    _processing.add(card_id)

    try:
        return await _process_card_locked(faro, card_id)
    finally:
        _processing.discard(card_id)


async def _process_card_locked(faro: FaroClient, card_id: str) -> bool:
    """Processamento real após adquirir o lock in-process."""
    # Busca card FRESCO — garante que a flag mais recente é visível
    try:
        card = await faro.get_card(card_id)
    except FaroError as e:
        logger.error("Precificação: erro ao buscar card %s: %s", card_id[:8], e)
        return False

    nome  = get_name(card)
    phone = get_phone(card)

    # Guarda principal: card deve estar atualmente em PRECIFICACAO
    # (watch_new retorna cards que entraram no stage recentemente, mesmo que já tenham saído)
    current_stage = card.get("stage_id") or card.get("stageId") or ""
    if current_stage != Stage.PRECIFICACAO:
        logger.info(
            "Precificação: card %s não está mais em PRECIFICACAO (stage atual: %s...), pulando.",
            card_id[:8], current_stage[:8],
        )
        return False

    # Valida campos obrigatórios
    if not phone:
        logger.warning("Precificação: card %s sem telefone, pulando.", card_id[:8])
        return False

    proposta = card.get("Proposta Realizada", "")
    if not proposta:
        logger.warning(
            "Precificação: card %s (%s) sem 'Proposta Realizada' preenchida. "
            "Aguardando o agente definir o valor de oferta.",
            card_id[:8], nome
        )
        return False

    logger.info("Precificação: enviando proposta para %s (%s) — adm=%s", nome, phone, get_adm(card))

    agora = datetime.now(timezone.utc).isoformat()

    # Move para EM_NEGOCIACAO ANTES de enviar — impede que o job re-processe o card
    # enquanto o envio está em andamento (a checagem de stage usa dados frescos do FARO)
    try:
        await faro.move_card(card_id, Stage.EM_NEGOCIACAO)
        logger.info("Precificação: card %s reservado → EM_NEGOCIACAO", card_id[:8])
    except FaroError as e:
        logger.error("Precificação: erro ao reservar card %s: %s", card_id[:8], e)
        return False

    # Envia pelo provider correto
    if is_lista(card):
        sucesso = await _send_proposal_whapi(phone, card)
    else:
        sucesso = await _send_proposal_zapi(phone, card)

    if sucesso:
        try:
            await faro.update_card(card_id, {"Ultima atividade": agora})
        except FaroError as e:
            logger.warning("Precificação: erro ao atualizar atividade do card %s: %s", card_id[:8], e)

        # Registra proposta no histórico + contexto de jornada
        try:
            history = load_history(card)
            history = history_append(history, "assistant", _build_proposal_message(card))
            await save_history(faro, card_id, history)

            # Extrai valor numérico da proposta para a jornada
            proposta_str = card.get("Proposta Realizada", "") or ""
            try:
                import re as _re
                nums = _re.sub(r"[^\d,.]", "", proposta_str)
                nums = nums.replace(".", "").replace(",", ".")
                proposta_num = float(nums) if nums else 0.0
            except (ValueError, TypeError):
                proposta_num = 0.0

            journey = load_journey(card)
            journey["proposta_inicial"] = proposta_num
            await save_journey(faro, card_id, journey)
        except Exception as e:
            logger.warning("Precificação: erro ao salvar histórico/jornada card %s: %s", card_id[:8], e)

        logger.info("Precificação: proposta enviada com sucesso para card %s", card_id[:8])
    else:
        # Envio falhou — devolve para PRECIFICACAO para nova tentativa
        try:
            await faro.move_card(card_id, Stage.PRECIFICACAO)
            logger.warning("Precificação: envio falhou, card %s devolvido a PRECIFICACAO", card_id[:8])
        except FaroError as rollback_err:
            logger.error(
                "Precificação: CRÍTICO — envio falhou E rollback falhou para card %s. "
                "Card preso em EM_NEGOCIACAO sem proposta enviada. Erro: %s",
                card_id[:8], rollback_err,
            )
            from services.slack import slack_error
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
    """
    Job de precificação.
    Verifica cards novos em PRECIFICACAO e envia propostas formais.
    Roda a cada 5 minutos via APScheduler.
    """
    if not _is_within_send_window():
        logger.info("Precificação: fora da janela de envio, pulando.")
        return

    logger.info("=== Iniciando Job Precificação ===")

    try:
        async with FaroClient() as faro:
            # watch_new com janela de 15 min para pegar cards recentes
            cards = await faro.watch_new(
                stage_id=Stage.PRECIFICACAO,
                minutes_ago=15,
                limit=JOB_BATCH_LIMIT,
            )

            if not cards:
                logger.info("Precificação: nenhum card novo.")
                return

            cards = filter_test_cards(cards)
            if TEST_MODE:
                logger.info("TEST_MODE ativo: %d card(s) após filtro de teste.", len(cards))
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
                    logger.exception(
                        "Precificação: erro inesperado no card %s: %s",
                        card.get("id", "?")[:8], e
                    )
                # Pequeno delay entre propostas (anti-spam)
                await asyncio.sleep(3)

    except FaroError as e:
        logger.error("Precificação: erro ao buscar cards do FARO: %s", e)
        return

    logger.info("=== Precificação concluída: %d proposta(s) enviada(s) ===", total_ok)


async def send_proposal_now(card: dict) -> None:
    """
    Dispara a proposta imediatamente para um card específico.
    Chamado pelo router/qualificador assim que o card entra em PRECIFICACAO,
    sem esperar o próximo ciclo do job.
    """
    if not _is_within_send_window():
        logger.info("Precificação imediata: fora da janela de envio, aguardando próximo ciclo.")
        return
    try:
        async with FaroClient() as faro:
            # Busca card atualizado antes de processar
            fresh = await faro.get_card(card.get("id", ""))
            await _process_card(faro, fresh)
    except Exception as e:
        logger.error("Precificação imediata: erro para card %s: %s", card.get("id","")[:8], e)
