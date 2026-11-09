# START OF FILE parser_worker/worker.py
"""
Удалённый Worker для парсинга форума.
Запускается на отдельном сервере где парсинг работает стабильно.

Запуск: uvicorn worker:app --host 0.0.0.0 --port 8765
"""

import asyncio
import base64
import logging
import os
import re
import time
import hashlib
from urllib.parse import urljoin, urlsplit, unquote
from typing import Optional
from contextlib import asynccontextmanager, AsyncExitStack

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from playwright.async_api import async_playwright, BrowserContext

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s'
)

# === КОНФИГУРАЦИЯ (из переменных окружения) ===
# HTTP прокси (опционально, если нужна на этом сервере тоже)
FORUM_HTTP_PROXY = os.environ.get('FORUM_HTTP_PROXY', None) or None  # Формат: 'host:port:username:password' или None

# Секретный ключ для аутентификации запросов
API_SECRET_KEY = os.environ.get('PARSER_SECRET_KEY', '')

# Учётные данные форумов, чьи разделы законодательной базы закрыты за авторизацией
# (аноним видит login-wall → 0 ссылок → срабатывает защита БЗ). Логинимся браузером при старте,
# cookies (xf_user/xf_session) наследуют все страницы контекста. gta5rp/rmrp публичны — не трогаем.
#   • forum.majestic-rp.ru — законка за логином c 2026-07;
#   • forum.russia.online  — за логином ВСЕ три законодательных раздела (гостю 403).
# Оба на XenForo, форма входа одинаковая (input[name=login]/[name=password]).
FORUM_LOGINS = {
    'forum.majestic-rp.ru': {
        'login_url': 'https://forum.majestic-rp.ru/login/',
        'user_env': 'FORUM_MAJESTIC_LOGIN',
        'password_env': 'FORUM_MAJESTIC_PASSWORD',
    },
    'forum.russia.online': {
        'login_url': 'https://forum.russia.online/login/',
        'user_env': 'FORUM_RUSSIA_ONLINE_LOGIN',
        'password_env': 'FORUM_RUSSIA_ONLINE_PASSWORD',
    },
}

# === FlareSolverr (обход Cloudflare managed-челленджа на gta5rp-форуме) ===
# forum.gta5rp.com закрыли за Cloudflare ("Just a moment…", HTTP 403) — headless Playwright палится,
# cf_clearance не выдаётся. FlareSolverr (отдельный контейнер с undetected-браузером) решает JS-челлендж
# и отдаёт готовый HTML + cf_clearance. CF-хосты тянем через него, majestic-путь НЕ трогаем.
# Инертно, пока FLARESOLVERR_URL пуст → поведение 1:1 прежнее.
FLARESOLVERR_URL = os.environ.get('FLARESOLVERR_URL', '') or None          # напр. http://flaresolverr:8191/v1
FLARESOLVERR_SESSION = os.environ.get('FLARESOLVERR_SESSION', 'gta5rp') or None
FLARESOLVERR_TIMEOUT_MS = int(os.environ.get('FLARESOLVERR_TIMEOUT_MS', '60000'))
FLARESOLVERR_PROXY = os.environ.get('FLARESOLVERR_PROXY', '') or None      # опц. резид. прокси http://user:pass@host:port
CF_PROTECTED_HOSTS = [h.strip().lower() for h in os.environ.get('CF_PROTECTED_HOSTS', 'forum.gta5rp.com').split(',') if h.strip()]
# У ОДНОЙ FlareSolverr-сессии — ОДНА вкладка браузера. Параллельные request.get затирают навигацию
# друг друга → часть отдаёт пустой/недогруженный DOM (HTTP 200, «Challenge not detected», но 0 ссылок).
# Именно это валило массовый прогон БЗ (секции gta5rp получали 0 → гард → «массовое удаление»).
# Поэтому запросы к FlareSolverr СЕРИАЛИЗУЕМ (дефолт 1). Поднять можно только с пулом сессий.
FLARESOLVERR_MAX_CONCURRENCY = max(1, int(os.environ.get('FLARESOLVERR_MAX_CONCURRENCY', '1')))

# === RMRP (DDoS-Guard): троттлинг и чистка разметки ===
# forum.rmrp.ru отдаёт контент гостю без челленджа, но жёстко рейт-лимитит IP (~12 быстрых
# запросов за 30с → деградация на 5-7 минут: таймауты, затем 502). Для таких хостов:
#   1) пауза между загрузками страниц (сериализованный per-host троттлинг),
#   2) блокировка субресурсов страницы (css/js/img): текст тем целиком в исходном HTML,
#      а ~20 same-host запросов на страницу сами по себе рвут лимит.
# Формат: 'host:секунды[,host:секунды]'. Пустое значение выключает троттлинг полностью.
def _parse_host_delays(raw: str) -> dict[str, float]:
    delays: dict[str, float] = {}
    for part in raw.split(','):
        part = part.strip()
        if not part or ':' not in part:
            continue
        host, _, delay = part.rpartition(':')
        try:
            delays[host.strip().lower()] = max(0.0, float(delay))
        except ValueError:
            continue
    return delays


# Дефолт 8с: смоук с прод-IP 2026-07-27 выдержал паузу 5с при полной загрузке субресурсов
# (589 запросов, 0 отказов) — берём 5с × 1.5 запаса. Синк 31 темы ≈ 4 мин при цикле 47ч.
HOST_FETCH_DELAYS = _parse_host_delays(os.environ.get('HOST_FETCH_DELAYS', 'forum.rmrp.ru:8'))

# Кап страниц пагинации для рейт-лимитируемых хостов: throttle × безлимитный обход длинной
# темы (жалобы парсятся без first_n) иначе гарантированно упирается в REQUEST_TIMEOUT=180с
# клиента (remote_parser_client.py) — воркер продолжал бы молотить после дисконнекта бота.
THROTTLED_HOST_MAX_PAGES = max(1, int(os.environ.get('THROTTLED_HOST_MAX_PAGES', '10')))

# Потолок вложения для /fetch-image (OCR всё равно отбрасывает файлы >10МБ)
MAX_FETCH_IMAGE_BYTES = 10 * 1024 * 1024

# Кап страниц темы для CF-хостов: каждая страница — отдельный запрос к FlareSolverr (до
# FLARESOLVERR_TIMEOUT_MS, сессия сериализована), а клиент ждёт REQUEST_TIMEOUT=180с. Без капа
# дело на 4+ страниц гарантированно обрывается по таймауту, и воркер молотит уже «в пустоту».
CF_THREAD_MAX_PAGES = max(1, int(os.environ.get('CF_THREAD_MAX_PAGES', '3')))

# Хосты, где перед извлечением текста из bbWrapper вырезаются цитаты (blockquote), а из
# заголовка темы — префикс XenForo (span.prefix). На RMRP первый пост каждого закона начинается
# с полного оглавления в раскрывающейся цитате (каждая строка «Статья N.» из TOC стала бы
# статьёй-заглушкой в каталоге), а заголовок склеивается с префиксом «ЗАКОНОДАТЕЛЬСТВО».
# ВАЖНО: только для перечисленных хостов — глобальный вырез изменил бы content_hash уже
# существующих серверов и триггернул массовый re-sync законов.
CONTENT_CLEANUP_HOSTS = [
    h.strip().lower()
    for h in os.environ.get('CONTENT_CLEANUP_HOSTS', 'forum.rmrp.ru').split(',')
    if h.strip()
]

_host_throttle_locks: dict[str, asyncio.Lock] = {}
_host_last_fetch: dict[str, float] = {}
_host_gates: dict[str, asyncio.Semaphore] = {}


def _host_delay_for(url: str) -> float:
    host = (urlsplit(url).hostname or '').lower()
    return HOST_FETCH_DELAYS.get(host, 0.0)


