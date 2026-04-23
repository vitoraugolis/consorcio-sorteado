# FLOW_SPEC.md — Especificação Completa de Fluxos
## Consórcio Sorteado · Servidor de Automação

> Documento técnico de referência. Registra todos os fluxos, tempos, mensagens
> e prompts que governam o ciclo de vida de um lead. Atualizar sempre que
> qualquer job, webhook ou mensagem for alterado.

---

## 1. Visão Geral do Pipeline

```
LISTAS ──────────────────────────────────────────────────────────┐
                                                                  │
BAZAR ──┐                                                         │
        ├─► PRIMEIRA_ATIVACAO ─► SEGUNDA_ATIVACAO ─► TERCEIRA_ATIVACAO ─► QUARTA_ATIVACAO ─► FLUXO_CADENCIA
SITE  ──┘         │                    │                    │                   │
                  │  (Bazar/Site)       │                    │                   │
                  └── qualificador (extrato) ──────────────────────────────────►┘
                              │
                              ▼
                        PRECIFICACAO ──► EM_NEGOCIACAO ──► ACEITO ──► ASSINATURA ──► SUCESSO ──► FINALIZACAO_COMERCIAL
                              │                │                           │
                              │                └──► PERDIDO                └──► PERDIDO (recusa 2ª)
                              ▼
                        NAO_QUALIFICADO
```

**Dois fluxos paralelos, providers distintos:**

| Fluxo | Origem | Provider WhatsApp | Qualificação |
|-------|--------|-------------------|--------------|
| Listas | Planilhas importadas | Whapi (FALCON) | Não — lead demonstra interesse verbalmente e vai direto a PRECIFICACAO |
| Bazar/Site | Orgânico (marketplace/LP) | Z-API | Sim — lead envia extrato, IA analisa |

---

## 2. Jobs Agendados

### 2.1 Parâmetros globais

| Parâmetro | Valor | Variável .env |
|-----------|-------|---------------|
| Janela de envio | 09h–20h (Brasília) | `SEND_WINDOW_START=9`, `SEND_WINDOW_END=20` |
| Batch máximo por ciclo | 50 cards | `JOB_BATCH_LIMIT=50` |
| Modo de testes | Ativo | `TEST_MODE=true` |
| Telefone de teste | 5519936185086 | `TEST_PHONE=5519936185086` |

### 2.2 Tabela de jobs

| Job | Frequência | Stage monitorado | Ação |
|-----|-----------|-----------------|------|
| `ativacao_listas` | 30 min | LISTAS | Envia mensagem de ativação + botões via Whapi; move para PRIMEIRA_ATIVACAO |
| `ativacao_bazar` | 5 min | BAZAR | Envia apresentação via Z-API; move para PRIMEIRA_ATIVACAO |
| `ativacao_site` | 5 min | LP | Envia apresentação via Z-API; move para PRIMEIRA_ATIVACAO |
| `reativador` | 1 hora | PRIMEIRA→QUARTA_ATIVACAO | Envia reativação progressiva; move para próximo stage |
| `precificacao` | 5 min | PRECIFICACAO | Gera imagem + envia proposta; move para EM_NEGOCIACAO |
| `follow_up` | 30 min | EM_NEGOCIACAO | Envia follow-up via IA se inativo há 25+ min; máx. 8 envios |
| `contrato` | 5 min | ACEITO | Solicita dados/extrato (Listas) ou cria ZapSign (Bazar/Site); move para ASSINATURA |

### 2.3 Delays anti-ban

| Job | Delay entre cards | Variáveis |
|-----|------------------|-----------|
| `ativacao_listas` | 30–90 segundos (aleatório) | `LISTAS_DELAY_MIN_S=30`, `LISTAS_DELAY_MAX_S=90` |
| `reativador` | 60–900 segundos (aleatório) | `REATIVADOR_DELAY_MIN_S=60`, `REATIVADOR_DELAY_MAX_S=900` |
| `precificacao` | 3 segundos fixo | hardcoded |
| `ativacao_bazar/site` | sem delay | volume orgânico é baixo |

