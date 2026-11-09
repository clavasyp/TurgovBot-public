# START OF FILE services/law_loophole/analysis_cache.py

"""
Кэш результатов дорогих анализов «весь свод» (full_server).

Идея: full_server-анализ детерминирован (нет текста запроса, берётся весь свод),
поэтому результат можно посчитать один раз и переиспользовать. Отпечаток (fingerprint)
включает версию свода (хэш content_hash всех законов сервера) — при изменении законки
отпечаток меняется и кэш автоматически инвалидируется.

Кэшируется ТОЛЬКО full_server (по решению заказчика). Прочие режимы (auto_rag/manual/hybrid)
не кэшируются — compute_fingerprint вернёт None.

Вкл/выкл кэширования — флаг в system_cache (по умолчанию ВКЛ), управляется из админки.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from database.manager import db_manager

logger = logging.getLogger(__name__)

_CACHE_TOGGLE_KEY = "loophole_cache_enabled"


def result_findings_count(result: Optional[Dict[str, Any]]) -> int:
    """Сколько «дыр» в результате анализа (единый источник истины для «0 дыр»).
    Берём summary.total_findings, иначе len(findings). Не-dict/None → 0."""
    if not isinstance(result, dict):
        return 0
    summ = result.get("summary") or {}
    tf = summ.get("total_findings")
    if tf is not None:
        try:
            return int(tf)
        except (ValueError, TypeError):
            pass
    return len(result.get("findings") or [])


# ---------------------------------------------------------------------------
# Вкл/выкл кэширования (флаг в system_cache)
# ---------------------------------------------------------------------------

async def is_caching_enabled() -> bool:
    """По умолчанию ВКЛ (если флаг не выставлен)."""
    try:
        val = await db_manager.fetchval(
            "SELECT value FROM system_cache WHERE key = $1", _CACHE_TOGGLE_KEY
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


async def set_caching_enabled(enabled: bool) -> None:
    await db_manager.execute(
        """
        INSERT INTO system_cache (key, value, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        _CACHE_TOGGLE_KEY, "true" if enabled else "false",
    )


# ---------------------------------------------------------------------------
# Отпечаток full_server-анализа
# ---------------------------------------------------------------------------

async def _corpus_hash(server_id: str) -> str:
    """Хэш версии свода: по content_hash всех законов сервера (как их видит full_server)."""
    rows = await db_manager.fetch(
        "SELECT content_hash FROM law_documents "
        "WHERE server_id = $1 AND gemini_file_uri IS NOT NULL ORDER BY id",
        server_id,
    )
    h = hashlib.sha256()
    for r in rows:
        h.update((str(r["content_hash"] or "")).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:32]


async def compute_fingerprint(
    server_id: str,
    analysis_type: Optional[str],
    profile: Optional[str],
    selection_mode: Optional[str],
) -> Optional[str]:
    """Отпечаток для кэша. Возвращает None, если режим не кэшируется (не full_server)."""
    if selection_mode != "full_server" or not server_id:
        return None
    ch = await _corpus_hash(server_id)
    raw = f"fullserver|{server_id}|{analysis_type}|{profile}|{ch}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Чтение / запись кэша
# ---------------------------------------------------------------------------

async def get_cached_result(fingerprint: Optional[str]) -> Optional[Dict[str, Any]]:
    """Возвращает сохранённый результат (и инкрементит hits), либо None."""
    if not fingerprint:
        return None
    row = await db_manager.fetchrow(
        "UPDATE loophole_analysis_cache SET hits = hits + 1, last_used_at = NOW() "
        "WHERE fingerprint = $1 RETURNING result",
        fingerprint,
    )
    if not row:
        return None
    res = row["result"]
    if isinstance(res, str):
        try:
            return json.loads(res)
        except (ValueError, TypeError):
            return None
    return res


