# START OF FILE knowledge_base_manager.py

import asyncio
import re
import traceback
import hashlib
import logging
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit, urlunsplit

# Сторонние библиотеки
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
# Импорт для работы с Google Cloud Storage
from google.cloud import storage
from google.api_core import exceptions as google_exceptions

# Локальные импорты
import config
from database.manager import db_manager
# ИЗМЕНЕНИЕ: Импортируем нашу новую функцию
from utils import get_enabled_servers, reinitialize_vertex_ai

# Импорт клиента удалённого парсера
from remote_parser_client import (
    remote_discover_section,
    remote_parse_topic,
    check_remote_parser_available,
    close_parser_client
)

# >>> НАСТРОЙКИ ПАРСЕРА <<<
# True = использовать удалённый сервер для парсинга
# False = парсить локально (как раньше)
USE_REMOTE_PARSER = True

# >>> НАЧАЛО ИЗМЕНЕНИЙ: КОНСТАНТА ДЛЯ УПРАВЛЕНИЯ ПАРАЛЛЕЛИЗАЦИЕЙ <<<
# Определяет, сколько серверов могут обновляться ОДНОВРЕМЕННО.
# Увеличение ускоряет процесс, но создает большую нагрузку.
# Рекомендуемые значения: 3-5
CONCURRENT_UPDATES = 4

# A document sync holds one session-level advisory-lock connection while its
# body uses ordinary short-lived pool connections.  Cap *all* entry points,
# including manual admin tasks, below the pool size so different servers cannot
# collectively pin every connection and deadlock while waiting for one more.
_LAW_DOCUMENT_SYNC_CONNECTION_SLOTS = asyncio.Semaphore(
    max(1, CONCURRENT_UPDATES)
)


@asynccontextmanager
async def _law_document_server_sync_lock(
    server_id: str,
    local_lock: asyncio.Lock,
):
    """Serialize one server's document sync locally and across processes.

    The PostgreSQL lock is session-scoped because parser and GCS work spans
    several short database transactions.  Its namespace is separate from the
    catalog lock, which may still be acquired later inside this context.
    """

    # Lazy import avoids services.__init__ -> qa_service -> this module while
    # knowledge_base_manager itself is still being initialized.
    from services.law_document_lock import law_document_server_lock

    async with law_document_server_lock(
        server_id,
        local_lock=local_lock,
        connection_slots=_LAW_DOCUMENT_SYNC_CONNECTION_SLOTS,
        manager=db_manager,
    ):
        yield


# >>> КОНЕЦ ИЗМЕНЕНИЙ <<<


async def notify_super_admin(message: str, reply_markup: dict | None = None):
    """
    Отправляет уведомление супер-админу через Telegram API напрямую.
    Используется для критических уведомлений из knowledge_base_manager.
    """
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": config.SUPER_ADMIN_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=10.0)
            if response.status_code == 200:
                logging.info(f"[KBManager] Уведомление отправлено супер-админу")
            else:
                logging.error(f"[KBManager] Ошибка отправки уведомления: {response.status_code}")
    except Exception as e:
        logging.error(f"[KBManager] Не удалось отправить уведомление супер-админу: {e}")


def extract_thread_id_from_url(url: str) -> int | None:
    """
    Извлекает ID только из XenForo-пути /threads/slug.ID.

    Устойчиво к query/fragment и хвостам /unread, /latest, /page-N, /post-N.
    Цифры из других URL намеренно не принимаются: ложный thread_id опасен для
    destructive cleanup.
    """
    match = re.search(
        r'(?:^|/)threads/[^/?#]*\.(\d+)(?:[/?#]|$)',
        str(url or ''),
        re.IGNORECASE,
    )
    if match:
        return int(match.group(1))
    logging.warning(f"Не удалось извлечь thread_id из URL: {url}")
    return None


def canonical_thread_url(url: str) -> str | None:
    """Канонический URL темы без навигационных хвостов/query/fragment."""
    raw = str(url or '')
    thread_id = extract_thread_id_from_url(raw)
    if thread_id is None:
        return None
    parts = urlsplit(raw)
    match = re.search(
        r'^(.*?/threads/[^/?#]*\.' + re.escape(str(thread_id)) + r')(?:/.*)?$',
        parts.path,
        re.IGNORECASE,
    )
    if not match:
        return None
    return urlunsplit((parts.scheme, parts.netloc, match.group(1) + '/', '', ''))


def canonical_thread_urls(source_urls) -> dict[int, str]:
    """Дедуплицирует варианты одного thread по ID и выбирает URL детерминированно."""
    grouped: dict[int, list[str]] = {}
    for url in source_urls:
        canonical = canonical_thread_url(url)
        if canonical is None:
            continue
        thread_id = extract_thread_id_from_url(canonical)
        if thread_id is not None:
            grouped.setdefault(thread_id, []).append(canonical)
    return {
        thread_id: sorted(set(urls), key=lambda value: (value.casefold(), value))[0]
        for thread_id, urls in grouped.items()
    }


def discovered_thread_ids(source_urls) -> set[int]:
    """Thread ID всех ссылок, найденных в разделе форума.

    Набор для удаления должен строиться по discovery, а не по результату
    скачивания каждой темы. Иначе временная ошибка topic-parser выглядит как
    удаление закона с форума и каскадно стирает его статьи/избранное.
    """
    return set(canonical_thread_urls(source_urls))


def filter_discovered_thread_urls(source_urls, excluded_urls) -> tuple[dict[int, str], int]:
    """Канонизирует discovery и применяет exclusions по thread ID, не сырой строке."""
    discovered = canonical_thread_urls(source_urls)
    excluded_ids = discovered_thread_ids(excluded_urls)
    removed = set(discovered) & excluded_ids
    return (
        {
            thread_id: url
            for thread_id, url in discovered.items()
            if thread_id not in excluded_ids
        },
        len(removed),
    )


def validated_discovery_thread_urls(
    hrefs,
    *,
    page_url: str,
    section_url: str,
) -> set[str]:
    """Fail-closed validation of every thread-like anchor on a discovery page."""
    expected_host = (urlsplit(section_url).hostname or "").casefold()
    result: set[str] = set()
    candidates = 0
    for href in hrefs:
        if not isinstance(href, str) or "/threads/" not in href.casefold():
            continue
        candidates += 1
        absolute_url = urljoin(page_url, href)
        actual_host = (urlsplit(absolute_url).hostname or "").casefold()
        if not expected_host or actual_host != expected_host:
            raise RuntimeError(
                f"cross-origin thread URL in discovery: {absolute_url}"
            )
        canonical = canonical_thread_url(absolute_url)
        if canonical is None:
            raise RuntimeError(
                f"unrecognised thread URL in discovery: {absolute_url}"
            )
        result.add(canonical)
    if candidates == 0 or not result:
        raise RuntimeError(
            "discovery page contains no recognised topic URLs"
        )
    return result


def _normalize_post_overrides(server_config: dict) -> dict[int, dict]:
    """
    Преобразует server_config["thread_post_overrides"] в map {thread_id: override_dict}.

    Поддерживаемые форматы ключей:
        - URL темы (string) -> thread_id извлекается автоматически
        - thread_id (int)
        - thread_id в виде строки

    Поддерживаемые значения override:
        {"first_n": N}            -> парсить только первые N постов (любого автора)
        {"post_ids": [int, ...]}  -> парсить только посты с этими XenForo post-id
        {"post_urls": [url, ...]} -> то же, но id извлекается из ссылок вида .../post-12345
    """
    raw = server_config.get("thread_post_overrides") or {}
    if not isinstance(raw, dict):
        return {}

    result: dict[int, dict] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        thread_id: int | None = None
        if isinstance(key, int):
            thread_id = key
        elif isinstance(key, str):
            if key.isdigit():
                thread_id = int(key)
            else:
                thread_id = extract_thread_id_from_url(key)
        if thread_id is None:
            logging.warning(f"  [Overrides] Не удалось определить thread_id для ключа: {key!r}")
            continue

        normalized = _normalize_override_value(value)
        if normalized:
            result[thread_id] = normalized

    return result