def _host_gate(url: str) -> Optional[asyncio.Semaphore]:
    """Входной гейт (1 слот) для рейт-лимитируемого хоста, берётся ДО общих семафоров.

    Троттлинг-сны иначе выполнялись бы ПОД topic/page-семафорами: несколько спящих
    rmrp-парсов пинили бы общие слоты (MAX_CONCURRENT_TOPICS=4) и отжимали majestic/gta5rp.
    Хост-лок и так сериализует фактические загрузки — гейт пропускной способности не режет."""
    if _host_delay_for(url) <= 0:
        return None
    host = (urlsplit(url).hostname or '').lower()
    return _host_gates.setdefault(host, asyncio.Semaphore(1))


async def _host_throttle(url: str) -> None:
    """Выдерживает паузу между загрузками страниц рейт-лимитируемого хоста.

    Лок сериализует конкурентные парсинги тем одного хоста (MAX_CONCURRENT_TOPICS=4),
    иначе паузы каждой корутины шли бы параллельно и лимит всё равно срывался."""
    delay = _host_delay_for(url)
    if delay <= 0:
        return
    host = (urlsplit(url).hostname or '').lower()
    lock = _host_throttle_locks.setdefault(host, asyncio.Lock())
    async with lock:
        elapsed = time.monotonic() - _host_last_fetch.get(host, 0.0)
        wait = delay - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        _host_last_fetch[host] = time.monotonic()


async def _lighten_page(page, url: str) -> None:
    """Для рейт-лимитируемых хостов оставляем только document-запросы (HTML отдаёт весь текст)."""
    if _host_delay_for(url) <= 0:
        return

    async def _route(route):
        if route.request.resource_type == 'document':
            await route.continue_()
        else:
            await route.abort()

    await page.route('**/*', _route)


