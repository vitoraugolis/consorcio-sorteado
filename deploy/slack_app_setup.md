# Criando o Slack App para o Guardião

## 1. Criar o App

1. Acesse https://api.slack.com/apps → **Create New App** → **From Scratch**
2. Nome: `Guardião CS` | Workspace: selecione o seu
3. Clique **Create App**

## 2. Ativar Socket Mode

1. Menu esquerdo: **Settings → Socket Mode**
2. Toggle **Enable Socket Mode** → ON
3. Em **App-Level Tokens** → **Generate a token**
   - Token Name: `guardiao-socket`
   - Scope: `connections:write`
4. Clique **Generate** → copie o token `xapp-...`
5. Salve no `.env` como `SLACK_APP_TOKEN=xapp-...`

## 3. Configurar Permissões do Bot

1. Menu esquerdo: **Features → OAuth & Permissions**
2. Em **Bot Token Scopes** → **Add an OAuth Scope** — adicione:
   - `chat:write`
   - `im:history`
   - `im:read`
   - `channels:history`
   - `app_mentions:read`
   - `channels:read`

## 4. Assinar Eventos

1. Menu esquerdo: **Features → Event Subscriptions**
2. Toggle **Enable Events** → ON
3. Em **Subscribe to bot events** → **Add Bot User Event**:
   - `app_mention`
   - `message.im`
4. Clique **Save Changes**

## 5. Instalar no Workspace

1. Menu esquerdo: **Settings → Install App**
2. Clique **Install to Workspace** → **Allow**
3. Copie o **Bot User OAuth Token** (`xoxb-...`)
4. Salve no `.env` como `SLACK_BOT_TOKEN=xoxb-...`

## 6. Obter o Channel ID

1. No Slack, vá ao canal onde quer receber os alertas (ex: #alertas-sistemas)
2. Clique no nome do canal → **About** → role até **Channel ID** (formato: `C0XXXXXXXXX`)
3. Salve no `.env` como `SLACK_GUARDIAN_CHANNEL=C0XXXXXXXXX`

## 7. Convidar o Bot ao Canal

No Slack, no canal escolhido, digite:
```
/invite @Guardião CS
```

## 8. Testar

Com o guardião rodando (`sudo systemctl start guardiao`),
mande uma DM para o bot ou mencione no canal:
```
@Guardião CS status
```

Ele deve responder com o status atual do sistema.