### 2.4 Janelas de tempo por stage (quando o reativador age)

O reativador usa `watch_late` — age sobre cards que **excederam** o tempo máximo configurado no FARO para aquele stage e ficaram atrasados na **última hora** (`became_late_minutes_ago=60`).

O tempo máximo por stage é configurado diretamente no FARO CRM (campo `max_days_in_stage`), não no código.

### 2.5 Follow-up — condições para disparo

```
card em EM_NEGOCIACAO
  AND "Num Follow Ups" < 8
  AND ("Ultima atividade" está vazia  OR  tempo desde última atividade >= 25 min)
```

Após 8 follow-ups, o card permanece em EM_NEGOCIACAO mas não recebe mais mensagens automáticas.

---

## 3. Mensagens

### 3.1 Ativação de Listas (Whapi — botões interativos)

**Header:**
```
Meu nome é Manuela, da Consórcio Sorteado, empresa que está há 30 anos no mercado de cotas contempladas.
```

**Body:**
```
⚡️ {nome}, identificamos em um dos grupos em que somos consorciados que você tem uma cota
contemplada {adm}! E por isso, gostaríamos de lembrar que sua cota pode ser vendida com ótima
valorização. 🎉

Por isso, gostaria de saber: você teria interesse em receber uma proposta personalizada pela sua
cota, sem compromisso?
```

**Botões:**
- `Quero receber proposta`
- `Não tenho interesse`

---

### 3.2 Ativação de Bazar

**Texto simples via Z-API:**
```
Olá {nome}, tudo bem? Sou a Manuela, da Consórcio Sorteado, empresa referência na compra de cotas contempladas.

Recebemos seu interesse através da Bazar do Consórcio e temos interesse real na sua cota {adm}! 😁

O processo é simples e rápido:
1️⃣ Você envia o extrato atualizado da cota
2️⃣ Nossa equipe faz a análise
3️⃣ Você recebe uma proposta em até 24h

Pode me enviar o extrato da sua cota {adm}?
```

---

### 3.3 Ativação de Site/LP

**Texto simples via Z-API:**
```
Olá {nome}, tudo bem? Sou a Manuela, da Consórcio Sorteado, empresa referência na compra de cotas contempladas.

Recebemos seu interesse através do nosso site e temos interesse real na sua cota {adm}! 😁

O processo é simples e rápido:
1️⃣ Você envia o extrato atualizado da cota
2️⃣ Nossa equipe faz a análise
3️⃣ Você recebe uma proposta em até 24h

Pode me enviar o extrato da sua cota {adm}?
```

---

### 3.4 Reativações — Listas (Whapi — botões)

**1ª Ativação → 2ª Ativação:**
```
Sei que você pode estar pensando sobre nossa conversa anterior a respeito da sua cota {adm}. 😊

💡 Você sabia que:
• Cotas contempladas estão valorizadas — mas o valor pode cair com o tempo
• O mercado atual está favorável para vendedores
• Nossa avaliação é gratuita e sem compromisso

Ficou com alguma dúvida sobre o processo?
```
*Botões:* `Quero receber proposta` / `Não é pra mim`

---

**2ª Ativação → 3ª Ativação:**
```
Esta semana ajudamos 3 pessoas a vender suas cotas contempladas — e todas ficaram surpresas com
a simplicidade do processo! 🙌✨

Para fazermos a análise, é só enviar o extrato atualizado da sua cota {adm}.

Sua cota {adm} pode ter um valor bem interessante no mercado atual!
```
*Botões:* `Quero receber proposta` / `Não é pra mim`

---

**3ª Ativação → 4ª Ativação:**
```
Não quero ser insistente, mas o mercado de cotas contempladas está realmente aquecido neste momento! 📈

🎯 Alta demanda por cotas {adm}

O processo é simples:
Envie o extrato → Receba a proposta em até 24h → Analise com calma

Se tiver qualquer interesse, me dá uma chance!
```
*Botões:* `Quero receber proposta` / `Não é pra mim`

