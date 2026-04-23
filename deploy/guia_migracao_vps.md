# Guia de Migração para VPS

> **VPS Windows?** Pule para a seção [Windows](#windows).
> Para Linux/Ubuntu, siga o guia completo abaixo.

## Visão Geral

O setup completo tem duas fases:
1. **No seu Mac** — preparar o código e transferir para o VPS
2. **No VPS** — instalar dependências, configurar serviços, testar

---

## Fase 1 — Preparação no Mac

### 1.1 Completar o `.env` com as variáveis do VPS

Abra o `.env` e adicione as variáveis novas (que ainda não existem):

```bash
# Variáveis que precisam ser adicionadas ao .env atual
TEST_MODE=false
PUBLIC_URL=http://SEU_IP_VPS        # ex: http://203.0.113.42
PORT=8000
IMAGES_DIR=/opt/consorcio-sorteado/images

# Guardião (Slack)
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_GUARDIAN_CHANNEL=C0XXXXXXXXX

# Configuração do guardião
APP_URL=http://127.0.0.1:8000
SERVICE_NAME=consorcio-sorteado
GUARDIAN_CHECK_INTERVAL=120
GUARDIAN_MAX_RESTARTS=3
```

> **Atenção:** Substitua `SEU_IP_VPS` pelo IP público real do seu VPS.
> Para o Slack, siga os passos em `deploy/slack_app_setup.md` antes de continuar.

---

## Fase 2 — Transferência dos Arquivos

### 2.1 Criar usuário e diretório no VPS (uma vez só)

```bash
# Conecte no VPS
ssh usuario@SEU_IP_VPS

# Crie o diretório de destino
sudo mkdir -p /opt/consorcio-sorteado
sudo chown $USER:$USER /opt/consorcio-sorteado

# Saia do VPS
exit
```

### 2.2 Transferir o código via `rsync`

```bash
# No seu Mac, dentro da pasta do projeto:
cd /Users/vitoraugolis/Documents/CS/consorcio-sorteado

rsync -avz \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='logs/' \
  --exclude='tests/__pycache__' \
  ./ usuario@SEU_IP_VPS:/opt/consorcio-sorteado/
```

> O `.env` é excluído propositalmente — ele tem os tokens reais e não deve sair da sua máquina.

### 2.3 Transferir o `.env` separadamente

```bash
scp .env usuario@SEU_IP_VPS:/opt/consorcio-sorteado/.env
```

> Não use `git` para isso — o `.env` nunca deve ir para um repositório.

---

## Fase 3 — Instalação no VPS

### 3.1 Conectar e rodar o script de setup

```bash
ssh usuario@SEU_IP_VPS

cd /opt/consorcio-sorteado
chmod +x deploy/setup_vps.sh
bash deploy/setup_vps.sh
```

O script faz automaticamente:
- Cria virtualenv Python 3.12
- Instala `requirements.txt`
- Instala `slack-bolt` e `anthropic` (Guardião)
- Instala Playwright + Chromium com todas as libs do sistema
- Cria `/opt/consorcio-sorteado/images` (diretório persistente de imagens)
- Configura nginx como proxy reverso na porta 80
- Registra e habilita os dois serviços systemd (`consorcio-sorteado` e `guardiao`)
- Configura sudoers para o guardião poder reiniciar o app sem senha

> **Se Playwright falhar:** rode manualmente:
> ```bash
> sudo apt-get install -y libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
>   libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
>   libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2
> /opt/consorcio-sorteado/.venv/bin/playwright install chromium
> ```

---

## Fase 4 — Iniciar os Serviços

```bash
# Iniciar a aplicação principal
sudo systemctl start consorcio-sorteado
sudo systemctl status consorcio-sorteado

# Iniciar o guardião
sudo systemctl start guardiao
sudo systemctl status guardiao
```

### Verificar que está funcionando

```bash
# Health check via nginx (porta 80)
curl http://localhost/health

# Health check direto no uvicorn
curl http://127.0.0.1:8000/health

# Logs em tempo real
journalctl -u consorcio-sorteado -f
journalctl -u guardiao -f
```

A resposta de `/health` deve ser algo como:
```json
{"status": "ok", "jobs": [...]}
```

---

## Fase 5 — Configurar Webhooks no Whapi e Z-API

As plataformas de WhatsApp precisam saber para onde enviar as mensagens recebidas.

### Whapi (leads de Listas)
- Painel: painel do Whapi → Webhooks
- URL: `http://SEU_IP_VPS/webhook/whapi`
- Evento: `messages`

### Z-API (leads Bazar/Site)
- Painel: painel Z-API → Webhooks → Mensagens Recebidas
- URL: `http://SEU_IP_VPS/webhook/zapi`

### ZapSign (contratos assinados)
- Painel: painel ZapSign → Configurações → Webhook
- URL: `http://SEU_IP_VPS/webhook/zapsign`

---

## Fase 6 — Teste End-to-End

### 6.1 Testar o health check externo

```bash
# Do seu Mac:
curl http://SEU_IP_VPS/health
```

### 6.2 Testar o guardião no Slack

No Slack, mande uma DM para o bot ou mencione no canal configurado:
```
@Guardião CS status
```

Deve responder com o status atual dos serviços.

### 6.3 Testar um job manualmente

```bash
curl "http://SEU_IP_VPS/jobs/run/ativacao_listas?key=SUA_SECRET_KEY"
```

---

## Fluxo de Atualização de Código (futuro)

Quando quiser atualizar o código após mudanças:

```bash
# No Mac:
rsync -avz \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.env' --exclude='logs/' \
  ./ usuario@SEU_IP_VPS:/opt/consorcio-sorteado/

# No VPS:
sudo systemctl restart consorcio-sorteado
```

O guardião vai notar o restart e confirmar no Slack que o serviço voltou.

---

## Troubleshooting Rápido

| Sintoma | Causa provável | Solução |
|---------|---------------|---------|
| `curl /health` timeout | nginx não está rodando | `sudo systemctl status nginx` → `sudo systemctl start nginx` |
| Serviço não sobe | Variável faltando no `.env` | `journalctl -u consorcio-sorteado -n 50` |
| Imagem de proposta não abre | Playwright/Chromium não instalado | `playwright install chromium --with-deps` |
| Guardião não responde | `SLACK_APP_TOKEN` ou `SLACK_BOT_TOKEN` incorreto | Verificar tokens no painel Slack |
| Job não dispara | Fora da janela de envio (9h–20h) | Normal — aguardar horário ou testar via `/jobs/run/` |

---

<a name="windows"></a>
## Windows — Diferenças e Passos Específicos

### O que muda em relação ao Linux

| Item | Linux | Windows |
|------|-------|---------|
| Gerenciador de serviços | systemd (`.service`) | **NSSM** (instala serviços Windows) |
| Logs | journalctl | Arquivo de texto (`LOG_FILE` no `.env`) |
| Disco/memória | `df`, `free` | PowerShell automático |
| Script de setup | `setup_vps.sh` | `setup_vps_windows.ps1` |
| Playwright | `--with-deps` obrigatório | sem `--with-deps` (Windows já tem as libs) |
| Transferência | `rsync` | `robocopy` ou WinSCP/SCP |
| `IMAGES_DIR` | `/opt/consorcio-sorteado/images` | `C:\consorcio-sorteado\images` |

### Pré-requisitos no Windows

1. **Python 3.12** — baixar em python.org, marcar "Add to PATH"
2. **nginx para Windows** — extrair em `C:\nginx` (nginx.org/en/download.html)
3. **NSSM** — extrair em `C:\nssm` (nssm.cc/download)
4. **WinSCP** ou OpenSSH — para transferir arquivos do Mac

### Transferência dos arquivos (do Mac)

```bash
# Via SCP (se o Windows tiver OpenSSH Server habilitado):
scp -r . usuario@SEU_IP_VPS:C:\consorcio-sorteado\

# Ou use o WinSCP com interface gráfica
```

### Setup no Windows (PowerShell como Administrador)

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
cd C:\consorcio-sorteado
.\deploy\setup_vps_windows.ps1
```

### Variáveis extras no `.env` para Windows

```env
IMAGES_DIR=C:\consorcio-sorteado\images
LOG_FILE=C:\consorcio-sorteado\logs\app.log
```

### Iniciar e verificar os serviços

```powershell
sc start consorcio-sorteado
sc start guardiao

# Status
sc query consorcio-sorteado
sc query guardiao

# Logs em tempo real
Get-Content C:\consorcio-sorteado\logs\app.log -Wait -Tail 50
```

### Iniciar o nginx

```powershell
C:\nginx\nginx.exe
# Para recarregar config:
C:\nginx\nginx.exe -s reload
# Para parar:
C:\nginx\nginx.exe -s stop
```

### Troubleshooting Windows

| Sintoma | Solução |
|---------|---------|
| Serviço não sobe | Ver `C:\consorcio-sorteado\logs\app.log` |
| Guardião sem logs | Confirmar `LOG_FILE` no `.env` |
| `sc start` "Acesso negado" | Abrir PowerShell como Administrador |
| Playwright falha | Rodar `.venv\Scripts\playwright.exe install chromium` manualmente |
| nginx 404 no `/images/` | Confirmar `IMAGES_DIR=C:\consorcio-sorteado\images` no `.env` |
