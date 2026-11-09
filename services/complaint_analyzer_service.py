# START OF FILE services/complaint_analyzer_service.py

"""
Сервис для Complaint Analyzer API.

Бизнес-логика для анализа жалоб:
- Парсинг жалоб по URL
- Проверка формы жалобы
- Анализ сложности и субъекта
- Извлечение фактов и требований
- Управление шаблонами форм и типами проблем
- Генерация AGO-документов через AI
"""

import asyncio
import json
import logging
import re
from typing import Optional, Dict, List, Tuple

from google.genai import errors as genai_errors
from google.genai import types
from google.api_core import exceptions as google_exceptions

from database.manager import db_manager
from config import (
    COMPLAINT_ANALYZER_MODEL_NAME,
    DEFAULT_COMPLAINT_ISSUE_TYPES,
    COMPLAINT_COMPLEXITY_LEVELS,
    COMPLAINT_REVIEWER_LEVELS,
    COMPLAINT_TARGET_TYPES
)
from prompts import (
    get_complaint_form_check_prompt,
    get_complaint_analysis_prompt,
    get_complaint_facts_extraction_prompt,
    get_document_law_router_prompt,
    get_ago_generation_prompt,
    get_custom_resolution_prompt
)
from rate_limiter import (
    execute_vertex_request,
    RequestPriority,
    get_current_client,
    mark_current_region_success,
    mark_current_region_error
)
from utils import reinitialize_vertex_ai
from remote_parser_client import remote_parse_topic

logger = logging.getLogger(__name__)


# === ПАРСИНГ ЖАЛОБЫ ===

async def parse_complaint_from_url(url: str) -> Dict:
    """
    Парсит жалобу по URL.

    Args:
        url: URL темы с жалобой на форуме

    Returns:
        Dict: {title, content, thread_id}

    Raises:
        ValueError: Если URL невалидный или парсинг не удался
    """
    logger.info(f"[COMPLAINT_ANALYZER] Парсинг жалобы: {url}")

    # content_cleanup=False: в жалобах blockquote-цитаты — содержимое (цитируемые
    # доказательства/реплики), чистка разметки rmrp-законки здесь недопустима.
    result = await remote_parse_topic(url, parse_only_author=False, content_cleanup=False)

    if not result:
        raise ValueError(f"Не удалось распарсить жалобу по URL: {url}")

    title = result.get('title', '')
    content = result.get('content', '')
    thread_id = result.get('thread_id')

    if not content:
        raise ValueError("Жалоба не содержит текста")

    logger.info(f"[COMPLAINT_ANALYZER] Успешно распарсено: {len(content)} символов")

    return {
        'title': title,
        'content': content,
        'thread_id': thread_id
    }


# === УПРАВЛЕНИЕ ШАБЛОНАМИ ФОРМ ===

async def get_form_template(server_id: str) -> Optional[Dict]:
    """
    Получает шаблон формы для сервера.

    Returns:
        Dict с form_url, form_content, updated_at или None
    """
    query = """
        SELECT server_id, form_url, form_content, updated_at
        FROM complaint_form_templates
        WHERE server_id = $1
    """
    row = await db_manager.fetchrow(query, server_id)

    if row:
        return dict(row)
    return None


async def set_form_template(server_id: str, form_url: str) -> Dict:
    """
    Устанавливает шаблон формы для сервера.
    Парсит URL и сохраняет контент.

    Args:
        server_id: ID сервера
        form_url: URL темы с образцом формы

    Returns:
        Dict с информацией о шаблоне

    Raises:
        ValueError: Если парсинг не удался
    """
    logger.info(f"[COMPLAINT_ANALYZER] Установка шаблона формы для {server_id}: {form_url}")

    # Парсим URL (content_cleanup=False: образец формы может быть завёрнут в цитату)
    result = await remote_parse_topic(form_url, parse_only_author=True, content_cleanup=False)

    if not result:
        raise ValueError(f"Не удалось распарсить шаблон по URL: {form_url}")

    form_content = result.get('content', '')

    if not form_content:
        raise ValueError("Шаблон не содержит текста")

    # Сохраняем в БД (upsert)
    query = """
        INSERT INTO complaint_form_templates (server_id, form_url, form_content, updated_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (server_id)
        DO UPDATE SET form_url = $2, form_content = $3, updated_at = NOW()
        RETURNING server_id, form_url, form_content, updated_at
    """
    row = await db_manager.fetchrow(query, server_id, form_url, form_content)

    logger.info(f"[COMPLAINT_ANALYZER] Шаблон формы сохранён для {server_id}")

    return dict(row)


# === УПРАВЛЕНИЕ ТИПАМИ ПРОБЛЕМ ===

async def get_issue_types(server_id: str) -> List[Dict]:
    """
    Получает список типов проблем для сервера.
    Если таблица пуста для сервера - автоматически вставляет дефолтные типы.

    Returns:
        Список словарей {code, name, description, is_required}
    """
    query = """
        SELECT code, name, description, is_required
        FROM complaint_issue_types
        WHERE server_id = $1
        ORDER BY created_at
    """
    rows = await db_manager.fetch(query, server_id)

    if not rows:
        # Автоинициализация дефолтных типов
        logger.info(f"[COMPLAINT_ANALYZER] Инициализация дефолтных issue_types для {server_id}")
        await _initialize_default_issue_types(server_id)
        rows = await db_manager.fetch(query, server_id)

    return [dict(row) for row in rows]


async def _initialize_default_issue_types(server_id: str) -> None:
    """Вставляет дефолтные типы проблем для сервера."""
    for issue_type in DEFAULT_COMPLAINT_ISSUE_TYPES:
        query = """
            INSERT INTO complaint_issue_types (server_id, code, name, description, is_required)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (server_id, code) DO NOTHING
        """
        await db_manager.execute(
            query,
            server_id,
            issue_type['code'],
            issue_type['name'],
            issue_type.get('description'),
            issue_type.get('is_required', False)
        )


async def add_issue_type(
    server_id: str,
    code: str,
    name: str,
    description: Optional[str] = None,
    is_required: bool = False
) -> Dict:
    """
    Добавляет новый тип проблемы.

    Args:
        server_id: ID сервера
        code: Код (snake_case)
        name: Название
        description: Описание
        is_required: Обязательное поле

    Returns:
        Dict с данными созданного типа

    Raises:
        ValueError: Если тип с таким кодом уже существует
    """
    # Нормализуем код
    code = code.lower().strip()

    # Проверяем формат
    if not re.match(r'^[a-z][a-z0-9_]*$', code):
        raise ValueError("Код должен быть в формате snake_case (латиница, цифры, _)")

    query = """
        INSERT INTO complaint_issue_types (server_id, code, name, description, is_required)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING code, name, description, is_required
    """
    try:
        row = await db_manager.fetchrow(query, server_id, code, name, description, is_required)
        logger.info(f"[COMPLAINT_ANALYZER] Добавлен issue_type '{code}' для {server_id}")
        return dict(row)
    except Exception as e:
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            raise ValueError(f"Тип проблемы с кодом '{code}' уже существует для сервера {server_id}")
        raise


async def update_issue_type(
    server_id: str,
    code: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_required: Optional[bool] = None
) -> Dict:
    """
    Обновляет тип проблемы.

    Returns:
        Dict с обновлёнными данными

    Raises:
        ValueError: Если тип не найден
    """
    # Формируем динамический UPDATE
    updates = []
    params = [server_id, code]
    param_idx = 3

    if name is not None:
        updates.append(f"name = ${param_idx}")
        params.append(name)
        param_idx += 1

    if description is not None:
        updates.append(f"description = ${param_idx}")
        params.append(description)
        param_idx += 1

    if is_required is not None:
        updates.append(f"is_required = ${param_idx}")
        params.append(is_required)
        param_idx += 1

    if not updates:
        # Ничего не обновляем, просто возвращаем текущие данные
        query = """
            SELECT code, name, description, is_required
            FROM complaint_issue_types
            WHERE server_id = $1 AND code = $2
        """
        row = await db_manager.fetchrow(query, server_id, code)
        if not row:
            raise ValueError(f"Тип проблемы '{code}' не найден для сервера {server_id}")
        return dict(row)

    query = f"""
        UPDATE complaint_issue_types
        SET {', '.join(updates)}
        WHERE server_id = $1 AND code = $2
        RETURNING code, name, description, is_required
    """
    row = await db_manager.fetchrow(query, *params)

    if not row:
        raise ValueError(f"Тип проблемы '{code}' не найден для сервера {server_id}")

    logger.info(f"[COMPLAINT_ANALYZER] Обновлён issue_type '{code}' для {server_id}")
    return dict(row)


async def delete_issue_type(server_id: str, code: str) -> bool:
    """
    Удаляет тип проблемы.

    Returns:
        True если удалён, False если не найден
    """
    query = """
        DELETE FROM complaint_issue_types
        WHERE server_id = $1 AND code = $2
        RETURNING id
    """
    result = await db_manager.fetchval(query, server_id, code)

    if result:
        logger.info(f"[COMPLAINT_ANALYZER] Удалён issue_type '{code}' для {server_id}")
        return True
    return False


# === AI-АНАЛИЗ ===