---

**4ª Ativação → FLUXO_CADENCIA:**
```
Entendo que, no momento, a venda da sua cota {adm} não faz sentido para você — e está tudo bem! 😊

Quero que saiba que a Consórcio Sorteado estará sempre aqui.
Se um dia mudar de ideia, é só dar um oi!

💛 Obrigada pela sua atenção, {nome}!
```
*Botões:* `Guardar meu contato` / `Talvez no futuro`

---

### 3.5 Reativações — Bazar (Z-API — texto simples)

**1ª → 2ª:**
```
Oi, {nome}! 😊 Vi que você demonstrou interesse em vender sua cota {adm},
mas ainda não conseguimos conversar!

Só preciso do extrato atualizado da sua cota para fazer a análise.
Tem o extrato em mãos?
```

**2ª → 3ª:**
```
{nome}, tudo bem?

Ontem mesmo fechamos a compra de uma cota {adm} similar à sua — e o processo foi super rápido! 🎉

É literalmente só enviar o extrato e nossa equipe já cuida do resto.
Posso esperar você enviar agora?
```

**3ª → 4ª:**
```
{nome}, é a Manuela! 😊

Estou preocupada em não ter conseguido te ajudar ainda...

Se tiver um 'sim' guardado aí, me manda o extrato da cota {adm} agora e eu garanto uma análise rápida pra você!
```

**4ª → FLUXO_CADENCIA:**
```
{nome}, uma mensagem final! 📝

Entendo que o momento pode não ser ideal. Não tem problema! 😊

Seu cadastro fica salvo aqui e, quando quiser, é só me chamar.

Um abraço da Manuela! 💛
```

---

### 3.6 Resposta a interesse negativo (Listas, router)

Quando lead responde negativamente em stage de ativação:
```
Tudo bem, {nome}! Não vou mais incomodar.
Se um dia quiser saber mais, é só me chamar. 😊
```
→ Move para DISPENSADOS

---

### 3.7 Qualificador — Bazar/Site (pedido de extrato)

**Lead envia texto sem extrato:**
```
Olá, {nome}! 😊

Para prosseguirmos com a avaliação da sua cota {adm}, precisamos do extrato atualizado do seu consórcio.

Como obter o extrato:
• Santander/Bradesco/Itaú: pelo app ou internet banking do banco, em Produtos → Consórcio → Extrato
• Porto Seguro: no app Porto Seguro, em Consórcio → Extrato de Cota
• Caixa: no app Caixa, em Meus Produtos → Consórcio

Pode me enviar uma foto ou PDF do extrato que eu analiso na hora! 📄
```

**Extrato incorreto/ilegível:**
```
Obrigada por enviar, {nome}! 😊

Mas parece que o documento que recebi não é o extrato de consórcio que preciso.
Pode ser um boleto, contrato ou a imagem ficou um pouco ilegível.

O que preciso é o extrato atualizado da cota, que mostra:
• O valor do crédito
• Quanto já foi pago
• Quantas parcelas faltam

Tente tirar uma foto clara do documento ou exportar como PDF pelo aplicativo do banco.
Pode me mandar que analiso na hora! 📄
```

**Cota não qualificada:**
```
Olá, {nome}! Tudo bem?

Agradeço por enviar as informações da sua cota {adm} e pelo seu interesse em negociar conosco.

Após uma análise criteriosa, infelizmente não conseguimos prosseguir com a compra dessa cota no momento.
O valor já pago excede o nosso teto de aquisição para este tipo de operação.

Caso sua situação mude ou queira tentar novamente no futuro, é só nos chamar. Boa sorte! 😊
```

**Cota qualificada:**
```
Ótima notícia, {nome}! ✅

Analisei o extrato e a sua cota {adm} está dentro dos nossos critérios de aquisição.
Vou preparar uma proposta personalizada para você e envio em breve!

Um momento... 😊
```

