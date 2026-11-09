# START OF FILE utils.py

# utils.py (полностью обновленный файл)

# -*- coding: utf-8 -*-

import os
import re
from datetime import datetime
import functools
import logging
import logging.handlers
from telegram import Update, CallbackQuery
from telegram.ext import ContextTypes
from telegram.error import BadRequest
import asyncio

# НОВЫЙ ИМПОРТ для надежного парсинга HTML
from bs4 import BeautifulSoup, NavigableString

# ИЗМЕНЕНИЕ: Добавляем импорты для работы с Vertex AI
from google import genai
from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions

# Локальные импорты
import config
from database.manager import db_manager
from config import COMMAND_RATE_LIMIT_SECONDS


def is_resource_exhausted_error(e: Exception) -> bool:
    """
    Проверяет, является ли исключение ошибкой 429 (Resource Exhausted).
    Поддерживает как старый API (google.api_core.exceptions.ResourceExhausted),
    так и новый API (google.genai.errors.ClientError с кодом 429).
    """
    if isinstance(e, google_exceptions.ResourceExhausted):
        return True
    if isinstance(e, genai_errors.ClientError):
        # В новом google.genai API ClientError не имеет атрибута status_code
        # Проверяем через строковое представление ошибки
        error_str = str(e)
        return '429' in error_str or 'RESOURCE_EXHAUSTED' in error_str
    return False


async def safe_answer_callback_query(query: CallbackQuery, text: str = None, show_alert: bool = False) -> bool:
    """
    Безопасно отвечает на callback query, игнорируя ошибку "Query is too old".

    Args:
        query: Callback query для ответа
        text: Текст уведомления (опционально)
        show_alert: Показывать как alert вместо уведомления

    Returns:
        True если ответ успешно отправлен, False если произошла ошибка
    """
    try:
        await query.answer(text=text, show_alert=show_alert)
        return True
    except BadRequest as e:
        if "Query is too old" in str(e) or "query id is invalid" in str(e):
            # Игнорируем ошибку устаревшего query - это нормально
            logging.debug(f"Callback query устарел (это нормально): {e}")
            return False
        else:
            # Другие BadRequest ошибки логируем
            logging.warning(f"BadRequest при ответе на callback query: {e}")
            return False
    except Exception as e:
        logging.error(f"Неожиданная ошибка при ответе на callback query: {e}")
        return False


