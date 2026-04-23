# Análise dos Cenários Make.com — Consórcio Sorteado

> Gerado automaticamente em 2026-04-23. Referência para migração Python.

---

## Mapa Geral (20 cenários, pasta 313456)

| ID | Nome | Status | Função |
|----|------|--------|--------|
| 4057692 | Agendador | ✅ Ativo | Scheduler: dispara subscenários via Google Sheets |
| 4087048 | Agendador 3 | ✅ Ativo | Scheduler alternativo |
| 4503650 | Agente de IA - Bazar do Consórcio Novo CRM | ✅ Ativo | Agente IA Bazar (Z-API + Claude + GPT-4o Vision) |
| 4492403 | Analisador de contexto | ✅ Ativo | Subscenário: analisa contexto da conversa (ai-agent) |
| 4487921 | Capacitor de Conversas | ✅ Ativo | Subscenário: buffer/debounce de mensagens (datastore) |
| 4536427 | Cenário de Proposta | ✅ Ativo | Subscenário: gera proposta com imagem (html-css-to-image) |
| 4501673 | [CS] NEGOCIADOR NOVO CRM | ✅ Ativo | Negociador Bazar/Site (Z-API) |
| 4574687 | [CS] NEGOCIADOR NOVO CRM_LISTAS | ✅ Ativo | Negociador Listas (Whapi) — mais completo |
| 4516018 | [CS] NEGOCIADOR NOVO CRM_TESTE_PA | ✅ Ativo | Negociador teste |
| 4495573 | Disparos de Proposta | ✅ Ativo | Disparo de proposta com imagem (Whapi + Z-API) |
| 4503354 | Dispensa de Cliente | ✅ Ativo | Envia msg de dispensa e move card (Z-API) |
| 4503296 | Fila de Ativação + Bazar | ✅ Ativo | Ativação leads Bazar (Z-API) |
| 4625425 | Fila de Ativação + Site | ✅ Ativo | Ativação leads Site/LP (Z-API) |
| 4501249 | Fluxo de Negociação | ✅ Ativo | Subscenário: lógica de negociação (Claude + ai-tools) |
| 4543585 | Follow up | ✅ Ativo | Follow-up em propostas pendentes |
| 4503187 | Migrador de Leads Bazar | ✅ Ativo | Importa leads do Google Sheets para o FARO |
| 4568772 | ZAPSIGN_CONTRATOS_SANTANDER | ✅ Ativo | Gera e envia contrato ZapSign (Santander) |
| 4640752 | Agente de Listas [FALCON] | ❌ Desativado | SDR para leads de lista fria (Whapi) |
| 4637461 | [API Não Oficial] Ativação de Leads | ❌ Desativado | Ativação via API não oficial |
| 4502947 | Reativador Novo CRM | ❌ Desativado | Reativa leads sem resposta (4 msgs sequenciais) |

---

## Cenários Desativados — Análise Detalhada

### [4640752] Agente de Listas [FALCON] ❌

**Função:** SDR conversacional para leads de listas frias via Whapi.
Recebe webhook Whapi → roteamento por estado → agente IA responde.

**Módulos principais:**
- `gateway:CustomWebHook` — entrada de mensagens
- `ai-agent:RunAnAIAgent` (3x) — agente conversacional
- `whapi-cloud:text` (9x) — envio de mensagens
- `google-sheets:addRow/filterRows/updateRow` — log/controle de estado
- `http:MakeRequest` (26x) — chamadas FARO CRM
- `builtin:BasicRouter` (11x) — roteamento por stage/condição
- `openai-gpt-3:CreateTranslation` — transcrição de áudio

**Problema que causou desativação:** Instabilidade no Make.com (timeouts, execuções duplicadas).
**Equivalente Python:** `webhooks/agente_listas.py` + `jobs/ativacao_listas.py` ✅

**Mensagens capturadas:**
- Msg pós-extrato: *"Obrigado, [nome]! Vamos avaliar o caso e te damos retorno em breve. Aproveitamos para te oferecer a participação no nosso grupo especial..."*
- Link grupo WhatsApp: `https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD`

---

### [4637461] [API Não Oficial] Ativação de Leads ❌

**Função:** Disparo de ativação para leads de lista usando API não oficial do WhatsApp.
Substituído pelo Whapi (API oficial) para evitar banimento.

**Módulos:**
- `http:MakeRequest` (2x) — chamada API não oficial
- `http:ActionSendData` (1x)
- `util:FunctionSleep` — delay anti-ban
- `ai-tools:Ask` — formatação de mensagem

**Mensagem de ativação capturada:**
> *"➡️ [NOME], identificamos em um dos grupos em que somos consorciados que você tem uma cota contemplada..."*

**Status:** Aposentado. Substituído por Whapi com rotação de tokens.

