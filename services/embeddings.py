# START OF FILE services/embeddings.py

"""
Эмбеддинги для семантического ретривала (RAG) голосового ассистента.

Два пути в зависимости от config.EMBEDDING_MODEL_NAME:

1) gemini-embedding-2-preview (АКТУАЛЬНЫЙ, 3072d) — НОВЫЙ raw-путь.
   SDK google-genai 1.55 для эмбеддингов ходит на legacy :predict → у -2 это
   400 FAILED_PRECONDITION. Поэтому дёргаем REST :embedContent напрямую, и ТОЛЬКО
   в регионе us-central1 (в global/прочих — 404). Особенности модели:
     • taskType игнорируется → тип задачи задаём ИНСТРУКЦИЕЙ В ТЕКСТЕ
       (документ: "title: {title} | text: {content}", запрос: "task: search result | query: {q}");
     • батч-эндпоинта нет (:batchEmbedContents = 404) → 1 контент за вызов,
       параллелим bounded-семафором (config.EMBEDDING_CONCURRENCY) + ретрай на 429/5xx.
   Токен — из активного сервис-аккаунта (config.VERTEX_CREDENTIALS_FILE; на проде это
   ГОРЯЧИЙ SA, project_id берём из самих creds, НЕ из config.GCP_PROJECT_ID).

2) gemini-embedding-001 (фолбэк, 768d) — старый путь через SDK embed_content + общий
   rate_limiter (батчевый). Сохранён на случай отката модели.

Векторы форматируются в строку для каста ::vector / ::halfvec (pgvector) — отдельная
python-зависимость pgvector не требуется (рантайм работает через asyncpg напрямую).
Тип столбца берётся из config.EMBEDDING_PG_TYPE (halfvec для 3072: HNSW поддерживает
halfvec до 4000 dims, обычный vector — только до 2000).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Sequence

import config
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client

logger = logging.getLogger(__name__)

# Типы задач эмбеддинга (см. документацию Gemini Embedding).
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"  # при индексации документов
TASK_QUERY = "RETRIEVAL_QUERY"        # при поиске по запросу


def _is_v2_model() -> bool:
    """gemini-embedding-2* → новый raw :embedContent путь (иначе SDK-фолбэк -001)."""
    return str(getattr(config, "EMBEDDING_MODEL_NAME", "")).startswith("gemini-embedding-2")


# ---------------------------------------------------------------------------
# Путь -2: raw :embedContent в us-central1
# ---------------------------------------------------------------------------

# Кэш creds активного SA (ленивая загрузка + refresh при истечении токена).
_raw_creds = None
_raw_creds_lock: Optional[asyncio.Lock] = None


def _creds_lock() -> asyncio.Lock:
    global _raw_creds_lock
    if _raw_creds_lock is None:
        _raw_creds_lock = asyncio.Lock()
    return _raw_creds_lock


async def _token_and_project(force: bool = False):
    """Возвращает (access_token, project_id) активного SA. Токен кэшируется и
    рефрешится только когда невалиден/истёк (или force=True). project_id — из самого
    ключа (на проде SA горячий → проект bot5-498212, а не config.GCP_PROJECT_ID)."""
    global _raw_creds
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    async with _creds_lock():
        if _raw_creds is None:
            _raw_creds = await asyncio.to_thread(
                lambda: service_account.Credentials.from_service_account_file(
                    config.VERTEX_CREDENTIALS_FILE,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
            )
        if force or not _raw_creds.valid:
            await asyncio.to_thread(_raw_creds.refresh, Request())
        return _raw_creds.token, _raw_creds.project_id


def _embed_url(project_id: str) -> str:
    region = getattr(config, "EMBEDDING_REGION", "us-central1")
    model = config.EMBEDDING_MODEL_NAME
    return (
        f"https://{region}-aiplatform.googleapis.com/v1/"
        f"projects/{project_id}/locations/{region}/publishers/google/"
        f"models/{model}:embedContent"
    )


# --- Адаптивный пейсер (AIMD) под ПЛАВАЮЩУЮ квоту -2-preview ---------------
# Квота per-base-model у preview не «жёстко залита»: Google режет её в пик и
# расширяет ночью. Поэтому не фиксируем интервал, а адаптируем: каждый успех —
# чуть ускоряемся (additive decrease), каждый 429 — резко тормозим (multiplicative
# increase). Так сами находим текущий потолок и эксплуатируем ночное расширение.
_pace_lock: Optional[asyncio.Lock] = None
_pace_interval = float(getattr(config, "EMBEDDING_PACE_START_SEC", 6.5))  # текущий интервал, сек
_pace_next = 0.0          # monotonic: момент, когда можно слать следующий запрос
_PACE_DEC = 0.12          # ускорение на успех (сек)
_PACE_INC = 1.6           # замедление на 429 (множитель)


def _pace_lock_get() -> asyncio.Lock:
    global _pace_lock
    if _pace_lock is None:
        _pace_lock = asyncio.Lock()
    return _pace_lock


async def _pace_acquire() -> None:
    """Зарезервировать слот по текущему интервалу (глобально, под локом)."""
    global _pace_next
    async with _pace_lock_get():
        now = time.monotonic()
        start = now if now > _pace_next else _pace_next
        _pace_next = start + _pace_interval
    wait = start - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)


def _pace_on_success() -> None:
    global _pace_interval
    lo = float(getattr(config, "EMBEDDING_PACE_MIN_SEC", 0.5))
    if _pace_interval > lo:
        _pace_interval = max(lo, _pace_interval - _PACE_DEC)


def _pace_on_429() -> None:
    global _pace_interval, _pace_next
    hi = float(getattr(config, "EMBEDDING_PACE_MAX_SEC", 30.0))
    _pace_interval = min(hi, _pace_interval * _PACE_INC)
    # отодвинуть следующий слот, чтобы дать бакету восстановиться
    nxt = time.monotonic() + _pace_interval
    if nxt > _pace_next:
        _pace_next = nxt


def pace_interval() -> float:
    """Текущий интервал пейсера (для диагностики/логов драйвера)."""
    return _pace_interval


def _v2_instruction(text: str, title: Optional[str], is_query: bool) -> str:
    """Тип задачи — инструкцией в тексте (taskType модель игнорирует), формат Google,
    консистентный для документов и запросов (cross-lingual «запрос → норма»)."""
    text = (text or "").strip()
    if is_query:
        return f"task: search result | query: {text}"
    t = (title or "").strip()
    return f"title: {t or 'none'} | text: {text}"


async def _embed_v2_one(http, text: str, title: Optional[str], is_query: bool) -> List[float]:
    """Один :embedContent с ретраем на 429/5xx/сетевые и refresh-токеном на 401."""
    import httpx

    body = {
        "content": {"parts": [{"text": _v2_instruction(text, title, is_query)}]},
        "outputDimensionality": int(config.EMBEDDING_DIMENSIONS),
    }
    last = ""
    net_delay = 1.0
    max_attempts = 20  # адаптивный пейсер обычно пробивает за 1-3; запас на плавающую квоту
    for attempt in range(max_attempts):
        await _pace_acquire()  # глобальный AIMD-пейсер: слот по текущему интервалу
        token, pid = await _token_and_project()
        try:
            r = await http.post(
                _embed_url(pid),
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as e:  # сетевые/таймаут
            last = f"net: {e}"
            await asyncio.sleep(net_delay)
            net_delay = min(net_delay * 2, 30.0)
            continue

        if r.status_code == 200:
            _pace_on_success()  # квота не сопротивляется → чуть ускоряемся
            data = r.json()
            vals = ((data.get("embedding") or {}).get("values")) or []
            return [float(v) for v in vals]

        if r.status_code == 401:  # токен протух — форс-refresh и повтор
            await _token_and_project(force=True)
            last = "401 unauthorized"
            continue

        if r.status_code == 429:  # квота исчерпана → резко тормозим (пейсер отодвинул слот)
            _pace_on_429()
            last = f"429: {r.text[:120]}"
            continue

        if r.status_code >= 500:  # временное серверное — ретрай
            last = f"{r.status_code}: {r.text[:120]}"
            await asyncio.sleep(net_delay)
            net_delay = min(net_delay * 2, 30.0)
            continue

        # прочие 4xx — невосстановимо
        raise RuntimeError(f"embedContent {r.status_code}: {r.text[:300]}")

    raise RuntimeError(f"embedContent: исчерпаны ретраи ({max_attempts}), последняя ошибка: {last}")


async def _embed_v2_many(
    texts: Sequence[str],
    titles: Optional[Sequence[Optional[str]]],
    is_query: bool,
) -> List[List[float]]:
    """Параллельный (bounded) прогон списка текстов через :embedContent. Порядок сохраняется."""
    import httpx

    n = len(texts)
    results: List[List[float]] = [[] for _ in range(n)]
    sem = asyncio.Semaphore(int(getattr(config, "EMBEDDING_CONCURRENCY", 6)))
    # Прогреваем creds один раз (refresh под локом не размножится по всем корутинам).
    await _token_and_project()

    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as http:
        async def _worker(i: int):
            title = titles[i] if titles is not None and i < len(titles) else None
            async with sem:
                results[i] = await _embed_v2_one(http, texts[i], title, is_query)

        await asyncio.gather(*[_worker(i) for i in range(n)])
    return results


# ---------------------------------------------------------------------------
# Путь -001: SDK embed_content (батч) — фолбэк
# ---------------------------------------------------------------------------

async def _embed_v1_many(
    texts: Sequence[str],
    task_type: str,
    priority: RequestPriority,
) -> List[List[float]]:
    from google.genai import types

    items = list(texts)
    out: List[List[float]] = []
    batch_size = config.EMBEDDING_BATCH_SIZE

    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]

        async def _call(_batch=batch):
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.embed_content(
                model=config.EMBEDDING_MODEL_NAME,
                contents=_batch,
                config=types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=config.EMBEDDING_DIMENSIONS,
                ),
            )

        resp = await execute_vertex_request(
            func=_call,
            model_name=config.EMBEDDING_MODEL_NAME,
            priority=priority,
            max_retries=3,
        )
        for emb in (resp.embeddings or []):
            out.append([float(v) for v in (emb.values or [])])

    return out


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

async def embed_texts(
    texts: Sequence[str],
    task_type: str,
    priority: RequestPriority = RequestPriority.NORMAL,
    titles: Optional[Sequence[Optional[str]]] = None,
) -> List[List[float]]:
    """Возвращает эмбеддинги для списка текстов (порядок сохраняется).

    task_type: TASK_DOCUMENT (индексация) или TASK_QUERY (запрос).
    titles: опц. заголовки документов (только путь -2 — уходят в инструкцию «title: …»;
            для запросов и пути -001 игнорируются).
    """
    if not texts:
        return []
    if _is_v2_model():
        is_query = (task_type == TASK_QUERY)
        return await _embed_v2_many(texts, None if is_query else titles, is_query)
    return await _embed_v1_many(texts, task_type, priority)


async def embed_one(text: str, task_type: str,
                    priority: RequestPriority = RequestPriority.NORMAL) -> List[float]:
    """Эмбеддинг одного текста (удобная обёртка для запроса)."""
    res = await embed_texts([text], task_type=task_type, priority=priority)
    return res[0] if res else []


def to_pgvector(values: Sequence[float]) -> str:
    """Форматирует вектор в строку '[v1,v2,...]' для каста $N::vector / $N::halfvec.
    Формат текстового представления у vector и halfvec одинаковый."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


# END OF FILE services/embeddings.py
