"""
services/pdf_extractor.py — Extração estruturada de extratos de consórcio via Gemini

Migração da Supabase Edge Function (Deno/Lovable) para Python nativo na VPS.
Lê PDFs de consórcio e retorna dados estruturados com alta precisão usando
Gemini 2.5 Flash com inline PDF (base64) — sem bibliotecas de parsing local.

Administradoras suportadas com regras específicas:
  EMBRACON, CAIXA, ITAÚ, VOLKSWAGEN, MYCON/COIMEX, BANCO DO BRASIL, SANTANDER

Uso:
    from services.pdf_extractor import extract_extrato, ExtratorError
    resultado = await extract_extrato("https://s3.wasabi.../extrato.pdf")
    print(resultado.dados_plano.valor_credito)  # ex: 150000.0
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceções tipadas
# ---------------------------------------------------------------------------

class ExtratorError(Exception):
    """Erro genérico do extrator."""

class PDFInvalido(ExtratorError):
    """Arquivo não é um PDF válido ou URL inacessível."""

class PDFCorrompido(ExtratorError):
    """PDF com estrutura corrompida ou ilegível."""

class GeminiError(ExtratorError):
    """Falha na chamada ao Gemini (timeout, quota, resposta inválida)."""

# ---------------------------------------------------------------------------
# Dataclasses de resultado (Pydantic-free — dataclass pura para leveza)
# ---------------------------------------------------------------------------

@dataclass
class DadosCadastrais:
    nome: Optional[str] = None
    cpf: Optional[str] = None
    tipo_pessoa: Optional[str] = None
    data_nascimento: Optional[str] = None
    profissao: Optional[str] = None
    documento_rg: Optional[str] = None
    endereco: Optional[Any] = None
    telefone: Optional[str] = None
    email: Optional[str] = None

@dataclass
class DadosPlano:
    administradora: Optional[str] = None
    grupo: Optional[str] = None
    cota: Optional[str] = None
    contrato: Optional[str] = None
    data_venda: Optional[str] = None
    data_adesao: Optional[str] = None
    produto: Optional[str] = None
    codigo_produto: Optional[str] = None
    bem: Optional[str] = None
    valor_credito: Optional[float] = None
    prazo_grupo_meses: Optional[int] = None
    meses_pagos: Optional[int] = None
    meses_a_pagar: Optional[int] = None
    taxa_administracao: Optional[float] = None
    fundo_reserva: Optional[float] = None
    percentual_mensal: Optional[float] = None
    valor_parcela_atual: Optional[float] = None
    sit_cobranca: Optional[str] = None
    assembleia_atual: Optional[Any] = None
    ultimo_reajuste: Optional[str] = None
    encerramento: Optional[str] = None

@dataclass
class Contemplacao:
    data_contemplacao: Optional[str] = None
    tipo: Optional[str] = None
    credito_original: Optional[float] = None
    credito_corrigido: Optional[float] = None
    valor_bem_entregue: Optional[float] = None
    valor_liquido: Optional[float] = None

@dataclass
class ResumoFinanceiro:
    valores_pagos: dict = field(default_factory=dict)
    valores_a_pagar: dict = field(default_factory=dict)
    parcelas_pagas: Optional[int] = None
    parcelas_restantes: Optional[int] = None
    total_pago_percentual: Optional[float] = None
    percentual_antecipado: Optional[float] = None
    ideal_pago: Optional[float] = None

@dataclass
class Pendencias:
    proxima_parcela: Optional[dict] = None
    parcelas_atrasadas: list = field(default_factory=list)

@dataclass
class ExtratoEstruturado:
    """Resultado completo da extração de um extrato de consórcio."""
    dados_cadastrais: DadosCadastrais = field(default_factory=DadosCadastrais)
    dados_plano: DadosPlano = field(default_factory=DadosPlano)
    contemplacao: Contemplacao = field(default_factory=Contemplacao)
    resumo_financeiro: ResumoFinanceiro = field(default_factory=ResumoFinanceiro)
    pendencias: Pendencias = field(default_factory=Pendencias)
    confidence_score: float = 0.0
    extraction_method: str = "gemini_inline_pdf"
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # JSON bruto retornado pelo Gemini


# ---------------------------------------------------------------------------
# Normalização pós-processamento (port fiel do Lovable)
# ---------------------------------------------------------------------------

_ADMIN_ALIASES: dict[str, str] = {
    "embracon": "EMBRACON",
    "caixa": "CAIXA",
    "xs5": "CAIXA",
    "porto seguro": "PORTO SEGURO",
    "porto": "PORTO SEGURO",
    "rodobens": "RODOBENS",
    "bradesco": "BRADESCO",
    "itau": "ITAU",
    "itaú": "ITAU",
    "santander": "SANTANDER",
    "banco do brasil": "BANCO DO BRASIL",
    "bb consorcio": "BANCO DO BRASIL",
    "bb consórcio": "BANCO DO BRASIL",
    "volkswagen": "VOLKSWAGEN",
    "vw": "VOLKSWAGEN",
    "mycon": "MYCON",
    "coimex": "MYCON",
}

def _normalize_cpf(value: Any) -> Any:
    """Normaliza CPF/CNPJ — remove lixo colado (ex: '168.410.987-65AUTOM' → '168.410.987-65')."""
    if not isinstance(value, str):
        return value
    cpf_match = re.search(r"(\d{3}\.\d{3}\.\d{3}-\d{2})", value)
    if cpf_match:
        return cpf_match.group(1)
    cnpj_match = re.search(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", value)
    if cnpj_match:
        return cnpj_match.group(1)
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    return value

def _normalize_date(value: Any) -> Any:
    """Converte datas para ISO 8601 (YYYY-MM-DD)."""
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", trimmed):
        return trimmed[:10]
    m = re.match(r"^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})", trimmed)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = ("19" if int(y) > 50 else "20") + y
        return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
    return value

def _normalize_money(value: Any) -> Any:
    """Converte 'R$ 1.234,56' → 1234.56 (float)."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return value
    cleaned = re.sub(r"R\$\s*", "", value, flags=re.IGNORECASE).strip()
    cleaned = cleaned.replace(" ", "")
    if not cleaned:
        return value
    # Formato BR: 1.234,56
    if re.match(r"^-?[\d.]+,\d{1,2}$", cleaned):
        n = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(n)
        except ValueError:
            return value
    # Decimal puro
    if re.match(r"^-?\d+(\.\d+)?$", cleaned):
        try:
            return float(cleaned)
        except ValueError:
            return value
    return value

