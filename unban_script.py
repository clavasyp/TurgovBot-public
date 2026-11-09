# START OF FILE unban_script.py

"""
Скрипт для массовой "амнистии" пользователей, которые были некорректно
забанены старой версией функции рассылки.

КАК ЭТО РАБОТАЕТ:
1. Скрипт получает из базы данных (БД) PostgreSQL список всех user_id,
   у которых стоит флаг is_banned = TRUE.
2. Затем он подключается к Google Sheets и из листа 'Admin_Logs' собирает
   список всех user_id, которые были забанены администратором вручную
   (действие 'BAN_USER').
3. Скрипт находит разницу: определяет пользователей, которые забанены в БД,
   но на которых НЕТ записи о бане от администратора в логах.
4. По умолчанию скрипт работает в режиме "сухого запуска" (dry run) - он
   только выведет в консоль список ID, которые подлежат разбану.
5. Для реального выполнения операции запустите скрипт с флагом --execute.

ТРЕБОВАНИЯ:
- Убедитесь, что в файле .env указаны правильные учетные данные для БД.
- Убедитесь, что файл credentials.json для доступа к Google API находится на месте.

ИСПОЛЬЗОВАНИЕ:
- Сухой запуск (покажет, кого нужно разбанить):
  python unban_script.py

- Реальный запуск (выполнит разбан):
  python unban_script.py --execute
"""

import sys
import os
import asyncio
import re
import argparse
import logging

# Добавление корневой директории в путь для корректного импорта модулей.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Локальные импорты.
from database.manager import db_manager
from google_sheets_handler import get_worksheet
from config import SPREADSHEET_ID, ADMIN_LOG_SHEET_NAME

# Настройка простого логирования для вывода в консоль
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


async def get_legitimately_banned_ids() -> set[int]:
    """
    Получает из Google Sheets ID пользователей, забаненных администраторами.
    """
    logging.info("Подключаюсь к Google Sheets для получения списка легитимных банов...")
    worksheet = await get_worksheet(SPREADSHEET_ID, ADMIN_LOG_SHEET_NAME)
    if not worksheet:
        logging.error("Не удалось получить доступ к листу 'Admin_Logs'. Невозможно продолжить.")
        return set()

    try:
        # Получаем все записи с листа, кроме заголовка
        records = await asyncio.to_thread(worksheet.get_all_records)
    except Exception as e:
        logging.error(f"Ошибка при чтении данных с листа 'Admin_Logs': {e}")
        return set()

    legit_banned_ids = set()
    for record in records:
        action = record.get('Action')
        details = record.get('Details', '')

        # Ищем записи о бане и извлекаем ID цели
        if action == 'BAN_USER' and 'Target ID:' in details:
            # Используем регулярное выражение для надежного извлечения числа
            match = re.search(r'Target ID:\s*(\d+)', details)
            if match:
                try:
                    user_id = int(match.group(1))
                    legit_banned_ids.add(user_id)
                except (ValueError, TypeError):
                    logging.warning(f"Не удалось извлечь ID из записи: {record}")

    logging.info(f"Найдено {len(legit_banned_ids)} ID, забаненных администраторами в логах.")
    return legit_banned_ids


async def get_db_banned_ids() -> set[int]:
    """
    Получает из базы данных PostgreSQL ID всех пользователей с флагом is_banned = TRUE.
    """
    logging.info("Получаю из БД список всех забаненных пользователей...")
    records = await db_manager.fetch("SELECT user_id FROM users WHERE is_banned = TRUE;")
    if not records:
        logging.info("В базе данных нет забаненных пользователей.")
        return set()

    db_banned_ids = {record['user_id'] for record in records}
    logging.info(f"В БД найдено {len(db_banned_ids)} забаненных пользователей.")
    return db_banned_ids


async def main():
    """
    Основная функция-оркестратор.
    """
    parser = argparse.ArgumentParser(description="Скрипт для разбана пользователей, заблокированных по ошибке.")
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Включите этот флаг для реального выполнения разбана. Без него будет выполнен "сухой запуск".'
    )
    args = parser.parse_args()

    if not args.execute:
        print("\n" + "=" * 50)
        print("ВНИМАНИЕ: СКРИПТ ЗАПУЩЕН В РЕЖИМЕ СУХОГО ЗАПУСКА (DRY RUN).")
        print("Никакие изменения в базу данных вноситься НЕ БУДУТ.")
        print("Для реального разбана перезапустите скрипт с флагом --execute")
        print("=" * 50 + "\n")

    await db_manager.connect()

    # Получаем списки
    legit_banned_ids = await get_legitimately_banned_ids()
    db_banned_ids = await get_db_banned_ids()

    # Находим тех, кого нужно разбанить
    ids_to_unban = db_banned_ids - legit_banned_ids

    if not ids_to_unban:
        logging.info("Отлично! Не найдено пользователей для разбана.")
        await db_manager.close()
        return

    print("\n--- Список пользователей для разбана ---")
    for user_id in ids_to_unban:
        print(f"  - {user_id}")
    print(f"----------------------------------------")
    logging.info(f"Всего к разбану: {len(ids_to_unban)} пользователей.")

    if args.execute:
        logging.info("Режим --execute активен. Выполняю разбан...")
        try:
            # Используем ANY для выполнения операции одним запросом
            query = "UPDATE users SET is_banned = FALSE WHERE user_id = ANY($1);"
            result = await db_manager.execute(query, list(ids_to_unban))
            logging.info(f"Операция завершена. Статус от БД: {result}")
            print("\n✅ Все указанные пользователи были успешно разбанены.")
        except Exception as e:
            logging.critical("Произошла ошибка во время обновления базы данных!", exc_info=e)
    else:
        print("\nЭто был сухой запуск. Для применения изменений используйте флаг --execute.")

    await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())

# END OF FILE unban_script.py