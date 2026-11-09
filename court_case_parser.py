# court_case_parser.py
"""
Парсер судебных дел с форума Majestic RP.

Функционал:
- Парсинг темы форума (1 тема = 1 судебное дело)
- Извлечение всех сообщений в хронологическом порядке
- Обнаружение BB-кода изображений ([IMG]...[/IMG])
- Извлечение URL изображений из различных фотохостингов (imgur, postimg и др.)
- Определение автора и роли участника (истец/ответчик/судья)
"""

import asyncio
import logging
import os
import re
import time
from typing import Optional, List, Dict
from urllib.parse import urlsplit
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup


# --- Авторизация на форумах, где темы видны только залогиненным ---
# forum.majestic-rp.ru — судебные дела за логином с 2026-07; forum.russia.online — за
# логином вообще всё, включая суды и жалобы. Оба XenForo с одинаковой формой входа.
# Зеркалит FORUM_LOGINS воркера (parser_worker/worker.py): те же env-переменные.
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


def _login_host(url: str) -> Optional[str]:
    """Хост URL, если для него настроен вход по паролю; иначе None."""
    host = (urlsplit(url).hostname or '').lower()
    return host if host in FORUM_LOGINS else None

# --- forum.rmrp.ru (DDoS-Guard) ---
# Этот парсер ходит ЛОКАЛЬНЫМ Chromium из контейнера бота (не через parser-worker), а DDoS-Guard
# рейт-лимитит по IP. Для rmrp: пауза между загрузками (конкурентные запросы юзеров сериализуются)
# и блокировка тяжёлых субресурсов (css/img/fonts; СКРИПТЫ ОСТАВЛЯЕМ — спойлеры раскрываются JS).
# majestic/gta5rp путь не меняется ни на байт.
RMRP_HOST = 'forum.rmrp.ru'


def _env_float(name: str, default: float) -> float:
    """Нечисловой env не должен ронять импорт модуля (а с ним весь бот) — тихо берём дефолт."""
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logging.warning(f"[CourtParser] Некорректное значение env {name} — использую {default}")
        return default


RMRP_FETCH_DELAY_SECONDS = _env_float('RMRP_COURT_FETCH_DELAY_SECONDS', 8.0)
_rmrp_fetch_lock = asyncio.Lock()
_rmrp_last_fetch = 0.0


def _is_rmrp_url(url: str) -> bool:
    return RMRP_HOST in (url or '')


# Форумы за Cloudflare: локальный headless-браузер получает от них 403, тему берёт только
# воркер (у него FlareSolverr). Зеркалит CF_PROTECTED_HOSTS воркера.
CF_FORUM_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get('CF_PROTECTED_HOSTS', 'forum.gta5rp.com').split(',')
    if h.strip()
)


def _is_cf_forum(url: str) -> bool:
    return any(host in (url or '').lower() for host in CF_FORUM_HOSTS)


async def _rmrp_throttle() -> None:
    """Выдерживает паузу между обращениями к forum.rmrp.ru (по всем юзерам процесса)."""
    global _rmrp_last_fetch
    if RMRP_FETCH_DELAY_SECONDS <= 0:
        return
    async with _rmrp_fetch_lock:
        wait = RMRP_FETCH_DELAY_SECONDS - (time.monotonic() - _rmrp_last_fetch)
        if wait > 0:
            await asyncio.sleep(wait)
        _rmrp_last_fetch = time.monotonic()


async def _rmrp_lighten_page(page) -> None:
    """Блокирует стили/картинки/шрифты/медиа: текст дела в HTML, а лишние same-host запросы
    тратят лимит DDoS-Guard. Скрипты и XHR не блокируем — нужны для раскрытия спойлеров."""
    async def _route(route):
        if route.request.resource_type in ('stylesheet', 'image', 'media', 'font'):
            await route.abort()
        else:
            await route.continue_()
    await page.route('**/*', _route)


async def _wait_challenge_passed(page, tries: int = 15) -> None:
    """Ждёт прохождения vddos JS-челленджа majestic (заглушка slowAES → вычисляет cookie R3ACTLB и reload)."""
    for _ in range(tries):
        try:
            if 'slowAES' not in (await page.content()):
                return
        except Exception:
            pass
        await asyncio.sleep(2)


