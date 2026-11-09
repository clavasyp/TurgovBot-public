# START OF FILE google_docs_exporter.py

"""
Модуль для работы с Google Docs API.

Содержит функции для:
- Получения содержимого шаблона документа
- Копирования шаблона с переименованием
- Заполнения документа контентом
- Установки публичного доступа к документу
"""

import io
import os
import logging
from typing import Tuple, Dict

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from config import BASE_DIR, CCW_TEMPLATE_DOC_ID, CCW_OUTPUT_FOLDER_ID, _resolve_credentials_path

# Области доступа для Google APIs
SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive'
]

# Пути к файлам OAuth
OAUTH_CREDENTIALS_FILE = _resolve_credentials_path('client_secret_253445722850-rcsq16t3mbqhb810dele588kucs0eagq.apps.googleusercontent.com.json')
OAUTH_TOKEN_FILE = _resolve_credentials_path('token_gdocs.json')

# Глобальные клиенты для повторного использования
_docs_service = None
_drive_service = None
_credentials = None


def _get_credentials():
    """
    Получает OAuth credentials от имени пользователя.

    При первом запуске:
    - Если есть GUI — откроется браузер
    - Если нет GUI (Linux сервер) — выведет URL в консоль для ручной авторизации

    После авторизации токен сохраняется в token_gdocs.json
    """
    global _credentials

    if _credentials and _credentials.valid:
        return _credentials

    # Пробуем загрузить сохранённый токен
    if os.path.exists(OAUTH_TOKEN_FILE):
        _credentials = Credentials.from_authorized_user_file(OAUTH_TOKEN_FILE, SCOPES)

    # Если токен недействителен или отсутствует
    if not _credentials or not _credentials.valid:
        if _credentials and _credentials.expired and _credentials.refresh_token:
            # Обновляем истёкший токен
            _credentials.refresh(Request())
            logging.info("[GoogleDocs] OAuth токен обновлён")
        else:
            # Запускаем авторизацию
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CREDENTIALS_FILE, SCOPES)

            # Пробуем открыть браузер, если не получится — используем консоль
            try:
                _credentials = flow.run_local_server(port=0)
            except Exception as e:
                logging.warning(f"[GoogleDocs] Не удалось открыть браузер: {e}")
                logging.info("[GoogleDocs] Используем консольную авторизацию...")

                # Консольная авторизация для серверов без GUI
                auth_url, _ = flow.authorization_url(prompt='consent')
                print("\n" + "="*60)
                print("[GoogleDocs] ТРЕБУЕТСЯ АВТОРИЗАЦИЯ")
                print("="*60)
                print(f"\n1. Откройте эту ссылку в браузере:\n\n{auth_url}\n")
                print("2. Войдите в Google аккаунт и разрешите доступ")
                print("3. Скопируйте код авторизации и вставьте ниже\n")

                code = input("Введите код авторизации: ").strip()
                flow.fetch_token(code=code)
                _credentials = flow.credentials

        # Сохраняем токен для следующего раза
        with open(OAUTH_TOKEN_FILE, 'w') as token:
            token.write(_credentials.to_json())
        logging.info("[GoogleDocs] OAuth токен сохранён")

    return _credentials


def _get_docs_service():
    """Возвращает клиент Google Docs API (с кэшированием)."""
    global _docs_service
    if _docs_service is None:
        credentials = _get_credentials()
        _docs_service = build('docs', 'v1', credentials=credentials)
    return _docs_service


def _get_drive_service():
    """Возвращает клиент Google Drive API (с кэшированием)."""
    global _drive_service
    if _drive_service is None:
        credentials = _get_credentials()
        _drive_service = build('drive', 'v3', credentials=credentials)
    return _drive_service


