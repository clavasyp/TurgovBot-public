# START OF FILE services/complaint_service.py

"""
Сервис генерации жалоб/исков (пользовательский «составить документ»).

Извлечённая бизнес-логика из user_handlers/complaint_flow.py::process_and_send_complaint
для веб-API (/v1/web/complaint). Чтобы не дублировать и не расходиться с живым ботом,
переиспалует context-free хелперы бота через ЛЕНИВЫЙ импорт
(parse_forum_form / get_relevant_laws_for_complaint / check_lawsuit_legality) —
модульный импорт user_handlers.* замыкает цикл telegram↔api (см. web_auth.py).

Отличия от бота: Telegram-санитизация HTML НЕ применяется — веб получает полноценный
HTML (для отображения) + BB-код (для вставки на форум).
"""

import logging
from typing import Dict

from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions

from database.manager import db_manager
from services import case_law_context
from config import COMPLAINT_TYPES, COMPLAINT_GENERATOR_MODEL_NAME, SERVERS_CONFIG
from prompts import get_complaint_generation_prompt
from html_to_bbcode import html_to_bbcode
from rate_limiter import (
    execute_vertex_request, RequestPriority, get_current_client,
    mark_current_region_success, mark_current_region_error,
)
from utils import reinitialize_vertex_ai

logger = logging.getLogger(__name__)

# Минимальная длина описания ситуации (как в боте, generate_complaint_document).
_MIN_SITUATION_LEN = 50


async def generate_complaint(
    server_id: str,
    complaint_type: str,
    forum_url: str,
    situation_description: str,
) -> Dict:
    """
    Составляет жалобу/иск: парсит форму с форума → подбирает законы → (для иска) проверка
    законности → генерация документа → конвертация в BB-код.

    Args:
        server_id: ID сервера
        complaint_type: 'complaint' (жалоба в прокуратуру) | 'lawsuit' (иск в суд)
        forum_url: ссылка на тему форума с формой (обязательна — из неё берётся шаблон формы)
        situation_description: описание ситуации пользователем

    Returns:
        {complaint_type, complaint_type_name, generated_html, generated_bbcode, legality_warning}

    Raises:
        ValueError: невалидные параметры / плохая ссылка / форма не извлечена / пустая база
        RuntimeError: ошибка аутентификации или перегрузка AI (429)
    """
    # --- Валидация ---
    if server_id not in SERVERS_CONFIG:
        raise ValueError(f"Неизвестный сервер: {server_id}")
    if complaint_type not in COMPLAINT_TYPES:
        raise ValueError("Неизвестный тип документа")
    if len((situation_description or "").strip()) < _MIN_SITUATION_LEN:
        raise ValueError(f"Опишите ситуацию подробнее (минимум {_MIN_SITUATION_LEN} символов)")

    # SSRF: форум-URL фетчится на сервере → проверяем по вайтлисту доменов.
    from api.url_validator import validate_forum_url
    ok, reason = validate_forum_url(forum_url)
    if not ok:
        raise ValueError(f"Недопустимая ссылка на форум: {reason}")

    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # Context-free хелперы бота — лениво (избегаем модульного цикла telegram/api).
    from user_handlers.complaint_flow import (
        parse_forum_form, get_relevant_laws_for_complaint, check_lawsuit_legality,
    )

    client = get_current_client()

    # 1. Парсинг формы с форума (шаблон документа).
    form_text = await parse_forum_form(forum_url)
    if not form_text:
        raise ValueError("Не удалось извлечь форму со страницы форума. Проверьте ссылку на тему.")

    # 2. Законы: сначала подборка конкретных статей по сути ситуации (каталог + FRIDA),
    # затем прежний интеллектуальный подбор документов, и только в крайнем случае — все
    # законы сервера целиком (десятки документов, из-за которых нужная норма терялась).
    law_files: list = []
    try:
        ctx = await case_law_context.law_context_for_case(
            server_id, situation_description, recent=form_text,
            header='ПОДБОРКА СТАТЕЙ ЗАКОНОДАТЕЛЬСТВА СЕРВЕРА (релевантные ситуации)',
        )
        if ctx:
            logger.info(
                "[COMPLAINT_SERVICE] подборка статей: %s статей, %s симв. (server=%s)",
                ctx['count'], ctx['chars'], server_id,
            )
            law_files = [types.Part(text=ctx['text'])]
    except Exception as e:
        logger.warning("[COMPLAINT_SERVICE] подборка статей не удалась: %s", e)

    if not law_files:
        law_files = await get_relevant_laws_for_complaint(
            server_id=server_id, complaint_type=complaint_type,
            situation_description=situation_description, form_text=form_text, genai_client=client,
        )
    if not law_files:
        records = await db_manager.fetch(
            "SELECT gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id,
        )
        if not records:
            raise ValueError("База знаний законов для этого сервера недоступна")
        law_files = [types.Part.from_uri(file_uri=r['gemini_file_uri'], mime_type="text/plain") for r in records]

    # 3. Проверка законности (только иск) — best-effort, не блокирует генерацию.
    legality_warning = ""
    if complaint_type == "lawsuit":
        try:
            check = await check_lawsuit_legality(server_id, situation_description, form_text, client)
            if check:
                legality_warning = f"<b>⚠️ Предупреждение о законности иска:</b>\n\n{check}\n\n"
        except Exception as e:
            logger.warning("[COMPLAINT_SERVICE] Проверка законности не удалась: %s", e)

    # 4. Генерация документа.
    system_prompt = get_complaint_generation_prompt(
        complaint_type=complaint_type, form_text=form_text, situation_description=situation_description,
    )
    content_parts = law_files + [
        types.Part(text="Начинай составление документа на основе предоставленной формы и описания ситуации.")
    ]

    async def send_generation_request():
        c = get_current_client()
        if not c:
            raise RuntimeError("genai_client не инициализирован")
        return await c.aio.models.generate_content(
            model=COMPLAINT_GENERATOR_MODEL_NAME,
            contents=content_parts,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt, temperature=0.3, top_p=0.95, max_output_tokens=8192,
            ),
        )

    try:
        response = await execute_vertex_request(
            func=send_generation_request, model_name=COMPLAINT_GENERATOR_MODEL_NAME,
            priority=RequestPriority.HIGH, max_retries=3,
        )
        await mark_current_region_success()
    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        if isinstance(e, genai_errors.ClientError):
            es = str(e)
            if '429' not in es and 'RESOURCE_EXHAUSTED' not in es:
                raise
        logger.error("[COMPLAINT_SERVICE] AI перегружен: %s", e)
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")

    generated_html = (getattr(response, "text", None) or "").strip()
    if not generated_html:
        raise RuntimeError("AI не вернул документ, попробуйте ещё раз")
    if legality_warning:
        generated_html = legality_warning + generated_html

    # 5. BB-код для форума (та же функция, что у бота).
    generated_bbcode = html_to_bbcode(generated_html)

    return {
        "complaint_type": complaint_type,
        "complaint_type_name": COMPLAINT_TYPES[complaint_type],
        "generated_html": generated_html,
        "generated_bbcode": generated_bbcode,
        "legality_warning": legality_warning or None,
    }


# END OF FILE services/complaint_service.py
