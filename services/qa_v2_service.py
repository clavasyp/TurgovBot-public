# START OF FILE services/qa_v2_service.py

"""
Q&A v2 (эксперимент, админская команда /qa2): новый пайплайн юрконсультанта.

Отличия от services/qa_service.py (v1, основной путь — НЕ тронут):
  - контекст = подборка статей (services/qa_context.py) вместо целых кодексов файлами;
  - модель config.QA_V2_MODEL_NAME с ЯВНЫМ thinking_level (у 3.5-flash-lite дефолт minimal);
  - инструмент search_laws: модель сама дозапрашивает недостающие нормы; цикл ограничен
    QA_V2_TOOL_MAX_ROUNDS в коде, уже переданные статьи в дозапросы не возвращаются;
  - без модели-роутера и без оптимизатора вопросов (вопрос идёт как есть; грамотную
    формулировку для поиска модель строит сама через search_laws).

Возвращает и ответ, и диагностику (модель, статьи, раунды, время) — для сравнения с v1.

google.genai импортируется лениво (паттерн services/law_loophole/analyzer.py), чтобы
юнит-тесты гоняли цикл на фейках без SDK/сети.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

import config
from services.qa_context import build_qa_context, ArticleKey
from prompts_qa_v2 import (
    QA_V2_PROMPT_VERSION,
    get_qa_v2_system_prompt,
    SEARCH_LAWS_TOOL_DESCRIPTION,
    SEARCH_LAWS_QUERY_DESCRIPTION,
)

logger = logging.getLogger(__name__)

_TOOL_EMPTY_RESULT = (
    "Ничего нового не найдено. Отвечай по уже переданным материалам; если нужной нормы "
    "в них нет — честно скажи, что норма не найдена. Этот запрос повторять бессмысленно."
)
_TOOL_LIMIT_NOTE = (
    "\n\nВНИМАНИЕ: лимит дозапросов исчерпан — это был последний. Дальше отвечай только "
    "по уже переданным материалам."
)


class QaV2EmptyContext(ValueError):
    """Подборка статей пуста: у сервера нет ни каталога (law_articles), ни векторного
    индекса (law_chunks) — эксперимент v2 на таком сервере невозможен."""

    def __init__(self, server_id: str, legs: Dict[str, str]):
        super().__init__(f"пустая подборка для {server_id}: {legs}")
        self.server_id = server_id
        self.legs = legs


def _make_thinking_config(types_mod: Any, level: Optional[str]):
    """types.ThinkingConfig по строке уровня; None/пусто/мусор → не слать thinking
    (аварийный откат правкой env, без деплоя) — паттерн loophole-анализатора."""
    lv = (level or '').strip().lower()
    mapping = {
        'minimal': getattr(types_mod.ThinkingLevel, 'MINIMAL', None),
        'low': getattr(types_mod.ThinkingLevel, 'LOW', None),
        'medium': getattr(types_mod.ThinkingLevel, 'MEDIUM', None),
        'high': getattr(types_mod.ThinkingLevel, 'HIGH', None),
    }
    enum_val = mapping.get(lv)
    if enum_val is None:
        return None
    return types_mod.ThinkingConfig(thinking_level=enum_val)


def _make_search_tool(types_mod: Any):
    return types_mod.Tool(function_declarations=[
        types_mod.FunctionDeclaration(
            name='search_laws',
            description=SEARCH_LAWS_TOOL_DESCRIPTION,
            parameters=types_mod.Schema(
                type=types_mod.Type.OBJECT,
                properties={
                    'query': types_mod.Schema(
                        type=types_mod.Type.STRING,
                        description=SEARCH_LAWS_QUERY_DESCRIPTION,
                    ),
                },
                required=['query'],
            ),
        ),
    ])


def _extract_function_calls(response: Any) -> List[Any]:
    """function_call-части первого кандидата (у SDK есть шорткат .function_calls — используем
    его, если отдал; иначе руками по parts). Пусто = модель ответила текстом."""
    try:
        shortcut = getattr(response, 'function_calls', None)
        if shortcut:
            return list(shortcut)
    except Exception:
        pass
    calls: List[Any] = []
    candidates = getattr(response, 'candidates', None) or []
    if not candidates:
        return calls
    content = getattr(candidates[0], 'content', None)
    for part in (getattr(content, 'parts', None) or []):
        fc = getattr(part, 'function_call', None)
        if fc is not None:
            calls.append(fc)
    return calls


async def _run_tool_search(
    server_id: str,
    raw_query: Any,
    seen_keys: Set[ArticleKey],
    seen_queries: Set[str],
) -> Dict[str, Any]:
    """Один дозапрос search_laws: подборка без уже переданных статей, меньшие лимиты.
    Возвращает {'text': ответ инструменту, 'added': int, 'query': str}."""
    query = str(raw_query or '').strip()
    norm_q = query.lower()
    if not query:
        return {'text': 'Пустой поисковый запрос — сформулируй тему поиска.', 'added': 0, 'query': ''}
    if norm_q in seen_queries:
        return {'text': _TOOL_EMPTY_RESULT, 'added': 0, 'query': query}
    seen_queries.add(norm_q)

    ctx = await build_qa_context(
        server_id, query,
        # Дозапрос добавляется К уже переданному контексту → потолок втрое ниже стартового.
        max_chars=max(10_000, config.QA_V2_CONTEXT_MAX_CHARS // 3),
        search_limit=config.QA_V2_TOOL_SEARCH_LIMIT,
        vector_top_k=max(8, config.QA_V2_VECTOR_TOP_K // 2),
        exclude_keys=seen_keys,
        header='ДОПОЛНИТЕЛЬНО НАЙДЕННЫЕ СТАТЬИ',
    )
    if ctx is None:
        return {'text': _TOOL_EMPTY_RESULT, 'added': 0, 'query': query}
    seen_keys |= ctx['keys']
    return {'text': ctx['text'], 'added': ctx['count'], 'query': query}


async def ask_question_v2(
    server_id: str,
    question: str,
    q_type: str = 'detailed',
    *,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Полный пайплайн v2. Возвращает {'answer': str, 'diag': dict}.

    Raises:
        ValueError / QaV2EmptyContext — плохой сервер или пустая подборка;
        RuntimeError — аутентификация / пустой ответ модели;
        исключения Vertex (429 и пр.) пробрасываются как есть — /qa2 покажет их админу,
        инфраструктура кнопок-повторов v1 тут не нужна.
    """
    from google.genai import types  # ленивый импорт (см. докстринг модуля)
    from rate_limiter import execute_vertex_request, RequestPriority, get_current_client
    from utils import reinitialize_vertex_ai

    if server_id not in config.SERVERS_CONFIG:
        raise ValueError(f"Неизвестный сервер: {server_id}")
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    started = asyncio.get_running_loop().time()
    model_name = config.QA_V2_MODEL_NAME
    thinking_level = (config.QA_V2_THINKING_LEVEL or '').strip().lower()

    # 1. Стартовая подборка по вопросу пользователя.
    ctx = await build_qa_context(server_id, question)
    if ctx is None:
        # Детальный статус ног уже в логе build_qa_context.
        raise QaV2EmptyContext(server_id, {'catalog': 'empty/error', 'vector': 'empty/error'})

    seen_keys: Set[ArticleKey] = set(ctx['keys'])
    seen_queries: Set[str] = set()
    diag: Dict[str, Any] = {
        'model': model_name,
        'prompt_version': QA_V2_PROMPT_VERSION,
        'thinking': thinking_level or 'off',
        'initial_count': ctx['count'],
        'initial_chars': ctx['chars'],
        'initial_dropped': ctx['dropped'],
        'legs': ctx['legs'],
        'tool_rounds': [],
        'model_calls': 0,
    }

    contents: List[Any] = [types.Content(role='user', parts=[
        types.Part(text=ctx['text']),
        types.Part(text=f"ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n{question}"),
    ])]

    system_instruction = get_qa_v2_system_prompt(q_type)
    thinking_config = _make_thinking_config(types, thinking_level)
    search_tool = _make_search_tool(types)
    max_rounds = max(0, int(config.QA_V2_TOOL_MAX_ROUNDS))

    def _gen_config(with_tools: bool):
        kwargs: Dict[str, Any] = {'system_instruction': system_instruction}
        if thinking_config is not None:
            kwargs['thinking_config'] = thinking_config
        if with_tools:
            kwargs['tools'] = [search_tool]
        return types.GenerateContentConfig(**kwargs)

    async def _model_call(with_tools: bool):
        async def _do():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=_gen_config(with_tools),
            )
        diag['model_calls'] += 1
        return await execute_vertex_request(
            func=_do,
            model_name=model_name,
            priority=RequestPriority.CRITICAL,
            user_id=user_id,
            max_retries=3,
        )

    # 2. Цикл: модель либо отвечает текстом, либо зовёт search_laws (потолок раундов В КОДЕ).
    # Жёсткий предохранитель max_rounds+2: даже если SDK вопреки выключенным tools вернёт
    # function_call, цикл гарантированно конечен (последняя итерация отдаст text/пусто).
    rounds_used = 0
    for _ in range(max_rounds + 2):
        # После исчерпания раундов tools не передаются вовсе → function_call невозможен,
        # модель обязана дать текст (надёжнее, чем просить промптом).
        response = await _model_call(with_tools=rounds_used < max_rounds)
        function_calls = _extract_function_calls(response)
        if not function_calls:
            break

        # Даже если SDK/модель аномально вернули function_call в ходе, где tools уже
        # отключены, жёсткий лимит остаётся лимитом: такой вызов не исполняем.
        if rounds_used >= max_rounds:
            logger.warning(
                "[QA_V2] function_call после исчерпания лимита (%d) проигнорирован",
                max_rounds,
            )
            break

        rounds_used += 1
        candidates = getattr(response, 'candidates', None) or []
        model_content = getattr(candidates[0], 'content', None) if candidates else None
        if model_content is None:
            logger.warning("[QA_V2] function_call без content — отвечаем тем, что есть")
            break
        # ВАЖНО (Gemini 3, thinking+tools): контент модели возвращается в историю КАК ЕСТЬ,
        # вместе со служебными thought signatures — иначе 4xx на следующем ходе.
        contents.append(model_content)

        response_parts: List[Any] = []
        last_round = rounds_used >= max_rounds
        for fc in function_calls:
            args = getattr(fc, 'args', None) or {}
            result = await _run_tool_search(server_id, args.get('query'), seen_keys, seen_queries)
            diag['tool_rounds'].append({'query': result['query'], 'added': result['added']})
            tool_text = result['text'] + (_TOOL_LIMIT_NOTE if last_round else '')
            response_parts.append(types.Part.from_function_response(
                name=getattr(fc, 'name', 'search_laws') or 'search_laws',
                response={'result': tool_text},
            ))
        contents.append(types.Content(role='user', parts=response_parts))

    answer = (getattr(response, 'text', None) or '').strip()
    if not answer:
        logger.warning("[QA_V2] пустой ответ модели (safety/MAX_TOKENS/только function_call)")
        raise RuntimeError("AI не вернул ответ — попробуйте переформулировать вопрос")

    diag['rounds_used'] = rounds_used
    diag['total_added'] = sum(r['added'] for r in diag['tool_rounds'])
    diag['elapsed_sec'] = asyncio.get_running_loop().time() - started
    logger.info(
        "[QA_V2] server=%s статей=%d(+%d) раундов=%d вызовов=%d за %.1fс",
        server_id, diag['initial_count'], diag['total_added'],
        rounds_used, diag['model_calls'], diag['elapsed_sec'],
    )
    return {'answer': answer, 'diag': diag}

# END OF FILE services/qa_v2_service.py
