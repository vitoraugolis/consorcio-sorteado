#!/bin/bash
# Wrapper para launchd — carrega .env e ativa venv antes de subir o servidor
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Carrega variáveis do .env
set -a
source "$DIR/.env"
set +a

# Ativa venv e sobe servidor
exec "$DIR/.venv/bin/uvicorn" main:app --host 0.0.0.0 --port 8000 --log-level info