def diagnose_drive_quota():
    """
    Диагностика Google Drive: проверяет квоту, владельцев файлов и права доступа.
    Выводит подробную информацию в лог.
    """
    drive_service = _get_drive_service()

    logging.info("=" * 60)
    logging.info("[GoogleDocs] ДИАГНОСТИКА GOOGLE DRIVE")
    logging.info("=" * 60)

    # 1. Информация о сервисном аккаунте
    try:
        about = drive_service.about().get(fields='user,storageQuota').execute()
        user = about.get('user', {})
        quota = about.get('storageQuota', {})

        logging.info(f"[Диагностика] Сервисный аккаунт: {user.get('emailAddress', 'N/A')}")
        logging.info(f"[Диагностика] Имя: {user.get('displayName', 'N/A')}")

        # Квота
        limit = int(quota.get('limit', 0))
        usage = int(quota.get('usage', 0))
        usage_in_drive = int(quota.get('usageInDrive', 0))
        usage_in_trash = int(quota.get('usageInDriveTrash', 0))

        logging.info(f"[Диагностика] Квота лимит: {limit / (1024**3):.2f} GB" if limit else "[Диагностика] Квота лимит: НЕТ ЛИМИТА (сервисный аккаунт)")
        logging.info(f"[Диагностика] Использовано всего: {usage / (1024**3):.4f} GB")
        logging.info(f"[Диагностика] В Drive: {usage_in_drive / (1024**3):.4f} GB")
        logging.info(f"[Диагностика] В корзине: {usage_in_trash / (1024**3):.4f} GB")

        if limit == 0:
            logging.warning("[Диагностика] ⚠️ ПРОБЛЕМА: Сервисный аккаунт имеет квоту 0 GB!")
            logging.warning("[Диагностика] Решение: Файлы должны создаваться в папке, владельцем которой является РЕАЛЬНЫЙ пользователь")
    except Exception as e:
        logging.error(f"[Диагностика] Ошибка получения информации об аккаунте: {e}")

    # 2. Проверка шаблона
    try:
        template = drive_service.files().get(
            fileId=CCW_TEMPLATE_DOC_ID,
            fields='id,name,owners,shared,capabilities',
            supportsAllDrives=True
        ).execute()

        logging.info(f"[Диагностика] Шаблон: {template.get('name')}")
        owners = template.get('owners', [])
        for owner in owners:
            logging.info(f"[Диагностика] Владелец шаблона: {owner.get('emailAddress')} ({owner.get('displayName', 'N/A')})")
        logging.info(f"[Диагностика] Шаблон расшарен: {template.get('shared', False)}")

        caps = template.get('capabilities', {})
        logging.info(f"[Диагностика] Можно копировать: {caps.get('canCopy', False)}")
    except Exception as e:
        logging.error(f"[Диагностика] Ошибка доступа к шаблону {CCW_TEMPLATE_DOC_ID}: {e}")

    # 3. Проверка целевой папки
    if CCW_OUTPUT_FOLDER_ID:
        try:
            folder = drive_service.files().get(
                fileId=CCW_OUTPUT_FOLDER_ID,
                fields='id,name,owners,shared,capabilities,driveId',
                supportsAllDrives=True
            ).execute()

            logging.info(f"[Диагностика] Целевая папка: {folder.get('name')}")
            owners = folder.get('owners', [])
            for owner in owners:
                logging.info(f"[Диагностика] Владелец папки: {owner.get('emailAddress')} ({owner.get('displayName', 'N/A')})")

            if folder.get('driveId'):
                logging.info(f"[Диагностика] Папка на Shared Drive: {folder.get('driveId')}")

            caps = folder.get('capabilities', {})
            logging.info(f"[Диагностика] Можно добавлять файлы: {caps.get('canAddChildren', False)}")
            logging.info(f"[Диагностика] Можно редактировать: {caps.get('canEdit', False)}")
        except Exception as e:
            logging.error(f"[Диагностика] Ошибка доступа к папке {CCW_OUTPUT_FOLDER_ID}: {e}")

    # 4. Список файлов сервисного аккаунта
    try:
        files = drive_service.files().list(
            q="'me' in owners",
            fields='files(id,name,size,mimeType)',
            pageSize=20
        ).execute()

        file_list = files.get('files', [])
        logging.info(f"[Диагностика] Файлов принадлежащих сервисному аккаунту: {len(file_list)}")
        total_size = 0
        for f in file_list[:10]:
            size = int(f.get('size', 0))
            total_size += size
            logging.info(f"[Диагностика]   - {f.get('name')} ({size} bytes)")
        if len(file_list) > 10:
            logging.info(f"[Диагностика]   ... и ещё {len(file_list) - 10} файлов")
        logging.info(f"[Диагностика] Общий размер файлов SA: {total_size / 1024:.2f} KB")
    except Exception as e:
        logging.error(f"[Диагностика] Ошибка получения списка файлов: {e}")

    logging.info("=" * 60)
    logging.info("[GoogleDocs] КОНЕЦ ДИАГНОСТИКИ")
    logging.info("=" * 60)


