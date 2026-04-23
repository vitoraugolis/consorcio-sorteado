# CLAUDE.md — Consórcio Sorteado · Contexto Completo do Projeto

> Este arquivo é lido automaticamente pelo Claude Code ao abrir esta pasta.
> Ele contém todo o contexto construído na sessão de desenvolvimento anterior (Cowork).

---

## O que é este sistema

Servidor de automação comercial para a empresa **Consórcio Sorteado** (Guará Lab).
Automatiza o ciclo completo de vendas de cotas de consórcio: desde a ativação de leads frios
até a assinatura do contrato via ZapSign.

**Stack:** Python 3.12 · FastAPI · APScheduler · HTTPX · python-dotenv

---

## Arquitetura Geral

```
                  ┌─────────────────────────────────────────┐
                  │              FARO CRM                    │
                  │  (Supabase-backed, pipeline de stages)   │
                  └──────────────┬──────────────────────────┘
                                 │ API REST
              ┌──────────────────▼──────────────────────────┐
              │         SERVIDOR (main.py + FastAPI)         │
              │                                              │
              │  ┌─────────────┐   ┌──────────────────────┐ │
              │  │  APScheduler│   │  Webhooks (entrada)   │ │
              │  │  (7 jobs)   │   │  /webhook/whapi       │ │
              │  └──────┬──────┘   │  /webhook/zapi        │ │
              │         │          │  /webhook/zapsign      │ │
              └─────────┼──────────┴──────────┬─────────────┘
                        │                     │
          ┌─────────────▼──────┐   ┌──────────▼──────────┐
          │   WHAPI (Listas)   │   │   Z-API (Bazar/Site) │
          │  2 canais, anti-ban│   │   instância única     │
          └────────────────────┘   └─────────────────────┘
```

---

## Dois Fluxos de Lead Totalmente Distintos

### Fluxo 1 — Listas (cold leads)
- **Origem:** Planilhas de leads importadas no FARO
- **WhatsApp:** Whapi (2 tokens em rotação aleatória anti-ban)
- **Identificação:** `is_lista(card)` → `True` se campo "Lista" preenchido no FARO
- **Webhook entrada:** `POST /webhook/whapi`

### Fluxo 2 — Bazar/Site (leads orgânicos)
- **Origem:** Leads do site e do bazar (marketplace) que entram no FARO
- **WhatsApp:** Z-API (instância única: `3E1E234073A4F045CF922E7FADD21987`)
- **Diferencial:** passam por **qualificação de extrato** antes da precificação
- **Webhook entrada:** `POST /webhook/zapi`

---

## Pipeline de Stages (FARO CRM)

```
NOVO → PRIMEIRA_ATIVACAO → SEGUNDA_ATIVACAO → TERCEIRA_ATIVACAO → QUARTA_ATIVACAO
     ↓ (Bazar/Site apenas: qualificação inline durante ativações)
PRECIFICACAO → NEGOCIACAO → ACEITO → ASSINATURA → SUCESSO → FINALIZACAO_COMERCIAL
     ↓                    ↓
NAO_QUALIFICADO       PERDIDO
```

Todos os stages estão em `config.py` como `class Stage(str, Enum)`.

---

## Estrutura de Arquivos

```
consorcio-sorteado/
├── main.py                    # FastAPI + APScheduler, lifespan, todos os endpoints
├── config.py                  # Todas as variáveis de ambiente + enum Stage
├── requirements.txt           # fastapi, uvicorn, httpx, apscheduler, python-dotenv
├── .env                       # ← NUNCA commitar. Contém todos os tokens reais.
├── Dockerfile                 # Para Railway (python:3.12-slim)
├── railway.toml               # Configuração de deploy Railway
├── Procfile                   # web: uvicorn main:app --host 0.0.0.0 --port $PORT
├── setup.sh                   # Cria .venv e instala dependências (rodar 1x)
├── run.sh                     # Ativa .venv e sobe servidor com --reload
│
├── services/
│   ├── faro.py                # FaroClient: get_cards, move_card, update_card, etc.
│   ├── whapi.py               # WhapiClient: send_text, send_media + dual-token rotation
│   ├── zapi.py                # ZapiClient: send_text, send_media (Z-API REST)
│   ├── zapsign.py             # ZapSignClient: create_from_template (por administradora)
│   ├── ai.py                  # AIClient: complete() + complete_with_image() (multimodal)
│   └── slack.py               # slack_alert/error/warning/info → Incoming Webhook
│
├── webhooks/
│   ├── router.py              # IncomingMessage dataclass + route_message()
│   ├── negociador.py          # Lógica de negociação (responde leads em stages ativos)
│   └── qualificador.py        # Qualificação de extrato via IA visão (Bazar/Site)
│
├── jobs/
│   ├── ativacao_listas.py     # Job: dispara msgs de ativação para leads de Listas
│   ├── ativacao_bazar_site.py # Job: dispara msgs de ativação para leads Bazar/Site
│   ├── reativador.py          # Job: reativa leads sem resposta há X dias
│   ├── follow_up.py           # Job: follow-up em propostas enviadas sem resposta
│   ├── precificacao.py        # Job: envia proposta quando card entra em PRECIFICACAO
│   ├── contrato.py            # Job: gera contrato ZapSign quando card entra em ACEITO
│   └── __init__.py
│
└── tests/
    └── simulation.py          # 19 cenários de teste simulando payloads reais
```