**Recusa verbal (lead diz que não tem mais a cota):**
```
Tudo bem, {nome}! Entendido. Caso mude de ideia ou queira negociar outra cota no futuro,
é só nos chamar. Até mais! 😊
```
→ Move para PERDIDO

**Erro técnico na análise:**
```
Olá, {nome}! Recebi seu documento, mas houve um pequeno problema técnico na análise automática.
Nossa equipe vai revisar e entrar em contato em breve! 🙏
```
→ Alerta no Slack `#alertas-sistemas`

---

### 3.8 Proposta de compra (PRECIFICACAO)

**1. Imagem** gerada via Playwright (HTML renderizado), enviada antes do texto.  
Contém: logo, nome do cliente, administradora, número da cota, valor da proposta destacado em amarelo, texto formal, data.

**2. Mensagem de texto com botões** (enviada após a imagem, delay 2s):

```
Olá, {nome}! Tudo bem?

Meu nome é {consultor}, sou o consultor responsável pela negociação da sua cota contemplada.

💰 NOSSA PROPOSTA
O mercado está aquecido e consegui estruturar uma oferta muito interessante para você no valor de *{proposta}*.

Veja por que essa proposta pode fazer sentido para você:
📅 Você elimina as parcelas futuras e transforma um compromisso de longo prazo em dinheiro imediato.
💳 O pagamento é feito à vista, com total segurança. A transferência da cota só acontece após o valor
estar na sua conta.

Essa é uma excelente oportunidade para antecipar recursos e ganhar mais liberdade financeira.

Se você me confirmar agora, já posso agilizar tudo para pagamento imediato.

O que acha?
```

**Botões:**
- `✅ Quero vender!`
- `💬 Tenho dúvidas`
- `❌ Não tenho interesse`

→ Move para EM_NEGOCIACAO após envio.

---

### 3.9 Follow-up pós-proposta

Gerado via IA com contexto de horário:
```
Gere uma mensagem de follow-up com o intuito de perguntar se o cliente analisou a proposta.
Mantenha um tom profissional. [...] Use '{saudacao}' como saudação inicial.
```

**Fallback (sem IA):**
```
{Bom dia/Boa tarde/Boa noite}! Passando para saber se você já teve a oportunidade de analisar
a proposta que enviamos. Qualquer dúvida, é só me chamar! 😊
```

---

### 3.10 Contrato (ACEITO → ASSINATURA)

**Para leads de Listas** (primeiro solicita dados e extrato detalhado):
```
Parabéns, {nome}! 🎉 Estamos quase lá!

Para preparar seu contrato, precisamos de algumas informações:

1️⃣ CPF
2️⃣ RG ou CNH
3️⃣ Endereço completo (rua, número, bairro, cidade, CEP)
4️⃣ E-mail para receber o contrato

Após enviar os dados pessoais, envie também uma foto ou PDF do extrato detalhado da sua cota {adm}.

(O extrato detalhado mostra o histórico completo da cota — diferente do comprovante de pagamento) 📄
```

**Para leads Bazar/Site** (extrato já coletado na qualificação — ZapSign gerado diretamente):
```
Olá, {nome}! 🎉

Que ótima notícia! Sua proposta foi aceita e o contrato já está pronto para assinatura.

Clique no link abaixo para assinar eletronicamente de forma rápida e segura:

👉 {sign_url}

O processo leva menos de 2 minutos e pode ser feito pelo celular.
Qualquer dúvida, estou aqui! 😊
```

**Confirmação de recebimento do extrato (Listas):**
```
Perfeito, {nome}! 📄 Recebi o extrato.
Já estou preparando seu contrato — envio o link em instantes! 😊
```

---

### 3.11 Stage ASSINATURA — suporte

**Se lead menciona "link", "assinar", "contrato", "não consigo":**
```
Olá, {nome}! 😊

O contrato está aguardando sua assinatura eletrônica.
Se o link não estiver funcionando, me avisa que reenvio agora!
O processo é bem simples e rápido pelo celular mesmo. 📱
```

