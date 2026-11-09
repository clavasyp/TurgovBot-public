# START OF FILE services/checklists.py

"""
Живые чек-листы процедуры под роль (адвокат/прокурор) для голосового оверлея.

Идея: у каждой роли есть статический чек-лист ключевых пунктов процедуры (из законки/ресёрча).
По ходу диалога (ситуация + rolling-память) LLM оценивает статус каждого пункта:
  ok       — пункт соблюдён/выполнен;
  violation — пункт нарушен / не выполнен (на это обратить внимание);
  unclear  — недостаточно данных.

Грунтуется тем же RAG-контекстом (полные статьи) и тем же риврайтом запросов (rewrite_queries +
retrieve_multi, fail-open на сырой retrieve), что и совет — чтобы оценка опиралась на нормы сервера,
а не на общие знания. Модель — быстрая (Flash), вывод — строгий JSON.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import config
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client
from services.advice_service import rewrite_queries
from services.retrieval_service import retrieve, retrieve_multi, format_chunks_for_prompt
from services.roles import normalize_mode

logger = logging.getLogger(__name__)


# Статические пункты чек-листов (нейтральны к серверу; конкретные сроки/статьи додумывает LLM по нормам).
_ADVOCATE_ITEMS = [
    {"id": "intro", "text": "Адвокат представился и назвал клиента/основание участия"},
    {"id": "grounds", "text": "Получены основания задержания от сотрудника"},
    {"id": "video", "text": "Есть видеозапись процессуальных действий"},
    {"id": "rights", "text": "Зачитаны права (правило Миранды) до объяснений"},
    {"id": "time", "text": "Соблюдён срок задержания / заморозка времени до прибытия адвоката"},
    {"id": "private", "text": "Проведена конфиденциальная встреча с клиентом"},
    {"id": "presence", "text": "Адвокат допущен к присутствию при допросе/обыске"},
    {"id": "violations", "text": "Зафиксированы процессуальные нарушения (если есть)"},
]

_PROSECUTOR_ITEMS = [
    {"id": "grounds", "text": "Достаточные основания задержания"},
    {"id": "procedure", "text": "Соблюдена процедура: зачитаны права, ведётся видеозапись"},
    {"id": "time", "text": "Соблюдены сроки задержания"},
    {"id": "proportion", "text": "Мера пропорциональна нарушению"},
    {"id": "notify", "text": "Для госслужащего: уведомлены структура и прокурор в срок"},
    {"id": "advocate", "text": "Вызван/допущен адвокат (если требовалось)"},
    {"id": "decision", "text": "Сформулировано решение: законно / отменить / устранить нарушения"},
]

_CHECKLISTS = {"advocate": _ADVOCATE_ITEMS, "prosecutor": _PROSECUTOR_ITEMS}

_VALID_STATUS = {"ok", "violation", "unclear"}

_EVAL_SYSTEM = (
    "Ты — юридический ассистент RP-сервера. По описанию ситуации (и контексту диалога) и предоставленным "
    "НОРМАМ сервера оцени КАЖДЫЙ пункт чек-листа процедуры. Для каждого пункта верни статус:\n"
    "- \"ok\" — пункт явно соблюдён/выполнен;\n"
    "- \"violation\" — пункт нарушен или НЕ выполнен (на это обратить внимание игрока);\n"
    "- \"unclear\" — в ситуации нет данных, чтобы судить.\n"
    "Опирайся ТОЛЬКО на предоставленные нормы и факты ситуации, не выдумывай. note — максимум одна короткая строка "
    "по-русски (можно пусто). Верни СТРОГО JSON по схеме, без лишнего текста."
)


def get_checklist(mode: Optional[str]) -> List[Dict[str, str]]:
    """Статические пункты чек-листа под роль (или пусто для general)."""
    return _CHECKLISTS.get(normalize_mode(mode), [])


def _default_result(items: List[Dict[str, str]]) -> Dict[str, Any]:
    return {"items": [{"id": i["id"], "text": i["text"], "status": "unclear", "note": ""} for i in items]}


async def evaluate_checklist(
    server_id: str,
    situation: str,
    *,
    history: Optional[str] = None,
    mode: Optional[str] = None,
    top_k: Optional[int] = None,
) -> Dict[str, Any]:
    """Возвращает {items: [{id, text, status, note}]} с оценкой статусов под роль."""
    items = get_checklist(mode)
    if not items:
        return {"items": []}

    situation = (situation or "").strip()
    if not situation:
        return _default_result(items)

    try:
        # Этап 3: чек-лист получает ТОТ ЖЕ риврайт, что и совет (шумный транскрипт → 1–4
        # юридических запроса, мульти-ретривал с union). Fail-open: пустой риврайт ИЛИ пустой
        # мульти-ретривал → сырой retrieve по ситуации, как раньше.
        queries = await rewrite_queries(situation, history)
        if queries:
            logger.info("[CHECKLIST] риврайт запросов server=%s: %s", server_id, queries)
            chunks = await retrieve_multi(server_id, queries, mode=mode)
        else:
            chunks = []
        if not chunks:
            chunks = await retrieve(server_id, situation, top_k=top_k or config.OVERLAY_RETRIEVAL_TOP_K, mode=mode)
    except Exception:
        logger.exception("[CHECKLIST] retrieve failed")
        chunks = []
    context = format_chunks_for_prompt(chunks) if chunks else "(нормы не найдены)"

    items_for_prompt = "\n".join(f"- {i['id']}: {i['text']}" for i in items)
    parts: List[str] = []
    if history:
        parts.append(f"Контекст диалога (последние реплики):\n{history}\n")
    parts.append(f"Ситуация:\n{situation}\n")
    parts.append(f"Релевантные нормы сервера:\n{context}\n")
    parts.append(f"Пункты чек-листа (id: текст):\n{items_for_prompt}\n")
    parts.append("Оцени каждый пункт и верни JSON.")
    user_prompt = "\n".join(parts)

    from google.genai import types

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "status": {"type": "string", "enum": ["ok", "violation", "unclear"]},
                        "note": {"type": "string"},
                    },
                    "required": ["id", "status"],
                },
            }
        },
        "required": ["items"],
    }

    async def _call():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=config.OVERLAY_ADVICE_MODEL_FAST,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=_EVAL_SYSTEM,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )

    try:
        resp = await execute_vertex_request(
            func=_call, model_name=config.OVERLAY_ADVICE_MODEL_FAST,
            priority=RequestPriority.HIGH, max_retries=1,
        )
        raw = (getattr(resp, "text", "") or "").strip()
        parsed = json.loads(raw) if raw else {}
        by_id = {}
        for it in (parsed.get("items") or []):
            st = (it.get("status") or "").strip().lower()
            if it.get("id") and st in _VALID_STATUS:
                by_id[it["id"]] = {"status": st, "note": (it.get("note") or "").strip()[:200]}
    except Exception:
        logger.exception("[CHECKLIST] eval failed")
        by_id = {}

    # Склеиваем статусы на статические пункты (по умолчанию unclear).
    out = []
    for i in items:
        ev = by_id.get(i["id"], {})
        out.append({
            "id": i["id"],
            "text": i["text"],
            "status": ev.get("status", "unclear"),
            "note": ev.get("note", ""),
        })
    return {"items": out}


# END OF FILE services/checklists.py