async def _is_forum_authed(context, host: str) -> bool:
    """Есть ли xf_user ИМЕННО ЭТОГО форума.

    «Любой xf_user в контексте» — негодная проверка: в одном контексте могут жить сессии
    двух форумов, и cookie соседа выдала бы неавторизованный форум за авторизованный."""
    for c in await context.cookies():
        if c.get('name') != 'xf_user':
            continue
        domain = (c.get('domain') or '').lstrip('.').lower()
        if domain and (host == domain or host.endswith('.' + domain)):
            return True
    return False


async def _login_forum(context, host: str) -> bool:
    """Логин на форуме host в переданном контексте — cookies (xf_user) наследуют все страницы."""
    cfg = FORUM_LOGINS.get(host)
    if not cfg:
        return False
    user = os.environ.get(cfg['user_env']) or None
    password = os.environ.get(cfg['password_env']) or None
    if not user or not password:
        logging.warning(
            f"[CourtParser] {cfg['user_env']}/{cfg['password_env']} не заданы — темы {host} недоступны"
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
                return True
            logging.error(f"[CourtParser] Форма входа {host} не найдена")
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
        logging.info(f"[CourtParser] Авторизация {host}: {'успех (xf_user получен)' if ok else 'НЕ удалась'}")
        return ok
    except Exception as e:
        logging.error(f"[CourtParser] Ошибка авторизации {host}: {e}")
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


# DEBUG: Флаг для включения/выключения детального логирования
DEBUG_PARSER = False  # Установите True для отладки


# Регулярное выражение для парсинга BB-кода изображений
# Примеры:
# [IMG]https://i.imgur.com/mOfwQCU.png[/IMG]
# [IMG size="1328x1724"]https://i.imgur.com/mOfwQCU.png[/IMG]
# [img]http://example.com/image.jpg[/img]
BBCODE_IMAGE_PATTERN = re.compile(
    r'\[IMG(?:\s+size="[^"]+")?\](https?://[^]]+)\[/IMG\]',
    re.IGNORECASE
)

# Регулярное выражение для извлечения URL изображений из HTML img тегов
HTML_IMAGE_PATTERN = re.compile(
    r'<img[^>]+src=["\'](https?://[^"\']+)["\'][^>]*>',
    re.IGNORECASE
)

# Регулярное выражение для извлечения URL из data-src (прокси-изображения)
DATA_SRC_PATTERN = re.compile(
    r'data-src=["\'](/proxy\.php\?image=([^&"\']+)[^"\']*)["\']',
    re.IGNORECASE
)


async def _extract_images_from_html(html_content: str) -> List[Dict[str, str]]:
    """
    Извлекает URL изображений из HTML тегов <img>.

    Args:
        html_content: HTML контент сообщения

    Returns:
        Список словарей с информацией об изображениях
    """
    images = []

    # Сначала ищем data-src (прокси-изображения форума)
    data_src_matches = DATA_SRC_PATTERN.finditer(html_content)
    for match in data_src_matches:
        encoded_url = match.group(2)
        # Декодируем URL из URL-encoding
        try:
            from urllib.parse import unquote
            image_url = unquote(encoded_url)

            if DEBUG_PARSER:
                logging.info(f"[CourtParser-DEBUG] Найдено изображение в data-src: {image_url}")

            images.append({
                'bbcode': f'[IMG]{image_url}[/IMG]',
                'url': image_url,
            })
            logging.info(f"[CourtParser] Найдено изображение через прокси (data-src): {image_url}")
        except Exception as e:
            logging.warning(f"[CourtParser] Ошибка декодирования URL из data-src: {e}")

    # Затем ищем обычные src
    matches = HTML_IMAGE_PATTERN.finditer(html_content)
    for match in matches:
        image_url = match.group(1)

        # Фильтруем служебные изображения (смайлики, аватары и т.д.)
        if any(skip in image_url.lower() for skip in [
            'emoji', 'smilie', 'avatar', 'styles/default',
            'data:image', 'xenforo', 'data/avatars', 'proxy.php'  # Пропускаем прокси, т.к. уже обработали data-src
        ]):
            continue

        images.append({
            'bbcode': f'[IMG]{image_url}[/IMG]',
            'url': image_url,
        })

        logging.info(f"[CourtParser] Найдено изображение в HTML: {image_url}")

    return images


async def _extract_images_from_bbcode(text_content: str) -> List[Dict[str, str]]:
    """
    Извлекает URL изображений из BB-кода в тексте сообщения.

    Args:
        text_content: Текст сообщения с форума

    Returns:
        Список словарей с информацией об изображениях:
        [
            {
                'bbcode': '[IMG]url[/IMG]',
                'url': 'https://...',
            }
        ]
    """
    images = []
    matches = BBCODE_IMAGE_PATTERN.finditer(text_content)

    for match in matches:
        full_bbcode = match.group(0)
        image_url = match.group(1)

        images.append({
            'bbcode': full_bbcode,
            'url': image_url,
        })

        logging.info(f"[CourtParser] Найдено изображение в BB-коде: {image_url}")

    return images


def _clean_html_to_text(html_content: str) -> str:
    """
    Очищает HTML от тегов и технических элементов, оставляя только чистый текст.

    Args:
        html_content: HTML контент сообщения

    Returns:
        Чистый текст без HTML тегов и технических элементов
    """
    try:
        # Используем BeautifulSoup для парсинга HTML
        soup = BeautifulSoup(html_content, 'html.parser')

        # Удаляем скрипты, стили и другие технические элементы
        for element in soup(['script', 'style', 'meta', 'link', 'noscript', 'iframe']):
            element.decompose()

        # >>> НАЧАЛО: ОБРАБОТКА СПОЙЛЕРОВ <<<
        # Ищем все спойлеры и извлекаем их содержимое
        for spoiler in soup.find_all(class_='bbCodeSpoiler'):
            # Ищем заголовок спойлера
            spoiler_title = spoiler.find(class_='bbCodeSpoiler-button-title')
            title_text = ""
            if spoiler_title:
                title_text = spoiler_title.get_text(strip=True)

            # Ищем содержимое спойлера
            spoiler_content = spoiler.find(class_='bbCodeSpoiler-content')

            # Заменяем весь спойлер на текст с пометкой
            replacement_text = f"\n[СПОЙЛЕР: {title_text}]\n"
            if spoiler_content:
                replacement_text += spoiler_content.get_text(separator='\n', strip=True)
            replacement_text += "\n[/СПОЙЛЕР]\n"

            spoiler.replace_with(replacement_text)
        # >>> КОНЕЦ: ОБРАБОТКА СПОЙЛЕРОВ <<<

        # Удаляем элементы с техническими классами (кнопки, иконки и т.д.)
        technical_classes = [
            'bbCodeBlock-expandLink', 'bbCodeBlock-shrinkLink',
            'actionBar', 'message-actionBar', 'reactionsBar',
            'attribution', 'message-attribution', 'message-cell--user',
            'message-userDetails', 'message-avatar', 'username',
            'userTitle', 'userBanner', 'message-signature'
        ]
        for class_name in technical_classes:
            for element in soup.find_all(class_=class_name):
                element.decompose()

        # Заменяем теги переноса строк на символы переноса
        for br in soup.find_all('br'):
            br.replace_with('\n')

        # Заменяем параграфы на перенос строки
        for p in soup.find_all('p'):
            p.insert_after('\n')

        # Заменяем div на перенос строки
        for div in soup.find_all('div'):
            if div.get_text(strip=True):  # Только если есть текст
                div.insert_after('\n')

        # Получаем текст
        text = soup.get_text(separator=' ', strip=True)

        # Очищаем множественные пробелы и переносы строк
        lines = []
        for line in text.split('\n'):
            # Убираем лишние пробелы в строке
            cleaned_line = ' '.join(line.split())
            if cleaned_line:  # Только непустые строки
                lines.append(cleaned_line)

        # Объединяем обратно с одиночными переносами строк
        clean_text = '\n'.join(lines)

        # Убираем множественные переносы строк (оставляем максимум 2 подряд)
        import re
        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)

        return clean_text.strip()
    except Exception as e:
        logging.warning(f"[CourtParser] Ошибка при очистке HTML: {e}")
        # В случае ошибки возвращаем исходный текст
        return html_content


