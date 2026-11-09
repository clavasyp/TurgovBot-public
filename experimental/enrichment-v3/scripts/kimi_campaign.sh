#!/bin/sh
# Автономный разовый прогон обогащения через Kimi K3 по списку серверов.
#
# Запускается ВНУТРИ прод-контейнера бота и не трогает базу: для каждого сервера
# делает неизменяемый экспорт исходных батчей и складывает рядом reviewed-артефакты.
# Запись в БД (validate -> apply) выполняется отдельно и вручную.
#
# Свойства:
#   * идемпотентность — готовый батч не перегенерируется, экспорт не повторяется;
#   * живучесть — генератор перезапускается, пока в сервере остаются недоделанные батчи;
#   * никакой параллельности между серверами: у бесплатного тира потолок ~5-7
#     одновременных запросов, и он уже выбран параллелизмом внутри одного прогона.
#
# Использование:
#   TOKENROUTER_API_KEY=... sh /tmp/kimi_campaign.sh "memphis richman portland" /tmp/kimi
set -u

SERVERS="${1:?перечисли server_id через пробел}"
ROOT="${2:-/tmp/kimi-campaign}"
CHUNK="${KIMI_CHUNK:-5}"
CONCURRENCY="${KIMI_CONCURRENCY:-5}"
IDLE_TIMEOUT="${KIMI_IDLE_TIMEOUT:-75}"
MAX_RESTARTS="${KIMI_MAX_RESTARTS:-40}"

if [ -z "${TOKENROUTER_API_KEY:-}" ]; then
    echo "TOKENROUTER_API_KEY не задан" >&2
    exit 2
fi

mkdir -p "$ROOT"
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

for SERVER in $SERVERS; do
    SRC="$ROOT/$SERVER-src"
    OUT="$ROOT/$SERVER-out"
    mkdir -p "$OUT"

    # --- экспорт (ровно один раз на сервер) ---
    if [ ! -f "$SRC/manifest.json" ]; then
        log "$SERVER: экспорт исходных батчей"
        IDS_FILE="$ROOT/$SERVER-ids.txt"
        python - "$SERVER" "$IDS_FILE" <<'PY'
import asyncio, sys
sys.path.insert(0, "/app")
from database.manager import db_manager

async def main() -> None:
    server_id, path = sys.argv[1], sys.argv[2]
    await db_manager.connect()
    try:
        rows = await db_manager.fetch(
            "SELECT id FROM law_articles WHERE server_id = $1 AND kind = 'article' ORDER BY id",
            server_id,
        )
    finally:
        await db_manager.close()
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(f"{row['id']}\n")
    print(f"{server_id}: {len(rows)} статей")

asyncio.run(main())
PY
        if [ ! -s "$IDS_FILE" ]; then
            log "$SERVER: статей нет — пропуск"
            continue
        fi
        # Статьи бедных серверов уже помечены обогащёнными, поэтому экспорт идёт
        # явными --article-id (иначе экспортёр посчитал бы, что делать нечего).
        ARGS=$(awk '{printf " --article-id %s", $1}' "$IDS_FILE")
        # shellcheck disable=SC2086
        if ! python -m scripts.export_law_enrichment_batches "$SERVER" \
                --output-dir "$SRC" --batch-size 15 $ARGS; then
            log "$SERVER: ЭКСПОРТ НЕ УДАЛСЯ — пропуск сервера"
            continue
        fi
    else
        log "$SERVER: экспорт уже есть, использую его"
    fi

    TOTAL=$(find "$SRC" -name 'source-batch-*.json' | wc -l)
    log "$SERVER: батчей к обработке $TOTAL"

    # --- генерация с перезапусками, пока остаются недоделанные батчи ---
    RESTARTS=0
    while [ "$RESTARTS" -lt "$MAX_RESTARTS" ]; do
        DONE=$(find "$OUT" -name 'reviewed-batch-*.json' | wc -l)
        if [ "$DONE" -ge "$TOTAL" ]; then
            log "$SERVER: ГОТОВО ($DONE/$TOTAL)"
            break
        fi
        RESTARTS=$((RESTARTS + 1))
        log "$SERVER: генерация, попытка $RESTARTS (готово $DONE/$TOTAL)"
        python -m scripts.enrich_with_kimi \
            --batches-dir "$SRC" --out-dir "$OUT" \
            --chunk "$CHUNK" --concurrency "$CONCURRENCY" --timeout "$IDLE_TIMEOUT" \
            >> "$ROOT/$SERVER.log" 2>&1
        sleep 10
    done

    DONE=$(find "$OUT" -name 'reviewed-batch-*.json' | wc -l)
    if [ "$DONE" -lt "$TOTAL" ]; then
        log "$SERVER: НЕ ДОБИТ ($DONE/$TOTAL) после $RESTARTS попыток — иду дальше"
    fi
done

log "кампания завершена"
find "$ROOT" -name 'reviewed-batch-*.json' | wc -l | while read -r n; do
    log "всего reviewed-батчей: $n"
done
