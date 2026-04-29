"""
config.py — Configurações centralizadas do sistema Consórcio Sorteado
"""

import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")

# ---------------------------------------------------------------------------
# FARO CRM
# ---------------------------------------------------------------------------
FARO_API_KEY = os.environ["FARO_API_KEY"]
FARO_BASE_URL = "https://ecinffgoycqvktmrmvaz.supabase.co/functions/v1"
PIPELINE_ID = "380763c7-4bf7-456b-bc59-be28e04ffb33"


class Stage:
    PRECIFICACAO = "a1e04ddf-0107-4d71-af83-2ae4c9799edb"
    EM_NEGOCIACAO = "7ce3a0e6-3602-42d9-8374-b4d093fb41fb"
    FINALIZACAO_COMERCIAL = "56166777-d827-4a89-9d8e-c833c152c241"
    NEG_CONGELADA = "bc7d38d3-069a-4be7-93ca-2071b381f4ff"
    LISTAS = "144bf577-1e41-44ab-b620-28d6cb6f7db2"
    BAZAR = "7c6405fc-63c5-46ca-b1cf-d9162ed73aa8"
    PRIMEIRA_ATIVACAO = "e0c7411e-c62e-4091-b717-0270ae26dd57"
    SEGUNDA_ATIVACAO = "1e38c62a-4b90-4ae0-b545-4cf7a2538726"
    TERCEIRA_ATIVACAO = "1cf8c820-90c2-4438-bd2a-7b54867ababd"
    QUARTA_ATIVACAO = "e7a00875-f0ec-4bed-b981-48431498e0de"
    ACEITO = "66f1d4c4-dd6e-45b2-b624-d6880936b39c"
    ASSINATURA = "7dc8bca0-af09-4f74-a3d0-13cbabb14bf0"
    SUCESSO = "c6ac32c6-74c2-459f-9a98-3e14cf81ebac"
    PERDIDO = "d5c9a6e1-1b5b-424d-8659-4d002599586b"
    ON_HOLD = "be69c623-f1a9-4c57-b6bd-1d9d3291ae02"
    TESTES = "824ccd4e-aba5-47b5-826d-414e5923c37b"
    LIXO = "e86bd9b3-f2aa-4b32-9d80-3e1c249a50ad"
    FLUXO_CADENCIA = "b4f34818-ba01-478f-a163-e900ba51daef"
    DISPENSADOS = "fb52b454-de52-4057-bd2c-645014636cba"
    NAO_QUALIFICADO = "38c91042-2205-4d7d-9015-215a526acefc"
    LP = "f3d1c2ea-ab74-4275-9583-bcec89c58c0c"
    PROBLEMA_CONTATO = "0f593ed2-3c5e-477e-9b0d-1740808fe145"


ACTIVATION_SEQUENCE = {
    Stage.LISTAS: Stage.PRIMEIRA_ATIVACAO,
    Stage.BAZAR: Stage.PRIMEIRA_ATIVACAO,
    Stage.LP: Stage.PRIMEIRA_ATIVACAO,
    Stage.PRIMEIRA_ATIVACAO: Stage.SEGUNDA_ATIVACAO,
    Stage.SEGUNDA_ATIVACAO: Stage.TERCEIRA_ATIVACAO,
    Stage.TERCEIRA_ATIVACAO: Stage.QUARTA_ATIVACAO,
    Stage.QUARTA_ATIVACAO: Stage.FLUXO_CADENCIA,
}

REATIVACAO_DIAS = {
    Stage.PRIMEIRA_ATIVACAO: int(os.getenv("REATIVACAO_DIAS_1", "2")),
    Stage.SEGUNDA_ATIVACAO:  int(os.getenv("REATIVACAO_DIAS_2", "5")),
    Stage.TERCEIRA_ATIVACAO: int(os.getenv("REATIVACAO_DIAS_3", "7")),
    Stage.QUARTA_ATIVACAO:   int(os.getenv("REATIVACAO_DIAS_4", "14")),
}

# ---------------------------------------------------------------------------
# WHAPI — único provider WhatsApp (substitui Whapi Listas + Z-API Bazar/Site)
#
# Três pools distintos:
#   LISTA : até 5 tokens com rotação aleatória anti-ban (volume alto)
#   BAZAR : 1 token dedicado para leads orgânicos via empresa parceira
#   LP    : 1 token dedicado para leads do site próprio / tráfego pago
#
# .env:
#   WHAPI_TOKEN_LISTA_1=...  (obrigatório; aceita WHAPI_TOKEN como retrocompat)
#   WHAPI_TOKEN_LISTA_2=...  (opcional; aceita WHAPI_TOKEN_2 como retrocompat)
#   WHAPI_TOKEN_LISTA_3..5=  (opcionais)
#   WHAPI_TOKEN_BAZAR=...    (obrigatório para fluxo Bazar)
#   WHAPI_TOKEN_LP=...       (obrigatório para fluxo LP/Site)
# ---------------------------------------------------------------------------
WHAPI_BASE_URL = os.getenv("WHAPI_BASE_URL", "https://gate.whapi.cloud")