def _normalize_override_value(value) -> dict | None:
    """Нормализует одно значение override ({"first_n"/"post_ids"/"post_urls"}) или None."""
    if not isinstance(value, dict):
        return None

    normalized: dict = {}
    first_n = value.get("first_n")
    if isinstance(first_n, int) and first_n > 0:
        normalized["first_n"] = first_n

    post_ids: list[int] = []
    for pid in value.get("post_ids") or []:
        try:
            post_ids.append(int(pid))
        except (TypeError, ValueError):
            continue
    for purl in value.get("post_urls") or []:
        if not isinstance(purl, str):
            continue
        m = re.search(r'/post-(\d+)', purl)
        if m:
            post_ids.append(int(m.group(1)))
    if post_ids:
        normalized["post_ids"] = sorted(set(post_ids))

    return normalized or None


async def fetch_gdoc_content(doc_id: str) -> str | None:
    """
    Загружает текстовое содержимое Google документа по его ID.
    """
    export_url = f'https://docs.google.com/document/d/{doc_id}/export?format=txt'
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            logging.info(f"  [GDoc] Загружаю Google Doc с ID: {doc_id}")
            response = await client.get(export_url, timeout=30.0)
            response.raise_for_status()
            response.encoding = 'utf-8'
            return response.text
    except Exception as e:
        logging.error(f"  [GDoc] Не удалось загрузить Google Doc с ID {doc_id}", exc_info=e)
        return None


async def save_law_document_to_db(
        server_id: str,
        thread_id: int,
        source_url: str,
        title: str,
        content: str,
        content_hash: str
):
    """
    Сохраняет или обновляет закон в таблице `law_documents`.
    """
    query = """
            INSERT INTO law_documents (server_id, source_thread_id, source_url, title, content, content_hash, \
                                       derived_sync_pending, last_updated)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, NOW()) ON CONFLICT (server_id, source_thread_id) DO \
            UPDATE SET
                source_url = EXCLUDED.source_url, \
                title = EXCLUDED.title, \
                content = EXCLUDED.content, \
                content_hash = EXCLUDED.content_hash, \
                gemini_file_uri = NULL, \
                derived_sync_pending = TRUE, \
                last_updated = NOW()
            """
    await db_manager.execute(query, server_id, thread_id, source_url, title, content, content_hash)
    logging.info(
        f"Документ '{title}' (ID: {thread_id}) сохранен/обновлен в БД с хешем {content_hash[:8]}...")


def _law_document_gcs_location(server_id: str, thread_id: int) -> tuple[str, str]:
    """Return the one canonical blob name/URI used for a forum thread."""

    file_hash_name = hashlib.md5(str(thread_id).encode("utf-8")).hexdigest()
    blob_name = f"{server_id}/{file_hash_name}.txt"
    return blob_name, f"gs://{config.active_gcs_bucket()}/{blob_name}"


async def _run_storage_call_cancellation_safe(func, /, *args, **kwargs):
    """Run one blocking GCS call without releasing sync locks too early.

    Cancelling ``asyncio.to_thread`` only cancels the awaiter; the underlying
    thread keeps running.  We therefore shield and drain the real operation
    before re-raising cancellation.  The caller's per-server advisory lock stays
    held until the upload/delete can no longer overwrite a newer sync.
    """

    worker = asyncio.create_task(
        asyncio.to_thread(func, *args, **kwargs)
    )
    cancellation_requested = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            cancellation_requested = True
            if worker.done():
                break
            # A second cancellation must not let the still-running thread escape
            # the surrounding advisory-lock lifetime either.
            continue
        except Exception:
            if cancellation_requested:
                logging.exception(
                    "GCS-операция завершилась ошибкой после отмены задачи."
                )
                raise asyncio.CancelledError()
            raise

    if cancellation_requested:
        if worker.done() and not worker.cancelled():
            try:
                worker.result()
            except Exception:
                logging.exception(
                    "GCS-операция завершилась ошибкой после отмены задачи."
                )
        raise asyncio.CancelledError()
    return result


async def _persist_law_document_and_gcs(
    *,
    server_id: str,
    thread_id: int,
    source_url: str,
    title: str,
    content: str,
    content_hash: str,
    existing_hash: str | None,
    existing_title: str | None,
    existing_source_url: str | None,
    existing_gcs_uri: str | None,
    force_update: bool,
    bucket: storage.Bucket,
) -> tuple[bool, bool]:
    """Persist one parsed topic and attest that its canonical GCS object is ready.

    Returns ``(document_changed, storage_repaired)``.  Document identity
    includes title and source URL as well as content: the GCS object embeds the
    title, and both metadata fields feed downstream derived data.  A matching
    content hash alone is therefore not enough to skip work.

    Before a repair upload, the old URI attestation is cleared with a
    compare-and-set update.  An upload or final URI-update failure consequently
    leaves an explicit retryable NULL instead of a stale URI.
    """

    blob_name, expected_gcs_uri = _law_document_gcs_location(
        server_id,
        thread_id,
    )
    document_changed = (
        force_update
        or not existing_hash
        or existing_hash != content_hash
        or existing_title != title
        or existing_source_url != source_url
    )
    storage_ready = existing_gcs_uri == expected_gcs_uri
    if not document_changed and storage_ready:
        return False, False

    if document_changed:
        await save_law_document_to_db(
            server_id,
            thread_id,
            source_url,
            title,
            content,
            content_hash,
        )
    else:
        invalidated_document_id = await db_manager.fetchval(
            """
            UPDATE law_documents
            SET gemini_file_uri = NULL,
                derived_sync_pending = TRUE
            WHERE server_id = $1
              AND source_thread_id = $2
              AND content_hash IS NOT DISTINCT FROM $3
              AND title IS NOT DISTINCT FROM $4
              AND source_url IS NOT DISTINCT FROM $5
              AND gemini_file_uri IS NOT DISTINCT FROM $6
            RETURNING id
            """,
            server_id,
            thread_id,
            content_hash,
            title,
            source_url,
            existing_gcs_uri,
        )
        if invalidated_document_id is None:
            raise RuntimeError(
                "Law document changed concurrently before GCS repair: "
                f"server={server_id!r}, thread_id={thread_id}"
            )
        logging.warning(
            "  [GCS] Документ '%s' (ID: %s) совпадает по hash, но его "
            "канонический GCS URI не подтверждён. Повторяю upload.",
            title,
            thread_id,
        )

    blob = bucket.blob(blob_name)
    file_content_for_upload = f"НАЗВАНИЕ ДОКУМЕНТА: {title}\n\n{content}"
    await _run_storage_call_cancellation_safe(
        blob.upload_from_string,
        file_content_for_upload,
        content_type="text/plain",
    )
    persisted_document_id = await db_manager.fetchval(
        """
        UPDATE law_documents
        SET gemini_file_uri = $1
        WHERE server_id = $2
          AND source_thread_id = $3
          AND content_hash IS NOT DISTINCT FROM $4
          AND title IS NOT DISTINCT FROM $5
          AND source_url IS NOT DISTINCT FROM $6
          AND gemini_file_uri IS NULL
        RETURNING id
        """,
        expected_gcs_uri,
        server_id,
        thread_id,
        content_hash,
        title,
        source_url,
    )
    if persisted_document_id is None:
        raise RuntimeError(
            "Parsed law document disappeared before its GCS URI was saved: "
            f"server={server_id!r}, thread_id={thread_id}"
        )
    return document_changed, not storage_ready


