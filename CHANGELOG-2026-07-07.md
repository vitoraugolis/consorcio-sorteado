# 📦 Changelog — 2026-07-07

> Sessão de desenvolvimento: Zeca (IA) + Vitor Augusto  
> Branch: `main` | Repositório: `vitoraugolis/consorcio-sorteado`

---

## 🔀 Mudanças implementadas

### 1. Pool de tokens Whapi — 5 canais unificados para Listas

**Arquivos:** `.env`

Os canais `DEADPL-V592K` (ex-LP) e `DAREDL-F4375` (ex-Bazar) foram migrados para o pool de rotação de Listas, centralizando todos os disparos em um único pool de 5 tokens com round-robin e anti-ban.

| Variável | Canal | Antes | Depois |
|----------|-------|-------|--------|
| `WHAPI_TOKEN_LISTA_1` | `FALCON-9TE4X` | Listas | Listas |
| `WHAPI_TOKEN_LISTA_2` | `GROOTT-PUH8G` | Listas | Listas |
| `WHAPI_TOKEN_LISTA_3` | `WOLVRN-WEMHU` | Listas | Listas |
| `WHAPI_TOKEN_LISTA_4` | `DEADPL-V592K` | LP (dedicado) | **Listas (pool)** |
| `WHAPI_TOKEN_LISTA_5` | `DAREDL-F4375` | Bazar (dedicado) | **Listas (pool)** |
| `WHAPI_TOKEN_BAZAR` | — | `DAREDL-F4375` | *(vazio)* |
| `WHAPI_TOKEN_LP` | — | `DEADPL-V592K` | *(vazio)* |

---

### 2. Cadência garantida — 130 a 150 disparos/dia

**Arquivo:** `.env`  
**Variável:** `FILA_LISTAS_TOKEN_GAP_S`

| Parâmetro | Antes | Depois |
|-----------|-------|--------|
| `FILA_LISTAS_TOKEN_GAP_S` | `900s` (15 min) | `720s` (12 min) |

**Matemática da cadência:**
- Janela de envio: 08h–20h BRT = 12 horas = 720 minutos
- Ciclo do job: 1 disparo a cada 5 min = **144 ciclos/dia**
- Capacidade do pool: 5 tokens × (720 min ÷ 12 min) = 300 disparos/dia de capacidade
- Limitador real: ciclo de 5 min → **~144 disparos/dia** ✅ dentro da meta de 130–150

---

### 3. Fallback silencioso para pools Bazar e LP

**Arquivo:** `services/whapi.py`  
**Funções:** `_build_bazar_pool()`, `_build_lp_pool()`

Com os tokens dedicados de Bazar e LP removidos, as funções foram atualizadas para usar o pool de Listas como fallback silencioso (sem warnings desnecessários no startup):

```python
# Antes: warning fatal se LP não configurado
def _build_lp_pool():
    if WHAPI_LP_TOKEN:
        return [WHAPI_LP_TOKEN]
    logger.warning("WHAPI_TOKEN_LP não configurado — leads LP sem canal!")
    return []

# Depois: fallback para Listas
def _build_lp_pool():
    if WHAPI_LP_TOKEN:
        return [WHAPI_LP_TOKEN]
    if WHAPI_LISTA_TOKENS:
        logger.info("WHAPI_TOKEN_LP não configurado — usando pool de Listas como fallback.")
        return WHAPI_LISTA_TOKENS
    return []
```

---

### 4. `notify_team()` — ordem de canais corrigida

**Arquivo:** `services/whapi.py`

O `DEADPL-V592K` (número participante do grupo Alarmes Sistemas CS) foi migrado para o pool de Listas. A função `notify_team()` foi atualizada para refletir isso:

```python
# Antes: tentava lp → bazar → lista (LP agora vazio, falhava de cara)
for _canal_grupo in ("lp", "bazar", "lista"):

# Depois: tenta lista primeiro (DEADPL está como LISTA_4)
for _canal_grupo in ("lista", "bazar"):
```

---

### 5. Fluxo de interesse — proposta automática imediata

**Arquivo:** `webhooks/agente_listas.py`  
**Função:** `_handle_intent()`

Quando o lead clica em "Quero receber proposta" (intent `INTERESSE`), o sistema agora dispara a proposta imediatamente via `create_task`, sem aguardar preenchimento manual no FARO:

```python
# Antes: movia para PRECIFICACAO e parava (aguardava proposta manual)
await faro.move_card(card_id, Stage.PRECIFICACAO)
# → fim. Proposta dependia de ação humana no FARO.

# Depois: dispara proposta automática como task assíncrona
await faro.move_card(card_id, Stage.PRECIFICACAO)
asyncio.create_task(send_proposal_now(card))
# → proposta calculada + enviada + SDR + notificação de grupo, tudo automático
```

---

### 6. `send_proposal_now()` — sem guard de janela horária

**Arquivo:** `jobs/precificacao.py`

A função de disparo reativo não deve ser bloqueada pela janela de horário — o lead acabou de responder e espera a proposta agora:

```python
# Antes: guard de janela bloqueava disparo reativo
async def send_proposal_now(card):
    if not _is_within_send_window():
        return  # bloqueava silenciosamente

# Depois: sem guard — disparo sempre imediato
async def send_proposal_now(card):
    # sem verificação de janela
    async with FaroClient() as faro:
        fresh = await faro.get_card(card["id"])
        await _process_card(faro, fresh)
```

---

### 7. Mensagem SDR após proposta

**Arquivo:** `jobs/precificacao.py`

Após o envio bem-sucedido da proposta, o sistema envia automaticamente uma mensagem ao lead informando que um consultor entrará em contato:

```
"Um de nossos consultores entrará em contato em breve para esclarecer 
qualquer dúvida e acompanhar a negociação. 😊"
```

---

### 8. Notificação no grupo Alarmes Sistemas CS após proposta

**Arquivo:** `jobs/precificacao.py`

Após o envio bem-sucedido, o grupo `NOTIFY_GROUP` recebe uma notificação completa:

```
🎯 *Nova proposta enviada!*

👤 *Nome:* {nome}
📱 *Telefone:* {telefone}
🏦 *Administradora:* {adm}
💰 *Proposta:* R$ {proposta}
📊 *Crédito:* R$ {credito}
🔀 *Canal:* Listas
🆔 *Card:* `{card_id[:8]}`
```

---

## 🔄 Fluxo completo pós-implementação

```
Lead clica "Quero receber proposta"
    │
    ▼
agente_listas._handle_intent(INTERESSE)
    ├── move card → PRECIFICACAO (FARO)
    └── asyncio.create_task(send_proposal_now(card))
            │
            ▼
        precificacao._process_card_locked()
            ├── calcula proposta (cluster A/B/C por ADM)
            ├── gera imagem HTML via Playwright
            ├── envia imagem + texto da proposta (Whapi)
            ├── envia mensagem SDR ("consultor entrará em contato")
            ├── move card → EM_NEGOCIACAO (FARO)
            ├── grava histórico + jornada
            └── notify_team() → grupo Alarmes Sistemas CS
```

---

## 🧪 Bateria de Testes — `tests/test_implementacao_2026_07_07.py`

27 testes criados especificamente para cobrir todas as mudanças desta sessão.

### Resultado final

```
============================= 27 passed in 15.34s ==============================
```

**Total da suíte completa (excluindo test_contemplacao_guard que usa IO real):**

```
134 passed, 1 failed (pré-existente), 1 warning
```

---

### Cobertura dos novos testes

| Classe | Testes | O que valida |
|--------|--------|-------------|
| `TestPoolTokensListas` | 5 | Pool tem 5 tokens; DEADPL e DAREDL presentes; BAZAR/LP vazios |
| `TestPoolFallbacks` | 4 | Bazar/LP sem token → usa Listas; sem fallback → vazio |
| `TestNotifyTeamOrdemCanais` | 3 | `lista` tentado primeiro; fallback `bazar`; `lp` nunca tentado |
| `TestTokenGapCadencia` | 3 | Gap=720s lido do env; cadência 130–150/dia; 12 min = 720s |
| `TestAgentListasInteresse` | 3 | INTERESSE cria `create_task`; move para PRECIFICACAO; RECUSA não dispara |
| `TestPrecificacaoSDRNotify` | 4 | Mensagem SDR enviada; `notify_team` chamado com dados; não chamado em falha |
| `TestSendProposalNow` | 2 | Dispara dentro e fora da janela horária |
| `TestCadenciaMath` | 3 | 144 ciclos/dia; capacidade pool ≥ 130; gap = 12 min |

---

### Falha pré-existente (não relacionada a esta sessão)

```
FAILED tests/test_router.py::TestRouteMessage::test_assinatura_sem_zapsign_vai_para_agente_contrato
```

**Causa:** teste desatualizado — assume que `agente_contrato` usa `debounce.schedule`, mas o router foi refatorado em sessão anterior e não usa mais debounce para o stage `ASSINATURA`. Confirmado via `git stash` que a falha existia antes das mudanças de hoje.

**Impacto em produção:** zero — o roteamento de `ASSINATURA` está em `SILENCE_STAGES` (agentes comerciais humanos assumem).

---

## 📁 Arquivos modificados

| Arquivo | Tipo de mudança |
|---------|----------------|
| `.env` | `WHAPI_TOKEN_LISTA_4/5`, `WHAPI_CHANNEL_ID_LISTA_4/5`, `FILA_LISTAS_TOKEN_GAP_S=720` |
| `services/whapi.py` | `_build_bazar_pool()`, `_build_lp_pool()`, `notify_team()` |
| `webhooks/agente_listas.py` | `_handle_intent()` — INTERESSE dispara `send_proposal_now` |
| `jobs/precificacao.py` | mensagem SDR, `notify_team`, `send_proposal_now` sem guard |
| `tests/test_implementacao_2026_07_07.py` | ✨ novo — 27 testes |