def get_template_content(doc_id: str = None) -> str:
    """
    Получает текстовое содержимое шаблона Google Doc для анализа AI.

    Args:
        doc_id: ID документа (по умолчанию CCW_TEMPLATE_DOC_ID)

    Returns:
        Текстовое содержимое документа

    Raises:
        HttpError: При ошибках API
    """
    if doc_id is None:
        doc_id = CCW_TEMPLATE_DOC_ID

    try:
        service = _get_docs_service()
        document = service.documents().get(documentId=doc_id).execute()

        # Извлекаем текст из всех элементов документа
        content_parts = []

        for element in document.get('body', {}).get('content', []):
            if 'paragraph' in element:
                for text_run in element['paragraph'].get('elements', []):
                    if 'textRun' in text_run:
                        content_parts.append(text_run['textRun'].get('content', ''))
            elif 'table' in element:
                # Обрабатываем таблицы
                for row in element['table'].get('tableRows', []):
                    row_cells = []
                    for cell in row.get('tableCells', []):
                        cell_text = ''
                        for cell_content in cell.get('content', []):
                            if 'paragraph' in cell_content:
                                for text_run in cell_content['paragraph'].get('elements', []):
                                    if 'textRun' in text_run:
                                        cell_text += text_run['textRun'].get('content', '')
                        row_cells.append(cell_text.strip())
                    content_parts.append(' | '.join(row_cells))

        full_content = ''.join(content_parts)
        logging.info(f"[GoogleDocs] Получено содержимое шаблона: {len(full_content)} символов")
        return full_content

    except HttpError as e:
        logging.error(f"[GoogleDocs] Ошибка при получении шаблона {doc_id}: {e}")
        raise


def _cleanup_service_account_storage() -> int:
    """
    Очищает хранилище сервисного аккаунта: удаляет файлы из корзины
    и старые документы CCW.

    Returns:
        Количество освобождённых файлов
    """
    try:
        drive_service = _get_drive_service()
        cleaned = 0

        # 1. Очистка корзины
        try:
            drive_service.files().emptyTrash().execute()
            logging.info("[GoogleDocs] Корзина сервисного аккаунта очищена")
            cleaned += 1
        except HttpError as e:
            logging.warning(f"[GoogleDocs] Не удалось очистить корзину: {e}")

        # 2. Удаление старых CCW документов (старше 7 дней), принадлежащих сервисному аккаунту
        try:
            from datetime import datetime, timedelta
            cutoff_date = (datetime.utcnow() - timedelta(days=7)).isoformat() + 'Z'

            # Ищем старые документы с названием "ATTORNEY GENERAL CRIMINAL CASE"
            response = drive_service.files().list(
                q=f"name contains 'ATTORNEY GENERAL CRIMINAL CASE' and createdTime < '{cutoff_date}' and 'me' in owners",
                fields='files(id, name, createdTime)',
                pageSize=50
            ).execute()

            old_files = response.get('files', [])
            for file in old_files:
                try:
                    drive_service.files().delete(fileId=file['id']).execute()
                    logging.info(f"[GoogleDocs] Удалён старый документ: {file['name']} ({file['id']})")
                    cleaned += 1
                except HttpError:
                    pass

        except HttpError as e:
            logging.warning(f"[GoogleDocs] Не удалось удалить старые документы: {e}")

        return cleaned
    except Exception as e:
        logging.error(f"[GoogleDocs] Ошибка при очистке хранилища: {e}")
        return 0


