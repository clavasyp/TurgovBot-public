"""
Модуль для асинхронного взаимодействия с Google Sheets.
"""

import asyncio
import gspread
from datetime import datetime
import logging  # <-- Добавлен импорт

# Локальные импорты конфигурации и утилит.
from config import SPREADSHEET_ID, GOOGLE_CREDENTIALS_FILE, ADMIN_LOG_SHEET_NAME
# debug_print больше не нужен, его заменяет logging


async def get_worksheet(spreadsheet_key: str, sheet_name: str) -> gspread.Worksheet | None:
    """
    Асинхронно подключается к Google Sheets и возвращает объект рабочего листа.
    """
    try:
        # Аутентификация и открытие таблицы.
        creds = await asyncio.to_thread(gspread.service_account, filename=GOOGLE_CREDENTIALS_FILE)
        spreadsheet = await asyncio.to_thread(creds.open_by_key, spreadsheet_key)

        # Попытка получить лист; создание при отсутствии.
        try:
            worksheet = await asyncio.to_thread(spreadsheet.worksheet, sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = await asyncio.to_thread(spreadsheet.add_worksheet, title=sheet_name, rows="1000", cols="5")
            # Инициализация заголовков для нового листа.
            if sheet_name == ADMIN_LOG_SHEET_NAME:
                headers = ["Timestamp", "Admin_ID", "Action", "Details"]
                await asyncio.to_thread(worksheet.append_row, headers)
            else:  # Предполагается, что это лист 'Logs'.
                headers = [["Timestamp", "User_ID", "Type", "Question", "Answer"]]
                await asyncio.to_thread(worksheet.update, 'A1:E1', headers)
            logging.info(f"Лист '{sheet_name}' не был найден и был создан заново.")

        return worksheet
    except Exception as e:
        logging.critical(f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось подключиться к Google Sheet '{sheet_name}'.", exc_info=e)
        return None


async def log_to_google_sheet(chat_id: int, question: str, answer: str, q_type: str):
    """
    Логирует запрос пользователя и ответ AI в лист 'Logs'.
    """
    worksheet = await get_worksheet(SPREADSHEET_ID, 'Logs')
    if worksheet:
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row = [timestamp, str(chat_id), q_type, question, answer]
            await asyncio.to_thread(worksheet.append_row, row)
        except Exception as e:
            logging.error("КРИТИЧЕСКАЯ ОШИБКА: Не удалось записать данные в Google Sheet (Logs).", exc_info=e)


async def log_admin_action_to_sheet(admin_id: int, action: str, details: str = "N/A"):
    """
    Логирует действие администратора в лист 'Admin_Logs'.
    """
    worksheet = await get_worksheet(SPREADSHEET_ID, ADMIN_LOG_SHEET_NAME)
    if worksheet:
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row = [timestamp, str(admin_id), action, details]
            await asyncio.to_thread(worksheet.append_row, row)
        except Exception as e:
            logging.error("КРИТИЧЕСКАЯ ОШИБКА: Не удалось записать действие администратора в Google Sheet.", exc_info=e)