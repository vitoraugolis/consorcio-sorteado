# deploy/setup_vps_windows.ps1 — Configura o VPS Windows do Consórcio Sorteado
# Execute em um terminal PowerShell como Administrador:
#   Set-ExecutionPolicy Bypass -Scope Process -Force
#   .\deploy\setup_vps_windows.ps1

$ErrorActionPreference = "Stop"

$APP_DIR      = "C:\consorcio-sorteado"
$VENV         = "$APP_DIR\.venv"
$PYTHON       = "python"        # ajuste para "py -3.12" se necessário
$SERVICE_MAIN = "consorcio-sorteado"
$SERVICE_GUAR = "guardiao"
$NSSM         = "C:\nssm\nssm.exe"   # baixar em https://nssm.cc/download

function Step($msg) { Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "✓ $msg" -ForegroundColor Green }

Write-Host ""
Write-Host "╔══════════════════════════════════════════════╗"
Write-Host "║  Setup VPS Windows — Consórcio Sorteado      ║"
Write-Host "╚══════════════════════════════════════════════╝"
Write-Host ""

# ── 1. Cria diretório da aplicação ────────────────────────────────────────────
Step "Criando diretório da aplicação"
New-Item -ItemType Directory -Force -Path $APP_DIR | Out-Null
New-Item -ItemType Directory -Force -Path "$APP_DIR\images" | Out-Null
New-Item -ItemType Directory -Force -Path "$APP_DIR\logs"   | Out-Null
Ok "$APP_DIR criado"

# ── 2. Copia o código ─────────────────────────────────────────────────────────
Step "Copiando código para $APP_DIR"
$SCRIPT_DIR = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
robocopy $SCRIPT_DIR $APP_DIR /E /XD ".venv" "__pycache__" "logs" /XF "*.pyc" ".env" | Out-Null
Ok "Código copiado"

# ── 3. Cria virtualenv e instala dependências ─────────────────────────────────
Step "Criando virtualenv Python"
Set-Location $APP_DIR
& $PYTHON -m venv .venv
Ok "Virtualenv criado"

Step "Instalando dependências"
& "$VENV\Scripts\pip.exe" install --upgrade pip --quiet
& "$VENV\Scripts\pip.exe" install -r requirements.txt --quiet
& "$VENV\Scripts\pip.exe" install "slack-bolt>=1.18" "anthropic>=0.25" --quiet
Ok "Dependências instaladas"

Step "Instalando Playwright + Chromium"
& "$VENV\Scripts\playwright.exe" install chromium
Ok "Playwright instalado"

# ── 4. Configura nginx ────────────────────────────────────────────────────────
Step "Configurando nginx"
$NGINX_DIR = "C:\nginx"
if (-not (Test-Path $NGINX_DIR)) {
    Write-Host "  ⚠️  nginx não encontrado em C:\nginx" -ForegroundColor Yellow
    Write-Host "  Baixe em https://nginx.org/en/download.html e extraia para C:\nginx"
    Write-Host "  Depois copie manualmente:"
    Write-Host "  copy $APP_DIR\deploy\nginx.conf C:\nginx\conf\nginx.conf"
} else {
    Copy-Item "$APP_DIR\deploy\nginx_windows.conf" "C:\nginx\conf\nginx.conf" -Force
    Ok "nginx.conf copiado"
}

