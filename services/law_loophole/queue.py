# START OF FILE services/law_loophole/queue.py

"""
Очередь анализатора дыр в законке.

Содержит:
  * Репозиторий очереди (CRUD над таблицей loophole_jobs на db_manager/asyncpg).
  * Исполнитель анализа run_loophole_analysis(bot, params, chat_id) — перенесён из
    user_handlers.test_loophole_flow._run_analysis_background (context.bot -> bot).
  * Класс LoopholeQueueWorker — единственный долгоживущий воркер основной очереди
    (concurrency = config.LOOPHOLE_QUEUE_CONCURRENCY, обычно 1, без preemption).
  * admin_run_out_of_band(...) — параллельный запуск «без очереди» (is_out_of_band).

Порядок очереди (канонический, используется и при выборке, и при подсчёте позиции):
    COALESCE(manual_position, BIG) ASC, priority DESC, created_at ASC
  — задачи с ручной позицией от админа идут первыми (по возрастанию позиции),
    остальные — по приоритету (VIP/админ выше) и времени (FIFO).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

import config
from database.manager import db_manager

from services.law_loophole.bundle import build_law_context_bundle
from services.law_loophole.analyzer import (
    analyze_loopholes,
    make_vertex_model_call,
    LoopholeModelUnavailable,
)
from services.law_loophole.model_policy import get_loophole_models, reconcile_bundle_profile
from services.law_loophole.report import (
    format_loophole_report,
    format_loophole_summary,
    build_loophole_report_html_doc,
)
from services.law_loophole.analysis_cache import (
    compute_fingerprint,
    save_cached_result,
    is_caching_enabled,
    save_analysis_result,
    result_findings_count,
)
from api.prompts.law_loopholes import get_loophole_router_prompt

logger = logging.getLogger(__name__)

# «Бесконечность» для COALESCE(manual_position) — задачи без ручной позиции в конец.
_MP_INF = 2147483647

# Канонический ORDER BY очереди (без ключевого слова ORDER BY).
_QUEUE_ORDER = (
    "COALESCE(manual_position, 2147483647) ASC, priority DESC, created_at ASC"
)


# ---------------------------------------------------------------------------
# Сериализация строк
# ---------------------------------------------------------------------------

def _row_to_job(row) -> Optional[Dict[str, Any]]:
    """asyncpg.Record -> dict с распарсенным params (JSONB приходит строкой)."""
    if row is None:
        return None
    job = dict(row)
    params = job.get("params")
    if isinstance(params, str):
        try:
            job["params"] = json.loads(params)
        except (ValueError, TypeError):
            job["params"] = {}
    return job


# ---------------------------------------------------------------------------
# Репозиторий очереди
# ---------------------------------------------------------------------------

async def enqueue_job(
    user_id: int,
    chat_id: int,
    params: Dict[str, Any],
    priority: int,
    is_out_of_band: bool = False,
    status: str = "queued",
    fingerprint: Optional[str] = None,
) -> int:
    """Создаёт задачу в очереди. Возвращает её id.

    fingerprint — отпечаток full_server-анализа (для кэша/дедупа); None для прочих.

    Может бросить asyncpg.UniqueViolationError, если у пользователя уже есть
    активная (queued/running, не out-of-band) задача — это БД-гарантия требования
    «1 активный запрос на пользователя» (частичный уникальный индекс).
    """
    started_at_sql = "NOW()" if status == "running" else "NULL"
    job_id = await db_manager.fetchval(
        f"""
        INSERT INTO loophole_jobs
            (user_id, chat_id, status, priority, params, is_out_of_band, started_at, fingerprint)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, {started_at_sql}, $7)
        RETURNING id
        """,
        user_id, chat_id, status, priority, json.dumps(params), is_out_of_band, fingerprint,
    )
    return int(job_id)


async def get_active_job_for_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Активная (queued/running, не out-of-band) задача пользователя, если есть."""
    row = await db_manager.fetchrow(
        """
        SELECT * FROM loophole_jobs
        WHERE user_id = $1 AND is_out_of_band = false
          AND status IN ('queued','running')
        LIMIT 1
        """,
        user_id,
    )
    return _row_to_job(row)


async def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    row = await db_manager.fetchrow("SELECT * FROM loophole_jobs WHERE id = $1", job_id)
    return _row_to_job(row)


