#!/bin/bash
# Auditoria tecnica - validacao, testes, Redis e commit
# Execute no host: bash /home/ubuntu/.openclaw/workspace/consorcio-sorteado/run_audit_commit.sh

set -e
PROJ=/home/ubuntu/.openclaw/workspace/consorcio-sorteado
cd "$PROJ"

echo "=== 1. Validando sintaxe Python ==="
for f in main.py webhooks/agente_bazar.py webhooks/agente_lp.py services/faro.py webhooks/router.py; do
    .venv/bin/python -m py_compile "$f" && echo "  OK: $f" || echo "  ERRO: $f"
done

echo ""
echo "=== 2. Configurando Redis ==="
redis-cli CONFIG GET save
redis-cli CONFIG SET save "300 1 60 100 30 1000" && echo "  save: OK"
redis-cli CONFIG SET appendonly yes && echo "  appendonly: OK"
redis-cli CONFIG SET appendfsync everysec && echo "  appendfsync: OK"
redis-cli CONFIG REWRITE && echo "  REWRITE: OK" || echo "  REWRITE: falhou (sem redis.conf — config em memoria apenas)"

echo ""
echo "=== 3. Rodando testes ==="
.venv/bin/python -m pytest tests/test_contemplacao_guard.py tests/test_edge_cases.py -q 2>&1 | tail -5

echo ""
echo "=== 4. Commit ==="
git add -A
git commit -m "fix: auditoria tecnica -- itens 2,4,5,6,7,8,9"
echo "DONE."