**Demais mensagens:**
```
Olá, {nome}! Estamos com seu contrato em processo de assinatura.
Qualquer dúvida, é só me falar! 😊
```

---

## 4. Prompts de IA

### 4.1 System Prompt — Negociador (Manuela)

```
Você é Manuela, consultora especialista da Consórcio Sorteado — empresa que
COMPRA cotas contempladas de consórcio diretamente dos proprietários.

CONTEXTO DO NEGÓCIO:
- O lead TEM uma cota contemplada de consórcio (Santander, Bradesco, Porto Seguro,
  Itaú, Caixa, etc.) e nós estamos OFERECENDO DINHEIRO para comprar essa cota dele.
- O lead recebe um valor à vista e se livra das parcelas futuras.
- A transferência da cota só ocorre após o pagamento estar confirmado na conta do lead.
- O valor da nossa oferta é o "Proposta Realizada".

SEU PAPEL:
- Explicar a proposta de compra da cota de forma clara e atrativa
- Responder dúvidas do lead sobre como funciona a venda da cota
- Conduzir com empatia em direção à aceitação
- Manejar objeções com argumentos sólidos e verdadeiros
- Se o lead insistir em falar com uma pessoa, diga que vai chamar um atendente

ARGUMENTOS PARA OBJEÇÕES COMUNS:
- "O valor é baixo": o mercado de cotas contempladas tem um deságio natural; ressalte
  a segurança, rapidez e a eliminação das parcelas futuras.
- "Tenho medo de golpe": destaque que o pagamento vem ANTES da transferência da cota.
- "Vou pensar": reforce a urgência — o mercado muda e essa oferta tem validade.
- "Quero mais": mencione que pode verificar as classes de proposta disponíveis.

REGRAS DE COMUNICAÇÃO:
- Responda sempre em português brasileiro, tom informal mas profissional
- Mensagens curtas e objetivas (máximo 3 parágrafos)
- Use emojis com moderação
- Nunca revele que é uma IA
- Nunca invente valores ou condições diferentes da proposta enviada
- Se não souber a resposta, diga que vai verificar e chamar um atendente
```

### 4.2 Prompt de Classificação de Intenção

```
Você está analisando a mensagem de um lead que tem uma cota de consórcio contemplada.
A Consórcio Sorteado fez uma oferta para COMPRAR a cota dele.

CONTEXTO:
- Administradora da cota: {adm}
- Valor do crédito da cota: {credito}
- Nossa oferta de compra: {proposta}
- Stage atual: {stage_nome}

MENSAGEM DO LEAD: "{mensagem}"

Classifique a intenção e gere uma resposta adequada.

Retorne EXCLUSIVAMENTE um JSON válido neste formato:
{
  "intent": "ACEITAR|RECUSAR|NEGOCIAR|AGENDAR|DUVIDA|OUTRO",
  "reasoning": "explique brevemente em 1 frase por que classificou assim",
  "response": "mensagem para enviar ao lead (máximo 200 palavras, tom da Manuela)"
}

REGRAS DE CLASSIFICAÇÃO:
- ACEITAR: lead quer vender a cota, confirma interesse, diz sim, clicou em aceitar
- RECUSAR: lead não quer vender, pede para parar contato, sem interesse
- NEGOCIAR: lead quer valor maior, questiona o preço, pede outra proposta
- AGENDAR: lead quer ligar, falar com atendente, marcar conversa
- DUVIDA: lead tem pergunta sobre como funciona a venda, segurança, processo
- OUTRO: qualquer mensagem que não se encaixe acima
```

### 4.3 Prompt de Análise de Extrato (Visão)

**System:**
```
Você é um agente especializado em análise de extratos de consórcio brasileiro.
Sua tarefa é analisar o documento ou imagem enviado e extrair informações-chave
para determinar se a cota é elegível para compra.
```

