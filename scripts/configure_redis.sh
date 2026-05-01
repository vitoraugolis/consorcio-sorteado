#!/bin/bash
# ITEM 7 - Configurar Redis com persistencia
# Execute no host: sudo bash scripts/configure_redis.sh

echo "=== Configuracao atual ==="
redis-cli CONFIG GET save
redis-cli CONFIG GET appendonly
redis-cli CONFIG GET appendfsync

echo "=== Ativando persistencia ==="
redis-cli CONFIG SET save "300 1 60 100 30 1000"
redis-cli CONFIG SET appendonly yes
redis-cli CONFIG SET appendfsync everysec
redis-cli CONFIG REWRITE

echo "=== Verificando redis.conf ==="
for f in /etc/redis/redis.conf /etc/redis.conf; do
    [ -f "$f" ] && echo "Encontrado: $f" && grep -E "^save|^appendonly|^appendfsync" "$f"
done

echo "=== Configuracao final ==="
redis-cli CONFIG GET save
redis-cli CONFIG GET appendonly
echo "DONE."