async def _parse_forum_thread(page: Page, thread_url: str) -> Optional[Dict]:
    """
    Парсит тему с форума и извлекает все сообщения.

    Args:
        page: Экземпляр Playwright Page
        thread_url: URL темы на форуме

    Returns:
        Словарь с данными дела или None при ошибке:
        {
            'thread_id': '12345',
            'thread_title': 'Иск к гражданину Иванову',
            'thread_url': 'https://...',
            'messages': [...]
        }
    """
    try:
        logging.info(f"[CourtParser] Переход на страницу темы: {thread_url}")
        if _is_rmrp_url(thread_url):
            # rmrp: облегчённая загрузка + троттлинг; networkidle не ждём (заблокированные
            # субресурсы иначе держали бы ожидание), сообщения дождётся wait_for_selector ниже.
            await _rmrp_lighten_page(page)
            await _rmrp_throttle()
            await page.goto(thread_url, timeout=60000, wait_until='domcontentloaded')
        else:
            await page.goto(thread_url, timeout=60000, wait_until='networkidle')

        # Пройти vddos JS-челлендж (majestic и russia.online — одна и та же защита slowAES) и
        # дождаться прогрузки сообщений темы, иначе извлечение уйдёт по частично
        # отрендеренной/челлендж-странице.
        if _login_host(thread_url):
            await _wait_challenge_passed(page)
        try:
            await page.wait_for_selector('article.message', timeout=20000)
        except Exception:
            pass

        # >>> НАЧАЛО: РАСКРЫТИЕ ВСЕХ СПОЙЛЕРОВ НА СТРАНИЦЕ <<<
        # Ищем и раскрываем все спойлеры (bbCodeSpoiler)
        try:
            # Ищем все кнопки раскрытия спойлеров
            spoiler_buttons = await page.locator('a.bbCodeSpoiler-button, button.bbCodeSpoiler-button, div.bbCodeSpoiler-button').all()

            if spoiler_buttons:
                logging.info(f"[CourtParser] Найдено спойлеров для раскрытия: {len(spoiler_buttons)}")

                for idx, button in enumerate(spoiler_buttons, start=1):
                    try:
                        # Проверяем, видима ли кнопка
                        if await button.is_visible():
                            await button.click(timeout=2000)
                            # Небольшая задержка для анимации раскрытия
                            await page.wait_for_timeout(100)
                            logging.info(f"[CourtParser] Раскрыт спойлер #{idx}")
                    except Exception as e:
                        logging.warning(f"[CourtParser] Не удалось раскрыть спойлер #{idx}: {e}")

                # Даем время на полное раскрытие всех спойлеров
                await page.wait_for_timeout(500)
                logging.info(f"[CourtParser] Все спойлеры раскрыты")
            else:
                logging.info(f"[CourtParser] Спойлеры на странице не найдены")

        except Exception as e:
            logging.warning(f"[CourtParser] Ошибка при раскрытии спойлеров: {e}")
        # >>> КОНЕЦ: РАСКРЫТИЕ СПОЙЛЕРОВ <<<

        # Извлекаем ID темы из URL
        # Пример URL: https://forum.majestic-rp.ru/threads/название.12345/
        thread_id_match = re.search(r'threads/[^/]+\.(\d+)', thread_url)
        thread_id = thread_id_match.group(1) if thread_id_match else None

        # Извлекаем название темы
        title_selector = 'h1.p-title-value'
        thread_title = await page.locator(title_selector).inner_text(timeout=10000)
        logging.info(f"[CourtParser] Название дела: {thread_title}")

        # Извлекаем все сообщения
        messages_selector = 'article.message'
        message_elements = await page.locator(messages_selector).all()

        if not message_elements:
            logging.warning("[CourtParser] Сообщения не найдены на странице")
            return None

        logging.info(f"[CourtParser] Найдено сообщений: {len(message_elements)}")

        messages = []
        for idx, msg_element in enumerate(message_elements, start=1):
            # Извлекаем автора сообщения
            try:
                author_elem = msg_element.locator('a.username')
                author = await author_elem.inner_text(timeout=5000)
            except Exception:
                author = "Неизвестный"

            # Извлекаем время публикации
            try:
                time_elem = msg_element.locator('time')
                message_timestamp = await time_elem.get_attribute('datetime', timeout=5000)
            except Exception:
                message_timestamp = None

            # Извлекаем текст сообщения
            # Используем innerText для получения чистого текста без HTML-разметки
            try:
                content_elem = msg_element.locator('article.message-body div.bbWrapper')

                # >>> НАЧАЛО: ДОПОЛНИТЕЛЬНОЕ РАСКРЫТИЕ СПОЙЛЕРОВ В СООБЩЕНИИ <<<
                # На случай если глобальное раскрытие не сработало, раскрываем спойлеры в конкретном сообщении
                try:
                    spoilers_in_msg = await msg_element.locator('a.bbCodeSpoiler-button, button.bbCodeSpoiler-button').all()
                    if spoilers_in_msg:
                        for spoiler_btn in spoilers_in_msg:
                            try:
                                if await spoiler_btn.is_visible():
                                    await spoiler_btn.click(timeout=1000)
                                    await page.wait_for_timeout(50)
                            except:
                                pass  # Игнорируем ошибки при клике на отдельные спойлеры
                except:
                    pass  # Игнорируем ошибки при поиске спойлеров в сообщении
                # >>> КОНЕЦ: ДОПОЛНИТЕЛЬНОЕ РАСКРЫТИЕ СПОЙЛЕРОВ <<<

                # Сначала пытаемся получить чистый текст через innerText
                try:
                    # innerText возвращает текст как его видит пользователь (без HTML тегов)
                    text_content = await content_elem.inner_text(timeout=5000)
                except:
                    # Fallback 1: используем textContent
                    try:
                        text_content = await content_elem.text_content(timeout=5000)
                    except:
                        # Fallback 2: получаем innerHTML и очищаем его
                        html_content = await content_elem.evaluate('el => el.innerHTML', timeout=5000)
                        text_content = _clean_html_to_text(html_content)

            except Exception as e:
                logging.warning(f"[CourtParser] Ошибка при извлечении текста сообщения #{idx}: {e}")
                text_content = ""

            # Извлекаем изображения из BB-кода и HTML
            images = []

            # Поиск в BB-коде
            bb_images = await _extract_images_from_bbcode(text_content)
            images.extend(bb_images)

            # Поиск в HTML (если BB-код не найден)
            if len(images) == 0 and '<img' in text_content.lower():
                html_images = await _extract_images_from_html(text_content)
                images.extend(html_images)

            # НОВЫЙ МЕТОД: Парсинг изображений напрямую из DOM
            if len(images) == 0:
                try:
                    img_elements = await msg_element.locator('div.bbWrapper img').all()
                    for img_elem in img_elements:
                        img_src = await img_elem.get_attribute('src')
                        if img_src and img_src.startswith('http'):
                            # Фильтруем служебные изображения
                            if not any(skip in img_src.lower() for skip in [
                                'emoji', 'smilie', 'avatar', 'styles/default',
                                'data:image', 'xenforo', 'data/avatars'
                            ]):
                                images.append({
                                    'bbcode': f'[IMG]{img_src}[/IMG]',
                                    'url': img_src,
                                })
                                logging.info(f"[CourtParser] Найдено изображение в DOM: {img_src}")
                except Exception as e:
                    logging.warning(f"[CourtParser] Ошибка при парсинге изображений из DOM: {e}")

            # Логирование для отладки
            if len(images) > 0:
                logging.info(f"[CourtParser] В сообщении #{idx} найдено изображений: {len(images)}")
            elif '[IMG' in text_content.upper() or '<img' in text_content.lower():
                if DEBUG_PARSER:
                    # В DEBUG режиме показываем больше контекста
                    logging.warning(
                        f"[CourtParser-DEBUG] В сообщении #{idx} есть теги изображений, но они не извлечены.\n"
                        f"  Первые 500 символов контента:\n{text_content[:500]}\n"
                        f"  Есть [IMG]: {'[IMG' in text_content.upper()}\n"
                        f"  Есть <img: {'<img' in text_content.lower()}\n"
                        f"  Количество найденных img элементов в DOM: будет проверено..."
                    )
                else:
                    logging.warning(
                        f"[CourtParser] В сообщении #{idx} есть теги изображений, "
                        f"но они не были извлечены. Включите DEBUG_PARSER для деталей."
                    )

            # Определяем роль автора (простая эвристика, можно улучшить)
            author_role = _determine_author_role(author, idx, thread_title)

            message_data = {
                'message_order': idx,
                'author': author,
                'author_role': author_role,
                'message_timestamp': message_timestamp,
                'text_content': text_content,
                'has_images': len(images) > 0,
                'images': images
            }

            messages.append(message_data)
            logging.info(
                f"[CourtParser] Сообщение #{idx}: автор={author}, "
                f"роль={author_role}, изображений={len(images)}"
            )

        # Определяем истца и ответчика (из первого сообщения и заголовка)
        plaintiff, defendant = _extract_parties(messages, thread_title)

        case_data = {
            'thread_id': thread_id,
            'thread_title': thread_title,
            'thread_url': thread_url,
            'plaintiff': plaintiff,
            'defendant': defendant,
            'messages': messages
        }

        logging.info(
            f"[CourtParser] Парсинг завершен. Сообщений: {len(messages)}, "
            f"Истец: {plaintiff}, Ответчик: {defendant}"
        )

        return case_data

    except PlaywrightTimeoutError:
        logging.error(f"[CourtParser] Таймаут при загрузке страницы: {thread_url}")
        return None
    except Exception as e:
        logging.error(f"[CourtParser] Ошибка при парсинге темы: {e}", exc_info=True)
        return None


