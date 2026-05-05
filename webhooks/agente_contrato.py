"""
webhooks/agente_contrato.py — Agente de coleta de dados para contrato

Fluxo para leads de lista em stage ASSINATURA (sem ZapSign Token ainda):

  Etapa 1 — Dados pessoais (guiada por IA)
    - IA extrai CPF, RG, Endereço, Email de qualquer texto enviado pelo lead
    - Confirma o que foi recebido e pede especificamente o que falta
    - Só avança quando todos os 4 campos estiverem coletados

  Etapa 2 — Extrato detalhado
    - Com dados completos, bot pede o extrato da cota
    - Quando extrato chega (mídia) → gera contrato ZapSign
"""

import json
import logging
import re
from typing import Optional

from services.ai import AIClient, AIError
from services.faro import (
    FaroClient, FaroError,
    get_name, get_phone, get_adm, is_lista, is_pj,
    load_history, history_append, save_history, history_to_text,
    load_journey, journey_to_text,
)
from services.whapi import WhapiClient, WhapiError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Campos obrigatórios e labels
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS_LISTA = ["NomeCompleto", "CPF", "RG", "Endereco", "CEP", "Email", "EstadoCivil", "Ocupacao", "Nacionalidade", "DadosPagamento"]
_REQUIRED_FIELDS_BAZAR = ["NomeCompleto", "CPF", "RG", "Endereco", "CEP", "Email", "EstadoCivil", "Ocupacao", "Nacionalidade", "DadosPagamento"]

# Campos para cota em nome de pessoa jurídica
_REQUIRED_FIELDS_PJ = ["NomeEmpresa", "CNPJ", "NomeSocio", "CPF", "RG", "Endereco", "CEP", "Email", "EstadoCivil", "Ocupacao", "Nacionalidade", "DadosPagamentoPJ"]

_REQUIRED_FIELDS = _REQUIRED_FIELDS_LISTA  # compat legado (listas)

_FIELD_LABELS = {
    "NomeCompleto":    "Nome completo",
    "CPF":             "CPF",
    "RG":              "RG",
    "Endereco":        "Endereço completo (rua, número, bairro, cidade)",
    "CEP":             "CEP",
    "Email":           "E-mail para receber o contrato",
    "EstadoCivil":     "Estado civil (solteiro, casado, etc.)",
    "Ocupacao":        "Profissão / ocupação",
    "Nacionalidade":   "Nacionalidade",
    "DadosPagamento":  "Dados para pagamento — Conta/Agência/PIX em nome do CPF",
    # PJ
    "NomeEmpresa":     "Nome da empresa",
    "CNPJ":            "CNPJ",
    "NomeSocio":       "Nome completo do sócio",
    "DadosPagamentoPJ":"Dados para pagamento — Conta/Agência/PIX em nome do CNPJ",
}

# Mapeamento para campos do FARO
_FARO_FIELD_MAP = {
    "NomeCompleto":    "Nome do contato",
    "CPF":             "CPF",
    "RG":              "RG",
    "Endereco":        "Endereço",
    "CEP":             "CEP",
    "Email":           "Email",
    "EstadoCivil":     "Estado Civil",
    "Ocupacao":        "Ocupação",
    "Nacionalidade":   "Nacionalidade",
    "DadosPagamento":  "Dados Pagamento",
    "NomeEmpresa":     "Nome Empresa",
    "CNPJ":            "CNPJ",
    "NomeSocio":       "Nome Sócio",
    "DadosPagamentoPJ":"Dados Pagamento PJ",
}


def _required_fields_for_card(card: dict) -> list[str]:
    """Retorna lista de campos obrigatórios conforme titularidade da cota."""
    if is_pj(card):
        return _REQUIRED_FIELDS_PJ
    return _REQUIRED_FIELDS_LISTA  # PF padrão (Listas e Bazar)

# ---------------------------------------------------------------------------
# Extração de dados com IA
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """
Você extrai dados pessoais de mensagens de texto para preenchimento de contrato.
Retorne EXCLUSIVAMENTE JSON válido, sem markdown, sem explicações.
""".strip()

