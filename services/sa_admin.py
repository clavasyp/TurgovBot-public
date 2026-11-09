# START OF FILE services/sa_admin.py

"""
Горячая смена сервис-аккаунта Vertex/Gemini из админки (ТОЛЬКО суперадмин).

Заказчик часто меняет SA — каждый раз НОВЫЙ GCP-проект (ради свежей квоты Gemini),
поэтому project_id берём ИЗ загруженного .json, а не из config.GCP_PROJECT_ID.

Активный SA — это файл config.VERTEX_CREDENTIALS_FILE (он же для STT). Его перезаписываем
(каталог credentials/ смонтирован на запись). Консьюмеры, читающие файл-путь заново при
каждом вызове (GCS/STT), подхватят новый ключ сами. Кэшированный genai-клиент
(rate_limiter._genai_client) переинициализируем (init_client) с project_id из нового SA.

Бот и API (оверлей) — РАЗНЫЕ процессы, у каждого свой genai-клиент. Бот после свопа
переинициализирует свой и дёргает reload-эндпоинт API (trigger_api_reload), чтобы оверлей
тоже сразу заработал на новом ключе.

⚠️ Новый SA из другого проекта НЕ имеет доступа к GCS-бакету старого проекта (botlaw2) —
Gemini заработает на новой квоте, но загрузка законов/precedents/KB через GCS может отвалиться,
пока не выдашь IAM-доступ. Это осознанный компромисс (приоритет — квота Gemini).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Optional, Tuple

import config
from database.manager import db_manager

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = ("type", "project_id", "private_key", "client_email", "token_uri")
_ACTIVE_BUCKET_KEY = "active_gcs_bucket"


def validate_sa_bytes(data: bytes) -> Tuple[bool, dict, str]:
    """Проверка, что это валидный JSON сервис-аккаунта Google. Возвращает (ok, info, error)."""
    try:
        info = json.loads(data.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, {}, f"файл не парсится как JSON: {e}"
    if not isinstance(info, dict):
        return False, {}, "JSON не является объектом"
    if info.get("type") != "service_account":
        return False, {}, f"type != 'service_account' (получено {info.get('type')!r})"
    missing = [k for k in _REQUIRED_FIELDS if not info.get(k)]
    if missing:
        return False, {}, f"в ключе нет полей: {', '.join(missing)}"
    return True, info, ""


def project_id_for(path: str) -> str:
    """project_id из файла сервис-аккаунта (фолбэк — config.GCP_PROJECT_ID)."""
    try:
        with open(path, "rb") as f:
            info = json.load(f)
        return info.get("project_id") or config.GCP_PROJECT_ID
    except Exception:  # noqa: BLE001
        return config.GCP_PROJECT_ID


def reload_local_vertex() -> Tuple[bool, str]:
    """Переинициализировать genai-клиент В ЭТОМ процессе из активного SA-файла.
    Возвращает (ok, project_id|error)."""
    from rate_limiter import init_client

    path = config.VERTEX_CREDENTIALS_FILE
    if not os.path.exists(path):
        return False, f"нет файла {path}"
    pid = project_id_for(path)
    try:
        init_client(project_id=pid, credentials_file=path, region=config.GCP_REGION)
        logger.info("[SA] genai-клиент переинициализирован, project=%s", pid)
        return True, pid
    except Exception as e:  # noqa: BLE001
        logger.exception("[SA] не удалось переинициализировать genai-клиент")
        return False, str(e)


def swap_service_account(new_bytes: bytes) -> Tuple[bool, str, Optional[dict]]:
    """Валидировать и ЗАМЕНИТЬ активный SA-файл (с бэкапом .bak, атомарно через .tmp+rename).
    Перезагрузку клиентов делает вызывающий (reload_local_vertex + trigger_api_reload).
    Возвращает (ok, msg, info)."""
    ok, info, err = validate_sa_bytes(new_bytes)
    if not ok:
        return False, f"Невалидный сервис-аккаунт: {err}", None

    path = config.VERTEX_CREDENTIALS_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")  # бэкап для отката
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(new_bytes)
        os.replace(tmp, path)  # атомарная замена
    except OSError as e:
        logger.exception("[SA] не удалось записать SA-файл")
        return False, f"Не удалось записать файл (каталог credentials/ доступен на запись?): {e}", None
    logger.info("[SA] активный сервис-аккаунт заменён: project=%s, email=%s",
                info.get("project_id"), info.get("client_email"))
    return True, "файл заменён", info


async def trigger_api_reload() -> Tuple[bool, str]:
    """Дёрнуть reload-эндпоинт API-процесса (оверлей), чтобы он тоже переинициализировал
    свой genai-клиент из обновлённого SA-файла."""
    base = getattr(config, "INTERNAL_API_BASE", "http://api:8000").rstrip("/")
    url = base + "/internal/reload-vertex"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, headers={"X-Internal-Secret": config.INTERNAL_API_SECRET})
        if r.status_code == 200:
            return True, "оверлей (API) перезагрузил клиент"
        return False, f"API ответил {r.status_code}: {r.text[:200]}"
    except Exception as e:  # noqa: BLE001
        logger.warning("[SA] reload API failed: %s", e)
        return False, f"API недоступен: {e}"


# ---------------------------------------------------------------------------
# Активный GCS-бакет (меняется вместе с проектом) + перенос законки в новый бакет
# ---------------------------------------------------------------------------

async def load_active_bucket() -> Optional[str]:
    """Прочитать активный бакет из system_cache и применить в config (вызывать на старте
    обоих процессов и в reload-эндпоинте). None → остаётся дефолт config.GCS_BUCKET_NAME."""
    try:
        val = await db_manager.fetchval(
            "SELECT value FROM system_cache WHERE key = $1", _ACTIVE_BUCKET_KEY
        )
    except Exception:
        return None
    if val is None:
        return None
    name = (val.strip().strip('"') if isinstance(val, str) else str(val)).strip() or None
    config.set_active_gcs_bucket(name)
    return name


async def set_active_bucket(name: str) -> None:
    """Сохранить активный бакет (system_cache, jsonb) + применить локально в config."""
    name = (name or "").strip()
    await db_manager.execute(
        "INSERT INTO system_cache (key, value, updated_at) VALUES ($1, $2::jsonb, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
        _ACTIVE_BUCKET_KEY, json.dumps(name),
    )
    config.set_active_gcs_bucket(name)


def _blob_name_from_uri(uri: Optional[str], fallback: str) -> str:
    """gs://<bucket>/<path> -> <path>; иначе fallback (сохраняем структуру путей)."""
    if uri and uri.startswith("gs://"):
        parts = uri.split("/", 3)
        if len(parts) == 4 and parts[3]:
            return parts[3]
    return fallback