async def _get_relevant_laws_with_rag(context_text: str, server_id: str) -> List[types.Part]:
    """
    RAG-фильтр законов: AI-роутер выбирает релевантные законы по контексту.

    Адаптация из qa_service._get_relevant_laws() — вместо вопроса
    используется контекст документа (тип + описание ситуации + текст жалобы).

    Args:
        context_text: Контекст для выбора законов (описание ситуации, текст жалобы)
        server_id: ID сервера

    Returns:
        Список types.Part с gemini_file_uri релевантных законов
    """
    from config import ROUTER_MODEL_NAME
    from prompts import get_document_law_router_prompt

    start_time = asyncio.get_running_loop().time()
    logger.info(f"[COMPLAINT_ANALYZER_RAG] Запуск RAG-фильтра законов для сервера {server_id}...")

    try:
        # Загружаем все законы из БД
        law_records = await db_manager.fetch(
            "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )

        if not law_records:
            logger.warning(f"[COMPLAINT_ANALYZER_RAG] В БД отсутствуют документы для сервера {server_id}.")
            return []

        # Создаём карту законов по индексу
        law_map_by_index = {
            i + 1: {'title': r['title'], 'uri': r['gemini_file_uri']}
            for i, r in enumerate(law_records)
        }
        law_titles_str = "\n".join([f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)])

        # Формируем промпт для роутера
        router_prompt = get_document_law_router_prompt(
            context_text=context_text[:3000],  # Ограничиваем размер контекста
            law_titles_str=law_titles_str
        )

        # Функция запроса к AI-роутеру
        async def make_router_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ROUTER_MODEL_NAME,
                contents=router_prompt
            )

        # Выполняем запрос через rate limiter
        response = await execute_vertex_request(
            func=make_router_request,
            model_name=ROUTER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        # Парсим ответ (номера законов)
        selected_indices = {int(line.strip()) for line in response.text.split('\n') if line.strip().isdigit()}
        relevant_laws = [law_map_by_index[index] for index in sorted(selected_indices) if index in law_map_by_index]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logger.info(
            f"[COMPLAINT_ANALYZER_RAG] RAG-фильтр выбрал {len(relevant_laws)} законов за {duration:.2f} мс. "
            f"Законы: {[law['title'] for law in relevant_laws]}"
        )

        # Возвращаем Part объекты с URI
        return [types.Part.from_uri(file_uri=law['uri'], mime_type="text/plain") for law in relevant_laws]

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.warning(f"[COMPLAINT_ANALYZER_RAG] Ошибка RAG-фильтра: {e}. Возврат пустого списка.")
        return []
    except Exception as e:
        logger.error(f"[COMPLAINT_ANALYZER_RAG] Критическая ошибка RAG-фильтра.", exc_info=e)
        return []


async def _get_relevant_laws_with_rag_extended(context_text: str, server_id: str) -> Tuple[List[types.Part], List[str]]:
    """
    RAG-фильтр законов: возвращает и Part объекты, и названия законов.

    Расширенная версия _get_relevant_laws_with_rag() для CMS API,
    которая также возвращает названия выбранных законов для ответа клиенту.

    Args:
        context_text: Контекст для выбора законов (описание ситуации, текст жалобы)
        server_id: ID сервера

    Returns:
        Tuple[List[types.Part], List[str]]: (law_parts для контекста AI, law_titles для ответа)
    """
    from config import ROUTER_MODEL_NAME
    from prompts import get_document_law_router_prompt

    start_time = asyncio.get_running_loop().time()
    logger.info(f"[RAG_EXTENDED] Запуск RAG-фильтра законов для сервера {server_id}...")

    try:
        # Загружаем все законы из БД
        law_records = await db_manager.fetch(
            "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )

        if not law_records:
            logger.warning(f"[RAG_EXTENDED] В БД отсутствуют документы для сервера {server_id}.")
            return [], []

        # Создаём карту законов по индексу
        law_map_by_index = {
            i + 1: {'title': r['title'], 'uri': r['gemini_file_uri']}
            for i, r in enumerate(law_records)
        }
        law_titles_str = "\n".join([f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)])

        # Формируем промпт для роутера
        router_prompt = get_document_law_router_prompt(
            context_text=context_text[:3000],
            law_titles_str=law_titles_str
        )

        # Функция запроса к AI-роутеру
        async def make_router_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ROUTER_MODEL_NAME,
                contents=router_prompt
            )

        # Выполняем запрос через rate limiter
        response = await execute_vertex_request(
            func=make_router_request,
            model_name=ROUTER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        # Парсим ответ (номера законов)
        selected_indices = {int(line.strip()) for line in response.text.split('\n') if line.strip().isdigit()}
        relevant_laws = [law_map_by_index[index] for index in sorted(selected_indices) if index in law_map_by_index]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logger.info(
            f"[RAG_EXTENDED] RAG-фильтр выбрал {len(relevant_laws)} законов за {duration:.2f} мс. "
            f"Законы: {[law['title'] for law in relevant_laws]}"
        )

        # Возвращаем и Parts, и названия
        law_parts = [types.Part.from_uri(file_uri=law['uri'], mime_type="text/plain") for law in relevant_laws]
        law_titles = [law['title'] for law in relevant_laws]

        return law_parts, law_titles

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.warning(f"[RAG_EXTENDED] Ошибка RAG-фильтра: {e}. Возврат пустых списков.")
        return [], []
    except Exception as e:
        logger.error(f"[RAG_EXTENDED] Критическая ошибка RAG-фильтра.", exc_info=e)
        return [], []


async def _get_relevant_laws(server_id: str) -> str:
    """
    Получает все законы для сервера (упрощённая версия без RAG-фильтра).

    Используется в analyze_complaint и generate_complaint_document_text
    где нужен полный контекст законов.

    Args:
        server_id: ID сервера

    Returns:
        Строка с текстами законов (первые N документов)
    """
    try:
        law_records = await db_manager.fetch(
            """SELECT title, content 
               FROM law_documents 
               WHERE server_id = $1 AND content IS NOT NULL
               ORDER BY title
               LIMIT 10""",
            server_id
        )

        if not law_records:
            logger.warning(f"[COMPLAINT_ANALYZER] Нет законов для сервера {server_id}")
            return ""

        laws_text = "\n\n---\n\n".join([
            f"# {r['title']}\n{r['content'][:5000]}"
            for r in law_records
        ])

        logger.info(f"[COMPLAINT_ANALYZER] Загружено {len(law_records)} законов для {server_id}")
        return laws_text[:30000]  # Ограничиваем общий размер

    except Exception as e:
        logger.error(f"[COMPLAINT_ANALYZER] Ошибка загрузки законов: {e}")
        return ""


async def check_complaint_form(server_id: str, complaint_text: str) -> Dict:
    """
    Проверяет форму жалобы на соответствие шаблону.

    Args:
        server_id: ID сервера
        complaint_text: Текст жалобы

    Returns:
        Dict: {is_valid, explanation, issues}

    Raises:
        RuntimeError: При ошибке AI
    """
    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Получаем шаблон формы
    template = await get_form_template(server_id)
    form_template = template.get('form_content', '') if template else ''

    # Получаем типы проблем
    issue_types = await get_issue_types(server_id)
    issue_types_json = json.dumps(issue_types, ensure_ascii=False, indent=2)

    # Формируем промпт
    prompt = get_complaint_form_check_prompt(complaint_text, form_template, issue_types_json)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=2048
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Парсим JSON из ответа
        result = _parse_json_response(response.text)

        # Валидируем ответ
        is_valid = result.get('is_valid', False)
        explanation = result.get('explanation', '')
        issues = result.get('issues', [])

        # Фильтруем только валидные коды
        valid_codes = {it['code'] for it in issue_types}
        issues = [code for code in issues if code in valid_codes]

        logger.info(f"[COMPLAINT_ANALYZER] Проверка формы: valid={is_valid}, issues={len(issues)}")

        return {
            'is_valid': is_valid,
            'explanation': explanation,
            'issues': issues
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")


async def analyze_complaint(server_id: str, complaint_text: str) -> Dict:
    """
    Анализирует жалобу: сложность, субъект, рецензент.

    Args:
        server_id: ID сервера
        complaint_text: Текст жалобы

    Returns:
        Dict: {complexity, needs_additional_materials, subject, reviewer_level}

    Raises:
        RuntimeError: При ошибке AI
    """
    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Получаем законы
    relevant_laws = await _get_relevant_laws(server_id)

    # Формируем промпт
    prompt = get_complaint_analysis_prompt(complaint_text, relevant_laws)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=2048
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Парсим JSON
        result = _parse_json_response(response.text)

        # Валидируем и нормализуем
        complexity = result.get('complexity', 'medium')
        if complexity not in COMPLAINT_COMPLEXITY_LEVELS:
            complexity = 'medium'

        reviewer_level = result.get('reviewer_level', 'any')
        if reviewer_level not in COMPLAINT_REVIEWER_LEVELS:
            reviewer_level = 'any'

        subject = result.get('subject', {})
        target_type = subject.get('target_type')
        if target_type and target_type not in COMPLAINT_TARGET_TYPES:
            target_type = None

        logger.info(f"[COMPLAINT_ANALYZER] Анализ: complexity={complexity}, reviewer={reviewer_level}")

        return {
            'complexity': complexity,
            'needs_additional_materials': bool(result.get('needs_additional_materials', False)),
            'subject': {
                'target_type': target_type,
                'nickname': subject.get('nickname'),
                'organization': subject.get('organization'),
                'position': subject.get('position')
            },
            'reviewer_level': reviewer_level
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")


async def extract_complaint_facts(server_id: str, complaint_text: str) -> Dict:
    """
    Извлекает факты и требования из жалобы.

    Args:
        server_id: ID сервера (для будущего расширения)
        complaint_text: Текст жалобы

    Returns:
        Dict: {facts, requirements}

    Raises:
        RuntimeError: При ошибке AI
    """
    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем промпт
    prompt = get_complaint_facts_extraction_prompt(complaint_text)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=4096
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Парсим JSON
        result = _parse_json_response(response.text)

        facts = result.get('facts', [])
        requirements = result.get('requirements', [])

        # Убедимся что это списки строк
        if not isinstance(facts, list):
            facts = []
        if not isinstance(requirements, list):
            requirements = []

        facts = [str(f) for f in facts if f]
        requirements = [str(r) for r in requirements if r]

        logger.info(f"[COMPLAINT_ANALYZER] Извлечено: {len(facts)} фактов, {len(requirements)} требований")

        return {
            'facts': facts,
            'requirements': requirements
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")


def _parse_json_response(text: str) -> Dict:
    """
    Извлекает JSON из ответа AI.
    Обрабатывает случаи когда JSON обёрнут в markdown блок.
    Всегда возвращает словарь (пустой при неудаче).
    """
    if not text:
        logger.warning("[COMPLAINT_ANALYZER] Пустой текст для парсинга")
        return {}

    text = text.strip()

    # Логируем входные данные для отладки
    logger.debug(f"[COMPLAINT_ANALYZER] Парсинг JSON, длина текста: {len(text)}")

    # Убираем markdown блоки ```json или ```
    if text.startswith('```'):
        lines = text.split('\n')
        # Убираем первую строку с ``` или ```json
        if lines[0].startswith('```'):
            lines = lines[1:]
        # Убираем последнюю строку с ```
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines).strip()

    # Пытаемся парсить JSON напрямую
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            logger.debug(f"[COMPLAINT_ANALYZER] JSON успешно распарсен напрямую")
            return result
        else:
            logger.warning(f"[COMPLAINT_ANALYZER] JSON распарсен, но это не словарь: {type(result).__name__}")
            return {}
    except json.JSONDecodeError as e:
        logger.debug(f"[COMPLAINT_ANALYZER] Прямой парсинг не удался: {e}")

    # Пробуем найти JSON объект в тексте (от { до последней })
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            json_text = json_match.group()
            result = json.loads(json_text)
            if isinstance(result, dict):
                logger.debug(f"[COMPLAINT_ANALYZER] JSON найден через regex")
                return result
            else:
                logger.warning(f"[COMPLAINT_ANALYZER] Regex JSON не словарь: {type(result).__name__}")
        except json.JSONDecodeError as e:
            logger.debug(f"[COMPLAINT_ANALYZER] Regex парсинг не удался: {e}")
            # Пробуем исправить экранирование
            try:
                # Заменяем неэкранированные переносы строк
                fixed_json = json_text.replace('\n', '\\n').replace('\r', '\\r')
                result = json.loads(fixed_json)
                if isinstance(result, dict):
                    logger.debug(f"[COMPLAINT_ANALYZER] JSON распарсен после исправления переносов")
                    return result
            except json.JSONDecodeError:
                pass

    logger.warning(f"[COMPLAINT_ANALYZER] Не удалось распарсить JSON: {text[:500]}")
    return {}


# === ГЕНЕРАЦИЯ ТЕКСТА ДОКУМЕНТОВ ===

async def generate_complaint_document_text(
    server_id: str,
    document_type: str,
    case_number: str,
    complainant: str,
    attorney: str,
    rank: str,
    complaint_text: str = None,
    complaint_summary: str = None,
    additional_data: dict = None
) -> Dict:
    """
    Генерирует текст документа ответа на жалобу через AI.

    Args:
        server_id: ID сервера
        document_type: 'accept', 'reject', 'close'
        case_number: Номер обращения
        complainant: ФИО заявителя
        attorney: ФИО прокурора
        rank: Должность прокурора
        complaint_text: Текст жалобы
        complaint_summary: AI-резюме жалобы
        additional_data: Доп. данные (rejection_issues, defendant, charges и т.д.)

    Returns:
        Dict с text_blocks (header_0, main_0, main_1, bottom_0) и raw_text
    """
    from prompts import get_complaint_response_generation_prompt

    logger.info(f"[COMPLAINT_ANALYZER] Генерация документа '{document_type}' для обращения {case_number}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем контекст жалобы
    complaint_context = complaint_summary or complaint_text or "Контекст не предоставлен"

    # Получаем релевантные законы
    relevant_laws = await _get_relevant_laws(server_id)

    # Формируем дополнительные данные как строку
    additional_data_str = json.dumps(additional_data or {}, ensure_ascii=False, indent=2)

    prompt = get_complaint_response_generation_prompt(
        document_type=document_type,
        case_number=case_number,
        complainant=complainant,
        attorney=attorney,
        rank=rank,
        complaint_context=complaint_context,
        relevant_laws=relevant_laws,
        additional_data=additional_data_str
    )

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=2048
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Парсим JSON из ответа
        result = _parse_json_response(response.text)

        # Валидируем наличие всех полей
        required_fields = ['header_0', 'main_0', 'main_1', 'bottom_0']
        for field in required_fields:
            if field not in result:
                result[field] = ''

        # Формируем raw_text
        raw_text = f"{result.get('main_0', '')}\n\n{result.get('main_1', '')}\n\n{result.get('bottom_0', '')}"

        logger.info(f"[COMPLAINT_ANALYZER] Документ '{document_type}' успешно сгенерирован")

        return {
            'text_blocks': {
                'header_0': result.get('header_0', ''),
                'main_0': result.get('main_0', ''),
                'main_1': result.get('main_1', ''),
                'bottom_0': result.get('bottom_0', '')
            },
            'raw_text': raw_text
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[COMPLAINT_ANALYZER] Ошибка генерации документа: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации документа: {str(e)}")


# === AGO DOCUMENT AI GENERATION ===

async def generate_ago_document_text(
    server_id: str,
    ago_type: str,
    form_data: Dict,
    situation_description: str = None,
    complaint_id: Optional[int] = None
) -> Dict:
    """
    Генерирует текст AGO-документа (постановления) через AI с RAG-фильтрацией законов.

    Args:
        server_id: ID сервера для загрузки законов
        ago_type: Тип постановления (REQUEST_INFO, INITIATE_CASE, CLOSE_CASE,
                  FINE_ORDER, DISMISSAL_ORDER, INTERROGATION_ORDER, CANCEL_ORDER)
        form_data: Данные формы (attorney, rank, defendant, complainant, organization и т.д.)
        situation_description: Описание ситуации / контекст (опционально)
        complaint_id: ID привязанного обращения (опционально, для будущего расширения)

    Returns:
        Dict: {title: str, main_text: str}

    Raises:
        RuntimeError: При ошибке AI или аутентификации
    """
    logger.info(f"[AGO_GENERATOR] Генерация AGO '{ago_type}' для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем контекст для RAG-фильтра
    context_for_rag = f"Тип документа: {ago_type}\n"
    if situation_description:
        context_for_rag += f"Описание ситуации: {situation_description}\n"
    if form_data.get('charges'):
        context_for_rag += f"Статья: {form_data.get('charges')}\n"
    if form_data.get('charges_type'):
        context_for_rag += f"Тип обвинения: {form_data.get('charges_type')}\n"

    # Получаем релевантные законы через RAG
    law_parts = await _get_relevant_laws_with_rag(context_for_rag, server_id)

    logger.info(f"[AGO_GENERATOR] RAG вернул {len(law_parts)} законов")

    # Формируем данные формы как JSON строку
    document_data_str = json.dumps(form_data, ensure_ascii=False, indent=2)

    # Формируем промпт
    prompt_text = get_ago_generation_prompt(
        ago_type=ago_type,
        document_data=document_data_str,
        situation_description=situation_description or ""
    )

    # Собираем контент: промпт + законы как файлы
    contents = [prompt_text]
    if law_parts:
        contents.extend(law_parts)

    logger.info(f"[AGO_GENERATOR] Отправка запроса в AI, law_parts={len(law_parts)}")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.4,
                max_output_tokens=4096
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Логируем сырой ответ для отладки
        raw_response = response.text if hasattr(response, 'text') else str(response)
        logger.info(f"[AGO_GENERATOR] Сырой ответ AI (первые 500 символов): {raw_response[:500]}")

        # Парсим JSON из ответа
        result = _parse_json_response(raw_response)

        logger.info(f"[AGO_GENERATOR] Распарсенный результат: {list(result.keys()) if result else 'пусто'}")

        title = result.get('title', 'Постановление')

        # Проверяем новый формат с блоками
        if 'preamble' in result:
            # Новый формат с text_blocks
            text_blocks = {
                'title': title,
                'preamble': result.get('preamble', ''),
                'section_1': result.get('section_1', ''),
                'section_2': result.get('section_2', ''),
                'section_3': result.get('section_3', ''),
                'section_4': result.get('section_4', ''),
            }

            # Убираем пустые секции
            text_blocks = {k: v for k, v in text_blocks.items() if v}

            # Собираем main_text для обратной совместимости
            main_text_parts = [text_blocks.get('preamble', '')]
            for i in range(1, 5):
                section = text_blocks.get(f'section_{i}')
                if section:
                    main_text_parts.append(section)
            main_text = '\n\n'.join(main_text_parts)

            logger.info(f"[AGO_GENERATOR] Успешно сгенерирован AGO '{ago_type}': {title}, блоков: {len(text_blocks)}")

            return {
                'title': title,
                'main_text': main_text,
                'text_blocks': text_blocks
            }
        else:
            # Старый формат с main_text
            main_text = result.get('main_text', '')

            # Если main_text пустой, пробуем альтернативные ключи
            if not main_text:
                main_text = result.get('mainText', '') or result.get('text', '') or result.get('content', '')

            if not main_text:
                # Логируем весь результат для отладки
                logger.error(f"[AGO_GENERATOR] AI вернул пустой main_text. Полный результат: {result}")
                logger.error(f"[AGO_GENERATOR] Сырой ответ: {raw_response[:1000]}")
                raise RuntimeError("AI не сгенерировал текст документа")

            logger.info(f"[AGO_GENERATOR] Успешно сгенерирован AGO '{ago_type}': {title}, длина текста: {len(main_text)}")

            return {
                'title': title,
                'main_text': main_text
            }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[AGO_GENERATOR] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[AGO_GENERATOR] Ошибка генерации AGO: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации постановления: {str(e)}")


async def generate_custom_resolution_text(
    server_id: str,
    user_description: str,
    context: str = None,
    complaint_id: Optional[int] = None
) -> Dict:
    """
    Генерирует текст кастомного постановления через AI с RAG-фильтрацией законов.

    Args:
        server_id: ID сервера для загрузки законов
        user_description: Описание от пользователя какое постановление нужно
        context: Дополнительный контекст (текст жалобы, данные дела)
        complaint_id: ID привязанного обращения (опционально)

    Returns:
        Dict: {title: str, main_text: str}

    Raises:
        RuntimeError: При ошибке AI или аутентификации
    """
    logger.info(f"[CUSTOM_RESOLUTION] Генерация кастомного постановления для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем контекст для RAG-фильтра
    context_for_rag = f"Кастомное постановление\nОписание: {user_description[:1500]}"
    if context:
        context_for_rag += f"\nКонтекст: {context[:1000]}"

    # Получаем релевантные законы через RAG
    law_parts = await _get_relevant_laws_with_rag(context_for_rag, server_id)

    logger.info(f"[CUSTOM_RESOLUTION] RAG вернул {len(law_parts)} законов")

    # Формируем промпт
    prompt_text = get_custom_resolution_prompt(
        user_description=user_description,
        context=context or ""
    )

    # Собираем контент: промпт + законы как файлы
    contents = [prompt_text]
    if law_parts:
        contents.extend(law_parts)

    logger.info(f"[CUSTOM_RESOLUTION] Отправка запроса в AI, law_parts={len(law_parts)}")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.5,  # Чуть выше для креативности
                max_output_tokens=4096
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Логируем сырой ответ для отладки
        raw_response = response.text if hasattr(response, 'text') else str(response)
        logger.info(f"[CUSTOM_RESOLUTION] Сырой ответ AI (первые 500 символов): {raw_response[:500]}")

        # Парсим JSON из ответа
        result = _parse_json_response(raw_response)

        title = result.get('title', 'Постановление')
        main_text = result.get('main_text', '')

        # Если main_text пустой, пробуем альтернативные ключи
        if not main_text:
            main_text = result.get('mainText', '') or result.get('text', '') or result.get('content', '')

        if not main_text:
            logger.error(f"[CUSTOM_RESOLUTION] AI вернул пустой main_text. Полный результат: {result}")
            logger.error(f"[CUSTOM_RESOLUTION] Сырой ответ: {raw_response[:1000]}")
            raise RuntimeError("AI не сгенерировал текст документа")

        logger.info(f"[CUSTOM_RESOLUTION] Успешно сгенерировано кастомное постановление: {title}, длина текста: {len(main_text)}")

        return {
            'title': title,
            'main_text': main_text
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CUSTOM_RESOLUTION] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CUSTOM_RESOLUTION] Ошибка генерации: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации постановления: {str(e)}")


# === CMS MIGRATION: New AI Functions ===

# === RAG-фильтр законов для CMS ===

async def _get_relevant_laws_for_cms(complaint_text: str, server_id: str) -> str:
    """
    RAG-фильтр законов для CMS API: выбирает релевантные законы по тексту жалобы.

    Алгоритм:
    1. Загружает все законы для server_id из БД (law_documents)
    2. Использует AI-роутер для выбора релевантных законов (3-8 шт)
    3. Возвращает текст выбранных законов

    Args:
        complaint_text: Текст жалобы (заголовок + контент)
        server_id: ID сервера

    Returns:
        Строка с текстами релевантных законов или пустая строка
    """
    from config import ROUTER_MODEL_NAME
    from api.prompts.law_router import get_cms_law_router_prompt

    start_time = asyncio.get_running_loop().time()
    logger.info(f"[CMS_RAG] Запуск RAG-фильтра законов для сервера {server_id}...")

    try:
        # Загружаем все законы из БД
        law_records = await db_manager.fetch(
            """SELECT id, title, content 
               FROM law_documents 
               WHERE server_id = $1 AND content IS NOT NULL 
               ORDER BY title""",
            server_id
        )

        if not law_records:
            logger.warning(f"[CMS_RAG] Нет законов в БД для сервера {server_id}")
            return ""

        # Создаём карту законов по индексу
        law_map_by_index = {
            i + 1: {
                'id': r['id'],
                'title': r['title'],
                'content': r['content']
            }
            for i, r in enumerate(law_records)
        }

        # Формируем список названий для роутера
        law_titles_str = "\n".join([
            f"{i + 1}. {r['title']}"
            for i, r in enumerate(law_records)
        ])

        logger.info(f"[CMS_RAG] Загружено {len(law_records)} законов для сервера {server_id}")

        # Формируем промпт для роутера
        router_prompt = get_cms_law_router_prompt(
            complaint_text=complaint_text[:3000],
            law_titles_str=law_titles_str
        )

        # Функция запроса к AI-роутеру
        async def make_router_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ROUTER_MODEL_NAME,
                contents=router_prompt
            )

        # Выполняем запрос через rate limiter
        response = await execute_vertex_request(
            func=make_router_request,
            model_name=ROUTER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        # Парсим ответ (номера законов)
        selected_indices = set()
        for line in response.text.split('\n'):
            line = line.strip()
            if line.isdigit():
                selected_indices.add(int(line))

        relevant_laws = [
            law_map_by_index[idx]
            for idx in sorted(selected_indices)
            if idx in law_map_by_index
        ]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logger.info(
            f"[CMS_RAG] RAG-фильтр выбрал {len(relevant_laws)} законов за {duration:.2f} мс. "
            f"Законы: {[law['title'] for law in relevant_laws]}"
        )

        if not relevant_laws:
            # Fallback: если роутер не выбрал ничего, берём первые 5 законов
            relevant_laws = [law_map_by_index[i] for i in range(1, min(6, len(law_records) + 1))]
            logger.info(f"[CMS_RAG] Fallback: используем первые {len(relevant_laws)} законов")

        # Формируем текст законов
        laws_text = "\n\n---\n\n".join([
            f"### {law['title']}\n{law['content'][:4000]}"
            for law in relevant_laws
        ])

        return laws_text[:25000]  # Ограничиваем общий размер

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.warning(f"[CMS_RAG] Ошибка RAG-фильтра: {e}. Возврат без законов.")
        return ""
    except Exception as e:
        logger.error(f"[CMS_RAG] Критическая ошибка RAG-фильтра: {e}", exc_info=True)
        return ""


async def analyze_complaint_full_cms(
    server_id: str,
    title: str,
    content: str
) -> Dict:
    """
    Полный анализ жалобы (аналог analyzeComplaintFull из complaintAiService.js).

    Возвращает формат, идентичный OpenAI-версии на сайте:
    - extractedData (с respondents массивом)
    - facts, legalCharacterization, requirements
    - formCheck, summary, recommendations
    - evidence, pipelineFlags

    Использует RAG-фильтр для получения релевантных законов сервера.

    Args:
        server_id: ID сервера
        title: Заголовок жалобы
        content: Текст жалобы

    Returns:
        Dict в формате CMS (идентичный OpenAI-версии)

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.complaint_analysis import (
        get_full_complaint_analysis_prompt,
        COMPLAINT_FULL_ANALYSIS_SCHEMA
    )

    logger.info(f"[CMS_FULL_ANALYSIS] Полный анализ жалобы для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем промпт без законов (RAG отключён)
    prompt = get_full_complaint_analysis_prompt(title, content)
    logger.info(f"[CMS_FULL_ANALYSIS] Используем промпт без законов")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=4000
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Парсим JSON
        result = _parse_json_response(response.text)

        if not result:
            logger.warning("[CMS_FULL_ANALYSIS] AI вернул пустой результат")
            return _get_default_full_analysis()

        # Нормализуем respondents
        respondents = []
        raw_respondents = result.get('extractedData', {}).get('respondents', [])
        if isinstance(raw_respondents, list):
            for r in raw_respondents:
                if r and isinstance(r.get('name'), str):
                    respondents.append({
                        'name': str(r['name']),
                        'position': str(r['position']) if r.get('position') else None,
                        'org': str(r['org']) if r.get('org') else None
                    })

        first = respondents[0] if respondents else {}

        extracted_data = {
            'complainantName': result.get('extractedData', {}).get('complainantName'),
            'respondent': first.get('name'),
            'respondentPosition': first.get('position'),
            'respondentOrg': first.get('org'),
            'respondents': respondents,
            'complexity': result.get('extractedData', {}).get('complexity', 'MEDIUM'),
            'complaintNumber': result.get('extractedData', {}).get('complaintNumber')
        }

        # Нормализуем complexity
        if extracted_data['complexity'] not in ('LOW', 'MEDIUM', 'HIGH'):
            extracted_data['complexity'] = 'MEDIUM'

        # Нормализуем facts и legalCharacterization
        facts = [str(f) for f in result.get('facts', []) if f]
        legal_chars = result.get('legalCharacterization', [])
        if not isinstance(legal_chars, list):
            legal_chars = []
        legal_chars = [str(c) if c else '' for c in legal_chars]

        # Выравниваем длину
        while len(legal_chars) < len(facts):
            legal_chars.append('')
        legal_chars = legal_chars[:len(facts)]

        # Нормализуем requirements
        requirements = [str(r) for r in result.get('requirements', []) if r]

        # Нормализуем formCheck
        form_check_raw = result.get('formCheck', {})
        form_check = {
            'hasSignature': bool(form_check_raw.get('hasSignature')),
            'hasEmail': bool(form_check_raw.get('hasEmail')),
            'hasPassportCopy': bool(form_check_raw.get('hasPassportCopy')),
            'isValid': bool(form_check_raw.get('isValid'))
        }

        # Нормализуем recommendations
        recommendations = []
        for rec in result.get('recommendations', []):
            if rec and (rec.get('title') or rec.get('description')):
                recommendations.append({
                    'type': rec.get('type', 'info'),
                    'title': rec.get('title', ''),
                    'description': rec.get('description', '')
                })

        # Нормализуем evidence
        evidence = []
        for ev in result.get('evidence', []):
            if ev and isinstance(ev.get('url'), str) and ev['url'].strip():
                evidence.append({
                    'url': ev['url'].strip(),
                    'typeLabel': str(ev.get('typeLabel', 'Ссылка')).strip() or 'Ссылка'
                })

        # Нормализуем pipelineFlags
        flags_raw = result.get('pipelineFlags', {})
        pipeline_flags = {
            'requiresDocs': bool(flags_raw.get('requiresDocs')),
            'requiresInterrogation': bool(flags_raw.get('requiresInterrogation')),
            'requiresCourt': bool(flags_raw.get('requiresCourt'))
        }

        logger.info(f"[CMS_FULL_ANALYSIS] Успешно: {len(facts)} фактов, {len(respondents)} ответчиков")

        return {
            'extractedData': extracted_data,
            'facts': facts,
            'legalCharacterization': legal_chars,
            'requirements': requirements,
            'formCheck': form_check,
            'summary': result.get('summary', ''),
            'recommendations': recommendations,
            'evidence': evidence,
            'pipelineFlags': pipeline_flags
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FULL_ANALYSIS] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FULL_ANALYSIS] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка анализа: {str(e)}")


def _get_default_full_analysis() -> Dict:
    """Возвращает дефолтный результат полного анализа."""
    return {
        'extractedData': {
            'complainantName': None,
            'respondent': None,
            'respondentPosition': None,
            'respondentOrg': None,
            'respondents': [],
            'complexity': 'MEDIUM',
            'complaintNumber': None
        },
        'facts': [],
        'legalCharacterization': [],
        'requirements': [],
        'formCheck': {
            'hasSignature': False,
            'hasEmail': False,
            'hasPassportCopy': False,
            'isValid': False
        },
        'summary': None,
        'recommendations': [],
        'evidence': [],
        'pipelineFlags': {
            'requiresDocs': False,
            'requiresInterrogation': False,
            'requiresCourt': False
        }
    }


async def analyze_complaint_combined_cms(
    server_id: str,
    title: str,
    content: str
) -> Dict:
    """
    Комбинированный анализ жалобы: fullAnalysis + issueTags за 2 запроса.

    Оптимизация: вместо 4 запросов (2 RAG + 2 анализа) делаем 2:
    1. RAG-роутер — выбирает законы для обеих задач
    2. Комбинированный анализ — возвращает fullAnalysis + issueTags

    Args:
        server_id: ID сервера
        title: Заголовок жалобы
        content: Текст жалобы

    Returns:
        Dict: {
            "fullAnalysis": { ... результат analyzeComplaintFull ... },
            "issueTags": ["тег1", "тег2", ...],
            "rag_laws_count": int
        }

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.complaint_analysis import get_combined_analysis_prompt_with_laws

    logger.info(f"[CMS_COMBINED] Комбинированный анализ для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Комбинированный анализ без законов (RAG отключён)
    prompt = get_combined_analysis_prompt_with_laws(title, content, "")
    rag_laws_count = 0  # RAG отключён
    logger.info(f"[CMS_COMBINED] Запускаем комбинированный анализ без законов")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=5000
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        # Парсим JSON
        result = _parse_json_response(response.text)

        if not result:
            logger.warning("[CMS_COMBINED] AI вернул пустой результат")
            return {
                "fullAnalysis": _get_default_full_analysis(),
                "issueTags": [],
                "rag_laws_count": rag_laws_count
            }

        # Проверка типа result
        if not isinstance(result, dict):
            logger.warning(f"[CMS_COMBINED] AI вернул не словарь, а {type(result).__name__}: {str(result)[:200]}")
            return {
                "fullAnalysis": _get_default_full_analysis(),
                "issueTags": [],
                "rag_laws_count": rag_laws_count
            }

        # Извлекаем analysis
        analysis_raw = result.get('analysis', {})

        # Проверка типа analysis_raw
        if not isinstance(analysis_raw, dict):
            logger.warning(f"[CMS_COMBINED] analysis не словарь, а {type(analysis_raw).__name__}")
            analysis_raw = {}

        # Нормализуем respondents
        respondents = []
        raw_respondents = analysis_raw.get('extractedData', {}).get('respondents', [])
        if isinstance(raw_respondents, list):
            for r in raw_respondents:
                if r and isinstance(r.get('name'), str):
                    respondents.append({
                        'name': str(r['name']),
                        'position': str(r['position']) if r.get('position') else None,
                        'org': str(r['org']) if r.get('org') else None
                    })

        first = respondents[0] if respondents else {}

        extracted_data = {
            'complainantName': analysis_raw.get('extractedData', {}).get('complainantName'),
            'respondent': first.get('name'),
            'respondentPosition': first.get('position'),
            'respondentOrg': first.get('org'),
            'respondents': respondents,
            'complexity': analysis_raw.get('extractedData', {}).get('complexity', 'MEDIUM'),
            'complaintNumber': analysis_raw.get('extractedData', {}).get('complaintNumber')
        }

        if extracted_data['complexity'] not in ('LOW', 'MEDIUM', 'HIGH'):
            extracted_data['complexity'] = 'MEDIUM'

        # Нормализуем facts и legalCharacterization
        facts = [str(f) for f in analysis_raw.get('facts', []) if f]
        legal_chars = analysis_raw.get('legalCharacterization', [])
        if not isinstance(legal_chars, list):
            legal_chars = []
        legal_chars = [str(c) if c else '' for c in legal_chars]

        while len(legal_chars) < len(facts):
            legal_chars.append('')
        legal_chars = legal_chars[:len(facts)]

        requirements = [str(r) for r in analysis_raw.get('requirements', []) if r]

        form_check_raw = analysis_raw.get('formCheck', {})
        if not isinstance(form_check_raw, dict):
            form_check_raw = {}
        form_check = {
            'hasSignature': bool(form_check_raw.get('hasSignature')),
            'hasEmail': bool(form_check_raw.get('hasEmail')),
            'hasPassportCopy': bool(form_check_raw.get('hasPassportCopy')),
            'isValid': bool(form_check_raw.get('isValid'))
        }

        recommendations = []
        for rec in analysis_raw.get('recommendations', []):
            # AI может вернуть строку вместо объекта
            if isinstance(rec, str) and rec.strip():
                recommendations.append({
                    'type': 'info',
                    'title': rec.strip(),
                    'description': ''
                })
            elif isinstance(rec, dict) and (rec.get('title') or rec.get('description')):
                recommendations.append({
                    'type': rec.get('type', 'info'),
                    'title': rec.get('title', ''),
                    'description': rec.get('description', '')
                })

        evidence = []
        for ev in analysis_raw.get('evidence', []):
            # AI может вернуть строку URL вместо объекта
            if isinstance(ev, str) and ev.strip():
                evidence.append({
                    'url': ev.strip(),
                    'typeLabel': 'Ссылка'
                })
            elif isinstance(ev, dict) and isinstance(ev.get('url'), str) and ev['url'].strip():
                evidence.append({
                    'url': ev['url'].strip(),
                    'typeLabel': str(ev.get('typeLabel', 'Ссылка')).strip() or 'Ссылка'
                })

        flags_raw = analysis_raw.get('pipelineFlags', {})
        if not isinstance(flags_raw, dict):
            flags_raw = {}
        pipeline_flags = {
            'requiresDocs': bool(flags_raw.get('requiresDocs')),
            'requiresInterrogation': bool(flags_raw.get('requiresInterrogation')),
            'requiresCourt': bool(flags_raw.get('requiresCourt'))
        }

        full_analysis = {
            'extractedData': extracted_data,
            'facts': facts,
            'legalCharacterization': legal_chars,
            'requirements': requirements,
            'formCheck': form_check,
            'summary': analysis_raw.get('summary', ''),
            'recommendations': recommendations,
            'evidence': evidence,
            'pipelineFlags': pipeline_flags
        }

        # Извлекаем issueTags
        issue_tags = []
        raw_tags = result.get('issueTags', [])
        if isinstance(raw_tags, list):
            seen = set()
            for t in raw_tags:
                if isinstance(t, str) and t.strip():
                    key = t.lower().strip()
                    if key not in seen:
                        seen.add(key)
                        issue_tags.append(t.strip())

        logger.info(f"[CMS_COMBINED] Успешно: {len(facts)} фактов, {len(respondents)} ответчиков, {len(issue_tags)} тегов")

        return {
            "fullAnalysis": full_analysis,
            "issueTags": issue_tags,
            "rag_laws_count": rag_laws_count
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_COMBINED] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_COMBINED] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка комбинированного анализа: {str(e)}")


async def get_issue_spotting_tags_cms(facts: list, server_id: str = None) -> Dict:
    """
    Получение юридических тегов по фактам (аналог getIssueSpottingTags из complaintAiService.js).

    Если передан server_id, использует RAG-фильтр для получения законов
    и формирует теги с учётом терминологии законов.

    Args:
        facts: Список фактов
        server_id: ID сервера (опционально, для RAG)

    Returns:
        Dict: {tags: List[str]}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.issue_spotting import (
        get_issue_spotting_prompt,
        get_issue_spotting_prompt_with_laws,
        ISSUE_SPOTTING_SCHEMA
    )

    logger.info(f"[CMS_ISSUE_SPOTTING] Получение тегов для {len(facts)} фактов, server_id={server_id}")

    if not facts:
        return {'tags': []}

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Получаем законы если передан server_id
    laws_text = ""
    if server_id:
        facts_text = "\n".join(facts[:10])  # Берём первые 10 фактов для контекста
        laws_text = await _get_relevant_laws_for_cms(facts_text, server_id)

    # Формируем промпт
    if laws_text:
        prompt = get_issue_spotting_prompt_with_laws(facts, laws_text)
        logger.info(f"[CMS_ISSUE_SPOTTING] Используем промпт с законами")
    else:
        prompt = get_issue_spotting_prompt(facts)
        logger.info(f"[CMS_ISSUE_SPOTTING] Используем промпт без законов")

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1500
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        tags = []
        raw_tags = result.get('tags', [])
        if isinstance(raw_tags, list):
            seen = set()
            for t in raw_tags:
                if isinstance(t, str) and t.strip():
                    key = t.lower().strip()
                    if key not in seen:
                        seen.add(key)
                        tags.append(t.strip())

        logger.info(f"[CMS_ISSUE_SPOTTING] Получено {len(tags)} тегов")
        return {'tags': tags}

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_ISSUE_SPOTTING] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_ISSUE_SPOTTING] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка получения тегов: {str(e)}")


async def get_evidence_type_labels_cms(text: str, urls: list) -> Dict:
    """
    Классификация типов ссылок на доказательства (аналог getEvidenceTypeLabels из complaintAiService.js).

    Args:
        text: Текст обращения
        urls: Список URL для классификации

    Returns:
        Dict: {labels: List[{url, typeLabel}]}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.evidence_labels import get_evidence_labels_prompt

    logger.info(f"[CMS_EVIDENCE_LABELS] Классификация {len(urls)} ссылок")

    if not urls:
        return {'labels': []}

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_evidence_labels_prompt(text, urls)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1500
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        labels = []
        # Поддерживаем разные форматы ответа
        raw_labels = result.get('labels', []) or result.get('evidence', []) or result.get('items', [])
        if isinstance(raw_labels, list):
            for item in raw_labels:
                if item and (item.get('url') or item.get('externalUrl')):
                    labels.append({
                        'url': item.get('url') or item.get('externalUrl'),
                        'typeLabel': item.get('typeLabel') or item.get('title') or 'Ссылка'
                    })

        logger.info(f"[CMS_EVIDENCE_LABELS] Классифицировано {len(labels)} ссылок")
        return {'labels': labels}

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_EVIDENCE_LABELS] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_EVIDENCE_LABELS] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка классификации: {str(e)}")


async def validate_form_ai_cms(title: str, content: str) -> Dict:
    """
    AI-валидация формы обращения (аналог validateFormAI из formValidatorService.js).

    Args:
        title: Заголовок обращения
        content: Текст обращения

    Returns:
        Dict в формате CMS (идентичный OpenAI-версии)

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.form_validation import get_form_validation_ai_prompt, FORM_VALIDATION_SCHEMA

    logger.info(f"[CMS_FORM_VALIDATION] AI-валидация формы")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_form_validation_ai_prompt(title, content)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1500
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        # Нормализуем ответ
        is_valid = bool(result.get('isValid', False))
        confidence = int(result.get('confidenceScore', 0))
        if confidence < 0:
            confidence = 0
        if confidence > 100:
            confidence = 100

        form_quality = result.get('formQuality', 'INVALID')
        if form_quality not in ('EXCELLENT', 'GOOD', 'ACCEPTABLE', 'POOR', 'INVALID'):
            form_quality = 'INVALID'

        # Нормализуем issues
        issues = []
        for issue in result.get('issues', []):
            if issue:
                issues.append({
                    'severity': issue.get('severity', 'INFO'),
                    'field': issue.get('field', ''),
                    'issue': issue.get('issue', ''),
                    'suggestion': issue.get('suggestion', '')
                })

        # Нормализуем extractedData
        extracted_raw = result.get('extractedData', {})
        extracted_data = {
            'complainant': extracted_raw.get('complainant'),
            'defendant': extracted_raw.get('defendant'),
            'organization': extracted_raw.get('organization'),
            'incidentDate': extracted_raw.get('incidentDate'),
            'incidentTime': extracted_raw.get('incidentTime')
        }

        recommendation = result.get('recommendation', 'REJECT')
        if recommendation not in ('ACCEPT', 'ACCEPT_WITH_NOTICE', 'REQUEST_CORRECTION', 'REJECT'):
            recommendation = 'REJECT'

        logger.info(f"[CMS_FORM_VALIDATION] Результат: valid={is_valid}, quality={form_quality}")

        return {
            'isValid': is_valid,
            'confidenceScore': confidence,
            'formQuality': form_quality,
            'issues': issues,
            'missingInfo': result.get('missingInfo', []),
            'contradictions': result.get('contradictions', []),
            'extractedData': extracted_data,
            'recommendation': recommendation,
            'summary': result.get('summary', '')
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FORM_VALIDATION] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FORM_VALIDATION] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка валидации: {str(e)}")


async def generate_case_file_draft_cms(
    complaint_title: str,
    complaint_content: str,
    facts_with_verdict: list,
    law_sections: list,
    requirements: list
) -> Dict:
    """
    Генерация черновика кейс-файла (аналог generateCaseFileDraft из caseFileDraftService.js).

    Args:
        complaint_title: Заголовок жалобы
        complaint_content: Текст жалобы
        facts_with_verdict: Список фактов с пометкой прокурора
        law_sections: Тексты релевантных статей законов
        requirements: Требования заявителя

    Returns:
        Dict: {draft: {...}, sections_count: int}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.case_file_draft import get_case_file_draft_prompt, REQUIRED_KEYS

    logger.info(f"[CMS_CASE_FILE_DRAFT] Генерация черновика")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Формируем текст законов
    law_sections_text = "\n\n---\n\n".join(law_sections) if law_sections else ""
    requirements_text = "; ".join(requirements) if requirements else ""

    prompt = get_case_file_draft_prompt(
        complaint_title=complaint_title,
        complaint_content=complaint_content,
        facts_with_verdict=facts_with_verdict,
        law_sections_text=law_sections_text,
        requirements_text=requirements_text
    )

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=4000
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        # Валидируем обязательные поля
        draft = {}
        for key in REQUIRED_KEYS:
            value = result.get(key, '')
            if not isinstance(value, str):
                value = str(value) if value else ''
            draft[key] = value.strip()

        # Проверяем что все поля заполнены
        empty_fields = [k for k in REQUIRED_KEYS if not draft[k]]
        if empty_fields:
            logger.warning(f"[CMS_CASE_FILE_DRAFT] Пустые поля: {empty_fields}")

        logger.info(f"[CMS_CASE_FILE_DRAFT] Успешно сгенерирован черновик")

        return {
            'draft': draft,
            'sections_count': len(law_sections)
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации: {str(e)}")


async def generate_case_file_draft_with_rag_cms(
    server_id: str,
    complaint_title: str,
    complaint_content: str,
    facts_with_verdict: list,
    requirements: list,
    pipeline_context: str = None
) -> Dict:
    """
    Генерация черновика кейс-файла с умным RAG-фильтром.

    Законы выбираются AI-роутером и передаются как GCS Files
    в контекст генерации (AI видит их полные тексты).

    Args:
        server_id: ID сервера для загрузки законов
        complaint_title: Заголовок жалобы
        complaint_content: Текст жалобы
        facts_with_verdict: Список фактов с пометкой прокурора
        requirements: Требования заявителя
        pipeline_context: Контекст из пайплайна (комментарии, допросы, решения прокурора)

    Returns:
        Dict: {draft: {...}, sections_count: int, selected_laws: [...]}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.case_file_draft import get_case_file_draft_prompt_for_rag, REQUIRED_KEYS

    logger.info(f"[CMS_CASE_FILE_DRAFT_RAG] Генерация с RAG для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # 1. Формируем контекст для RAG-фильтра
    facts_summary = ', '.join([f.get('text', '')[:100] for f in facts_with_verdict[:5]])
    context_for_rag = f"""Тип документа: Кейс-файл (дело о привлечении к ответственности)
Заголовок жалобы: {complaint_title}
Содержание: {complaint_content[:3000]}
Факты: {facts_summary}"""

    # 2. AI выбирает релевантные законы (возвращает List[types.Part] + названия)
    law_parts, selected_law_titles = await _get_relevant_laws_with_rag_extended(context_for_rag, server_id)

    logger.info(f"[CMS_CASE_FILE_DRAFT_RAG] RAG выбрал {len(law_parts)} законов: {selected_law_titles}")

    # 3. Формируем промпт (без текстов законов — они будут как Part)
    requirements_text = "; ".join(requirements) if requirements else ""
    prompt = get_case_file_draft_prompt_for_rag(
        complaint_title=complaint_title,
        complaint_content=complaint_content,
        facts_with_verdict=facts_with_verdict,
        requirements_text=requirements_text,
        pipeline_context=pipeline_context
    )

    # 4. Собираем контент: промпт + законы как файлы
    contents = [prompt]
    if law_parts:
        contents.extend(law_parts)

    logger.info(f"[CMS_CASE_FILE_DRAFT_RAG] Отправка запроса в AI, law_parts={len(law_parts)}")

    # 5. Генерируем черновик (AI видит и промпт, и тексты законов)
    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=4000
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        # Валидируем обязательные поля
        draft = {}
        for key in REQUIRED_KEYS:
            value = result.get(key, '')
            if not isinstance(value, str):
                value = str(value) if value else ''
            draft[key] = value.strip()

        # Проверяем что все поля заполнены
        empty_fields = [k for k in REQUIRED_KEYS if not draft[k]]
        if empty_fields:
            logger.warning(f"[CMS_CASE_FILE_DRAFT_RAG] Пустые поля: {empty_fields}")

        logger.info(f"[CMS_CASE_FILE_DRAFT_RAG] Успешно сгенерирован черновик с {len(law_parts)} законами")

        return {
            'draft': draft,
            'sections_count': len(law_parts),
            'selected_laws': selected_law_titles
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT_RAG] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT_RAG] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации: {str(e)}")


async def generate_case_file_draft_with_feedback_cms(
    server_id: str,
    complaint_title: str,
    complaint_content: str,
    facts_with_verdict: list,
    requirements: list,
    previous_draft: dict,
    prosecutor_feedback: str,
    pipeline_context: str = None
) -> Dict:
    """
    Перегенерация черновика кейс-файла с учётом замечаний прокурора.

    Использует RAG для выбора законов + промпт с предыдущим черновиком и замечаниями.

    Args:
        server_id: ID сервера для загрузки законов
        complaint_title: Заголовок жалобы
        complaint_content: Текст жалобы
        facts_with_verdict: Список фактов с пометкой прокурора
        requirements: Требования заявителя
        previous_draft: Предыдущий черновик (dict с 7 полями)
        prosecutor_feedback: Текст замечаний прокурора
        pipeline_context: Контекст из пайплайна (комментарии, допросы, решения)

    Returns:
        Dict: {draft: {...}, sections_count: int, selected_laws: [...]}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.case_file_draft import get_case_file_draft_feedback_prompt, REQUIRED_KEYS

    logger.info(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Перегенерация с замечаниями для сервера {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # 1. Формируем контекст для RAG-фильтра
    facts_summary = ', '.join([f.get('text', '')[:100] for f in facts_with_verdict[:5]])
    context_for_rag = f"""Тип документа: Кейс-файл (дело о привлечении к ответственности)
Заголовок жалобы: {complaint_title}
Содержание: {complaint_content[:3000]}
Факты: {facts_summary}"""

    # 2. AI выбирает релевантные законы (возвращает List[types.Part] + названия)
    law_parts, selected_law_titles = await _get_relevant_laws_with_rag_extended(context_for_rag, server_id)

    logger.info(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] RAG выбрал {len(law_parts)} законов: {selected_law_titles}")

    # 3. Формируем промпт с предыдущим черновиком и замечаниями
    requirements_text = "; ".join(requirements) if requirements else ""
    prompt = get_case_file_draft_feedback_prompt(
        complaint_title=complaint_title,
        complaint_content=complaint_content,
        facts_with_verdict=facts_with_verdict,
        requirements_text=requirements_text,
        previous_draft=previous_draft,
        prosecutor_feedback=prosecutor_feedback,
        pipeline_context=pipeline_context
    )

    # 4. Собираем контент: промпт + законы как файлы
    contents = [prompt]
    if law_parts:
        contents.extend(law_parts)

    logger.info(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Отправка запроса в AI, law_parts={len(law_parts)}")

    # 5. Генерируем черновик
    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=4000
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        # Валидируем обязательные поля
        draft = {}
        for key in REQUIRED_KEYS:
            value = result.get(key, '')
            if not isinstance(value, str):
                value = str(value) if value else ''
            draft[key] = value.strip()

        # Проверяем что все поля заполнены
        empty_fields = [k for k in REQUIRED_KEYS if not draft[k]]
        if empty_fields:
            logger.warning(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Пустые поля: {empty_fields}")

        logger.info(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Успешно перегенерирован черновик с {len(law_parts)} законами")

        return {
            'draft': draft,
            'sections_count': len(law_parts),
            'selected_laws': selected_law_titles
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_CASE_FILE_DRAFT_FEEDBACK] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации: {str(e)}")


async def generate_plan_ai_cms(context: dict) -> Dict:
    """
    Генерация плана работы (аналог generatePlanAI из adaptivePlanService.js).

    Args:
        context: Контекст жалобы (complaint, analysis, defendantHistory)

    Returns:
        Dict с планом работы

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.adaptive_plan import get_generate_plan_prompt

    logger.info(f"[CMS_GENERATE_PLAN] Генерация плана работы")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_generate_plan_prompt(context)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=2000
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        # Валидируем и нормализуем результат
        plan_type = result.get('planType', 'STANDARD')
        if plan_type not in ('STANDARD', 'WITH_DOCS_REQUEST', 'REJECTION', 'INTERROGATION', 'COMPLEX'):
            plan_type = 'STANDARD'

        complexity = result.get('complexity', 'MEDIUM')
        if complexity not in ('LOW', 'MEDIUM', 'HIGH'):
            complexity = 'MEDIUM'

        logger.info(f"[CMS_GENERATE_PLAN] План создан: type={plan_type}, complexity={complexity}")

        return {
            'planType': plan_type,
            'estimatedDuration': result.get('estimatedDuration', '3-5 дней'),
            'complexity': complexity,
            'phases': result.get('phases', []),
            'decisionPoints': result.get('decisionPoints', []),
            'riskFactors': result.get('riskFactors', []),
            'recommendations': result.get('recommendations', [])
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_GENERATE_PLAN] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_GENERATE_PLAN] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации плана: {str(e)}")


async def adapt_plan_ai_cms(
    current_plan: dict,
    new_information: dict,
    completed_steps: list
) -> Dict:
    """
    Адаптация плана работы (аналог adaptPlanAI из adaptivePlanService.js).

    Args:
        current_plan: Текущий план
        new_information: Новая информация
        completed_steps: Список номеров выполненных шагов

    Returns:
        Dict с результатом адаптации

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.adaptive_plan import get_adapt_plan_prompt

    logger.info(f"[CMS_ADAPT_PLAN] Адаптация плана, выполнено шагов: {len(completed_steps)}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_adapt_plan_prompt(current_plan, new_information, completed_steps)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=1500
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        needs_adaptation = bool(result.get('needsAdaptation', False))

        logger.info(f"[CMS_ADAPT_PLAN] Результат: needsAdaptation={needs_adaptation}")

        return {
            'needsAdaptation': needs_adaptation,
            'reason': result.get('reason', ''),
            'changes': result.get('changes', []),
            'newEstimatedDuration': result.get('newEstimatedDuration'),
            'warnings': result.get('warnings', [])
        }

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_ADAPT_PLAN] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_ADAPT_PLAN] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка адаптации плана: {str(e)}")


async def extract_passport_data_cms(image_url: str) -> Dict:
    """
    OCR паспорта (аналог extractPassportData из citizenService.js).

    Args:
        image_url: URL изображения паспорта

    Returns:
        Dict с данными паспорта

    Raises:
        RuntimeError: При ошибке AI или недоступности Vision
    """
    import httpx
    import tempfile
    import os
    from pathlib import Path
    from api.prompts.passport_ocr import get_passport_ocr_prompt

    logger.info(f"[CMS_PASSPORT_OCR] Распознавание паспорта: {image_url[:100]}...")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt_text = get_passport_ocr_prompt()

    # Скачиваем изображение
    async def download_image(url: str) -> tuple:
        """Скачивает изображение и возвращает (bytes, mime_type)"""
        # Определяем Referer на основе URL
        referer_url = 'https://forum.gta5rp.com/'
        if 'majestic-rp.ru' in url:
            referer_url = 'https://forum.majestic-rp.ru/'
        elif 'rmrp.ru' in url:
            referer_url = 'https://forum.rmrp.ru/'
        elif 'russia.online' in url:
            referer_url = 'https://forum.russia.online/'

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': referer_url,
        }

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=30.0,
                headers=headers
            ) as client:
                response = await client.get(url)
                response.raise_for_status()

                # Определяем mime-type из Content-Type или URL
                content_type = response.headers.get('content-type', '').lower()
                if 'jpeg' in content_type or 'jpg' in content_type:
                    mime_type = 'image/jpeg'
                elif 'png' in content_type:
                    mime_type = 'image/png'
                elif 'gif' in content_type:
                    mime_type = 'image/gif'
                elif 'webp' in content_type:
                    mime_type = 'image/webp'
                else:
                    # Определяем по расширению URL
                    url_lower = url.lower().split('?')[0]
                    if url_lower.endswith('.png'):
                        mime_type = 'image/png'
                    elif url_lower.endswith('.gif'):
                        mime_type = 'image/gif'
                    elif url_lower.endswith('.webp'):
                        mime_type = 'image/webp'
                    else:
                        mime_type = 'image/jpeg'  # По умолчанию

                logger.info(f"[CMS_PASSPORT_OCR] Изображение скачано: {len(response.content)} байт, mime: {mime_type}")
                return response.content, mime_type

        except httpx.HTTPStatusError as e:
            logger.error(f"[CMS_PASSPORT_OCR] HTTP ошибка {e.response.status_code} при скачивании {url}")
            raise RuntimeError(f"Не удалось скачать изображение: HTTP {e.response.status_code}")
        except httpx.TimeoutException:
            logger.error(f"[CMS_PASSPORT_OCR] Таймаут при скачивании {url}")
            raise RuntimeError("Таймаут при скачивании изображения")
        except Exception as e:
            logger.error(f"[CMS_PASSPORT_OCR] Ошибка при скачивании {url}: {e}")
            raise RuntimeError(f"Ошибка при скачивании изображения: {str(e)}")

    # Скачиваем изображение
    logger.info(f"[CMS_PASSPORT_OCR] Начинаем скачивание: {image_url}")
    try:
        image_data, mime_type = await download_image(image_url)
        logger.info(f"[CMS_PASSPORT_OCR] Скачано успешно: {len(image_data)} байт, mime: {mime_type}")
    except RuntimeError as e:
        logger.error(f"[CMS_PASSPORT_OCR] Ошибка скачивания: {e}")
        raise e

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")

        # Формируем контент с изображением как inline_data (не from_uri!)
        image_part = types.Part(inline_data=types.Blob(
            data=image_data,
            mime_type=mime_type
        ))

        contents = [
            types.Part.from_text(text=prompt_text),
            image_part
        ]

        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=500
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        # Нормализуем dateOfBirth
        dob = result.get('dateOfBirth')
        if dob and isinstance(dob, str):
            import re
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', dob):
                try:
                    from datetime import datetime
                    parsed = datetime.fromisoformat(dob.replace('/', '-'))
                    dob = parsed.strftime('%Y-%m-%d')
                except:
                    pass
        result['dateOfBirth'] = dob

        confidence = int(result.get('confidence', 0))
        if confidence < 0:
            confidence = 0
        if confidence > 100:
            confidence = 100
        result['confidence'] = confidence

        result['isValid'] = bool(result.get('isValid', False))
        result['readabilityIssues'] = result.get('readabilityIssues', [])

        logger.info(f"[CMS_PASSPORT_OCR] Успешно: {result.get('fullName')}, confidence={confidence}")

        return result

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_PASSPORT_OCR] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_PASSPORT_OCR] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка распознавания: {str(e)}")


async def resolve_passport_url_cms(content: str, urls: list) -> Dict:
    """
    Определение ссылки на паспорт среди нескольких URL (аналог resolvePassportUrlFromCandidates).

    Args:
        content: Текст обращения
        urls: Список URL-ссылок (минимум 2)

    Returns:
        Dict: {passportUrl: str | None}

    Raises:
        RuntimeError: При ошибке AI
    """
    from api.prompts.passport_resolver import get_passport_resolver_prompt

    logger.info(f"[CMS_PASSPORT_RESOLVER] Определение паспорта среди {len(urls)} ссылок")

    if not urls or len(urls) < 2:
        return {'passportUrl': None}

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    prompt = get_passport_resolver_prompt(content, urls)

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=500
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        result = _parse_json_response(response.text)

        passport_url = result.get('passportUrl')

        # Проверяем что URL из списка
        if passport_url and isinstance(passport_url, str):
            passport_url = passport_url.strip()
            if passport_url not in urls:
                # Попробуем найти похожий
                for u in urls:
                    if passport_url in u or u in passport_url:
                        passport_url = u
                        break
                else:
                    passport_url = None

        logger.info(f"[CMS_PASSPORT_RESOLVER] Результат: {passport_url}")

        return {'passportUrl': passport_url}

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_PASSPORT_RESOLVER] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_PASSPORT_RESOLVER] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка определения: {str(e)}")


async def generate_free_form_response_cms(
    server_id: str,
    complaint_number: str,
    complainant: str,
    complaint_summary: str,
    prosecutor_instructions: str,
    use_laws_rag: bool,
    attorney: str,
    rank: str,
    regenerate_feedback: Optional[str] = None
) -> Dict:
    """
    Генерация ответа в свободной форме (option_5) через AI.

    AI создаёт официальный ответ на русском языке по инструкциям прокурора.

    Args:
        server_id: ID сервера для RAG законов
        complaint_number: Номер обращения
        complainant: ФИО заявителя
        complaint_summary: Краткое содержание жалобы
        prosecutor_instructions: Инструкции прокурора (что ответить)
        use_laws_rag: Использовать RAG законов для генерации
        attorney: ФИО прокурора
        rank: Должность прокурора
        regenerate_feedback: Что исправить при перегенерации (опционально)

    Returns:
        Dict: {main_text: str}

    Raises:
        RuntimeError: При ошибке AI
    """
    logger.info(f"[CMS_FREE_FORM] Генерация свободной формы: complaint={complaint_number}, use_rag={use_laws_rag}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # RAG законов пока не реализован в свободной форме - укажем это в промпте
    law_context = ""
    if use_laws_rag:
        law_context = "\n\n[AI: Прокурор запросил использование законодательной базы. Постарайся ссылаться на релевантные статьи УК, АК, ЭК штата Сан-Андреас.]"
        logger.info(f"[CMS_FREE_FORM] RAG законов запрошен (будет использован контекст AI)")

    # Формируем промпт
    prompt = _build_free_form_prompt(
        complaint_number=complaint_number,
        complainant=complainant,
        complaint_summary=complaint_summary,
        prosecutor_instructions=prosecutor_instructions,
        attorney=attorney,
        rank=rank,
        law_context=law_context,
        regenerate_feedback=regenerate_feedback
    )

    async def make_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=COMPLAINT_ANALYZER_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.7,
                max_output_tokens=2000
            )
        )

    try:
        response = await execute_vertex_request(
            func=make_request,
            model_name=COMPLAINT_ANALYZER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=2
        )
        await mark_current_region_success()

        main_text = response.text.strip()

        # Убираем markdown если есть
        if main_text.startswith("```"):
            lines = main_text.split('\n')
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            main_text = '\n'.join(lines).strip()

        logger.info(f"[CMS_FREE_FORM] Успешно сгенерировано: {len(main_text)} символов")

        return {'main_text': main_text}

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FREE_FORM] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")
    except Exception as e:
        await mark_current_region_error()
        logger.error(f"[CMS_FREE_FORM] Ошибка: {e}", exc_info=True)
        raise RuntimeError(f"Ошибка генерации: {str(e)}")


def _build_free_form_prompt(
    complaint_number: str,
    complainant: str,
    complaint_summary: str,
    prosecutor_instructions: str,
    attorney: str,
    rank: str,
    law_context: str = "",
    regenerate_feedback: Optional[str] = None
) -> str:
    """
    Формирует промпт для генерации свободной формы ответа.
    """
    rank_mapping = {
        'Прокурор': 'Прокурором',
        'Заместитель Генерального Прокурора': 'Заместителем Генерального Прокурора',
        'Генеральный Прокурор': 'Генеральным Прокурором'
    }
    rank_inst = rank_mapping.get(rank, rank)

    # Get first name safely
    first_name = 'гражданин'
    if complainant and complainant.strip():
        parts = complainant.strip().split()
        if parts:
            first_name = parts[0]

    base_prompt = f"""Ты — опытный прокурор Офиса Генерального Прокурора штата Сан-Андреас.

Твоя задача: написать официальный ответ на обращение гражданина в свободной форме.

=== ДАННЫЕ ОБРАЩЕНИЯ ===
Номер обращения: №{complaint_number}
Заявитель: {complainant}
Суть обращения: {complaint_summary}

=== ДАННЫЕ ПРОКУРОРА ===
ФИО: {attorney}
Должность: {rank}
{law_context}

=== ИНСТРУКЦИИ ПРОКУРОРА ===
{prosecutor_instructions}

=== ТРЕБОВАНИЯ К ОТВЕТУ ===
1. Ответ должен быть на русском языке
2. Официально-деловой стиль
3. Структура:
   - Обращение к заявителю ("Уважаемый/ая {first_name}")
   - Указание что обращение №{complaint_number} рассмотрено {rank_inst} {attorney}
   - Суть ответа согласно инструкциям прокурора
   - Информация о праве на обжалование (если уместно)
   - Подпись: "С уважением, {rank} {attorney}"

4. НЕ добавляй шапку документа (дату, адрес) — это добавится автоматически
5. Ответ должен быть конкретным, без воды
6. Используй законы и статьи если они предоставлены в контексте
7. Ответ НЕ ДОЛЖЕН превышать 600 символов. Если содержание требует больше — сократи, сохранив ключевую информацию. Это ограничение размера документа.

Напиши только текст ответа, без дополнительных комментариев."""

    if regenerate_feedback:
        base_prompt += f"""

=== ИСПРАВЛЕНИЯ (ПЕРЕГЕНЕРАЦИЯ) ===
Предыдущая версия ответа не удовлетворила прокурора. 
Учти следующие замечания при генерации нового ответа:
{regenerate_feedback}"""

    return base_prompt


# END OF FILE services/complaint_analyzer_service.py