def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    log_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(module)s.%(funcName)s] - %(message)s'
    )
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_formatter)
    logger.addHandler(console_handler)
    log_dir = os.path.join(config.BASE_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, 'bot.log')
    file_handler = logging.handlers.RotatingFileHandler(
        log_file_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('gspread').setLevel(logging.WARNING)
    # Добавляем логгер для BeautifulSoup, чтобы он не спамил
    logging.getLogger('bs4').setLevel(logging.WARNING)


def debug_print(message: str):
    logging.info(message)


# --- Режим тех. работ: персистентный флаг (переживает рестарт bot/БД) ---
# Рантайм-источник — bot_data['maintenance_mode'] (быстро, читается в rate_limited),
# но он теряется при рестарте. Поэтому дублируем в system_cache: грузим на старте,
# сохраняем при переключении из админки.
_MAINTENANCE_KEY = "maintenance_mode"


async def get_maintenance_mode() -> bool:
    """Прочитать сохранённый флаг тех. работ из system_cache. По умолчанию ВЫКЛ."""
    try:
        val = await db_manager.fetchval(
            "SELECT value FROM system_cache WHERE key = $1", _MAINTENANCE_KEY
        )
    except Exception:
        return False
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().strip('"').lower() in ("true", "1")
    return bool(val)


async def set_maintenance_mode(enabled: bool) -> None:
    """Сохранить флаг тех. работ в system_cache (персистентно)."""
    await db_manager.execute(
        """
        INSERT INTO system_cache (key, value, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        _MAINTENANCE_KEY, "true" if enabled else "false",
    )


def rate_limited(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id

        # ПРОВЕРКА РЕЖИМА ТЕХНИЧЕСКИХ РАБОТ
        maintenance_mode = context.application.bot_data.get('maintenance_mode', False)
        if maintenance_mode:
            # Проходят ТОЛЬКО админы; тестеры блокируются наравне с остальными (решение
            # заказчика 2026-06-12; то же в оверлее — services/maintenance.py).
            user_record = await db_manager.fetchrow(
                "SELECT is_admin FROM users WHERE user_id = $1", user_id
            )
            is_admin = (user_record and user_record['is_admin']) or user_id == config.SUPER_ADMIN_ID

            if not is_admin:
                query = update.callback_query
                maintenance_message = (
                    "⚙️ Бот временно недоступен.\n\n"
                    "Ведутся технические работы.\n"
                    "Попробуйте позже."
                )
                if query:
                    await safe_answer_callback_query(query, maintenance_message, show_alert=True)
                else:
                    await update.message.reply_text(maintenance_message)
                logging.info(f"Пользователь {user_id} заблокирован режимом тех. работ при попытке {func.__name__}.")
                return

        # ПРОВЕРКА БАНА
        user_record = await db_manager.fetchrow("SELECT is_banned FROM users WHERE user_id = $1", user_id)
        if user_record and user_record['is_banned']:
            # Для забаненных пользователей разрешены только функции start и main_menu
            # (они показывают специальное меню с доступом к репортам)
            # Все остальные функции блокируются
            allowed_for_banned = ['start', 'main_menu']

            if func.__name__ not in allowed_for_banned:
                query = update.callback_query
                ban_message = (
                    "❌ Вы заблокированы и не можете использовать этого бота.\n\n"
                    "Для связи с администрацией используйте систему репортов."
                )
                if query:
                    await safe_answer_callback_query(query, ban_message, show_alert=True)
                else:
                    await update.message.reply_text(ban_message)
                logging.warning(f"Заблокированный пользователь {user_id} попытался использовать {func.__name__}.")
                return

        now = datetime.now()
        last_command_time = context.chat_data.get('last_command_time')
        if last_command_time:
            time_diff = (now - last_command_time).total_seconds()
            if time_diff < COMMAND_RATE_LIMIT_SECONDS:
                query = update.callback_query
                if query:
                    await safe_answer_callback_query(query, "Пожалуйста, не так часто.", show_alert=True)
                logging.warning(f"Пользователь {user_id} получил ограничение по частоте запросов (rate-limited).")
                return
        context.chat_data['last_command_time'] = now
        return await func(update, context, *args, **kwargs)

    return wrapper


async def safe_answer_callback_query(query: CallbackQuery, text: str = None, show_alert: bool = False) -> bool:
    """
    Безопасный ответ на callback query с обработкой ошибок устаревших запросов.

    Args:
        query: CallbackQuery объект
        text: Текст ответа (опционально)
        show_alert: Показать alert вместо уведомления

    Returns:
        True если ответ успешно отправлен, False если произошла ошибка
    """
    try:
        await query.answer(text=text, show_alert=show_alert)
        return True
    except BadRequest as e:
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            # Игнорируем ошибки устаревших callback query
            logging.debug(f"Игнорируем устаревший callback query: {e}")
            return False
        else:
            # Другие BadRequest ошибки логируем
            logging.warning(f"BadRequest при ответе на callback query: {e}")
            return False
    except Exception as e:
        logging.error(f"Неожиданная ошибка при ответе на callback query: {e}")
        return False


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name)


# --- >>> НАЧАЛО ИЗМЕНЕНИЙ: УДАЛЕНА АСИНХРОННАЯ ОБЕРТКА И ProcessPoolExecutor <<< ---
def sanitize_telegram_html(html_text: str) -> str:
    """
    (СИНХРОННАЯ ВЕРСИЯ) Надежно очищает HTML-текст, оставляя только теги,
    разрешенные Telegram, преобразуя заголовки в жирный текст и списки в текстовые маркеры.
    ИСПРАВЛЯЕТ незакрытые теги для предотвращения ошибок парсинга Telegram.
    """
    if not html_text:
        return ""

    try:
        # Теги, разрешенные в Telegram Bot API
        allowed_tags = {
            'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
            'a', 'code', 'pre', 'tg-spoiler'
        }

        # Используем html.parser с автоматическим исправлением незакрытых тегов
        soup = BeautifulSoup(html_text, 'html.parser')

        # ШАГ 1: Преобразуем заголовки (h1-h6) в жирный текст (<b>)
        for header_tag in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            header_tag.name = 'b'

        # ШАГ 2: Преобразуем списки (ul, ol) в текст с маркерами
        for list_tag in soup.find_all(['ul', 'ol']):
            for item_tag in list_tag.find_all('li', recursive=False):
                # Вставляем маркер перед каждым элементом списка
                item_tag.insert(0, NavigableString('• '))
            # Заменяем тег списка на его содержимое (с уже добавленными маркерами)
            list_tag.unwrap()

        # ШАГ 3: Финальная очистка. Итерируемся по всем оставшимся тегам
        for tag in soup.find_all(True):
            if tag.name not in allowed_tags:
                # Если тег не разрешен, заменяем его на его содержимое.
                tag.unwrap()
            else:
                # Если тег разрешен, удаляем все его атрибуты, кроме 'href' для тега <a>
                if tag.name == 'a':
                    href = tag.get('href')
                    tag.attrs = {}
                    if href:
                        tag['href'] = href
                else:
                    tag.attrs = {}

        # Преобразуем обработанную структуру обратно в строку
        cleaned_html = str(soup)

        # ШАГ 4: ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА - удаляем остатки некорректных тегов
        # Иногда BeautifulSoup может оставить фрагменты типа [document], <html>, <body>
        import re
        # Убираем обертки, которые BeautifulSoup может добавить
        cleaned_html = re.sub(r'^\s*<html><body>', '', cleaned_html)
        cleaned_html = re.sub(r'</body></html>\s*$', '', cleaned_html)
        cleaned_html = re.sub(r'^\s*<html>', '', cleaned_html)
        cleaned_html = re.sub(r'</html>\s*$', '', cleaned_html)
        cleaned_html = re.sub(r'^\s*<body>', '', cleaned_html)
        cleaned_html = re.sub(r'</body>\s*$', '', cleaned_html)

        # ШАГ 5: ФИНАЛЬНАЯ ПРОВЕРКА - убеждаемся, что все теги закрыты
        # Проверяем баланс тегов и закрываем незакрытые
        telegram_tags = ['b', 'i', 'u', 's', 'code', 'pre', 'a', 'tg-spoiler']
        for tag_name in telegram_tags:
            open_pattern = re.compile(rf'<{tag_name}(?:\s[^>]*)?>'.replace('a(?:\\s[^>]*)?', 'a(?:\\s+href="[^"]*")?'), re.IGNORECASE)
            close_pattern = re.compile(rf'</{tag_name}>', re.IGNORECASE)
            open_count = len(open_pattern.findall(cleaned_html))
            close_count = len(close_pattern.findall(cleaned_html))
            # Добавляем недостающие закрывающие теги в конец
            if open_count > close_count:
                cleaned_html += f'</{tag_name}>' * (open_count - close_count)

        return cleaned_html.strip()

    except Exception as e:
        logging.error("Непредвиденная ошибка в sanitize_telegram_html", exc_info=e)
        # В случае любой ошибки - удаляем ВСЕ HTML теги для безопасности
        import re
        clean_text = re.sub(r'<[^>]+>', '', html_text)
        return clean_text
# --- >>> КОНЕЦ ИЗМЕНЕНИЙ <<< ---


async def populate_servers_from_config():
    """
    Синхронизирует таблицы 'projects' и 'servers' с PROJECTS_CONFIG.
    Поддерживает иерархическую структуру проект -> серверы.
    Автоматически создает необходимые таблицы и колонки если их нет.
    """
    logging.info("Синхронизация таблицы 'projects' с PROJECTS_CONFIG...")

    # === АВТОМАТИЧЕСКАЯ МИГРАЦИЯ: Создаем таблицу projects если не существует ===
    logging.info("Проверка/создание таблицы 'projects'...")
    await db_manager.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            display_name TEXT UNIQUE NOT NULL,
            description TEXT,
            forum_base_url TEXT,
            is_disabled BOOLEAN DEFAULT FALSE
        );
    """)

    # === АВТОМАТИЧЕСКАЯ МИГРАЦИЯ: Добавляем колонку project_id в servers если её нет ===
    logging.info("Проверка колонки 'project_id' в таблице 'servers'...")
    column_exists = await db_manager.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name = 'servers' AND column_name = 'project_id'
        );
    """)

    if not column_exists:
        logging.info("Добавление колонки 'project_id' в таблицу 'servers'...")
        await db_manager.execute("""
            ALTER TABLE servers ADD COLUMN project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE;
        """)
        logging.info("Колонка 'project_id' успешно добавлена.")

    # === АВТОМАТИЧЕСКАЯ МИГРАЦИЯ: Удаляем устаревший UNIQUE constraint на display_name ===
    logging.info("Проверка/удаление устаревшего constraint 'servers_display_name_key'...")
    constraint_exists = await db_manager.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.table_constraints 
            WHERE constraint_name = 'servers_display_name_key' 
            AND table_name = 'servers'
        );
    """)

    if constraint_exists:
        logging.info("Удаление устаревшего constraint 'servers_display_name_key'...")
        await db_manager.execute("""
            ALTER TABLE servers DROP CONSTRAINT servers_display_name_key;
        """)
        logging.info("Constraint 'servers_display_name_key' успешно удален.")

    # Сначала добавляем проекты
    insert_project_query = """
        INSERT INTO projects (project_id, display_name, description, forum_base_url)
        VALUES ($1, $2, $3, $4) ON CONFLICT (project_id) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            description = EXCLUDED.description,
            forum_base_url = EXCLUDED.forum_base_url;
    """

    projects_added = 0
    for project_id, project_data in config.PROJECTS_CONFIG.items():
        display_name = project_data.get('display_name', project_id)
        description = project_data.get('description', '')
        forum_base_url = project_data.get('forum_base_url', '')

        result = await db_manager.execute(
            insert_project_query,
            project_id, display_name, description, forum_base_url
        )
        if result and '1' in result:
            projects_added += 1
            logging.info(f"В БД добавлен/обновлен проект: {project_id} ('{display_name}')")

    logging.info(f"Синхронизация проектов завершена. Обработано {len(config.PROJECTS_CONFIG)} проектов.")

    # Теперь добавляем серверы
    logging.info("Синхронизация таблицы 'servers' с SERVERS_CONFIG...")
    insert_server_query = """
        INSERT INTO servers (server_id, project_id, display_name)
        VALUES ($1, $2, $3) ON CONFLICT (server_id) DO UPDATE
        SET project_id = EXCLUDED.project_id,
            display_name = EXCLUDED.display_name;
    """

    servers_added = 0
    for server_id, server_data in config.SERVERS_CONFIG.items():
        display_name = server_data.get('display_name', server_id)
        project_id = server_data.get('project_id')

        result = await db_manager.execute(
            insert_server_query,
            server_id, project_id, display_name
        )
        if result and '1' in result:
            servers_added += 1
            logging.info(f"В БД добавлен/обновлен сервер: {server_id} ('{display_name}') в проекте {project_id}")

    logging.info(f"Синхронизация серверов завершена. Обработано {len(config.SERVERS_CONFIG)} серверов.")