def _determine_author_role(author: str, message_order: int, thread_title: str) -> str:
    """
    Определяет роль автора сообщения.

    Эвристика:
    - Первое сообщение обычно от истца
    - Сообщения от пользователей с префиксом "Судья" или "Judge" - от судьи
    - Остальные - ответчик или другие участники

    Args:
        author: Имя автора
        message_order: Порядковый номер сообщения
        thread_title: Название темы

    Returns:
        Роль: 'plaintiff', 'defendant', 'judge', 'court', 'other'
    """
    author_lower = author.lower()

    # Проверяем, судья ли это
    if any(keyword in author_lower for keyword in ['судья', 'judge', 'суд']):
        return 'judge'

    # Первое сообщение обычно от истца
    if message_order == 1:
        return 'plaintiff'

    # Остальные участники - ответчики или другие
    return 'defendant'


def _extract_parties(messages: List[Dict], thread_title: str) -> tuple[str, str]:
    """
    Извлекает имена истца и ответчика из данных дела.

    Args:
        messages: Список сообщений
        thread_title: Название темы

    Returns:
        Кортеж (истец, ответчик)
    """
    plaintiff = "Неизвестно"
    defendant = "Неизвестно"

    # Истец - автор первого сообщения
    if messages:
        plaintiff = messages[0]['author']

    # Ответчик - попытка извлечь из заголовка
    # Пример: "Иск к гражданину Иванову"
    match = re.search(r'иск (?:к|на) (?:гражданину|гражданке)?\s*(.+)', thread_title, re.IGNORECASE)
    if match:
        defendant = match.group(1).strip()

    return plaintiff, defendant


