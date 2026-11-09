"""
Модуль для проверки статуса подписки пользователя на Telegram-канал.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError, BadRequest

from config import TELEGRAM_CHANNEL_ID, TELEGRAM_CHANNEL_URL


def _get_channel_identifier():
    """
    Возвращает идентификатор канала для API-запросов.
    Приоритет: числовой ID, затем username из URL.
    """
    if TELEGRAM_CHANNEL_ID:
        return TELEGRAM_CHANNEL_ID

    # Если числовой ID не указан, пытаемся извлечь username из URL
    if TELEGRAM_CHANNEL_URL:
        # Из "https://t.me/turgovovka" извлекаем "@turgovovka"
        username = TELEGRAM_CHANNEL_URL.rstrip('/').split('/')[-1]
        if username and not username.startswith('@'):
            username = f"@{username}"
        return username

    return None


async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Проверяет, является ли пользователь подписчиком указанного в конфиге канала.

    Args:
        user_id: Telegram ID пользователя для проверки.
        context: Контекст бота, через который выполняется API-запрос.

    Returns:
        True, если пользователь подписан.
        False, если пользователь не подписан или произошла ошибка.
    """
    channel_id = _get_channel_identifier()

    if not channel_id:
        logging.warning("Проверка подписки пропущена: TELEGRAM_CHANNEL_ID не задан в config.py.")
        return False

    try:
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)

        # Статусы, которые подтверждают, что пользователь находится в канале
        allowed_statuses = ['member', 'administrator', 'creator']

        if member.status in allowed_statuses:
            logging.info(f"Проверка подписки для user_id {user_id}: УСПЕХ (статус: {member.status}).")
            return True
        else:
            # Сюда попадают статусы 'left', 'kicked' или любые другие
            logging.info(f"Проверка подписки для user_id {user_id}: НЕУДАЧА (статус: {member.status}).")
            return False

    except BadRequest as e:
        error_message = str(e).lower()
        if "chat not found" in error_message:
            # Канал не найден - бот не добавлен в канал или ID неверный
            logging.warning(
                f"Канал {channel_id} не найден. Убедитесь, что бот добавлен в канал как администратор."
            )
        elif "user not found" in error_message:
            # Пользователь никогда не взаимодействовал с каналом
            logging.debug(f"Пользователь {user_id} не найден в канале {channel_id}.")
        else:
            logging.warning(f"BadRequest при проверке подписки user_id {user_id}: {e}")
        return False

    except TelegramError as e:
        # Другие ошибки Telegram API
        logging.warning(
            f"Ошибка API при проверке подписки для user_id {user_id} в канале {channel_id}: {e}"
        )
        return False

    except Exception as e:
        # Ловим другие неожиданные ошибки
        logging.error(
            f"Непредвиденная ошибка в check_subscription для user_id {user_id}.",
            exc_info=e
        )
        return False