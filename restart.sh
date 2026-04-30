#!/bin/bash
# restart.sh — reinicia o servidor CS de forma limpa
# Uso: ./restart.sh

set -e
WORKDIR="$(cd "$(dirname "$0")" && pwd)"
LOGFILE="$WORKDIR/logs/server.log"
VENV="$WORKDIR/.venv/bin/uvicorn"

echo "[restart.sh] Parando processo uvicorn anterior..."
pkill -f "uvicorn main:app" 2>/dev/null && sleep 2 || true

echo "[restart.sh] Subindo servidor..."
cd "$WORKDIR"
nohup "$VENV" main:app --host 0.0.0.0 --port 8000 >> "$LOGFILE" 2>&1 &

echo "[restart.sh] PID: $! | Aguardando health check..."
sleep 4
curl -sf http://localhost:8000/health | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'[restart.sh] Status: {d[\"status\"]} | Jobs: {len(d.get(\"jobs\",[]))}')
" || echo "[restart.sh] ⚠️  Health check falhou"