def _needs_content_cleanup(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    return host in CONTENT_CLEANUP_HOSTS


def _resolve_cleanup(url: str, content_cleanup: Optional[bool]) -> bool:
    """Активна ли чистка разметки для этого запроса.

    Чистка задумана ТОЛЬКО для синка законки (TOC-оглавления), а /parse-topic обслуживает
    и разбор жалоб/форм — там цитаты (blockquote) являются содержимым (цитируемые
    доказательства), вырезать их нельзя. Поэтому вызывающий может передать
    content_cleanup=False (complaint/prosecution-пути). None → хостовое поведение
    (law-sync и старые боты без поля). Хост-гейт остаётся вторым предохранителем."""
    if not _needs_content_cleanup(url):
        return False
    return True if content_cleanup is None else bool(content_cleanup)


def _clean_topic_title(title_el, cleanup: bool) -> str:
    """Заголовок темы без бейджей XenForo.

    Форматы разные: rmrp вешает префикс как span.prefix («ЗАКОНОДАТЕЛЬСТВО»), gta5rp — как
    span.label («Важно») + span.label-append с пробелом. Оба — служебные метки, а не название.
    """
    if cleanup:
        for badge in title_el.select('span.prefix, span.label, span.label-append, .labelLink'):
            badge.decompose()
        return re.sub(r'\s+', ' ', title_el.get_text(' ', strip=True)).strip()
    return title_el.get_text(strip=True)


def _post_text(content_el, cleanup: bool) -> str:
    """Текст поста; при активной чистке вырезает цитаты (TOC-оглавления RMRP). Спойлеры
    (div.bbCodeSpoiler) не трогаем — там бывает содержимое закона."""
    if cleanup:
        for quote_el in content_el.find_all('blockquote'):
            quote_el.decompose()
    return content_el.get_text(separator='\n', strip=True)

# === НАСТРОЙКИ ПАРАЛЛЕЛИЗМА ===
# Максимум одновременных парсингов разделов (тяжёлые операции)
MAX_CONCURRENT_SECTIONS = 2
# Максимум одновременных парсингов тем
MAX_CONCURRENT_TOPICS = 4
# Максимум страниц браузера одновременно
MAX_BROWSER_PAGES = 6

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
browser_context: Optional[BrowserContext] = None
playwright_instance = None
browser = None

# Семафоры для контроля параллелизма
section_semaphore: Optional[asyncio.Semaphore] = None
topic_semaphore: Optional[asyncio.Semaphore] = None
page_semaphore: Optional[asyncio.Semaphore] = None

# Статистика
stats = {
    'total_requests': 0,
    'successful_requests': 0,
    'failed_requests': 0,
    'active_section_tasks': 0,
    'active_topic_tasks': 0,
    'active_pages': 0,
}


# === МОДЕЛИ ДАННЫХ ===
class ParseSectionRequest(BaseModel):
    """Запрос на парсинг раздела форума"""
    section_url: str
    secret_key: str


class ParseTopicRequest(BaseModel):
    """Запрос на парсинг одной темы"""
    url: str
    parse_only_author: bool = True
    thread_creators: dict = {}
    secret_key: str
    # Опциональный override на уровне темы:
    #   {"first_n": N}      — парсить только первые N постов (любого автора)
    #   {"post_ids": [...]} — парсить только посты с указанными числовыми ID XenForo
    post_override: Optional[dict] = None
    # Чистка разметки (blockquote/префикс темы) для CONTENT_CLEANUP_HOSTS:
    #   None (дефолт) — хостовое поведение (включена для законки rmrp; старый бот поля не шлёт);
    #   False — принудительно выключить (разбор жалоб/форм: цитаты там — содержимое).
    # Старый воркер незнакомое поле игнорирует (rolling deploy безопасен).
    content_cleanup: Optional[bool] = None


class FetchImageRequest(BaseModel):
    """Скачивание вложения через воркер (CF-хосты не отдают картинки боту напрямую)"""
    url: str
    secret_key: str
    referer: Optional[str] = None


class FetchImageResponse(BaseModel):
    success: bool
    content_b64: Optional[str] = None
    content_type: Optional[str] = None
    error: Optional[str] = None


class ParseThreadRequest(BaseModel):
    """Запрос на структурный разбор темы (судебное дело / форма жалобы).

    В отличие от /parse-topic отдаёт не склеенный текст, а сообщения по отдельности
    с авторами, временем и ссылками на вложения — это нужно судебному помощнику.
    """
    url: str
    secret_key: str
    max_pages: Optional[int] = None


class ThreadMessage(BaseModel):
    """Одно сообщение темы"""
    order: int
    author: str
    timestamp: Optional[str] = None
    text: str
    images: list[str] = []


class ParseThreadResponse(BaseModel):
    """Ответ структурного разбора темы"""
    success: bool
    title: Optional[str] = None
    thread_id: Optional[int] = None
    messages: list[ThreadMessage] = []
    truncated: bool = False   # обход остановлен капом страниц
    error: Optional[str] = None


class ParseSectionResponse(BaseModel):
    """Ответ с URLs тем из раздела"""
    urls: list[str]
    success: bool
    # Явная аттестация полноты нужна для безопасного rolling deploy:
    # новый bot не должен принимать success старого worker, который мог вернуть
    # частичный список после сбоя одной из страниц.
    complete: bool = False
    error: Optional[str] = None


class ParsedTopic(BaseModel):
    """Результат парсинга темы"""
    title: str
    content: str
    thread_id: int
    content_hash: str


class ParseTopicResponse(BaseModel):
    """Ответ парсинга темы"""
    success: bool
    data: Optional[ParsedTopic] = None
    error: Optional[str] = None
    thread_creators: dict = {}


class HealthResponse(BaseModel):
    """Ответ проверки здоровья"""
    status: str
    browser_ready: bool


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def extract_thread_id_from_url(url: str) -> Optional[int]:
    """Извлекает числовой ID темы из URL. Устойчиво к хвостам /unread, /latest, /page-N, /post-N:
    у ЗАЛОГИНЕННОГО парсера непрочитанные темы приходят как .../threads/slug.ID/unread, и прежний
    якорь \\.(\\d+)/?$ их терял → недосбор законов (напр. washington 17/31 → ложное удаление 14)."""
    match = re.search(r'/threads/[^/?#]*\.(\d+)(?:[/?#]|$)', url)
    if match:
        return int(match.group(1))
    match = re.search(r'\.(\d+)/?$', url)  # запасной вариант (id в конце URL)
    if match:
        return int(match.group(1))
    return None


def validated_discovery_thread_urls(
    hrefs,
    *,
    page_url: str,
    section_url: str,
) -> set[str]:
    """Validate every thread-like anchor or fail the whole section discovery."""
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
        if extract_thread_id_from_url(absolute_url) is None:
            raise RuntimeError(
                f"unrecognised thread URL in discovery: {absolute_url}"
            )
        canonical = re.sub(
            r'(/threads/[^/?#]*\.\d+).*$',
            r'\1/',
            absolute_url,
            flags=re.IGNORECASE,
        )
        result.add(canonical)
    if candidates == 0 or not result:
        raise RuntimeError(
            "discovery page contains no recognised topic URLs"
        )
    return result


async def fetch_gdoc_content(doc_id: str) -> Optional[str]:
    """Загружает содержимое Google документа"""
    export_url = f'https://docs.google.com/document/d/{doc_id}/export?format=txt'
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(export_url, timeout=30.0)
            response.raise_for_status()
            response.encoding = 'utf-8'
            return response.text
    except Exception as e:
        logging.error(f"Ошибка загрузки GDoc {doc_id}: {e}")
        return None


# === FlareSolverr-КЛИЕНТ (CF-обход для gta5rp) ===
_fs_session_ready = False
_fs_warmup_task = None  # фоновый прогрев сессии при старте (fire-and-forget)
_fs_semaphore = None    # сериализация запросов к FlareSolverr (одна сессия = одна вкладка)


def _fs_sem() -> "asyncio.Semaphore":
    """Семафор сериализации FlareSolverr: у одной сессии одна вкладка браузера — параллельные
    request.get затирают навигацию друг друга (пустой DOM → 0 ссылок → ложное «массовое удаление»).
    Создаётся лениво в текущем event loop; дефолтная конкуренция 1 (строго последовательно)."""
    global _fs_semaphore
    if _fs_semaphore is None:
        _fs_semaphore = asyncio.Semaphore(FLARESOLVERR_MAX_CONCURRENCY)
    return _fs_semaphore


def _is_cf_protected(url: str) -> bool:
    """CF-защищённый хост (gta5rp) → тянем через FlareSolverr. Без FLARESOLVERR_URL — обычный путь."""
    if not FLARESOLVERR_URL:
        return False
    u = url.lower()
    return any(h in u for h in CF_PROTECTED_HOSTS)


async def _flaresolverr_ensure_session() -> None:
    """Best-effort постоянная сессия FlareSolverr: держит тёплую вкладку с cf_clearance, чтобы
    последующие request.get переиспользовали cookie (быстрее, меньше пересолвов челленджа)."""
    global _fs_session_ready
    if not FLARESOLVERR_URL or not FLARESOLVERR_SESSION or _fs_session_ready:
        return
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with _fs_sem():   # не пересекаться с request.get той же вкладки
                resp = await client.post(FLARESOLVERR_URL, json={
                    "cmd": "sessions.create", "session": FLARESOLVERR_SESSION,
                })
            data = resp.json()
        msg = (data.get("message") or "")
        if data.get("status") == "ok" or "exist" in msg.lower():
            _fs_session_ready = True
            logging.info(f"[FlareSolverr] сессия '{FLARESOLVERR_SESSION}' готова")
        else:
            logging.warning(f"[FlareSolverr] sessions.create: {data.get('status')} {msg[:200]}")
    except Exception as e:
        logging.warning(f"[FlareSolverr] ошибка sessions.create: {e}")


async def _flaresolverr_get(url: str, _no_session: bool = False) -> Optional[str]:
    """Тянет URL через FlareSolverr (решает CF managed-челлендж) и возвращает отрендеренный HTML.
    None — если FlareSolverr не настроен/недоступен/не решил челлендж (вызывающий фолбэчит)."""
    global _fs_session_ready
    if not FLARESOLVERR_URL:
        return None
    # Рейт-лимитируемый хост может попасть сюда через CF_PROTECTED_HOSTS-фолбэк — паузу держим и тут.
    await _host_throttle(url)
    await _flaresolverr_ensure_session()
    use_session = bool(FLARESOLVERR_SESSION) and _fs_session_ready and not _no_session
    payload = {"cmd": "request.get", "url": url, "maxTimeout": FLARESOLVERR_TIMEOUT_MS}
    if use_session:
        payload["session"] = FLARESOLVERR_SESSION
    if FLARESOLVERR_PROXY:
        payload["proxy"] = {"url": FLARESOLVERR_PROXY}
    try:
        async with httpx.AsyncClient(timeout=FLARESOLVERR_TIMEOUT_MS / 1000 + 20) as client:
            # Сериализация: одна навигация вкладки за раз. Гейт держим только на самом POST'е к
            # FlareSolverr (не на разборе) — рекурсивный ретрай ниже возьмёт гейт заново, без дедлока.
            async with _fs_sem():
                resp = await client.post(FLARESOLVERR_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logging.error(f"[FlareSolverr] запрос {url} упал: {e}")
        return None
    if data.get("status") != "ok":
        msg = (data.get("message") or "")
        # Сессия пропала (рестарт FlareSolverr) → сброс и разовый ретрай без сессии.
        if use_session and "session" in msg.lower():
            _fs_session_ready = False
            logging.warning("[FlareSolverr] сессия потеряна — ретрай без сессии")
            return await _flaresolverr_get(url, _no_session=True)
        logging.error(f"[FlareSolverr] status={data.get('status')} msg={msg[:200]}")
        return None
    solution = data.get("solution") or {}
    html = solution.get("response")
    if not html:
        logging.error(f"[FlareSolverr] пустой response для {url} (http {solution.get('status')})")
        return None
    # Если вернулась сама CF-заглушка (челлендж не пройден) — не кормим ей парсер (иначе 0 ссылок → гард).
    if ('Just a moment' in html or 'cf-browser-verification' in html or '__cf_chl' in html) \
            and 'structItem' not in html and 'p-title-value' not in html:
        logging.warning(f"[FlareSolverr] челлендж не решён для {url}")
        return None
    return html


# --- Скачивание вложений с CF-хостов -----------------------------------------------------------
# Cloudflare отдаёт 403 не только на страницы, но и на /attachments/*: сканы решений из судебных
# дел не доходили до OCR. FlareSolverr выдаёт cf_clearance — с ним обычный HTTP-запрос проходит.
_cf_creds: dict = {}          # {'cookies': {...}, 'ua': str, 'ts': float}
_CF_CREDS_TTL = 1800.0


async def _cf_credentials(url: str, force_refresh: bool = False) -> tuple[dict, Optional[str]]:
    """cf_clearance + User-Agent от FlareSolverr (UA обязан совпадать, иначе cookie не примут)."""
    global _cf_creds
    fresh = _cf_creds and (time.monotonic() - _cf_creds.get('ts', 0.0)) < _CF_CREDS_TTL
    if fresh and not force_refresh:
        return _cf_creds['cookies'], _cf_creds['ua']
    if not FLARESOLVERR_URL:
        return {}, None
    origin = f"{urlsplit(url).scheme}://{urlsplit(url).hostname}/"
    payload = {"cmd": "request.get", "url": origin, "maxTimeout": FLARESOLVERR_TIMEOUT_MS}
    if FLARESOLVERR_PROXY:
        payload["proxy"] = {"url": FLARESOLVERR_PROXY}
    try:
        async with httpx.AsyncClient(timeout=(FLARESOLVERR_TIMEOUT_MS / 1000) + 30) as client:
            async with _fs_sem():
                resp = await client.post(FLARESOLVERR_URL, json=payload)
        resp.raise_for_status()
        solution = (resp.json() or {}).get("solution") or {}
    except Exception as e:
        logging.error(f"[CF] не удалось получить cookies: {e}")
        return {}, None
    cookies = {c['name']: c['value'] for c in solution.get('cookies', []) if c.get('name')}
    ua = solution.get('userAgent')
    if cookies:
        _cf_creds = {'cookies': cookies, 'ua': ua, 'ts': time.monotonic()}
    return cookies, ua


async def fetch_binary(url: str, referer: Optional[str] = None) -> Optional[tuple[bytes, str]]:
    """Скачивает файл (вложение/картинку). Для CF-хостов — с cf_clearance, иначе прямым запросом."""
    host_origin = f"{urlsplit(url).scheme}://{urlsplit(url).hostname}/"
    headers = {
        'Referer': referer or host_origin,
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8',
    }
    cookies: dict = {}
    if _is_cf_protected(url):
        cookies, ua = await _cf_credentials(url)
        if ua:
            headers['User-Agent'] = ua
    headers.setdefault(
        'User-Agent',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/126.0.0.0 Safari/537.36',
    )

    for attempt in range(2):
        await _host_throttle(url)
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=45.0) as client:
                r = await client.get(url, headers=headers, cookies=cookies)
            if r.status_code == 403 and _is_cf_protected(url) and attempt == 0:
                # Клиренс протух — обновляем и пробуем ещё раз.
                logging.warning(f"[CF] 403 на {url} — обновляю cf_clearance")
                cookies, ua = await _cf_credentials(url, force_refresh=True)
                if ua:
                    headers['User-Agent'] = ua
                continue
            r.raise_for_status()
            return r.content, r.headers.get('content-type', '')
        except Exception as e:
            logging.warning(f"[Fetch] {url}: {str(e)[:120]}")
            if attempt:
                return None
    return None


# === АВТОРИЗАЦИЯ НА ФОРУМЕ (Majestic закрыл законку за логином) ===
async def _wait_challenge_passed(page, tries: int = 15) -> None:
    """Ждёт прохождения vddos JS-челленджа (заглушка со slowAES → вычисляет cookie R3ACTLB и reload)."""
    for _ in range(tries):
        try:
            if 'slowAES' not in (await page.content()):
                return
        except Exception:
            pass
        await asyncio.sleep(2)


def _is_login_wall(content: str) -> bool:
    """Страница = стена авторизации XenForo (раздел требует входа).

    Один и тот же текст у majestic и у russia.online — русская локаль XenForo."""
    return ('Вы должны быть авторизованы' in content) or ('<title>Вход |' in content)


def _login_host(url: str) -> Optional[str]:
    """Хост из URL, если для него настроен вход по паролю; иначе None."""
    host = (urlsplit(url).hostname or '').lower()
    return host if host in FORUM_LOGINS else None


def _forum_credentials(host: str) -> tuple[Optional[str], Optional[str]]:
    cfg = FORUM_LOGINS.get(host) or {}
    user = os.environ.get(cfg.get('user_env', ''), '') or None
    password = os.environ.get(cfg.get('password_env', ''), '') or None
    return user, password


async def _is_forum_authed(context, host: str) -> bool:
    """Есть ли xf_user ИМЕННО ЭТОГО форума.

    Проверять «любой xf_user в контексте» нельзя: в одном браузерном контексте живут
    сессии нескольких форумов, и cookie majestic выдала бы russia.online за
    авторизованный (перелогин бы не сработал, разделы остались бы пустыми).
    XenForo ставит cookie на домен вида '.forum.russia.online' либо 'forum.russia.online'."""
    for c in await context.cookies():
        if c.get('name') != 'xf_user':
            continue
        domain = (c.get('domain') or '').lstrip('.').lower()
        if domain and (host == domain or host.endswith('.' + domain)):
            return True
    return False


async def _login_forum(context, host: str) -> bool:
    """Авторизация на форуме host. Логинится в переданном контексте — cookies
    (xf_user/xf_session) наследуют все последующие страницы этого контекста."""
    cfg = FORUM_LOGINS.get(host)
    if not cfg:
        return False
    user, password = _forum_credentials(host)
    if not user or not password:
        logging.warning(
            f"[Login] {cfg['user_env']}/{cfg['password_env']} не заданы — "
            f"разделы {host} будут недоступны"
        )
        return False
    page = await context.new_page()
    try:
        await page.goto(cfg['login_url'], timeout=45000, wait_until='commit')
        await _wait_challenge_passed(page)
        try:
            await page.wait_for_selector('input[name="login"]', timeout=20000)
        except Exception:
            if await _is_forum_authed(context, host):
                logging.info(f"[Login] Уже авторизованы на {host} (xf_user есть)")
                return True
            logging.error(f"[Login] Форма входа {host} не найдена")
            return False
        await page.fill('input[name="login"]', user)
        await page.fill('input[name="password"]', password)
        try:
            await page.check('input[name="remember"]')
        except Exception:
            pass
        await page.press('input[name="password"]', 'Enter')
        await asyncio.sleep(5)
        await _wait_challenge_passed(page)
        ok = await _is_forum_authed(context, host)
        logging.info(f"[Login] Авторизация {host}: {'УСПЕХ (xf_user получен)' if ok else 'НЕ УДАЛАСЬ'}")
        return ok
    except Exception as e:
        logging.error(f"[Login] Ошибка авторизации {host}: {e}")
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


# === УПРАВЛЕНИЕ БРАУЗЕРОМ ===
async def init_browser():
    """Инициализация браузера и семафоров при старте"""
    global playwright_instance, browser, browser_context
    global section_semaphore, topic_semaphore, page_semaphore

    logging.info("Инициализация семафоров для контроля параллелизма...")
    section_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SECTIONS)
    topic_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TOPICS)
    page_semaphore = asyncio.Semaphore(MAX_BROWSER_PAGES)
    logging.info(f"  Семафоры: sections={MAX_CONCURRENT_SECTIONS}, topics={MAX_CONCURRENT_TOPICS}, pages={MAX_BROWSER_PAGES}")

    logging.info("Инициализация Playwright браузера...")
    playwright_instance = await async_playwright().start()

    # Настройка прокси
    proxy_config = None
    if FORUM_HTTP_PROXY:
        parts = FORUM_HTTP_PROXY.split(':')
        if len(parts) == 4:
            proxy_config = {
                'server': f'http://{parts[0]}:{parts[1]}',
                'username': parts[2],
                'password': parts[3]
            }
            logging.info(f"Используется прокси: {parts[0]}:{parts[1]}")

    browser = await playwright_instance.firefox.launch(
        headless=True,
        proxy=proxy_config
    )

    browser_context = await browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
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
        }
    )

    # JavaScript для эмуляции Windows
    await browser_context.add_init_script("""
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
        Object.defineProperty(navigator, 'oscpu', { get: () => 'Windows NT 10.0; Win64; x64' });
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
    """)

    browser_context.set_default_timeout(60000)
    browser_context.set_default_navigation_timeout(60000)

    # Авторизация на форумах с закрытой законкой (majestic, russia.online).
    # Не роняем воркер при неудаче отдельного форума — остальные (в т.ч. публичные
    # gta5rp/rmrp) продолжают работать; синк закрытого форума упрётся в защиту БЗ.
    for _host in FORUM_LOGINS:
        try:
            await _login_forum(browser_context, _host)
        except Exception as e:
            logging.error(f"[Login] Непредвиденная ошибка авторизации {_host} при старте: {e}")

    # Прогрев FlareSolverr-сессии (CF-форумы, напр. gta5rp) — В ФОНЕ, не блокируя старт воркера:
    # flaresolverr может ещё грузить Chrome, а sessions.create ждёт до 120с → иначе задержим
    # готовность /health и провалим `docker compose up --wait`. Ленивый ensure в _flaresolverr_get
    # всё равно поднимет сессию к первому реальному запросу.
    global _fs_warmup_task
    try:
        _fs_warmup_task = asyncio.create_task(_flaresolverr_ensure_session())
    except Exception as e:
        logging.warning(f"[FlareSolverr] не удалось запланировать прогрев сессии: {e}")

    logging.info("Браузер успешно инициализирован")


