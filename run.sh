#!/bin/bash
# ============================================================
# run.sh — Inicia o servidor localmente
# Consórcio Sorteado
#
# Uso:
#   bash run.sh           → modo interativo (logs no terminal + arquivo)
#   bash run.sh --daemon  → modo background (logs só em logs/server.log)
#   bash run.sh --stop    → para o daemon
#   bash run.sh --logs    → acompanha logs em tempo real
# ============================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/logs/server.pid"
LOG_FILE="$DIR/logs/server.log"

# --- Verifica se o venv existe ---
if [ ! -d "$DIR/.venv" ]; then
    echo "❌ Ambiente virtual não encontrado. Rode primeiro: bash setup.sh"
    exit 1
fi

# --- Verifica .env ---
if [ ! -f "$DIR/.env" ]; then
    echo "❌ Arquivo .env não encontrado."
    exit 1
fi

mkdir -p "$DIR/logs"
source "$DIR/.venv/bin/activate"

case "${1:-}" in
    --stop)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            kill "$PID" 2>/dev/null && echo "✅ Servidor parado (PID $PID)" || echo "⚠️  Processo não encontrado"
            rm -f "$PID_FILE"
        else
            echo "⚠️  Nenhum daemon rodando (sem $PID_FILE)"
        fi
        exit 0
        ;;

    --logs)
        echo "📋 Acompanhando logs (Ctrl+C para sair):"
        tail -f "$LOG_FILE"
        exit 0
        ;;

    --daemon)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "⚠️  Servidor já está rodando (PID $(cat "$PID_FILE"))"
            exit 1
        fi
        nohup uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info \
            >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "✅ Servidor iniciado em background (PID $(cat "$PID_FILE"))"
        echo "   Logs: bash run.sh --logs"
        echo "   Stop: bash run.sh --stop"
        exit 0
        ;;

    *)
        echo ""
        echo "Consórcio Sorteado — Servidor Local"
        echo "====================================="
        echo "URL:    http://localhost:8000"
        echo "Health: http://localhost:8000/health"
        echo "Logs:   $LOG_FILE"
        echo "Parar:  Ctrl+C"
        echo "====================================="
        echo ""
        uvicorn main:app --reload --host 0.0.0.0 --port 8000 --log-level info
        ;;
esac