async def get_enabled_servers(project_id: str = None) -> list[str]:
    """
    Возвращает список включенных серверов.
    Список серверов берётся из конфига (law_sources_data.py),
    БД используется только для проверки is_disabled статуса.

    Args:
        project_id: Опциональный ID проекта для фильтрации серверов.
                   Если не указан, возвращает все включенные серверы.

    Returns:
        Список server_id включенных серверов в порядке из конфига.
    """
    from law_sources_data import PROJECTS_DATA, get_servers_by_project

    # Получаем список отключенных серверов из БД
    disabled_query = """
        SELECT server_id FROM servers WHERE is_disabled = TRUE
    """
    disabled_records = await db_manager.fetch(disabled_query)
    disabled_server_ids = {record['server_id'] for record in disabled_records}

    # Получаем список отключенных проектов из БД
    disabled_projects_query = """
        SELECT project_id FROM projects WHERE is_disabled = TRUE
    """
    disabled_projects_records = await db_manager.fetch(disabled_projects_query)
    disabled_project_ids = {record['project_id'] for record in disabled_projects_records}

    result = []

    if project_id:
        # Проверяем, что проект не отключен
        if project_id in disabled_project_ids:
            return []

        # Берём серверы из конфига для указанного проекта
        servers = get_servers_by_project(project_id)
        for server_id in servers.keys():
            if server_id not in disabled_server_ids:
                result.append(server_id)
    else:
        # Берём все серверы из всех проектов
        for proj_id, proj_data in PROJECTS_DATA.items():
            if proj_id in disabled_project_ids:
                continue
            for server_id in proj_data.get("servers", {}).keys():
                if server_id not in disabled_server_ids:
                    result.append(server_id)

    return result


