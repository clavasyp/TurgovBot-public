#!/bin/bash
# =============================================================================
# Восстановление базы данных PostgreSQL из бэкапа
# =============================================================================
# Использование:
#   ./scripts/restore_db.sh backups/turgovbot_20260412_120000.sql.gz
#   ./scripts/restore_db.sh backups/turgovbot_20260412_120000.sql.gz --local
# =============================================================================

set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "❌ Использование: $0 <path-to-backup.sql.gz> [--local]"
    echo ""
    echo "Доступные бэкапы:"
    ls -la "$(cd "$(dirname "$0")/.." && pwd)/backups/"*.sql.gz 2>/dev/null || echo "   (нет бэкапов)"
    exit 1
fi

BACKUP_FILE="$1"
IS_LOCAL="${2:-}"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "❌ Файл не найден: $BACKUP_FILE"
    exit 1
fi

# Загружаем .env
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
if [ -f "$ENV_FILE" ]; then
    export $(grep -v '^#' "$ENV_FILE" | xargs)
fi

echo "⚠️  ВНИМАНИЕ: Восстановление ПОЛНОСТЬЮ ПЕРЕЗАПИШЕТ базу данных '${DB_NAME}'!"
echo "   Файл: ${BACKUP_FILE}"
read -p "   Продолжить? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo "❌ Отменено."
    exit 0
fi

echo "📦 Восстановление базы данных..."

# Распаковываем если сжат
TEMP_SQL=$(mktemp /tmp/turgovbot_restore_XXXXXX.sql)
if [[ "$BACKUP_FILE" == *.gz ]]; then
    gunzip -c "$BACKUP_FILE" > "$TEMP_SQL"
else
    cp "$BACKUP_FILE" "$TEMP_SQL"
fi

if [ "$IS_LOCAL" = "--local" ]; then
    # Восстановление в локальный PostgreSQL
    PGPASSWORD="${DB_PASS}" psql \
        -h "${DB_HOST}" \
        -p "${DB_PORT}" \
        -U "${DB_USER}" \
        -d "${DB_NAME}" \
        < "$TEMP_SQL"
else
    # Восстановление в Docker-контейнер
    docker compose exec -T db psql \
        -U "${DB_USER}" \
        -d "${DB_NAME}" \
        < "$TEMP_SQL"
fi

rm -f "$TEMP_SQL"

echo "✅ База данных успешно восстановлена из: ${BACKUP_FILE}"