WHAPI_LISTA_TOKENS: list[str] = [
    t for t in [
        os.getenv("WHAPI_TOKEN_LISTA_1", os.getenv("WHAPI_TOKEN", "")),
        os.getenv("WHAPI_TOKEN_LISTA_2", os.getenv("WHAPI_TOKEN_2", "")),
        os.getenv("WHAPI_TOKEN_LISTA_3", ""),
        os.getenv("WHAPI_TOKEN_LISTA_4", ""),
        os.getenv("WHAPI_TOKEN_LISTA_5", ""),
    ] if t
]

WHAPI_BAZAR_TOKEN: str = os.getenv("WHAPI_TOKEN_BAZAR", "")
WHAPI_LP_TOKEN: str    = os.getenv("WHAPI_TOKEN_LP", "")

# Avisos em startup se tokens não configurados
if not WHAPI_BAZAR_TOKEN and WHAPI_LISTA_TOKENS:
    import logging as _log
    _log.getLogger(__name__).warning(
        "WHAPI_TOKEN_BAZAR não configurado — usando pool de Listas como fallback. "
        "Configure WHAPI_TOKEN_BAZAR no .env para isolar os fluxos."
    )
if not WHAPI_LP_TOKEN and WHAPI_LISTA_TOKENS:
    import logging as _log
    _log.getLogger(__name__).warning(
        "WHAPI_TOKEN_LP não configurado — usando WHAPI_TOKEN_BAZAR como fallback para LP. "
        "Configure WHAPI_TOKEN_LP no .env para isolar o fluxo LP."
    )

# ---------------------------------------------------------------------------
# IA
# ---------------------------------------------------------------------------
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
DEFAULT_AI_MODEL   = os.getenv("DEFAULT_AI_MODEL", "gpt-4o-mini")
DEFAULT_VISION_MODEL = os.getenv("DEFAULT_VISION_MODEL", "gpt-4o")
GEMINI_MODEL_PDF     = os.getenv("GEMINI_MODEL_PDF", "gemini-2.5-flash")
HISTORY_MODEL      = os.getenv("HISTORY_MODEL", "gemini-2.0-flash")

# ---------------------------------------------------------------------------
# Comportamento dos jobs
# ---------------------------------------------------------------------------
LISTAS_DELAY_MIN_S    = int(os.getenv("LISTAS_DELAY_MIN_S", "30"))
LISTAS_DELAY_MAX_S    = int(os.getenv("LISTAS_DELAY_MAX_S", "90"))
REATIVADOR_DELAY_MIN_S = int(os.getenv("REATIVADOR_DELAY_MIN_S", "60"))
REATIVADOR_DELAY_MAX_S = int(os.getenv("REATIVADOR_DELAY_MAX_S", "900"))
JOB_BATCH_LIMIT       = int(os.getenv("JOB_BATCH_LIMIT", "50"))

# Janelas de envio por fluxo (hora BRT)
SEND_WINDOW_START     = int(os.getenv("SEND_WINDOW_START", "8"))   # Listas
SEND_WINDOW_END       = int(os.getenv("SEND_WINDOW_END", "20"))
BAZAR_WINDOW_START    = int(os.getenv("BAZAR_WINDOW_START", "6"))  # Bazar/LP
BAZAR_WINDOW_END      = int(os.getenv("BAZAR_WINDOW_END", "20"))

# Jitter por fluxo (segundos)
BAZAR_JITTER_MIN_S    = int(os.getenv("BAZAR_JITTER_MIN_S", "900"))   # 15 min
BAZAR_JITTER_MAX_S    = int(os.getenv("BAZAR_JITTER_MAX_S", "1800"))  # 30 min
LISTAS_JITTER_MIN_S   = int(os.getenv("LISTAS_JITTER_MIN_S", "1200")) # 20 min
LISTAS_JITTER_MAX_S   = int(os.getenv("LISTAS_JITTER_MAX_S", "2100")) # 35 min