---

## Serviços — Detalhes Críticos

### `services/faro.py`
- `FaroClient` — context manager assíncrono (`async with FaroClient() as faro`)
- Métodos principais: `get_cards(stage)`, `get_cards_all_pages(stage)`, `move_card(id, stage)`, `update_card(id, fields)`
- Helpers de leitura de campos: `get_name(card)`, `get_phone(card)`, `get_administradora(card)`, `is_lista(card)`
- `is_lista(card)` → `True` se campo "Lista" preenchido → determina qual canal WhatsApp usar

### `services/whapi.py`
- `_TOKEN_POOL` = lista com `WHAPI_TOKEN` + `WHAPI_TOKEN_2` (rotação aleatória com `random.choice`)
- `WhapiClient(token=None)` → se token não passado, sorteia do pool automaticamente
- Usado **exclusivamente** para leads de Listas

### `services/zapi.py`
- `ZapiClient(instance_key)` → recebe chave do `.env` (ex: `"BAZAR"`, `"SITE"`, `"ITAU"`)
- Internamente faz: `os.environ[f"ZAPI_INSTANCE_{instance_key}"]` → `"INSTANCE_ID:TOKEN"`
- Usado **exclusivamente** para leads de Bazar/Site

### `services/ai.py`
- `AIClient` suporta OpenAI, Anthropic Claude, Gemini
- `complete(prompt, system, max_tokens, model)` → resposta textual
- `complete_with_image(prompt, media_url, system, max_tokens, model)` → multimodal
  - Baixa a mídia via URL → base64 → envia para o modelo de visão
  - Usado pelo qualificador para ler extratos de consórcio enviados como imagem/PDF

### `services/zapsign.py`
- Templates por administradora já mapeados em `TEMPLATE_BY_ADM` (dict hardcoded)
- `INTERNAL_SIGNERS` lido de `ZAPSIGN_INTERNAL_SIGNERS` no formato `Nome:email,Nome:email`
- Signatários internos configurados: Gisele (giseleexavier@hotmail.com) + Comercial (comercial@consorciosorteado.com.br)

### `services/slack.py`
- Alertas **técnicos** (erros de IA, falhas de API, jobs com falha) → Slack `#alertas-sistemas`
- Alertas **comerciais** (lead aceitou, contrato assinado) → WhatsApp via `NOTIFY_PHONES`
- `slack_error(msg, exception, context)` / `slack_warning(msg)` / `slack_info(msg)`
- Silencioso se `SLACK_WEBHOOK_URL` não configurado

---

## Qualificação de Extrato (Bazar/Site)

Lógica em `webhooks/qualificador.py`:

1. Lead em `QUALIFICATION_STAGES` = {PRIMEIRA_ATIVACAO, SEGUNDA_ATIVACAO, TERCEIRA_ATIVACAO, QUARTA_ATIVACAO}
2. Recebe mensagem → `router.py` verifica se é Bazar/Site + stage de qualificação
3. Se mídia (imagem/PDF): chama `AIClient.complete_with_image()` com prompt de extração
4. IA retorna JSON com: `administradora`, `valor_credito`, `valor_pago`, `parcelas`, `status`
5. Roteamento:
   - `QUALIFICADO` (≤50% pago, ≤R$150k) → move para `PRECIFICACAO`
   - `NAO_QUALIFICADO` → mensagem gentil de dispensa → move para `NAO_QUALIFICADO`
   - `EXTRATO_INCORRETO` → guia lead a obter extrato correto → permanece no stage
