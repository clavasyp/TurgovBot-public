# ocr_processor.py
"""
OCR-процессор для распознавания текста с изображений судебных документов.

Использует Gemini Vision API для:
- Распознавания текста с изображений (постановления судей, ходатайства)
- Определения типа документа
- Извлечения ключевой информации (даты, ФИО, резолютивная часть)
"""

import asyncio
import logging
import httpx
import tempfile
import os
from pathlib import Path
from typing import Optional, Dict

from google import genai
from google.genai import types

from rate_limiter import get_client_for_model

# Проверка наличия socksio
try:
    import socksio
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False
    logging.warning("[OCR] ⚠️ socksio не установлен, скачивание без прокси. Установите: pip install socksio")


# DEBUG: Флаг для включения/выключения детального логирования
DEBUG_OCR = False  # Установите True для отладки


# Промпт для OCR и анализа судебных документов
OCR_ANALYSIS_PROMPT = """
Ты - специализированная система распознавания и анализа судебных документов.

Перед тобой изображение документа из судебного дела (постановление, ходатайство, протокол и т.д.).

**ЗАДАЧИ:**
1. **Распознай ВЕСЬ текст** с изображения максимально точно
2. **Определи тип документа**: постановление суда / ходатайство / протокол / иное
3. **Извлеки ключевую информацию**:
   - Дата документа
   - ФИО судьи (если есть)
   - ФИО участников процесса
   - Резолютивная часть (решение суда)

**ФОРМАТ ОТВЕТА:**

```
ТИП ДОКУМЕНТА: [тип]

ДАТА: [дата или "не указана"]

КЛЮЧЕВЫЕ УЧАСТНИКИ:
- [ФИО и роль]

РЕЗОЛЮТИВНАЯ ЧАСТЬ:
[основное решение]

ПОЛНЫЙ ТЕКСТ:
[весь распознанный текст с сохранением структуры]
```

**ВАЖНО:**
- Сохраняй структуру и форматирование оригинального текста
- Если текст нечитаем - укажи это явно
- Исправляй очевидные опечатки OCR (например, "O" вместо "0")
- Если документ на другом языке или это не судебный документ - укажи это
"""