_EXTRACT_PROMPT = """
Extraia os dados pessoais/empresariais presentes nesta mensagem. Use null para campos ausentes.

MENSAGEM: "{texto}"

JSON esperado:
{{
  "NomeCompleto": "nome completo da pessoa física ou null",
  "CPF": "xxx.xxx.xxx-xx ou null",
  "RG": "número do RG ou null",
  "Endereco": "endereço completo com rua, número, bairro, cidade (sem CEP) ou null",
  "CEP": "xxxxx-xxx ou null",
  "Email": "endereço de e-mail ou null",
  "EstadoCivil": "solteiro/casado/divorciado/viúvo ou null",
  "Ocupacao": "profissão ou ocupação ou null",
  "Nacionalidade": "ex: brasileiro/brasileira ou outra nacionalidade ou null",
  "DadosPagamento": "dados bancários PF (banco, agência, conta, pix) ou null",
  "NomeEmpresa": "razão social da empresa ou null",
  "CNPJ": "xx.xxx.xxx/xxxx-xx ou null",
  "NomeSocio": "nome completo do sócio ou null",
  "DadosPagamentoPJ": "dados bancários PJ (banco, agência, conta, pix) ou null"
}}
"""


async def _extract_fields_with_ai(texto: str) -> dict:
    """Extrai campos pessoais do texto via IA. Retorna dict com os campos encontrados."""
    async with AIClient() as ai:
        try:
            raw = await ai.complete(
                prompt=_EXTRACT_PROMPT.format(texto=texto.replace('"', "'")),
                system=_EXTRACT_SYSTEM,
                max_tokens=250,
            )
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                raise AIError("sem JSON")
            data = json.loads(m.group())
            return {
                k: v for k, v in data.items()
                if v and str(v).strip().lower() not in ("null", "none", "")
            }
        except (AIError, json.JSONDecodeError, KeyError) as e:
            logger.warning("agente_contrato: falha na extração IA: %s", e)
            # Fallback: CPF, CNPJ e e-mail via regex
            result = {}
            cpf_m = re.search(r"\b(\d{3}[.\-]?\d{3}[.\-]?\d{3}[.\-]?\d{2})\b", texto)
            if cpf_m:
                result["CPF"] = cpf_m.group(1)
            cnpj_m = re.search(r"\b(\d{2}[.\-]?\d{3}[.\-]?\d{3}[/]?\d{4}[.\-]?\d{2})\b", texto)
            if cnpj_m:
                result["CNPJ"] = cnpj_m.group(1)
            cep_m = re.search(r"\b(\d{5}-?\d{3})\b", texto)
            if cep_m:
                result["CEP"] = cep_m.group(1)
            email_m = re.search(r"[\w.\-+]+@[\w.\-]+\.\w{2,}", texto)
            if email_m:
                result["Email"] = email_m.group(0)
            return result


# ---------------------------------------------------------------------------
# Persistência dos dados coletados
# ---------------------------------------------------------------------------

def _load_collected(card: dict) -> dict:
    """Carrega dados pessoais já coletados (armazenados como JSON em Dados Pessoais Texto)."""
    raw = card.get("Dados Pessoais Texto") or ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


async def _save_collected(faro: FaroClient, card_id: str, collected: dict) -> None:
    """
    Salva dados coletados no FARO:
    - Como JSON em Dados Pessoais Texto (sempre)
    - Campos individuais onde o FARO aceitar
    """
    fields: dict = {"Dados Pessoais Texto": json.dumps(collected, ensure_ascii=False)}
    for key, faro_field in _FARO_FIELD_MAP.items():
        if key in collected:
            fields[faro_field] = collected[key]
    try:
        await faro.update_card(card_id, fields)
    except FaroError as e:
        logger.warning("agente_contrato: erro ao salvar dados do card %s: %s", card_id[:8], e)


# ---------------------------------------------------------------------------
# Construção de resposta (IA com fallback estático)
# ---------------------------------------------------------------------------

_COLLECT_SYSTEM = """
Você é Manuela, consultora da Consórcio Sorteado.
Está coletando dados pessoais de um lead que aceitou vender sua cota contemplada.
Tom: caloroso, direto, pessoal — continuidade natural da conversa anterior.
Máximo 5 linhas. Apenas o texto da mensagem, sem aspas.
""".strip()


