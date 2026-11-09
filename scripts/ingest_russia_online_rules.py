#!/usr/bin/env python3
"""
Ингест правил «Россия Онлайн» в своды law_chunks для режима «Обзвон».

Четыре слоя-namespace (project_id NULL — отдельные от игровой законки; gemini_file_uri НЕ
ставим → своды не всплывают в пикере серверов оверлея):
  • russia_online_rules_project     — общие правила проекта (3 раздела форума);
  • russia_online_rules_arbat       — правила Арбатского (Форт/военная база/поставки);
  • russia_online_rules_tverskoy    — правила Тверского;
  • russia_online_rules_kutuzovsky  — правила Кутузовского.

Обзвон ретривит UNION проектного слоя и слоя выбранного сервера — как на GTA5RP,
см. LEADERSHIP_PROJECTS в law_sources_data.py и leadership-ветку advice_service.

⚠️ forum.russia.online закрыт логином целиком: parser-worker должен быть поднят с
FORUM_RUSSIA_ONLINE_LOGIN/PASSWORD, иначе разделы вернут стену авторизации.

ЗАПУСК (в live-контейнере с БД + парсер-воркером + VERTEX_CREDENTIALS_FILE):
  docker cp scripts/ingest_russia_online_rules.py <api>:/app/scripts/ \
    && docker exec <api> python /app/scripts/ingest_russia_online_rules.py
Идемпотентно: law_documents upsert по (server_id, source_thread_id); index_server_laws пере-чанкит.

Порционность: индексация каждого слоя — отдельный эмбеддинг-прогон (gemini-embedding-001,
квота ~10-12 req/min). Можно гнать по одному слою через --only project|arbat|tverskoy|kutuzovsky.
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
log = logging.getLogger("ingest_russia_online_rules")

BASE = "https://forum.russia.online"

# Слой проекта — общие правила, применимы ко всем трём серверам.
PROJECT_LAYER = {
    "server_id": "russia_online_rules_project",
    "display": "Правила проекта Россия Онлайн",
    "sections": [
        f"{BASE}/forums/general-server-rules/",              # 11 тем: основные, ограбления, лидеры…
        f"{BASE}/forums/rules-of-criminal-organizations/",   # правила криминальных организаций
        f"{BASE}/forums/rules-of-state-organizations/",      # правила государственных организаций
    ],
}

# Серверные слои — «Дополнительные серверные правила» (Форт / военная база / Air Drop / поставки).
SERVER_LAYERS = {
    "arbat": {
        "server_id": "russia_online_rules_arbat",
        "display": "Правила Арбатского (Россия Онлайн)",
        "sections": [f"{BASE}/forums/arbat-skii.1523/"],
    },
    "tverskoy": {
        "server_id": "russia_online_rules_tverskoy",
        "display": "Правила Тверского (Россия Онлайн)",
        "sections": [f"{BASE}/forums/tverskoi.1630/"],
    },
    "kutuzovsky": {
        "server_id": "russia_online_rules_kutuzovsky",
        "display": "Правила Кутузовского (Россия Онлайн)",
        "sections": [f"{BASE}/forums/kutuzovskii.1631/"],
    },
}


async def ensure_server_row(server_id: str, display: str) -> None:
    """Свод-сервер (project_id NULL — самостоятельный namespace). Нужен для FK law_documents.server_id."""
    await db_manager.execute(
        "INSERT INTO servers (server_id, project_id, display_name, is_disabled) "
        "VALUES ($1, NULL, $2, false) ON CONFLICT (server_id) DO NOTHING",
        server_id, display,
    )
    log.info("servers: %s («%s») готов", server_id, display)


async def _save_topic(server_id: str, url: str, idx: int, total: int) -> bool:
    """Спарсить тему (только посты автора) и сохранить в law_documents[server_id]."""
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
    if title.strip().lower().startswith("вход"):
        # Стена авторизации: без кред воркера раздел отдаёт логин-страницу, а не правила.
        log.error("  [%d/%d] СТЕНА ЛОГИНА на %s — проверь FORUM_RUSSIA_ONLINE_LOGIN/PASSWORD "
                  "у parser-worker", idx, total, url)
        return False
    await save_law_document_to_db(server_id, int(thread_id), url, title, content, content_hash)
    log.info("  [%d/%d] сохранено: %s (%d симв.)", idx, total, title[:60], len(content))
    return True


async def scrape_sections(server_id: str, section_urls: list[str]) -> int:
    """Разделы форума: для каждого получаем список тем и парсим их. Дедуп по URL."""
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
    """Инициализация genai-клиента (эмбеддинги gemini-embedding-001) — образец ingest_rules.py."""
    import config
    from rate_limiter import init_client
    from services.sa_admin import project_id_for
    init_client(
        project_id=project_id_for(config.VERTEX_CREDENTIALS_FILE),
        credentials_file=config.VERTEX_CREDENTIALS_FILE,
        region=config.GCP_REGION,
    )
    log.info("Vertex genai-клиент инициализирован")


async def ingest_layer(layer: dict) -> None:
    """Один namespace: ensure server row → скрейп → индексация (эмбеддинги)."""
    server_id, display = layer["server_id"], layer["display"]
    await ensure_server_row(server_id, display)
    docs = await scrape_sections(server_id, layer["sections"])
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
            await ingest_layer(PROJECT_LAYER)
        for key, layer in SERVER_LAYERS.items():
            if only in ("all", key):
                await ingest_layer(layer)
    finally:
        await db_manager.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ингест правил «Россия Онлайн» в своды Обзвона.")
    ap.add_argument("--only", choices=["all", "project", *SERVER_LAYERS], default="all",
                    help="какой слой ингестить (по умолчанию все)")
    args = ap.parse_args()
    asyncio.run(main(args.only))