# Список поддерживаемых форумов
SUPPORTED_FORUMS = [
    'forum.majestic-rp.ru',
    'forum.gta5rp.com',
    'forum.rmrp.ru',
    'forum.russia.online'
]


def is_supported_forum_url(url: str) -> bool:
    """
    Проверяет, принадлежит ли URL одному из поддерживаемых форумов.

    Args:
        url: URL для проверки

    Returns:
        True если URL поддерживается, иначе False
    """
    return any(forum in url for forum in SUPPORTED_FORUMS)


async def _case_from_worker(thread_url: str) -> Optional[Dict]:
    """Дело через parser-worker: он ходит на форум своим транспортом (FlareSolverr для
    Cloudflare-хостов, троттлинг для рейт-лимитируемых), поэтому берёт gta5rp, который
    локальному Chromium отдаёт 403. None → вызывающий падает на локальный путь."""
    try:
        from remote_parser_client import remote_parse_thread
    except Exception as e:  # окружение без конфигурации воркера
        logging.warning(f"[CourtParser] remote-парсер недоступен: {e}")
        return None

    data = await remote_parse_thread(thread_url)
    if isinstance(data, dict) and data.get('success') is False:
        # Воркер дошёл до форума, но не смог. Для CF-форума локальный браузер тем более не
        # сможет (403 → минуты ожидания перед той же ошибкой) — не мучаем пользователя.
        if _is_cf_forum(thread_url):
            logging.error(f"[CourtParser] Воркер не смог разобрать CF-тему: {data.get('error')}")
            return None
    if not data or not data.get('messages'):
        return None

    thread_title = data.get('title') or ''
    messages: List[Dict] = []
    for raw in data['messages']:
        images = [
            {'bbcode': f"[IMG]{url}[/IMG]", 'url': url}
            for url in (raw.get('images') or [])
        ]
        order = raw.get('order') or (len(messages) + 1)
        author = raw.get('author') or 'Неизвестный'
        messages.append({
            'message_order': order,
            'author': author,
            'author_role': _determine_author_role(author, order, thread_title),
            'message_timestamp': raw.get('timestamp'),
            'text_content': raw.get('text') or '',
            'has_images': bool(images),
            'images': images,
        })

    plaintiff, defendant = _extract_parties(messages, thread_title)
    if data.get('truncated'):
        logging.warning(f"[CourtParser] Тема обрезана капом страниц: {thread_url}")
    logging.info(
        f"[CourtParser] Дело получено через воркер: сообщений={len(messages)}, "
        f"изображений={sum(len(m['images']) for m in messages)}"
    )
    return {
        'thread_id': str(data['thread_id']) if data.get('thread_id') else None,
        'thread_title': thread_title,
        'thread_url': thread_url,
        'plaintiff': plaintiff,
        'defendant': defendant,
        'messages': messages,
    }