async def close_browser():
    """Закрытие браузера"""
    global playwright_instance, browser, browser_context

    if browser_context:
        await browser_context.close()
    if browser:
        await browser.close()
    if playwright_instance:
        await playwright_instance.stop()

    logging.info("Браузер закрыт")


# === ФУНКЦИИ ПАРСИНГА ===
async def parse_section_urls(section_url: str) -> list[str]:
    """Собирает все URLs тем из раздела форума"""
    global browser_context, stats

    # CF-защищённый раздел (gta5rp) → FlareSolverr, минуя Playwright.
    if _is_cf_protected(section_url):
        return await _parse_section_flaresolverr(section_url)

    if not browser_context:
        raise RuntimeError("Браузер не инициализирован")

    # Гейт рейт-лимитируемого хоста ДО общего семафора, затем слот для парсинга раздела
    async with (_host_gate(section_url) or AsyncExitStack()), section_semaphore:
        stats['active_section_tasks'] += 1
        logging.info(f"  [Section] Начинаю парсинг (активных: {stats['active_section_tasks']})")

        try:
            all_urls = set()
            current_page_url = section_url
            page_num = 1

            # Ждём свободную страницу браузера
            async with page_semaphore:
                stats['active_pages'] += 1
                page = await browser_context.new_page()
                await _lighten_page(page, section_url)

                try:
                    while current_page_url:
                        logging.info(f"    Сканирую страницу {page_num}: {current_page_url}")

                        # Retry логика
                        for attempt in range(3):
                            try:
                                await _host_throttle(current_page_url)
                                response = await page.goto(
                                    current_page_url,
                                    timeout=45000,
                                    wait_until='commit',
                                )
                                await asyncio.sleep(2)

                                content = await page.content()
                                if len(content) < 1000:
                                    raise Exception("Мало контента")
                                if response is None or response.status != 200:
                                    status_code = (
                                        response.status
                                        if response is not None
                                        else "no-response"
                                    )
                                    raise RuntimeError(
                                        f"discovery page returned {status_code}"
                                    )
                                break
                            except Exception as e:
                                logging.warning(f"    Попытка {attempt + 1}/3 провалилась: {str(e)[:100]}")
                                if attempt < 2:
                                    try:
                                        await page.close()
                                    except:
                                        pass
                                    await asyncio.sleep(5)
                                    page = await browser_context.new_page()
                                    await _lighten_page(page, section_url)
                                else:
                                    raise

                        # Стена авторизации (сессия истекла между синками) → перелогин и повтор
                        _lh = _login_host(current_page_url)
                        if _lh:
                            await _wait_challenge_passed(page)
                            if _is_login_wall(await page.content()):
                                logging.warning(f"    {_lh}: login-wall — перелогиниваюсь и повторяю страницу")
                                await _login_forum(browser_context, _lh)
                                await page.goto(current_page_url, timeout=45000, wait_until='commit')
                                await asyncio.sleep(2)
                                await _wait_challenge_passed(page)

                        # Ждём контент
                        try:
                            await page.wait_for_selector('div.structItem-title a', timeout=20000)
                        except:
                            await asyncio.sleep(3)

                        # Собираем ссылки
                        elements = await page.query_selector_all('div.structItem-title a')
                        logging.info(f"    Найдено ссылок: {len(elements)}")
                        if not elements:
                            raise RuntimeError(
                                "discovery page contains no topic links; "
                                "partial results are unsafe"
                            )

                        page_hrefs = [
                            await el.get_attribute("href")
                            for el in elements
                        ]
                        all_urls.update(
                            validated_discovery_thread_urls(
                                page_hrefs,
                                page_url=current_page_url,
                                section_url=section_url,
                            )
                        )

                        # Следующая страница
                        next_btn = await page.query_selector('a.pageNav-jump--next')
                        if next_btn:
                            next_href = await next_btn.get_attribute('href')
                            current_page_url = urljoin(current_page_url, next_href)
                            page_num += 1
                            await asyncio.sleep(1)
                        else:
                            current_page_url = None

                finally:
                    try:
                        await page.close()
                    except:
                        pass
                    stats['active_pages'] -= 1

            logging.info(f"  [Section] Всего найдено {len(all_urls)} уникальных ссылок")
            return list(all_urls)

        finally:
            stats['active_section_tasks'] -= 1