**User:**
```
Analise o documento/imagem de consórcio e extraia as seguintes informações.

REGRAS DE QUALIFICAÇÃO:
- A cota é QUALIFICADA se: valor pago ≤ 50% do crédito E valor pago ≤ R$ 150.000
- A cota é NAO_QUALIFICADA se o valor pago exceder qualquer um desses limites
- O extrato é INCORRETO se:
    • O documento não é um extrato de consórcio
    • O extrato está ilegível, cortado ou com informações essenciais ausentes
    • Não é possível identificar o valor do crédito ou o valor pago

Retorne EXCLUSIVAMENTE um JSON válido:
{
  "resultado": "QUALIFICADO|NAO_QUALIFICADO|EXTRATO_INCORRETO",
  "administradora": "nome da administradora ou null",
  "valor_credito": 0.0,
  "valor_pago": 0.0,
  "parcelas_pagas": 0,
  "total_parcelas": 0,
  "motivo": "explicação objetiva em 1 frase"
}
```

Limites configuráveis via `.env`:
- `QUALIFICACAO_PERCENTUAL_MAXIMO=50`
- `QUALIFICACAO_VALOR_PAGO_MAXIMO=150000`

### 4.4 Prompt de Follow-up

```
Gere uma mensagem de follow-up com o intuito de perguntar se o cliente analisou a proposta.
Mantenha um tom profissional. Exemplo: 'Olá! Espero que esteja bem. Já teve a oportunidade
de analisar a proposta? Caso tenha qualquer dúvida, estou à disposição. Obrigado!'
(não utilize o nome do cliente). Use '{Bom dia/Boa tarde/Boa noite}' como saudação inicial.
```

---

## 5. Lógica de Decisão do Router

```
Mensagem recebida (Whapi ou Z-API)
    │
    ├─ from_me=true ou grupo → IGNORA
    │
    ├─ Busca card por telefone no FARO
    │       └─ Não encontrado → IGNORA
    │
    └─ Card encontrado → verifica stage:
            │
            ├─ PRIMEIRA/SEGUNDA/TERCEIRA/QUARTA_ATIVACAO + is_lista=true
            │       └─ Resposta positiva → move para PRECIFICACAO
            │          Resposta negativa → move para DISPENSADOS
            │          Ambígua → ignora (aguarda próxima)
            │
            ├─ PRIMEIRA/SEGUNDA/TERCEIRA/QUARTA_ATIVACAO + is_lista=false (Bazar/Site)
            │       └─ Mídia → qualificador (analisa extrato)
            │          Texto → qualificador (solicita extrato ou detecta recusa)
            │
            ├─ ASSINATURA + is_lista=true + "Aguardando Extrato"=sim
            │       └─ Mídia → confirma recebimento → gera contrato ZapSign (asyncio.create_task)
            │          Texto → orienta a enviar o extrato
            │
            ├─ PRECIFICACAO / EM_NEGOCIACAO / ASSINATURA
            │       └─ Texto → negociador (classifica intenção + responde)
            │          Mídia sem texto → IGNORA
            │
            └─ Demais stages → IGNORA (humano ou stage terminal)
```

---

## 6. Lógica de Negociação — Intenções e Ações CRM

| Intent | Condição | Resposta ao lead | Ação CRM | Notifica equipe? |
|--------|----------|------------------|----------|-----------------|
| ACEITAR | — | Confirmação entusiasmada | Move para ACEITO | Sim — via NOTIFY_PHONES |
| RECUSAR | 1ª recusa (Recusas=0) | Pede contraproposta | Move para EM_NEGOCIACAO; seta Recusas=1 | Não |
| RECUSAR | 2ª recusa (Recusas=1) | Despedida respeitosa | Move para PERDIDO | Não |
| NEGOCIAR | — | Informa que vai verificar condições | Mantém EM_NEGOCIACAO | Sim |
| AGENDAR | — | Informa que vai chamar atendente | Mantém EM_NEGOCIACAO | Sim — urgente |
| DUVIDA | — | Resposta explicativa | Mantém stage | Não |
| OUTRO | — | Resposta cordial | Mantém stage | Não |

