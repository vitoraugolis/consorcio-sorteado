# Deploy — Consórcio Sorteado no Railway

## Pré-requisitos

- Conta no [Railway](https://railway.app) (login com GitHub recomendado)
- [Railway CLI](https://docs.railway.app/guides/cli) instalado: `npm install -g @railway/cli`
- Repositório GitHub com o código (ou upload direto via CLI)

---

## Passo 1 — Criar repositório GitHub

No terminal, dentro da pasta `consorcio-sorteado/`:

```bash
git init
git add .
git commit -m "feat: sistema inicial Consórcio Sorteado"
git remote add origin https://github.com/SEU_USUARIO/consorcio-sorteado.git
git push -u origin main
```

> ⚠️ O `.gitignore` já exclui o `.env`. As variáveis são configuradas diretamente no Railway.

---

## Passo 2 — Criar projeto no Railway

1. Acesse [railway.app/new](https://railway.app/new)
2. Clique em **"Deploy from GitHub repo"**
3. Selecione o repositório `consorcio-sorteado`
4. Railway detecta o `Dockerfile` automaticamente e inicia o build

---

## Passo 3 — Configurar variáveis de ambiente

No painel do Railway → seu projeto → aba **Variables** → clique em **"Raw Editor"** e cole:

```env
FARO_API_KEY=sua_chave_faro
WHAPI_TOKEN=seu_token_whapi_1
WHAPI_TOKEN_2=seu_token_whapi_2
WHAPI_BASE_URL=https://gate.whapi.cloud
ZAPI_INSTANCE_BAZAR=SUA_INSTANCIA_ZAPI:SEU_TOKEN_ZAPI
ZAPI_INSTANCE_SITE=SUA_INSTANCIA_ZAPI:SEU_TOKEN_ZAPI
ZAPI_INSTANCE_ITAU=SUA_INSTANCIA_ZAPI:SEU_TOKEN_ZAPI
ZAPI_INSTANCE_SANTANDER=SUA_INSTANCIA_ZAPI:SEU_TOKEN_ZAPI
ZAPI_INSTANCE_BRADESCO=SUA_INSTANCIA_ZAPI:SEU_TOKEN_ZAPI
ZAPI_INSTANCE_PORTO=SUA_INSTANCIA_ZAPI:SEU_TOKEN_ZAPI
ZAPI_INSTANCE_CAIXA=SUA_INSTANCIA_ZAPI:SEU_TOKEN_ZAPI
ZAPI_INSTANCE_DEFAULT=SUA_INSTANCIA_ZAPI:SEU_TOKEN_ZAPI
OPENAI_API_KEY=sua_chave_openai
GEMINI_API_KEY=sua_chave_gemini
DEFAULT_AI_MODEL=gpt-4o-mini
DEFAULT_VISION_MODEL=gpt-4o
QUALIFICACAO_PERCENTUAL_MAXIMO=50
QUALIFICACAO_VALOR_PAGO_MAXIMO=150000
ZAPSIGN_TOKEN=seu_token_zapsign
ZAPSIGN_INTERNAL_SIGNERS=Nome:email@exemplo.com,Nome2:email2@exemplo.com
NOTIFY_PHONES=5511999999999,5511888888888
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/SEU/WEBHOOK/URL
LISTAS_DELAY_MIN_S=30
LISTAS_DELAY_MAX_S=90
REATIVADOR_DELAY_MIN_S=60
REATIVADOR_DELAY_MAX_S=900
JOB_BATCH_LIMIT=50
SEND_WINDOW_START=9
SEND_WINDOW_END=20
SECRET_KEY=gere_uma_chave_segura_aqui
```

Clique em **"Update Variables"** — o Railway reinicia o container automaticamente.

---

## Passo 4 — Obter a URL pública

Railway gera uma URL no formato:
```
https://consorcio-sorteado-production.up.railway.app
```

Anote esta URL — ela será usada nos webhooks do Whapi, Z-API e ZapSign.

Teste o health check:
```bash
curl https://SUA_URL.railway.app/health
```

Deve retornar `{"status": "ok", "jobs": [...]}`.

---

## Passo 5 — Confirmar Slack

Ao subir, o sistema envia automaticamente para `#alertas-sistemas`:
> ℹ️ **Sistema Consórcio Sorteado iniciado** — Jobs ativos: 7 | Ambiente: Produção

Se a mensagem aparecer, tudo está funcionando.

---

## Passo 6 — Configurar webhooks externos

### Whapi (fluxo Listas)
- Painel: [app.whapi.cloud](https://app.whapi.cloud) → canal → **Webhook URL**
- URL: `https://SUA_URL.railway.app/webhook/whapi`
- Eventos: `message`

### Z-API (fluxo Bazar/Site)
- Painel: [app.z-api.io](https://app.z-api.io) → instância → **Webhooks**
- URL de recebimento: `https://SUA_URL.railway.app/webhook/zapi`

### ZapSign (contratos)
- Painel ZapSign → Conta → **Webhooks**
- URL: `https://SUA_URL.railway.app/webhook/zapsign`
- Evento: `document_signed`

---

## Disparar jobs manualmente (para testes)

```bash
# Testa o reativador
curl "https://SUA_URL.railway.app/jobs/run/reativador?key=428139a70d1528608ceb87a498f2a5cffffcb209222b8ec4d6f982e161cc4950"

# Testa ativação Bazar
curl "https://SUA_URL.railway.app/jobs/run/ativacao_bazar?key=428139a70d1528608ceb87a498f2a5cffffcb209222b8ec4d6f982e161cc4950"

# Jobs disponíveis: reativador, ativacao_listas, ativacao_bazar, ativacao_site,
#                   follow_up, contrato, precificacao
```

---

## Logs em tempo real

```bash
# Via Railway CLI
railway logs --follow
```

Ou pelo painel: projeto → **Deployments** → clique no deploy ativo → **View Logs**.