def _copy_via_export_import(case_number: str) -> Tuple[str, str]:
    """
    Fallback метод: экспортирует шаблон как DOCX и загружает как новый Google Doc.

    Использует impersonation — сервисный аккаунт действует от имени владельца папки,
    поэтому файл создаётся с использованием квоты владельца.

    Args:
        case_number: Номер дела

    Returns:
        Кортеж (doc_id, doc_url)
    """
    from google.oauth2 import service_account

    logging.info("[GoogleDocs] Используем fallback с impersonation")

    # Получаем email владельца папки для impersonation
    drive_service = _get_drive_service()

    try:
        folder_info = drive_service.files().get(
            fileId=CCW_OUTPUT_FOLDER_ID,
            fields='owners',
            supportsAllDrives=True
        ).execute()

        owners = folder_info.get('owners', [])
        if not owners:
            raise ValueError("Не удалось определить владельца папки")

        owner_email = owners[0].get('emailAddress')
        logging.info(f"[GoogleDocs] Владелец папки: {owner_email}")

        # Создаём credentials с impersonation (действуем от имени владельца)
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE,
            scopes=SCOPES,
            subject=owner_email  # Impersonation!
        )

        # Создаём новые сервисы с impersonated credentials
        impersonated_drive = build('drive', 'v3', credentials=credentials)

        # Шаг 1: Экспортируем шаблон как DOCX
        docx_content = drive_service.files().export(
            fileId=CCW_TEMPLATE_DOC_ID,
            mimeType='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        ).execute()

        logging.info(f"[GoogleDocs] Шаблон экспортирован: {len(docx_content)} байт")

        # Шаг 2: Загружаем от имени владельца папки
        file_metadata = {
            'name': f'ATTORNEY GENERAL CRIMINAL CASE №{case_number}',
            'mimeType': 'application/vnd.google-apps.document',
            'parents': [CCW_OUTPUT_FOLDER_ID]
        }

        media = MediaIoBaseUpload(
            io.BytesIO(docx_content),
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            resumable=True
        )

        new_file = impersonated_drive.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True
        ).execute()

        new_doc_id = new_file.get('id')
        new_doc_url = f"https://docs.google.com/document/d/{new_doc_id}/edit"

        logging.info(f"[GoogleDocs] Документ создан через impersonation: {new_doc_id}")

        # Делаем публичным
        try:
            impersonated_drive.permissions().create(
                fileId=new_doc_id,
                body={'type': 'anyone', 'role': 'reader'},
                fields='id'
            ).execute()
        except HttpError as e:
            logging.warning(f"[GoogleDocs] Не удалось сделать публичным: {e}")

        return new_doc_id, new_doc_url

    except Exception as e:
        logging.error(f"[GoogleDocs] Impersonation не удался: {e}")
        logging.info("[GoogleDocs] Пробуем создать shortcut...")

        # Последний fallback — создаём shortcut на шаблон
        # (это не использует квоту вообще)
        try:
            shortcut_metadata = {
                'name': f'ATTORNEY GENERAL CRIMINAL CASE №{case_number}',
                'mimeType': 'application/vnd.google-apps.shortcut',
                'shortcutDetails': {
                    'targetId': CCW_TEMPLATE_DOC_ID
                }
            }

            if CCW_OUTPUT_FOLDER_ID:
                shortcut_metadata['parents'] = [CCW_OUTPUT_FOLDER_ID]

            shortcut = drive_service.files().create(
                body=shortcut_metadata,
                fields='id,shortcutDetails',
                supportsAllDrives=True
            ).execute()

            # Возвращаем ссылку на оригинальный шаблон
            new_doc_url = f"https://docs.google.com/document/d/{CCW_TEMPLATE_DOC_ID}/edit"
            logging.info(f"[GoogleDocs] Создан shortcut: {shortcut.get('id')}")

            return CCW_TEMPLATE_DOC_ID, new_doc_url

        except HttpError as shortcut_error:
            logging.error(f"[GoogleDocs] Shortcut тоже не удался: {shortcut_error}")
            raise


