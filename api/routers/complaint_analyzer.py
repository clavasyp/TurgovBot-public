# START OF FILE api/routers/complaint_analyzer.py

"""
API эндпоинты для Complaint Analyzer (анализ жалоб для сайта).

Stateless API для интеграции с сайтом:
- /parse — парсинг жалобы по URL темы форума
- /check-form — проверка формы жалобы на соответствие шаблону
- /analyze — анализ сложности, субъекта и уровня рецензента
- /extract-facts — извлечение юридически значимых фактов и требований
- /form-template — управление шаблонами форм (CRUD)
- /issue-types — управление типами проблем формы (CRUD)

Workflow:
1. POST /parse — парсим жалобу один раз, получаем текст
2. POST /check-form, /analyze, /extract-facts — передаём текст во все эндпоинты
"""

import asyncio
import logging
import time
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status, Path

from ..schemas import (
    ComplaintParseRequest, ComplaintParseResponse,
    ComplaintCheckFormRequest, ComplaintCheckFormResponse,
    ComplaintAnalyzeRequest, ComplaintAnalyzeResponse,
    ComplaintExtractFactsRequest, ComplaintExtractFactsResponse,
    ComplaintFullAnalysisRequest, ComplaintFullAnalysisResponse,
    ComplaintSubject,
    FormTemplateSetRequest, FormTemplateResponse,
    IssueType, IssueTypeCreateRequest, IssueTypeUpdateRequest, IssueTypeListResponse,
    DocumentTextBlocks,
    ComplaintDocumentGenerateRequest,
    ComplaintDocumentGenerateResponse,
    AgoGenerateTextRequest,
    AgoGenerateTextResponse,
    CustomResolutionGenerateRequest,
    CustomResolutionGenerateResponse,
    ErrorResponse
)
from ..dependencies import get_api_key_data
from ..url_validator import validate_forum_url  # SSRF Protection