async def save_cached_result(
    fingerprint: Optional[str],
    server_id: str,
    analysis_type: Optional[str],
    profile: Optional[str],
    result: Dict[str, Any],
) -> None:
    if not fingerprint or result is None:
        return
    # НЕ кэшируем нулевые результаты (0 дыр): «0 дыр» нередко = квота/баг, а не реальный итог,
    # и кэш не должен «залипать» на пустом — пусть пересчитывается. В архив (loophole_analysis_results)
    # нулевые при этом ПИШУТСЯ — чтобы разбирать, почему выдало 0 (см. save_analysis_result).
    if result_findings_count(result) == 0:
        logger.info("[LOOPHOLE_CACHE] 0 дыр — результат НЕ кэшируем (fp=%s)", fingerprint[:12])
        return
    ch = await _corpus_hash(server_id) if server_id else None
    await db_manager.execute(
        """
        INSERT INTO loophole_analysis_cache
            (fingerprint, server_id, analysis_type, model_profile, corpus_hash, result)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        ON CONFLICT (fingerprint) DO UPDATE
            SET result = EXCLUDED.result, corpus_hash = EXCLUDED.corpus_hash, last_used_at = NOW()
        """,
        fingerprint, server_id, analysis_type, profile, ch,
        json.dumps(result, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# Админ-управление
# ---------------------------------------------------------------------------

async def cache_count(server_id: Optional[str] = None) -> int:
    if server_id:
        n = await db_manager.fetchval(
            "SELECT COUNT(*) FROM loophole_analysis_cache WHERE server_id = $1", server_id
        )
    else:
        n = await db_manager.fetchval("SELECT COUNT(*) FROM loophole_analysis_cache")
    return int(n or 0)


async def clear_cache(server_id: Optional[str] = None) -> int:
    """Удаляет кэш (по серверу или весь). Возвращает число удалённых записей."""
    if server_id:
        res = await db_manager.execute(
            "DELETE FROM loophole_analysis_cache WHERE server_id = $1", server_id
        )
    else:
        res = await db_manager.execute("DELETE FROM loophole_analysis_cache")
    try:
        return int(str(res).split()[-1])
    except (ValueError, IndexError):
        return 0


async def count_zero_cache() -> int:
    """Сколько в кэше записей с 0 дыр (обычно 0 — нули не кэшируются)."""
    n = await db_manager.fetchval(
        "SELECT COUNT(*) FROM loophole_analysis_cache "
        "WHERE COALESCE((result->'summary'->>'total_findings')::int, "
        "jsonb_array_length(COALESCE(result->'findings','[]'::jsonb))) = 0"
    )
    return int(n or 0)


async def clear_zero_cache() -> int:
    """Удаляет из КЭША записи с 0 дыр (на случай старых). Архив НЕ трогает. Возвращает число удалённых."""
    res = await db_manager.execute(
        "DELETE FROM loophole_analysis_cache "
        "WHERE COALESCE((result->'summary'->>'total_findings')::int, "
        "jsonb_array_length(COALESCE(result->'findings','[]'::jsonb))) = 0"
    )
    try:
        return int(str(res).split()[-1])
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# Архив ВСЕХ анализов (loophole_analysis_results) — просмотр/разбор из админки.
# Пишется на КАЖДЫЙ завершённый анализ (вкл. 0-дырочные), НЕ связан с кэшем.
# ---------------------------------------------------------------------------

async def save_analysis_result(
    job_id: Optional[int],
    user_id: Optional[int],
    chat_id: Optional[int],
    params: Optional[Dict[str, Any]],
    result: Optional[Dict[str, Any]],
    is_out_of_band: bool = False,
) -> None:
    """Архивирует результат анализа (любой тип, вкл. 0 дыр). Ошибки глушим — запись
    в журнал не должна ломать выдачу отчёта пользователю."""
    if result is None:
        return
    p = params or {}
    try:
        await db_manager.execute(
            """
            INSERT INTO loophole_analysis_results
                (job_id, user_id, chat_id, server_id, analysis_type, model_profile,
                 selection_mode, total_findings, result, is_out_of_band)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10)
            """,
            job_id, user_id, chat_id,
            p.get("server_id"), p.get("analysis_type"), p.get("profile"),
            p.get("selection_mode") or p.get("mode"),
            result_findings_count(result),
            json.dumps(result, ensure_ascii=False),
            bool(is_out_of_band),
        )
    except Exception:
        logger.exception("[LOOPHOLE] не удалось записать анализ в архив (job=%s)", job_id)


async def list_analysis_results(limit: int = 20, offset: int = 0,
                                only_zero: bool = False) -> list:
    where = "WHERE total_findings = 0" if only_zero else ""
    rows = await db_manager.fetch(
        f"""
        SELECT id, job_id, user_id, server_id, analysis_type, model_profile,
               selection_mode, total_findings, is_out_of_band, created_at
        FROM loophole_analysis_results
        {where}
        ORDER BY created_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )
    return [dict(r) for r in rows]


async def count_analysis_results(only_zero: bool = False) -> int:
    where = "WHERE total_findings = 0" if only_zero else ""
    n = await db_manager.fetchval(
        f"SELECT COUNT(*) FROM loophole_analysis_results {where}"
    )
    return int(n or 0)


async def get_analysis_result(result_id: int) -> Optional[Dict[str, Any]]:
    """Полная запись архива (включая result JSONB как dict)."""
    row = await db_manager.fetchrow(
        "SELECT * FROM loophole_analysis_results WHERE id = $1", result_id
    )
    if not row:
        return None
    d = dict(row)
    res = d.get("result")
    if isinstance(res, str):
        try:
            d["result"] = json.loads(res)
        except (ValueError, TypeError):
            d["result"] = {}
    return d


# END OF FILE services/law_loophole/analysis_cache.py
