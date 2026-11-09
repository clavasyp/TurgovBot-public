# START OF FILE database/assistant_logger.py

"""
Модуль для инкапсуляции всей логики взаимодействия с базой данных,
касающейся функции "Ролевой Помощник".

Предоставляет асинхронные функции для создания "Дел", добавления сообщений
и извлечения истории диалогов.
"""

import logging
from .manager import db_manager


async def create_case(
    user_id: int,
    server_id: str,
    rp_role: str,
    case_stage: str
) -> int | None:
    """
    Создает новую запись о "Деле" в таблице assistant_cases.

    Returns:
        ID созданного дела (case_id) или None в случае ошибки.
    """
    query = """
        INSERT INTO assistant_cases (user_id, server_id, rp_role, case_stage)
        VALUES ($1, $2, $3, $4)
        RETURNING id;
    """
    try:
        case_id = await db_manager.fetchval(query, user_id, server_id, rp_role, case_stage)
        logging.info(f"В БД создано новое 'Дело' #{case_id} для пользователя {user_id}.")
        return case_id
    except Exception as e:
        logging.error(f"Не удалось создать 'Дело' для пользователя {user_id} в БД.", exc_info=e)
        return None


async def add_message_to_case(
    case_id: int,
    sender_role: str,
    content: str
):
    """
    Добавляет новое сообщение в историю "Дела" и обновляет время последнего сообщения.
    """
    # Транзакция, чтобы обеспечить атомарность операций
    async with db_manager._pool.acquire() as connection:
        async with connection.transaction():
            try:
                # 1. Добавляем сообщение
                insert_query = """
                    INSERT INTO assistant_messages (case_id, sender_role, content)
                    VALUES ($1, $2, $3);
                """
                await connection.execute(insert_query, case_id, sender_role, content)

                # 2. Обновляем время последнего действия в "Деле"
                update_query = """
                    UPDATE assistant_cases
                    SET last_updated_at = NOW()
                    WHERE id = $1;
                """
                await connection.execute(update_query, case_id)

                logging.info(f"В 'Дело' #{case_id} добавлено сообщение от '{sender_role}'.")

            except Exception as e:
                logging.error(f"Не удалось добавить сообщение в 'Дело' #{case_id}.", exc_info=e)
                # Транзакция автоматически откатится в случае ошибки


async def get_case_history(case_id: int) -> list[dict] | None:
    """
    Извлекает полную историю сообщений для указанного "Дела".

    Returns:
        Список словарей [{'id': ..., 'sender_role': '...', 'content': '...'}, ...]
        или None в случае ошибки.

    `id` нужен карте дела (services/case_digest): по нему видно, какие сообщения уже
    вошли в сводку, а какие подавать модели сырыми.
    """
    query = """
        SELECT id, sender_role, content
        FROM assistant_messages
        WHERE case_id = $1
        ORDER BY timestamp ASC, id ASC;
    """
    try:
        records = await db_manager.fetch(query, case_id)
        # Преобразуем asyncpg.Record в стандартные словари для удобства
        return [dict(record) for record in records]
    except Exception as e:
        logging.error(f"Не удалось получить историю для 'Дела' #{case_id}.", exc_info=e)
        return None


async def get_case_digest(case_id: int) -> dict | None:
    """Карта дела (сжатая сводка старой части переписки) или None, если её ещё нет.

    Ошибка чтения тоже даёт None: карта — кэш, без неё флоу обязан работать по-старому.
    """
    query = """
        SELECT digest, covered_message_id, covered_count, source_chars, model, updated_at
        FROM assistant_case_digests
        WHERE case_id = $1;
    """
    try:
        record = await db_manager.fetchrow(query, case_id)
        return dict(record) if record else None
    except Exception as e:
        logging.warning(f"Не удалось прочитать карту 'Дела' #{case_id}: {e}")
        return None


async def save_case_digest(
    case_id: int,
    digest: str,
    covered_message_id: int,
    covered_count: int = 0,
    source_chars: int = 0,
    model: str | None = None,
) -> bool:
    """Сохраняет карту дела.

    Условие в DO UPDATE защищает от гонки двух параллельных ходов по одному делу:
    выигрывает та карта, которая покрывает больше сообщений, а не та, что записалась
    последней (иначе более полная сводка могла бы затереться более старой).
    """
    query = """
        INSERT INTO assistant_case_digests
            (case_id, digest, covered_message_id, covered_count, source_chars, model, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (case_id) DO UPDATE SET
            digest = EXCLUDED.digest,
            covered_message_id = EXCLUDED.covered_message_id,
            covered_count = EXCLUDED.covered_count,
            source_chars = EXCLUDED.source_chars,
            model = EXCLUDED.model,
            updated_at = NOW()
        WHERE EXCLUDED.covered_message_id >= assistant_case_digests.covered_message_id;
    """
    try:
        await db_manager.execute(query, case_id, digest, covered_message_id,
                                 covered_count, source_chars, model)
        logging.info(
            f"Карта 'Дела' #{case_id} обновлена: покрыто {covered_count} сообщений "
            f"(до id={covered_message_id}, {source_chars} симв.)"
        )
        return True
    except Exception as e:
        logging.warning(f"Не удалось сохранить карту 'Дела' #{case_id}: {e}")
        return False


async def get_active_cases_for_user(user_id: int) -> list[dict] | None:
    """
    Извлекает список всех активных "Дел" для указанного пользователя.

    Returns:
        Список словарей с информацией о делах или None в случае ошибки.
    """
    query = """
        SELECT id, server_id, rp_role, case_stage, created_at
        FROM assistant_cases
        WHERE user_id = $1 AND status = 'active'
        ORDER BY created_at DESC;
    """
    try:
        records = await db_manager.fetch(query, user_id)
        return [dict(record) for record in records]
    except Exception as e:
        logging.error(f"Не удалось получить список активных дел для пользователя {user_id}.", exc_info=e)
        return None

async def close_case(case_id: int):
    """
    Меняет статус "Дела" на 'closed'.
    """
    query = "UPDATE assistant_cases SET status = 'closed' WHERE id = $1;"
    try:
        await db_manager.execute(query, case_id)
        logging.info(f"'Дело' #{case_id} было успешно закрыто.")
    except Exception as e:
        logging.error(f"Не удалось закрыть 'Дело' #{case_id}.", exc_info=e)