async def _download_image(image_url: str, retry_count: int = 3, delay_between_images: float = 1.0) -> Optional[str]:
    """
    Скачивает изображение во временный файл с retry при ошибках.
    Использует SOCKS5 прокси для всех хостингов изображений.

    Args:
        image_url: URL изображения
        retry_count: Количество попыток при ошибках
        delay_between_images: Базовая задержка между попытками (сек)

    Returns:
        Путь к скачанному файлу или None при ошибке
    """
    from config import IMGUR_SOCKS5_PROXY

    # Определяем Referer на основе URL изображения или используем дефолтный
    referer_url = 'https://forum.majestic-rp.ru/'
    if 'gta5rp.com' in image_url or 'forum.gta5rp.com' in image_url:
        referer_url = 'https://forum.gta5rp.com/'
    elif 'rmrp.ru' in image_url:
        referer_url = 'https://forum.rmrp.ru/'
    elif 'russia.online' in image_url:
        referer_url = 'https://forum.russia.online/'

    # User-Agent для обхода блокировок
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Referer': referer_url,
        'Sec-Fetch-Dest': 'image',
        'Sec-Fetch-Mode': 'no-cors',
        'Sec-Fetch-Site': 'cross-site'
    }

    for attempt in range(retry_count):
        try:
            if DEBUG_OCR:
                logging.info(f"[OCR-DEBUG] Скачивание #{attempt + 1}/{retry_count}: {image_url}")

            # Задержка между попытками для избежания rate limit
            if attempt > 0:
                delay = delay_between_images * (2 ** attempt)  # Экспоненциальный backoff
                if DEBUG_OCR:
                    logging.info(f"[OCR-DEBUG] Задержка {delay:.1f}с перед повторной попыткой...")
                await asyncio.sleep(delay)

            # Настройка прокси для всех хостингов
            proxy = None
            use_proxy = False
            if IMGUR_SOCKS5_PROXY and HAS_SOCKS:
                # Парсим прокси формата host:port:username:password
                proxy_parts = IMGUR_SOCKS5_PROXY.split(':')
                if len(proxy_parts) == 4:
                    host, port, username, password = proxy_parts
                    proxy = f"socks5://{username}:{password}@{host}:{port}"
                    use_proxy = True
                    if DEBUG_OCR:
                        logging.info(f"[OCR-DEBUG] Используется SOCKS5 прокси: {host}:{port}")

            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=30.0,
                headers=headers,
                proxy=proxy if use_proxy else None
            ) as client:
                response = await client.get(image_url)
                response.raise_for_status()

                # Определяем расширение файла
                content_type = response.headers.get('content-type', '')
                if 'jpeg' in content_type or 'jpg' in content_type:
                    ext = '.jpg'
                elif 'png' in content_type:
                    ext = '.png'
                elif 'gif' in content_type:
                    ext = '.gif'
                else:
                    ext = Path(image_url.split('?')[0]).suffix or '.jpg'

                # Создаем временный файл
                with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
                    temp_file.write(response.content)
                    temp_path = temp_file.name

                file_size = os.path.getsize(temp_path)

                # Проверка размера (максимум 10 МБ)
                if file_size > 10 * 1024 * 1024:
                    logging.warning(f"[OCR] ⚠️ Изображение слишком большое: {file_size} байт, URL: {image_url}")
                    os.unlink(temp_path)
                    return None

                logging.info(f"[OCR] ✅ Изображение скачано: {file_size} байт, попытка {attempt + 1}")
                return temp_path

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logging.warning(f"[OCR] ⚠️ Rate limit (429) от {image_url.split('/')[2]}, попытка {attempt + 1}/{retry_count}")
                if attempt < retry_count - 1:
                    continue
            else:
                logging.error(f"[OCR] ❌ HTTP {e.response.status_code} для {image_url}")
            if attempt >= retry_count - 1:
                break
        except httpx.TimeoutException:
            logging.warning(f"[OCR] ⏱️ Таймаут при скачивании {image_url}, попытка {attempt + 1}/{retry_count}")
            if attempt >= retry_count - 1:
                break
        except Exception as e:
            logging.error(f"[OCR] ❌ Ошибка при скачивании {image_url}: {type(e).__name__}: {e}")
            if attempt >= retry_count - 1:
                break

    # Форумы за Cloudflare (gta5rp) отдают боту 403 даже на /attachments/*: сканы судебных
    # решений так не доходили до OCR. У воркера есть cf_clearance — просим скачать его.
    remote_path = await _download_image_via_worker(image_url, referer_url)
    if remote_path:
        return remote_path

    logging.error(f"[OCR] ❌ Не удалось скачать после {retry_count} попыток: {image_url}")
    return None


async def _download_image_via_worker(image_url: str, referer_url: str) -> Optional[str]:
    """Фолбэк-загрузка вложения через parser-worker (у него транспорт с обходом Cloudflare)."""
    try:
        from remote_parser_client import remote_fetch_image
    except Exception:
        return None

    try:
        result = await remote_fetch_image(image_url, referer_url)
    except Exception as e:
        logging.warning(f"[OCR] Воркер не смог скачать {image_url}: {type(e).__name__}: {e}")
        return None
    if not result:
        return None

    content, content_type = result
    if len(content) > 10 * 1024 * 1024:
        logging.warning(f"[OCR] ⚠️ Изображение от воркера слишком большое: {len(content)} байт")
        return None
    if 'jpeg' in content_type or 'jpg' in content_type:
        ext = '.jpg'
    elif 'png' in content_type:
        ext = '.png'
    elif 'gif' in content_type:
        ext = '.gif'
    else:
        ext = Path(image_url.split('?')[0]).suffix or '.jpg'
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_file.write(content)
        temp_path = temp_file.name
    logging.info(f"[OCR] ✅ Изображение скачано через воркер: {len(content)} байт")
    return temp_path