# Импортируем сервисы
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from services.complaint_analyzer_service import (
    parse_complaint_from_url,
    get_form_template,
    set_form_template,
    get_issue_types,
    add_issue_type,
    update_issue_type,
    delete_issue_type,
    check_complaint_form,
    analyze_complaint,
    extract_complaint_facts,
    generate_complaint_document_text,
    generate_ago_document_text,
    generate_custom_resolution_text
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/complaint", tags=["Анализ жалоб"])


# === ПАРСИНГ ===

@router.post(
    "/parse",
    response_model=ComplaintParseResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидный URL или ошибка парсинга"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
    },
    summary="Парсинг жалобы по URL",
    description="""
Парсит тему форума с жалобой и возвращает её текст.

**Важно:** Вызывается один раз в начале workflow. 
Полученный `content` затем передаётся в `/check-form`, `/analyze`, `/extract-facts`.

**Пример URL:** `https://forum.majestic-rp.ru/threads/123456/`
"""
)
async def parse_complaint(
    request: ComplaintParseRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Парсит жалобу по URL темы форума.

    Параметры:
    - **complaint_url**: URL темы с жалобой на форуме

    Возвращает:
    - **title**: Заголовок темы
    - **content**: Полный текст жалобы (все сообщения)
    - **thread_id**: ID темы на форуме
    """
    try:
        # SSRF Protection: Validate URL before parsing
        is_valid, error_msg = validate_forum_url(request.complaint_url)
        if not is_valid:
            logger.warning(f"[API/COMPLAINT] SSRF blocked: {error_msg} (URL: {request.complaint_url})")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"URL validation failed: {error_msg}"
            )

        logger.info(f"[API/COMPLAINT] Парсинг от ключа {api_key['key_prefix']}: {request.complaint_url}")

        result = await parse_complaint_from_url(request.complaint_url)

        return ComplaintParseResponse(
            title=result['title'],
            content=result['content'],
            thread_id=result.get('thread_id')
        )

    except ValueError as e:
        logger.warning(f"[API/COMPLAINT] Ошибка парсинга: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка парсинга: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === АНАЛИЗ ===

@router.post(
    "/check-form",
    response_model=ComplaintCheckFormResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
        503: {"model": ErrorResponse, "description": "AI-сервис временно недоступен"},
    },
    summary="Проверка формы жалобы",
    description="""
Проверяет соответствие жалобы установленному шаблону формы.

**Возвращает:**
- `is_valid` — корректна ли форма (true/false)
- `explanation` — текст для постановления об отказе (если форма невалидна)
- `issues` — коды проблем из списка issue_types сервера

**Шаблон формы** задаётся через `PUT /form-template`.
"""
)
async def check_form(
    request: ComplaintCheckFormRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Проверяет форму жалобы на соответствие шаблону.

    Параметры:
    - **server_id**: ID сервера (например, 'majestic_1')
    - **complaint_text**: Текст жалобы (из /parse)

    Возвращает:
    - **is_valid**: Корректна ли форма
    - **explanation**: Пояснение для постановления об отказе
    - **issues**: Список кодов проблем
    - **processing_time_ms**: Время обработки
    """
    try:
        start_time = time.time()

        logger.info(f"[API/COMPLAINT] Проверка формы от ключа {api_key['key_prefix']}: server={request.server_id}")

        result = await check_complaint_form(request.server_id, request.complaint_text)

        processing_time = (time.time() - start_time) * 1000

        return ComplaintCheckFormResponse(
            is_valid=result['is_valid'],
            explanation=result['explanation'],
            issues=result['issues'],
            processing_time_ms=processing_time
        )

    except ValueError as e:
        logger.warning(f"[API/COMPLAINT] Ошибка валидации: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[API/COMPLAINT] Runtime ошибка: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка проверки формы: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post(
    "/analyze",
    response_model=ComplaintAnalyzeResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
        503: {"model": ErrorResponse, "description": "AI-сервис временно недоступен"},
    },
    summary="Анализ жалобы",
    description="""
Проводит AI-анализ жалобы для маршрутизации.

**Определяет:**
- `complexity` — сложность рассмотрения (low/medium/high)
- `needs_additional_materials` — нужны ли дополнительные материалы
- `subject` — на кого жалоба (никнейм, организация, должность)
- `reviewer_level` — кто может рассмотреть (any/sgp/zgp/gp)
"""
)
async def analyze(
    request: ComplaintAnalyzeRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Анализирует жалобу для определения параметров маршрутизации.

    Параметры:
    - **server_id**: ID сервера
    - **complaint_text**: Текст жалобы (из /parse)

    Возвращает:
    - **complexity**: Сложность (low | medium | high)
    - **needs_additional_materials**: Нужны ли доп. материалы
    - **subject**: Информация о субъекте жалобы
    - **reviewer_level**: Минимальный уровень рецензента
    - **processing_time_ms**: Время обработки
    """
    try:
        start_time = time.time()

        logger.info(f"[API/COMPLAINT] Анализ от ключа {api_key['key_prefix']}: server={request.server_id}")

        result = await analyze_complaint(request.server_id, request.complaint_text)

        processing_time = (time.time() - start_time) * 1000

        from ..schemas import ComplaintSubject
        return ComplaintAnalyzeResponse(
            complexity=result['complexity'],
            needs_additional_materials=result['needs_additional_materials'],
            subject=ComplaintSubject(**result['subject']),
            reviewer_level=result['reviewer_level'],
            processing_time_ms=processing_time
        )

    except ValueError as e:
        logger.warning(f"[API/COMPLAINT] Ошибка валидации: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[API/COMPLAINT] Runtime ошибка: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка анализа: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post(
    "/extract-facts",
    response_model=ComplaintExtractFactsResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
        503: {"model": ErrorResponse, "description": "AI-сервис временно недоступен"},
    },
    summary="Извлечение фактов из жалобы",
    description="""
Извлекает юридически значимую информацию из жалобы.

**Возвращает:**
- `facts` — список юридически значимых фактов
- `requirements` — список требований заявителя
"""
)
async def extract_facts(
    request: ComplaintExtractFactsRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Извлекает факты и требования из текста жалобы.

    Параметры:
    - **server_id**: ID сервера
    - **complaint_text**: Текст жалобы (из /parse)

    Возвращает:
    - **facts**: Юридически значимые факты
    - **requirements**: Требования заявителя
    - **processing_time_ms**: Время обработки
    """
    try:
        start_time = time.time()

        logger.info(f"[API/COMPLAINT] Извлечение фактов от ключа {api_key['key_prefix']}: server={request.server_id}")

        result = await extract_complaint_facts(request.server_id, request.complaint_text)

        processing_time = (time.time() - start_time) * 1000

        return ComplaintExtractFactsResponse(
            facts=result['facts'],
            requirements=result['requirements'],
            processing_time_ms=processing_time
        )

    except ValueError as e:
        logger.warning(f"[API/COMPLAINT] Ошибка валидации: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[API/COMPLAINT] Runtime ошибка: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка извлечения фактов: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === ПОЛНЫЙ АНАЛИЗ (все виды за один вызов) ===

@router.post(
    "/full-analysis",
    response_model=ComplaintFullAnalysisResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
        503: {"model": ErrorResponse, "description": "AI-сервис временно недоступен"},
    },
    summary="Полный анализ жалобы",
    description="""
Выполняет все виды анализа за один HTTP-запрос:

1. **Проверка формы** (check-form) — валидация соответствия шаблону
2. **Анализ** (analyze) — определение сложности, субъекта, уровня рецензента
3. **Извлечение фактов** (extract-facts) — юридически значимые факты и требования

**Рекомендуется использовать этот эндпоинт** вместо вызова трёх отдельных эндпоинтов.
Все анализы выполняются параллельно для оптимизации времени.

**Пример использования:**
```json
POST /v1/complaint/full-analysis
{
    "server_id": "la_puerta",
    "complaint_text": "Текст жалобы...",
    "title": "Жалоба на сотрудника LSPD",
    "defendant": "John_Doe",
    "complainant": "Jane_Smith"
}
```
"""
)
async def full_analysis(
    request: ComplaintFullAnalysisRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Выполняет полный анализ жалобы за один вызов.

    Параметры:
    - **server_id**: ID сервера (например, 'la_puerta')
    - **complaint_text**: Текст жалобы
    - **title**: Заголовок жалобы (опционально)
    - **defendant**: Ответчик (опционально)
    - **complainant**: Заявитель (опционально)

    Возвращает:
    - **check_form**: Результат проверки формы
    - **analysis**: Результат анализа сложности/субъекта
    - **facts**: Извлечённые факты и требования
    - **processing_time_ms**: Общее время обработки
    """
    try:
        start_time = time.time()

        logger.info(f"[API/COMPLAINT] Полный анализ от ключа {api_key['key_prefix']}: server={request.server_id}")

        # Выполняем все анализы параллельно
        check_form_result, analyze_result, facts_result = await asyncio.gather(
            check_complaint_form(request.server_id, request.complaint_text),
            analyze_complaint(request.server_id, request.complaint_text),
            extract_complaint_facts(request.server_id, request.complaint_text),
            return_exceptions=True  # Не прерываем при ошибке одного из анализов
        )

        # Проверяем на исключения
        errors = []
        if isinstance(check_form_result, Exception):
            logger.error(f"[API/COMPLAINT] Ошибка check_form: {check_form_result}")
            errors.append(f"check_form: {check_form_result}")
            check_form_result = {'is_valid': True, 'explanation': '', 'issues': []}

        if isinstance(analyze_result, Exception):
            logger.error(f"[API/COMPLAINT] Ошибка analyze: {analyze_result}")
            errors.append(f"analyze: {analyze_result}")
            analyze_result = {
                'complexity': 'medium',
                'needs_additional_materials': False,
                'subject': {'target_type': None, 'nickname': None, 'organization': None, 'position': None},
                'reviewer_level': 'any'
            }

        if isinstance(facts_result, Exception):
            logger.error(f"[API/COMPLAINT] Ошибка extract_facts: {facts_result}")
            errors.append(f"extract_facts: {facts_result}")
            facts_result = {'facts': [], 'requirements': []}

        # Если все три анализа завершились с ошибками — возвращаем ошибку
        if len(errors) == 3:
            raise RuntimeError(f"Все анализы завершились с ошибками: {'; '.join(errors)}")

        processing_time = (time.time() - start_time) * 1000

        return ComplaintFullAnalysisResponse(
            check_form=ComplaintCheckFormResponse(
                is_valid=check_form_result['is_valid'],
                explanation=check_form_result['explanation'],
                issues=check_form_result['issues'],
                processing_time_ms=0  # включено в общее время
            ),
            analysis=ComplaintAnalyzeResponse(
                complexity=analyze_result['complexity'],
                needs_additional_materials=analyze_result['needs_additional_materials'],
                subject=ComplaintSubject(**analyze_result['subject']),
                reviewer_level=analyze_result['reviewer_level'],
                processing_time_ms=0  # включено в общее время
            ),
            facts=ComplaintExtractFactsResponse(
                facts=facts_result['facts'],
                requirements=facts_result['requirements'],
                processing_time_ms=0  # включено в общее время
            ),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        logger.warning(f"[API/COMPLAINT] Ошибка валидации: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[API/COMPLAINT] Runtime ошибка: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка полного анализа: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === УПРАВЛЕНИЕ ШАБЛОНАМИ ФОРМ ===

@router.get(
    "/form-template/{server_id}",
    response_model=FormTemplateResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        404: {"model": ErrorResponse, "description": "Шаблон не найден"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
    },
    summary="Получить шаблон формы",
    description="""
Возвращает текущий шаблон формы жалобы для сервера.

Если шаблон не задан, возвращает пустые поля `form_url` и `form_content`.
"""
)
async def get_template(
    server_id: str = Path(..., description="ID сервера (например, 'majestic_1')"),
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Получает шаблон формы для сервера.

    Параметры:
    - **server_id**: ID сервера

    Возвращает:
    - **form_url**: URL темы с образцом формы
    - **form_content**: Распарсенный текст образца
    - **updated_at**: Время последнего обновления
    """
    try:
        logger.info(f"[API/COMPLAINT] Получение шаблона от ключа {api_key['key_prefix']}: server={server_id}")

        result = await get_form_template(server_id)

        if not result:
            return FormTemplateResponse(
                server_id=server_id,
                form_url=None,
                form_content=None,
                updated_at=None
            )

        return FormTemplateResponse(
            server_id=result['server_id'],
            form_url=result.get('form_url'),
            form_content=result.get('form_content'),
            updated_at=result.get('updated_at')
        )

    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка получения шаблона: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.put(
    "/form-template",
    response_model=FormTemplateResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидный URL или ошибка парсинга"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
    },
    summary="Установить шаблон формы",
    description="""
Парсит URL темы с образцом формы и сохраняет как шаблон для сервера.

**Важно:** URL должен вести на тему форума с примером правильно заполненной жалобы.
При повторном вызове шаблон перезаписывается.
"""
)
async def set_template(
    request: FormTemplateSetRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Устанавливает шаблон формы для сервера.

    Параметры:
    - **server_id**: ID сервера
    - **form_url**: URL темы с образцом формы

    Возвращает:
    - **form_url**: Сохранённый URL
    - **form_content**: Распарсенный текст образца
    - **updated_at**: Время обновления
    """
    try:
        logger.info(f"[API/COMPLAINT] Установка шаблона от ключа {api_key['key_prefix']}: server={request.server_id}")

        result = await set_form_template(request.server_id, request.form_url)

        return FormTemplateResponse(
            server_id=result['server_id'],
            form_url=result.get('form_url'),
            form_content=result.get('form_content'),
            updated_at=result.get('updated_at')
        )

    except ValueError as e:
        logger.warning(f"[API/COMPLAINT] Ошибка установки шаблона: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка установки шаблона: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === УПРАВЛЕНИЕ ТИПАМИ ПРОБЛЕМ ===

@router.get(
    "/issue-types/{server_id}",
    response_model=IssueTypeListResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
    },
    summary="Получить типы проблем",
    description="""
Возвращает список типов проблем формы для сервера.

**При первом обращении** автоматически создаются дефолтные типы:
- `header` — Заголовок
- `nickname` — Никнейм
- `oos_mg_info` — OOC/МГ информация
- `description` — Описание
- `evidence` — Доказательства
- `requirements` — Требования
- `format` — Формат
- `attachments` — Вложения
- `other` — Другое
"""
)
async def list_issue_types(
    server_id: str = Path(..., description="ID сервера (например, 'majestic_1')"),
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Получает список типов проблем для сервера.

    Параметры:
    - **server_id**: ID сервера

    Возвращает:
    - **issue_types**: Список типов с кодом, названием, описанием и флагом обязательности
    """
    try:
        logger.info(f"[API/COMPLAINT] Получение issue_types от ключа {api_key['key_prefix']}: server={server_id}")

        result = await get_issue_types(server_id)

        return IssueTypeListResponse(
            server_id=server_id,
            issue_types=[IssueType(**it) for it in result]
        )

    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка получения issue_types: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post(
    "/issue-types",
    response_model=IssueType,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        409: {"model": ErrorResponse, "description": "Тип с таким кодом уже существует"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
    },
    summary="Создать тип проблемы",
    description="""
Добавляет новый тип проблемы для сервера.

**Код** должен быть в формате `snake_case` (латиница, цифры, подчёркивание).
"""
)
async def create_issue_type(
    request: IssueTypeCreateRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Создаёт новый тип проблемы.

    Параметры:
    - **server_id**: ID сервера
    - **code**: Код типа (snake_case, латиница)
    - **name**: Название
    - **description**: Описание (опционально)
    - **is_required**: Обязательное поле (по умолчанию false)

    Возвращает:
    - Созданный тип проблемы
    """
    try:
        logger.info(f"[API/COMPLAINT] Создание issue_type от ключа {api_key['key_prefix']}: "
                    f"server={request.server_id}, code={request.code}")

        result = await add_issue_type(
            server_id=request.server_id,
            code=request.code,
            name=request.name,
            description=request.description,
            is_required=request.is_required
        )

        return IssueType(**result)

    except ValueError as e:
        error_msg = str(e)
        if 'уже существует' in error_msg.lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_msg)
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка создания issue_type: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.put(
    "/issue-types/{server_id}/{code}",
    response_model=IssueType,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        404: {"model": ErrorResponse, "description": "Тип проблемы не найден"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
    },
    summary="Обновить тип проблемы",
    description="""
Обновляет существующий тип проблемы.

Можно обновить любое поле (name, description, is_required) независимо.
"""
)
async def update_existing_issue_type(
    request: IssueTypeUpdateRequest,
    server_id: str = Path(..., description="ID сервера (например, 'majestic_1')"),
    code: str = Path(..., description="Код типа проблемы (например, 'header')"),
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Обновляет тип проблемы.

    Параметры:
    - **server_id**: ID сервера
    - **code**: Код типа проблемы
    - **name**: Новое название (опционально)
    - **description**: Новое описание (опционально)
    - **is_required**: Обязательное поле (опционально)

    Возвращает:
    - Обновлённый тип проблемы
    """
    try:
        logger.info(f"[API/COMPLAINT] Обновление issue_type от ключа {api_key['key_prefix']}: "
                    f"server={server_id}, code={code}")

        result = await update_issue_type(
            server_id=server_id,
            code=code,
            name=request.name,
            description=request.description,
            is_required=request.is_required
        )

        return IssueType(**result)

    except ValueError as e:
        error_msg = str(e)
        if 'не найден' in error_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_msg)
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка обновления issue_type: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.delete(
    "/issue-types/{server_id}/{code}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        404: {"model": ErrorResponse, "description": "Тип проблемы не найден"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
    },
    summary="Удалить тип проблемы",
    description="""
Удаляет тип проблемы для сервера.

**Внимание:** Удаление необратимо. Если тип используется в проверках, они перестанут возвращать этот код.
"""
)
async def delete_existing_issue_type(
    server_id: str = Path(..., description="ID сервера (например, 'majestic_1')"),
    code: str = Path(..., description="Код типа проблемы (например, 'custom_type')"),
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Удаляет тип проблемы.

    Параметры:
    - **server_id**: ID сервера
    - **code**: Код типа проблемы

    Возвращает:
    - 204 No Content при успешном удалении
    """
    try:
        logger.info(f"[API/COMPLAINT] Удаление issue_type от ключа {api_key['key_prefix']}: "
                    f"server={server_id}, code={code}")

        deleted = await delete_issue_type(server_id, code)

        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Тип проблемы '{code}' не найден для сервера {server_id}"
            )

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Ошибка удаления issue_type: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === ГЕНЕРАЦИЯ ДОКУМЕНТОВ ===

@router.post(
    "/generate-document-text",
    response_model=ComplaintDocumentGenerateResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Ошибка генерации"},
    },
    summary="AI-генерация текста документа ответа",
    description="""
Генерирует текст документа ответа на жалобу с помощью AI.

Текст возвращается разбитым на 4 зоны для редактирования:
- **header_0**: Дата (26th of January 2026)
- **main_0**: Re: обращение... + Dear Mr...
- **main_1**: Основной текст документа  
- **bottom_0**: Sincerely, + подпись

**Типы документов:**
- `accept` — принятие в производство
- `reject` — отклонение обращения
- `close` — завершение производства

После получения текста, его можно редактировать и отправлять в python-renderer 
для генерации PNG с обновлённым текстом в реальном времени.
"""
)
async def generate_document_text(
    request: ComplaintDocumentGenerateRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Генерирует текст документа ответа на жалобу.
    """
    start_time = time.time()

    try:
        logger.info(f"[API/COMPLAINT] Генерация документа от ключа {api_key['key_prefix']}: "
                    f"type={request.document_type}, case={request.case_number}")

        # Формируем additional_data из request
        additional_data = {
            'rejection_reason': request.rejection_reason,
            'rejection_issues': request.rejection_issues,
            'defendant': request.defendant,
            'conclusion_type': request.conclusion_type,
            'charges': request.charges,
            'charges_type': request.charges_type,
            'sanction_sum': request.sanction_sum,
            'jurisdiction_court': request.jurisdiction_court,
            'is_complainant_victim': request.is_complainant_victim,
            'additional_context': request.additional_context
        }
        # Убираем None значения
        additional_data = {k: v for k, v in additional_data.items() if v is not None}

        result = await generate_complaint_document_text(
            server_id=request.server_id,
            document_type=request.document_type,
            case_number=request.case_number,
            complainant=request.complainant,
            attorney=request.attorney,
            rank=request.rank,
            complaint_text=request.complaint_text,
            complaint_summary=request.complaint_summary,
            additional_data=additional_data
        )

        processing_time = (time.time() - start_time) * 1000

        return ComplaintDocumentGenerateResponse(
            text_blocks=DocumentTextBlocks(**result['text_blocks']),
            raw_text=result['raw_text'],
            document_type=request.document_type,
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[API/COMPLAINT] Ошибка генерации документа: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"[API/COMPLAINT] Неожиданная ошибка генерации: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Внутренняя ошибка сервера"
        )


# === AGO DOCUMENT AI GENERATION ===

@router.post(
    "/generate-ago-text",
    response_model=AgoGenerateTextResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
        503: {"model": ErrorResponse, "description": "AI-сервис временно недоступен"},
    },
    summary="AI-генерация текста постановления (AGO)",
    description="""
Генерирует текст постановления Офиса Генерального прокурора с использованием AI.

**Типы постановлений:**
- `REQUEST_INFO` — Об истребовании информации
- `INITIATE_CASE` — О возбуждении дела
- `CLOSE_CASE` — О завершении производства
- `FINE_ORDER` — О привлечении к ответственности (штраф)
- `DISMISSAL_ORDER` — О расторжении трудового договора
- `INTERROGATION_ORDER` — О назначении допроса
- `CANCEL_ORDER` — Об отмене правового акта

**Особенности:**
- Использует RAG-фильтрацию для выбора релевантных законов
- Генерирует текст в административно-канцелярском стиле
- Возвращает `title` и `main_text` для рендеринга документа
"""
)
async def generate_ago_text(
    request: AgoGenerateTextRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Генерирует текст AGO-документа через AI.
    """
    start_time = time.time()

    try:
        logger.info(f"[API/AGO] AI-генерация AGO от ключа {api_key['key_prefix']}: "
                    f"type={request.ago_type}, defendant={request.defendant}")

        # Собираем данные формы
        form_data = {
            'attorney': request.attorney,
            'rank': request.rank,
            'defendant': request.defendant,
            'complainant': request.complainant,
            'organization': request.organization,
            'case_number': request.case_number,
            'discord_tag': request.discord_tag,
            'incident_date': request.incident_date,
            'incident_time': request.incident_time,
            'arrest_type': request.arrest_type,
            'charges': request.charges,
            'charges_type': request.charges_type,
            'is_witness': request.is_witness,
            'finish_reason': request.finish_reason,
            'fine_amount': request.fine_amount,
            'dismissal_reason': request.dismissal_reason,
            'previous_charges': request.previous_charges,
            'interrogation_date': request.interrogation_date,
            'interrogation_time': request.interrogation_time,
            'cancel_ago_num': request.cancel_ago_num,
        }
        # Убираем None значения
        form_data = {k: v for k, v in form_data.items() if v is not None}

        result = await generate_ago_document_text(
            server_id=request.server_id,
            ago_type=request.ago_type,
            form_data=form_data,
            situation_description=request.situation_description,
            complaint_id=request.complaint_id
        )

        processing_time = (time.time() - start_time) * 1000

        # Формируем text_blocks если они есть в результате
        text_blocks = None
        if 'text_blocks' in result:
            from api.schemas import AgoTextBlocks
            text_blocks = AgoTextBlocks(**result['text_blocks'])

        return AgoGenerateTextResponse(
            title=result['title'],
            main_text=result['main_text'],
            text_blocks=text_blocks,
            ago_type=request.ago_type,
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[API/AGO] Ошибка AI-генерации: {e}")
        if "перегружен" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(e)
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"[API/AGO] Неожиданная ошибка: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Внутренняя ошибка сервера"
        )


@router.post(
    "/generate-custom-resolution",
    response_model=CustomResolutionGenerateResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
        503: {"model": ErrorResponse, "description": "AI-сервис временно недоступен"},
    },
    summary="AI-генерация кастомного постановления",
    description="""
Генерирует кастомное постановление по произвольному описанию пользователя.

**Использование:**
- Пользователь описывает какое постановление нужно
- AI генерирует текст с учётом законодательства сервера
- Можно добавить контекст (текст жалобы, данные дела)

**Особенности:**
- Использует RAG-фильтрацию для выбора релевантных законов
- Генерирует текст в административно-канцелярском стиле
- Подходит для нестандартных типов постановлений
"""
)
async def generate_custom_resolution(
    request: CustomResolutionGenerateRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Генерирует кастомное постановление через AI.
    """
    start_time = time.time()

    try:
        logger.info(f"[API/CUSTOM] AI-генерация кастомного постановления от ключа {api_key['key_prefix']}")

        result = await generate_custom_resolution_text(
            server_id=request.server_id,
            user_description=request.user_description,
            context=request.context,
            complaint_id=request.complaint_id
        )

        processing_time = (time.time() - start_time) * 1000

        return CustomResolutionGenerateResponse(
            title=result['title'],
            main_text=result['main_text'],
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[API/CUSTOM] Ошибка AI-генерации: {e}")
        if "перегружен" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(e)
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"[API/CUSTOM] Неожиданная ошибка: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Внутренняя ошибка сервера"
        )


# END OF FILE api/routers/complaint_analyzer.py
