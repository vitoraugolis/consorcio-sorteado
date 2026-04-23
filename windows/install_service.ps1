# install_service.ps1 — Instala o servidor como Serviço Windows
# Execute como Administrador: powershell -ExecutionPolicy Bypass -File install_service.ps1
#
# Pré-requisitos no VPS:
#   1. Python 3.12 instalado (python.org/downloads)
#   2. Projeto copiado para C:\consorcio-sorteado (ou ajuste $PROJECT_DIR abaixo)
#   3. .env preenchido no diretório do projeto

$PROJECT_DIR = "C:\consorcio-sorteado"
$PYTHON      = "$PROJECT_DIR\.venv\Scripts\python.exe"
$SERVICE_NAME = "ConsorcioSorteado"
$SERVICE_DESC = "Consorcio Sorteado — Automação Comercial"
$LOG_DIR     = "$PROJECT_DIR\logs"

Write-Host "=== Instalando Consórcio Sorteado como Serviço Windows ===" -ForegroundColor Cyan

# 1. Cria venv e instala dependências
Write-Host "`n[1/4] Criando ambiente virtual..."
Set-Location $PROJECT_DIR
python -m venv .venv
& "$PROJECT_DIR\.venv\Scripts\pip.exe" install -r requirements.txt --quiet

# 2. Cria diretório de logs
Write-Host "[2/4] Criando diretório de logs..."
New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

# 3. Baixa o NSSM (Non-Sucking Service Manager) se não existir
$NSSM = "$PROJECT_DIR\windows\nssm.exe"
if (-not (Test-Path $NSSM)) {
    Write-Host "[3/4] Baixando NSSM..."
    $nssmZip = "$env:TEMP\nssm.zip"
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmZip
    Expand-Archive -Path $nssmZip -DestinationPath "$env:TEMP\nssm_extract" -Force
    Copy-Item "$env:TEMP\nssm_extract\nssm-2.24\win64\nssm.exe" $NSSM
    Remove-Item $nssmZip, "$env:TEMP\nssm_extract" -Recurse -Force
} else {
    Write-Host "[3/4] NSSM já presente."
}

# 4. Instala/reconfigura o serviço via NSSM
Write-Host "[4/4] Instalando serviço '$SERVICE_NAME'..."
& $NSSM stop $SERVICE_NAME 2>$null
& $NSSM remove $SERVICE_NAME confirm 2>$null

& $NSSM install $SERVICE_NAME $PYTHON
& $NSSM set $SERVICE_NAME AppParameters "-m uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info"
& $NSSM set $SERVICE_NAME AppDirectory $PROJECT_DIR
& $NSSM set $SERVICE_NAME AppEnvironmentExtra "PYTHONPATH=$PROJECT_DIR"
& $NSSM set $SERVICE_NAME DisplayName $SERVICE_DESC
& $NSSM set $SERVICE_NAME Description $SERVICE_DESC
& $NSSM set $SERVICE_NAME Start SERVICE_AUTO_START
& $NSSM set $SERVICE_NAME AppStdout "$LOG_DIR\server.log"
& $NSSM set $SERVICE_NAME AppStderr "$LOG_DIR\server.log"
& $NSSM set $SERVICE_NAME AppRotateFiles 1
& $NSSM set $SERVICE_NAME AppRotateBytes 10485760   # 10 MB
& $NSSM set $SERVICE_NAME AppRestartDelay 10000     # reinicia 10s após falha

# Carrega variáveis do .env no ambiente do serviço
Write-Host "Carregando .env no serviço..."
Get-Content "$PROJECT_DIR\.env" | Where-Object { $_ -match "^\s*[A-Z_]+=.+" -and $_ -notmatch "^\s*#" } | ForEach-Object {
    $parts = $_ -split "=", 2
    & $NSSM set $SERVICE_NAME AppEnvironmentExtra "$($parts[0])=$($parts[1].Trim('"').Trim("'"))"
}

& $NSSM start $SERVICE_NAME

Write-Host "`n=== Concluído! ===" -ForegroundColor Green
Write-Host "Serviço: $SERVICE_NAME"
Write-Host "Status:  " -NoNewline
& $NSSM status $SERVICE_NAME
Write-Host "Logs:    $LOG_DIR\server.log"
Write-Host ""
Write-Host "Comandos úteis:"
Write-Host "  nssm start $SERVICE_NAME"
Write-Host "  nssm stop $SERVICE_NAME"
Write-Host "  nssm restart $SERVICE_NAME"
Write-Host "  nssm status $SERVICE_NAME"
Write-Host "  Get-Content $LOG_DIR\server.log -Wait -Tail 50   # tail -f no PowerShell"