async def reconcile_missing_thread_documents(*args, **kwargs):
    """Lazy bridge which avoids the legacy ``services`` package import cycle."""

    from services.law_document_handoff import (
        reconcile_missing_thread_documents as reconcile,
    )

    return await reconcile(*args, **kwargs)


async def get_law_urls_from_section(context, section_url: str) -> list[str]:
    """
    Заходит на страницу раздела форума и собирает все ссылки на темы (законы),
    автоматически переходя по страницам пагинации.
    Использует context для создания новых страниц при ошибках.
    """
    logging.info(f"  [Discovery] Начинаю сбор ссылок из раздела: {section_url}")
    all_urls = set()
    current_page_url = section_url
    page_num = 1

    # Создаём страницу для этой функции
    page = await context.new_page()

    try:
        while current_page_url:
            try:
                logging.info(f"    -> Сканирую страницу {page_num}: {current_page_url}")

                # Retry логика с пересозданием страницы при ошибке
                response = None
                for attempt in range(3):
                    try:
                        response = await page.goto(current_page_url, timeout=45000, wait_until='commit')
                        await asyncio.sleep(2)  # Пауза после загрузки для стабильности

                        # Проверяем что страница действительно загрузилась
                        content = await page.content()
                        if len(content) < 1000:
                            logging.warning(f"    -> Страница загрузилась но контента мало ({len(content)} байт), повторяю...")
                            raise Exception("Empty or minimal content received")
                        break
                    except Exception as retry_e:
                        logging.warning(f"    -> Попытка {attempt + 1}/3 провалилась: {str(retry_e)[:100]}")
                        if attempt < 2:
                            logging.info(f"    -> Пересоздаю страницу и жду 5 сек...")
                            # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: закрываем старую страницу и создаём новую
                            try:
                                await page.close()
                            except:
                                pass
                            await asyncio.sleep(5)
                            page = await context.new_page()
                        else:
                            raise retry_e

                # Ждём появления контента (ссылки на темы)
                try:
                    await page.wait_for_selector('div.structItem-title a', timeout=20000)
                except Exception:
                    await asyncio.sleep(3)

                if response is None or response.status != 200:
                    status_code = response.status if response is not None else "no-response"
                    raise RuntimeError(
                        f"discovery page returned non-200 status: {status_code}"
                    )

                link_selector = 'div.structItem-title a'
                link_elements = await page.query_selector_all(link_selector)
                logging.info(f"    -> Найдено ссылок на темы: {len(link_elements)}")
                if not link_elements:
                    raise RuntimeError(
                        "discovery page contains no topic links; completeness is unknown"
                    )

                page_hrefs = [
                    await element.get_attribute("href")
                    for element in link_elements
                ]
                all_urls.update(
                    validated_discovery_thread_urls(
                        page_hrefs,
                        page_url=current_page_url,
                        section_url=section_url,
                    )
                )

                next_button_selector = 'a.pageNav-jump--next'
                next_button = await page.query_selector(next_button_selector)

                if next_button:
                    next_page_href = await next_button.get_attribute('href')
                    current_page_url = urljoin(current_page_url, next_page_href)
                    page_num += 1
                    await asyncio.sleep(1)
                else:
                    logging.info("    -> Кнопка 'Следующая страница' не найдена. Завершаю сканирование раздела.")
                    current_page_url = None

            except Exception as e:
                if "TimeoutError" in str(e):
                    logging.warning(
                        f"  [Discovery] На странице {current_page_url} не найдено ссылок на законы. Возможно, раздел пуст или изменилась верстка.")
                else:
                    logging.error(f"  [Discovery] Ошибка при сканировании страницы {current_page_url}", exc_info=e)
                raise RuntimeError(
                    f"incomplete forum discovery at {current_page_url}"
                ) from e
    finally:
        # Закрываем страницу
        try:
            await page.close()
        except:
            pass

    logging.info(f"  [Discovery] Сбор завершен. Всего найдено {len(all_urls)} уникальных ссылок.")
    return list(all_urls)


# Хосты с чисткой разметки при извлечении текста (зеркало CONTENT_CLEANUP_HOSTS в
# parser_worker/worker.py — прод-путь идёт через воркер, этот локальный путь легаси-фолбэк).
# На RMRP первый пост закона начинается с TOC-оглавления в blockquote-цитате (каждая строка
# «Статья N.» стала бы статьёй-заглушкой), а заголовок темы склеивается с префиксом
# «ЗАКОНОДАТЕЛЬСТВО». Только для перечисленных хостов: глобальный вырез изменил бы
# content_hash существующих серверов. Троттлинга рейт-лимита здесь нет — он в воркере.
_CONTENT_CLEANUP_HOSTS = ('forum.rmrp.ru',)


def _needs_content_cleanup(url: str) -> bool:
    return (urlsplit(url).hostname or '').lower() in _CONTENT_CLEANUP_HOSTS


def _clean_topic_title(title_el, url: str) -> str:
    """Заголовок темы; на cleanup-хостах префикс (span.prefix) не склеивается с названием."""
    if _needs_content_cleanup(url):
        for prefix_el in title_el.select('span.prefix'):
            prefix_el.decompose()
        return re.sub(r'\s+', ' ', title_el.get_text(' ', strip=True)).strip()
    return title_el.get_text(strip=True)


def _post_text(content_el, url: str) -> str:
    """Текст поста; на cleanup-хостах вырезает цитаты (TOC-оглавления RMRP), спойлеры не трогает."""
    if _needs_content_cleanup(url):
        for quote_el in content_el.find_all('blockquote'):
            quote_el.decompose()
    return content_el.get_text(separator='\n', strip=True)


