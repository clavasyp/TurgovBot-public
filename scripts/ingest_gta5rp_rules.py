#!/usr/bin/env python3
"""
Ингест правил GTA5RP в своды law_chunks для режима «Обзвон» (пилот — сервер Davis).

Два слоя-namespace (project_id NULL, отдельные от игровой законки; gemini_file_uri НЕ ставим → не
всплывают в пикере серверов оверлея):
  • gta5rp_rules_project — общие «Правила проекта» (один тред-topic).
  • gta5rp_rules_davis   — правила сервера Davis: категория + 4 сабфорума (section → topics).

Режим leadership на GTA5RP ретривит ОБА слоя (union) — см. LEADERSHIP_RULES_MAP в law_sources_data.py
и advice_service.leadership-ветку. Слияние «показывать оба» (без override по номеру клаузы).

Переиспользует контент-агностичный парсер (forum.gta5rp.com — XenForo, как majestic) + индексатор:
  remote_parse_topic / remote_parse_section -> save_law_document_to_db -> index_server_laws.

ЗАПУСК (в live-контейнере с БД + парсер-воркером + VERTEX_CREDENTIALS_FILE):
  docker cp scripts/ingest_gta5rp_rules.py <api>:/app/scripts/ \
    && docker exec <api> python /app/scripts/ingest_gta5rp_rules.py
Идемпотентно: law_documents upsert по (server_id, source_thread_id); index_server_laws пере-чанкит.

Порционность: индексация каждого слоя — отдельные эмбеддинг-прогоны (gemini-embedding-001, квота
~10-12 req/min). Можно гнать по одному namespace через --only project|davis.
"""
import argparse
import asyncio
import logging

from database.manager import db_manager
from remote_parser_client import (
    remote_parse_section,
    remote_parse_topic,
    check_remote_parser_available,
)
from knowledge_base_manager import save_law_document_to_db
from services.indexing_service import index_server_laws

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_gta5rp_rules")

# --- Слой проекта (общие правила, применимы ко всем серверам GTA5RP) ---
PROJECT_SERVER_ID = "gta5rp_rules_project"
PROJECT_DISPLAY = "Правила проекта GTA5RP"
PROJECT_TOPICS = [
    "https://forum.gta5rp.com/threads/pravila-proekta.652405/",
]

# --- Слой сервера Davis (пилот). Категория 1219 + её 4 сабфорума. ---
# parse-section возвращает темы ТЕКУЩЕЙ страницы раздела, но НЕ обходит сабфорумы, поэтому
# перечисляем и категорию (её 4 прямых треда), и каждый сабфорум явно.
DAVIS_SERVER_ID = "gta5rp_rules_davis"
DAVIS_DISPLAY = "Правила Davis (GTA5RP)"
DAVIS_SECTIONS = [
    "https://forum.gta5rp.com/forums/pravila-servera-davis.1219/",                   # 4 прямых треда категории
    "https://forum.gta5rp.com/forums/obschie-pravila.1220/",                         # Общие правила
    "https://forum.gta5rp.com/forums/pravila-dlja-gosudarstvennyx-organizacii.1221/",# Гос. организации
    "https://forum.gta5rp.com/forums/pravila-dlja-kriminalnyx-organizacii.1222/",    # Крим. организации
    "https://forum.gta5rp.com/forums/pravila-neoficialnyx-organizacii.1223/",        # Неофициальные организации
]


async def ensure_server_row(server_id: str, display: str) -> None:
    """Свод-сервер (project_id NULL — самостоятельный namespace). Нужен для FK law_documents.server_id."""
    await db_manager.execute(
        "INSERT INTO servers (server_id, project_id, display_name, is_disabled) "
        "VALUES ($1, NULL, $2, false) ON CONFLICT (server_id) DO NOTHING",
        server_id, display,
    )
    log.info("servers: %s («%s») готов", server_id, display)


