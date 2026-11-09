#!/bin/bash
# =============================================================================
# Бэкап базы данных PostgreSQL
# =============================================================================
# Использование:
#   ./scripts/backup_db.sh                    # бэкап из Docker
#   ./scripts/backup_db.sh --local            # бэкап с локального PostgreSQL
# =============================================================================

set -euo pipefail

BACKUP_DIR="$(cd "$(dirname "$0")/.." && pwd)/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/turgovbot_${TIMESTAMP}.sql"

# Создаём директорию для бэкапов
mkdir -p "$BACKUP_DIR"

# Загружаем .env
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
if [ -f "$ENV_FILE" ]; then
    export $(grep -v '^#' "$ENV_FILE" | xargs)
fi

echo "📦 Создание бэкапа базы данных..."
echo "   DB: ${DB_NAME}"
echo "   Файл: ${BACKUP_FILE}"

if [ "${1:-}" = "--local" ]; then
    # Бэкап с локального PostgreSQL
    PGPASSWORD="${DB_PASS}" pg_dump \
        -h "${DB_HOST}" \
        -p "${DB_PORT}" \
        -U "${DB_USER}" \
        -d "${DB_NAME}" \
        --verbose \
        --format=plain \
        > "$BACKUP_FILE"
else
    # Бэкап из Docker-контейнера
    docker compose exec -T db pg_dump \
        -U "${DB_USER}" \
        -d "${DB_NAME}" \
        --verbose \
        --format=plain \
        > "$BACKUP_FILE"
fi

# Сжимаем
gzip "$BACKUP_FILE"
BACKUP_FILE="${BACKUP_FILE}.gz"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "✅ Бэкап создан: ${BACKUP_FILE} (${SIZE})"

# Удаляем бэкапы старше 30 дней
find "$BACKUP_DIR" -name "turgovbot_*.sql.gz" -mtime +30 -delete 2>/dev/null || true
echo "🧹 Старые бэкапы (>30 дней) удалены"
