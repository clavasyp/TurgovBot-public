# START OF FILE services/tester_applications.py

"""
Заявки в тестировщики.

Юзер подаёт анкету через deep-link ?start=apply_tester
(user_handlers/tester_application_flow.py), супер-/админ отбирает её в админ-панели
(admin_handlers.py), глядя на метрики активности юзера. Хранилище — таблица
tester_applications (alembic 010).

Приём заявок включается/выключается персистентным флагом в system_cache — по тому же
шаблону, что фича-тумблер анализатора (queue.is_loophole_enabled) и режим тех. работ
(utils.get_maintenance_mode).

Политика повтора: одна активная (pending) заявка на юзера (БД-гарантия — частичный
UNIQUE-индекс uq_tester_apps_pending); после отклонения можно подать снова.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from database.manager import db_manager

logger = logging.getLogger(__name__)

_INTAKE_KEY = "tester_intake_enabled"

# Лимит Telegram на текст сообщения — 4096. Держим запас.
TELEGRAM_TEXT_LIMIT = 3900


def clip_html(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Обрезать ГОТОВУЮ HTML-строку до limit символов, не оставляя «рваную»
    HTML-сущность (&...;) в конце — иначе Telegram бросит BadRequest при разборе.

    Применять к уже собранному (с html.escape) тексту: ответы юзера произвольной
    длины + экранирование могут раздуть сообщение за лимит 4096."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    amp = cut.rfind('&')
    # если последний '&' не закрыт ';' — это обрезанная сущность, отрезаем её
    if amp != -1 and ';' not in cut[amp:]:
        cut = cut[:amp]
    return cut + "…"


# ---------------------------------------------------------------------------
# Тумблер приёма заявок (персистентно, system_cache)
# ---------------------------------------------------------------------------

async def is_intake_enabled() -> bool:
    """Открыт ли приём заявок в тестировщики. По умолчанию ВКЛ."""
    try:
        val = await db_manager.fetchval(
            "SELECT value FROM system_cache WHERE key = $1", _INTAKE_KEY
        )
    except Exception:
        return True
    if val is None:
        return True
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().strip('"').lower() not in ("false", "0")
    return bool(val)


async def set_intake_enabled(enabled: bool) -> None:
    """Открыть/закрыть приём заявок (персистентно)."""
    await db_manager.execute(
        """
        INSERT INTO system_cache (key, value, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        _INTAKE_KEY, "true" if enabled else "false",
    )


# ---------------------------------------------------------------------------
# CRUD заявок
# ---------------------------------------------------------------------------

def _row(row) -> Optional[Dict[str, Any]]:
    """asyncpg.Record -> dict с распарсенным answers (JSONB приходит строкой)."""
    if row is None:
        return None
    d = dict(row)
    ans = d.get("answers")
    if isinstance(ans, str):
        try:
            d["answers"] = json.loads(ans)
        except Exception:
            d["answers"] = {}
    elif ans is None:
        d["answers"] = {}
    return d


async def get_pending_application(user_id: int) -> Optional[Dict[str, Any]]:
    """Активная (pending) заявка юзера, если есть — для блокировки дублей."""
    row = await db_manager.fetchrow(
        "SELECT * FROM tester_applications WHERE user_id = $1 AND status = 'pending'",
        user_id,
    )
    return _row(row)


async def create_application(user_id: int, answers: Dict[str, Any]) -> Optional[int]:
    """Создать заявку. Возвращает id или None, если активная заявка уже есть
    (частичный UNIQUE-индекс uq_tester_apps_pending) либо при ошибке."""
    try:
        new_id = await db_manager.fetchval(
            """
            INSERT INTO tester_applications (user_id, answers, status)
            VALUES ($1, $2::jsonb, 'pending')
            RETURNING id
            """,
            user_id, json.dumps(answers, ensure_ascii=False),
        )
        return int(new_id) if new_id is not None else None
    except asyncpg.UniqueViolationError:
        return None
    except Exception:
        logger.exception("[tester_apps] не удалось создать заявку user_id=%s", user_id)
        return None


async def count_pending() -> int:
    try:
        val = await db_manager.fetchval(
            "SELECT COUNT(*) FROM tester_applications WHERE status = 'pending'"
        )
    except Exception:
        return 0
    return int(val or 0)


async def count_applications(only_pending: bool) -> int:
    if only_pending:
        return await count_pending()
    try:
        val = await db_manager.fetchval("SELECT COUNT(*) FROM tester_applications")
    except Exception:
        return 0
    return int(val or 0)


async def list_applications(only_pending: bool, offset: int, limit: int) -> List[Dict[str, Any]]:
    """Страница заявок. pending — старые сверху (FIFO, кого раньше отбирать),
    все — свежие сверху."""
    if only_pending:
        rows = await db_manager.fetch(
            """
            SELECT * FROM tester_applications
            WHERE status = 'pending'
            ORDER BY created_at ASC
            OFFSET $1 LIMIT $2
            """,
            offset, limit,
        )
    else:
        rows = await db_manager.fetch(
            """
            SELECT * FROM tester_applications
            ORDER BY created_at DESC
            OFFSET $1 LIMIT $2
            """,
            offset, limit,
        )
    return [_row(r) for r in rows]