def copy_template_document(case_number: str) -> Tuple[str, str]:
    """
    Создаёт новый документ на основе шаблона путём копирования.

    С OAuth credentials файл создаётся от имени реального пользователя,
    поэтому использует его квоту (не сервисного аккаунта).

    Args:
        case_number: Номер дела (например, DJR-015)

    Returns:
        Кортеж (doc_id, doc_url)

    Raises:
        HttpError: При ошибках API
    """
    drive_service = _get_drive_service()

    # Копируем шаблон сразу в целевую папку
    file_metadata = {
        'name': f'ATTORNEY GENERAL CRIMINAL CASE №{case_number}'
    }

    if CCW_OUTPUT_FOLDER_ID:
        file_metadata['parents'] = [CCW_OUTPUT_FOLDER_ID]

    logging.info(f"[GoogleDocs] Копирование шаблона {CCW_TEMPLATE_DOC_ID}...")

    try:
        new_file = drive_service.files().copy(
            fileId=CCW_TEMPLATE_DOC_ID,
            body=file_metadata,
            fields='id'
        ).execute()

        new_doc_id = new_file.get('id')
        logging.info(f"[GoogleDocs] Создана копия шаблона: {new_doc_id}")

        new_doc_url = f"https://docs.google.com/document/d/{new_doc_id}/edit"

        # Делаем документ доступным по ссылке
        try:
            drive_service.permissions().create(
                fileId=new_doc_id,
                body={
                    'type': 'anyone',
                    'role': 'reader'
                },
                fields='id'
            ).execute()
            logging.info(f"[GoogleDocs] Документ {new_doc_id} доступен по ссылке")
        except HttpError as perm_error:
            logging.warning(f"[GoogleDocs] Не удалось сделать документ публичным: {perm_error}")

        return new_doc_id, new_doc_url

    except HttpError as e:

        logging.error(f"[GoogleDocs] Ошибка при копировании шаблона: {e}")
        raise


def fill_document_content(doc_id: str, replacements: Dict[str, str]) -> bool:
    """
    Заменяет плейсхолдеры в документе на реальные данные.

    Args:
        doc_id: ID документа
        replacements: Словарь {плейсхолдер: значение}

    Returns:
        True при успешном заполнении

    Raises:
        HttpError: При ошибках API
    """
    try:
        service = _get_docs_service()

        # Формируем запросы на замену
        requests = []
        for placeholder, value in replacements.items():
            if value:  # Заменяем только если есть значение
                requests.append({
                    'replaceAllText': {
                        'containsText': {
                            'text': placeholder,
                            'matchCase': False  # Нечувствительный к регистру
                        },
                        'replaceText': value
                    }
                })

        if requests:
            service.documents().batchUpdate(
                documentId=doc_id,
                body={'requests': requests}
            ).execute()
            logging.info(f"[GoogleDocs] Выполнено {len(requests)} замен в документе {doc_id}")

        return True

    except HttpError as e:
        logging.error(f"[GoogleDocs] Ошибка при заполнении документа {doc_id}: {e}")
        raise


def fill_criminal_case_document(doc_id: str, case_number: str, collected_data: Dict, final_parts: Dict[str, str], template_structure: Dict = None) -> bool:
    """
    ВРЕМЕННАЯ ВЕРСИЯ: Просто вставляет весь сгенерированный контент в конец документа.

    Args:
        doc_id: ID документа Google Doc
        case_number: Номер дела
        collected_data: Собранные данные о деле
        final_parts: Словарь {part_type: content} со сгенерированными частями
        template_structure: Не используется в этой версии

    Returns:
        True при успешном заполнении
    """
    try:
        service = _get_docs_service()

        # Порядок частей для вставки
        parts_order = [
            ('inventory', 'ОПИСЬ ДОКУМЕНТОВ'),
            ('accused_card', 'КАРТОЧКА ОБВИНЯЕМОГО'),
            ('case_materials', 'МАТЕРИАЛЫ ДЕЛА'),
            ('indictment', 'ОБВИНИТЕЛЬНОЕ ЗАКЛЮЧЕНИЕ'),
            ('summons_list', 'СПИСОК ВЫЗЫВАЕМЫХ В СУД'),
            ('prosecution_demands', 'ТРЕБОВАНИЯ ПРОКУРАТУРЫ'),
        ]

        # Собираем весь контент для вставки
        content_to_insert = "\n\n"
        content_to_insert += "=" * 60 + "\n"
        content_to_insert += f"УГОЛОВНОЕ ДЕЛО №{case_number}\n"
        content_to_insert += "=" * 60 + "\n\n"

        # Добавляем информацию из collected_data
        if collected_data:
            accused_name = collected_data.get('accused_name', '')
            charges = collected_data.get('charges', '')
            if accused_name:
                content_to_insert += f"Обвиняемый: {accused_name}\n"
            if charges:
                content_to_insert += f"Статьи обвинения: {charges}\n"
            content_to_insert += "\n"

        # Добавляем все сгенерированные части
        parts_added = 0
        for part_type, part_title in parts_order:
            if part_type in final_parts and final_parts[part_type]:
                content_to_insert += "-" * 60 + "\n"
                content_to_insert += f"{part_title}\n"
                content_to_insert += "-" * 60 + "\n\n"
                content_to_insert += final_parts[part_type]
                content_to_insert += "\n\n"
                parts_added += 1

        if parts_added == 0:
            content_to_insert += "[Части дела не сгенерированы]\n"

        # Получаем позицию конца документа
        doc = service.documents().get(documentId=doc_id).execute()
        body_content = doc.get('body', {}).get('content', [])

        if body_content:
            end_index = body_content[-1].get('endIndex', 1) - 1
        else:
            end_index = 1

        # Вставляем контент в конец документа
        requests = [{
            'insertText': {
                'location': {'index': end_index},
                'text': content_to_insert
            }
        }]

        service.documents().batchUpdate(
            documentId=doc_id,
            body={'requests': requests}
        ).execute()

        logging.info(f"[GoogleDocs] Документ {doc_id} заполнен: {parts_added} частей вставлено в конец")
        return True

    except HttpError as e:
        logging.error(f"[GoogleDocs] Ошибка при заполнении документа {doc_id}: {e}")
        raise