async def scrape_source(context, url: str, thread_creators: dict, parse_only_author: bool, post_override: dict | None = None) -> tuple[str, str, int] | None:
    """
    Универсальная функция для парсинга одного источника (URL) с поддержкой пагинации внутри темы.
    Использует context для создания новых страниц при ошибках.

    Параметр post_override (опционально) позволяет переопределить выбор постов для конкретной темы:
        {"first_n": N}            -> парсить только первые N постов (любого автора)
        {"post_ids": [int, ...]}  -> парсить только посты с указанными XenForo post-id
    Если override активен, флаг parse_only_author и проверка Google Doc игнорируются.
    """
    logging.info(f"  [Scraper] Начинаю парсинг: {url}")

    thread_id = extract_thread_id_from_url(url)
    if not thread_id:
        logging.error(f"  [Scraper] Не удалось извлечь thread_id из URL: {url}. Пропускаю.")
        return None

    # Создаём страницу для этого парсинга
    page = await context.new_page()

    # Нормализация post_override
    override_first_n: int | None = None
    override_post_ids: set[int] = set()
    if isinstance(post_override, dict):
        if isinstance(post_override.get("first_n"), int) and post_override["first_n"] > 0:
            override_first_n = post_override["first_n"]
        for pid in post_override.get("post_ids") or []:
            try:
                override_post_ids.add(int(pid))
            except (TypeError, ValueError):
                continue
    override_active = override_first_n is not None or bool(override_post_ids)
    if override_active:
        logging.info(
            f"  [Scraper][Override] Тема {thread_id}: first_n={override_first_n}, "
            f"post_ids={sorted(override_post_ids) if override_post_ids else None}"
        )

    try:
        # --- ШАГ 1: Загружаем первую страницу и извлекаем базовую информацию ---
        # Retry логика с пересозданием страницы при ошибке
        for attempt in range(3):
            try:
                await page.goto(url, timeout=45000, wait_until='commit')
                await asyncio.sleep(2)  # Пауза после загрузки для стабильности

                # Проверяем что страница действительно загрузилась
                content = await page.content()
                if len(content) < 1000:
                    logging.warning(f"  [Scraper] Страница загрузилась но контента мало ({len(content)} байт)")
                    raise Exception("Empty or minimal content received")
                break
            except Exception as retry_e:
                logging.warning(f"  [Scraper] Попытка {attempt + 1}/3 провалилась для {url}: {str(retry_e)[:100]}")
                if attempt < 2:
                    logging.info(f"  [Scraper] Пересоздаю страницу и жду 5 сек...")
                    # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: закрываем старую страницу и создаём новую
                    try:
                        await page.close()
                    except:
                        pass
                    await asyncio.sleep(5)
                    page = await context.new_page()
                else:
                    raise retry_e

        # Ждём появления контента страницы (заголовок темы)
        try:
            await page.wait_for_selector('h1.p-title-value', timeout=20000)
        except Exception:
            # Если заголовок не появился, пробуем подождать ещё
            await asyncio.sleep(3)

        initial_soup = BeautifulSoup(await page.content(), 'html.parser')

        title_element = initial_soup.select_one('h1.p-title-value')
        if not title_element:
            logging.warning(f"  [Scraper] Не найден заголовок на странице {url}. Пропускаю.")
            return None
        law_title = _clean_topic_title(title_element, url)
        logging.info(f"  [Scraper] Найден заголовок: '{law_title}' (ID: {thread_id})")

        # --- Проверка на Google Doc (пропускается при активном override) ---
        first_post_on_page = initial_soup.select_one('article.message')
        if (not override_active) and first_post_on_page and first_post_on_page.select_one('a[href*="docs.google.com/document/d/"]'):
            link_element = first_post_on_page.select_one('a[href*="docs.google.com/document/d/"]')
            gdoc_url = link_element.get('href')
            logging.info(f"    -> Найдена ссылка на Google Doc: {gdoc_url}")
            match = re.search(r'/document/d/([a-zA-Z0-9-_]+)', gdoc_url)
            if match:
                doc_id = match.group(1)
                content = await fetch_gdoc_content(doc_id)
                if content:
                    return law_title, content.strip(), thread_id
            # Если не удалось скачать GDoc, прекращаем обработку, т.к. он является основным источником
            return None

            # --- Логика парсинга темы с пагинацией ---
        logging.info(f"    -> GDoc не найден. Парсим тему. Режим 'Только автор': {parse_only_author}.")

        content_to_save = ""
        official_author = None
        collected_count = 0
        stop_collecting = False
        seen_post_ids: set[int] = set()

        # Определяем автора темы по первому посту на первой странице
        if parse_only_author and not override_active:
            # Используем thread_id как ключ для thread_creators, так как заголовок может меняться
            if thread_id not in thread_creators:
                author_element = first_post_on_page.select_one('a.username') if first_post_on_page else None
                if author_element:
                    thread_creators[thread_id] = author_element.get_text(strip=True)
                    logging.info(
                        f"    -> Определен автор темы '{law_title}' (ID: {thread_id}): {thread_creators[thread_id]}")
            official_author = thread_creators.get(thread_id)

        # --- ШАГ 2: Запускаем цикл для обхода всех страниц темы ---
        current_page_url = url
        page_num = 1

        while current_page_url:
            logging.info(f"    -> Сканирую страницу {page_num} темы: {current_page_url}")

            # Если это не первая страница, переходим на нее. Первую мы уже загрузили.
            if page_num > 1:
                for attempt in range(3):
                    try:
                        await page.goto(current_page_url, timeout=45000, wait_until='commit')
                        await asyncio.sleep(2)  # Пауза после загрузки

                        # Проверяем что страница загрузилась
                        content = await page.content()
                        if len(content) < 1000:
                            raise Exception("Empty or minimal content received")
                        break
                    except Exception as retry_e:
                        logging.warning(f"    -> Попытка {attempt + 1}/3 провалилась: {str(retry_e)[:100]}")
                        if attempt < 2:
                            logging.info(f"    -> Пересоздаю страницу и жду 5 сек...")
                            try:
                                await page.close()
                            except:
                                pass
                            await asyncio.sleep(5)
                            page = await context.new_page()
                        else:
                            raise retry_e
                try:
                    await page.wait_for_selector('article.message', timeout=20000)
                except Exception:
                    await asyncio.sleep(3)

            soup = BeautifulSoup(await page.content(), 'html.parser')
            all_posts_on_page = soup.select('article.message')

            if not all_posts_on_page:
                logging.warning(f"    -> На странице {page_num} не найдено постов.")
                break

            posts_found_on_page = 0
            for post in all_posts_on_page:
                # Скипаем highlighted / important-message блоки:
                # XenForo иногда дублирует выделенный/первый пост сверху с усечённым
                # текстом. Без этого фильтра в override-режиме first_n=2 мы взяли бы
                # обрезанный дубль вместо полного текста.
                classes = post.get('class') or []
                if 'message--highlighted' in classes:
                    continue
                if post.find_parent(class_='block-importantMessage') is not None:
                    continue

                # Берём только реальные посты XenForo: data-content="post-NNN".
                # В override-режиме это обязательно, чтобы не попасть на цитатный/превью блок.
                dc = post.get('data-content') or ''
                m_pid = re.match(r'^post-(\d+)$', dc)
                post_id = int(m_pid.group(1)) if m_pid else None
                if override_active and post_id is None:
                    continue
                if override_active and post_id in seen_post_ids:
                    continue

                if override_active:
                    should_parse_this_post = False
                    if override_first_n is not None:
                        if collected_count < override_first_n:
                            should_parse_this_post = True
                    elif override_post_ids:
                        if post_id in override_post_ids:
                            should_parse_this_post = True
                else:
                    should_parse_this_post = False
                    if not parse_only_author:
                        should_parse_this_post = True
                    elif official_author:
                        author_element = post.select_one('a.username')
                        if author_element and author_element.get_text(strip=True) == official_author:
                            should_parse_this_post = True

                if should_parse_this_post:
                    content_element = post.select_one('div.bbWrapper')
                    if content_element:
                        content_to_save += _post_text(content_element, url) + "\n\n"
                        posts_found_on_page += 1
                        if override_active and post_id is not None:
                            seen_post_ids.add(post_id)
                            collected_count += 1
                            if override_first_n is not None and collected_count >= override_first_n:
                                stop_collecting = True
                                break

            if posts_found_on_page > 0:
                logging.info(f"      -> Найдено и обработано {posts_found_on_page} релевантных постов.")

            if stop_collecting:
                logging.info(
                    f"    -> Достигнут лимит first_n={override_first_n} для темы '{law_title}'. Завершаю."
                )
                current_page_url = None
                break

            # --- ШАГ 3: Ищем кнопку "Следующая страница" и обновляем URL для следующей итерации ---
            next_button_selector = 'a.pageNav-jump--next'
            next_button = await page.query_selector(next_button_selector)

            if next_button:
                next_page_href = await next_button.get_attribute('href')
                current_page_url = urljoin(url, next_page_href)  # Используем базовый URL для надежности
                page_num += 1
                await asyncio.sleep(1)  # Небольшая задержка, чтобы не нагружать сервер
            else:
                logging.info(
                    f"    -> Кнопка 'Следующая страница' не найдена. Завершаю парсинг темы '{law_title}' (ID: {thread_id}).")
                current_page_url = None  # Выход из цикла

        if content_to_save:
            return law_title, content_to_save.strip(), thread_id
        else:
            logging.warning(
                f"  [Scraper] Не удалось извлечь контент для '{law_title}' (ID: {thread_id}) со страницы {url}.")
            return None

    except Exception as e:
        logging.error(f"  [Scraper] Критическая ошибка во время парсинга {url}", exc_info=e)
        return None
    finally:
        # Закрываем страницу
        try:
            await page.close()
        except:
            pass


