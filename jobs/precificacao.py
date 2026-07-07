"""
jobs/precificacao.py — Envio de proposta de precificação ao lead
Provider: Whapi (get_whapi_for_card — substitui Z-API para Bazar/Site)
"""

import asyncio
import logging
import math
import os
import uuid
from datetime import datetime, timezone

from config import (
    Stage, JOB_BATCH_LIMIT, SEND_WINDOW_START, SEND_WINDOW_END,
    PUBLIC_URL, TEST_MODE, TZ_BRASILIA, filter_test_cards, NOTIFY_PHONES,
)
from services.html_image import render_to_file
from services.faro import (
    FaroClient, FaroError,
    get_phone, get_name, get_adm, is_lista,
    load_history, history_append, save_history,
    load_journey, save_journey,
)
from services.whapi import WhapiClient, WhapiError, get_whapi_for_card, notify_team
from services.slack import slack_error

logger = logging.getLogger(__name__)

_processing: dict[str, asyncio.Lock] = {}

# ---------------------------------------------------------------------------
# Cálculo automático de proposta para leads de Listas
# Replica a lógica do blueprint Make.com (fluxograma completo)
# ---------------------------------------------------------------------------

# Cluster A: índices 0-3 para escalada de negociação (teto máximo: 30%)
_CLUSTER_A = [0.20, 0.23, 0.27, 0.30]

# Cluster B: Ademicon/Embracon com meses a pagar entre 80-110
_CLUSTER_B = [0.17, 0.20, 0.23, 0.27, 0.30]

# Cluster C: Caixa — teto máximo 27% (não chegamos a 30%)
_CLUSTER_C = [0.20, 0.23, 0.27]

# Admins aceitas
_ADMS_ACEITAS = {
    "porto seguro", "itaú", "itau", "bradesco", "santander",
    "sicoob", "mycon", "caixa", "embracon", "ademicon",
}

# Adms com cluster especial (80-110 meses)
_ADMS_CLUSTER_B = {"ademicon", "embracon"}

# Adms com teto reduzido (máximo 27%)
_ADMS_CLUSTER_C = {"caixa"}

def _arredondar_milhar(valor: float) -> float:
    return math.floor(valor / 1000) * 1000


# ---------------------------------------------------------------------------
# Camada 1 — Validação de teto antes do envio
# ---------------------------------------------------------------------------

def _validar_proposta_contra_teto(
    proposta: float,
    credito: float,
    adm: str,
    meses_a_pagar: int = 999,
) -> tuple[bool, float, str]:
    """
    Valida se a proposta respeita o teto máximo do cluster da ADM.

    Retorna (válida, teto_valor, motivo_bloqueio).
    - válida=True  → proposta dentro do permitido
    - válida=False → proposta excede o teto; motivo_bloqueio descreve o problema
    """
    if credito <= 0 or proposta <= 0:
        return True, 0.0, ""

    cluster = _get_cluster(adm, meses_a_pagar)
    teto_pct = cluster[-1]                          # último item = máximo do cluster
    teto_val = _arredondar_milhar(teto_pct * credito)
    pct_proposta = proposta / credito

    if proposta > teto_val:
        motivo = (
            f"Proposta R$ {proposta:,.0f} ({pct_proposta:.1%}) excede o teto do cluster "
            f"para ADM '{adm}': {teto_pct:.0%} = R$ {teto_val:,.0f}"
        )
        return False, teto_val, motivo

    return True, teto_val, ""

def _get_cluster(adm: str, meses_a_pagar: int) -> list:
    """
    Retorna o cluster correto baseado na adm e meses a pagar.

    Cluster A (padrão): 20%, 23%, 27%, 30% — todas as ADMs
    Cluster B (Ademicon/Embracon, 80-110 meses): 17%, 20%, 23%, 27%, 30%
    Cluster C (Caixa): 20%, 23%, 27% — teto máximo 27%
    """
    adm_lower = adm.lower().strip()

    # Cluster B: Ademicon/Embracon com meses específicos
    if any(a in adm_lower for a in _ADMS_CLUSTER_B) and 80 <= meses_a_pagar <= 110:
        return _CLUSTER_B

    # Cluster C: Caixa — teto 27%
    if any(a in adm_lower for a in _ADMS_CLUSTER_C):
        return _CLUSTER_C

    return _CLUSTER_A

