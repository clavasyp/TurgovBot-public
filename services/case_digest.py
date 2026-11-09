# START OF FILE services/case_digest.py

"""Карта дела: сжатие старой части переписки вместо её выбрасывания.

Зачем. Дела ролевого помощника доходят до 432 000 символов при 135 сообщениях, и вся
история переотправлялась модели каждым ходом. Потолок истории (блок 1) спас от переполнения
контекста, но лечит грубо: середина дела просто исчезает с пометкой «N сообщений опущено».
На длинной консультации это значит, что помощник забывает эпизоды, имена, даты и уже
принятые решения — ровно то, ради чего дело и ведётся.

Здесь старая часть дела сжимается в структурированную карту (стороны, хронология,
доказательства, нормы, договорённости, открытые вопросы) и подаётся модели вместе со свежим
хвостом диалога. Карта хранится в БД и обновляется инкрементально: следующее сжатие получает
предыдущую карту плюс новые сообщения, поэтому факты из начала дела переносятся дальше, а не
теряются.

Устройство. Планировщик (`plan_case_context`) — чистая функция без БД и модели: он решает,
что отдать сырым, что сжать и хватит ли уже имеющейся карты. Асинхронная обёртка
(`build_case_messages`) добавляет чтение/запись карты и вызов модели, и на любой ошибке
откатывается на прежнее поведение — грубую обрезку. Без карты флоу обязан работать.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import config
from database import assistant_logger
from prompts import get_case_digest_prompt

logger = logging.getLogger(__name__)

Message = Dict[str, Any]

# Служебная шапка карты. Модель должна понимать, что это не реплика пользователя, и что при
# расхождении с сырыми сообщениями верить нужно сообщениям.
_DIGEST_HEADER = (
    "[КАРТА ДЕЛА — автоматическая сводка {count} более ранних сообщений этой консультации. "
    "Это не реплика пользователя, а выжимка из переписки; если карта расходится с сообщениями "
    "ниже, верь сообщениям.]"
)

# Минимум нового текста, ради которого стоит переписывать карту. Ниже порога дешевле и
# безопаснее оставить прежнюю карту: пара реплик подождёт следующего обновления.
_MIN_REFRESH_CHARS = 4000

# Какую долю свободного бюджета занимает сырой хвост в момент пересборки карты.
# Если набивать хвост под завязку, следующее же сообщение переполняет бюджет и карта
# пересобирается почти каждым ходом (на профиле самого большого дела прода — 50 пересборок
# на 135 ходов). Оставленный запас даёт делу вырасти на десяток ходов до следующей.
_REFRESH_TAIL_FILL = 0.6

_TRIM_MARKER = (
    "[Служебная пометка: ранее в деле было ещё {dropped} сообщений; "
    "они опущены из-за объёма. Фабула дела выше, последние обсуждения ниже. "
    "Если для ответа нужны опущенные детали — попроси пользователя их повторить.]"
)


# --- Мелкие хелперы ---------------------------------------------------------------------

def _content(message: Message) -> str:
    return message.get('content') or ''


def _size(message: Message) -> int:
    return len(_content(message))


def _total(messages: List[Message]) -> int:
    return sum(_size(m) for m in messages)


def _mid(message: Message) -> int:
    try:
        return int(message.get('id') or 0)
    except (TypeError, ValueError):
        return 0


def _has_ids(messages: List[Message]) -> bool:
    """Карта опирается на id сообщений: без них непонятно, что уже покрыто сводкой."""
    return all(_mid(m) > 0 for m in messages)


# --- Планировщик (чистый) ---------------------------------------------------------------

@dataclass
class CasePlan:
    """Что именно уйдёт модели.

    mode:
        full    — история помещается целиком, ничего делать не надо;
        digest  — имеющейся карты достаточно, сжатие не нужно;
        refresh — карту нужно построить/обновить по `to_summarize`;
        trim    — сжатие невозможно (выключено, нечего сжимать, модель не ответила),
                  работает прежняя грубая обрезка с маркером.
    """
    mode: str
    head: List[Message] = field(default_factory=list)
    tail: List[Message] = field(default_factory=list)
    to_summarize: List[Message] = field(default_factory=list)
    digest: Optional[Dict[str, Any]] = None
    dropped: int = 0


def _pick_tail(messages: List[Message], budget: int) -> List[Message]:
    """Самые свежие сообщения, влезающие в бюджет.

    Последнее сообщение берётся всегда, даже если оно одно больше бюджета: это текущий
    вопрос пользователя, без него ответ бессмыслен (на проде одно сообщение доходило до
    143 265 символов).
    """
    if not messages:
        return []
    tail = [messages[-1]]
    used = _size(messages[-1])
    for message in reversed(messages[:-1]):
        size = _size(message)
        if used + size > budget:
            break
        tail.insert(0, message)
        used += size
    return tail


def plan_case_context(
    history: List[Message],
    digest: Optional[Dict[str, Any]] = None,
    *,
    limit: Optional[int] = None,
    reserve: Optional[int] = None,
) -> CasePlan:
    """Решает, что подать модели: всю историю, карту + хвост или карту нужно пересобрать.

    Чистая функция — ни БД, ни сети, поэтому поведение на граничных делах проверяется тестами.
    """
    if limit is None:
        limit = getattr(config, 'ASSISTANT_HISTORY_MAX_CHARS', 0) or 0
    if reserve is None:
        reserve = getattr(config, 'ASSISTANT_DIGEST_MAX_CHARS', 12000) or 12000

    if limit <= 0 or len(history) <= 1:
        return CasePlan('full', head=list(history))

    head, rest = history[0], history[1:]
    head_len = _size(head)
    if head_len + _total(rest) <= limit:
        # Помещается целиком — карта не нужна, сырая переписка всегда точнее сводки.
        return CasePlan('full', head=list(history))

    digest_text = ((digest or {}).get('digest') or '').strip()
    covered_id = int((digest or {}).get('covered_message_id') or 0)

    if digest_text:
        fresh = [m for m in rest if _mid(m) > covered_id]
        if not fresh:
            fresh = rest[-1:]
        if head_len + len(digest_text) + _total(fresh) <= limit:
            return CasePlan('digest', head=[head], tail=fresh, digest=digest)

    # Карты нет либо она отстала: считаем новый раскрой «хвост сырым, остальное в карту».
    # Хвост берём с запасом по бюджету — иначе следующее сообщение снова переполнит лимит.
    tail = _pick_tail(rest, int((limit - head_len - reserve) * _REFRESH_TAIL_FILL))
    older = rest[:len(rest) - len(tail)]
    to_summarize = [m for m in older if _mid(m) > covered_id]

    if digest_text and _total(to_summarize) < _MIN_REFRESH_CHARS:
        # Карту каждый раз переписывает модель, а каждое переписывание — риск растерять
        # детали («испорченный телефон»). Ради пары реплик не трогаем: они накопятся и
        # войдут в карту следующим обновлением. Бывает на делах, где одна только фабула
        # больше лимита, — там иначе пришлось бы пересобирать карту каждым ходом.
        return CasePlan('digest', head=[head], tail=tail, digest=digest)

    if not to_summarize:
        # Сжимать нечего, а карты нет: остаётся прежняя грубая обрезка.
        return CasePlan('trim', head=[head], tail=tail, dropped=len(older))

    return CasePlan('refresh', head=[head], tail=tail, to_summarize=to_summarize,
                    digest=digest, dropped=len(older))


def digest_message(digest_text: str, covered_count: int) -> Message:
    """Карта дела в виде сообщения для модели."""
    return {
        'sender_role': 'user',
        'content': f"{_DIGEST_HEADER.format(count=covered_count)}\n\n{digest_text.strip()}",
        'synthetic': True,
    }


def assemble(plan: CasePlan, digest_text: str = '', covered_count: int = 0) -> List[Message]:
    """Итоговый список сообщений для модели по плану."""
    if plan.mode == 'full':
        return list(plan.head)
    if plan.mode == 'trim':
        if plan.dropped > 0:
            marker = {'sender_role': 'user',
                      'content': _TRIM_MARKER.format(dropped=plan.dropped),
                      'synthetic': True}
            return plan.head + [marker] + plan.tail
        return plan.head + plan.tail
    text = (digest_text or ((plan.digest or {}).get('digest') or '')).strip()
    if not text:
        return plan.head + plan.tail
    count = covered_count or int((plan.digest or {}).get('covered_count') or 0) or plan.dropped
    return plan.head + [digest_message(text, count)] + plan.tail


# --- Прежняя грубая обрезка (fallback) ---------------------------------------------------

def trim_history(history: List[Message], case_id: int) -> List[Message]:
    """Ограничивает историю дела по символам, сохраняя фабулу и текущий ход.

    Прежнее поведение блока 1: используется, когда карта выключена, ещё не построена или
    модель не смогла её собрать. Ничего не удаляет из БД — обрезка только для запроса.
    """
    limit = getattr(config, 'ASSISTANT_HISTORY_MAX_CHARS', 0) or 0
    if limit <= 0 or not history:
        return history

    total = _total(history)
    if total <= limit or len(history) == 1:
        return history

    # Фабула и текущий ход обязательны при любом бюджете: без первого модель не понимает
    # дело, без последнего — не видит, о чём её вообще спросили.
    first, last = history[0], history[-1]
    middle = history[1:-1]
    budget = limit - _size(first) - _size(last)

    tail: List[Message] = []
    for message in reversed(middle):
        size = _size(message)
        if size > budget:
            break
        tail.append(message)
        budget -= size
    tail.reverse()
    tail.append(last)

    dropped = len(history) - 1 - len(tail)
    if dropped > 0:
        logger.info(
            "[ASSISTANT] Дело #%s: история %s симв. (%s сообщений) обрезана до %s симв.; "
            "скрыто %s сообщений середины диалога",
            case_id, total, len(history), limit - budget, dropped,
        )
        return [first, {'sender_role': 'user',
                        'content': _TRIM_MARKER.format(dropped=dropped),
                        'synthetic': True}] + tail
    return [first] + tail


# --- Генерация карты --------------------------------------------------------------------

def render_transcript(messages: List[Message]) -> str:
    """Переписка в виде текста для модели-сжимателя."""
    lines = []
    for message in messages:
        who = 'Ассистент' if message.get('sender_role') == 'ai' else 'Пользователь'
        lines.append(f"{who}: {_content(message)}")
    return "\n\n".join(lines)


def chunk_messages(messages: List[Message], cap: int) -> List[List[Message]]:
    """Режет переписку на порции не длиннее cap символов, по границам сообщений.

    Одно сообщение, которое само длиннее cap, уходит отдельной порцией: резать его по
    символам нельзя — потеряется середина показаний.
    """
    if cap <= 0:
        return [list(messages)] if messages else []
    chunks: List[List[Message]] = []
    current: List[Message] = []
    used = 0
    for message in messages:
        size = _size(message)
        if current and used + size > cap:
            chunks.append(current)
            current, used = [], 0
        current.append(message)
        used += size
    if current:
        chunks.append(current)
    return chunks


def digest_enabled() -> bool:
    return bool(getattr(config, 'ASSISTANT_DIGEST_ENABLED', False))


async def _ask_model(prompt: str) -> Optional[str]:
    """Один вызов модели-сжимателя. None — любая неудача (карта не критична)."""
    from google.genai import types

    from rate_limiter import (
        RequestPriority, execute_vertex_request, get_current_client,
        mark_current_region_error, mark_current_region_success,
    )

    model_name = getattr(config, 'ASSISTANT_DIGEST_MODEL_NAME', None) \
        or getattr(config, 'ASSISTANT_ROUTER_MODEL_NAME', 'gemini-3.1-flash-lite')

    async def request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=getattr(config, 'ASSISTANT_DIGEST_MAX_CHARS', 12000) // 2,
            ),
        )

    try:
        response = await execute_vertex_request(
            func=request, model_name=model_name,
            priority=RequestPriority.HIGH, max_retries=2,
        )
        await mark_current_region_success()
    except Exception as e:
        try:
            await mark_current_region_error()
        except Exception:
            pass
        logger.warning("[CASE_DIGEST] модель не собрала карту: %s", e)
        return None

    text = (getattr(response, 'text', None) or '').strip()
    return text or None


async def generate_digest(
    messages: List[Message],
    *,
    previous_digest: str = '',
    role_label: str = '',
    stage_label: str = '',
) -> tuple[Optional[str], List[Message]]:
    """Строит карту по порциям переписки.

    Возвращает (текст карты, реально покрытые сообщения). Покрытые важны отдельно: за один
    ход обрабатывается ограниченное число порций, и в карту нельзя записать покрытие больше
    фактического — иначе непокрытая часть дела молча исчезнет из контекста навсегда.
    (None, []) — не получилось вообще, вызывающий уходит на грубую обрезку.
    """
    if not messages:
        return None, []

    cap = getattr(config, 'ASSISTANT_DIGEST_SOURCE_MAX_CHARS', 250000) or 250000
    max_passes = max(1, int(getattr(config, 'ASSISTANT_DIGEST_MAX_PASSES', 2) or 2))
    all_chunks = chunk_messages(messages, cap)
    chunks = all_chunks[:max_passes]
    if len(all_chunks) > len(chunks):
        logger.info(
            "[CASE_DIGEST] переписка не влезла в %s проход(ов): в карту пойдёт %s из %s порций, "
            "остальное попадёт в неё следующим ходом", max_passes, len(chunks), len(all_chunks),
        )

    digest = (previous_digest or '').strip()
    covered: List[Message] = []
    for chunk in chunks:
        prompt = get_case_digest_prompt(
            role_label=role_label or 'участник дела',
            stage_label=stage_label or 'не указана',
            previous_digest=digest,
            transcript=render_transcript(chunk),
        )
        text = await _ask_model(prompt)
        if not text:
            # Часть уже собрана — отдаём её, это лучше, чем откат к грубой обрезке.
            break
        digest = text
        covered.extend(chunk)

    if not covered:
        return None, []

    limit = getattr(config, 'ASSISTANT_DIGEST_MAX_CHARS', 12000) or 12000
    if len(digest) > limit:
        digest = digest[:limit].rstrip() + '…'
    logger.info("[CASE_DIGEST] карта собрана: покрыто %s сообщений (%s симв.), карта %s симв.",
                len(covered), _total(covered), len(digest))
    return (digest or None), covered


# --- Публичная точка входа ---------------------------------------------------------------

async def build_case_messages(
    case_id: int,
    history: List[Message],
    *,
    rp_role: Optional[str] = None,
    case_stage: Optional[str] = None,
) -> List[Message]:
    """История дела в том виде, в каком её надо отдать модели.

    Короткое дело возвращается как есть (без единого обращения к БД). Длинное — фабула,
    карта дела и свежий хвост. Любая проблема с картой означает откат на прежнюю обрезку,
    а не отказ в ответе.
    """
    limit = getattr(config, 'ASSISTANT_HISTORY_MAX_CHARS', 0) or 0
    if limit <= 0 or not history or _total(history) <= limit:
        return history

    if not digest_enabled() or not _has_ids(history):
        return trim_history(history, case_id)

    try:
        stored = await assistant_logger.get_case_digest(case_id)
    except Exception as e:
        logger.warning("[CASE_DIGEST] дело #%s: карта не прочиталась (%s)", case_id, e)
        stored = None

    plan = plan_case_context(history, stored)

    if plan.mode in ('full', 'trim'):
        return trim_history(history, case_id) if plan.mode == 'trim' else list(history)

    if plan.mode == 'digest':
        covered = len([m for m in history
                       if _mid(m) <= int((stored or {}).get('covered_message_id') or 0)])
        return assemble(plan, covered_count=covered)

    # mode == 'refresh': собираем/обновляем карту.
    roles = getattr(config, 'ASSISTANT_ROLES', {}) or {}
    stages = getattr(config, 'ASSISTANT_STAGES', {}) or {}
    try:
        text, covered = await generate_digest(
            plan.to_summarize,
            previous_digest=((stored or {}).get('digest') or ''),
            role_label=roles.get(rp_role, rp_role or ''),
            stage_label=stages.get(case_stage, case_stage or ''),
        )
    except Exception as e:
        logger.warning("[CASE_DIGEST] дело #%s: сжатие упало (%s)", case_id, e)
        text, covered = None, []

    if not text or not covered:
        # Старая карта (если была) всё ещё лучше обрезки и по построению влезает в бюджет.
        if (stored or {}).get('digest'):
            return assemble(plan, digest_text=stored['digest'])
        return trim_history(history, case_id)

    covered_id = _mid(covered[-1])
    covered_count = len([m for m in history if _mid(m) <= covered_id])
    try:
        await assistant_logger.save_case_digest(
            case_id=case_id, digest=text, covered_message_id=covered_id,
            covered_count=covered_count, source_chars=_total(covered),
            model=getattr(config, 'ASSISTANT_DIGEST_MODEL_NAME', None),
        )
    except Exception as e:
        # Не смогли сохранить — карту всё равно используем в этом запросе.
        logger.warning("[CASE_DIGEST] дело #%s: карта не сохранилась (%s)", case_id, e)

    # Пересчитываем раскрой под фактическую карту: она обычно меньше зарезервированного места,
    # и в освободившийся бюджет влезет больше сырых сообщений (в т.ч. те, что не успели попасть
    # в карту за отведённые проходы).
    fresh_plan = plan_case_context(
        history,
        {'digest': text, 'covered_message_id': covered_id, 'covered_count': covered_count},
    )
    if fresh_plan.mode == 'full':
        return list(history)
    return assemble(fresh_plan, digest_text=text, covered_count=covered_count)


# END OF FILE services/case_digest.py
