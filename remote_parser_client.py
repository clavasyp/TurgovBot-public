# START OF FILE remote_parser_client.py
"""
Клиент для удалённого парсера.
Используется основным ботом для отправки задач на парсинг к Worker-серверу.
"""

import asyncio
from dataclasses import dataclass
import logging
from typing import Optional
import httpx

# Попытка загрузить настройки из config
try:
    import config
    PARSER_WORKER_URL = getattr(config, 'PARSER_WORKER_URL', "http://YOUR_WORKER_IP:8765")
    PARSER_SECRET_KEY = getattr(config, 'PARSER_SECRET_KEY', "your-secret-key-change-me")
except ImportError:
    # Если config не доступен - используем значения по умолчанию
    PARSER_WORKER_URL = "http://YOUR_WORKER_IP:8765"  # URL Worker-сервера
    PARSER_SECRET_KEY = "your-secret-key-change-me-in-production"  # Должен совпадать с Worker

# Таймауты
REQUEST_TIMEOUT = 180.0  # Секунды на один запрос (увеличено для парсинга)
CONNECT_TIMEOUT = 15.0


@dataclass(frozen=True)
class SectionDiscoveryResult:
    """Результат discovery с явным признаком полноты.

    Пустой список сам по себе неоднозначен: раздел может быть пуст, а может не
    загрузиться. Destructive cleanup имеет право использовать только complete.
    """

    urls: list[str]
    complete: bool
    error: Optional[str] = None


class RemoteParserClient:
    """Асинхронный клиент для удалённого парсера"""

    def __init__(self, base_url: str = PARSER_WORKER_URL, secret_key: str = PARSER_SECRET_KEY):
        self.base_url = base_url.rstrip('/')
        self.secret_key = secret_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить или создать HTTP клиент"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
            )
        return self._client

    async def close(self):
        """Закрыть клиент"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        """Проверка доступности Worker'а"""
        try:
            client = await self._get_client()
            response = await client.get(f"{self.base_url}/health", timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                return data.get('status') == 'ok' and data.get('browser_ready', False)
            return False
        except Exception as e:
            logging.error(f"[RemoteParser] Health check failed: {e}")
            return False

    async def discover_section_urls(self, section_url: str) -> SectionDiscoveryResult:
        """
        Получить URLs тем и явный статус полноты discovery.

        Args:
            section_url: URL раздела форума

        Returns:
            SectionDiscoveryResult; complete=False запрещает удаление документов.
        """
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/parse-section",
                json={
                    "section_url": section_url,
                    "secret_key": self.secret_key
                }
            )

            if response.status_code == 200:
                data = response.json()
                if data.get('success') and data.get('complete') is True:
                    return SectionDiscoveryResult(
                        urls=list(data.get('urls', [])),
                        complete=True,
                    )
                elif data.get('success'):
                    error = "worker did not attest complete discovery"
                    logging.error(f"[RemoteParser] parse_section failed closed: {error}")
                    return SectionDiscoveryResult([], False, error)
                else:
                    error = str(data.get('error') or 'worker returned success=false')
                    logging.error(f"[RemoteParser] parse_section failed: {error}")
                    return SectionDiscoveryResult([], False, error)
            else:
                error = f"HTTP {response.status_code}"
                logging.error(f"[RemoteParser] parse_section {error}")
                return SectionDiscoveryResult([], False, error)

        except httpx.TimeoutException:
            logging.error(f"[RemoteParser] parse_section timeout for {section_url}")
            return SectionDiscoveryResult([], False, "timeout")
        except Exception as e:
            logging.error(f"[RemoteParser] parse_section error: {e}")
            return SectionDiscoveryResult([], False, str(e))

    async def parse_section_urls(self, section_url: str) -> list[str]:
        """Совместимый list-only API для недеструктивных ingest-скриптов."""
        result = await self.discover_section_urls(section_url)
        return result.urls

    async def fetch_image(self, url: str, referer: Optional[str] = None) -> Optional[tuple]:
        """Скачивает вложение руками воркера (у него есть cf_clearance для Cloudflare-форумов).

        Returns: (bytes, content_type) или None.
        """
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/fetch-image",
                json={"url": url, "referer": referer, "secret_key": self.secret_key},
            )
            if response.status_code == 404:
                logging.warning("[RemoteParser] /fetch-image отсутствует на воркере")
                return None
            if response.status_code != 200:
                logging.error(f"[RemoteParser] fetch_image HTTP {response.status_code} для {url}")
                return None
            data = response.json()
            if not data.get('success') or not data.get('content_b64'):
                logging.warning(f"[RemoteParser] fetch_image не удался для {url}: {data.get('error')}")
                return None
            import base64
            return base64.b64decode(data['content_b64']), (data.get('content_type') or '')
        except httpx.TimeoutException:
            logging.error(f"[RemoteParser] fetch_image timeout для {url}")
            return None
        except Exception as e:
            logging.error(f"[RemoteParser] fetch_image error для {url}: {e}")
            return None

    async def parse_thread(self, url: str, max_pages: Optional[int] = None) -> Optional[dict]:
        """Структурный разбор темы: сообщения с авторами, временем и вложениями.

        В отличие от parse_topic сохраняет границы сообщений — нужно судебному помощнику
        и разбору форм. Идёт транспортом воркера, поэтому работает и на Cloudflare-форумах,
        где локальный браузер бота получает 403.

        Returns:
            {title, thread_id, messages: [{order, author, timestamp, text, images}], truncated}
            или None, если воркер недоступен/не смог (вызывающий решает про фолбэк).
        """
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/parse-thread",
                json={"url": url, "max_pages": max_pages, "secret_key": self.secret_key},
            )
            if response.status_code == 404:
                # Старый воркер (rolling deploy) — эндпоинта ещё нет.
                logging.warning("[RemoteParser] /parse-thread отсутствует на воркере")
                return None
            if response.status_code != 200:
                logging.error(f"[RemoteParser] parse_thread HTTP {response.status_code} для {url}")
                return None
            data = response.json()
            if not data.get('success'):
                # Воркер ответил, но разобрать не смог — вызывающий по этому признаку решает,
                # осмысленно ли пробовать локальный браузер (для CF-форумов — нет).
                logging.warning(f"[RemoteParser] parse_thread не удался для {url}: {data.get('error')}")
                return {'success': False, 'error': data.get('error')}
            return data
        except httpx.TimeoutException:
            logging.error(f"[RemoteParser] parse_thread timeout для {url}")
            return None
        except Exception as e:
            logging.error(f"[RemoteParser] parse_thread error для {url}: {e}")
            return None

    async def parse_topic(
        self,
        url: str,
        parse_only_author: bool = True,
        thread_creators: dict = None,
        post_override: Optional[dict] = None,
        content_cleanup: Optional[bool] = None
    ) -> Optional[dict]:
        """
        Спарсить одну тему форума.

        Args:
            url: URL темы
            parse_only_author: Парсить только посты автора темы
            thread_creators: Словарь с авторами тем
            post_override: Переопределение постов для этой темы:
                {"first_n": N} — только первые N постов;
                {"post_ids": [...]} — только посты с указанными ID.
            content_cleanup: Чистка разметки на CONTENT_CLEANUP_HOSTS воркера
                (вырезание blockquote-цитат/префикса темы). None — хостовый дефолт
                (для законки rmrp включена); False обязателен для разбора жалоб/форм,
                где цитаты — содержимое. Старый воркер поле игнорирует.

        Returns:
            dict с полями: title, content, thread_id, content_hash, thread_creators
            или None при ошибке
        """
        if thread_creators is None:
            thread_creators = {}

        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/parse-topic",
                json={
                    "url": url,
                    "parse_only_author": parse_only_author,
                    "thread_creators": thread_creators,
                    "post_override": post_override,
                    "content_cleanup": content_cleanup,
                    "secret_key": self.secret_key
                }
            )

            if response.status_code == 200:
                data = response.json()
                if data.get('success') and data.get('data'):
                    result = data['data']
                    result['thread_creators'] = data.get('thread_creators', {})
                    return result
                else:
                    logging.warning(f"[RemoteParser] parse_topic failed for {url}: {data.get('error')}")
                    return None
            else:
                logging.error(f"[RemoteParser] parse_topic HTTP {response.status_code} for {url}")
                return None

        except httpx.TimeoutException:
            logging.error(f"[RemoteParser] parse_topic timeout for {url}")
            return None
        except Exception as e:
            logging.error(f"[RemoteParser] parse_topic error for {url}: {e}")
            return None


