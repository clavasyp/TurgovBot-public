#!/usr/bin/env python3
"""
Backfill/пере-эмбеддинг каталога локальной FRIDA через устойчивую DB-очередь.

Это НЕ обогащение (LLM/квота не дёргаются) — просто пересчёт векторной колонки под смену модели.
После миграции 020 пишет canonical law_article_embeddings с паспортом space_id/input_hash,
зеркалит legacy law_articles.embedding_local и пересчитывает related того же пространства.

Требует: миграция 020 прогнана; модель FRIDA разложена в LAW_CATALOG_EMBED_MODEL_DIR,
зависимости sentence-transformers + torch установлены. Gemini/Vertex не вызываются.

ЗАПУСК (в контейнере api):
  docker cp scripts/reembed_law_articles.py <api>:/app/scripts/
  docker exec <api> python /app/scripts/reembed_law_articles.py --all
  docker exec <api> python /app/scripts/reembed_law_articles.py orlando --force

Идемпотентно: по умолчанию считает только отсутствующие/устаревшие; --force → пересчёт active space целиком.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reembed_law_articles")


async def catalog_servers() -> list[str]:
    """Серверы, у которых есть статьи каталога."""
    rows = await db_manager.fetch(
        "SELECT DISTINCT server_id FROM law_articles WHERE kind = 'article' ORDER BY server_id"
    )
    return [r["server_id"] for r in rows]


async def main() -> None:
    ap = argparse.ArgumentParser(description="Пере-эмбеддинг каталога локальной моделью (FRIDA)")
    ap.add_argument("servers", nargs="*", help="server_id (пусто + --all = все с каталогом)")
    ap.add_argument("--all", action="store_true", help="все серверы с каталогом")
    ap.add_argument("--force", action="store_true", help="пересчитать ВСЕ статьи (не только пустые)")
    ap.add_argument("--no-related", action="store_true", help="не пересчитывать related-матрицу")
    args = ap.parse_args()

    import config
    from services.law_embedder import init_law_embedder
    from services import law_catalog_service as cat

    log.info("Грузим локальный эмбеддер (FRIDA): %s (ждём dims=%s)",
             config.LAW_CATALOG_EMBED_MODEL_DIR, config.LAW_CATALOG_EMBED_DIMS)
    emb = init_law_embedder(warmup=True)

    await db_manager.connect()
    try:
        servers = args.servers or (await catalog_servers() if args.all else [])
        if not servers:
            log.error("Не указаны серверы. Дай список или --all.")
            return
        log.info("Пере-эмбеддинг серверов: %s (force=%s, related=%s)",
                 ", ".join(servers), args.force, not args.no_related)

        grand = 0
        for i, sv in enumerate(servers, 1):
            log.info("[%d/%d] ===== %s =====", i, len(servers), sv)
            n = await cat.embed_articles_for_server(
                sv, force=args.force, rebuild_related=not args.no_related,
            )
            grand += n
            log.info("[%s] статей=%d, related=%s", sv, n, not args.no_related)

        # sanity: размерность вектора совпадает с конфигом?
        if emb.dims and emb.dims != config.LAW_CATALOG_EMBED_DIMS:
            log.warning("⚠️ Модель отдаёт %d dims, а конфиг/колонка ждут %d — проверь LAW_CATALOG_EMBED_DIMS!",
                        emb.dims, config.LAW_CATALOG_EMBED_DIMS)
        log.info("ГОТОВО: пере-эмбеддено статей=%d по %d серверам (dims=%s)", grand, len(servers), emb.dims)
    finally:
        await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
