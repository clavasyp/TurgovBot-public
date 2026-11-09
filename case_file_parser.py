# START OF FILE case_file_parser.py
import asyncio
import logging
import re
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError


async def _fetch_gdoc_content(doc_id: str) -> str | None:
    """
    Загружает текстовое содержимое Google документа по его ID.
    Сначала пробует прямой экспорт; если владелец включил защиту от
    копирования (экспорт отдаёт 401/403 даже у публичного документа) —
    забирает текст из упрощённой версии /mobilebasic.
    """
    export_url = f'https://docs.google.com/document/d/{doc_id}/export?format=txt'
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            logging.info(f"[CaseParser] Загружаю Google Doc с ID: {doc_id}")
            response = await client.get(export_url, timeout=30.0)
            if response.status_code in (401, 403):
                logging.info(
                    f"[CaseParser] Экспорт Google Doc {doc_id} закрыт ({response.status_code}), "
                    f"пробую /mobilebasic")
                return await _fetch_gdoc_mobilebasic(client, doc_id)
            response.raise_for_status()
            response.encoding = 'utf-8'
            return response.text
    except Exception as e:
        logging.error(f"[CaseParser] Не удалось загрузить Google Doc с ID {doc_id}", exc_info=e)
        return None


async def _fetch_gdoc_mobilebasic(client: httpx.AsyncClient, doc_id: str) -> str | None:
    """
    Фолбэк для документов с защитой от копирования: /export закрыт,
    но упрощённая мобильная версия остаётся доступной и содержит весь текст.
    """
    url = f'https://docs.google.com/document/d/{doc_id}/mobilebasic'
    response = await client.get(url, timeout=30.0)
    if 'accounts.google.com' in str(response.url):
        # Редирект на логин = документ вообще не расшарен по ссылке
        logging.warning(f"[CaseParser] Google Doc {doc_id} недоступен без входа в аккаунт "
                        f"(доступ по ссылке не открыт)")
        return None
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    for tag in soup(['script', 'style']):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text(separator='\n').splitlines())
    text = '\n'.join(line for line in lines if line)
    if text:
        logging.info(f"[CaseParser] Google Doc {doc_id} получен через /mobilebasic "
                     f"({len(text)} символов)")
        return text
    return None


async def _fetch_docossum_content_playwright(page: Page, url: str) -> str | None:
    """
    --- ФИНАЛЬНАЯ ВЕРСИЯ 3.0: РЕШЕНИЕ ПРОБЛЕМЫ STRICT MODE ---
    Использует .all_inner_texts() для сбора текста из МНОЖЕСТВА
    элементов, найденных по селектору.
    """
    try:
        logging.info(f"[CaseParser/Playwright] Перехожу на страницу Docossum: {url}")
        await page.goto(url, timeout=60000, wait_until='networkidle')

        content_selector = "div.tiptap.ProseMirror"

        # Ждем появления первого из множества элементов
        await page.wait_for_selector(content_selector, timeout=15000)

        content_locator = page.locator(content_selector)

        # Проверяем, что найден хотя бы один элемент
        if await content_locator.count() > 0:
            logging.info(
                f"[CaseParser/Playwright] УСПЕХ! Найдено {await content_locator.count()} текстовых блоков. Собираю их вместе...")

            # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ ---
            # Собираем текст из ВСЕХ найденных элементов в список
            list_of_texts = await content_locator.all_inner_texts()

            # Объединяем список в один большой текст, разделяя блоки двойным переносом строки
            full_text = "\n\n".join(list_of_texts)

            return full_text
        else:
            logging.error(f"[CaseParser/Playwright] Элементы по селектору '{content_selector}' не найдены.")
            return None

    except PlaywrightTimeoutError:
        logging.error(
            f"[CaseParser/Playwright] Ошибка таймаута: элемент '{content_selector}' не появился на странице {url} вовремя.")
        return None
    except Exception as e:
        logging.error(f"[CaseParser/Playwright] Критическая ошибка при парсинге Docossum: {url}", exc_info=e)
        return None


async def fetch_case_content_from_url(page: Page | None, url: str) -> str | None:
    """
    Главная функция-маршрутизатор.
    """
    if "docs.google.com/document/d/" in url:
        match = re.search(r'/document/d/([a-zA-Z0-9-_]+)', url)
        if match:
            doc_id = match.group(1)
            return await _fetch_gdoc_content(doc_id)

    elif "docossum.space" in url or "docossum.com" in url:
        if not page:
            logging.error("[CaseParser] Ошибка: для парсинга Docossum требуется объект страницы Playwright.")
            return None
        return await _fetch_docossum_content_playwright(page, url)

    else:
        logging.warning(f"[CaseParser] Получена неподдерживаемая ссылка: {url}")
        return None

# END OF FILE case_file_parser.py