async def parse_topic(url: str, parse_only_author: bool, thread_creators: dict, post_override: Optional[dict] = None,
                      content_cleanup: Optional[bool] = None) -> Optional[dict]:
    """Парсит одну тему форума"""
    global browser_context, stats

    # CF-защищённая тема (gta5rp) → FlareSolverr, минуя Playwright.
    if _is_cf_protected(url):
        return await _parse_topic_flaresolverr(url, parse_only_author, thread_creators, post_override, content_cleanup)

    if not browser_context:
        raise RuntimeError("Браузер не инициализирован")

    thread_id = extract_thread_id_from_url(url)
    if not thread_id:
        return None

    cleanup = _resolve_cleanup(url, content_cleanup)

    # Нормализация override
    override_first_n: Optional[int] = None
    override_post_ids: set = set()
    if isinstance(post_override, dict):
        if isinstance(post_override.get("first_n"), int) and post_override["first_n"] > 0:
            override_first_n = post_override["first_n"]
        raw_ids = post_override.get("post_ids") or []
        if isinstance(raw_ids, (list, tuple, set)):
            for pid in raw_ids:
                try:
                    override_post_ids.add(int(pid))
                except (TypeError, ValueError):
                    continue
    override_active = override_first_n is not None or bool(override_post_ids)
    if override_active:
        logging.info(f"    [Override] Тема {thread_id}: first_n={override_first_n}, post_ids={sorted(override_post_ids) if override_post_ids else None}")

    # Гейт рейт-лимитируемого хоста берём ДО общих семафоров (AsyncExitStack = no-op
    # для обычных хостов), затем слот для парсинга темы
    async with (_host_gate(url) or AsyncExitStack()), topic_semaphore:
        stats['active_topic_tasks'] += 1

        try:
            # Ждём свободную страницу браузера
            async with page_semaphore:
                stats['active_pages'] += 1
                page = await browser_context.new_page()
                await _lighten_page(page, url)

                try:
                    # Загрузка с retry
                    for attempt in range(3):
                        try:
                            await _host_throttle(url)
                            await page.goto(url, timeout=45000, wait_until='commit')
                            await asyncio.sleep(2)

                            content = await page.content()
                            if len(content) < 1000:
                                raise Exception("Мало контента")
                            break
                        except Exception as e:
                            logging.warning(f"    Попытка {attempt + 1}/3: {str(e)[:100]}")
                            if attempt < 2:
                                try:
                                    await page.close()
                                except:
                                    pass
                                await asyncio.sleep(5)
                                page = await browser_context.new_page()
                                await _lighten_page(page, url)
                            else:
                                raise

                    # Стена авторизации на самой теме (сессия истекла между синками, а
                    # discovery успел пройти) → перелогин и ОДИН повтор страницы. Без этого
                    # тема вернула бы пустой текст и защита БЗ отбраковала бы весь синк.
                    _lh = _login_host(url)
                    if _lh:
                        await _wait_challenge_passed(page)
                        if _is_login_wall(await page.content()):
                            logging.warning(f"    {_lh}: login-wall на теме — перелогиниваюсь и повторяю")
                            await _login_forum(browser_context, _lh)
                            await _host_throttle(url)
                            await page.goto(url, timeout=45000, wait_until='commit')
                            await asyncio.sleep(2)
                            await _wait_challenge_passed(page)

                    # Ждём заголовок
                    try:
                        await page.wait_for_selector('h1.p-title-value', timeout=20000)
                    except:
                        await asyncio.sleep(3)

                    soup = BeautifulSoup(await page.content(), 'html.parser')

                    # Заголовок
                    title_el = soup.select_one('h1.p-title-value')
                    if not title_el:
                        return None
                    law_title = _clean_topic_title(title_el, cleanup)
                    logging.info(f"    Заголовок: '{law_title}' (ID: {thread_id})")

                    # Проверка на Google Doc (пропускается, если задан override постов)
                    first_post = soup.select_one('article.message')
                    if first_post and not override_active:
                        gdoc_link = first_post.select_one('a[href*="docs.google.com/document/d/"]')
                        if gdoc_link:
                            gdoc_url = gdoc_link.get('href')
                            match = re.search(r'/document/d/([a-zA-Z0-9-_]+)', gdoc_url)
                            if match:
                                doc_id = match.group(1)
                                gdoc_content = await fetch_gdoc_content(doc_id)
                                if gdoc_content:
                                    return {
                                        'title': law_title,
                                        'content': gdoc_content.strip(),
                                        'thread_id': thread_id,
                                        'thread_creators': thread_creators
                                    }
                            return None

                    # Парсинг постов
                    logging.info(f"    GDoc не найден, парсим тему")
                    content_to_save = ""
                    official_author = None

                    if parse_only_author and first_post and not override_active:
                        if thread_id not in thread_creators:
                            author_el = first_post.select_one('a.username')
                            if author_el:
                                thread_creators[thread_id] = author_el.get_text(strip=True)
                        official_author = thread_creators.get(thread_id)

                    # Обход страниц темы
                    current_url = url
                    page_num = 1
                    collected_count = 0
                    stop_collecting = False
                    seen_post_ids: set[int] = set()

                    while current_url:
                        if page_num > 1:
                            for attempt in range(3):
                                try:
                                    await _host_throttle(current_url)
                                    await page.goto(current_url, timeout=45000, wait_until='commit')
                                    await asyncio.sleep(2)
                                    break
                                except Exception as e:
                                    if attempt < 2:
                                        try:
                                            await page.close()
                                        except:
                                            pass
                                        await asyncio.sleep(5)
                                        page = await browser_context.new_page()
                                        await _lighten_page(page, current_url)
                                    else:
                                        raise

                            soup = BeautifulSoup(await page.content(), 'html.parser')

                        posts = soup.select('article.message')
                        for post in posts:
                            # Скипаем XenForo highlighted / important-message блоки:
                            # это усечённый дубликат выделенного/первого поста,
                            # показываемый сверху. Без этого фильтра в override-режиме first_n=2
                            # мы взяли бы обрезанный дубль вместо полного текста.
                            classes = post.get('class') or []
                            if 'message--highlighted' in classes:
                                continue
                            if post.find_parent(class_='block-importantMessage') is not None:
                                continue

                            # Берём только реальные посты: data-content="post-NNN".
                            dc = post.get('data-content') or ''
                            m_pid = re.match(r'^post-(\d+)$', dc)
                            post_id = int(m_pid.group(1)) if m_pid else None
                            if override_active and post_id is None:
                                continue
                            if override_active and post_id in seen_post_ids:
                                continue

                            if override_active:
                                should_parse = False
                                if override_first_n is not None:
                                    if collected_count < override_first_n:
                                        should_parse = True
                                elif override_post_ids:
                                    if post_id in override_post_ids:
                                        should_parse = True
                            else:
                                should_parse = not parse_only_author
                                if parse_only_author and official_author:
                                    author_el = post.select_one('a.username')
                                    if author_el and author_el.get_text(strip=True) == official_author:
                                        should_parse = True

                            if should_parse:
                                content_el = post.select_one('div.bbWrapper')
                                if content_el:
                                    content_to_save += _post_text(content_el, cleanup) + "\n\n"
                                    if override_active and post_id is not None:
                                        seen_post_ids.add(post_id)
                                        collected_count += 1
                                        if override_first_n is not None and collected_count >= override_first_n:
                                            stop_collecting = True
                                            break

                        if stop_collecting:
                            current_url = None
                            break

                        # Следующая страница
                        next_btn = await page.query_selector('a.pageNav-jump--next')
                        if next_btn and _host_delay_for(url) > 0 and page_num >= THROTTLED_HOST_MAX_PAGES:
                            # Троттлинг (пауза/страница) × длинная тема упёрлись бы в
                            # REQUEST_TIMEOUT клиента — обход честно обрезаем.
                            logging.warning(
                                f"    [Throttle] Тема {thread_id}: остановка на стр. {page_num} "
                                f"(кап {THROTTLED_HOST_MAX_PAGES} для рейт-лимитируемого хоста)")
                            next_btn = None
                        if next_btn:
                            next_href = await next_btn.get_attribute('href')
                            current_url = urljoin(url, next_href)
                            page_num += 1
                            await asyncio.sleep(1)
                        else:
                            current_url = None

                    if content_to_save:
                        return {
                            'title': law_title,
                            'content': content_to_save.strip(),
                            'thread_id': thread_id,
                            'thread_creators': thread_creators
                        }

                    return None

                except Exception as e:
                    logging.error(f"Ошибка парсинга {url}: {e}")
                    return None
                finally:
                    try:
                        await page.close()
                    except:
                        pass
                    stats['active_pages'] -= 1

        finally:
            stats['active_topic_tasks'] -= 1


