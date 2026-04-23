# Comparação Make.com vs Python — Consórcio Sorteado

> Análise lado a lado de cada função crítica.
> Gerado em 2026-04-23.

---

## 1. Ativação de Leads de Listas

### Make.com ([4637461] + [4640752])
- **Mecanismo:** HTTP para API não oficial → depois migrou para Whapi
- **Mensagem:**
  > "➡️ [NOME], identificamos em um dos grupos em que somos consorciados que você tem uma cota contemplada..."
- **Problema:** API não oficial causava banimentos → desativado

### Python (`jobs/ativacao_listas.py`)
- **Mecanismo:** Whapi com pool de 5 tokens em rotação aleatória + delay 30-90s entre disparos
- **Mensagem:**
  > "⚡️ {nome}, identificamos em um dos grupos em que somos consorciados que você tem uma cota contemplada {adm}! [...] você teria interesse em receber uma proposta personalizada pela sua cota, sem compromisso?"
- **Diferencial:** Mensagem com **botões interativos** (Whapi) — aumenta taxa de resposta
- **Fallback:** Se endpoint de botões retornar 404 → envia texto simples automaticamente

### ✅ STATUS: Alinhado — Python é superior (botões + anti-ban estruturado)

---

## 2. Ativação de Leads Bazar/Site

### Make.com ([4503296] + [4625425])
- **Mecanismo:** Z-API (instância única)
- **Mensagem Bazar:**
  > "Olá [nome], tudo bem? Sou a Manuela, da Consórcio Sorteado [...] Recebemos seu interesse através da Bazar do Consórcio e temos interesse real na sua cota [adm] 😁 [...] 1️⃣ Você envia o extrato atualizado por aqui mesmo [...]"
- **Mensagem Site:** Idêntica, com "nosso site" no lugar de "Bazar do Consórcio"

### Python (`jobs/ativacao_bazar_site.py`)
- **Mecanismo:** Whapi canal "bazar" (substituiu Z-API)
- **Mensagem Bazar:**
  > "Olá {nome}, tudo bem? Sou a Manuela, da Consórcio Sorteado [...] Recebemos seu interesse através da Bazar do Consórcio e temos interesse real na sua cota {adm}! 😁 [...] 1️⃣ Você envia o extrato atualizado da cota [...]"

### ✅ STATUS: Alinhado — conteúdo praticamente idêntico, migração Z-API→Whapi transparente
### ⚠️ DIFERENÇA MENOR: Make usava Z-API; Python usa Whapi canal bazar. Confirmar que `WHAPI_TOKEN_BAZAR` está configurado no `.env`.

---

## 3. Reativador

### Make.com ([4502947]) — DESATIVADO
Sequência de 4 mensagens com progressão de tom:

| # | Tom | Trecho da mensagem |
|---|-----|--------------------|
| 1 | Curiosidade/urgência | "Oi, [nome]! Vi que você demonstrou interesse em vender sua cota [adm]..." |
| 2 | Prova social | "[nome], tudo bem? Ontem mesmo fechamos a compra de uma cota [adm] similar..." |
| 3 | Empatia | "[nome], é a Manuela! Estou preocupada em não ter conseguido te ajudar ainda..." |
| 4 | Despedida | "[nome], uma mensagem final! Entendo que o momento pode não ser ideal..." |

### Python (`jobs/reativador.py`)
Duas sequências separadas: **Listas** (com botões) e **Bazar** (texto simples):

**BAZAR — Progressão:**
| # | Stage | Tom | Trecho |
|---|-------|-----|--------|
| 1 | PRIMEIRA_ATIVACAO | Curiosidade | "Oi, {nome}! Vi que você demonstrou interesse em vender sua cota {adm}..." |
| 2 | SEGUNDA_ATIVACAO | Prova social | "{nome}, tudo bem? Ontem mesmo fechamos a compra de uma cota {adm} similar..." |
| 3 | TERCEIRA_ATIVACAO | Empatia | "{nome}, é a Manuela! Estou preocupada em não ter conseguido te ajudar ainda..." |
| 4 | QUARTA_ATIVACAO | Despedida | "{nome}, uma mensagem final! Entendo que o momento pode não ser ideal..." |

