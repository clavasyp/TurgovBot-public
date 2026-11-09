# START OF FILE services/case_law_context.py

"""Подборка статей закона для «тяжёлых» флоу: ролевой помощник, жалобы, иски.

Зачем. Исторически эти потоки подавали модели ЦЕЛЫЕ кодексы через Gemini File API:
помощник — те, что выбрал роутер по списку заголовков (не видя текста), генератор жалоб —
вообще все законы сервера. На реальных делах (до 432 000 символов истории, 31-44 документа
законки) это давало три беды: цена, латентность и «потеря середины» — модель перестаёт
замечать нормы в глубине огромного контекста, ровно как в Q&A v2 до перехода на статьи.

Здесь тот же приём, что уже работает в Q&A v2: подобрать КОНКРЕТНЫЕ статьи каталожным
поиском + вектором (services/qa_context.py) и отдать их текстом.

Совместимость. Каталог статей есть не у всех серверов (его достраивают), поэтому функции
возвращают None, если подборка пустая — вызывающий откатывается на прежний режим целых
документов. Режим переключается `config.ASSISTANT_LAW_CONTEXT_MODE`.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import config
from services.qa_context import build_qa_context

logger = logging.getLogger(__name__)

# Сколько символов фабулы добавлять к поисковому запросу. Сам по себе вопрос вроде
# «а что дальше?» или «продолжай» ничего не даёт поиску — тему задаёт описание дела.
_FACTS_QUERY_CHARS = 1200
# Хвост диалога тоже влияет на тему («перешли к обыску»), но целиком он слишком шумный.
_RECENT_QUERY_CHARS = 600

_WS_RE = re.compile(r'\s+')


def _clean(text: Optional[str], limit: int) -> str:
    return _WS_RE.sub(' ', (text or '')).strip()[:limit]


def build_search_query(
    current_request: str,
    *,
    facts: Optional[str] = None,
    recent: Optional[str] = None,
) -> str:
    """Поисковый запрос для подборки статей: текущий вопрос + фабула + свежий контекст.

    Порядок важен: текущий запрос идёт первым, он определяет, что именно ищем сейчас.
    """
    parts = [
        _clean(current_request, 2000),
        _clean(facts, _FACTS_QUERY_CHARS),
        _clean(recent, _RECENT_QUERY_CHARS),
    ]
    return ' '.join(p for p in parts if p).strip()


def articles_mode_enabled() -> bool:
    return (getattr(config, 'ASSISTANT_LAW_CONTEXT_MODE', 'articles') or '').strip().lower() == 'articles'


async def law_context_for_case(
    server_id: str,
    current_request: str,
    *,
    facts: Optional[str] = None,
    recent: Optional[str] = None,
    max_chars: Optional[int] = None,
    header: str = 'ПОДБОРКА СТАТЕЙ ЗАКОНОДАТЕЛЬСТВА СЕРВЕРА',
) -> Optional[Dict[str, Any]]:
    """Статьи, релевантные делу, готовым текстовым блоком.

    None — режим выключен, каталога у сервера нет или подборка пуста: вызывающий обязан
    откатиться на прежний режим целых документов, иначе модель останется без законов.
    """
    if not articles_mode_enabled():
        return None

    query = build_search_query(current_request, facts=facts, recent=recent)
    if not query:
        return None

    limit = max_chars or getattr(config, 'ASSISTANT_LAW_CONTEXT_MAX_CHARS', 60000)
    try:
        ctx = await build_qa_context(server_id, query, max_chars=limit, header=header)
    except Exception as e:
        # Подборка не критична для работоспособности: без неё просто уходим на старый путь.
        logger.warning("[CASE_LAW] подборка статей упала для server=%s: %s", server_id, e)
        return None

    if not ctx:
        logger.info("[CASE_LAW] пустая подборка для server=%s — откат на целые документы", server_id)
        return None

    logger.info(
        "[CASE_LAW] server=%s: статей %s, %s симв. (ноги %s)%s",
        server_id, ctx['count'], ctx['chars'], ctx.get('legs'),
        ', часть отброшена по потолку' if ctx.get('dropped') else '',
    )
    return ctx


def history_facts_and_recent(history: List[Dict[str, Any]]) -> tuple[str, str]:
    """(фабула, свежий контекст) из истории дела для построения поискового запроса."""
    if not history:
        return '', ''
    facts = history[0].get('content') or ''
    recent = ' '.join((m.get('content') or '') for m in history[-3:])
    return facts, recent

# END OF FILE services/case_law_context.py