async def get_enabled_projects() -> list[str]:
    """
    Возвращает список включенных проектов.
    Список проектов берётся из конфига (law_sources_data.py),
    БД используется только для проверки is_disabled статуса.
    Порядок: majestic_rp первый, gta5rp второй.

    Returns:
        Список project_id включенных проектов.
    """
    from law_sources_data import PROJECTS_DATA

    # Получаем список отключенных проектов из БД
    disabled_query = """
        SELECT project_id FROM projects WHERE is_disabled = TRUE
    """
    disabled_records = await db_manager.fetch(disabled_query)
    disabled_project_ids = {record['project_id'] for record in disabled_records}

    # Определяем порядок проектов
    project_order = {'majestic_rp': 1, 'gta5rp': 2, 'rmrp': 3, 'russia_online': 4}

    # Берём проекты из конфига, исключая отключенные
    result = [
        proj_id for proj_id in PROJECTS_DATA.keys()
        if proj_id not in disabled_project_ids
    ]

    # Сортируем согласно заданному порядку
    result.sort(key=lambda x: project_order.get(x, 99))

    return result


async def send_project_selection_menu(
    context,
    chat_id: int,
    callback_prefix: str,
    text: str = "🌐 <b>Выберите проект:</b>",
    back_callback: str = "main_menu"
) -> None:
    """
    Отправляет меню выбора проекта с картинкой-логотипом.

    Args:
        context: Telegram context
        chat_id: ID чата для отправки
        callback_prefix: Префикс для callback_data кнопок (например, "select_project_")
        text: Текст сообщения
        back_callback: callback_data для кнопки "Назад"
    """
    import os
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton

    enabled_projects = await get_enabled_projects()

    keyboard = []
    for project_id in enabled_projects:
        project_data = config.PROJECTS_CONFIG.get(project_id, {})
        display_name = project_data.get('display_name', project_id)
        keyboard.append([InlineKeyboardButton(display_name, callback_data=f"{callback_prefix}{project_id}")])

    keyboard.append([InlineKeyboardButton("◀️ Отмена", callback_data=back_callback)])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Пытаемся отправить с картинкой
    combined_logo_path = os.path.join(config.BASE_DIR, "projects_logo.png")

    if os.path.exists(combined_logo_path):
        with open(combined_logo_path, 'rb') as photo:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
    else:
        # Fallback: отправляем без картинки
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )


async def notify_super_admin(context, error: Exception, additional_info: str = ""):
    """
    Отправляет уведомление супер-админу о критической ошибке.

    Args:
        context: Контекст Telegram бота
        error: Исключение, которое произошло
        additional_info: Дополнительная информация об ошибке
    """
    try:
        error_type = type(error).__name__
        error_message = str(error)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Ограничиваем длину сообщения об ошибке
        if len(error_message) > 500:
            error_message = error_message[:500] + "..."

        notification_text = (
            f"🚨 **КРИТИЧЕСКАЯ ОШИБКА**\n\n"
            f"**Тип:** `{error_type}`\n"
            f"**Сообщение:** `{error_message}`\n"
            f"**Время:** `{timestamp}`"
        )

        if additional_info:
            notification_text += f"\n**Доп. информация:** `{additional_info}`"

        await context.bot.send_message(
            chat_id=config.SUPER_ADMIN_ID,
            text=notification_text,
            parse_mode="Markdown"
        )
        logging.info(f"Уведомление об ошибке отправлено супер-админу (ID: {config.SUPER_ADMIN_ID})")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление об ошибке супер-админу: {e}")


async def reinitialize_vertex_ai():
    """
    Принудительно ре-инициализирует Vertex AI SDK для обновления токенов доступа.
    В новом API google.genai это не требуется, так как клиент управляет токенами автоматически.
    Функция оставлена для обратной совместимости.
    """
    try:
        if not os.path.exists(config.VERTEX_CREDENTIALS_FILE):
            logging.critical(f"VERTEX_AUTH: Файл учетных данных не найден по пути: {config.VERTEX_CREDENTIALS_FILE}")
            return False

        logging.info("VERTEX_AUTH: Проверка Vertex AI SDK (новый API google.genai управляет токенами автоматически)...")
        logging.info("VERTEX_AUTH: УСПЕХ. Vertex AI SDK работает корректно.")
        return True
    except FileNotFoundError:
        logging.critical(f"VERTEX_AUTH: КРИТИЧЕСКАЯ ОШИБКА: Файл учетных данных не найден по пути: {config.VERTEX_CREDENTIALS_FILE}")
        return False
    except Exception as e:
        logging.error("VERTEX_AUTH: КРИТИЧЕСКАЯ ОШИБКА при проверке Vertex AI.", exc_info=e)
        return False