async def _save_topic(server_id: str, url: str, idx: int, total: int) -> bool:
    """Спарсить тему (только посты автора) и сохранить в law_documents[server_id]. True если сохранено."""
    try:
        data = await remote_parse_topic(url, parse_only_author=True)
    except Exception as e:
        log.warning("  [%d/%d] парс упал %s: %s", idx, total, url, e)
        return False
    if not data:
        log.warning("  [%d/%d] пусто %s", idx, total, url)
        return False
    title = (data.get("title") or "").strip() or url
    content = (data.get("content") or "").strip()
    thread_id = data.get("thread_id")
    content_hash = data.get("content_hash") or ""
    if not content or thread_id is None:
        log.warning("  [%d/%d] нет content/thread_id %s", idx, total, url)
        return False
    await save_law_document_to_db(server_id, int(thread_id), url, title, content, content_hash)
    log.info("  [%d/%d] сохранено: %s (%d симв.)", idx, total, title[:60], len(content))
    return True


async def scrape_topics(server_id: str, topic_urls: list[str]) -> int:
    """Прямые темы (слой проекта): парсим каждый URL как topic."""
    saved = 0
    for i, url in enumerate(topic_urls, 1):
        if await _save_topic(server_id, url, i, len(topic_urls)):
            saved += 1
    return saved


async def scrape_sections(server_id: str, section_urls: list[str]) -> int:
    """Разделы форума (слой сервера): для каждого раздела получаем темы и парсим их.
    Дедуп по URL — один тред может засветиться и в категории, и в сабфоруме."""
    saved = 0
    seen: set[str] = set()
    for sec in section_urls:
        try:
            urls = await remote_parse_section(sec)
        except Exception as e:
            log.warning("раздел %s → ошибка: %s", sec, e)
            continue
        fresh = [u for u in urls if u not in seen]
        seen.update(fresh)
        log.info("раздел %s → %d тем (%d новых)", sec, len(urls), len(fresh))
        for i, url in enumerate(fresh, 1):
            if await _save_topic(server_id, url, i, len(fresh)):
                saved += 1
    return saved


def _init_vertex() -> None:
    """Инициализация genai-клиента (эмбеддинги gemini-embedding-001) — образец из ingest_rules.py."""
    import config
    from rate_limiter import init_client
    from services.sa_admin import project_id_for
    init_client(
        project_id=project_id_for(config.VERTEX_CREDENTIALS_FILE),
        credentials_file=config.VERTEX_CREDENTIALS_FILE,
        region=config.GCP_REGION,
    )
    log.info("Vertex genai-клиент инициализирован")


async def ingest_layer(server_id: str, display: str, *, topics=None, sections=None) -> None:
    """Один namespace: ensure server row → скрейп → индексация (эмбеддинги)."""
    await ensure_server_row(server_id, display)
    docs = 0
    if topics:
        docs += await scrape_topics(server_id, topics)
    if sections:
        docs += await scrape_sections(server_id, sections)
    log.info("СКРЕЙП [%s]: сохранено документов=%d", server_id, docs)
    if docs == 0:
        log.error("0 документов в [%s] — индексацию пропускаю", server_id)
        return
    log.info("ИНДЕКСАЦИЯ [%s] (эмбеддинги, может быть долго из-за квоты)…", server_id)
    res = await index_server_laws(server_id)
    log.info("ГОТОВО [%s]: документов=%s, чанков=%s",
             server_id, res.get("documents"), res.get("chunks"))


async def main(only: str) -> None:
    _init_vertex()
    await db_manager.connect()
    try:
        if not await check_remote_parser_available():
            log.error("парсер-воркер недоступен — прерываю (проверь PARSER_WORKER_URL/SECRET)")
            return
        if only in ("all", "project"):
            await ingest_layer(PROJECT_SERVER_ID, PROJECT_DISPLAY, topics=PROJECT_TOPICS)
        if only in ("all", "davis"):
            await ingest_layer(DAVIS_SERVER_ID, DAVIS_DISPLAY, sections=DAVIS_SECTIONS)
    finally:
        await db_manager.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ингест правил GTA5RP (пилот Davis) в своды Обзвона.")
    ap.add_argument("--only", choices=["all", "project", "davis"], default="all",
                    help="какой слой ингестить (по умолчанию оба)")
    args = ap.parse_args()
    asyncio.run(main(args.only))
