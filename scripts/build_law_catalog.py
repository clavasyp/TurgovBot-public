#!/usr/bin/env python3
"""
Backfill каталога статей умного поиска (law_articles/law_synonyms/law_article_related).

Собирает каталог из УЖЕ существующих law_documents + центроиды из УЖЕ существующих
law_chunks (эмбеддинг-API не дёргает). LLM используется только для обогащения
(структура наказаний + синонимы, gemini-3.5-flash) — отключается флагом --no-enrich.

ЗАПУСК (в окружении с БД; для обогащения нужен VERTEX_CREDENTIALS_FILE), напр. в контейнере:
  docker cp scripts/build_law_catalog.py <api>:/app/scripts/
  docker exec <api> python /app/scripts/build_law_catalog.py --all          # все серверы с RAG-индексом
  docker exec <api> python /app/scripts/build_law_catalog.py orlando miami  # выборочно
  docker exec <api> python /app/scripts/build_law_catalog.py --all --no-enrich  # без LLM (быстро)

Идемпотентно: существующие статьи обновляются с прежним публичным ID, новые
добавляются, реально исчезнувшие удаляются. Обогащение запускается только для
протухших статей (enrich_hash != article_hash); owner/user-синонимы остаются на ID.
Требует прогнанных миграций 017-021.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Самодостаточный запуск из любого cwd: docker exec <api> python /app/scripts/build_law_catalog.py
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_law_catalog")


def _init_vertex() -> None:
    """Инициализация genai-клиента (нужна только для LLM-обогащения) — образец ingest_rules.py."""
    import config
    from rate_limiter import init_client
    from services.sa_admin import project_id_for
    init_client(
        project_id=project_id_for(config.VERTEX_CREDENTIALS_FILE),
        credentials_file=config.VERTEX_CREDENTIALS_FILE,
        region=config.GCP_REGION,
    )
    log.info("Vertex genai-клиент инициализирован")


async def rag_servers() -> list[str]:
    """Серверы с построенным RAG-индексом — только для них имеет смысл каталог.

    Свод правил (majestic_rules) исключаем: это не игровая законка, поиск по нему
    не нужен; при необходимости можно собрать явно: build_law_catalog.py majestic_rules.
    """
    import config
    rows = await db_manager.fetch("SELECT DISTINCT server_id FROM law_chunks ORDER BY server_id")
    rules_ns = getattr(config, "LEADERSHIP_RULES_SERVER_ID", "majestic_rules")
    return [r["server_id"] for r in rows if r["server_id"] != rules_ns]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill каталога статей умного поиска")
    parser.add_argument("servers", nargs="*", help="server_id (пусто + --all = все серверы с RAG)")
    parser.add_argument("--all", action="store_true", help="все серверы с law_chunks (кроме свода правил)")
    parser.add_argument("--no-enrich", action="store_true", help="без LLM-обогащения (penalty/синонимы)")
    parser.add_argument("--retitle", action="store_true",
                        help="сбросить enrich_hash статей без title — обогащение перегенерит их "
                             "(разовый догон после апгрейда промпта с извлечением названий)")
    parser.add_argument("--reenrich", action="store_true",
                        help="ПОЛНАЯ перегенерация обогащения: сбросить enrich_hash ВСЕХ статей "
                             "(penalty+синонимы заново новым промптом). Дорого по LLM.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только показать план preserved/inserted/deleted/ambiguous без записей и LLM",
    )
    args = parser.parse_args()
    if args.dry_run and (args.retitle or args.reenrich):
        parser.error("--dry-run нельзя совмещать с --retitle/--reenrich")

    from services.law_catalog_service import build_catalog_for_server

    enrich = not args.no_enrich and not args.dry_run
    if enrich:
        _init_vertex()

    await db_manager.connect()
    try:
        targets = list(args.servers)
        if args.all:
            targets.extend(s for s in await rag_servers() if s not in targets)
        if not targets:
            parser.error("укажи server_id или --all")

        log.info(
            "Каталогизация %d серверов: %s (enrich=%s, dry_run=%s)",
            len(targets), ", ".join(targets), enrich, args.dry_run,
        )
        totals = {
            "articles": 0,
            "related": 0,
            "preserved": 0,
            "inserted": 0,
            "deleted": 0,
            "ambiguous": 0,
        }
        failed_servers = []
        for i, server_id in enumerate(targets, 1):
            log.info("[%d/%d] ===== %s =====", i, len(targets), server_id)
            try:
                res = await build_catalog_for_server(server_id, enrich=enrich, retitle=args.retitle,
                                                     reenrich=args.reenrich,
                                                     dry_run=args.dry_run)
            except Exception:
                log.exception("[%d/%d] сервер %s упал — продолжаю со следующим", i, len(targets), server_id)
                failed_servers.append(server_id)
                continue
            for key in totals:
                totals[key] += res.get(key) or 0
            log.info("[%d/%d] %s: %s", i, len(targets), server_id, res)
        if args.dry_run:
            log.info(
                "DRY-RUN ГОТОВ: статей=%d, preserved=%d, inserted=%d, "
                "deleted=%d, ambiguous=%d",
                totals["articles"], totals["preserved"], totals["inserted"],
                totals["deleted"], totals["ambiguous"],
            )
        else:
            log.info(
                "ГОТОВО: статей=%d, related-связей=%d",
                totals["articles"], totals["related"],
            )

        # Авто-аудит качества титулов: настоящие названия почти не повторяются, а маркер,
        # ошибочно принятый за название («★★★★ [Фед./Рег.]», «*»), размножается на десятки
        # статей. Повторы ≥8 — предупреждение для ручной проверки (бывают и легитимные:
        # «Утратила силу» у отменённых статей).
        if not args.dry_run:
            rows = await db_manager.fetch(
                """
                SELECT server_id, title, count(*) AS n FROM law_articles
                WHERE kind = 'article' AND title IS NOT NULL
                GROUP BY server_id, title HAVING count(*) >= 8 ORDER BY n DESC LIMIT 15
                """
            )
            for r in rows:
                log.warning("АУДИТ: server=%s титул «%s» повторяется %d раз — проверь, не маркер ли это",
                            r["server_id"], r["title"], r["n"])
            if not rows:
                log.info("АУДИТ: аномально повторяющихся титулов нет")
        if failed_servers:
            log.error("ОШИБКА: не завершены серверы: %s", ", ".join(failed_servers))
            raise SystemExit(1)
    finally:
        await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