def _get_indice_por_percentual(percentual_pago: float) -> int | None:
    """
    Retorna o índice inicial do cluster baseado no % pago.
    Retorna None se o lead não se qualifica (% pago > 30%).

    Classe A: ≤ 5%  → índice 0 (20% ou 17%)
    Classe B: ≤ 15% → índice 1 (23% ou 20%)
    Classe C: ≤ 30% → índice 2 (27% ou 23%)
    > 30%           → None (desqualificado)
    """
    if percentual_pago <= 0.05:
        return 0
    if percentual_pago <= 0.15:
        return 1
    if percentual_pago <= 0.30:
        return 2
    return None

def calcular_proposta_listas(
    credito: float,
    valor_pago: float,
    percentual_pago: float,
    adm: str = "",
    meses_a_pagar: int = 999,
    indice_override: int | None = None,
) -> tuple[float, int, list]:
    """
    Calcula proposta para todos os fluxos (Listas, Bazar, LP).
    Baseado exclusivamente no crédito × percentual do cluster.

    Classe A: % pago ≤ 5%  → índice 0
    Classe B: % pago ≤ 15% → índice 1
    Classe C: % pago ≤ 30% → índice 2
    > 30%                  → não compramos (retorna 0.0)

    Cluster por ADM:
      Cluster A (padrão):            20%, 23%, 27%, 30%
      Cluster B (Ademicon/Embracon): 17%, 20%, 23%, 27%, 30%
      Cluster C (Caixa):             20%, 23%, 27%  ← teto máximo 27%

    Retorna (proposta, indice_usado, cluster) para permitir escalada posterior.
    """
    if credito <= 0:
        return 0.0, 0, _CLUSTER_A

    cluster = _get_cluster(adm, meses_a_pagar)

    if indice_override is not None:
        indice = max(0, min(indice_override, len(cluster) - 1))
    else:
        indice = _get_indice_por_percentual(percentual_pago)
        if indice is None:
            return 0.0, 0, cluster  # % pago > 30% → não compramos

    proposta = _arredondar_milhar(cluster[indice] * credito)
    return proposta, indice, cluster

def _parse_float(value) -> float:
    """Converte string de valor monetário brasileiro/americano para float."""
    if not value:
        return 0.0
    try:
        s = str(value).strip().replace("R$", "").replace(" ", "")
        # Remove caracteres não numéricos exceto vírgula e ponto
        # Formato BR: 144.984,10 → 144984.10
        # Formato US/FARO: 144,984.10 → 144984.10
        if "," in s and "." in s:
            # Descobre qual é separador decimal pelo último separador
            last_comma = s.rfind(",")
            last_dot   = s.rfind(".")
            if last_dot > last_comma:
                # US: 144,984.10 — vírgula é milhar, ponto é decimal
                s = s.replace(",", "")
            else:
                # BR: 144.984,10 — ponto é milhar, vírgula é decimal
                s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        return float(s)
    except (ValueError, AttributeError):
        return 0.0

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
      solicitamos os dados pessoais para formalizar o contrato eletrônico de compra e venda —
      <strong>o pagamento é efetuado à vista, diretamente na sua conta, ANTES de qualquer transferência da cota.</strong>
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

def _build_guarantee_message() -> str:
    return (
        "💡 *Por que fechar com a Consórcio Sorteado?*\n\n"
        "✅ Você recebe o pagamento *à vista, direto na sua conta*, logo após assinar o contrato.\n\n"
        "📋 A partir daí, as parcelas seguintes ficam por nossa conta — assim como todas as despesas de transferência.\n\n"
        "🔒 E o mais importante: *a cota só sai do seu nome depois que o dinheiro já está na sua conta.* "
        "Sem risco, sem surpresa.\n\n"
        "Ficou alguma dúvida? É só perguntar! 😊"
    )


