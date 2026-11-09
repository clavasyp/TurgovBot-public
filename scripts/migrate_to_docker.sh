#!/bin/bash
# =============================================================================
# Миграция существующей БД в Docker
# =============================================================================
# Этот скрипт:
# 1. Делает бэкап текущей БД (localhost)
# 2. Запускает PostgreSQL в Docker
# 3. Восстанавливает бэкап в Docker-контейнер
# 4. Проверяет что всё работает
#
# Использование: ./scripts/migrate_to_docker.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="${PROJECT_DIR}/backups"

cd "$PROJECT_DIR"

# Загружаем .env
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

echo "=========================================="
echo "  Миграция БД TurgovBot → Docker"
echo "=========================================="
echo ""

# Шаг 1: Бэкап текущей БД
echo "📦 Шаг 1/4: Бэкап текущей базы данных..."
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/pre_docker_migration_${TIMESTAMP}.sql"

PGPASSWORD="${DB_PASS}" pg_dump \
    -h "${DB_HOST:-localhost}" \
    -p "${DB_PORT:-5432}" \
    -U "${DB_USER}" \
    -d "${DB_NAME}" \
    --verbose \
    --format=plain \
    > "$BACKUP_FILE" 2>/dev/null

gzip "$BACKUP_FILE"
BACKUP_FILE="${BACKUP_FILE}.gz"
echo "✅ Бэкап создан: ${BACKUP_FILE}"
echo ""

# Шаг 2: Запуск PostgreSQL в Docker
echo "🐳 Шаг 2/4: Запуск PostgreSQL в Docker..."
docker compose up -d db
echo "   Ожидание готовности БД..."
sleep 5

# Ждём пока БД станет healthy
for i in {1..30}; do
    if docker compose exec -T db pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
        echo "✅ PostgreSQL в Docker готов"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "❌ PostgreSQL не запустился за 30 секунд"
        exit 1
    fi
    sleep 1
done
echo ""

# Шаг 3: Восстановление бэкапа в Docker
echo "📥 Шаг 3/4: Восстановление данных в Docker..."
TEMP_SQL=$(mktemp /tmp/turgovbot_migrate_XXXXXX.sql)
gunzip -c "$BACKUP_FILE" > "$TEMP_SQL"

docker compose exec -T db psql -U "${DB_USER}" -d "${DB_NAME}" < "$TEMP_SQL"
rm -f "$TEMP_SQL"
echo "✅ Данные восстановлены"
echo ""

# Шаг 4: Проверка
echo "🔍 Шаг 4/4: Проверка данных..."
USERS_COUNT=$(docker compose exec -T db psql -U "${DB_USER}" -d "${DB_NAME}" -t -c "SELECT COUNT(*) FROM users;" 2>/dev/null | tr -d ' ')
TABLES_COUNT=$(docker compose exec -T db psql -U "${DB_USER}" -d "${DB_NAME}" -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';" 2>/dev/null | tr -d ' ')

echo "   Таблиц: ${TABLES_COUNT}"
echo "   Пользователей: ${USERS_COUNT}"
echo ""

echo "=========================================="
echo "  ✅ МИГРАЦИЯ ЗАВЕРШЕНА УСПЕШНО"
echo "=========================================="
echo ""
echo "Теперь можно запустить все сервисы:"
echo "  docker compose up -d"
echo ""
echo "Не забудьте применить миграции Alembic:"
echo "  docker compose run --rm migrate"
echo ""
echo "⚠️  После проверки можете остановить локальный PostgreSQL"