# ── 5. Instala serviços via NSSM ──────────────────────────────────────────────
Step "Instalando serviços Windows via NSSM"
if (-not (Test-Path $NSSM)) {
    Write-Host "  ⚠️  NSSM não encontrado em $NSSM" -ForegroundColor Yellow
    Write-Host "  Baixe em https://nssm.cc/download e extraia para C:\nssm\"
    Write-Host "  Depois execute manualmente os comandos abaixo:`n"
    Write-Host "  # Serviço principal (uvicorn):"
    Write-Host "  nssm install $SERVICE_MAIN `"$VENV\Scripts\uvicorn.exe`""
    Write-Host "  nssm set $SERVICE_MAIN AppParameters `"main:app --host 127.0.0.1 --port 8000`""
    Write-Host "  nssm set $SERVICE_MAIN AppDirectory `"$APP_DIR`""
    Write-Host "  nssm set $SERVICE_MAIN AppEnvironmentExtra `"PYTHONPATH=$APP_DIR`""
    Write-Host "  nssm set $SERVICE_MAIN AppStdout `"$APP_DIR\logs\app.log`""
    Write-Host "  nssm set $SERVICE_MAIN AppStderr `"$APP_DIR\logs\app.log`""
    Write-Host ""
    Write-Host "  # Serviço guardião:"
    Write-Host "  nssm install $SERVICE_GUAR `"$VENV\Scripts\python.exe`""
    Write-Host "  nssm set $SERVICE_GUAR AppParameters `"deploy\guardiao.py`""
    Write-Host "  nssm set $SERVICE_GUAR AppDirectory `"$APP_DIR`""
    Write-Host "  nssm set $SERVICE_GUAR AppStdout `"$APP_DIR\logs\guardiao.log`""
    Write-Host "  nssm set $SERVICE_GUAR AppStderr `"$APP_DIR\logs\guardiao.log`""
    Write-Host "  nssm set $SERVICE_GUAR Start SERVICE_AUTO_START`n"
} else {
    # Serviço principal
    & $NSSM install $SERVICE_MAIN "$VENV\Scripts\uvicorn.exe"
    & $NSSM set $SERVICE_MAIN AppParameters "main:app --host 127.0.0.1 --port 8000"
    & $NSSM set $SERVICE_MAIN AppDirectory $APP_DIR
    & $NSSM set $SERVICE_MAIN AppStdout "$APP_DIR\logs\app.log"
    & $NSSM set $SERVICE_MAIN AppStderr "$APP_DIR\logs\app.log"
    & $NSSM set $SERVICE_MAIN Start SERVICE_AUTO_START

    # Serviço guardião
    & $NSSM install $SERVICE_GUAR "$VENV\Scripts\python.exe"
    & $NSSM set $SERVICE_GUAR AppParameters "deploy\guardiao.py"
    & $NSSM set $SERVICE_GUAR AppDirectory $APP_DIR
    & $NSSM set $SERVICE_GUAR AppStdout "$APP_DIR\logs\guardiao.log"
    & $NSSM set $SERVICE_GUAR AppStderr "$APP_DIR\logs\guardiao.log"
    & $NSSM set $SERVICE_GUAR Start SERVICE_AUTO_START
    Ok "Serviços NSSM instalados"
}

# ── 6. Verificação do .env ────────────────────────────────────────────────────
Step "Verificando .env"
if (Test-Path "$APP_DIR\.env") {
    Ok ".env encontrado"
    $env_content = Get-Content "$APP_DIR\.env" -Raw
    foreach ($var in @("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_GUARDIAN_CHANNEL", "LOG_FILE")) {
        if ($env_content -match "^$var=") {
            Ok "  $var configurado"
        } else {
            Write-Host "  ⚠️  $var não encontrado no .env" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "  ⚠️  .env não encontrado — crie antes de iniciar os serviços" -ForegroundColor Yellow
}

# ── Instruções finais ─────────────────────────────────────────────────────────
Write-Host ""
Write-Host "╔══════════════════════════════════════════════╗"
Write-Host "║  Setup concluído!                            ║"
Write-Host "╚══════════════════════════════════════════════╝"
Write-Host ""
Write-Host "Próximos passos:"
Write-Host ""
Write-Host "  1. Certifique-se que o .env está completo:"
Write-Host "     $APP_DIR\.env"
Write-Host "     Inclua: LOG_FILE=C:\consorcio-sorteado\logs\app.log"
Write-Host "             IMAGES_DIR=C:\consorcio-sorteado\images"
Write-Host ""
Write-Host "  2. Inicie os serviços:"
Write-Host "     sc start $SERVICE_MAIN"
Write-Host "     sc start $SERVICE_GUAR"
Write-Host ""
Write-Host "  3. Verifique os logs:"
Write-Host "     Get-Content $APP_DIR\logs\app.log -Tail 50"
Write-Host ""
Write-Host "  4. Inicie o nginx:"
Write-Host "     C:\nginx\nginx.exe"
Write-Host ""
Write-Host "  5. Teste o health check:"
Write-Host "     Invoke-RestMethod http://localhost/health"
Write-Host ""