async def delete_files_from_gcs(gcs_uris: list[str], bucket: storage.Bucket):
    """
    Асинхронно и конкурентно удаляет список файлов из Google Cloud Storage.
    """
    if not gcs_uris:
        return

    logging.info(f"  [GCS] Начинаю удаление {len(gcs_uris)} файлов из GCS...")

    async def _delete_blob(blob_name: str):
        """Внутренняя функция для удаления одного файла."""
        try:
            blob = bucket.blob(blob_name)
            # Запускаем блокирующую операцию в отдельном потоке
            await _run_storage_call_cancellation_safe(blob.delete)
            logging.info(f"    -> [GCS] Файл '{blob_name}' успешно удален.")
        except google_exceptions.NotFound:
            logging.warning(f"    -> [GCS] Файл '{blob_name}' не найден. Возможно, был удален ранее.")
        except Exception as e:
            logging.error(f"    -> [GCS] Ошибка при удалении файла '{blob_name}'.", exc_info=e)

    tasks = []
    for uri in gcs_uris:
        if not uri or not uri.startswith(f"gs://{config.active_gcs_bucket()}/"):
            logging.warning(f"  [GCS] Пропущен некорректный или пустой URI: '{uri}'")
            continue
        # Извлекаем имя файла (blob name) из URI
        blob_name = uri.replace(f"gs://{config.active_gcs_bucket()}/", "", 1)
        tasks.append(_delete_blob(blob_name))

    # Запускаем все задачи на удаление конкурентно
    if tasks:
        await asyncio.gather(*tasks)
    logging.info(f"  [GCS] Удаление файлов из GCS завершено.")


async def _unreferenced_law_document_gcs_uris(
    gcs_uris: list[str] | tuple[str, ...],
) -> list[str]:
    """Return only blobs no surviving law_document still references.

    ``gemini_file_uri`` is not unique in the legacy schema.  A malformed,
    migrated, or manually repaired row can therefore share the old document's
    URI.  Deleting such a blob after the old DB row commits would make the
    surviving row point to a missing object.  If the survivor check itself
    fails, delete nothing: a harmless orphan is safer than a broken live URI.
    """

    candidates = list(
        dict.fromkeys(
            str(uri)
            for uri in gcs_uris
            if isinstance(uri, str) and uri
        )
    )
    if not candidates:
        return []

    try:
        rows = await db_manager.fetch(
            """
            SELECT DISTINCT gemini_file_uri
            FROM law_documents
            WHERE gemini_file_uri = ANY($1::text[])
            """,
            candidates,
        )
    except Exception:
        logging.exception(
            "  [GCS] Не удалось проверить ссылки живых law_documents; "
            "удаление %s объектов пропущено безопасно.",
            len(candidates),
        )
        return []

    referenced = {
        str(row["gemini_file_uri"])
        for row in rows
        if row.get("gemini_file_uri")
    }
    if referenced:
        logging.warning(
            "  [GCS] Не удаляю %s объектов: на них всё ещё ссылаются "
            "живые law_documents.",
            len(referenced),
        )
    return [uri for uri in candidates if uri not in referenced]