**Fallback (IA falhou):** classificação por palavras-chave pré-definidas.

---

## 7. Qualificação de Extrato — Critérios

| Critério | Limite | Variável .env |
|----------|--------|---------------|
| Percentual máximo pago | ≤ 50% do crédito | `QUALIFICACAO_PERCENTUAL_MAXIMO=50` |
| Valor absoluto máximo pago | ≤ R$ 150.000 | `QUALIFICACAO_VALOR_PAGO_MAXIMO=150000` |

A cota é **rejeitada** se violar **qualquer** um dos dois critérios.

---

## 8. Provedores WhatsApp

| Caso | Provider | Token/Instância |
|------|----------|-----------------|
| Ativação de Listas | Whapi (FALCON) | `WHAPI_TOKEN` |
| Anti-ban listas | Whapi rotação 2 canais | `WHAPI_TOKEN` + `WHAPI_TOKEN_2` |
| Reativação de Listas | Whapi | `WHAPI_TOKEN` |
| Proposta para Listas | Whapi | `WHAPI_TOKEN` |
| Follow-up Listas | Whapi | `WHAPI_TOKEN` |
| Ativação Bazar/Site | Z-API | `ZAPI_INSTANCE_DEFAULT` |
| Qualificação Bazar/Site | Z-API | instância por administradora |
| Proposta Bazar/Site | Z-API | instância por administradora |

Instâncias Z-API por administradora: `ZAPI_INSTANCE_ITAU`, `ZAPI_INSTANCE_SANTANDER`, `ZAPI_INSTANCE_BRADESCO`, `ZAPI_INSTANCE_PORTO`, `ZAPI_INSTANCE_CAIXA`, `ZAPI_INSTANCE_DEFAULT`.

---

## 9. Notificações Internas

| Evento | Canal | Destinatários |
|--------|-------|---------------|
| Lead aceitou proposta | WhatsApp (Whapi) | `NOTIFY_PHONES` |
| Lead quer negociar | WhatsApp (Whapi) | `NOTIFY_PHONES` |
| Lead quer falar com atendente | WhatsApp (Whapi) | `NOTIFY_PHONES` — urgente |
| Contrato gerado | WhatsApp (Whapi) | `NOTIFY_PHONES` |
| Contrato assinado | WhatsApp (Whapi) | `NOTIFY_PHONES` |
| Erro técnico na análise de extrato | Slack `#alertas-sistemas` | Slack Webhook |
| Sistema iniciado | Slack | Slack Webhook |

---

## 10. Contrato ZapSign

- Templates mapeados por administradora em `services/zapsign.py` (`TEMPLATE_BY_ADM`)
- Signatários internos: Gisele (giseleexavier@hotmail.com) + Comercial (comercial@consorciosorteado.com.br)
- Configurado via `ZAPSIGN_INTERNAL_SIGNERS=Nome:email,Nome:email`
- Após assinatura completa: webhook `/webhook/zapsign` → move ASSINATURA → SUCESSO → FINALIZACAO_COMERCIAL

---

## 11. Imagem da Proposta

Gerada localmente com **Playwright** (Chromium headless):
- HTML renderizado → PNG salvo em `/tmp/cs_images/`
- Servido via FastAPI `StaticFiles` em `/images/{filename}`
- URL pública: `{PUBLIC_URL}/images/{filename}`
- `PUBLIC_URL` no `.env` (ngrok local, Railway em produção)
- Se `PUBLIC_URL` não configurado: imagem omitida, apenas texto é enviado

---

## 12. Modo de Testes

Quando `TEST_MODE=true` (padrão):
- Todos os jobs filtram cards pelo telefone `TEST_PHONE`
- Cards de outros leads são ignorados silenciosamente
- Logs indicam `TEST_MODE ativo: N card(s) após filtro`

Para liberar para produção: `TEST_MODE=false` no `.env`