def _build_response_static(nome: str, collected: dict, adm: str, required: list | None = None) -> tuple[str, bool]:
    """Fallback estático caso a IA falhe."""
    req = required or _REQUIRED_FIELDS
    missing = [f for f in req if not collected.get(f)]
    if not missing:
        msg = (
            f"Perfeito, {nome}! Todos os seus dados foram confirmados ✅\n\n"
            f"Agora só falta o *extrato detalhado* da sua cota {adm}. "
            f"Pode enviar uma foto ou PDF por aqui mesmo.\n\n"
            f"_(O extrato detalhado mostra o histórico completo da cota — "
            f"é diferente do comprovante de pagamento)_ 📄"
        )
        return msg, True
    received = [_FIELD_LABELS[f] for f in req if collected.get(f)]
    missing_labels = [_FIELD_LABELS[f] for f in missing]
    partes = []
    if received:
        partes.append(f"Recebi: {', '.join(received)} ✅")
    partes.append("Ainda preciso de:")
    partes += [f"• *{label}*" for label in missing_labels]
    return "\n".join(partes), False


async def _build_response(
    nome: str, collected: dict, adm: str, history: list,
    journey: dict | None = None, required: list | None = None,
) -> tuple[str, bool]:
    """
    Gera resposta personalizada para a coleta de dados pessoais.
    Usa IA com contexto do histórico + jornada completa da conversa.
    Fallback para resposta estática se IA falhar.
    """
    req     = required or _REQUIRED_FIELDS
    missing = [f for f in req if not collected.get(f)]
    is_complete = not missing
    history_ctx = history_to_text(history)
    journey_ctx = journey_to_text(journey or {})

    campos_str = ", ".join(_FIELD_LABELS[f] for f in req)

    if is_complete:
        prompt = (
            f"Lead: {nome} | Administradora: {adm}\n"
            f"Todos os dados pessoais foram coletados ({campos_str}).\n\n"
            f"Jornada do lead:\n{journey_ctx}\n\n"
            f"Histórico da conversa:\n{history_ctx}\n\n"
            f"Escreva uma mensagem curta confirmando que recebeu todos os dados "
            f"e pedindo o extrato detalhado da cota {adm} (foto ou PDF). "
            f"Mencione que o extrato mostra o histórico completo da cota — diferente do comprovante."
        )
    else:
        received_labels = [_FIELD_LABELS[f] for f in req if collected.get(f)]
        missing_labels  = [_FIELD_LABELS[f] for f in missing]
        received_str = ", ".join(received_labels) if received_labels else "nenhum ainda"
        missing_bullets = "\n".join(f"• *{l}*" for l in missing_labels)
        prompt = (
            f"Lead: {nome}\n"
            f"Dados recebidos: {received_str}\n"
            f"Dados ainda faltando:\n{missing_bullets}\n\n"
            f"Jornada do lead:\n{journey_ctx}\n\n"
            f"Histórico da conversa:\n{history_ctx}\n\n"
            f"Escreva uma mensagem confirmando o que já recebeu e pedindo os dados "
            f"que faltam. Liste os dados faltantes com bullet points em *negrito*."
        )

    try:
        async with AIClient() as ai:
            msg = await ai.complete(prompt=prompt, system=_COLLECT_SYSTEM, max_tokens=220)
        return msg.strip(), is_complete
    except (AIError, Exception) as e:
        logger.warning("agente_contrato: IA falhou na geração de resposta: %s — usando fallback", e)
        return _build_response_static(nome, collected, adm, required=req)


# ---------------------------------------------------------------------------
# Handlers públicos
# ---------------------------------------------------------------------------

