#!/bin/bash
# deploy/setup_vps.sh — Configura o VPS do Consórcio Sorteado
# Execute como o usuário da aplicação (não root), com sudo disponível.
#
# Uso:
#   chmod +x deploy/setup_vps.sh
#   bash deploy/setup_vps.sh

set -e

# ── Configuração ──────────────────────────────────────────────────────────────
APP_DIR="/opt/consorcio-sorteado"
APP_USER="$(whoami)"
SERVICE_MAIN="consorcio-sorteado"
SERVICE_GUARDIAN="guardiao"

GREEN="\033[32m"
CYAN="\033[36m"
RESET="\033[0m"
step() { echo -e "\n${CYAN}▶ $1${RESET}"; }
ok()   { echo -e "${GREEN}✓ $1${RESET}"; }

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Setup VPS — Consórcio Sorteado              ║"
echo "╚══════════════════════════════════════════════╝"
echo "  Usuário:    $APP_USER"
echo "  Diretório:  $APP_DIR"
echo ""

# ── 1. Cria diretório da aplicação ────────────────────────────────────────────
step "Criando diretório da aplicação"
sudo mkdir -p "$APP_DIR"
sudo chown "$APP_USER:$APP_USER" "$APP_DIR"
ok "$APP_DIR criado"

# ── 2. Copia o código (assume que o script roda de dentro do repo) ────────────
step "Copiando código para $APP_DIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
      --exclude='.env' --exclude='tests/__pycache__' \
      "$SCRIPT_DIR/" "$APP_DIR/"
ok "Código copiado"

# ── 3. Cria virtualenv e instala dependências ─────────────────────────────────
step "Criando virtualenv Python 3.12"
cd "$APP_DIR"
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip --quiet
ok "Virtualenv criado"

step "Instalando dependências da aplicação"
.venv/bin/pip install -r requirements.txt --quiet
ok "requirements.txt instalado"

step "Instalando dependências do guardião"
.venv/bin/pip install "slack-bolt>=1.18" "anthropic>=0.25" --quiet
ok "Dependências do guardião instaladas"

step "Instalando Playwright + Chromium"
.venv/bin/playwright install chromium --with-deps
ok "Playwright instalado"

step "Criando diretório persistente de imagens"
mkdir -p "$APP_DIR/images"
ok "Diretório de imagens criado em $APP_DIR/images"

# ── 4. Configura nginx ────────────────────────────────────────────────────────
step "Configurando nginx"
sudo cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/consorcio-sorteado
sudo ln -sf /etc/nginx/sites-available/consorcio-sorteado \
            /etc/nginx/sites-enabled/consorcio-sorteado
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
ok "nginx configurado"

# ── 5. Instala e habilita systemd services ────────────────────────────────────
step "Configurando systemd services"

# Substitui APP_USER pelo usuário real nos arquivos .service
sed "s/APP_USER/$APP_USER/g" \
    "$APP_DIR/deploy/consorcio-sorteado.service" | \
    sudo tee /etc/systemd/system/consorcio-sorteado.service > /dev/null

sed "s/APP_USER/$APP_USER/g" \
    "$APP_DIR/deploy/guardiao.service" | \
    sudo tee /etc/systemd/system/guardiao.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_MAIN"
sudo systemctl enable "$SERVICE_GUARDIAN"
ok "Systemd services habilitados"

# ── 6. Configura sudoers para o guardião ──────────────────────────────────────
step "Configurando permissões de restart para o guardião"
SUDOERS_LINE="$APP_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart $SERVICE_MAIN, /bin/systemctl stop $SERVICE_MAIN, /bin/systemctl status $SERVICE_MAIN, /bin/systemctl start $SERVICE_MAIN"
echo "$SUDOERS_LINE" | sudo tee /etc/sudoers.d/guardiao > /dev/null
sudo chmod 440 /etc/sudoers.d/guardiao
ok "sudoers configurado"

# ── 7. Verificação do .env ────────────────────────────────────────────────────
step "Verificando .env"
if [ -f "$APP_DIR/.env" ]; then
    ok ".env encontrado"
    # Verifica variáveis críticas do guardião
    for var in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_GUARDIAN_CHANNEL; do
        if grep -q "^${var}=" "$APP_DIR/.env" 2>/dev/null; then
            ok "  $var configurado"
        else
            echo "  ⚠️  $var não encontrado no .env — adicione antes de iniciar o guardião"
        fi
    done
else
    echo "  ⚠️  .env não encontrado em $APP_DIR"
    echo "  Crie o arquivo antes de iniciar os serviços."
fi

# ── Instruções finais ─────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Setup concluído!                            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Próximos passos:"
echo ""
echo "  1. Certifique-se que o .env está completo:"
echo "     $APP_DIR/.env"
echo "     (veja as variáveis necessárias em deploy/env_exemplo.txt)"
echo ""
echo "  2. Inicie a aplicação:"
echo "     sudo systemctl start consorcio-sorteado"
echo "     sudo systemctl status consorcio-sorteado"
echo ""
echo "  3. Inicie o guardião:"
echo "     sudo systemctl start guardiao"
echo "     sudo systemctl status guardiao"
echo ""
echo "  4. Verifique os logs em tempo real:"
echo "     journalctl -u consorcio-sorteado -f"
echo "     journalctl -u guardiao -f"
echo ""
echo "  5. Teste o health check:"
echo "     curl http://localhost/health"
echo ""
