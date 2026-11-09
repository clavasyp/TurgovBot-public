# START OF FILE services/assistant_service.py

"""
Сервис «Ролевой помощник» для веб-API (/v1/web/assistant).

Извлечённая бизнес-логика из user_handlers/assistant_flow.py (context-free): состояние дела
живёт в БД (assistant_cases/assistant_messages через database.assistant_logger), запросы
веба — stateless по case_id. БЕЗ зависимостей от python-telegram-bot/playwright
(assistant_flow тянет telegram+playwright → его НЕ импортируем; маленькие словари
ролей/стадий/инструментов продублированы здесь как чистые данные).

Лимит ассистента (скользящее окно 72ч, usage_type=f"{rp_role}_{case_stage}") списывается
в веб-роутере при СОЗДАНИИ дела (1 списание = 1 дело), как в боте.

Первый этап: текстовая фабула. OCR/парсинг форумных тем (судья) и job_queue-автозакрытие
не переносим — TTL дела считается лениво, парсинг URL — follow-up.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions

import config
from services import case_digest, case_law_context
from database import assistant_logger
from database.manager import db_manager
from config import (
    SERVERS_CONFIG, ASSISTANT_ROUTER_MODEL_NAME, ROLE_ASSISTANT_MODEL_NAME,
    ASSISTANT_LIMIT_RESET_HOURS,
)
from prompts import (
    get_assistant_router_prompt, get_assistant_system_prompt, get_assistant_tool_prompt,
)
from precedents_manager import get_relevant_precedents
from rate_limiter import (
    execute_vertex_request, RequestPriority, get_current_client,
    mark_current_region_success, mark_current_region_error,
)
from utils import reinitialize_vertex_ai, sanitize_telegram_html

logger = logging.getLogger(__name__)

# --- Справочники (чистые данные; зеркало assistant_flow.py, без telegram-кнопок) ---

ASSISTANT_ROLES: Dict[str, str] = {
    "advocate": "Адвокат",
    "prosecutor": "Прокурор",
    "judge": "Судья",
}

ASSISTANT_STAGES: Dict[str, str] = {
    "field_work": "🚨 На месте происшествия / задержания",
    "case_analysis": "🗂️ Анализ материалов дела",
    "court_case": "⚖️ Судебное дело",
}

# Доступные стадии по роли (как в боте: судье — только судебное дело).
STAGES_BY_ROLE: Dict[str, List[str]] = {
    "advocate": ["field_work", "case_analysis"],
    "prosecutor": ["field_work", "case_analysis"],
    "judge": ["court_case"],
}

# Инструменты роли/стадии: {name, label}. name = callback_data бота без префикса "tool_".
ASSISTANT_TOOLS: Dict[str, Dict[str, List[Dict[str, str]]]] = {
    "advocate": {
        "field_work": [
            {"name": "adv_build_defense", "label": "🛡️ Экспресс-анализ и линия защиты"},
            {"name": "adv_procedural_audit", "label": "🔍 Аудит действий офицеров"},
            {"name": "adv_client_briefing", "label": "💬 Подготовка к общению с клиентом/полицией"},
        ],
        "case_analysis": [
            {"name": "adv_build_defense", "label": "🛡️ Анализ дела и линия защиты"},
            {"name": "adv_procedural_audit", "label": "🔍 Процессуальный аудит"},
            {"name": "adv_search_precedents", "label": "💥 Поиск прецедентов и контраргументов"},
        ],
    },
    "prosecutor": {
        "field_work": [
            {"name": "pro_qualify_crime", "label": "⚖️ Первичная правовая квалификация"},
            {"name": "pro_procedural_audit", "label": "📋 Чек-лист процессуальных действий"},
            {"name": "pro_prepare_interrogation", "label": "❗ Фиксация ключевых доказательств"},
        ],
        "case_analysis": [
            {"name": "pro_qualify_crime", "label": "🖋️ Квалификация и формирование обвинения"},
            {"name": "pro_procedural_audit", "label": "🔍 Процессуальный аудит (сторона обвинения)"},
            {"name": "pro_prepare_interrogation", "label": "❓ Подготовка к допросу"},
        ],
    },
    "judge": {"court_case": []},
}


def get_options() -> Dict:
    """Справочник для фронта: роли, стадии по ролям, инструменты, окно лимита."""
    return {
        "roles": [{"key": k, "label": v} for k, v in ASSISTANT_ROLES.items()],
        "stages": {k: v for k, v in ASSISTANT_STAGES.items()},
        "stages_by_role": STAGES_BY_ROLE,
        "tools": ASSISTANT_TOOLS,
        "reset_hours": ASSISTANT_LIMIT_RESET_HOURS,
    }


def usage_type_for(rp_role: str, case_stage: str) -> str:
    """Тип лимита ассистента (как в боте): {role}_{stage}."""
    return f"{rp_role}_{case_stage}"


def validate_role_stage(rp_role: str, case_stage: str) -> None:
    """Бросает ValueError, если роль/стадия невалидны или несовместимы."""
    if rp_role not in ASSISTANT_ROLES:
        raise ValueError("Неизвестная роль")
    if case_stage not in ASSISTANT_STAGES:
        raise ValueError("Неизвестная стадия")
    if case_stage not in STAGES_BY_ROLE.get(rp_role, []):
        raise ValueError("Стадия недоступна для этой роли")


async def get_case(case_id: int) -> Optional[Dict]:
    """Метаданные дела (id, user_id, server_id, rp_role, case_stage, status)."""
    row = await db_manager.fetchrow(
        "SELECT id, user_id, server_id, rp_role, case_stage, status, created_at, last_updated_at "
        "FROM assistant_cases WHERE id = $1",
        case_id,
    )
    return dict(row) if row else None


# --- Внутреннее ядро (без telegram-контекста) ---

async def _get_law_parts(
    server_id: str, case_id: int, rp_role: str, case_stage: str, current_request: str
) -> List[types.Part]:
    """Законы для запроса: подборка статей, при неудаче — прежние целые документы.

    Зеркало telegram-флоу (user_handlers/assistant_flow.get_law_parts_for_assistant):
    статьи вместо целых кодексов, откат на старый путь там, где каталога ещё нет.
    """
    if case_law_context.articles_mode_enabled():
        try:
            history = await assistant_logger.get_case_history(case_id) or []
            facts, recent = case_law_context.history_facts_and_recent(history)
            ctx = await case_law_context.law_context_for_case(
                server_id, current_request, facts=facts, recent=recent,
            )
            if ctx:
                logger.info(
                    "[ASSISTANT_LAW] дело #%s: подборка статей — %s статей, %s симв.",
                    case_id, ctx['count'], ctx['chars'],
                )
                return [types.Part(text=ctx['text'])]
        except Exception as e:
            logger.warning("[ASSISTANT_LAW] дело #%s: подборка статей не удалась (%s)", case_id, e)

    return await _get_relevant_laws(server_id, case_id, rp_role, case_stage, current_request)


async def _get_relevant_laws(
    server_id: str, case_id: int, rp_role: str, case_stage: str, current_request: str
) -> List[types.Part]:
    """Интеллектуальный фильтр законов по полному контексту дела (context-free версия
    get_relevant_laws_for_assistant)."""
    try:
        law_records = await db_manager.fetch(
            "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id,
        )
        if not law_records:
            return []

        law_map = {i + 1: {"title": r["title"], "uri": r["gemini_file_uri"]} for i, r in enumerate(law_records)}
        law_titles_str = "\n".join([f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)])

        history_records = await assistant_logger.get_case_history(case_id)
        if not history_records:
            return []

        case_context = (
            f"Роль пользователя: {ASSISTANT_ROLES.get(rp_role, rp_role)}, "
            f"Стадия: {ASSISTANT_STAGES.get(case_stage, case_stage)}"
        )
        initial_description = history_records[0]["content"] if history_records else "Фабула дела не предоставлена."
        case_context += f"\nНачальное описание: {initial_description}"

        dialog_history = ""
        for msg in history_records[-6:]:
            sender = "Пользователь" if msg["sender_role"] == "user" else "Ассистент"
            dialog_history += f"{sender}: {msg['content']}\n"

        router_prompt = get_assistant_router_prompt(
            case_context=case_context, dialog_history=dialog_history,
            current_request=current_request, law_titles_str=law_titles_str,
        )

        async def make_router_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ASSISTANT_ROUTER_MODEL_NAME, contents=router_prompt,
            )

        response = await execute_vertex_request(
            func=make_router_request, model_name=ASSISTANT_ROUTER_MODEL_NAME,
            priority=RequestPriority.HIGH, max_retries=2,
        )
        await mark_current_region_success()

        selected = {int(line.strip()) for line in (response.text or "").split("\n") if line.strip().isdigit()}
        relevant = [law_map[i] for i in sorted(selected) if i in law_map]
        return [types.Part.from_uri(file_uri=law["uri"], mime_type="text/plain") for law in relevant]

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        if isinstance(e, genai_errors.ClientError):
            es = str(e)
            if "429" not in es and "RESOURCE_EXHAUSTED" not in es:
                raise
        logger.warning("[ASSISTANT_SERVICE] router 429 для дела #%s: %s", case_id, e)
        return []
    except Exception as e:
        logger.error("[ASSISTANT_SERVICE] ошибка фильтра законов дела #%s", case_id, exc_info=e)
        return []


async def _generate_reply(
    case_id: int, server_id: str, rp_role: str, case_stage: str,
    user_content: str, tool_name: Optional[str] = None,
) -> str:
    """Генерирует ответ ассистента по текущему состоянию дела. Историю НЕ пишет
    (вызывающий добавляет user-сообщение ДО, а ai-ответ — ПОСЛЕ)."""
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # 1. Релевантные законы.
    files_for_query = await _get_law_parts(server_id, case_id, rp_role, case_stage, user_content)

    # 2. Прецеденты (best-effort).
    client = get_current_client()
    if client:
        try:
            for precedent in await get_relevant_precedents(client, user_content, server_id):
                files_for_query.append(types.Part.from_uri(file_uri=precedent["uri"], mime_type="text/plain"))
        except Exception as e:
            logger.warning("[ASSISTANT_SERVICE] прецеденты дела #%s: %s", case_id, e)

    # 3. История → gemini_history.
    history = await assistant_logger.get_case_history(case_id)
    if history is None:
        raise RuntimeError("Не удалось загрузить историю дела")

    # Тот же путь, что в telegram-флоу: карта дела вместо сырой истории на длинных делах,
    # с откатом на обрезку по лимиту. Раньше веб отправлял историю целиком без ограничений.
    history = await case_digest.build_case_messages(
        case_id, history, rp_role=rp_role, case_stage=case_stage,
    )

    gemini_history = [
        types.Content(role=("model" if m["sender_role"] == "ai" else "user"),
                      parts=[types.Part(text=m["content"])])
        for m in history
    ]
    if not gemini_history:
        raise RuntimeError("Пустая история дела")

    system_prompt = get_assistant_system_prompt(rp_role, case_stage)
    prompt_for_ai = gemini_history[-1].parts[0].text
    if tool_name:
        # Задание инструмента ДОПОЛНЯЕТ последний ход, а не заменяет его (иначе модель не
        # видит, какой инструмент нажали) — тот же фикс, что в telegram-флоу.
        tool_prompt = get_assistant_tool_prompt(tool_name)
        prompt_for_ai = f"{prompt_for_ai}\n\n{tool_prompt}" if prompt_for_ai else tool_prompt

    final_content = types.Content(role="user", parts=[types.Part(text=prompt_for_ai)] + files_for_query)
    full_history = gemini_history[:-1] + [final_content]

    async def send_request():
        c = get_current_client()
        if not c:
            raise RuntimeError("genai_client не инициализирован")
        gen_kwargs: Dict[str, Any] = {'system_instruction': system_prompt}
        max_tokens = getattr(config, 'ASSISTANT_MAX_OUTPUT_TOKENS', 0) or 0
        if max_tokens > 0:
            gen_kwargs['max_output_tokens'] = max_tokens
        level = (getattr(config, 'ASSISTANT_THINKING_LEVEL', '') or '').strip().lower()
        enum_val = {
            'minimal': getattr(types.ThinkingLevel, 'MINIMAL', None),
            'low': getattr(types.ThinkingLevel, 'LOW', None),
            'medium': getattr(types.ThinkingLevel, 'MEDIUM', None),
            'high': getattr(types.ThinkingLevel, 'HIGH', None),
        }.get(level) if hasattr(types, 'ThinkingLevel') else None
        if enum_val is not None:
            gen_kwargs['thinking_config'] = types.ThinkingConfig(thinking_level=enum_val)
        return await c.aio.models.generate_content(
            model=ROLE_ASSISTANT_MODEL_NAME, contents=full_history,
            config=types.GenerateContentConfig(**gen_kwargs),
        )

    try:
        response = await execute_vertex_request(
            func=send_request, model_name=ROLE_ASSISTANT_MODEL_NAME,
            priority=RequestPriority.CRITICAL, max_retries=3,
        )
        await mark_current_region_success()
    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        if isinstance(e, genai_errors.ClientError):
            es = str(e)
            if "429" not in es and "RESOURCE_EXHAUSTED" not in es:
                raise
        logger.error("[ASSISTANT_SERVICE] AI перегружен дела #%s: %s", case_id, e)
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")

    reply = sanitize_telegram_html((getattr(response, "text", None) or "").strip())
    if not reply:
        raise RuntimeError("AI не вернул ответ, попробуйте переформулировать")
    return reply


# --- Публичные операции (вызываются роутером) ---

async def create_case(
    user_id: int, server_id: str, rp_role: str, case_stage: str, description: str,
) -> Tuple[int, str]:
    """Создаёт дело, записывает фабулу и возвращает первый ответ ассистента.
    ЛИМИТ списывается в роутере ДО вызова (1 списание = 1 дело)."""
    if server_id not in SERVERS_CONFIG:
        raise ValueError(f"Неизвестный сервер: {server_id}")
    validate_role_stage(rp_role, case_stage)
    if len((description or "").strip()) < 10:
        raise ValueError("Опишите фабулу дела подробнее (минимум 10 символов)")

    case_id = await assistant_logger.create_case(user_id, server_id, rp_role, case_stage)
    if not case_id:
        raise RuntimeError("Не удалось создать дело в базе данных")

    description = description.strip()
    await assistant_logger.add_message_to_case(case_id, "user", description)
    reply = await _generate_reply(case_id, server_id, rp_role, case_stage, description)
    await assistant_logger.add_message_to_case(case_id, "ai", reply)
    return case_id, reply


async def post_message(
    case: Dict, text: Optional[str] = None, tool: Optional[str] = None,
) -> str:
    """Добавляет сообщение/инструмент в существующее дело и возвращает ответ ассистента.
    `case` — строка из get_case (роутер уже проверил владельца и статус)."""
    case_id = case["id"]
    server_id = case["server_id"]
    rp_role = case["rp_role"]
    case_stage = case["case_stage"]

    if tool:
        tools = ASSISTANT_TOOLS.get(rp_role, {}).get(case_stage, [])
        label = next((t["label"] for t in tools if t["name"] == tool), None)
        if not label:
            raise ValueError("Неизвестный инструмент")
        user_content = f"Пользователь активировал инструмент: «{label}»"
    else:
        user_content = (text or "").strip()
        if not user_content:
            raise ValueError("Пустое сообщение")

    await assistant_logger.add_message_to_case(case_id, "user", user_content)
    reply = await _generate_reply(case_id, server_id, rp_role, case_stage, user_content, tool_name=tool)
    await assistant_logger.add_message_to_case(case_id, "ai", reply)
    return reply


async def get_history(case_id: int) -> List[Dict]:
    """История дела: [{role:'user'|'ai', content}]."""
    rows = await assistant_logger.get_case_history(case_id) or []
    return [{"role": ("ai" if r["sender_role"] == "ai" else "user"), "content": r["content"]} for r in rows]


async def list_cases(user_id: int) -> List[Dict]:
    """Активные дела пользователя для списка на фронте."""
    rows = await assistant_logger.get_active_cases_for_user(user_id) or []
    out = []
    for r in rows:
        out.append({
            "case_id": r["id"],
            "server_id": r["server_id"],
            "rp_role": r["rp_role"],
            "rp_role_label": ASSISTANT_ROLES.get(r["rp_role"], r["rp_role"]),
            "case_stage": r["case_stage"],
            "case_stage_label": ASSISTANT_STAGES.get(r["case_stage"], r["case_stage"]),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
        })
    return out


async def close_case(case_id: int) -> None:
    await assistant_logger.close_case(case_id)


# END OF FILE services/assistant_service.py