async def _send_proposal(phone: str, card: dict) -> bool:
    """Envia imagem da proposta + mensagem de texto + mensagem de garantia via Whapi."""
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

    mensagem = _build_proposal_message(card)
    try:
        async with get_whapi_for_card(card) as w:
            await w.send_text(phone, mensagem)
            await asyncio.sleep(3)
            await w.send_text(phone, _build_guarantee_message())
        logger.info("Proposta + garantia enviadas → %s", phone)
        return True
    except WhapiError as e:
        logger.error("Falha ao enviar proposta para %s: %s", phone, e)
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
    from services.session_store import acquire_mutex, release_mutex
    mutex_key = f"job:precificacao:{card_id}"
    if not await acquire_mutex(mutex_key, ttl=120):
        logger.info("Precificacao: card %s ja em processamento - pulando", card_id[:8])
        return False
    try:
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
        # Ignora valores zerados ou inválidos (ex: '0.00', '0', 'R$ 0')
        proposta_num = _parse_float(proposta)
        if proposta_num <= 0:
            proposta = ""
    
        if not proposta and is_lista(card):
            # Calcula automaticamente pelo fluxograma completo
            credito          = _parse_float(card.get("Crédito") or card.get("Valor do crédito", "0"))
            valor_pago       = _parse_float(card.get("Valor pago até o momento", "0"))
            # Validação de formato: o campo pode conter percentual (6.1) ou fração (0.061)
            # Convenção: qualificador grava como percentual (0-100). Se valor > 1.0, assumir percentual.
            _pct_raw = _parse_float(card.get("Porcentagem paga até o momento", "0"))
            if _pct_raw > 1.0:
                percentual_pago = _pct_raw / 100   # estava em formato percentual (ex: 6.1 → 0.061)
            else:
                percentual_pago = _pct_raw         # já estava em formato fração (ex: 0.061)
            # Fallback: recalcular a partir dos valores brutos se campo vazio ou zero
            if percentual_pago == 0.0 and credito > 0 and valor_pago > 0:
                percentual_pago = valor_pago / credito
                logger.info("Precificação Listas: card %s — recalculando %%pago=%.1f%% a partir dos valores brutos",
                            card.get("id","")[:8], percentual_pago * 100)
            adm              = get_adm(card)
            meses_a_pagar    = int(_parse_float(card.get("Quantidade meses a pagar") or card.get("Quantidade meses restantes", "999")) or 999)
            if credito > 0:
                proposta_calculada, indice_usado, cluster = calcular_proposta_listas(
                    credito, valor_pago, percentual_pago,
                    adm=adm, meses_a_pagar=meses_a_pagar,
                )
                if proposta_calculada <= 0:
                    logger.warning("Precificação Listas: card %s não se qualifica (% pago > 30%% ou meses < 80)", card_id[:8])
                    return False
                proposta = str(int(proposta_calculada))
                sequencia = ",".join(str(int(_arredondar_milhar(p * credito))) for p in cluster[indice_usado:])
                logger.info(
                    "Precificação Listas: card %s | adm=%s | %%pago=%.1f%% | indice=%d | proposta=%s | seq=%s",
                    card_id[:8], adm, percentual_pago*100, indice_usado, proposta, sequencia,
                )
                try:
                    await faro.update_card(card_id, {
                        "Proposta Realizada": proposta,
                        "Classes de Proposta": sequencia,
                        "Indice da Proposta": str(indice_usado),
                    })
                    card["Proposta Realizada"] = proposta
                    card["Classes de Proposta"] = sequencia
                except FaroError as e:
                    logger.warning("Precificação: erro ao gravar Proposta Realizada: %s", e)
            else:
                logger.warning("Precificação Listas: card %s sem crédito para calcular proposta", card_id[:8])
    
        if not proposta and not is_lista(card):
            # Bazar/LP: calcula pela mesma lógica usando dados do extrato (já gravados no FARO)
            credito       = _parse_float(card.get("Crédito") or "0")
            valor_pago    = _parse_float(card.get("Valor pago até o momento") or "0")
            meses_a_pagar = int(_parse_float(card.get("Quantidade meses a pagar") or "999") or 999)
            adm           = get_adm(card)
            percentual_pago = (valor_pago / credito) if credito > 0 else 0.0
            if credito > 0:
                proposta_calculada, indice_usado, cluster = calcular_proposta_listas(
                    credito, valor_pago, percentual_pago,
                    adm=adm, meses_a_pagar=meses_a_pagar,
                )
                if proposta_calculada <= 0:
                    logger.warning(
                        "Precificação Bazar/LP: card %s não se qualifica (%%pago=%.1f%%)",
                        card_id[:8], percentual_pago * 100,
                    )
                    return False
                proposta = str(int(proposta_calculada))
                sequencia = ",".join(str(int(_arredondar_milhar(p * credito))) for p in cluster[indice_usado:])
                logger.info(
                    "Precificação Bazar/LP: card %s | adm=%s | %%pago=%.1f%% | indice=%d | proposta=%s",
                    card_id[:8], adm, percentual_pago * 100, indice_usado, proposta,
                )
                try:
                    await faro.update_card(card_id, {
                        "Proposta Realizada": proposta,
                        "Classes de Proposta": sequencia,
                        "Indice da Proposta": str(indice_usado),
                    })
                    card["Proposta Realizada"] = proposta
                    card["Classes de Proposta"] = sequencia
                except FaroError as e:
                    logger.warning("Precificação: erro ao gravar proposta Bazar/LP: %s", e)
            else:
                logger.warning("Precificação Bazar/LP: card %s sem crédito para calcular proposta", card_id[:8])
    
        # Bazar/LP: proposta já existe mas Classes de Proposta está vazio → recalcular sequência
        elif proposta and not is_lista(card) and not (card.get("Classes de Proposta") or "").strip():
            credito       = _parse_float(card.get("Crédito") or "0")
            valor_pago    = _parse_float(card.get("Valor pago até o momento") or "0")
            meses_a_pagar = int(_parse_float(card.get("Quantidade meses a pagar") or "999") or 999)
            adm           = get_adm(card)
            percentual_pago = (valor_pago / credito) if credito > 0 else 0.0
            if credito > 0 and valor_pago > 0:
                _, indice_usado, cluster = calcular_proposta_listas(
                    credito, valor_pago, percentual_pago, adm=adm, meses_a_pagar=meses_a_pagar,
                )
                sequencia = ",".join(str(int(_arredondar_milhar(p * credito))) for p in cluster[indice_usado:])
                logger.info(
                    "Precificação Bazar/LP: card %s recalculando sequência (proposta=%s já existe) → seq=%s",
                    card_id[:8], proposta, sequencia,
                )
                try:
                    await faro.update_card(card_id, {
                        "Classes de Proposta": sequencia,
                        "Indice da Proposta": str(indice_usado),
                    })
                    card["Classes de Proposta"] = sequencia
                except FaroError as e:
                    logger.warning("Precificação: erro ao gravar sequência Bazar/LP: %s", e)
    
        if not proposta:
            logger.warning("Precificação: card %s sem Proposta Realizada.", card_id[:8])
            return False

        # ── Camada 1: BLOQUEIO DE TETO — valida proposta contra o cluster da ADM ──
        # Impede o envio de qualquer proposta (manual ou calculada) que ultrapasse
        # o percentual máximo permitido para a administradora.
        _credito_v   = _parse_float(card.get("Crédito") or "0")
        _meses_v     = int(_parse_float(card.get("Quantidade meses a pagar") or "999") or 999)
        _proposta_v  = _parse_float(proposta)
        _adm_v       = get_adm(card)
        _valida, _teto_v, _motivo_bloqueio = _validar_proposta_contra_teto(
            _proposta_v, _credito_v, _adm_v, _meses_v
        )
        if not _valida:
            logger.error(
                "Precificação: BLOQUEIO DE TETO — card %s | %s",
                card_id[:8], _motivo_bloqueio,
            )
            await slack_error(
                f"🚨 *BLOQUEIO DE TETO* — Proposta acima do cluster!\n\n"
                f"*Lead:* {nome} | *Card:* `{card_id[:8]}`\n"
                f"*ADM:* {_adm_v} | *Crédito:* R$ {_credito_v:,.0f}\n"
                f"*Proposta bloqueada:* R$ {_proposta_v:,.0f} "
                f"({_proposta_v / _credito_v:.1%} do crédito)\n"
                f"*Teto permitido:* R$ {_teto_v:,.0f} "
                f"({_teto_v / _credito_v:.1%} do crédito)\n\n"
                f"⚠️ Corrija o campo *Proposta Realizada* no FARO e reprocesse o card.",
                context={"card_id": card_id, "nome": nome, "adm": _adm_v},
            )
            return False   # não envia, mantém card em PRECIFICACAO para correção manual

        # ── Camada 2: ALERTA de proposta manual próxima do teto ──────────────────
        # Quando a proposta veio preenchida do FARO (não calculada agora), alerta se
        # estiver acima de 90% do teto do cluster — sinal de atenção para revisão.
        _proposta_original_faro = _parse_float(card.get("Proposta Realizada", "") or "")
        _foi_manual = (
            _proposta_original_faro > 0
            and _proposta_original_faro == _proposta_v   # não foi recalculada nesta execução
        )
        if _foi_manual and _credito_v > 0 and _teto_v > 0:
            _cluster_v   = _get_cluster(_adm_v, _meses_v)
            _teto_pct_v  = _cluster_v[-1]
            _pct_manual  = _proposta_v / _credito_v
            if _pct_manual >= _teto_pct_v * 0.90:
                logger.warning(
                    "Precificação: proposta manual próxima do teto — card %s | "
                    "%.1f%% do crédito (teto=%s%%)",
                    card_id[:8], _pct_manual * 100, f"{_teto_pct_v:.0%}",
                )
                await slack_error(
                    f"⚠️ *PROPOSTA MANUAL PRÓXIMA DO TETO*\n\n"
                    f"*Lead:* {nome} | *Card:* `{card_id[:8]}`\n"
                    f"*ADM:* {_adm_v} | *Crédito:* R$ {_credito_v:,.0f}\n"
                    f"*Proposta manual:* R$ {_proposta_v:,.0f} "
                    f"({_pct_manual:.1%} do crédito)\n"
                    f"*Teto do cluster:* {_teto_pct_v:.0%} = R$ {_teto_v:,.0f}\n\n"
                    f"ℹ️ Proposta dentro do limite, mas revisão recomendada.",
                    context={"card_id": card_id, "nome": nome, "adm": _adm_v},
                )

        # ── Envio da proposta ────────────────────────────────────────────────────
        # SEGURANÇA: Só compramos cotas contempladas por SORTEIO.
        # Bloqueio de lance abaixo — segunda camada de defesa (camada 1 está no qualificador).
        fonte_bazar_lp = not is_lista(card)
        tipo_cont = (card.get("Tipo contemplação") or "").strip().lower()
        _e_lance = tipo_cont in ("lance", "contemplada-lance")
    
        # Segunda camada de defesa: bloqueia proposta para cota de lance antes de enviar
        if _e_lance:
            logger.error(
                "Precificação: BLOQUEIO DE SEGURANÇA — card %s tem Tipo contemplação='%s'. "
                "Proposta NÃO enviada. Movendo para Não Qualificado.",
                card_id[:8], tipo_cont,
            )
            try:
                await faro.move_card(card_id, Stage.NAO_QUALIFICADO)
                await faro.update_card(card_id, {
                    "Motivo dispensa": f"Bloqueio precificação: cota contemplada por lance (tipo='{tipo_cont}')",
                })
            except FaroError as e:
                logger.error("Precificação: erro no rollback de lance para card %s: %s", card_id[:8], e)
            await slack_error(
                f"🚨 BLOQUEIO SEGURANÇA — Proposta de lance interceptada na precificação\n"
                f"Lead: {nome} | Card: `{card_id[:8]}` | Tipo: `{tipo_cont}`\n"
                f"Movido para Não Qualificado automaticamente.",
                context={"card_id": card_id, "nome": nome, "tipo_contemplacao": tipo_cont},
            )
            return False

        # Safety Car de precificação removido — disparo direto para todos os fluxos
        logger.info("Precificação: disparo direto para card %s (sem aprovação manual)", card_id[:8])

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
                adm   = get_adm(card)
                cred  = card.get("Crédito", "?")
                pago  = card.get("Porcentagem paga até o momento", "?")
                await faro.append_description(
                    card_id,
                    f"🟢 Proposta enviada — R$ {proposta} | {adm} | Crédito: R$ {cred} | {pago}% pago"
                )
            except Exception as e:
                logger.warning("Precificação: erro ao gravar marco descrição %s: %s", card_id[:8], e)
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

            # ── Mensagem SDR ao lead: informa que consultor entrará em contato ──
            try:
                _sdr_msg = (
                    "Um de nossos consultores entrará em contato em breve para "
                    "esclarecer qualquer dúvida e acompanhar a negociação. 😊"
                )
                async with get_whapi_for_card(card) as _w:
                    await asyncio.sleep(2)
                    await _w.send_text(phone, _sdr_msg)
                logger.info("Precificação: mensagem SDR enviada para card %s", card_id[:8])
            except WhapiError as e:
                logger.warning("Precificação: falha ao enviar mensagem SDR para %s: %s", card_id[:8], e)

            # ── Notificação no grupo Alarmes Sistemas CS ──────────────────────
            try:
                from services.whapi import notify_team as _notify_team
                _adm_notif   = get_adm(card)
                _nome_notif  = nome
                _phone_notif = phone
                _prop_notif  = _fmt_currency(card.get("Proposta Realizada", ""))
                _cred_notif  = _fmt_currency(str(card.get("Crédito", "")))
                _canal_notif = "Listas" if is_lista(card) else "Bazar/LP"
                _notif_msg = (
                    f"🎯 *Nova proposta enviada!*\n\n"
                    f"👤 *Nome:* {_nome_notif}\n"
                    f"📱 *Telefone:* {_phone_notif}\n"
                    f"🏦 *Administradora:* {_adm_notif}\n"
                    f"💰 *Proposta:* {_prop_notif}\n"
                    f"📊 *Crédito:* {_cred_notif}\n"
                    f"🔀 *Canal:* {_canal_notif}\n"
                    f"🆔 *Card:* `{card_id[:8]}`"
                )
                await _notify_team(_notif_msg)
                logger.info("Precificação: grupo notificado para card %s", card_id[:8])
            except Exception as e:
                logger.warning("Precificação: falha ao notificar grupo para %s: %s", card_id[:8], e)

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
    finally:
        await release_mutex(mutex_key)



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
            cards = await faro.watch_new(stage_id=Stage.PRECIFICACAO, minutes_ago=480, limit=JOB_BATCH_LIMIT)
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
    """
    Dispara proposta imediatamente para um card específico.
    Chamado de forma reativa (lead demonstrou interesse) — sem guard de janela,
    pois o lead acabou de responder e espera a proposta agora.
    """
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


async def run_precificacao_safe():
    """Wrapper resiliente — garante que exceções não derrubam o scheduler."""
    try:
        await run_precificacao()
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("run_precificacao: erro inesperado: %s", e)
        try:
            from services.slack import slack_error
            await slack_error("Job precificacao falhou inesperadamente", exception=e)
        except Exception:
            pass