# === СТРУКТУРНЫЙ РАЗБОР ТЕМЫ (судебные дела, формы жалоб) ===
# Судебному помощнику нужны сообщения по отдельности + вложения-сканы, а /parse-topic отдаёт
# склеенный текст. Раньше это делал локальный Chromium в контейнере бота, но forum.gta5rp.com
# за Cloudflare он не проходит (403 → таймаут). Здесь тот же разбор, но на транспорте воркера:
# FlareSolverr для CF-хостов, Playwright для остальных, троттлинг для рейт-лимитируемых.

# Служебная графика XenForo, которая не является доказательством по делу
_SKIP_IMAGE_MARKERS = (
    'emoji', 'smilie', 'avatar', 'styles/default', 'xenforo', 'data/avatars',
)


def _images_from_body(body, page_url: str) -> list[str]:
    """URL картинок поста: прямые src и проксированные data-src (/proxy.php?image=...)."""
    urls: list[str] = []
    for img in body.find_all('img'):
        raw = img.get('data-src') or img.get('src') or ''
        if not raw:
            continue
        if 'proxy.php' in raw:
            m = re.search(r'[?&]image=([^&"\']+)', raw)
            if not m:
                continue
            raw = unquote(m.group(1))
        absolute = urljoin(page_url, raw)
        if not absolute.startswith('http'):
            continue
        if any(marker in absolute.lower() for marker in _SKIP_IMAGE_MARKERS):
            continue
        if absolute not in urls:
            urls.append(absolute)
    return urls


def _messages_from_soup(soup, page_url: str, start_order: int) -> list[dict]:
    """Сообщения страницы темы. Спойлеры XenForo скрыты стилями, но лежат в HTML —
    get_text достаёт их без кликов (локальному парсеру для этого нужен был браузер)."""
    messages: list[dict] = []
    order = start_order
    for post in soup.select('article.message'):
        classes = post.get('class') or []
        if 'message--highlighted' in classes:
            continue
        if post.find_parent(class_='block-importantMessage') is not None:
            continue
        if not re.match(r'^post-\d+$', post.get('data-content') or ''):
            continue

        author_el = post.select_one('a.username')
        time_el = post.select_one('time')
        body = post.select_one('div.bbWrapper')
        order += 1
        messages.append({
            'order': order,
            'author': author_el.get_text(strip=True) if author_el else 'Неизвестный',
            'timestamp': time_el.get('datetime') if time_el else None,
            # Цитаты НЕ вырезаем даже на cleanup-хостах: в судебном деле цитата — часть позиции стороны.
            'text': body.get_text(separator='\n', strip=True) if body else '',
            'images': _images_from_body(body, page_url) if body else [],
        })
    return messages


async def _thread_page_html(url: str, page) -> Optional[str]:
    """HTML страницы темы: FlareSolverr для CF-хостов, иначе Playwright (page переиспользуется)."""
    if _is_cf_protected(url):
        return await _flaresolverr_get(url)
    host = _login_host(url)
    relogged = False
    for attempt in range(3):
        try:
            await _host_throttle(url)
            await page.goto(url, timeout=45000, wait_until='commit')
            await asyncio.sleep(2)
            if host:
                await _wait_challenge_passed(page)
            html = await page.content()
            if len(html) < 1000:
                raise RuntimeError("страница почти пуста")
            # Стена авторизации коварна именно здесь: h1.p-title-value на ней ЕСТЬ («Вход»),
            # поэтому без явной проверки дело/жалоба вернулись бы пустыми, а не ошибкой.
            # Один перелогин и повтор загрузки.
            if host and not relogged and _is_login_wall(html):
                relogged = True
                logging.warning(f"    [Thread] {host}: login-wall — перелогиниваюсь и повторяю")
                await _login_forum(browser_context, host)
                continue
            return html
        except Exception as e:
            logging.warning(f"    [Thread] попытка {attempt + 1}/3 для {url}: {str(e)[:120]}")
            if attempt == 2:
                raise
            await asyncio.sleep(5)
    return None


