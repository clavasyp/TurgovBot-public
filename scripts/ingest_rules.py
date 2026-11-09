#!/usr/bin/env python3
"""
Разовый ингест ПРАВИЛ сервера (3 раздела форума) в отдельный свод law_chunks для режима «Обзвон».

Переиспользует существующий парсер законов (он контент-агностичен) + индексатор:
  remote_parse_section -> remote_parse_topic -> save_law_document_to_db -> index_server_laws.

Свод — серверо-нейтральный namespace server_id=config.LEADERSHIP_RULES_SERVER_ID ('majestic_rules'),
ОТДЕЛЬНЫЙ от игровой законки. gemini_file_uri у этих law_documents НЕ ставим (NULL) → свод НЕ всплывает
в пикере серверов оверлея. Режим leadership принудительно ретривит этот свод (advice_service).

ЗАПУСК (в окружении с доступом к БД + парсер-воркеру + VERTEX_CREDENTIALS_FILE), напр. внутри
live-контейнера:  docker cp scripts/ingest_rules.py <api>:/app/scripts/ && docker exec <api> python /app/scripts/ingest_rules.py
Идемпотентно: law_documents upsert по (server_id, source_thread_id); index_server_laws пере-чанкит заново.
"""
import asyncio
import logging

from database.manager import db_manager
from remote_parser_client import remote_parse_section, remote_parse_topic, check_remote_parser_available
from knowledge_base_manager import save_law_document_to_db
from services.indexing_service import index_server_laws

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_rules")

# Самодостаточно (НЕ из config) — скрипт запускается на ЖИВОМ образе, где config ещё без этих полей.
# ДОЛЖНО совпадать с config.LEADERSHIP_RULES_* в коде сервера (majestic_rules).
SERVER_ID = "majestic_rules"
DISPLAY = "Правила Majestic"
SECTIONS = [
    "https://forum.majestic-rp.ru/forums/general-server-rules/",
    "https://forum.majestic-rp.ru/forums/rules-of-criminal-organizations/",
    "https://forum.majestic-rp.ru/forums/rules-of-state-organizations/",
]


async def ensure_server_row() -> None:
    """Свод-сервер (project_id NULL — самостоятельный namespace). Нужен для FK law_documents.server_id."""
    await db_manager.execute(
        "INSERT INTO servers (server_id, project_id, display_name, is_disabled) "
        "VALUES ($1, NULL, $2, false) ON CONFLICT (server_id) DO NOTHING",
        SERVER_ID, DISPLAY,
    )
    log.info("servers: %s («%s») готов", SERVER_ID, DISPLAY)


async def scrape_section(section_url: str) -> int:
    saved = 0
    urls = await remote_parse_section(section_url)
    log.info("раздел %s → %d тем", section_url, len(urls))
    for i, url in enumerate(urls, 1):
        try:
            data = await remote_parse_topic(url, parse_only_author=True)
        except Exception as e:
            log.warning("  [%d/%d] парс упал %s: %s", i, len(urls), url, e)
            continue
        if not data:
            log.warning("  [%d/%d] пусто %s", i, len(urls), url)
            continue
        title = (data.get("title") or "").strip() or url
        content = (data.get("content") or "").strip()
        thread_id = data.get("thread_id")
        content_hash = data.get("content_hash") or ""
        if not content or thread_id is None:
            log.warning("  [%d/%d] нет content/thread_id %s", i, len(urls), url)
            continue
        await save_law_document_to_db(SERVER_ID, int(thread_id), url, title, content, content_hash)
        saved += 1
        log.info("  [%d/%d] сохранено: %s (%d симв.)", i, len(urls), title[:60], len(content))
    return saved


def _init_vertex() -> None:
    """Инициализация genai-клиента (для эмбеддингов gemini-embedding-001) — образец api/main.py/golden_advice."""
    import config
    from rate_limiter import init_client
    from services.sa_admin import project_id_for
    init_client(
        project_id=project_id_for(config.VERTEX_CREDENTIALS_FILE),
        credentials_file=config.VERTEX_CREDENTIALS_FILE,
        region=config.GCP_REGION,
    )
    log.info("Vertex genai-клиент инициализирован")


async def main() -> None:
    _init_vertex()
    await db_manager.connect()
    try:
        if not await check_remote_parser_available():
            log.error("парсер-воркер недоступен — прерываю (проверь PARSER_WORKER_URL/SECRET)")
            return
        await ensure_server_row()

        total_docs = 0
        for sec in SECTIONS:
            total_docs += await scrape_section(sec)
        log.info("СКРЕЙП ГОТОВ: сохранено документов=%d в свод %s", total_docs, SERVER_ID)

        if total_docs == 0:
            log.error("0 документов — индексацию пропускаю (нечего эмбеддить)")
            return

        log.info("ИНДЕКСАЦИЯ (эмбеддинги, может быть долго из-за квоты)…")
        res = await index_server_laws(SERVER_ID)
        log.info("ГОТОВО: документов=%s, чанков=%s в law_chunks[%s]", res.get("documents"), res.get("chunks"), SERVER_ID)
    finally:
        await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