def _normalize_admin(value: Any) -> Any:
    """Normaliza nome da administradora para canonical."""
    if not isinstance(value, str):
        return value
    lower = value.lower().strip()
    for key, canonical in _ADMIN_ALIASES.items():
        if key in lower:
            return canonical
    return value

_DATE_KEYS = {
    "data_adesao", "data_venda", "data_contemplacao", "data_pagamento",
    "primeira_assembleia", "ultima_assembleia", "encerramento",
    "ultimo_reajuste", "proximo_reajuste", "ultimo_vencimento_cota",
    "vencimento", "nascimento", "data_nascimento",
}
_MONEY_KEY_RE = re.compile(
    r"(valor|saldo|credito|crédito|total|fundo|taxa|multa|juros|seguro|parcela"
    r"|bem|liquido|líquido|original|corrigido|pago|pagar|antecipado|reserva"
    r"|administracao|administração|outros|rateio|adesao|adesão)",
    re.IGNORECASE,
)
_NON_MONEY_RE = re.compile(r"(percentual|numero|tipo|numero)", re.IGNORECASE)

def _walk_normalize(obj: Any) -> Any:
    """Percorre o JSON recursivamente normalizando campos."""
    if isinstance(obj, list):
        return [_walk_normalize(item) for item in obj]
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            lk = k.lower()
            if lk in ("cpf", "cnpj", "cpf_cnpj"):
                out[k] = _normalize_cpf(v)
            elif lk == "administradora":
                out[k] = _normalize_admin(v)
            elif lk in _DATE_KEYS or lk.startswith("data_") or lk.endswith("_data"):
                out[k] = _normalize_date(v) if isinstance(v, str) else _walk_normalize(v)
            elif (isinstance(v, str) and _MONEY_KEY_RE.search(lk)
                  and not _NON_MONEY_RE.search(lk)):
                out[k] = _normalize_money(v)
            elif isinstance(v, dict):
                out[k] = _walk_normalize(v)
            else:
                out[k] = v
        return out
    return obj