async def get_application(app_id: int) -> Optional[Dict[str, Any]]:
    row = await db_manager.fetchrow(
        "SELECT * FROM tester_applications WHERE id = $1", app_id
    )
    return _row(row)


async def set_application_status(app_id: int, status: str, reviewed_by: int) -> Optional[Dict[str, Any]]:
    """Перевести заявку pending -> approved/rejected. Транзит только из pending
    (защита от двойной обработки). Возвращает обновлённую строку (с user_id для
    уведомления/выдачи роли) или None, если заявка уже обработана/не найдена."""
    row = await db_manager.fetchrow(
        """
        UPDATE tester_applications
        SET status = $2, reviewed_by = $3, reviewed_at = NOW()
        WHERE id = $1 AND status = 'pending'
        RETURNING *
        """,
        app_id, status, reviewed_by,
    )
    return _row(row)


# ---------------------------------------------------------------------------
# Метрики активности юзера (для карточки заявки)
# ---------------------------------------------------------------------------

async def gather_user_metrics(user_id: int) -> Dict[str, Any]:
    """Свод метрик активности юзера для отбора. Каждый счётчик — отдельным запросом
    с try/except: отсутствие/переименование таблицы не должно валить весь свод
    (покажет 0). Считается вживую при просмотре заявки (свежие данные)."""
    m: Dict[str, Any] = {}
    try:
        u = await db_manager.fetchrow(
            "SELECT full_name, username, first_seen, last_seen, is_vip, is_subscriber, "
            "is_banned, is_blocked, is_tester, is_admin FROM users WHERE user_id = $1",
            user_id,
        )
        if u:
            m.update(dict(u))
    except Exception:
        logger.exception("[tester_apps] не удалось прочитать users для %s", user_id)

    async def _count(sql: str) -> int:
        try:
            return int(await db_manager.fetchval(sql, user_id) or 0)
        except Exception:
            return 0

    m["questions_total"] = await _count(
        "SELECT COUNT(*) FROM message_history WHERE user_id = $1 AND role = 'user'"
    )
    m["court_cases"] = await _count("SELECT COUNT(*) FROM court_cases WHERE user_id = $1")
    m["complaints"] = await _count("SELECT COUNT(*) FROM complaints_history WHERE user_id = $1")
    m["assistant_cases"] = await _count("SELECT COUNT(*) FROM assistant_cases WHERE user_id = $1")
    m["loophole_done"] = await _count(
        "SELECT COUNT(*) FROM loophole_jobs WHERE user_id = $1 AND status = 'done'"
    )
    m["criminal_cases"] = await _count(
        "SELECT COUNT(*) FROM criminal_case_sessions WHERE user_id = $1"
    )
    m["reports"] = await _count("SELECT COUNT(*) FROM reports WHERE user_id = $1")

    # Сегодняшние вопросы (user_usage сбрасывается в полночь UTC; сравнение как в
    # user_handlers.core.personal_account).
    try:
        today = await db_manager.fetchval(
            "SELECT question_reqs FROM user_usage WHERE user_id = $1 AND usage_date = CURRENT_DATE",
            user_id,
        )
        m["questions_today"] = int(today or 0)
    except Exception:
        m["questions_today"] = 0

    return m


def format_metrics_block(m: Dict[str, Any]) -> str:
    """HTML-блок метрик для карточки заявки."""
    flags = []
    if m.get("is_admin"):
        flags.append("👑 админ")
    if m.get("is_tester"):
        flags.append("🧪 уже тестер")
    if m.get("is_vip"):
        flags.append("💎 VIP")
    if m.get("is_subscriber"):
        flags.append("⭐ подписчик")
    if m.get("is_banned"):
        flags.append("🚫 бан")
    if m.get("is_blocked"):
        flags.append("⛔ заблокировал бота")
    flags_s = ", ".join(flags) if flags else "обычный пользователь"

    def _fmt_date(dt) -> str:
        try:
            return dt.strftime("%d.%m.%Y")
        except Exception:
            return "?"

    def _fmt_dt(dt) -> str:
        try:
            return dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return "?"

    first_seen = m.get("first_seen")
    age_s = ""
    if first_seen:
        try:
            days = (datetime.now(timezone.utc) - first_seen).days
            age_s = f" ({days} дн. назад)"
        except Exception:
            age_s = ""

    return "\n".join([
        "📊 <b>Активность в боте</b>",
        f"Статус: {flags_s}",
        f"Регистрация: {_fmt_date(first_seen)}{age_s}",
        f"Последняя активность: {_fmt_dt(m.get('last_seen'))}",
        f"Вопросов Q&A: <b>{m.get('questions_total', 0)}</b> (сегодня {m.get('questions_today', 0)})",
        f"Судебных дел: <b>{m.get('court_cases', 0)}</b>",
        f"Жалоб сгенерировано: <b>{m.get('complaints', 0)}</b>",
        f"RP-ассистент (кейсы): <b>{m.get('assistant_cases', 0)}</b>",
        f"Анализов законов: <b>{m.get('loophole_done', 0)}</b>",
        f"Уголовных дел: <b>{m.get('criminal_cases', 0)}</b>",
        f"Репортов: <b>{m.get('reports', 0)}</b>",
    ])


# END OF FILE services/tester_applications.py