def append_content_to_document(doc_id: str, content: str, index: int = None) -> bool:
    """
    Добавляет текстовый контент в документ.

    Args:
        doc_id: ID документа
        content: Текст для добавления
        index: Позиция для вставки (None = в конец)

    Returns:
        True при успешном добавлении
    """
    try:
        service = _get_docs_service()

        requests = []

        if index is None:
            # Получаем текущую длину документа
            doc = service.documents().get(documentId=doc_id).execute()
            body_content = doc.get('body', {}).get('content', [])
            if body_content:
                # Индекс конца документа
                index = body_content[-1].get('endIndex', 1) - 1
            else:
                index = 1

        requests.append({
            'insertText': {
                'location': {'index': index},
                'text': content
            }
        })

        service.documents().batchUpdate(
            documentId=doc_id,
            body={'requests': requests}
        ).execute()

        logging.info(f"[GoogleDocs] Добавлен контент в документ {doc_id}")
        return True

    except HttpError as e:
        logging.error(f"[GoogleDocs] Ошибка при добавлении контента в {doc_id}: {e}")
        raise


def make_document_public(doc_id: str) -> bool:
    """
    Устанавливает доступ "anyone with link can view" для документа.

    Args:
        doc_id: ID документа

    Returns:
        True при успешной установке доступа

    Raises:
        HttpError: При ошибках API
    """
    try:
        drive_service = _get_drive_service()

        # Создаём разрешение для всех с ссылкой
        permission = {
            'type': 'anyone',
            'role': 'reader'
        }

        drive_service.permissions().create(
            fileId=doc_id,
            body=permission,
            fields='id'
        ).execute()

        logging.info(f"[GoogleDocs] Установлен публичный доступ для документа {doc_id}")
        return True

    except HttpError as e:
        logging.error(f"[GoogleDocs] Ошибка при установке доступа для {doc_id}: {e}")
        raise


def get_document_url(doc_id: str) -> str:
    """Формирует URL документа."""
    return f"https://docs.google.com/document/d/{doc_id}/edit"


async def create_criminal_case_document(case_number: str) -> Tuple[str, str]:
    """
    Создаёт новый документ уголовного дела на основе шаблона.

    Высокоуровневая функция, которая:
    1. Копирует шаблон
    2. Устанавливает публичный доступ

    Args:
        case_number: Номер дела

    Returns:
        Кортеж (doc_id, doc_url)
    """
    # Копируем шаблон
    doc_id, doc_url = copy_template_document(case_number)

    # Устанавливаем публичный доступ
    make_document_public(doc_id)

    return doc_id, doc_url


# END OF FILE google_docs_exporter.py