6. Detecção de recusa verbal ("vendi", "não tenho mais", etc.) → move para `PERDIDO`
7. Texto sem mídia → pede envio do extrato

Limites configuráveis via `.env`:
- `QUALIFICACAO_PERCENTUAL_MAXIMO=50`
- `QUALIFICACAO_VALOR_PAGO_MAXIMO=150000`

---

## Jobs Agendados (APScheduler)

| Job | Intervalo | Descrição |
|-----|-----------|-----------|
| `ativacao_listas` | 30 min | Ativa leads de Listas (Whapi) |
| `ativacao_bazar` | 5 min | Ativa leads do Bazar (Z-API) |
| `ativacao_site` | 5 min | Ativa leads do Site/LP (Z-API) |
| `reativador` | 1 hora | Reativa leads sem resposta |
| `follow_up` | 30 min | Follow-up em propostas pendentes |
| `contrato` | 5 min | Gera contrato ZapSign (ACEITO → ASSINATURA) |
| `precificacao` | 5 min | Envia proposta (PRECIFICACAO → NEGOCIACAO) |

Todos os jobs respeitam a janela de envio: `SEND_WINDOW_START=9` → `SEND_WINDOW_END=20`.

---

## Endpoints Disponíveis

| Método | Path | Descrição |
|--------|------|-----------|
| GET | `/health` | Health check + lista de jobs e próximos horários |
| GET | `/docs` | Swagger UI (FastAPI automático) |
| POST | `/webhook/whapi` | Entrada de mensagens Whapi (Listas) |
| POST | `/webhook/zapi` | Entrada de mensagens Z-API (Bazar/Site) |
| POST | `/webhook/zapsign` | Notificação de contrato assinado |
| GET | `/jobs/run/{job_id}?key=SECRET_KEY` | Disparo manual de job (protegido) |

---

## Variáveis de Ambiente

Todas em `.env` (já preenchido). Principais:

```
FARO_API_KEY          → CRM Supabase-backed
WHAPI_TOKEN           → Canal primário Listas (FALCON-9TE4X)
WHAPI_TOKEN_2         → Canal secundário Listas (DAREDL-F4375) — anti-ban
ZAPI_INSTANCE_*       → Instância única Z-API para todos os fluxos
OPENAI_API_KEY        → GPT-4o-mini (texto) + GPT-4o (visão extratos)
GEMINI_API_KEY        → Fallback multimodal
ZAPSIGN_TOKEN         → API de assinatura eletrônica
SLACK_WEBHOOK_URL     → Canal #alertas-sistemas
NOTIFY_PHONES         → Agentes comerciais (WhatsApp)
SECRET_KEY            → Protege endpoint /jobs/run/
```

---

## Estado Atual do Projeto

- [x] Todos os serviços implementados e testados (19 cenários em `tests/simulation.py`)
- [x] Qualificação de extrato com IA visão integrada ao fluxo de ativação
- [x] Dual-channel Whapi anti-ban implementado
- [x] Integração Slack para alertas técnicos
- [x] Todos os tokens e credenciais preenchidos no `.env`
- [x] Dockerfile + railway.toml prontos para deploy
- [ ] **Próximo:** rodar `bash setup.sh && bash run.sh` e validar `/health`
- [ ] **Próximo:** configurar webhooks no painel Whapi + Z-API com a URL pública
- [ ] **Próximo:** sanity check completo: simular lead Bazar, lead Site, lead Listas

---

## Como Rodar Localmente

```bash
# Primeira vez
bash setup.sh

# Todas as vezes
bash run.sh
# Servidor em: http://localhost:8000
# Docs:        http://localhost:8000/docs
```

---

## Decisões de Design Importantes

1. **Um único processo** — FastAPI + APScheduler no mesmo processo (sem Celery/Redis).
   Adequado para o volume atual. Se escalar, migrar jobs para worker separado.

2. **Sem banco de dados local** — todo o estado vive no FARO CRM. O servidor é stateless.

3. **Slack para técnico, WhatsApp para comercial** — separação clara de canais de alerta.

4. **Qualificação inline** — não há stage "QUALIFICACAO" separado. A qualificação acontece
   durante as ativações (PRIMEIRA→QUARTA), interceptada pelo router quando vem mídia.

5. **Z-API instância única** — o usuário confirmou que uma única instância Z-API serve
   todos os fluxos (Bazar, Site, Itaú, Santander, etc.).