# ---------------------------------------------------------------------------
# Notificações
# ---------------------------------------------------------------------------
NOTIFY_PHONES = [p.strip() for p in os.getenv("NOTIFY_PHONES", "").split(",") if p.strip()]
NOTIFY_GROUP  = os.getenv("NOTIFY_GROUP", "")  # Grupo Alarmes Sistemas CS


def _parse_consultant_phones(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if ":" in item:
            name, phone = item.split(":", 1)
            result[name.strip().lower()] = phone.strip()
    return result


_CONSULTANT_PHONES_RAW = os.getenv("CONSULTANT_PHONES", "")
CONSULTANT_PHONES: dict[str, str] = (
    _parse_consultant_phones(_CONSULTANT_PHONES_RAW)
    if _CONSULTANT_PHONES_RAW
    else {
        "vitor": "5519936185086",
        "vitor oliveira": "5519936185086",
        "manuela": "5511959411085",
        "sônia": "5511947882916",
        "sonia": "5511947882916",
    }
)

SLACK_WEBHOOK_URL     = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_LOG_CS_URL      = os.getenv("SLACK_LOG_CS_URL", "")

# ---------------------------------------------------------------------------
# Qualificação de extratos
# ---------------------------------------------------------------------------
QUALIFICACAO_PERCENTUAL_MAXIMO = float(os.getenv("QUALIFICACAO_PERCENTUAL_MAXIMO", "50"))
QUALIFICACAO_VALOR_PAGO_MAXIMO  = float(os.getenv("QUALIFICACAO_VALOR_PAGO_MAXIMO",  "150000"))

# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------
PUBLIC_URL  = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT        = int(os.getenv("PORT", "8000"))
SECRET_KEY  = os.getenv("SECRET_KEY", "change-me-in-production")
IMAGES_DIR  = os.getenv("IMAGES_DIR", "/tmp/cs_images")

# ---------------------------------------------------------------------------
# Modo de testes
# ---------------------------------------------------------------------------
TEST_MODE  = os.getenv("TEST_MODE", "true").lower() in ("1", "true", "yes")
TEST_PHONE = os.getenv("TEST_PHONE", "")


def filter_test_cards(cards: list) -> list:
    if not TEST_MODE:
        return cards
    if not TEST_PHONE:
        import logging
        logging.getLogger(__name__).error(
            "TEST_MODE=true mas TEST_PHONE não configurado — "
            "nenhum card será processado para evitar disparos acidentais."
        )
        return []
    digits = "".join(c for c in TEST_PHONE if c.isdigit())
    filtered = [
        card for card in cards
        if digits in "".join(
            c for c in str(card.get("Telefone") or card.get("Telefone alternativo") or "")
            if c.isdigit()
        )
    ]
    if not filtered:
        import logging
        logging.getLogger(__name__).info(
            "TEST_MODE: nenhum dos %d cards tem o telefone de teste (%s), pulando.",
            len(cards), TEST_PHONE,
        )
    return filtered


# ---------------------------------------------------------------------------
# Agentes de IA
# ---------------------------------------------------------------------------
DEBOUNCE_SECONDS  = int(os.getenv("DEBOUNCE_SECONDS", "15"))
HISTORY_MAX_TURNS = int(os.getenv("HISTORY_MAX_TURNS", "30"))
REDIS_URL         = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

# ---------------------------------------------------------------------------
# Filtros de ativação Bazar/Site
# ---------------------------------------------------------------------------
_DEFAULT_ADM_EXCLUSOES = (
    "bamaq,promove,honda,disal,remaza,primo rossi,yamaha,ancora,"
    "enviado,embracon,volkswagen,iveco,comauto,magalu,groscon,"
    "banrisul,simpala,unifisa,multimarcas,canopus,rodobens,cnp,"
    "servopa,volks,chevrolet,poupex,hs,consorcio santa emilia,vida nova"
)
ATIVACAO_ADM_EXCLUSOES = [
    x.strip().lower()
    for x in os.getenv("ATIVACAO_ADM_EXCLUSOES", _DEFAULT_ADM_EXCLUSOES).split(",")
    if x.strip()
]
ATIVACAO_CONTEMPLACAO_EXCLUSOES = [
    x.strip().lower()
    for x in os.getenv("ATIVACAO_CONTEMPLACAO_EXCLUSOES", "lance,nao-cont").split(",")
    if x.strip()
]
ATIVACAO_TIPO_BEM_EXCLUSOES = [
    x.strip().lower()
    for x in os.getenv("ATIVACAO_TIPO_BEM_EXCLUSOES", "veiculo,moto,caminh").split(",")
    if x.strip()
]