**LISTAS — Progressão:**
| # | Stage | Tom | Trecho |
|---|-------|-----|--------|
| 1 | PRIMEIRA_ATIVACAO | Reflexão | "Sei que você pode estar pensando sobre nossa proposta para a sua cota {adm}..." |
| 2 | SEGUNDA_ATIVACAO | Prova social | "Esta semana ajudamos 3 pessoas a vender suas cotas contempladas..." |
| 3 | TERCEIRA_ATIVACAO | Urgência | "Não quero ser insistente, {nome}, mas o mercado... está realmente aquecido agora!" |
| 4 | QUARTA_ATIVACAO | Despedida + grupo | "Entendo que a venda da sua cota {adm} não faz sentido agora..." + link grupo |

### ✅ STATUS: **Completamente alinhado**
- Bazar: 1:1 com Make.com — mesmos tons e progressão
- Listas: variação adequada para o canal (botões + tom mais reflexivo para leads frios)
- Link do grupo WhatsApp presente: `https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t`

---

## 4. Agente SDR Bazar (Qualificação / Conversa)

### Make.com ([4503650] Agente de IA - Bazar)
- Claude + GPT-4o Vision + Z-API
- Datastore para debounce
- Google Sheets para log
- `make-ai-extractors` para PDF

### Python (`webhooks/agente_bazar.py` + `webhooks/qualificador.py`)
- Claude/GPT-4o via `AIClient` — mesmo modelo
- `webhooks/debounce.py` — substituiu Datastore Make
- FARO CRM para log — substituiu Google Sheets
- `AIClient.complete_with_image()` com download base64 — substituiu make-ai-extractors

### ✅ STATUS: Alinhado — Python é mais robusto (sem limite de operações Make)

---

## 5. Agente SDR Listas (Conversa)

### Make.com ([4640752] Agente de Listas [FALCON]) — DESATIVADO
- Whapi Cloud (`whapi-cloud:text`) — 9 envios
- `ai-agent:RunAnAIAgent` (3x) + Claude
- Google Sheets para controle de estado
- Roteamento por intent (interesse, recusa, redirecionamento)
- Link do grupo WhatsApp em recusas

### Python (`webhooks/agente_listas.py`)
- Whapi canal "lista" ✅
- Claude via `AIClient.complete_with_history()` ✅
- FARO CRM para estado (histórico serializado no campo `Historico Conversa`) ✅
- Intents: `INTERESSE | RECUSA_COTA_VENDIDA | RECUSA_SEM_INTERESSE | REDIRECIONAR | OUTRO` ✅
- Link grupo em recusas ✅
- **Extra Python:** Roteamento por administradora (Itaú → Sônia, outras → Manuela)

### ✅ STATUS: Alinhado — Python tem feature extra de roteamento por adm

---

## 6. Negociador

### Make.com ([4574687] [CS] NEGOCIADOR NOVO CRM_LISTAS)
- Claude + Anthropic (15x) para geração de resposta
- Subscenário "Cenário de Proposta" para gerar imagem da proposta
- ZapSign para contrato
- Sequência: aceite → coleta dados → contrato → link assinatura

### Python (`webhooks/negociador.py` + `jobs/precificacao.py` + `jobs/contrato.py`)
- `AIClient` para geração de resposta ✅
- `services/html_image.py` para imagem de proposta ✅
- `services/zapsign.py` para contrato ✅

### ✅ STATUS: Alinhado estruturalmente — não lido em detalhe (fora do escopo atual)

---

## 7. Follow-up

### Make.com ([4543585] Follow up)
- `ai-tools:Ask` (1x) — gera mensagem de follow-up
- `http:ActionSendData` — envia via Z-API
- Simples: 1 módulo IA, 1 módulo envio