async def parse_thread_messages(url: str, max_pages: Optional[int] = None) -> dict:
    """Разбирает тему на сообщения (с вложениями). Пагинация с капом страниц."""
    thread_id = extract_thread_id_from_url(url)
    cap = max_pages if (isinstance(max_pages, int) and max_pages > 0) else None
    if cap is None and _is_cf_protected(url):
        cap = CF_THREAD_MAX_PAGES
    if cap is None and _host_delay_for(url) > 0:
        cap = THROTTLED_HOST_MAX_PAGES

    async with (_host_gate(url) or AsyncExitStack()), topic_semaphore:
        stats['active_topic_tasks'] += 1
        try:
            # CF-путь идёт через FlareSolverr и страницу браузера не занимает.
            async with (AsyncExitStack() if _is_cf_protected(url) else page_semaphore):
                page = None
                if not _is_cf_protected(url):
                    if not browser_context:
                        raise RuntimeError("Браузер не инициализирован")
                    stats['active_pages'] += 1
                    page = await browser_context.new_page()
                    await _lighten_page(page, url)
                try:
                    title: Optional[str] = None
                    messages: list[dict] = []
                    truncated = False
                    current_url = url
                    page_num = 1

                    while current_url:
                        html = await _thread_page_html(current_url, page)
                        if not html:
                            raise RuntimeError(f"не получен HTML: {current_url}")
                        soup = BeautifulSoup(html, 'html.parser')

                        if title is None:
                            title_el = soup.select_one('h1.p-title-value')
                            if title_el is None:
                                raise RuntimeError("заголовок темы не найден (челлендж/логин-стена?)")
                            # Префикс темы («Важно», «ЗАКОНОДАТЕЛЬСТВО») чистим всегда: эндпоинт новый,
                            # content_hash к нему не привязан, а склейка «ВажноИск №997» — мусор в UI.
                            title = _clean_topic_title(title_el, True)

                        messages.extend(_messages_from_soup(soup, current_url, len(messages)))

                        next_btn = soup.select_one('a.pageNav-jump--next')
                        if next_btn and next_btn.get('href'):
                            if cap is not None and page_num >= cap:
                                truncated = True
                                logging.warning(
                                    f"    [Thread] {url}: остановка на стр. {page_num} (кап {cap})")
                                break
                            current_url = urljoin(current_url, next_btn.get('href'))
                            page_num += 1
                            if not _is_cf_protected(url):
                                await asyncio.sleep(1)
                        else:
                            current_url = None

                    logging.info(f"  [Thread] {url}: сообщений {len(messages)}, страниц {page_num}")
                    return {
                        'title': title or '',
                        'thread_id': thread_id,
                        'messages': messages,
                        'truncated': truncated,
                    }
                finally:
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
                        stats['active_pages'] -= 1
        finally:
            stats['active_topic_tasks'] -= 1


# === ПАРСИНГ CF-ЗАЩИЩЁННЫХ РАЗДЕЛОВ/ТЕМ ЧЕРЕЗ FlareSolverr (gta5rp) ===
# Полностью изолировано от Playwright/majestic-пути. Источник HTML — FlareSolverr (готовый
# отрендеренный DOM после решённого CF-челленджа), разбор — тем же BeautifulSoup и селекторами.
async def _parse_section_flaresolverr(section_url: str) -> list[str]:
    """Собирает URLs тем раздела CF-форума через FlareSolverr (аналог parse_section_urls)."""
    async with section_semaphore:
        stats['active_section_tasks'] += 1
        logging.info(f"  [CF Section] Парсинг через FlareSolverr: {section_url}")
        try:
            all_urls = set()
            current_page_url = section_url
            page_num = 1
            while current_page_url:
                soup = None
                elements = []
                # 0 тем на ПЕРВОЙ странице секции — почти всегда транзиент (недогруженный DOM/остаточная
                # гонка вкладки), а не пустой раздел. Перезапрашиваем до 3 попыток, прежде чем отдать 0
                # (иначе гард увидит 0 ссылок → ложное «массовое удаление» законов сервера).
                for _try in range(1, 4):
                    html = await _flaresolverr_get(current_page_url)
                    if html:
                        soup = BeautifulSoup(html, 'html.parser')
                        elements = soup.select('div.structItem-title a')
                    if elements or page_num > 1 or _try == 3:
                        break
                    logging.warning(f"    [CF] стр.1 пусто (попытка {_try}/3) — перезапрос: {current_page_url}")
                    await asyncio.sleep(1)
                if soup is None:
                    raise RuntimeError(
                        f"FlareSolverr did not return discovery HTML: {current_page_url}"
                    )
                if not elements:
                    raise RuntimeError(
                        "FlareSolverr discovery page contains no topic links; "
                        f"partial results are unsafe: {current_page_url}"
                    )
                logging.info(f"    [CF] стр.{page_num}: найдено ссылок {len(elements)}")
                all_urls.update(
                    validated_discovery_thread_urls(
                        [el.get('href') for el in elements],
                        page_url=current_page_url,
                        section_url=section_url,
                    )
                )
                next_btn = soup.select_one('a.pageNav-jump--next')
                if next_btn and next_btn.get('href'):
                    current_page_url = urljoin(current_page_url, next_btn.get('href'))
                    page_num += 1
                else:
                    current_page_url = None
            logging.info(f"  [CF Section] Всего найдено {len(all_urls)} уникальных ссылок")
            return list(all_urls)
        finally:
            stats['active_section_tasks'] -= 1