async def migrate_corpus_to_active_bucket(enabled_only: bool = True, progress_cb=None) -> dict:
    """Перезалить тексты законов (law_documents.content, с заголовком — как при ингесте) и
    прецедентов (precedents.full_content) в АКТИВНЫЙ бакет текущим (новым) SA, обновив
    gemini_file_uri. Источник — БД (старый GCS обычно уже недоступен: SA мёртв). Бакет должен
    уже существовать в новом проекте. enabled_only=True → только сервера из PROJECTS_DATA."""
    import asyncio
    import hashlib
    from google.cloud import storage

    bucket_name = config.active_gcs_bucket()
    storage_client = await asyncio.to_thread(
        storage.Client.from_service_account_json, config.VERTEX_CREDENTIALS_FILE
    )
    bucket = storage_client.bucket(bucket_name)

    enabled = None
    if enabled_only:
        try:
            import law_sources_data
            enabled = set(law_sources_data.get_flat_servers().keys())
        except Exception:
            enabled = None

    stats = {"bucket": bucket_name, "laws_done": 0, "laws_skipped": 0,
             "precedents_done": 0, "errors": 0}

    async def _upload(blob_name: str, text: str) -> str:
        blob = bucket.blob(blob_name)
        await asyncio.to_thread(
            blob.upload_from_string, text, content_type="text/plain; charset=utf-8"
        )
        return f"gs://{bucket_name}/{blob_name}"

    # --- Законы ---
    laws = await db_manager.fetch(
        "SELECT id, server_id, source_thread_id, title, gemini_file_uri, content "
        "FROM law_documents WHERE content IS NOT NULL AND content <> ''"
    )
    total = len(laws)
    for i, r in enumerate(laws, 1):
        sid = r["server_id"]
        if enabled is not None and sid not in enabled:
            stats["laws_skipped"] += 1
            continue
        fb = f"{sid}/{hashlib.md5(str(r['source_thread_id']).encode()).hexdigest()}.txt"
        blob_name = _blob_name_from_uri(r["gemini_file_uri"], fb)
        text = f"НАЗВАНИЕ ДОКУМЕНТА: {r['title']}\n\n{r['content']}"
        try:
            new_uri = await _upload(blob_name, text)
            await db_manager.execute(
                "UPDATE law_documents SET gemini_file_uri = $1 WHERE id = $2", new_uri, r["id"]
            )
            stats["laws_done"] += 1
        except Exception:
            logger.exception("[SA] миграция закона id=%s не удалась", r["id"])
            stats["errors"] += 1
        if progress_cb and i % 50 == 0:
            try:
                await progress_cb(stats, i, total)
            except Exception:
                pass

    # --- Прецеденты ---
    precs = await db_manager.fetch(
        "SELECT id, server_id, source_thread_id, gemini_file_uri, full_content "
        "FROM precedents WHERE is_active = TRUE AND full_content IS NOT NULL AND full_content <> ''"
    )
    for r in precs:
        sid = r["server_id"]
        if enabled is not None and sid not in enabled:
            continue
        fb = f"{sid}/prec_{hashlib.md5(str(r['source_thread_id']).encode()).hexdigest()}.txt"
        blob_name = _blob_name_from_uri(r["gemini_file_uri"], fb)
        try:
            new_uri = await _upload(blob_name, r["full_content"])
            await db_manager.execute(
                "UPDATE precedents SET gemini_file_uri = $1 WHERE id = $2", new_uri, r["id"]
            )
            stats["precedents_done"] += 1
        except Exception:
            logger.exception("[SA] миграция прецедента id=%s не удалась", r["id"])
            stats["errors"] += 1

    return stats


# END OF FILE services/sa_admin.py