async def analyze_court_document_image(
    image_url: str,
    model_name: str = 'gemini-1.5-flash-002'
) -> Optional[Dict[str, str]]:
    """
    Анализирует изображение судебного документа с помощью Gemini Vision.

    Args:
        image_url: URL изображения документа
        model_name: Название модели Gemini для использования

    Returns:
        Словарь с результатами анализа или None при ошибке
    """
    temp_file_path = None

    try:
        # Скачиваем изображение
        temp_file_path = await _download_image(image_url)
        if not temp_file_path:
            return None

        # Создаем Part с изображением
        with open(temp_file_path, 'rb') as f:
            image_data = f.read()

        image_part = types.Part(inline_data=types.Blob(
            data=image_data,
            mime_type=f'image/{Path(temp_file_path).suffix[1:]}'
        ))

        if DEBUG_OCR:
            logging.info(f"[OCR-DEBUG] Отправка на анализ моделью {model_name}, размер: {len(image_data)} байт")

        # Получаем genai client с правильным регионом для модели
        client = get_client_for_model(model_name)
        if not client:
            raise RuntimeError("Не удалось получить genai client - RegionManager не инициализирован")

        # Отправляем запрос к модели
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=[types.Part(text=OCR_ANALYSIS_PROMPT), image_part],
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=4096
            )
        )

        if not response or not response.text:
            logging.error("[OCR] ❌ Пустой ответ от модели")
            return None

        raw_text = response.text
        logging.info(f"[OCR] ✅ Получен ответ от модели, длина: {len(raw_text)} символов")

        if DEBUG_OCR:
            logging.info(f"[OCR-DEBUG] Первые 300 символов ответа: {raw_text[:300]}...")

        # Парсим ответ модели
        result = _parse_ocr_response(raw_text)
        result['raw_response'] = raw_text

        return result

    except Exception as e:
        logging.error(f"[OCR] ❌ Ошибка при анализе изображения {image_url}: {e}", exc_info=DEBUG_OCR)
        return None

    finally:
        # Удаляем временный файл
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.unlink(temp_file_path)
                if DEBUG_OCR:
                    logging.info(f"[OCR-DEBUG] Временный файл удален: {temp_file_path}")
            except Exception as e:
                logging.warning(f"[OCR] ⚠️ Не удалось удалить временный файл: {e}")


def _parse_ocr_response(raw_text: str) -> Dict[str, str]:
    """
    Парсит структурированный ответ модели OCR.
    """
    import re

    result = {
        'document_type': 'неизвестно',
        'ocr_text': raw_text,
        'date': None,
        'participants': None,
        'resolution': None
    }

    # Извлекаем тип документа
    type_match = re.search(r'ТИП ДОКУМЕНТА:\s*(.+)', raw_text, re.IGNORECASE)
    if type_match:
        result['document_type'] = type_match.group(1).strip()

    # Извлекаем дату
    date_match = re.search(r'ДАТА:\s*(.+)', raw_text, re.IGNORECASE)
    if date_match:
        result['date'] = date_match.group(1).strip()

    # Извлекаем полный текст
    full_text_match = re.search(
        r'ПОЛНЫЙ ТЕКСТ:\s*(.+)',
        raw_text,
        re.IGNORECASE | re.DOTALL
    )
    if full_text_match:
        result['ocr_text'] = full_text_match.group(1).strip()

    return result


async def process_multiple_images(
    images: list[Dict[str, str]],
    model_name: str = 'gemini-1.5-flash-002'
) -> list[Dict]:
    """
    Обрабатывает несколько изображений с задержками между запросами.
    """
    logging.info(f"[OCR] 🚀 Начало обработки {len(images)} изображений")

    results = []

    for idx, img in enumerate(images, 1):
        if DEBUG_OCR:
            logging.info(f"[OCR-DEBUG] Обработка изображения {idx}/{len(images)}")

        # Задержка между изображениями для избежания rate limit
        if idx > 1:
            delay = 2.0  # 2 секунды между запросами
            if DEBUG_OCR:
                logging.info(f"[OCR-DEBUG] Задержка {delay}с между изображениями...")
            await asyncio.sleep(delay)

        try:
            result = await analyze_court_document_image(img['url'], model_name)
            results.append(result)

            if result:
                logging.info(f"[OCR] ✅ Изображение {idx}/{len(images)} обработано успешно")
            else:
                logging.warning(f"[OCR] ⚠️ Изображение {idx}/{len(images)} не обработано")

        except Exception as e:
            logging.error(f"[OCR] ❌ Ошибка обработки изображения {idx}: {e}")
            results.append(None)

    successful = sum(1 for r in results if r is not None)
    logging.info(f"[OCR] ✅ Обработка завершена. Успешно: {successful}/{len(images)}")

    return results


# --- Тестирование ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("OCR процессор готов к использованию")