async def retry_with_exponential_backoff(func, max_retries=8, initial_delay=3, max_delay=120, chat_id=None, context=None):
    """
    Выполняет функцию с повторными попытками при ошибке ResourceExhausted.

    Args:
        func: Async функция для выполнения
        max_retries: Максимальное количество попыток (default: 8)
        initial_delay: Начальная задержка в секундах (default: 3)
        max_delay: Максимальная задержка в секундах (default: 120)
        chat_id: ID чата для отправки уведомлений
        context: Telegram context для отправки сообщений

    Returns:
        Результат выполнения функции

    Raises:
        Exception: Если все попытки исчерпаны

    Примечание:
        Задержки растут экспоненциально: 3, 6, 12, 24, 48, 96, 120, 120 секунд
    """
    from google.api_core import exceptions as google_exceptions

    delay = initial_delay
    status_message = None

    for attempt in range(max_retries):
        try:
            return await func()
        except google_exceptions.ResourceExhausted as e:
            if attempt == max_retries - 1:
                # Последняя попытка - поднимаем ошибку
                raise

            # Вычисляем задержку с экспоненциальным ростом
            delay = min(initial_delay * (2 ** attempt), max_delay)

            logging.warning(
                f"[RETRY] Попытка {attempt + 1}/{max_retries} не удалась (ResourceExhausted). "
                f"Повтор через {delay} сек. Chat: {chat_id}"
            )

            # Уведомляем пользователя
            if context and chat_id:
                try:
                    message_text = (
                        f"⏳ <b>Высокая нагрузка на AI-сервис</b>\n\n"
                        f"Попытка {attempt + 1}/{max_retries}\n"
                        f"Повторная отправка через {int(delay)} сек.\n\n"
                        f"⏱ Время ответа увеличено, пожалуйста подождите..."
                    )

                    if status_message is None:
                        # Создаем новое сообщение при первой ошибке
                        status_message = await context.bot.send_message(
                            chat_id=chat_id,
                            text=message_text,
                            parse_mode='HTML'
                        )
                    else:
                        # Обновляем существующее сообщение
                        await status_message.edit_text(message_text, parse_mode='HTML')
                except Exception as msg_error:
                    logging.warning(f"[RETRY] Не удалось отправить/обновить статус: {msg_error}")

            # Ждем перед следующей попыткой
            await asyncio.sleep(delay)

    # Удаляем сообщение о статусе если оно было создано
    if status_message:
        try:
            await status_message.delete()
        except Exception:
            pass

    raise Exception("Все попытки исчерпаны")

# END OF FILE utils.py