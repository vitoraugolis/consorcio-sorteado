# Guia de Subida de Listas — Consórcio Sorteado

Este documento explica como preparar e subir listas de leads para o sistema de automação.
Seguir este guia evita erros de processamento, mensagens incorretas e falhas na geração de contrato.

---

## Campos obrigatórios

Cada linha da lista deve conter os seguintes campos:

| Campo | Obrigatório | Exemplo |
|---|---|---|
| `Nome do contato` | ✅ Sim | João da Silva |
| `Telefone` | ✅ Sim | 5511999990001 |
| `Adm` | ✅ Sim | Porto Seguro |
| `Crédito` | ✅ Sim | 230000 |
| `Tipo Pessoa` | ✅ Sim | `PF` ou `PJ` |
| `Tipo de bem` | Recomendado | Imóvel / Veículo |
| `CPF` | Recomendado | 123.456.789-00 |
| `CNPJ` | Se PJ | 12.345.678/0001-90 |
| `Grupo` | Opcional | 00123 |
| `Cota` | Opcional | 0042 |
| `Parcelas pagas` | Opcional | 24 |
| `Telefone alternativo` | Opcional | 5511888880001 |

---

## Campo `Tipo Pessoa` — atenção especial

Este campo é **obrigatório** e define qual conjunto de dados será pedido ao lead no momento da coleta para o contrato.

| Valor | Quando usar | Dados solicitados ao lead |
|---|---|---|
| `PF` | Cota em nome de pessoa física (CPF) | Nome, CPF, RG, Endereço, CEP, Profissão, Estado Civil, E-mail, Dados bancários (PIX/conta em nome do CPF) |
| `PJ` | Cota em nome de pessoa jurídica (CNPJ) | Nome empresa, CNPJ, Nome sócio, CPF sócio, RG sócio, Endereço, CEP, Profissão, Estado Civil, E-mail, Dados bancários (PIX/conta em nome do CNPJ) |

> ⚠️ **Se o campo `Tipo Pessoa` estiver vazio**, o sistema assume `PF` por padrão. Isso pode causar coleta incorreta de dados para leads PJ.

---

## Formato do telefone

O telefone deve estar no formato internacional **sem `+`** e **sem espaços ou traços**:

```
5511999990001   ✅ correto
+55 11 99999-0001   ❌ incorreto
11999990001   ❌ incorreto (falta DDI)
```

- DDI Brasil: `55`
- DDD + número: 10 ou 11 dígitos
- Total: **12 ou 13 dígitos**

---

## Formato do crédito

O valor do crédito deve ser um número **sem pontos ou vírgulas** de milhar, sem `R$`:

```
230000   ✅ correto
230.000   ❌ incorreto
R$ 230.000,00   ❌ incorreto
```

---

## Administradoras suportadas (Bazar)

O sistema aceita cotas das seguintes administradoras no fluxo Bazar:

- Porto Seguro
- Itaú
- Santander
- Bradesco
- Caixa
- RODOBENS
- Embracon

> Leads de outras administradoras são movidos para **Não Qualificado** automaticamente.

---

## Como subir a lista no FARO

1. Acesse o painel do FARO
2. Vá em **Pipeline → Listas**
3. Clique em **Importar leads** (ou **Upload CSV**)
4. Mapeie as colunas conforme os campos acima
5. Confirme a importação

Após a importação, o sistema ativa automaticamente 1 lead a cada 30 minutos dentro da janela de envio (09h–20h horário de Brasília).

---

## Erros comuns e como evitar

| Erro | Causa | Solução |
|---|---|---|
| Lead não recebe mensagem | Telefone em formato incorreto | Usar formato `55DDDNUMERO` |
| Contrato solicita dados errados | `Tipo Pessoa` vazio ou incorreto | Preencher `PF` ou `PJ` na planilha |
| Lead movido para Não Qualificado | Adm fora da lista suportada | Verificar lista de adms aceitas |
| Mensagem duplicada | Mesmo telefone em múltiplas linhas | Remover duplicatas antes de subir |
| Lead ativado fora do horário | — | Sistema aguarda janela 09h–20h automaticamente |

---

## Fluxo após ativação

```
Lead subido em LISTAS
  → Mensagem com botões enviada (09h–20h BRT)
  → Lead clica "Quero receber proposta"
  → Agente SDR responde
  → Lead envia extrato
  → Sistema analisa e calcula proposta
  → Proposta enviada automaticamente
  → Negociação com IA
  → Lead aceita → Coleta de dados → Contrato ZapSign → ✅ SUCESSO
```

---

*Dúvidas: entre em contato com a equipe técnica da Guará Marketing.*