async def _parse_topic_flaresolverr(url: str, parse_only_author: bool, thread_creators: dict,
                                    post_override: Optional[dict] = None,
                                    content_cleanup: Optional[bool] = None) -> Optional[dict]:
    """Парсит одну тему CF-форума через FlareSolverr (аналог parse_topic)."""
    thread_id = extract_thread_id_from_url(url)
    if not thread_id:
        return None

    cleanup = _resolve_cleanup(url, content_cleanup)

    # Нормализация override (совпадает с parse_topic)
    override_first_n: Optional[int] = None
    override_post_ids: set = set()
    if isinstance(post_override, dict):
        if isinstance(post_override.get("first_n"), int) and post_override["first_n"] > 0:
            override_first_n = post_override["first_n"]
        raw_ids = post_override.get("post_ids") or []
        if isinstance(raw_ids, (list, tuple, set)):
            for pid in raw_ids:
                try:
                    override_post_ids.add(int(pid))
                except (TypeError, ValueError):
                    continue
    override_active = override_first_n is not None or bool(override_post_ids)

    async with (_host_gate(url) or AsyncExitStack()), topic_semaphore:
        stats['active_topic_tasks'] += 1
        try:
            first_html = await _flaresolverr_get(url)
            if not first_html:
                logging.warning(f"    [CF] нет HTML для темы {url}")
                return None
            soup = BeautifulSoup(first_html, 'html.parser')

            title_el = soup.select_one('h1.p-title-value')
            if not title_el:
                return None
            law_title = _clean_topic_title(title_el, cleanup)
            logging.info(f"    [CF] Заголовок: '{law_title}' (ID: {thread_id})")

            # Google Doc (пропускается, если задан override постов)
            first_post = soup.select_one('article.message')
            if first_post and not override_active:
                gdoc_link = first_post.select_one('a[href*="docs.google.com/document/d/"]')
                if gdoc_link:
                    match = re.search(r'/document/d/([a-zA-Z0-9-_]+)', gdoc_link.get('href') or '')
                    if match:
                        gdoc_content = await fetch_gdoc_content(match.group(1))
                        if gdoc_content:
                            return {
                                'title': law_title,
                                'content': gdoc_content.strip(),
                                'thread_id': thread_id,
                                'thread_creators': thread_creators
                            }
                    return None

            content_to_save = ""
            official_author = None
            if parse_only_author and first_post and not override_active:
                if thread_id not in thread_creators:
                    author_el = first_post.select_one('a.username')
                    if author_el:
                        thread_creators[thread_id] = author_el.get_text(strip=True)
                official_author = thread_creators.get(thread_id)

            current_url = url
            page_num = 1
            collected_count = 0
            stop_collecting = False
            seen_post_ids: set[int] = set()
            cur_soup = soup

            while current_url:
                if page_num > 1:
                    html = await _flaresolverr_get(current_url)
                    if not html:
                        break
                    cur_soup = BeautifulSoup(html, 'html.parser')

                posts = cur_soup.select('article.message')
                for post in posts:
                    classes = post.get('class') or []
                    if 'message--highlighted' in classes:
                        continue
                    if post.find_parent(class_='block-importantMessage') is not None:
                        continue

                    dc = post.get('data-content') or ''
                    m_pid = re.match(r'^post-(\d+)$', dc)
                    post_id = int(m_pid.group(1)) if m_pid else None
                    if override_active and post_id is None:
                        continue
                    if override_active and post_id in seen_post_ids:
                        continue

                    if override_active:
                        should_parse = False
                        if override_first_n is not None:
                            if collected_count < override_first_n:
                                should_parse = True
                        elif override_post_ids:
                            if post_id in override_post_ids:
                                should_parse = True
                    else:
                        should_parse = not parse_only_author
                        if parse_only_author and official_author:
                            author_el = post.select_one('a.username')
                            if author_el and author_el.get_text(strip=True) == official_author:
                                should_parse = True

                    if should_parse:
                        content_el = post.select_one('div.bbWrapper')
                        if content_el:
                            content_to_save += _post_text(content_el, cleanup) + "\n\n"
                            if override_active and post_id is not None:
                                seen_post_ids.add(post_id)
                                collected_count += 1
                                if override_first_n is not None and collected_count >= override_first_n:
                                    stop_collecting = True
                                    break

                if stop_collecting:
                    break

                next_btn = cur_soup.select_one('a.pageNav-jump--next')
                if next_btn and _host_delay_for(url) > 0 and page_num >= THROTTLED_HOST_MAX_PAGES:
                    logging.warning(
                        f"    [CF][Throttle] Тема {thread_id}: остановка на стр. {page_num} "
                        f"(кап {THROTTLED_HOST_MAX_PAGES} для рейт-лимитируемого хоста)")
                    next_btn = None
                if next_btn and next_btn.get('href'):
                    current_url = urljoin(url, next_btn.get('href'))
                    page_num += 1
                else:
                    current_url = None

            if content_to_save:
                return {
                    'title': law_title,
                    'content': content_to_save.strip(),
                    'thread_id': thread_id,
                    'thread_creators': thread_creators
                }
            return None
        except Exception as e:
            logging.error(f"[CF] Ошибка парсинга темы {url}: {e}")
            return None
        finally:
            stats['active_topic_tasks'] -= 1


# === FASTAPI ПРИЛОЖЕНИЕ ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения"""
    await init_browser()
    yield
    await close_browser()


app = FastAPI(
    title="Parser Worker",
    description="Удалённый сервис для парсинга форума",
    version="1.0.0",
    lifespan=lifespan
)


def verify_secret(secret_key: str):
    """Проверка секретного ключа"""
    if secret_key != API_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid secret key")


@app.get("/health")
async def health_check():
    """Проверка здоровья сервиса"""
    return {
        "status": "ok",
        "browser_ready": browser_context is not None,
        "flaresolverr": {
            "configured": bool(FLARESOLVERR_URL),
            "session_ready": _fs_session_ready,
        },
        "active_tasks": {
            "sections": stats['active_section_tasks'],
            "topics": stats['active_topic_tasks'],
            "pages": stats['active_pages'],
        },
        "limits": {
            "max_sections": MAX_CONCURRENT_SECTIONS,
            "max_topics": MAX_CONCURRENT_TOPICS,
            "max_pages": MAX_BROWSER_PAGES,
        }
    }


@app.get("/stats")
async def get_stats():
    """Получение статистики работы"""
    return {
        "total_requests": stats['total_requests'],
        "successful_requests": stats['successful_requests'],
        "failed_requests": stats['failed_requests'],
        "active_section_tasks": stats['active_section_tasks'],
        "active_topic_tasks": stats['active_topic_tasks'],
        "active_pages": stats['active_pages'],
        "limits": {
            "max_concurrent_sections": MAX_CONCURRENT_SECTIONS,
            "max_concurrent_topics": MAX_CONCURRENT_TOPICS,
            "max_browser_pages": MAX_BROWSER_PAGES,
        }
    }


@app.post("/parse-section", response_model=ParseSectionResponse)
async def api_parse_section(request: ParseSectionRequest):
    """API для парсинга раздела форума"""
    verify_secret(request.secret_key)
    stats['total_requests'] += 1

    try:
        urls = await parse_section_urls(request.section_url)
        stats['successful_requests'] += 1
        return ParseSectionResponse(urls=urls, success=True, complete=True)
    except Exception as e:
        stats['failed_requests'] += 1
        logging.error(f"Ошибка parse-section: {e}")
        return ParseSectionResponse(
            urls=[],
            success=False,
            complete=False,
            error=str(e),
        )


@app.post("/fetch-image", response_model=FetchImageResponse)
async def api_fetch_image(request: FetchImageRequest):
    """Отдаёт бинарь вложения (base64). Нужен для OCR сканов с CF-защищённых форумов."""
    verify_secret(request.secret_key)
    stats['total_requests'] += 1
    try:
        result = await fetch_binary(request.url, request.referer)
        if not result:
            stats['failed_requests'] += 1
            return FetchImageResponse(success=False, error="не удалось скачать файл")
        content, content_type = result
        if len(content) > MAX_FETCH_IMAGE_BYTES:
            stats['failed_requests'] += 1
            return FetchImageResponse(success=False, error=f"файл слишком большой: {len(content)} байт")
        stats['successful_requests'] += 1
        return FetchImageResponse(
            success=True,
            content_b64=base64.b64encode(content).decode('ascii'),
            content_type=content_type,
        )
    except Exception as e:
        stats['failed_requests'] += 1
        logging.error(f"Ошибка fetch-image {request.url}: {e}")
        return FetchImageResponse(success=False, error=str(e))


@app.post("/parse-thread", response_model=ParseThreadResponse)
async def api_parse_thread(request: ParseThreadRequest):
    """Структурный разбор темы: сообщения с авторами и вложениями (судебка, формы жалоб)."""
    verify_secret(request.secret_key)
    stats['total_requests'] += 1

    try:
        result = await parse_thread_messages(request.url, request.max_pages)
        if not result.get('messages'):
            stats['failed_requests'] += 1
            return ParseThreadResponse(success=False, error="в теме не найдено сообщений")
        stats['successful_requests'] += 1
        return ParseThreadResponse(
            success=True,
            title=result['title'],
            thread_id=result['thread_id'],
            messages=[ThreadMessage(**m) for m in result['messages']],
            truncated=result['truncated'],
        )
    except Exception as e:
        stats['failed_requests'] += 1
        logging.error(f"Ошибка parse-thread {request.url}: {e}")
        return ParseThreadResponse(success=False, error=str(e))


@app.post("/parse-topic", response_model=ParseTopicResponse)
async def api_parse_topic(request: ParseTopicRequest):
    """API для парсинга одной темы"""
    verify_secret(request.secret_key)
    stats['total_requests'] += 1

    try:
        result = await parse_topic(
            request.url,
            request.parse_only_author,
            request.thread_creators,
            request.post_override,
            request.content_cleanup
        )

        if result:
            content_hash = hashlib.md5(result['content'].encode('utf-8')).hexdigest()
            stats['successful_requests'] += 1
            return ParseTopicResponse(
                success=True,
                data=ParsedTopic(
                    title=result['title'],
                    content=result['content'],
                    thread_id=result['thread_id'],
                    content_hash=content_hash
                ),
                thread_creators=result.get('thread_creators', {})
            )
        else:
            stats['failed_requests'] += 1
            return ParseTopicResponse(success=False, error="Не удалось спарсить тему")

    except Exception as e:
        stats['failed_requests'] += 1
        logging.error(f"Ошибка parse-topic: {e}")
        return ParseTopicResponse(success=False, error=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)