### Python (`jobs/follow_up.py`)
- `AIClient.complete()` com prompt rico (histórico, jornada, situação da negociação)
- 8 tentativas máximas com estados: `MELHORAR_VALOR | CONTRA_PROPOSTA | OFERECERAM_MAIS | RECUSAR | etc.`
- Move para PERDIDO automaticamente ao esgotar tentativas
- Notifica equipe via WhatsApp ao esgotar
- **Extra:** Follow-up de ASSINATURA parada (leads sem ZapSign Token há 3+ dias)

### ✅ STATUS: Python é significativamente mais completo que o Make

---

## 8. ZapSign / Contratos

### Make.com ([4568772] ZAPSIGN_CONTRATOS_SANTANDER)
- 14x `http:ActionSendData` — chama API ZapSign
- 8x `builtin:BasicRouter` — lógica de template por adm
- Apenas Santander mapeado explicitamente

### Python (`services/zapsign.py` + `jobs/contrato.py`)
- Templates por administradora em `TEMPLATE_BY_ADM` (dict)
- Múltiplas adms suportadas
- Signatários internos configuráveis via `.env`

### ✅ STATUS: Python mais completo (multi-adm vs Santander-only no Make)

---

## Resumo Executivo

| Função | Make.com | Python | Status |
|--------|----------|--------|--------|
| Ativação Listas | ❌ Desativado (API não oficial) | ✅ Whapi + botões + anti-ban | ✅ Python superior |
| Ativação Bazar | ✅ Z-API | ✅ Whapi bazar | ✅ Alinhado |
| Ativação Site | ✅ Z-API | ✅ Whapi bazar | ✅ Alinhado |
| Reativador Bazar | ❌ Desativado | ✅ 4 msgs progressivas (idêntico) | ✅ Alinhado |
| Reativador Listas | ❌ Desativado | ✅ 4 msgs progressivas (adaptado) | ✅ Alinhado |
| Agente SDR Listas | ❌ Desativado | ✅ Claude + intents + histórico | ✅ Python superior |
| Agente SDR Bazar | ✅ Ativo | ✅ Claude + qualificador | ✅ Alinhado |
| Qualificador extrato | ✅ Ativo | ✅ GPT-4o Vision | ✅ Alinhado |
| Negociador | ✅ Ativo | ✅ Implementado | ✅ Alinhado |
| Follow-up | ✅ Ativo | ✅ 8 tentativas + IA rica | ✅ Python superior |
| Contratos ZapSign | ✅ Ativo (Santander) | ✅ Multi-adm | ✅ Python superior |
| Debounce/Capacitor | ✅ Datastore Make | ✅ asyncio debounce | ✅ Alinhado |
| Estado/Histórico | ✅ Google Sheets | ✅ FARO CRM (stateless) | ✅ Python superior |

---

## Pendências / Riscos

### 🔴 Crítico
1. **`TEST_MODE=true` por padrão** no `config.py` — confirmar que o `.env` tem `TEST_MODE=false`

### 🟡 Importante
2. **`WHAPI_TOKEN_BAZAR` não configurado?** — sem esse token, o canal bazar usa fallback do pool lista (mesma instância para dois fluxos), quebrando o isolamento anti-ban
3. **Chave vazada no Make.com** — `sk_live_REDACTED` hardcoded nos blueprints. Verificar se é a mesma do `.env` e rotacionar
4. **Webhooks não configurados** — Whapi precisa ter a URL `POST /webhook/whapi` configurada no painel para receber mensagens

### 🟢 Observação
5. **Link do grupo WhatsApp** `https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD?mode=gi_t` — presente nos dois sistemas (Make e Python). Verificar se ainda está ativo
6. **`FLUXO_CADENCIA`** — stage para onde vai após QUARTA_ATIVACAO. Não há job Python para esse stage. Verificar se é intencional (cadência manual?)