# ---------------------------------------------------------------------------
# Prompt do Gemini — portado fielmente do Lovable (index.ts)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """Você é um especialista em análise de documentos financeiros brasileiros, especialmente documentos de consórcio.

TAREFA: Analise o documento PDF e extraia informações estruturadas com alta precisão.

ADMINISTRADORAS RECONHECIDAS:
- EMBRACON ADMINISTRADORA DE CONSORCIO S/A (ou "EMBRACON")
- CAIXA CONSORCIO / XS5 ADMª DE CONSORCIO S/A (ou "CAIXA")
- PORTO SEGURO
- RODOBENS
- BRADESCO
- ITAÚ CONSÓRCIO (ou "ITAÚ", "ITAU")
- SANTANDER
- BANCO DO BRASIL CONSÓRCIOS (ou "BB CONSÓRCIO", "BB", "BANCO DO BRASIL")
- VOLKSWAGEN CONSÓRCIO
- MYCON / COIMEX ADM. CONSÓRCIOS S.A

REGRAS DE FORMATAÇÃO:
1. CPF: formato XXX.XXX.XXX-XX
2. CNPJ: formato XX.XXX.XXX/XXXX-XX
3. Datas: formato YYYY-MM-DD (ISO 8601)
4. Valores monetários: número decimal puro (ex: 40573.82, não "R$ 40.573,82")
5. Percentuais: número decimal (ex: 18.5 para 18,5%)
6. Telefones: apenas dígitos, sem formatação

CAMPOS A EXTRAIR:

dados_cadastrais: nome, cpf, tipo_pessoa, data_nascimento, profissao, documento_rg, endereco, telefone, email

dados_plano: administradora, grupo, cota, contrato, data_venda, data_adesao, tipo_venda, primeira_assembleia,
produto (normalizado: AUTOMOVEL/IMOVEL/SERVICO/MOTO/CAMINHAO), codigo_produto, subproduto, bem,
valor_credito, prazo_basico_meses, prazo_grupo_meses, meses_pagos, meses_a_pagar,
taxa_administracao (%), fundo_reserva (%), percentual_mensal (%), valor_parcela_atual,
sit_cobranca, assembleia_atual, ultimo_reajuste, proximo_reajuste, encerramento, ultimo_vencimento_cota

contemplacao: data_contemplacao, tipo (Sorteio/Lance), credito_original, credito_corrigido,
valor_bem_entregue, valor_liquido

resumo_financeiro:
  valores_pagos: fundo_comum, fundo_comum_percentual, fundo_reserva, fundo_reserva_percentual,
    taxa_administracao, taxa_administracao_percentual, seguros, adesao, multas, juros, outros_valores,
    total_pago, total_pago_percentual
  valores_a_pagar: (mesma estrutura, campo total em vez de total_pago)
  parcelas_pagas, parcelas_restantes, total_pago_percentual, percentual_antecipado, ideal_pago

pendencias: proxima_parcela {numero, vencimento, valor}, parcelas_atrasadas []

REGRAS ESPECÍFICAS POR ADMINISTRADORA:

SANTANDER:
- CPF: limpar texto extra colado (ex: "168.410.987-65AUTOM" → "168.410.987-65")
- "Parcelas pagas" → meses_pagos; "Parcelas restantes" → meses_a_pagar
- prazo_grupo_meses = meses_pagos + meses_a_pagar
- "Valores pagos" → total_pago; "Saldo Devedor" → valores_a_pagar.total
- Não tem breakdown de fundo/taxa nos valores pagos

CAIXA CONSÓRCIO:
- "Sit. Cobrança" → sit_cobranca
- Seção "Resumo Parcelas Pagas" Qtde Total → parcelas_pagas
- Seção "Resumo Parcelas a Pagar" Qtde Total → parcelas_restantes

ITAÚ CONSÓRCIO:
- "Conta Corrente" tabela: contar linhas = meses_pagos
- "Assemb. Atual" → assembleia_atual
- Página 2: Valores/Percentuais pagos/a pagar → resumo_financeiro detalhado

VOLKSWAGEN:
- Cota: remover sufixo "-00" (ex: "01234-00" → "01234")
- "Conta Corrente": contar linhas "RECBTO. PARCELA" = meses_pagos (ignorar LANCE e PAGTO BEM)