# Глобальный экземпляр клиента
_parser_client: Optional[RemoteParserClient] = None


def get_parser_client() -> RemoteParserClient:
    """Получить глобальный экземпляр клиента"""
    global _parser_client
    if _parser_client is None:
        _parser_client = RemoteParserClient()
    return _parser_client


async def close_parser_client():
    """Закрыть глобальный клиент"""
    global _parser_client
    if _parser_client:
        await _parser_client.close()
        _parser_client = None


# === УДОБНЫЕ ФУНКЦИИ ДЛЯ ИСПОЛЬЗОВАНИЯ ===

async def remote_parse_section(section_url: str) -> list[str]:
    """
    Удобная функция для парсинга раздела.
    Использует глобальный клиент.
    """
    client = get_parser_client()
    return await client.parse_section_urls(section_url)


async def remote_discover_section(section_url: str) -> SectionDiscoveryResult:
    """Discovery для синхронизации БЗ, где неполный результат нельзя удалять."""
    client = get_parser_client()
    return await client.discover_section_urls(section_url)


async def remote_parse_topic(
    url: str,
    parse_only_author: bool = True,
    thread_creators: dict = None,
    post_override: Optional[dict] = None,
    content_cleanup: Optional[bool] = None
) -> Optional[dict]:
    """
    Удобная функция для парсинга темы.
    Использует глобальный клиент.
    """
    client = get_parser_client()
    return await client.parse_topic(url, parse_only_author, thread_creators, post_override, content_cleanup)


async def remote_parse_thread(url: str, max_pages: Optional[int] = None) -> Optional[dict]:
    """Структурный разбор темы через воркер (обходит Cloudflare). None → фолбэк вызывающего."""
    client = get_parser_client()
    return await client.parse_thread(url, max_pages)


async def remote_fetch_image(url: str, referer: Optional[str] = None) -> Optional[tuple]:
    """Скачивание вложения через воркер (обходит Cloudflare). None → фолбэк вызывающего."""
    client = get_parser_client()
    return await client.fetch_image(url, referer)


async def check_remote_parser_available() -> bool:
    """Проверить доступность удалённого парсера"""
    client = get_parser_client()
    return await client.health_check()

# END OF FILE remote_parser_client.py

