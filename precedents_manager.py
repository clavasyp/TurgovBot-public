# START OF FILE precedents_manager.py

"""
Модуль для управления системой судебных прецедентов и толкований.

Обеспечивает:
- Парсинг темы-реестра прецедентов с форума
- Извлечение содержимого судебных решений (последнее сообщение темы)
- OCR-обработку изображений судебных документов
- Генерацию AI-суммари для быстрой фильтрации
- Синхронизацию прецедентов с базой данных и GCS
- Фильтрацию релевантных прецедентов для ответов на вопросы
"""

import asyncio
import hashlib
import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from google.cloud import storage
from google.api_core import exceptions as google_exceptions
from google.genai import types, errors as genai_errors

import config
from database.manager import db_manager
from prompts import get_precedent_summary_prompt, get_precedent_router_prompt
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client, mark_current_region_success, mark_current_region_error
from remote_parser_client import remote_parse_topic, check_remote_parser_available
from knowledge_base_manager import extract_thread_id_from_url

# Настройки
USE_REMOTE_PARSER = True  # Использовать удалённый парсер

# Модель для OCR изображений судебных документов
OCR_MODEL_NAME = 'gemini-2.0-flash'

# Промпт для распознавания текста судебного решения с изображения
OCR_PRECEDENT_PROMPT = """
Ты — специализированная система распознавания судебных документов RP-проекта Majestic RP.

Перед тобой изображение СУДЕБНОГО РЕШЕНИЯ (постановление, приговор, определение).

**ЗАДАЧА:**
Распознай и извлеки ВЕСЬ текст с изображения максимально точно.

**ФОРМАТ ОТВЕТА:**
Выведи только распознанный текст документа, сохраняя:
- Структуру и форматирование
- Заголовки, пункты, нумерацию
- ФИО, даты, номера статей

**ВАЖНО:**
- НЕ добавляй комментарии или пояснения
- Если текст частично нечитаем — укажи [нечитаемо]
- Исправляй очевидные опечатки OCR (0/O, 1/l и т.д.)
"""


def extract_links_from_registry(html_content: str, base_url: str) -> list[dict]:
    """
    Извлекает все гиперссылки на прецеденты из HTML-контента темы-реестра.

    Args:
        html_content: HTML-контент страницы реестра
        base_url: Базовый URL для преобразования относительных ссылок

    Returns:
        Список словарей с информацией о каждой ссылке:
        {
            'url': полный URL,
            'link_text': текст ссылки,
            'precedent_type': 'precedent' | 'interpretation',
            'is_active': True | False (False если ссылка зачёркнута)
        }
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    links = []

    # Ищем все ссылки в контенте постов
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']

        # Проверяем, что это ссылка на тему форума
        if '/threads/' not in href:
            continue

        # Преобразуем относительный URL в абсолютный
        full_url = urljoin(base_url, href)

        link_text = a_tag.get_text(strip=True)

        # Проверяем, находится ли ссылка внутри зачёркнутого текста
        is_strikethrough = False
        for parent in a_tag.parents:
            # Проверяем стандартные теги зачёркивания
            if parent.name in ('s', 'del', 'strike'):
                is_strikethrough = True
                break
            # Проверяем CSS-классы (XenForo может использовать классы)
            parent_classes = parent.get('class', [])
            if isinstance(parent_classes, list):
                class_str = ' '.join(parent_classes)
            else:
                class_str = parent_classes or ''
            if any(cls in class_str.lower() for cls in ['strikethrough', 'line-through', 'strike']):
                is_strikethrough = True
                break
            # Проверяем inline-стили
            style = parent.get('style', '')
            if 'line-through' in style.lower():
                is_strikethrough = True
                break

        # Определяем тип: толкование или прецедент
        link_text_lower = link_text.lower()
        if any(word in link_text_lower for word in ['толкован', 'толковани', 'интерпретац', 'разъяснен']):
            precedent_type = 'interpretation'
        else:
            precedent_type = 'precedent'

        # Извлекаем thread_id для дедупликации
        thread_id = extract_thread_id_from_url(full_url)
        if not thread_id:
            logging.warning(f"[Precedents] Не удалось извлечь thread_id из URL: {full_url}")
            continue

        links.append({
            'url': full_url,
            'link_text': link_text,
            'precedent_type': precedent_type,
            'is_active': not is_strikethrough,
            'thread_id': thread_id
        })

    return links


async def fetch_registry_page(registry_url: str) -> str | None:
    """
    Загружает HTML-контент темы-реестра с форума.

    Args:
        registry_url: URL темы-реестра прецедентов

    Returns:
        HTML-контент страницы или None при ошибке
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            logging.info(f"[Precedents] Загружаю тему-реестр: {registry_url}")
            response = await client.get(
                registry_url,
                timeout=30.0,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
                }
            )
            response.raise_for_status()
            response.encoding = 'utf-8'
            return response.text
    except Exception as e:
        logging.error(f"[Precedents] Ошибка загрузки реестра {registry_url}", exc_info=e)
        return None