MYCON/COIMEX:
- Administradora: SEMPRE "MYCON"
- Cota: remover sufixo "-00"
- "BEM BÁSICO": extrair código, tipo (produto) e valor (valor_credito)

BANCO DO BRASIL:
- "Proposta" → contrato
- "Prazo Contratado" → prazo_grupo_meses
- meses_a_pagar = prazo_contratado - meses_pagos

EMBRACON:
- "Produto" no documento → normalizar para tipo padrão (produto) e usar como bem

Se um campo não for encontrado, use null.
Retorne APENAS o JSON válido, sem markdown ou texto adicional.

Estrutura esperada:
{
  "dados_cadastrais": {...},
  "dados_plano": {...},
  "contemplacao": {...},
  "resumo_financeiro": {...},
  "pendencias": {...},
  "confidence": 0.95
}"""


# ---------------------------------------------------------------------------
# Download do PDF com retry exponencial
# ---------------------------------------------------------------------------

_MAX_PDF_SIZE = 5 * 1024 * 1024  # 5 MB
_DOWNLOAD_TIMEOUT = 30.0          # segundos
_MAX_RETRIES = 3

async def _download_pdf(url: str) -> bytes:
    """
    Baixa o PDF da URL com retry exponencial.
    Valida que é um PDF real (header %PDF).
    Levanta PDFInvalido ou PDFCorrompido em caso de falha.
    """
    last_error: Exception = Exception("Sem tentativas")

    for attempt in range(_MAX_RETRIES):
        delay = 2 ** attempt  # 1s, 2s, 4s
        try:
            async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; ConsorcioBOT/1.0)",
                    "Accept": "application/pdf,application/octet-stream,*/*",
                })
                resp.raise_for_status()
                data = resp.content

            if len(data) < 100:
                raise PDFInvalido(f"Arquivo muito pequeno ({len(data)} bytes) — provável página de erro")

            if len(data) > _MAX_PDF_SIZE:
                raise PDFInvalido(f"PDF muito grande ({len(data)//1024}KB > 5MB)")

            if not data[:5].startswith(b"%PDF"):
                # Pode ser HTML de erro/login
                preview = data[:100].decode("latin1", errors="replace")
                if "<html" in preview.lower() or "<!doctype" in preview.lower():
                    raise PDFInvalido("Servidor retornou HTML — link expirado ou requer autenticação")
                raise PDFCorrompido(f"Arquivo não começa com %PDF (início: {preview[:40]!r})")

            logger.info("PDF baixado: %d bytes de %s (tentativa %d)", len(data), url[-60:], attempt + 1)
            return data

        except (PDFInvalido, PDFCorrompido):
            raise  # Não retenta erros definitivos
        except httpx.HTTPStatusError as e:
            last_error = PDFInvalido(f"HTTP {e.response.status_code} ao baixar PDF")
        except httpx.RequestError as e:
            last_error = PDFInvalido(f"Erro de rede: {e}")

        if attempt < _MAX_RETRIES - 1:
            logger.warning("Download falhou (tentativa %d/%d): %s — aguardando %ds",
                           attempt + 1, _MAX_RETRIES, last_error, delay)
            await asyncio.sleep(delay)

    raise last_error

# ---------------------------------------------------------------------------
# Chamada ao Gemini com inline PDF
# ---------------------------------------------------------------------------

_GEMINI_TIMEOUT = 120.0   # PDF pode demorar — 2 minutos máximo
_GEMINI_RETRIES = 3

async def _call_gemini(pdf_bytes: bytes) -> str:
    """
    Envia o PDF para o Gemini 2.5 Flash como inline data (base64).
    Retorna o texto bruto da resposta.
    """
    from config import GEMINI_API_KEY, GEMINI_MODEL_PDF

    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY não configurada no .env")

    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "application/pdf",
                            "data": pdf_b64,
                        }
                    },
                    {
                        "text": (
                            "Analise este extrato de consórcio e extraia as informações estruturadas. "
                            "Retorne APENAS o JSON válido conforme as instruções do sistema."
                        )
                    },
                ]
            }
        ],
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 16000,
        },
    }

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL_PDF}:generateContent?key={GEMINI_API_KEY}"
    )

    last_error: Exception = GeminiError("Sem tentativas")

    for attempt in range(_GEMINI_RETRIES):
        delay = 2 ** attempt  # 1s, 2s, 4s
        try:
            async with httpx.AsyncClient(timeout=_GEMINI_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                raise GeminiError(f"Gemini retornou sem candidatos: {data}")

            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()

            if not text:
                raise GeminiError("Gemini retornou texto vazio")

            logger.info("Gemini respondeu: %d chars (tentativa %d)", len(text), attempt + 1)
            return text

        except GeminiError:
            raise
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                last_error = GeminiError(f"Rate limit Gemini (429)")
            elif status >= 500:
                last_error = GeminiError(f"Erro servidor Gemini ({status})")
            else:
                raise GeminiError(f"HTTP {status}: {e.response.text[:200]}")
        except httpx.TimeoutException:
            last_error = GeminiError(f"Timeout após {_GEMINI_TIMEOUT}s")
        except Exception as e:
            last_error = GeminiError(str(e))

        if attempt < _GEMINI_RETRIES - 1:
            logger.warning("Gemini falhou (tentativa %d/%d): %s — aguardando %ds",
                           attempt + 1, _GEMINI_RETRIES, last_error, delay)
            await asyncio.sleep(delay)

    raise last_error


# ---------------------------------------------------------------------------
# Parsing do JSON retornado pelo Gemini
# ---------------------------------------------------------------------------

def _parse_gemini_json(text: str) -> dict:
    """
    Extrai e parseia o JSON da resposta do Gemini.
    Remove blocos markdown (```json ... ```) se presentes.
    """
    # Remove markdown code blocks
    cleaned = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*", "", cleaned)

    # Extrai o primeiro objeto JSON válido
    json_match = re.search(r"\{[\s\S]*\}", cleaned)
    if not json_match:
        raise GeminiError(f"Nenhum JSON encontrado na resposta: {text[:200]}")

    raw_json = json_match.group()

    # Tenta parse direto
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        pass

    # Tenta limpar problemas comuns
    fixed = (raw_json
             .replace(",}", "}")
             .replace(",]", "]")
             .replace("\n", " ")
             .replace("\r", ""))
    # Remove caracteres de controle
    fixed = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", fixed)

    try:
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        raise GeminiError(f"JSON inválido mesmo após limpeza: {e} | início: {raw_json[:200]}")

# ---------------------------------------------------------------------------
# Mapeamento do JSON bruto para dataclasses tipados
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None

def _map_to_dataclasses(raw: dict) -> ExtratoEstruturado:
    """Converte o dict normalizado do Gemini para dataclasses tipados."""
    dc_raw = raw.get("dados_cadastrais") or {}
    dp_raw = raw.get("dados_plano") or {}
    co_raw = raw.get("contemplacao") or {}
    rf_raw = raw.get("resumo_financeiro") or {}
    pe_raw = raw.get("pendencias") or {}

    dados_cad = DadosCadastrais(
        nome=dc_raw.get("nome"),
        cpf=dc_raw.get("cpf"),
        tipo_pessoa=dc_raw.get("tipo_pessoa"),
        data_nascimento=dc_raw.get("data_nascimento"),
        profissao=dc_raw.get("profissao"),
        documento_rg=dc_raw.get("documento_rg"),
        endereco=dc_raw.get("endereco"),
        telefone=dc_raw.get("telefone"),
        email=dc_raw.get("email"),
    )

    dados_plano = DadosPlano(
        administradora=dp_raw.get("administradora"),
        grupo=str(dp_raw["grupo"]) if dp_raw.get("grupo") is not None else None,
        cota=str(dp_raw["cota"]) if dp_raw.get("cota") is not None else None,
        contrato=str(dp_raw["contrato"]) if dp_raw.get("contrato") is not None else None,
        data_venda=dp_raw.get("data_venda"),
        data_adesao=dp_raw.get("data_adesao"),
        produto=dp_raw.get("produto"),
        codigo_produto=dp_raw.get("codigo_produto"),
        bem=dp_raw.get("bem"),
        valor_credito=_to_float(dp_raw.get("valor_credito")),
        prazo_grupo_meses=_to_int(dp_raw.get("prazo_grupo_meses") or dp_raw.get("prazo_meses")),
        meses_pagos=_to_int(dp_raw.get("meses_pagos")),
        meses_a_pagar=_to_int(dp_raw.get("meses_a_pagar")),
        taxa_administracao=_to_float(dp_raw.get("taxa_administracao")),
        fundo_reserva=_to_float(dp_raw.get("fundo_reserva")),
        percentual_mensal=_to_float(dp_raw.get("percentual_mensal")),
        valor_parcela_atual=_to_float(dp_raw.get("valor_parcela_atual")),
        sit_cobranca=dp_raw.get("sit_cobranca"),
        assembleia_atual=dp_raw.get("assembleia_atual"),
        ultimo_reajuste=dp_raw.get("ultimo_reajuste"),
        encerramento=dp_raw.get("encerramento"),
    )

    contemplacao = Contemplacao(
        data_contemplacao=co_raw.get("data_contemplacao"),
        tipo=co_raw.get("tipo"),
        credito_original=_to_float(co_raw.get("credito_original")),
        credito_corrigido=_to_float(co_raw.get("credito_corrigido")),
        valor_bem_entregue=_to_float(co_raw.get("valor_bem_entregue")),
        valor_liquido=_to_float(co_raw.get("valor_liquido")),
    )

    resumo = ResumoFinanceiro(
        valores_pagos=rf_raw.get("valores_pagos") or {},
        valores_a_pagar=rf_raw.get("valores_a_pagar") or {},
        parcelas_pagas=_to_int(rf_raw.get("parcelas_pagas")),
        parcelas_restantes=_to_int(rf_raw.get("parcelas_restantes")),
        total_pago_percentual=_to_float(rf_raw.get("total_pago_percentual")),
        percentual_antecipado=_to_float(rf_raw.get("percentual_antecipado")),
        ideal_pago=_to_float(rf_raw.get("ideal_pago")),
    )

    pendencias = Pendencias(
        proxima_parcela=pe_raw.get("proxima_parcela"),
        parcelas_atrasadas=pe_raw.get("parcelas_atrasadas") or [],
    )

    confidence = float(raw.get("confidence", 0.85))

    return ExtratoEstruturado(
        dados_cadastrais=dados_cad,
        dados_plano=dados_plano,
        contemplacao=contemplacao,
        resumo_financeiro=resumo,
        pendencias=pendencias,
        confidence_score=round(confidence, 2),
        extraction_method="gemini_inline_pdf",
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Função principal pública
# ---------------------------------------------------------------------------

async def extract_extrato(pdf_url: str) -> ExtratoEstruturado:
    """
    Extrai dados estruturados de um extrato de consórcio em PDF.

    Pipeline:
      1. Download do PDF (retry exponencial, validação de header)
      2. Envio ao Gemini 2.5 Flash como inline PDF base64
      3. Parsing e normalização do JSON retornado
      4. Mapeamento para dataclasses tipados

    Args:
        pdf_url: URL pública do PDF (ex: URL do Wasabi retornada pelo Whapi)

    Returns:
        ExtratoEstruturado com todos os campos extraídos

    Raises:
        PDFInvalido: URL inacessível, arquivo não é PDF, muito grande
        PDFCorrompido: Header inválido, arquivo corrompido
        GeminiError: Falha na chamada à IA (timeout, quota, resposta inválida)
        ExtratorError: Outros erros internos
    """
    warnings: list[str] = []

    # 1. Download
    logger.info("Extrator: baixando PDF de %s", pdf_url[-80:])
    pdf_bytes = await _download_pdf(pdf_url)
    logger.info("Extrator: %d bytes baixados", len(pdf_bytes))

    # 2. Gemini
    logger.info("Extrator: enviando para Gemini (%d bytes base64: %d chars)",
                len(pdf_bytes), len(base64.b64encode(pdf_bytes)))
    raw_text = await _call_gemini(pdf_bytes)

    # 3. Parsing
    raw_dict = _parse_gemini_json(raw_text)

    # 4. Normalização
    normalized = _walk_normalize(raw_dict)

    # 5. Mapeamento
    resultado = _map_to_dataclasses(normalized)
    resultado.warnings = warnings

    logger.info(
        "Extrator: concluído | adm=%s | credito=%.0f | pago=%.0f | confidence=%.2f",
        resultado.dados_plano.administradora or "?",
        resultado.dados_plano.valor_credito or 0,
        (resultado.resumo_financeiro.valores_pagos or {}).get("total_pago") or 0,
        resultado.confidence_score,
    )

    return resultado