async def parse_court_case_from_url(thread_url: str) -> Optional[Dict]:
    """
    Основная функция для парсинга судебного дела с форума.

    Сначала пробуем parser-worker: у него транспорт с обходом Cloudflare (forum.gta5rp.com
    иначе отдаёт локальному браузеру 403) и троттлингом. Если воркер недоступен или не смог —
    падаем на локальный Playwright, который умеет логин majestic и клики по спойлерам.

    Args:
        thread_url: URL темы на форуме

    Returns:
        Структурированные данные дела или None при ошибке
    """
    # Проверяем, что URL ведет на поддерживаемый форум
    if not is_supported_forum_url(thread_url):
        logging.error(f"[CourtParser] Неверный URL форума: {thread_url}")
        return None

    case_data = await _case_from_worker(thread_url)
    if case_data:
        return case_data
    logging.info(f"[CourtParser] Воркер не дал результат, пробую локальный браузер: {thread_url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            locale='ru-RU'
        )

        # Темы majestic и russia.online закрыты за логином → авторизуемся в контексте перед
        # парсингом (cookies xf_user наследует страница темы). gta5rp/rmrp публичны — вход не нужен.
        _lh = _login_host(thread_url)
        if _lh:
            await _login_forum(context, _lh)

        page = await context.new_page()

        try:
            case_data = await _parse_forum_thread(page, thread_url)
            return case_data
        finally:
            await browser.close()


# --- Тестирование ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Пример использования
    test_url = "https://forum.majestic-rp.ru/threads/test.12345/"

    async def test():
        result = await parse_court_case_from_url(test_url)
        if result:
            print(f"✅ Дело распарсено: {result['thread_title']}")
            print(f"   Сообщений: {len(result['messages'])}")
            print(f"   Истец: {result['plaintiff']}")
            print(f"   Ответчик: {result['defendant']}")

            # Показываем изображения
            total_images = sum(len(msg['images']) for msg in result['messages'])
            print(f"   Изображений найдено: {total_images}")
        else:
            print("❌ Ошибка парсинга")

    asyncio.run(test())