---

### [4502947] Reativador Novo CRM ❌

**Função:** Sequência de 4 mensagens para reativar leads sem resposta nos stages de ativação.
Disparado periodicamente via agendador.

**Módulos:**
- `http:MakeRequest` (22x) — FARO CRM (buscar cards, mover, atualizar)
- `http:ActionSendData` (12x) — envio via Z-API
- `ai-tools:Ask` (12x) — geração de variações de mensagem
- `builtin:BasicRouter` (10x) + `builtin:Break` (16x)
- `util:FunctionSleep` (16x) — delays entre mensagens
- `z-api:sendTextMessage` (4x)

**Sequência de mensagens (extraída):**

**MSG 1** (tom: curiosidade/urgência):
> *"Oi, [nome]! Vi que você demonstrou interesse em vender sua cota [adm] através da Bazar do Consórcio, mas ainda não conseguimos conversar! Imagino que deve estar ocupado(a), então vou resumir bem rápido: 📋 Para começar:..."*

**MSG 2** (tom: prova social):
> *"[nome], tudo bem? Só para você ter uma ideia: ontem mesmo fechamos a compra de uma cota [adm] similar à sua, e o cotista ficou muito satisfeito com o valor! 🎉 🔥 Por que as pessoas estão vendendo agora: - Mercado aquecido = melhores..."*

**MSG 3** (tom: empatia/preocupação):
> *"[nome], é a Manuela! Estou preocupada em não ter conseguido te ajudar ainda... Sei que você tem interesse real (senão não teria preenchido o formulário), então imagino que algo pode estar te impedindo de dar o próximo passo. 💭 Prin..."*

**MSG 4** (tom: despedida/última chance):
> *"[nome], uma mensagem final! Mesmo você tendo demonstrado interesse inicial em vender sua cota [adm], entendo que às vezes o timing não é o ideal. Não tem problema algum! 😊 🤝 Meu compromisso com você: - Seu contato..."*

**Equivalente Python:** `jobs/reativador.py` ✅ (implementado mas desativado no Make)

---

## Mensagens de Ativação Ativas (Bazar/Site)

### Fila de Ativação + Bazar [4503296]
> *"Olá [nome], tudo bem? Sou a Manuela, da Consórcio Sorteado, empresa referência na compra de cotas contempladas.*
> *Recebemos seu interesse através da Bazar do Consórcio e temos interesse real na sua cota [adm] 😁*
> *O processo é simples:*
> *1️⃣ Você envia o extrato atualizado por aqui mesmo*
> *2️⃣ Nossa equipe técnica faz a análise*
> *3️⃣ Em até 24h úteis, enviamos uma proposta com base nas melhores condições do mercado*"*

### Fila de Ativação + Site [4625425]
Idêntica à Bazar, com "nosso site" no lugar de "Bazar do Consórcio".

---

## Fluxo de Proposta (Negociador Listas — [4574687])

Lógica identificada no cenário mais completo:
1. Recebe mensagem → Capacitor de Conversas (debounce)
2. Agente IA analisa intenção
3. Se aceite → coleta dados pessoais para contrato
4. Formata proposta removendo apresentações desnecessárias
5. Gera link ZapSign → envia ao lead
6. Mensagem pós-link: *"Segue o link para assinatura → [link]. Para assinar é bem simples e leva menos de 2 minutos: 1️⃣ Clique no link acima..."*

---

## Observações Técnicas

1. **Google Sheets como estado temporário:** Usado extensivamente no Make para rastrear estado entre execuções. No Python, isso foi eliminado — todo estado vai direto pro FARO CRM. ✅
2. **Datastore Make:** Usado para debounce/capacitor. No Python: `webhooks/debounce.py` com asyncio. ✅
3. **html-css-to-image:** Geração de imagem para proposta. No Python: `services/html_image.py`. ✅
4. **Z-API → Whapi:** Migração concluída no Python. Make ainda usa Z-API nos cenários ativos.
5. **Token sk_live_REDACTED:** API key presente nos blueprints do Make. Verificar se ainda é válida/necessária ou se já foi rotacionada.
6. **Persona "Manuela":** Nome da agente IA usada em todas as mensagens. Manter consistência no Python.

---

## Pendências de Migração

- [ ] Verificar se `jobs/reativador.py` implementa as 4 mensagens com os tons corretos (curiosidade → prova social → empatia → despedida)
- [ ] Confirmar se mensagens de ativação Bazar/Site no Python estão alinhadas com as do Make
- [ ] Validar link do grupo WhatsApp: `https://chat.whatsapp.com/KwcE6QJHa33Bq0eHH9L9qD` — ainda ativo?
- [ ] Rever `services/html_image.py` — equivalente ao html-css-to-image do Make