async def parse_precedents_registry(server_id: str, registry_url: str) -> list[dict]:
    """
    Парсит тему-реестр и извлекает все гиперссылки на прецеденты.

    Args:
        server_id: ID сервера
        registry_url: URL темы-реестра

    Returns:
        Список словарей с информацией о прецедентах
    """
    html_content = await fetch_registry_page(registry_url)
    if not html_content:
        logging.error(f"[Precedents] Не удалось загрузить реестр для сервера {server_id}")
        return []

    links = extract_links_from_registry(html_content, registry_url)
    logging.info(f"[Precedents] Найдено {len(links)} ссылок в реестре для сервера {server_id}")

    # Дедупликация по thread_id
    seen_thread_ids = set()
    unique_links = []
    for link in links:
        if link['thread_id'] not in seen_thread_ids:
            seen_thread_ids.add(link['thread_id'])
            unique_links.append(link)

    if len(unique_links) < len(links):
        logging.info(f"[Precedents] После дедупликации осталось {len(unique_links)} уникальных ссылок")

    return unique_links


def extract_image_urls_from_html(html_content: str, base_url: str) -> list[str]:
    """
    Извлекает все URL изображений из HTML-контента сообщения.

    Args:
        html_content: HTML-контент сообщения
        base_url: Базовый URL для относительных ссылок

    Returns:
        Список URL изображений
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    image_urls = []

    # Ищем все img теги
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src')
        if src:
            # Пропускаем смайлики и иконки
            if any(skip in src.lower() for skip in ['smilies', 'emoji', 'avatar', 'icon', 'styles/']):
                continue
            # Преобразуем относительные URL
            full_url = urljoin(base_url, src)
            image_urls.append(full_url)

    # Ищем ссылки на изображения в тегах a (часто изображения обёрнуты в ссылки)
    for a in soup.find_all('a', href=True):
        href = a['href']
        # Проверяем расширения изображений
        if any(ext in href.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
            full_url = urljoin(base_url, href)
            if full_url not in image_urls:
                image_urls.append(full_url)
        # Проверяем популярные хостинги изображений
        if any(host in href.lower() for host in ['imgur.com', 'postimg', 'ibb.co', 'imgbb']):
            full_url = urljoin(base_url, href)
            if full_url not in image_urls:
                image_urls.append(full_url)

    return image_urls


async def download_image(image_url: str) -> bytes | None:
    """
    Скачивает изображение по URL.

    Args:
        image_url: URL изображения

    Returns:
        Байты изображения или None при ошибке
    """
    # Определяем Referer на основе URL
    referer_url = 'https://forum.majestic-rp.ru/'
    if 'gta5rp.com' in image_url:
        referer_url = 'https://forum.gta5rp.com/'
    elif 'rmrp.ru' in image_url:
        referer_url = 'https://forum.rmrp.ru/'
    elif 'russia.online' in image_url:
        referer_url = 'https://forum.russia.online/'

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Referer': referer_url
    }

    # Пробуем сначала с прокси, потом без
    proxy = None
    try:
        from config import IMGUR_SOCKS5_PROXY
        if IMGUR_SOCKS5_PROXY:
            proxy_parts = IMGUR_SOCKS5_PROXY.split(':')
            if len(proxy_parts) == 4:
                host, port, username, password = proxy_parts
                proxy = f"socks5://{username}:{password}@{host}:{port}"
    except:
        pass

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=30.0,
                headers=headers,
                proxy=proxy if attempt == 0 and proxy else None
            ) as client:
                response = await client.get(image_url)
                response.raise_for_status()

                # Проверяем размер (макс 10 МБ)
                if len(response.content) > 10 * 1024 * 1024:
                    logging.warning(f"[Precedents OCR] Изображение слишком большое: {image_url}")
                    return None

                return response.content

        except Exception as e:
            logging.warning(f"[Precedents OCR] Ошибка скачивания {image_url}, попытка {attempt + 1}: {e}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))

    return None


async def ocr_image(image_data: bytes, genai_client) -> str | None:
    """
    Выполняет OCR изображения судебного документа через Gemini Vision.

    Args:
        image_data: Байты изображения
        genai_client: Клиент Gemini API

    Returns:
        Распознанный текст или None при ошибке
    """
    try:
        # Определяем MIME-тип по сигнатуре
        if image_data[:4] == b'\x89PNG':
            mime_type = 'image/png'
        elif image_data[:2] == b'\xff\xd8':
            mime_type = 'image/jpeg'
        elif image_data[:6] in (b'GIF87a', b'GIF89a'):
            mime_type = 'image/gif'
        elif image_data[:4] == b'RIFF' and image_data[8:12] == b'WEBP':
            mime_type = 'image/webp'
        else:
            mime_type = 'image/jpeg'  # По умолчанию

        # Создаём Part с изображением
        image_part = types.Part(inline_data=types.Blob(
            data=image_data,
            mime_type=mime_type
        ))

        async def make_ocr_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на переданный клиент
                client = genai_client
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=OCR_MODEL_NAME,
                contents=[
                    types.Part(text=OCR_PRECEDENT_PROMPT),
                    image_part
                ],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=4096
                )
            )

        response = await execute_vertex_request(
            func=make_ocr_request,
            model_name=OCR_MODEL_NAME,
            priority=RequestPriority.LOW,
            max_retries=2
        )

        # Отмечаем успех/ошибку в текущем регионе
        if response and response.text:
            await mark_current_region_success()
            text = response.text.strip()
            logging.info(f"[Precedents OCR] Распознано {len(text)} символов")
            return text

        return None

    except Exception as e:
        await mark_current_region_error()
        logging.error(f"[Precedents OCR] Ошибка OCR", exc_info=e)
        return None


async def process_precedent_images(image_urls: list[str], genai_client) -> str:
    """
    Обрабатывает список URL изображений и возвращает объединённый текст OCR.

    Args:
        image_urls: Список URL изображений
        genai_client: Клиент Gemini API

    Returns:
        Объединённый текст со всех изображений
    """
    all_text_parts = []

    for i, url in enumerate(image_urls):
        logging.info(f"[Precedents OCR] Обработка изображения {i + 1}/{len(image_urls)}: {url[:80]}...")

        # Скачиваем изображение
        image_data = await download_image(url)
        if not image_data:
            logging.warning(f"[Precedents OCR] Не удалось скачать: {url}")
            continue

        # Выполняем OCR
        text = await ocr_image(image_data, genai_client)
        if text:
            all_text_parts.append(f"--- Страница {i + 1} ---\n{text}")

        # Пауза между запросами
        await asyncio.sleep(1)

    return "\n\n".join(all_text_parts) if all_text_parts else ""


async def fetch_precedent_content_with_ocr(
    url: str,
    genai_client,
    thread_creators: dict = None
) -> tuple[str, str, int, list[str]] | None:
    """
    Переходит по ссылке, парсит тему и извлекает текст судебного решения.
    Если в сообщении есть изображения - выполняет OCR.

    Args:
        url: URL темы с прецедентом
        genai_client: Клиент Gemini API для OCR
        thread_creators: Словарь для хранения авторов тем

    Returns:
        Кортеж (title, content, thread_id, image_urls) или None при ошибке
    """
    if thread_creators is None:
        thread_creators = {}

    thread_id = extract_thread_id_from_url(url)
    if not thread_id:
        logging.error(f"[Precedents] Не удалось извлечь thread_id из URL: {url}")
        return None

    try:
        # Загружаем страницу темы
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                url,
                timeout=30.0,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                }
            )
            response.raise_for_status()
            html_content = response.text

        soup = BeautifulSoup(html_content, 'html.parser')

        # Извлекаем заголовок
        title_elem = soup.select_one('h1.p-title-value')
        title = title_elem.get_text(strip=True) if title_elem else f"Прецедент {thread_id}"

        # Находим ПОСЛЕДНЕЕ сообщение (судебное решение)
        all_posts = soup.select('article.message')
        if not all_posts:
            logging.warning(f"[Precedents] Не найдено сообщений в теме {url}")
            return None

        last_post = all_posts[-1]  # Берём последний пост
        content_elem = last_post.select_one('div.bbWrapper')

        if not content_elem:
            logging.warning(f"[Precedents] Не найден контент в последнем сообщении: {url}")
            return None

        # Извлекаем текст из поста
        text_content = content_elem.get_text(separator='\n', strip=True)

        # Извлекаем URL изображений из поста
        image_urls = extract_image_urls_from_html(str(content_elem), url)

        logging.info(f"[Precedents] Тема '{title}': текст {len(text_content)} симв., изображений {len(image_urls)}")

        # Если есть изображения - выполняем OCR
        if image_urls and genai_client:
            logging.info(f"[Precedents] Запускаю OCR для {len(image_urls)} изображений...")
            ocr_text = await process_precedent_images(image_urls, genai_client)

            if ocr_text:
                # Объединяем текст поста и OCR
                if text_content.strip():
                    full_content = f"{text_content}\n\n=== ТЕКСТ ИЗ ДОКУМЕНТОВ (OCR) ===\n\n{ocr_text}"
                else:
                    full_content = ocr_text
            else:
                full_content = text_content
        else:
            full_content = text_content

        if not full_content.strip():
            logging.warning(f"[Precedents] Пустой контент после обработки: {url}")
            return None

        # Возвращаем также список URL изображений
        return title, full_content.strip(), thread_id, image_urls

    except Exception as e:
        logging.error(f"[Precedents] Ошибка парсинга темы {url}", exc_info=e)
        return None


async def fetch_precedent_content(url: str, thread_creators: dict = None) -> tuple[str, str, int, list[str]] | None:
    """
    Переходит по ссылке и парсит ПОСЛЕДНЕЕ сообщение темы (судебное решение).
    Использует локальный HTTP-парсинг для получения именно последнего поста.

    Args:
        url: URL темы с прецедентом
        thread_creators: Словарь для хранения авторов тем

    Returns:
        Кортеж (title, content, thread_id, image_urls) или None при ошибке
    """
    if thread_creators is None:
        thread_creators = {}

    thread_id = extract_thread_id_from_url(url)
    if not thread_id:
        logging.error(f"[Precedents] Не удалось извлечь thread_id из URL: {url}")
        return None

    try:
        # Для прецедентов важно получить ПОСЛЕДНЕЕ сообщение, а не все.
        # remote_parse_topic возвращает все посты автора, что не подходит.
        # Используем локальный HTTP-парсинг, который точно берёт последний пост.

        async with httpx.AsyncClient(follow_redirects=True) as client:
            # Сначала загружаем последнюю страницу темы
            # Добавляем параметр page=99999 чтобы попасть на последнюю страницу
            last_page_url = url.rstrip('/') + '/page-99999'

            response = await client.get(
                last_page_url,
                timeout=30.0,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                }
            )
            response.raise_for_status()
            html_content = response.text

        soup = BeautifulSoup(html_content, 'html.parser')

        # Извлекаем заголовок
        title_elem = soup.select_one('h1.p-title-value')
        title = title_elem.get_text(strip=True) if title_elem else f"Прецедент {thread_id}"

        # Находим ВСЕ сообщения на странице и берём ПОСЛЕДНЕЕ
        all_posts = soup.select('article.message')
        if not all_posts:
            logging.warning(f"[Precedents] Не найдено сообщений в теме {url}")
            return None

        # Берём именно последний пост на последней странице
        last_post = all_posts[-1]
        content_elem = last_post.select_one('div.bbWrapper')

        if not content_elem:
            logging.warning(f"[Precedents] Не найден контент в последнем сообщении: {url}")
            return None

        # Извлекаем текст из последнего поста
        text_content = content_elem.get_text(separator='\n', strip=True)

        # Извлекаем URL изображений из последнего поста
        image_urls = extract_image_urls_from_html(str(content_elem), url)

        logging.info(f"[Precedents] Тема '{title}': текст {len(text_content)} симв., изображений {len(image_urls)} (последний пост)")

        if not text_content.strip() and not image_urls:
            logging.warning(f"[Precedents] Пустой контент в последнем посте: {url}")
            return None

        return title, text_content.strip(), thread_id, image_urls

    except Exception as e:
        logging.error(f"[Precedents] Ошибка парсинга темы {url}", exc_info=e)
        return None


async def generate_precedent_summary(content: str, precedent_type: str, genai_client) -> str | None:
    """
    Генерирует краткое описание прецедента через Vertex AI.

    Args:
        content: Полный текст судебного решения
        precedent_type: 'precedent' или 'interpretation'
        genai_client: Клиент Gemini API

    Returns:
        Краткое описание (50-150 слов) или None при ошибке
    """
    try:
        prompt = get_precedent_summary_prompt(content, precedent_type)

        async def make_summary_request():
            return await genai_client.aio.models.generate_content(
                model=config.PRECEDENT_SUMMARY_MODEL_NAME,
                contents=prompt
            )

        response = await execute_vertex_request(
            func=make_summary_request,
            model_name=config.PRECEDENT_SUMMARY_MODEL_NAME,
            priority=RequestPriority.LOW,  # Низкий приоритет для фоновой задачи
            max_retries=2
        )

        summary = response.text.strip()

        # Ограничиваем размер суммари
        words = summary.split()
        if len(words) > 150:
            summary = ' '.join(words[:150]) + '...'

        return summary

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        logging.warning(f"[Precedents] Ошибка генерации суммари: {e}")
        return None
    except Exception as e:
        logging.error(f"[Precedents] Критическая ошибка генерации суммари", exc_info=e)
        return None


async def save_precedent_to_db(
    server_id: str,
    thread_id: int,
    source_url: str,
    title: str,
    content: str,
    summary: str | None,
    content_hash: str,
    image_urls: list[str],
    precedent_type: str,
    is_active: bool
):
    """
    Сохраняет или обновляет прецедент в таблице `precedents`.
    """
    query = """
        INSERT INTO precedents (server_id, source_thread_id, source_url, title, full_content, summary, 
                               content_hash, image_urls, precedent_type, is_active, last_updated)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
        ON CONFLICT (server_id, source_thread_id) DO UPDATE SET
            source_url = EXCLUDED.source_url,
            title = EXCLUDED.title,
            full_content = EXCLUDED.full_content,
            summary = COALESCE(EXCLUDED.summary, precedents.summary),
            content_hash = EXCLUDED.content_hash,
            image_urls = EXCLUDED.image_urls,
            precedent_type = EXCLUDED.precedent_type,
            is_active = EXCLUDED.is_active,
            last_updated = NOW()
    """
    await db_manager.execute(
        query, server_id, thread_id, source_url, title, content,
        summary, content_hash, image_urls, precedent_type, is_active
    )
    logging.info(f"[Precedents] Прецедент '{title}' (ID: {thread_id}) сохранен в БД")


async def sync_precedents_for_server(
    server_id: str,
    genai_client=None,
    force_update: bool = False
) -> int:
    """
    Основная функция синхронизации прецедентов для сервера.

    Args:
        server_id: ID сервера
        genai_client: Клиент Gemini API (если None, будет загружен из bot_data)
        force_update: Принудительное обновление всех прецедентов

    Returns:
        Количество обновлённых записей
    """
    logging.info(f"===== НАЧАЛО СИНХРОНИЗАЦИИ ПРЕЦЕДЕНТОВ ДЛЯ {server_id} =====")

    # Получаем конфигурацию сервера
    server_config = config.SERVERS_CONFIG.get(server_id)
    if not server_config:
        logging.error(f"[Precedents] Конфигурация для сервера {server_id} не найдена")
        return 0

    registry_url = server_config.get('precedents_registry_url')
    if not registry_url:
        logging.info(f"[Precedents] Для сервера {server_id} не настроена система прецедентов")
        return 0

    # Инициализируем GCS
    storage_client = storage.Client.from_service_account_json(config.VERTEX_CREDENTIALS_FILE)
    bucket = storage_client.bucket(config.active_gcs_bucket())

    # Парсим реестр
    registry_links = await parse_precedents_registry(server_id, registry_url)
    if not registry_links:
        logging.warning(f"[Precedents] Реестр для сервера {server_id} пуст")
        return 0

    # Получаем текущие данные из БД (включая image_urls для сравнения)
    db_records = await db_manager.fetch(
        "SELECT source_thread_id, content_hash, image_urls, is_active FROM precedents WHERE server_id = $1",
        server_id
    )
    db_precedents = {
        r['source_thread_id']: {
            'content_hash': r['content_hash'],
            'image_urls': r['image_urls'] or [],
            'is_active': r['is_active']
        }
        for r in db_records if r['source_thread_id'] is not None
    }

    logging.info(f"[Precedents] В БД для {server_id} найдено {len(db_precedents)} прецедентов")

    updated_count = 0
    thread_creators = {}
    registry_thread_ids = set()

    for link_info in registry_links:
        thread_id = link_info['thread_id']
        registry_thread_ids.add(thread_id)

        # Обновляем статус активности
        db_record = db_precedents.get(thread_id)
        if db_record and db_record['is_active'] != link_info['is_active']:
            # Статус изменился (например, ссылку зачеркнули)
            await db_manager.execute(
                "UPDATE precedents SET is_active = $1, last_updated = NOW() WHERE server_id = $2 AND source_thread_id = $3",
                link_info['is_active'], server_id, thread_id
            )
            logging.info(f"[Precedents] Обновлён статус активности для thread_id={thread_id}: {link_info['is_active']}")

        # Пропускаем неактивные (зачёркнутые) ссылки
        if not link_info['is_active']:
            continue

        try:
            # Сначала получаем только URL изображений для проверки изменений
            # Это позволяет не делать OCR если изображения не изменились

            # Парсим контент прецедента С ПОДДЕРЖКОЙ OCR для изображений
            if genai_client:
                result = await fetch_precedent_content_with_ocr(link_info['url'], genai_client, thread_creators)
                if not result:
                    continue
                title, content, parsed_thread_id, current_image_urls = result
            else:
                # Если нет клиента AI - используем версию без OCR (но всё равно получаем image_urls)
                result = await fetch_precedent_content(link_info['url'], thread_creators)
                if not result:
                    continue
                title, content, parsed_thread_id, current_image_urls = result

            # Создаём хеш на основе URL изображений (сортируем для консистентности)
            sorted_image_urls = sorted(current_image_urls)
            images_hash = hashlib.md5('|'.join(sorted_image_urls).encode('utf-8')).hexdigest()

            # Старый хеш из БД
            old_image_urls = db_record['image_urls'] if db_record else []
            old_sorted_urls = sorted(old_image_urls)
            old_images_hash = hashlib.md5('|'.join(old_sorted_urls).encode('utf-8')).hexdigest()

            # Проверяем, нужно ли обновление - сравниваем по URL изображений
            if not force_update and db_record and images_hash == old_images_hash:
                logging.info(f"[Precedents] '{title}' (ID: {thread_id}) - изображения не изменились, пропускаю")
                continue

            logging.info(f"[Precedents] {'Принудительное обновление' if force_update else 'Обнаружено изменение изображений'} в '{title}'")
            if current_image_urls:
                logging.info(f"[Precedents] Найдено {len(current_image_urls)} изображений: {current_image_urls[:3]}...")

            # Генерируем хеш контента для хранения
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

            # Генерируем суммари
            summary = None
            if genai_client:
                summary = await generate_precedent_summary(content, link_info['precedent_type'], genai_client)
                if summary:
                    logging.info(f"[Precedents] Сгенерировано суммари для '{title}' ({len(summary)} символов)")

            # Сохраняем в БД с image_urls
            await save_precedent_to_db(
                server_id=server_id,
                thread_id=thread_id,
                source_url=link_info['url'],
                title=title,
                content=content,
                summary=summary,
                content_hash=content_hash,
                image_urls=current_image_urls,
                precedent_type=link_info['precedent_type'],
                is_active=link_info['is_active']
            )

            # Загружаем в GCS
            file_hash_name = hashlib.md5(f"precedent_{thread_id}".encode('utf-8')).hexdigest()
            blob_name = f"{server_id}/precedents/{file_hash_name}.txt"
            gcs_uri = f"gs://{config.active_gcs_bucket()}/{blob_name}"

            blob = bucket.blob(blob_name)
            file_content = f"ТИП ДОКУМЕНТА: {link_info['precedent_type'].upper()}\n"
            file_content += f"НАЗВАНИЕ: {title}\n"
            if summary:
                file_content += f"КРАТКОЕ ОПИСАНИЕ: {summary}\n"
            file_content += f"\n{content}"

            await asyncio.to_thread(blob.upload_from_string, file_content, content_type="text/plain")

            # Проверяем, что файл реально загружен в GCS
            blob_exists = await asyncio.to_thread(blob.exists)
            if not blob_exists:
                logging.error(f"[Precedents] Файл НЕ найден в GCS после загрузки: {gcs_uri}")
                continue

            # Обновляем URI в БД
            await db_manager.execute(
                "UPDATE precedents SET gemini_file_uri = $1 WHERE server_id = $2 AND source_thread_id = $3",
                gcs_uri, server_id, thread_id
            )

            logging.info(f"[Precedents] '{title}' (ID: {thread_id}) успешно синхронизирован")
            updated_count += 1

            # Небольшая пауза между запросами
            await asyncio.sleep(1)

        except Exception as e:
            logging.error(f"[Precedents] Ошибка обработки прецедента {link_info['url']}", exc_info=e)

    # Помечаем как неактивные прецеденты, которых нет в реестре
    for thread_id in db_precedents.keys():
        if thread_id not in registry_thread_ids:
            await db_manager.execute(
                "UPDATE precedents SET is_active = FALSE, last_updated = NOW() WHERE server_id = $1 AND source_thread_id = $2",
                server_id, thread_id
            )
            logging.info(f"[Precedents] Прецедент ID={thread_id} помечен как неактивный (отсутствует в реестре)")

    logging.info(f"===== СИНХРОНИЗАЦИЯ ПРЕЦЕДЕНТОВ ДЛЯ {server_id} ЗАВЕРШЕНА. Обновлено: {updated_count} =====")
    return updated_count


async def get_relevant_precedents(
    genai_client,
    user_question: str,
    server_id: str
) -> list[dict]:
    """
    Выбирает релевантные прецеденты для вопроса пользователя.

    Args:
        genai_client: Клиент Gemini API
        user_question: Вопрос пользователя
        server_id: ID сервера

    Returns:
        Список словарей с релевантными прецедентами:
        [{'title': ..., 'uri': ..., 'summary': ..., 'type': ...}, ...]
    """
    # Проверяем, есть ли у сервера прецеденты
    server_config = config.SERVERS_CONFIG.get(server_id, {})
    if not server_config.get('precedents_registry_url'):
        return []  # На этом сервере нет прецедентной системы

    # Получаем активные прецеденты с суммари
    precedent_records = await db_manager.fetch(
        """SELECT id, title, summary, gemini_file_uri, precedent_type 
           FROM precedents 
           WHERE server_id = $1 AND is_active = TRUE AND gemini_file_uri IS NOT NULL""",
        server_id
    )

    if not precedent_records:
        logging.info(f"[Precedents] Нет активных прецедентов для сервера {server_id}")
        return []

    # Фильтруем: оставляем только прецеденты с валидным GCS URI из нашего бакета
    expected_prefix = f"gs://{config.active_gcs_bucket()}/"
    valid_records = []
    invalid_count = 0
    for r in precedent_records:
        uri = r['gemini_file_uri']
        if uri and uri.startswith(expected_prefix):
            valid_records.append(r)
        else:
            invalid_count += 1
            logging.warning(
                f"[Precedents] Пропускаю прецедент '{r['title']}' (id={r['id']}): "
                f"невалидный URI '{uri}' (ожидается prefix '{expected_prefix}')"
            )

    if invalid_count > 0:
        logging.warning(f"[Precedents] Отфильтровано {invalid_count} прецедентов с невалидными URI для {server_id}")

    if not valid_records:
        logging.info(f"[Precedents] Нет прецедентов с валидными GCS URI для сервера {server_id}")
        return []

    precedent_records = valid_records

    # Формируем список для роутера
    precedent_map = {i + 1: r for i, r in enumerate(precedent_records)}
    precedents_list_lines = []
    for i, r in enumerate(precedent_records):
        type_prefix = "[Прецедент]" if r['precedent_type'] == 'precedent' else "[Толкование]"
        summary_text = r['summary'] or "Описание отсутствует"
        precedents_list_lines.append(f"{i + 1}. {type_prefix} {r['title']}: {summary_text}")

    precedents_list = "\n".join(precedents_list_lines)

    try:
        # Вызываем роутер
        router_prompt = get_precedent_router_prompt(user_question, precedents_list)

        async def make_router_request():
            return await genai_client.aio.models.generate_content(
                model=config.ROUTER_MODEL_NAME,
                contents=router_prompt
            )

        response = await execute_vertex_request(
            func=make_router_request,
            model_name=config.ROUTER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )

        # Парсим ответ
        selected_indices = set()
        for line in response.text.split('\n'):
            line = line.strip()
            if line.isdigit():
                selected_indices.add(int(line))

        if 0 in selected_indices:
            logging.info(f"[Precedents] Роутер решил, что прецеденты не нужны для данного вопроса")
            return []

        relevant = [precedent_map[i] for i in sorted(selected_indices) if i in precedent_map]

        logging.info(f"[Precedents] Роутер выбрал {len(relevant)} прецедентов: {[r['title'] for r in relevant]}")
        if relevant:
            logging.debug(f"[Precedents] URI выбранных прецедентов: {[r['gemini_file_uri'] for r in relevant]}")

        return [
            {
                'title': r['title'],
                'uri': r['gemini_file_uri'],
                'summary': r['summary'],
                'type': r['precedent_type']
            }
            for r in relevant
        ]

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        logging.warning(f"[Precedents] Ошибка при фильтрации прецедентов: {e}")
        return []
    except Exception as e:
        logging.error(f"[Precedents] Критическая ошибка фильтрации прецедентов", exc_info=e)
        return []


async def validate_precedent_uris(server_id: str) -> dict:
    """
    Проверяет валидность GCS URI всех активных прецедентов для сервера.
    Сбрасывает gemini_file_uri в NULL для прецедентов с битыми URI.

    Returns:
        Статистика: {'total': N, 'valid': N, 'invalid': N, 'cleaned': N}
    """
    expected_prefix = f"gs://{config.active_gcs_bucket()}/"
    storage_client = storage.Client.from_service_account_json(config.VERTEX_CREDENTIALS_FILE)
    bucket = storage_client.bucket(config.active_gcs_bucket())

    records = await db_manager.fetch(
        "SELECT id, title, gemini_file_uri FROM precedents WHERE server_id = $1 AND is_active = TRUE AND gemini_file_uri IS NOT NULL",
        server_id
    )

    stats = {'total': len(records), 'valid': 0, 'invalid': 0, 'cleaned': 0}

    for r in records:
        uri = r['gemini_file_uri']

        # Проверка формата URI
        if not uri.startswith(expected_prefix):
            logging.warning(f"[Precedents] URI не из нашего бакета: id={r['id']}, title='{r['title']}', uri='{uri}'")
            stats['invalid'] += 1
            await db_manager.execute("UPDATE precedents SET gemini_file_uri = NULL WHERE id = $1", r['id'])
            stats['cleaned'] += 1
            continue

        # Проверка существования файла в GCS
        blob_name = uri.replace(expected_prefix, "", 1)
        blob = bucket.blob(blob_name)
        try:
            exists = await asyncio.to_thread(blob.exists)
            if exists:
                stats['valid'] += 1
            else:
                logging.warning(f"[Precedents] Файл не найден в GCS: id={r['id']}, title='{r['title']}', uri='{uri}'")
                stats['invalid'] += 1
                await db_manager.execute("UPDATE precedents SET gemini_file_uri = NULL WHERE id = $1", r['id'])
                stats['cleaned'] += 1
        except Exception as e:
            logging.error(f"[Precedents] Ошибка проверки GCS для id={r['id']}: {e}")
            stats['invalid'] += 1

    logging.info(f"[Precedents] Валидация URI для {server_id}: {stats}")
    return stats


# END OF FILE precedents_manager.py