# ---------------------------------------------------------------------------
# Пауза очереди (для безопасного рестарта бота)
# ---------------------------------------------------------------------------
# Флаг хранится в существующей kv-таблице system_cache (переживает рестарт бота),
# чтобы после перезапуска воркер не начал разгребать очередь, пока админ не возобновит.

_PAUSE_KEY = "loophole_queue_paused"


async def is_queue_paused() -> bool:
    """Стоит ли основная очередь на паузе (новые задачи не берутся в работу)."""
    try:
        val = await db_manager.fetchval(
            "SELECT value FROM system_cache WHERE key = $1", _PAUSE_KEY
        )
    except Exception:
        # Если таблицы/строки нет или ошибка чтения — считаем, что не на паузе.
        return False
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().strip('"').lower() in ("true", "1")
    return bool(val)


async def set_queue_paused(paused: bool) -> None:
    """Включает/выключает паузу очереди (персистентно)."""
    await db_manager.execute(
        """
        INSERT INTO system_cache (key, value, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        _PAUSE_KEY, "true" if paused else "false",
    )


_FEATURE_KEY = "loophole_feature_enabled"


async def is_loophole_enabled() -> bool:
    """Включён ли «Анализатор законов» как функция (кнопка у юзеров + вход). По умолчанию ВКЛ.
    Это НЕ пауза очереди: тут речь о видимости/доступности фичи пользователям."""
    try:
        val = await db_manager.fetchval(
            "SELECT value FROM system_cache WHERE key = $1", _FEATURE_KEY
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


async def set_loophole_enabled(enabled: bool) -> None:
    """Включает/выключает фичу «Анализатор законов» (персистентно)."""
    await db_manager.execute(
        """
        INSERT INTO system_cache (key, value, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        _FEATURE_KEY, "true" if enabled else "false",
    )


async def claim_next_job() -> Optional[Dict[str, Any]]:
    """Атомарно берёт следующую задачу основной очереди в работу.

    Возвращает захваченную задачу (status уже 'running') или None, если нет
    свободного слота concurrency либо очередь пуста. Безопасно к гонке с
    админскими действиями (отмена/перестановка): захват делается условным
    UPDATE ... WHERE status='queued'.
    """
    while True:
        # Пауза: новые задачи не берём, уже выполняющиеся продолжают идти.
        if await is_queue_paused():
            return None

        running = await db_manager.fetchval(
            "SELECT COUNT(*) FROM loophole_jobs "
            "WHERE status = 'running' AND is_out_of_band = false"
        )
        if (running or 0) >= config.LOOPHOLE_QUEUE_CONCURRENCY:
            return None

        candidate = await db_manager.fetchrow(
            f"""
            SELECT id FROM loophole_jobs
            WHERE status = 'queued' AND is_out_of_band = false
            ORDER BY {_QUEUE_ORDER}
            LIMIT 1
            """
        )
        if candidate is None:
            return None

        claimed = await db_manager.fetchrow(
            """
            UPDATE loophole_jobs
            SET status = 'running', started_at = NOW(), attempts = attempts + 1,
                first_started_at = COALESCE(first_started_at, NOW())
            WHERE id = $1 AND status = 'queued'
            RETURNING *
            """,
            candidate["id"],
        )
        if claimed is not None:
            return _row_to_job(claimed)
        # Гонка: задачу успели отменить/изменить между SELECT и UPDATE — берём следующую.


async def get_queue_position(job_id: int) -> int:
    """Позиция задачи в очереди (1 = следующая на выполнение).

    Для running-задачи вернёт 0. Считает только queued (не out-of-band) задачи,
    стоящие строго раньше данной по каноническому порядку.
    """
    job = await db_manager.fetchrow(
        "SELECT status, priority, manual_position, created_at FROM loophole_jobs WHERE id = $1",
        job_id,
    )
    if job is None:
        return 0
    if job["status"] != "queued":
        return 0

    ahead = await db_manager.fetchval(
        """
        SELECT COUNT(*) FROM loophole_jobs q
        WHERE q.status = 'queued' AND q.is_out_of_band = false
          AND (
            COALESCE(q.manual_position, $1) < COALESCE($2::int, $1)
            OR (COALESCE(q.manual_position, $1) = COALESCE($2::int, $1) AND q.priority > $3)
            OR (COALESCE(q.manual_position, $1) = COALESCE($2::int, $1) AND q.priority = $3 AND q.created_at < $4)
          )
        """,
        _MP_INF, job["manual_position"], job["priority"], job["created_at"],
    )
    return int(ahead or 0) + 1


async def set_position_msg_id(job_id: int, msg_id: Optional[int]) -> None:
    await db_manager.execute(
        "UPDATE loophole_jobs SET position_msg_id = $2 WHERE id = $1", job_id, msg_id
    )


async def mark_job_done(job_id: int) -> None:
    await db_manager.execute(
        "UPDATE loophole_jobs SET status = 'done', finished_at = NOW() WHERE id = $1",
        job_id,
    )


async def mark_job_failed(job_id: int, error: str) -> None:
    await db_manager.execute(
        "UPDATE loophole_jobs SET status = 'failed', error = $2, finished_at = NOW() WHERE id = $1",
        job_id, (error or "")[:2000],
    )


async def requeue_job(job_id: int) -> None:
    """Вернуть running-задачу обратно в очередь (повтор после сбоя/рестарта)."""
    await db_manager.execute(
        "UPDATE loophole_jobs SET status = 'queued', started_at = NULL WHERE id = $1",
        job_id,
    )


async def requeue_job_quota(job_id: int) -> None:
    """Молчаливый повтор задачи при исчерпании квоты (429): возвращаем в очередь и
    НЕЙТРАЛИЗУЕМ инкремент attempts (claim_next_job его повышает) — квота временная,
    задача должна пробовать СКОЛЬКО НУЖНО, не упираясь в LOOPHOLE_MAX_JOB_ATTEMPTS и
    не выдавая юзеру ошибку. Анти-спам обеспечивает cooldown воркера."""
    await db_manager.execute(
        "UPDATE loophole_jobs SET status = 'queued', started_at = NULL, "
        "attempts = GREATEST(attempts - 1, 0) WHERE id = $1",
        job_id,
    )


async def cancel_job(job_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Отменяет задачу, только если она ещё в очереди (running отменить нельзя —
    нет preemption). Возвращает отменённую задачу или None."""
    if user_id is not None:
        row = await db_manager.fetchrow(
            """
            UPDATE loophole_jobs SET status = 'cancelled', finished_at = NOW()
            WHERE id = $1 AND user_id = $2 AND status = 'queued'
            RETURNING *
            """,
            job_id, user_id,
        )
    else:
        row = await db_manager.fetchrow(
            """
            UPDATE loophole_jobs SET status = 'cancelled', finished_at = NOW()
            WHERE id = $1 AND status = 'queued'
            RETURNING *
            """,
            job_id,
        )
    return _row_to_job(row)


async def list_queued_jobs() -> List[Dict[str, Any]]:
    """Очередь (queued, не out-of-band) в каноническом порядке."""
    rows = await db_manager.fetch(
        f"""
        SELECT * FROM loophole_jobs
        WHERE status = 'queued' AND is_out_of_band = false
        ORDER BY {_QUEUE_ORDER}
        """
    )
    return [_row_to_job(r) for r in rows]


async def list_running_jobs(out_of_band: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Выполняющиеся задачи. out_of_band=None — все, True/False — фильтр."""
    if out_of_band is None:
        rows = await db_manager.fetch(
            "SELECT * FROM loophole_jobs WHERE status = 'running' ORDER BY started_at ASC"
        )
    else:
        rows = await db_manager.fetch(
            "SELECT * FROM loophole_jobs WHERE status = 'running' AND is_out_of_band = $1 "
            "ORDER BY started_at ASC",
            out_of_band,
        )
    return [_row_to_job(r) for r in rows]


async def set_job_position(job_id: int, target_pos: int) -> None:
    """Перемещает queued-задачу на абсолютную позицию target_pos (1-based) и
    проставляет manual_position = 1..k всем queued-задачам по новому порядку.

    После вмешательства админа порядок становится явным (manual_position у всех
    текущих queued). Новые входящие задачи (manual_position = NULL) встают после.
    """
    ordered = await list_queued_jobs()
    ids = [j["id"] for j in ordered]
    if job_id not in ids:
        return
    ids.remove(job_id)
    target_pos = max(1, min(target_pos, len(ids) + 1))
    ids.insert(target_pos - 1, job_id)
    # Батч-присвоение manual_position по новому порядку.
    for idx, jid in enumerate(ids, start=1):
        await db_manager.execute(
            "UPDATE loophole_jobs SET manual_position = $2 WHERE id = $1", jid, idx
        )


async def move_job_to_top(job_id: int) -> None:
    await set_job_position(job_id, 1)


async def get_current_position_in_list(job_id: int) -> int:
    """Текущая 1-based позиция задачи среди queued (для up/down)."""
    ordered = await list_queued_jobs()
    for idx, j in enumerate(ordered, start=1):
        if j["id"] == job_id:
            return idx
    return 0


async def recover_orphaned_jobs() -> None:
    """Восстановление после рестарта (вызывать в post_init ДО старта воркера):
      * основная очередь: зависшие running -> queued (повторим);
      * out-of-band running -> failed (их таски умерли с процессом, не реентерабельны).
    """
    requeued = await db_manager.execute(
        "UPDATE loophole_jobs SET status = 'queued', started_at = NULL "
        "WHERE status = 'running' AND is_out_of_band = false"
    )
    failed = await db_manager.execute(
        "UPDATE loophole_jobs SET status = 'failed', error = 'Прервано рестартом бота', "
        "finished_at = NOW() WHERE status = 'running' AND is_out_of_band = true"
    )
    logger.info("[LOOPHOLE_QUEUE] recovery: requeued=%s, oob_failed=%s", requeued, failed)


async def find_duplicate_queued_jobs(fingerprint: str, exclude_job_id: int) -> List[Dict[str, Any]]:
    """Queued-задачи с тем же отпечатком (идентичный full_server-анализ), кроме исходной."""
    if not fingerprint:
        return []
    rows = await db_manager.fetch(
        "SELECT * FROM loophole_jobs WHERE fingerprint = $1 AND status = 'queued' AND id <> $2",
        fingerprint, exclude_job_id,
    )
    return [_row_to_job(r) for r in rows]


async def mark_jobs_done(job_ids: List[int]) -> None:
    if not job_ids:
        return
    await db_manager.execute(
        "UPDATE loophole_jobs SET status = 'done', finished_at = NOW() WHERE id = ANY($1::int[])",
        job_ids,
    )


# ---------------------------------------------------------------------------
# Исполнитель анализа (перенос из _run_analysis_background)
# ---------------------------------------------------------------------------

async def _make_router_fn():
    """Создаёт router_fn, использующую существующий vertex pipeline."""
    from rate_limiter import (
        execute_vertex_request,
        RequestPriority,
        get_current_client,
    )

    async def router_fn(user_request: str, idx_pairs):
        if not idx_pairs:
            return set()
        law_titles = "\n".join(f"{idx}. {title}" for idx, title in idx_pairs)
        prompt = get_loophole_router_prompt(
            user_request=user_request,
            law_titles=law_titles,
            analysis_type="loophole",
        )

        async def _call():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=config.LOOPHOLE_ROUTER_MODEL_BALANCED,
                contents=prompt,
            )

        try:
            response = await execute_vertex_request(
                func=_call,
                model_name=config.LOOPHOLE_ROUTER_MODEL_BALANCED,
                priority=RequestPriority.NORMAL,
                max_retries=2,
            )
            text = getattr(response, "text", "") or ""
            return {int(line.strip()) for line in text.splitlines() if line.strip().isdigit()}
        except Exception as e:
            logger.warning("[LOOPHOLE] router_fn error: %s", e)
            return set()

    return router_fn


async def run_loophole_analysis(bot, params: Dict[str, Any], chat_id: int,
                                fallback_model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Тяжёлая работа: bundle -> analyze -> отправка отчёта. Без зависимости от
    per-update context (нужен только bot). Сама шлёт пользователю отчёт/ошибки и
    не бросает наружу обычные ошибки пайплайна.

    Возвращает отчёт (dict) при успехе — чтобы вызывающий мог его закэшировать;
    None — при ошибке (пользователю уже отправлено сообщение об ошибке).
    """
    try:
        mode = params["mode"]
        profile = params.get("profile", "balanced")
        analysis_type = params.get("analysis_type", "quick_scan")
        server_id = params["server_id"]
        project_id = params.get("project_id")
        user_request = params.get("user_request")
        manual_law_ids = params.get("manual_law_ids")

        router_fn = await _make_router_fn() if mode in ("auto_rag", "hybrid") else None

        bundle = await build_law_context_bundle(
            server_id=server_id,
            project_id=project_id,
            selection_mode=mode,
            model_profile=profile,
            user_request=user_request,
            manual_law_ids=manual_law_ids,
            router_fn=router_fn,
        )
        reconcile_bundle_profile(bundle)
        models = get_loophole_models(bundle.model_profile)

        # Временный фолбэк: если задание слишком долго не может пробиться сквозь 429 на
        # pro-модели — выполняем анализ на flash, чтобы юзер получил результат. Подменяем
        # analyzer/synthesis (тяжёлый pro-путь); router (flash-lite) оставляем как есть.
        if fallback_model:
            models = replace(models, analyzer_model=fallback_model, synthesis_model=fallback_model)
            logger.warning("[LOOPHOLE] FALLBACK: задание долго висело на pro → анализ на %s", fallback_model)

        logger.info(
            "[LOOPHOLE] run: mode=%s, profile=%s (req=%s), docs=%d, size=%d, batch=%s, analyzer=%s",
            bundle.selection_mode, bundle.model_profile, profile,
            len(bundle.selected_laws), bundle.estimated_size, bundle.requires_batching,
            models.analyzer_model,
        )

        report = await analyze_loopholes(
            bundle=bundle,
            analysis_type=analysis_type,
            models=models,
            model_call=make_vertex_model_call,
        )
    except ValueError as e:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠ Ошибка ввода: {e}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
                ]]),
            )
        except Exception:
            logger.exception("[LOOPHOLE] failed to send ValueError message")
        return None
    except LoopholeModelUnavailable:
        # Квотный сбой (429). НИЧЕГО не шлём юзеру (он остаётся в очереди) и НЕ выдаём
        # пустой отчёт. Пробрасываем — _execute_job молча повторит задачу после cooldown.
        logger.warning("[LOOPHOLE] модель недоступна (квота) — молчаливый повтор задачи")
        raise
    except Exception as e:
        logger.exception("[LOOPHOLE] critical error in loophole pipeline")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ Внутренняя ошибка анализатора: {e}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
                ]]),
            )
        except Exception:
            logger.exception("[LOOPHOLE] failed to send error message")
        return None

    await send_loophole_report(bot, chat_id, report)
    return report


async def send_loophole_report(bot, chat_id: int, report: Dict[str, Any],
                               from_cache: bool = False) -> None:
    """Отправляет готовый отчёт анализатора в чат (используется и воркером, и при
    выдаче из кэша / fan-out на дубли очереди)."""
    if from_cache:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="✅ Такой анализ уже выполнялся — отправляю готовый результат.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    async def _send_messages() -> None:
        for msg in format_loophole_report(report):
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning("[LOOPHOLE] send report part failed: %s", e)
                try:
                    await bot.send_message(chat_id=chat_id, text=msg[:3500])
                except Exception:
                    logger.exception("[LOOPHOLE] failed to send fallback message")

    n = len(report.get("findings") or [])
    threshold = int(getattr(config, "LOOPHOLE_DOC_THRESHOLD", 3) or 3)

    if n > threshold:
        # Много дыр → краткая сводка сообщением + оформленный HTML-документ (вместо кучи сообщений).
        try:
            await bot.send_message(chat_id=chat_id, text=format_loophole_summary(report),
                                   parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("[LOOPHOLE] send summary failed: %s", e)
        try:
            buf = io.BytesIO(build_loophole_report_html_doc(report))
            buf.name = "loophole_report.html"
            await bot.send_document(
                chat_id=chat_id, document=buf, filename="loophole_report.html",
                caption=f"📄 Полный отчёт: {n} уязвимостей. Открой в браузере.",
            )
        except Exception as e:
            # Фолбэк: если документ не ушёл — отдать привычными сообщениями.
            logger.warning("[LOOPHOLE] HTML-документ не отправлен (%s) — фолбэк на сообщения", e)
            await _send_messages()
    else:
        # Мало дыр (или 0) → привычными сообщениями.
        await _send_messages()

    try:
        await bot.send_message(
            chat_id=chat_id,
            text="✅ Анализ завершён. Чтобы задать вопрос или выбрать другое действие — вернитесь в главное меню.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
            ]]),
        )
    except Exception:
        logger.warning("[LOOPHOLE] failed to send final main_menu hint", exc_info=True)


async def cache_and_fanout(bot, source_job_id: int, fingerprint: Optional[str],
                           params: Dict[str, Any], report: Optional[Dict[str, Any]]) -> None:
    """Сохраняет результат в кэш и гасит идентичные queued-дубли, отдавая им готовый
    отчёт (fan-out). Вызывается после успешного завершения full_server-анализа."""
    if not fingerprint or report is None:
        return
    try:
        await save_cached_result(
            fingerprint, params.get("server_id"),
            params.get("analysis_type"), params.get("profile"), report,
        )
    except Exception:
        logger.exception("[LOOPHOLE_QUEUE] не удалось сохранить результат в кэш")

    try:
        dups = await find_duplicate_queued_jobs(fingerprint, exclude_job_id=source_job_id)
        if dups:
            await mark_jobs_done([d["id"] for d in dups])
            for d in dups:
                try:
                    await send_loophole_report(bot, d["chat_id"], report, from_cache=True)
                except Exception:
                    logger.warning("[LOOPHOLE_QUEUE] fan-out: не удалось отправить дублю job=%s", d["id"])
            logger.info("[LOOPHOLE_QUEUE] fan-out: погашено дублей очереди=%d", len(dups))
    except Exception:
        logger.exception("[LOOPHOLE_QUEUE] ошибка fan-out по дублям")


async def _notify_superadmin_zero(bot, job_id: int, params: Optional[Dict[str, Any]],
                                  user_id: Optional[int], is_oob: bool) -> None:
    """Алерт супер-админу: анализ вернул 0 уязвимостей (нередко квота/баг — повод разобраться)."""
    sa = getattr(config, "SUPER_ADMIN_ID", 0)
    if not sa:
        return
    p = params or {}
    txt = (
        "⚠️ <b>Анализатор дыр вернул 0 уязвимостей</b>\n"
        f"job: <code>{job_id}</code>{' (oob)' if is_oob else ''}\n"
        f"сервер: <code>{p.get('server_id')}</code>\n"
        f"тип: <code>{p.get('analysis_type')}</code> • профиль: <code>{p.get('profile')}</code>\n"
        f"режим: <code>{p.get('selection_mode') or p.get('mode')}</code>\n"
        f"инициатор: <code>{user_id}</code>\n"
        "Разбор: админка → 🕳 Очередь анализатора → 📊 Все анализы."
    )
    await bot.send_message(chat_id=sa, text=txt, parse_mode=ParseMode.HTML)


async def _archive_and_alert(bot, job_id: int, user_id: Optional[int], chat_id: Optional[int],
                             params: Optional[Dict[str, Any]], report: Optional[Dict[str, Any]],
                             *, is_oob: bool) -> None:
    """После успешного анализа: (1) пишем результат в архив (ВСЕ, вкл. 0 дыр — чтобы потом
    разбирать); (2) если 0 дыр — алертим супер-админа. Best-effort: сбой не влияет на
    уже отправленный пользователю отчёт. report=None (квота 429) — НИЧЕГО не делаем."""
    if report is None:
        return
    try:
        await save_analysis_result(job_id, user_id, chat_id, params, report, is_out_of_band=is_oob)
    except Exception:
        logger.exception("[LOOPHOLE] архивирование анализа не удалось (job=%s)", job_id)
    try:
        if result_findings_count(report) == 0:
            await _notify_superadmin_zero(bot, job_id, params, user_id, is_oob)
    except Exception:
        logger.exception("[LOOPHOLE] алерт супер-админу о 0 дырах не удался (job=%s)", job_id)


# ---------------------------------------------------------------------------
# Воркер основной очереди
# ---------------------------------------------------------------------------

def _fallback_model_for(job_params: Optional[Dict[str, Any]], started_ts) -> Optional[str]:
    """Вернуть fallback-модель, если pro-задание (expert/deep) уже ДОЛЬШЕ
    LOOPHOLE_PRO_FALLBACK_AFTER_SEC РЕАЛЬНО РАБОТАЕТ (пробует pro и ловит 429), иначе None.

    started_ts — момент ПЕРВОГО старта задания (first_started_at), НЕ постановки в очередь:
    простой в очереди не считается. Так каждый анализ сперва честно пробует pro, и уходит
    на flash только если 30 минут не смог пробиться сквозь 429. На первом запуске
    started_ts≈сейчас → возраст ~0 → pro пробуется обязательно."""
    after = int(getattr(config, "LOOPHOLE_PRO_FALLBACK_AFTER_SEC", 0) or 0)
    fb = getattr(config, "LOOPHOLE_PRO_FALLBACK_MODEL", None)
    if not after or not fb or started_ts is None:
        return None
    if (job_params or {}).get("profile") not in ("expert", "deep"):
        return None  # flash-профили (fast/balanced) и так на flash — фолбэк не нужен
    try:
        age = (datetime.now(timezone.utc) - started_ts).total_seconds()
    except Exception:
        return None
    return fb if age >= after else None


class LoopholeQueueWorker:
    """Единственный долгоживущий воркер основной очереди.

    Запускается в post_init через application.create_task(worker.run()).
    Пробуждается по wake_event (его выставляют enqueue/админ-действия) либо по
    таймауту-фолбэку config.LOOPHOLE_QUEUE_POLL_INTERVAL.
    """

    def __init__(self, application):
        self.application = application
        self.bot = application.bot
        self.wake_event = asyncio.Event()
        self._stopping = False
        self.task = None
        # monotonic-время, до которого воркер на паузе после квотного сбоя (анти-спам 429).
        self._quota_cooldown_until = 0.0
        # job_id, которым уже отправили «анализ начался» — чтобы при квотных повторах
        # НЕ слать это сообщение повторно (молчаливый повтор, без спама пользователю).
        self._notified_jobs: set = set()

    def stop(self) -> None:
        self._stopping = True
        self.wake_event.set()

    async def run(self) -> None:
        logger.info("[LOOPHOLE_QUEUE] worker started (concurrency=%s)",
                    config.LOOPHOLE_QUEUE_CONCURRENCY)
        while not self._stopping:
            try:
                # Анти-спам по квоте: после 429 держим паузу, прежде чем снова дёргать
                # Vertex. Задача всё это время МОЛЧА ждёт в очереди (юзеру не пишем).
                remaining = self._quota_cooldown_until - time.monotonic()
                while remaining > 0 and not self._stopping:
                    await asyncio.sleep(min(remaining, 2.0))
                    remaining = self._quota_cooldown_until - time.monotonic()
                if self._stopping:
                    break

                job = await claim_next_job()
                if job is None:
                    try:
                        await asyncio.wait_for(
                            self.wake_event.wait(),
                            timeout=config.LOOPHOLE_QUEUE_POLL_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        pass
                    self.wake_event.clear()
                    continue
                await self._execute_job(job)
            except asyncio.CancelledError:
                logger.info("[LOOPHOLE_QUEUE] worker cancelled")
                raise
            except Exception:
                logger.exception("[LOOPHOLE_QUEUE] worker loop error")
                await asyncio.sleep(1)  # анти-busy-loop
        logger.info("[LOOPHOLE_QUEUE] worker stopped")

    async def _notify_started(self, job: Dict[str, Any]) -> None:
        """Сообщить пользователю, что его анализ начался (редактируем сообщение
        о позиции, при неудаче — шлём новое)."""
        chat_id = job["chat_id"]
        msg_id = job.get("position_msg_id")
        text = "⏳ Ваш анализ начался. Это может занять некоторое время — отчёт придёт в этот чат."
        if msg_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text, parse_mode=ParseMode.HTML
                )
                return
            except Exception:
                pass
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.warning("[LOOPHOLE_QUEUE] failed to notify start for job %s", job["id"])

    async def _execute_job(self, job: Dict[str, Any]) -> None:
        job_id = job["id"]
        try:
            # «Анализ начался» — РОВНО один раз на задачу (на квотных повторах не спамим).
            if job_id not in self._notified_jobs:
                await self._notify_started(job)
                self._notified_jobs.add(job_id)
            fallback = _fallback_model_for(job.get("params"), job.get("first_started_at"))
            report = await run_loophole_analysis(self.bot, job["params"], job["chat_id"],
                                                 fallback_model=fallback)
            await mark_job_done(job_id)
            self._notified_jobs.discard(job_id)
            if report is not None and job.get("fingerprint"):
                await cache_and_fanout(self.bot, job_id, job.get("fingerprint"), job["params"], report)
            # Архив всех анализов + алерт супер-админу о 0-дырочных (best-effort).
            await _archive_and_alert(self.bot, job_id, job.get("user_id"), job.get("chat_id"),
                                     job["params"], report, is_oob=False)
        except LoopholeModelUnavailable:
            # Квота 429: МОЛЧА возвращаем задачу в очередь (без сжигания attempts, без
            # сообщения юзеру, без выдачи «0 дыр») и берём cooldown — не спамим Vertex.
            # Квота временная, задача дождётся свободного окна и выполнится.
            logger.warning("[LOOPHOLE_QUEUE] job %s: квота 429 — молчаливый повтор после cooldown", job_id)
            await requeue_job_quota(job_id)
            self._quota_cooldown_until = time.monotonic() + float(
                getattr(config, "LOOPHOLE_QUOTA_COOLDOWN_SEC", 60)
            )
        except Exception as e:
            logger.exception("[LOOPHOLE_QUEUE] job %s execution error", job_id)
            self._notified_jobs.discard(job_id)
            attempts = int(job.get("attempts") or 0)
            if attempts >= config.LOOPHOLE_MAX_JOB_ATTEMPTS:
                await mark_job_failed(job_id, str(e))
            else:
                await requeue_job(job_id)
        finally:
            # Сразу проверить следующую задачу.
            self.wake_event.set()


# ---------------------------------------------------------------------------
# Out-of-band запуск (админ, параллельно основной очереди)
# ---------------------------------------------------------------------------

async def admin_run_out_of_band(application, params: Dict[str, Any],
                                admin_user_id: int, chat_id: int) -> int:
    """Запускает анализ параллельно основной очереди (is_out_of_band).
    Для full_server считает отпечаток — результат пойдёт в кэш и погасит дубли очереди."""
    fingerprint = None
    try:
        if params.get("mode") == "full_server" and await is_caching_enabled():
            fingerprint = await compute_fingerprint(
                params.get("server_id"), params.get("analysis_type"),
                params.get("profile"), "full_server",
            )
    except Exception:
        logger.warning("[LOOPHOLE_QUEUE] oob: не удалось посчитать отпечаток", exc_info=True)

    job_id = await enqueue_job(
        admin_user_id, chat_id, params,
        priority=config.LOOPHOLE_PRIORITY_ADMIN,
        is_out_of_band=True, status="running", fingerprint=fingerprint,
    )
    created_at = datetime.now(timezone.utc)  # точка отсчёта для фолбэка pro→flash
    application.create_task(_run_oob_job(application, job_id, params, chat_id, admin_user_id,
                                         fingerprint, created_at))
    return job_id


async def _run_oob_job(application, job_id: int, params: Dict[str, Any], chat_id: int,
                       admin_user_id: Optional[int] = None,
                       fingerprint: Optional[str] = None, created_at=None) -> None:
    try:
        fallback = _fallback_model_for(params, created_at)
        report = await run_loophole_analysis(application.bot, params, chat_id, fallback_model=fallback)
        await mark_job_done(job_id)
        if report is not None and fingerprint:
            await cache_and_fanout(application.bot, job_id, fingerprint, params, report)
        # Архив всех анализов + алерт супер-админу о 0-дырочных (best-effort).
        await _archive_and_alert(application.bot, job_id, admin_user_id, chat_id,
                                 params, report, is_oob=True)
    except LoopholeModelUnavailable:
        # Квота 429: не валим задачу и ничего не шлём — ждём cooldown и перезапускаем
        # себя же (job остаётся running). Квота временная, рано или поздно пройдёт; а когда
        # возраст задания перевалит порог — следующий заход уйдёт на flash-фолбэк.
        logger.warning("[LOOPHOLE_QUEUE] oob job %s: квота 429 — повтор после паузы", job_id)
        await asyncio.sleep(float(getattr(config, "LOOPHOLE_QUOTA_COOLDOWN_SEC", 60)))
        application.create_task(_run_oob_job(application, job_id, params, chat_id, admin_user_id,
                                             fingerprint, created_at))
    except Exception as e:
        logger.exception("[LOOPHOLE_QUEUE] out-of-band job %s failed", job_id)
        await mark_job_failed(job_id, str(e))


# END OF FILE services/law_loophole/queue.py