async def handle_dados_pessoais(card: dict, texto: str) -> None:
    """
    Chamado quando lead em ASSINATURA envia texto (Listas e Bazar/LP).
    Extrai dados pessoais, confirma e guia para os campos faltantes.
    Se ZapSign Token já foi gerado (contrato enviado), silencia — não responde.
    """
    card_id  = card.get("id", "")
    nome     = get_name(card)
    phone    = get_phone(card)
    adm      = get_adm(card)
    required = _required_fields_for_card(card)

    if not phone:
        return

    # Contrato já enviado → silêncio total, agente comercial assume
    if card.get("ZapSign Token"):
        logger.info(
            "agente_contrato: card %s já tem ZapSign Token — ignorando mensagem de %s.",
            card_id[:8], phone,
        )
        return

    history   = load_history(card)
    journey   = load_journey(card)
    collected = _load_collected(card)

    # Pré-preenche campos que já temos do extrato/FARO (Bazar/LP)
    # Evita pedir ao lead dados que o sistema já possui
    _pre_fill = {
        "NomeCompleto": card.get("Nome do contato") or "",
        "CPF":          card.get("CPF") or "",
        "RG":           card.get("RG") or "",
        "Email":        card.get("Email") or "",
        "EstadoCivil":  card.get("Estado Civil") or "",
        "Ocupacao":     card.get("Ocupação") or "",
        "Nacionalidade":card.get("Nacionalidade") or "",
        "Endereco":     card.get("Endereço") or "",
        "CEP":          card.get("CEP") or "",
        "DadosPagamento": card.get("Dados Pagamento") or "",
    }
    for k, v in _pre_fill.items():
        if v and not collected.get(k):
            collected[k] = v

    new_data  = await _extract_fields_with_ai(texto)

    novos = {k: v for k, v in new_data.items() if v and not collected.get(k)}
    collected.update(novos)

    logger.info(
        "agente_contrato: card=%s | novos=%s | coletados=%s | faltam=%s",
        card_id[:8],
        list(novos.keys()),
        [f for f in required if collected.get(f)],
        [f for f in required if not collected.get(f)],
    )

    msg, completo = await _build_response(nome, collected, adm, history, journey, required=required)

    history = history_append(history, "user", texto)
    history = history_append(history, "assistant", msg)
    async with FaroClient() as faro:
        await _save_collected(faro, card_id, collected)
        await save_history(faro, card_id, history)

    try:
        async with WhapiClient() as w:
            await w.send_text(phone, msg)
    except WhapiError as e:
        logger.error("agente_contrato: erro ao enviar para %s: %s", phone, e)
        return

    if completo:
        logger.info("agente_contrato: card %s — dados completos, aguardando extrato.", card_id[:8])
        # Avisa Vitor via Slack com os dados coletados (informativo, sem aprovação)
        try:
            from services.safety_car import notify_dados_contrato
            await notify_dados_contrato(card, collected)
        except Exception as _sc_err:
            logger.warning("agente_contrato: falha ao notificar dados contrato Slack: %s", _sc_err)


async def handle_extrato_recebido(card: dict, msg) -> None:
    """
    Chamado quando lead envia mídia (foto/PDF do extrato) em ASSINATURA.
    Valida se dados pessoais estão completos antes de gerar o contrato ZapSign.
    Se ZapSign Token já foi gerado (contrato enviado), silencia.
    """
    card_id = card.get("id", "")
    nome    = get_name(card)
    phone   = get_phone(card)
    adm     = get_adm(card)

    # Contrato já enviado → ignora mídia adicional
    if card.get("ZapSign Token"):
        logger.info(
            "agente_contrato: card %s já tem ZapSign Token — ignorando mídia de %s.",
            card_id[:8], phone,
        )
        return

    collected = _load_collected(card)
    required  = _required_fields_for_card(card)
    missing   = [f for f in required if not collected.get(f)]

    if missing:
        # Dados incompletos — pede os que faltam antes de aceitar o extrato
        missing_labels = [_FIELD_LABELS[f] for f in missing]
        falta_str = ", ".join(f"*{l}*" for l in missing_labels)
        if phone:
            try:
                async with WhapiClient() as w:
                    await w.send_text(
                        phone,
                        f"Obrigada pelo extrato, {nome}! 😊 Antes de processar, "
                        f"ainda preciso dos seguintes dados pessoais:\n\n"
                        + "\n".join(f"• {_FIELD_LABELS[f]}" for f in missing)
                        + "\n\nAssim que receber, já avanço com seu contrato! 📋",
                    )
            except WhapiError as e:
                logger.error("agente_contrato: erro ao enviar para %s: %s", phone, e)
        logger.info(
            "agente_contrato: extrato recebido mas dados incompletos para card %s. Faltam: %s",
            card_id[:8], missing,
        )
        return

    # Dados completos → confirma e gera contrato
    bot_msg = (
        f"Perfeito, {nome}! 📄 Recebi o extrato. "
        f"Já estou preparando seu contrato — envio o link em instantes! 😊"
    )
    if phone:
        try:
            async with WhapiClient() as w:
                await w.send_text(phone, bot_msg)
        except WhapiError as e:
            logger.error("agente_contrato: erro ao confirmar extrato para %s: %s", phone, e)

    # Registra no histórico
    history = load_history(card)
    history = history_append(history, "user", "[Enviou extrato detalhado da cota]")
    history = history_append(history, "assistant", bot_msg)
    async with FaroClient() as faro:
        await save_history(faro, card_id, history)

    from jobs.contrato import generate_and_send_contract
    import asyncio
    asyncio.create_task(generate_and_send_contract(card))
    logger.info("agente_contrato: extrato recebido para card %s → gerando ZapSign.", card_id[:8])