async def update_knowledge_base_for_server(server_id: str, force_update: bool = False, bypass_protection: bool = False) -> bool:
    """
    Основная функция-оркестратор для "умной" СИНХРОНИЗАЦИИ БЗ одного сервера.
    """
    if not await reinitialize_vertex_ai():
        logging.critical(f"СИНХРОНИЗАЦИЯ БЗ ДЛЯ {server_id} ПРЕРВАНА: не удалось обновить токен Vertex AI.")
        return False

    logging.info(
        f"===== НАЧАЛО {'ПРИНУДИТЕЛЬНОЙ ' if force_update else ''}СИНХРОНИЗАЦИИ БЗ ДЛЯ СЕРВЕРА: {server_id} =====")

    lock = config.SERVER_UPDATE_LOCKS.get(server_id)
    if not lock:
        logging.error(f"Не найдена блокировка для сервера {server_id}! Обновление прервано.")
        return False

    async with _law_document_server_sync_lock(server_id, lock):
        try:
            server_config = config.SERVERS_CONFIG.get(server_id)
            if not server_config:
                logging.error(f"Конфигурация для сервера {server_id} не найдена.")
                return False

            storage_client = storage.Client.from_service_account_json(config.VERTEX_CREDENTIALS_FILE)
            bucket = storage_client.bucket(config.active_gcs_bucket())

            # Шаг 0: Удаление "записей-зомби" без source_thread_id
            zombie_records = await db_manager.fetch(
                "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND source_thread_id IS NULL",
                server_id
            )
            if zombie_records:
                logging.warning(f"Обнаружено {len(zombie_records)} 'записей-зомби' (без thread_id). Начинаю очистку...")
                zombie_uris = [rec['gemini_file_uri'] for rec in zombie_records if rec['gemini_file_uri']]
                if zombie_uris:
                    await delete_files_from_gcs(zombie_uris, bucket)

                # Тот же межпроцессный lock, что у catalog/related worker: каскадное
                # удаление law_articles не пересекается с пересборкой их графа.
                from services.law_embedding_service import catalog_server_lock
                async with catalog_server_lock(server_id):
                    await db_manager.execute(
                        "DELETE FROM law_documents WHERE server_id = $1 AND source_thread_id IS NULL",
                        server_id
                    )
                logging.info(f"Очистка 'записей-зомби' для сервера {server_id} завершена.")

            section_urls = server_config.get("forum_section_urls", [])
            if not section_urls:
                logging.warning(f"Список URL разделов форума пуст для {server_id}. Пропуск.")
                return True

            parse_only_author = server_config.get("parse_only_author_posts", True)
            post_overrides_map = _normalize_post_overrides(server_config)
            # Дефолтный override для тем БЕЗ точечной настройки (rmrp: новый закон в разделе
            # иначе парсился бы с changelog-постами до ручного добавления в overrides).
            default_post_override = _normalize_override_value(
                server_config.get("default_thread_post_override")
            )
            if post_overrides_map:
                logging.info(f"  [Overrides] Для сервера {server_id} заданы override'ы для {len(post_overrides_map)} тем.")
            if default_post_override:
                logging.info(f"  [Overrides] Дефолтный override сервера {server_id}: {default_post_override}")
            updated_docs_count = 0
            deleted_docs_count = 0
            discovered_forum_thread_ids: set[int] = set()
            parsed_thread_ids: set[int] = set()
            ready_thread_ids: set[int] = set()
            changed_thread_ids = set()  # доки, реально записанные в БД — кандидаты на переиндексацию law_chunks

            # === ВЫБОР РЕЖИМА ПАРСИНГА ===
            if USE_REMOTE_PARSER:
                # --- УДАЛЁННЫЙ ПАРСИНГ ---
                logging.info(f"  [Parser] Используется УДАЛЁННЫЙ парсер")

                # Проверяем доступность удалённого парсера
                if not await check_remote_parser_available():
                    logging.error(f"  [Parser] Удалённый парсер недоступен! Синхронизация прервана.")
                    return False

                # Собираем ссылки на все законы
                all_source_urls = set()
                for section_url in section_urls:
                    discovery = await remote_discover_section(section_url)
                    if not discovery.complete:
                        logging.error(
                            "  [Parser] Discovery раздела %s неполон (%s). "
                            "Синхронизация остановлена ДО destructive cleanup.",
                            section_url,
                            discovery.error or "unknown error",
                        )
                        return False
                    all_source_urls.update(discovery.urls)
                    logging.info(f"  [Parser] Из раздела получено {len(discovery.urls)} ссылок")

                logging.info(f"  [Parser] Всего найдено {len(all_source_urls)} уникальных ссылок на законы")

                # Канонизируем варианты /unread, query и fragment; exclusions
                # сравниваем по thread ID, а не по хрупкому сырому URL.
                excluded_urls = set(server_config.get("excluded_urls", []))
                discovered_urls, excluded_count = filter_discovered_thread_urls(
                    all_source_urls,
                    excluded_urls,
                )
                if excluded_count:
                    logging.info(f"  [Parser] Исключено {excluded_count} тем из парсинга (excluded_urls)")
                all_source_urls = set(discovered_urls.values())
                # Этот immutable-набор — единственный источник destructive cleanup.
                # Результат topic-parser не имеет права добавлять сюда чужой ID.
                discovered_forum_thread_ids.update(discovered_urls)

                # Получаем текущие данные из БД
                db_records = await db_manager.fetch(
                    "SELECT source_thread_id, source_url, title, content_hash, "
                    "gemini_file_uri, derived_sync_pending "
                    "FROM law_documents WHERE server_id = $1",
                    server_id)
                db_thread_records = {
                    r['source_thread_id']: dict(r)
                    for r in db_records
                    if r['source_thread_id'] is not None
                }
                db_thread_hashes = {
                    thread_id: record.get('content_hash')
                    for thread_id, record in db_thread_records.items()
                }
                logging.info(f"В БД для {server_id} найдено {len(db_thread_hashes)} существующих законов.")

                thread_creators = {}
                for thread_id_from_url, url in sorted(discovered_urls.items()):
                    try:
                        url_override = post_overrides_map.get(thread_id_from_url, default_post_override)
                        # Используем удалённый парсер для получения контента
                        scraped_data = await remote_parse_topic(url, parse_only_author, thread_creators, post_override=url_override)

                        if not scraped_data:
                            continue

                        law_title = scraped_data['title']
                        new_content = scraped_data['content']
                        thread_id = int(scraped_data['thread_id'])
                        if thread_id != thread_id_from_url:
                            logging.error(
                                "  [Parser] ID темы из ответа (%s) не совпал с discovery URL (%s): %s. "
                                "Документ не записан; discovery-ID защищён от удаления.",
                                thread_id,
                                thread_id_from_url,
                                url,
                            )
                            continue
                        new_hash = scraped_data.get('content_hash') or hashlib.md5(new_content.encode('utf-8')).hexdigest()

                        # Обновляем thread_creators из ответа
                        if 'thread_creators' in scraped_data:
                            thread_creators.update(scraped_data['thread_creators'])

                        if not new_content:
                            continue

                        parsed_thread_ids.add(thread_id)
                        existing_record = db_thread_records.get(thread_id, {})
                        old_hash_in_db = existing_record.get('content_hash')
                        document_will_change = (
                            force_update
                            or not old_hash_in_db
                            or old_hash_in_db != new_hash
                            or existing_record.get('title') != law_title
                            or existing_record.get('source_url') != url
                        )
                        if document_will_change:
                            logging.info(
                                f"!!! {'ПРИНУДИТЕЛЬНОЕ ОБНОВЛЕНИЕ' if force_update else 'ОБНАРУЖЕНО ИЗМЕНЕНИЕ'} "
                                f"в '{law_title}' (ID: {thread_id}). Запускаю обновление...")

                        content_changed, storage_repaired = await _persist_law_document_and_gcs(
                            server_id=server_id,
                            thread_id=thread_id,
                            source_url=url,
                            title=law_title,
                            content=new_content,
                            content_hash=new_hash,
                            existing_hash=old_hash_in_db,
                            existing_title=existing_record.get('title'),
                            existing_source_url=existing_record.get('source_url'),
                            existing_gcs_uri=existing_record.get('gemini_file_uri'),
                            force_update=force_update,
                            bucket=bucket,
                        )
                        # Готовность выставляется только после полного DB+GCS пути.
                        # Исключение выше оставит тему parsed, но не ready, и
                        # destructive cleanup будет отложен.
                        ready_thread_ids.add(thread_id)
                        # URI repair may be the retry after content/hash was
                        # committed but upload failed.  Reindex it as changed
                        # so the first failed attempt cannot leave stale
                        # law_chunks/law_articles indefinitely.
                        if (
                            content_changed
                            or storage_repaired
                            or existing_record.get('derived_sync_pending') is True
                        ):
                            changed_thread_ids.add(thread_id)
                        if content_changed or storage_repaired:
                            updated_docs_count += 1
                            logging.info(
                                f" -> Документ '{law_title}' (ID: {thread_id}) "
                                f"успешно {'обновлен' if content_changed else 'восстановлен в GCS'}.")
                            await asyncio.sleep(0.5)  # Небольшая пауза между запросами
                        else:
                            logging.info(f"'{law_title}' (ID: {thread_id}) не изменился. Пропускаю.")

                    except Exception as e:
                        logging.error(f"Критическая ошибка при обработке URL {url}", exc_info=e)

            else:
                # --- ЛОКАЛЬНЫЙ ПАРСИНГ (старый код) ---
                logging.info(f"  [Parser] Используется ЛОКАЛЬНЫЙ парсер")
                async with async_playwright() as p:
                    # Используем Chromium (совпадает с установленным в Dockerfile)
                    browser = await p.chromium.launch(
                        headless=True
                    )

                    # Полная эмуляция Windows 10 браузера
                    context = await browser.new_context(
                        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
                        viewport={'width': 1920, 'height': 1080},
                        locale='ru-RU',
                        timezone_id='Europe/Moscow',
                        ignore_https_errors=True,
                        extra_http_headers={
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                            'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
                            'Accept-Encoding': 'gzip, deflate, br',
                            'DNT': '1',
                            'Connection': 'keep-alive',
                            'Upgrade-Insecure-Requests': '1',
                            'Sec-Fetch-Dest': 'document',
                            'Sec-Fetch-Mode': 'navigate',
                            'Sec-Fetch-Site': 'none',
                            'Sec-Fetch-User': '?1',
                        }
                    )

                    # JavaScript для подмены navigator.platform
                    await context.add_init_script("""
                        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
                        Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    """)

                    context.set_default_timeout(60000)
                    context.set_default_navigation_timeout(60000)

                    # Собираем ссылки на все законы
                    all_source_urls = set()
                    for section_url in section_urls:
                        urls_from_section = await get_law_urls_from_section(context, section_url)
                        all_source_urls.update(urls_from_section)

                    # Та же канонизация/exclusion-by-ID, что и в remote-пути.
                    excluded_urls = set(server_config.get("excluded_urls", []))
                    discovered_urls, excluded_count = filter_discovered_thread_urls(
                        all_source_urls,
                        excluded_urls,
                    )
                    if excluded_count:
                        logging.info(f"  [Parser] Исключено {excluded_count} тем из парсинга (excluded_urls)")
                    all_source_urls = set(discovered_urls.values())
                    discovered_forum_thread_ids.update(discovered_urls)

                    db_records = await db_manager.fetch(
                        "SELECT source_thread_id, source_url, title, content_hash, "
                        "gemini_file_uri, derived_sync_pending "
                        "FROM law_documents WHERE server_id = $1",
                        server_id)
                    db_thread_records = {
                        r['source_thread_id']: dict(r)
                        for r in db_records
                        if r['source_thread_id'] is not None
                    }
                    db_thread_hashes = {
                        thread_id: record.get('content_hash')
                        for thread_id, record in db_thread_records.items()
                    }
                    logging.info(f"В БД для {server_id} найдено {len(db_thread_hashes)} существующих законов.")

                    thread_creators = {}
                    for thread_id_from_url, url in sorted(discovered_urls.items()):
                        try:
                            url_override = post_overrides_map.get(thread_id_from_url, default_post_override)
                            scraped_data = await scrape_source(context, url, thread_creators, parse_only_author, post_override=url_override)
                            if not scraped_data: continue
                            law_title, new_content, thread_id = scraped_data
                            thread_id = int(thread_id)
                            if thread_id != thread_id_from_url:
                                logging.error(
                                    "  [Parser] ID локально разобранной темы (%s) не совпал с discovery URL (%s): %s. "
                                    "Документ не записан.",
                                    thread_id,
                                    thread_id_from_url,
                                    url,
                                )
                                continue
                            if not new_content: continue
                            parsed_thread_ids.add(thread_id)
                            new_hash = hashlib.md5(new_content.encode('utf-8')).hexdigest()
                            existing_record = db_thread_records.get(thread_id, {})
                            old_hash_in_db = existing_record.get('content_hash')
                            document_will_change = (
                                force_update
                                or not old_hash_in_db
                                or old_hash_in_db != new_hash
                                or existing_record.get('title') != law_title
                                or existing_record.get('source_url') != url
                            )
                            if document_will_change:
                                logging.info(
                                    f"!!! {'ПРИНУДИТЕЛЬНОЕ ОБНОВЛЕНИЕ' if force_update else 'ОБНАРУЖЕНО ИЗМЕНЕНИЕ'} "
                                    f"в '{law_title}' (ID: {thread_id}). Запускаю обновление...")

                            content_changed, storage_repaired = await _persist_law_document_and_gcs(
                                server_id=server_id,
                                thread_id=thread_id,
                                source_url=url,
                                title=law_title,
                                content=new_content,
                                content_hash=new_hash,
                                existing_hash=old_hash_in_db,
                                existing_title=existing_record.get('title'),
                                existing_source_url=existing_record.get('source_url'),
                                existing_gcs_uri=existing_record.get('gemini_file_uri'),
                                force_update=force_update,
                                bucket=bucket,
                            )
                            ready_thread_ids.add(thread_id)
                            if (
                                content_changed
                                or storage_repaired
                                or existing_record.get('derived_sync_pending') is True
                            ):
                                changed_thread_ids.add(thread_id)
                            if content_changed or storage_repaired:
                                updated_docs_count += 1
                                logging.info(
                                    f" -> Документ '{law_title}' (ID: {thread_id}) "
                                    f"успешно {'обновлен' if content_changed else 'восстановлен в GCS'}.")
                                await asyncio.sleep(1)
                            else:
                                logging.info(f"'{law_title}' (ID: {thread_id}) не изменился. Пропускаю.")
                        except Exception as e:
                            logging.error(f"Критическая ошибка при обработке URL {url}", exc_info=e)
                    await browser.close()

            # --- ОБЩИЙ КОД: Удаление устаревших законов ---
            # ЗАЩИТА: Не удаляем законы, если парсинг не вернул ни одной ссылки
            # Это означает ошибку парсинга, а не реальное удаление всех законов с форума
            _bypass_markup = {
                "inline_keyboard": [[{
                    "text": "✅ Снять защиту и запустить повторно",
                    "callback_data": f"bypass_prot_{server_id}"
                }]]
            }
            if not discovered_forum_thread_ids and db_thread_hashes:
                if bypass_protection:
                    logging.warning(
                        f"  [KBManager] BYPASS: Парсинг вернул 0 ссылок, но bypass_protection=True. "
                        f"Будут удалены все {len(db_thread_hashes)} законов из БД.")
                else:
                    error_msg = (
                        f"⚠️ ЗАЩИТА ОТ УДАЛЕНИЯ: Парсинг не вернул ни одной ссылки, но в БД есть {len(db_thread_hashes)} законов. "
                        f"Вероятно, произошла ошибка парсинга. Удаление ОТМЕНЕНО для защиты данных.")
                    logging.error(error_msg)
                    await notify_super_admin(
                        f"🚨 <b>ЗАЩИТА БЗ СРАБОТАЛА</b>\n\n"
                        f"<b>Сервер:</b> <code>{server_id}</code>\n"
                        f"<b>Проблема:</b> Парсинг вернул 0 ссылок\n"
                        f"<b>Законов в БД:</b> {len(db_thread_hashes)}\n\n"
                        f"Удаление отменено. Нажмите кнопку, чтобы снять защиту и повторить.",
                        reply_markup=_bypass_markup
                    )
                    return False

            thread_ids_to_delete = set(db_thread_hashes.keys()) - discovered_forum_thread_ids
            not_ready_discovered_ids = discovered_forum_thread_ids - ready_thread_ids
            if thread_ids_to_delete and not_ready_discovered_ids:
                # При одновременной смене forum thread старая тема уже missing, а
                # новая может быть среди нескачанных или не полностью сохранённых.
                # Без успешного DB+GCS пути невозможно доказать, что это настоящее
                # удаление, а не replacement.
                logging.error(
                    "  [KBManager] destructive cleanup отложен: missing=%s, "
                    "discovered-but-not-ready=%s. Сначала нужен полный "
                    "topic parse и успешное сохранение DB+GCS.",
                    sorted(thread_ids_to_delete),
                    sorted(not_ready_discovered_ids),
                )
                return False
            if thread_ids_to_delete:
                # Дополнительная защита: не удалять, если удаляется больше 50% законов
                deletion_ratio = len(thread_ids_to_delete) / len(db_thread_hashes) if db_thread_hashes else 0
                if deletion_ratio > 0.5:
                    if bypass_protection:
                        logging.warning(
                            f"  [KBManager] BYPASS: Массовое удаление {len(thread_ids_to_delete)} из {len(db_thread_hashes)} "
                            f"законов ({deletion_ratio:.0%}) разрешено через bypass_protection.")
                    else:
                        error_msg = (
                            f"⚠️ ЗАЩИТА ОТ МАССОВОГО УДАЛЕНИЯ: Попытка удалить {len(thread_ids_to_delete)} из {len(db_thread_hashes)} законов "
                            f"({deletion_ratio:.0%}). Это подозрительно, удаление ОТМЕНЕНО. Проверьте парсер вручную.")
                        logging.error(error_msg)
                        await notify_super_admin(
                            f"🚨 <b>ЗАЩИТА БЗ СРАБОТАЛА</b>\n\n"
                            f"<b>Сервер:</b> <code>{server_id}</code>\n"
                            f"<b>Проблема:</b> Массовое удаление ({deletion_ratio:.0%})\n"
                            f"<b>К удалению:</b> {len(thread_ids_to_delete)} из {len(db_thread_hashes)}\n\n"
                            f"Удаление отменено. Нажмите кнопку, чтобы снять защиту и повторить.",
                            reply_markup=_bypass_markup
                        )
                        return False
                logging.warning(
                    f"Обнаружено {len(thread_ids_to_delete)} законов, удаленных с форума. ЗАПУСКАЮ УДАЛЕНИЕ...")
                from services.law_embedding_service import catalog_server_lock
                try:
                    # Один advisory lock + одна DB-транзакция: exact handoff,
                    # проверка его предупреждений и обычные удаления либо
                    # фиксируются вместе, либо полностью откатываются.
                    async with catalog_server_lock(server_id):
                        async with db_manager.transaction() as conn:
                            cleanup_result = await reconcile_missing_thread_documents(
                                conn,
                                server_id=server_id,
                                missing_thread_ids=thread_ids_to_delete,
                                current_thread_ids=ready_thread_ids,
                            )
                except Exception:
                    logging.exception(
                        "  [KBManager] destructive cleanup для server=%s "
                        "отменён и откатан; GCS не удалялся.",
                        server_id,
                    )
                    return False

                # DB уже committed, advisory lock освобождён. Теперь handoff target
                # можно безопасно переиндексировать, а старые GCS-объекты удалить.
                changed_thread_ids.update(cleanup_result.target_thread_ids)
                for removed in cleanup_result.removed_documents:
                    if removed.handoff_target_thread_id is not None:
                        logging.warning(
                            "  -> Стабильные article ID перенесены: '%s' "
                            "(thread %s -> %s).",
                            removed.title,
                            removed.thread_id,
                            removed.handoff_target_thread_id,
                        )
                    else:
                        logging.warning(
                            "  -> Подтверждено удаление: '%s' (thread %s).",
                            removed.title,
                            removed.thread_id,
                        )
                if cleanup_result.gcs_uris:
                    unreferenced_gcs_uris = (
                        await _unreferenced_law_document_gcs_uris(
                            cleanup_result.gcs_uris,
                        )
                    )
                    if unreferenced_gcs_uris:
                        await delete_files_from_gcs(
                            unreferenced_gcs_uris,
                            bucket,
                        )
                deleted_docs_count = len(cleanup_result.removed_documents)
                logging.warning(
                    "Успешно зафиксировано удаление %s старых документов из БД; "
                    "после commit завершена попытка очистки их GCS-объектов.",
                    deleted_docs_count,
                )
            else:
                logging.info("Удаленных законов не обнаружено. Удаление не требуется.")


            # --- Инкрементальная переиндексация RAG-индекса (law_chunks) изменённых документов ---
            # Удалённые доки чистятся каскадом FK; здесь докатываем изменённые/новые. Ошибка
            # переиндексации НЕ роняет синхронизацию БЗ — индекс догонится следующим циклом/вручную.
            reindexed_chunks = 0
            if changed_thread_ids:
                try:
                    from services.indexing_service import reindex_changed_documents
                    reindex_stats = await reindex_changed_documents(server_id, changed_thread_ids)
                    reindexed_chunks = reindex_stats.get("chunks", 0)
                except Exception as e:
                    logging.error(
                        f"Авто-переиндексация law_chunks для {server_id} не удалась "
                        f"(синхронизация БЗ продолжена).", exc_info=e)

            logging.info(
                f"===== СИНХРОНИЗАЦИЯ ДЛЯ {server_id} ЗАВЕРШЕНА. "
                f"Найдено тем: {len(discovered_forum_thread_ids)}, "
                f"успешно прочитано: {len(parsed_thread_ids)}, "
                f"Обновлено: {updated_docs_count}, Удалено: {deleted_docs_count}, "
                f"Переиндексировано чанков: {reindexed_chunks}. =====")

            # ПРИМЕЧАНИЕ: Синхронизация прецедентов выполняется ОТДЕЛЬНО через админ-панель
            # или команду sync_precedents, чтобы не замедлять обновление законов

            return True
        except Exception as e:
            logging.critical(f"КРИТИЧЕСКАЯ ОШИБКА во время синхронизации БЗ для {server_id}", exc_info=e)
            traceback.print_exc()
            return False


