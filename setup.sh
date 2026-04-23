#!/bin/bash
# ============================================================
# setup.sh — Configuração do ambiente local
# Consórcio Sorteado
#
# Uso: bash setup.sh
# ============================================================

set -e  # Para na primeira falha

echo ""
echo "🚀 Consórcio Sorteado — Setup do Ambiente Local"
echo "================================================"

# --- Verifica Python 3.10+ ---
PYTHON=$(command -v python3 || command -v python)
PYTHON_VERSION=$($PYTHON --version 2>&1 | awk '{print $2}')
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

echo "✓ Python encontrado: $PYTHON_VERSION"

if [ "$PYTHON_MINOR" -lt "10" ]; then
    echo "❌ Python 3.10 ou superior é necessário. Instale em https://python.org"
    exit 1
fi

# --- Cria ambiente virtual ---
echo ""
echo "→ Criando ambiente virtual (.venv)..."
$PYTHON -m venv .venv
echo "✓ Ambiente virtual criado"

# --- Ativa e instala dependências ---
echo ""
echo "→ Instalando dependências..."
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo "✓ Dependências instaladas"

# --- Verifica .env ---
echo ""
if [ -f ".env" ]; then
    echo "✓ Arquivo .env encontrado"
else
    echo "⚠️  Arquivo .env não encontrado!"
    echo "   Crie o .env com as variáveis necessárias antes de rodar."
fi

echo ""
echo "================================================"
echo "✅ Setup concluído!"
echo ""
echo "Para rodar o servidor:"
echo "   bash run.sh"
echo ""
echo "Ou manualmente:"
echo "   source .venv/bin/activate"
echo "   uvicorn main:app --reload --port 8000"
echo ""
