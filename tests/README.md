# Suite de Testes — Consórcio Sorteado

Testes de integração e unitários sem dependências externas (Whapi, FARO e IA mockados).

## Como rodar

```bash
cd /home/ubuntu/.openclaw/workspace/consorcio-sorteado
source .venv/bin/activate
pip install pytest pytest-asyncio
pytest tests/ -v
```

Para rodar uma suite específica:
```bash
pytest tests/test_negociador.py -v
pytest tests/test_edge_cases.py -v
```

Para rodar com relatório de cobertura:
```bash
pip install pytest-cov
pytest tests/ --cov=webhooks --cov=jobs --cov=services --cov-report=term-missing
```

## Arquivos

| Arquivo | O que testa | Testes |
|---------|-------------|--------|
| `test_extract_value.py` | Extração de valores monetários (`_extract_lead_value`) | ~17 |
| `test_negociador.py` | Lógica de negociação (sem IO) | ~20 |
| `test_agente_contrato.py` | Coleta de dados para contrato | ~8 |
| `test_router.py` | Roteamento de mensagens | ~7 |
| `test_edge_cases.py` | Casos extremos e helpers do FARO | ~22 |

**Total: ~74 testes**

## Suites

### test_extract_value.py
- Formatos: `31k`, `50K`, `320 mil`, `320mil`, `R$ 320.000`, `320.000`, `1.200.000`
- Âncora contextual: `"320"` com `proposta_atual=200000` → `320000`
- Casos sem valor: string vazia, só emoji, texto sem número

### test_negociador.py
- `_get_next_proposal`: escalada normal, salto para máximo (<27%), sem sequência, teto atingido
- `_build_result`: todos os 10 intents
- ACEITAR condicional com valor → reclassifica para CONTRA_PROPOSTA
- CONTRA_PROPOSTA: dentro da sequência, absurda (>40%), fora do alcance
- OFERECERAM_MAIS: sem valor (mantém), com `31k` (reclassifica)
- `_classify_with_ai`: mockando IA com resposta JSON válida e inválida

### test_agente_contrato.py
- Extração de CPF, RG, endereço, email com IA mockada
- Fallback regex quando IA falha (CPF + email)
- Email malformado (sem @) é ignorado
- CPF sem formatação é aceito
- Dados fragmentados em múltiplas mensagens (acumula corretamente)
- Extrato antes dos dados → pede dados primeiro
- Extrato com dados completos → dispara geração de contrato

### test_router.py
- Mensagem própria (from_me=True) → ignorada
- Mensagem de grupo → ignorada
- Card não encontrado → silencioso
- Lista em ACTIVATION_STAGES → agente_listas
- Sem Fonte em ACTIVATION_STAGES → agente_listas (nova regra)
- Bazar em QUALIFICATION_STAGES → agente_bazar
- ASSINATURA sem ZapSign Token → agente_contrato

### test_edge_cases.py
- Card sem crédito, sem sequência, sequência com lixo → sem crash
- Mensagem vazia, só emoji → sem crash
- `is_lista()` com todos os casos possíveis
- `get_name/phone/adm` com campos ausentes
- Nova regra do router (Fonte vazia = lista)
- `load_history` com JSON inválido → lista vazia
- `history_append` preserva ordem