async def _update_server_with_semaphore(server_id: str, semaphore: asyncio.Semaphore,
                                        force_update: bool = False) -> str:
    """
    Вспомогательная функция-обертка для запуска обновления сервера под контролем семафора.
    """
    async with semaphore:
        logging.info(
            f"--- [CONCURRENT] Начинаю {'принудительное ' if force_update else ''}обновление для сервера: [{server_id}] ---")
        success = await update_knowledge_base_for_server(server_id, force_update=force_update)
        if success:
            logging.info(f"--- [CONCURRENT] Успешно завершено обновление для: [{server_id}] ---")
            return server_id
        else:
            logging.error(f"--- [CONCURRENT] Ошибка при обновлении сервера: [{server_id}] ---")
            raise RuntimeError(f"Failed to update server {server_id}")


async def update_all_servers_knowledge_base(force_update: bool = False) -> tuple[int, list]:
    """
    Запускает обновление БЗ для всех активных серверов КОНКУРЕНТНО.
    """
    update_type = "ПРИНУДИТЕЛЬНОГО" if force_update else "КОНКУРЕНТНОГО"
    logging.info(
        f"Менеджер БЗ: Запуск {update_type} глобального обновления для ВСЕХ активных серверов..."
    )
    enabled_servers = await get_enabled_servers()
    if not enabled_servers:
        logging.warning("Менеджер БЗ: Нет активных серверов для обновления.")
        return 0, []

    semaphore = asyncio.Semaphore(CONCURRENT_UPDATES)
    logging.info(f"Лимит одновременных обновлений установлен на: {CONCURRENT_UPDATES}")

    tasks = [_update_server_with_semaphore(server_id, semaphore, force_update) for server_id in enabled_servers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successful_servers = []
    failed_servers = []
    for i, result in enumerate(results):
        server_id = enabled_servers[i]
        if isinstance(result, Exception):
            failed_servers.append(server_id)
            logging.error(f"Результат глобального обновления для '{server_id}': ОШИБКА", exc_info=result)
        else:
            successful_servers.append(server_id)

    successful_updates = len(successful_servers)
    total_servers = len(enabled_servers)
    logging.info(f"Менеджер БЗ: Глобальное обновление завершено. Успешно: {successful_updates}/{total_servers}")
    if failed_servers:
        logging.error(f"  -> Обновление завершилось с ошибками на серверах: {', '.join(failed_servers)}")

    return successful_updates, failed_servers


async def sync_precedents_for_all_servers(genai_client=None, force_update: bool = False) -> tuple[int, list]:
    """
    Запускает синхронизацию прецедентов для всех серверов с настроенной системой прецедентов.

    Args:
        genai_client: Клиент Gemini API для генерации суммари
        force_update: Принудительное обновление всех прецедентов

    Returns:
        Кортеж (количество серверов с обновлёнными прецедентами, список серверов с ошибками)
    """
    from precedents_manager import sync_precedents_for_server

    logging.info("===== НАЧАЛО ГЛОБАЛЬНОЙ СИНХРОНИЗАЦИИ ПРЕЦЕДЕНТОВ =====")

    enabled_servers = await get_enabled_servers()
    servers_with_precedents = []

    # Фильтруем серверы с настроенной системой прецедентов
    for server_id in enabled_servers:
        server_config = config.SERVERS_CONFIG.get(server_id, {})
        if server_config.get('precedents_registry_url'):
            servers_with_precedents.append(server_id)

    if not servers_with_precedents:
        logging.info("Нет серверов с настроенной системой прецедентов")
        return 0, []

    logging.info(f"Найдено {len(servers_with_precedents)} серверов с системой прецедентов: {servers_with_precedents}")

    successful = 0
    failed = []

    for server_id in servers_with_precedents:
        try:
            updated_count = await sync_precedents_for_server(server_id, genai_client, force_update)
            if updated_count >= 0:
                successful += 1
                logging.info(f"[Precedents] Сервер {server_id}: обновлено {updated_count} прецедентов")
            else:
                failed.append(server_id)
        except Exception as e:
            logging.error(f"[Precedents] Ошибка синхронизации для {server_id}", exc_info=e)
            failed.append(server_id)

    logging.info(f"===== СИНХРОНИЗАЦИЯ ПРЕЦЕДЕНТОВ ЗАВЕРШЕНА. Успешно: {successful}/{len(servers_with_precedents)} =====")
    return successful, failed

# END OF FILE knowledge_base_manager.py
