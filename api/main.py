# START OF FILE api/main.py

"""
Главный модуль FastAPI приложения.

Запуск: uvicorn api.main:app --host 0.0.0.0 --port 8080
Или через: python api/run_server.py
"""

import asyncio
import logging
import sys
import os
from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

# Добавляем корневую директорию в path для импортов
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import API_VERSION, API_SERVER_PORT, API_SERVER_HOST, GCP_REGION, GCP_PROJECT_ID, VERTEX_CREDENTIALS_FILE, DATABASE_URL, LAW_SEARCH_WARMUP_INTERVAL
from rate_limiter import init_client

from .schemas import HealthResponse, ErrorResponse
from .middleware import SecurityHeadersMiddleware, RequestLoggingMiddleware, DynamicCORSMiddleware, DocsProtectionMiddleware
from .dependencies import set_db_pool, get_db_pool
from .routers import qa_router, criminal_case_router, complaint_analyzer_router, cms_migration_router, overlay_router, overlay_laws_router, voice_router, auth_router, license_router, web_auth_router, web_tools_router, web_assistant_router, web_account_router

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s'
)
logger = logging.getLogger(__name__)

# Глобальный пул БД
_db_pool: asyncpg.Pool = None


def get_pool():
    """Геттер пула БД для middleware."""
    return _db_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения."""
    global _db_pool

    # Startup
    logger.info("[API] Запуск API сервера...")

    try:
        # Создаём пул соединений с БД
        _db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=2, max_size=10)
        set_db_pool(_db_pool)
        logger.info("[API] Пул соединений с БД создан")

        # Инициализируем db_manager для сервисов (complaint_analyzer_service и др.)
        from database.manager import db_manager
        await db_manager.connect()
        logger.info("[API] db_manager инициализирован")
    except Exception as e:
        logger.error(f"[API] Ошибка подключения к БД: {e}")
        raise

    # Purpose-specific law-search quality log.  Keep this outside the critical
    # DB-startup block: telemetry must remain fail-open even on an import or
    # initialisation defect.
    try:
        from services.law_search_analytics import start_law_search_logging
        await start_law_search_logging(_db_pool)
    except Exception as e:
        logger.warning(f"[API] Журнал law-search не запущен, поиск продолжит работу: {e}")

    # Инициализируем Vertex AI для AI-анализа
    # API запускается как отдельный процесс, поэтому нужна своя инициализация
    try:
        import os
        if os.path.exists(VERTEX_CREDENTIALS_FILE):
            from services.sa_admin import project_id_for
            init_client(
                project_id=project_id_for(VERTEX_CREDENTIALS_FILE),
                credentials_file=VERTEX_CREDENTIALS_FILE,
                region=GCP_REGION
            )
            logger.info("[API] Vertex AI Client инициализирован")
        else:
            logger.warning(f"[API] Файл credentials не найден: {VERTEX_CREDENTIALS_FILE}. AI-анализ будет использовать fallback.")
    except Exception as e:
        logger.error(f"[API] Ошибка инициализации Vertex AI: {e}")
        # Не прерываем запуск - API может работать без AI (fallback на rule-based)

    # Активный GCS-бакет (мог быть переопределён при смене проекта/SA).
    try:
        from services.sa_admin import load_active_bucket
        await load_active_bucket()
    except Exception as e:
        logger.warning(f"[API] не удалось загрузить активный GCS-бакет: {e}")

    # Keep-warm облачных embeddings остаётся только для отдельного law_chunks/RAG-контура.
    # Поиск законов Overlay этот вызов не использует: его vector-нога полностью локальная FRIDA.
    async def _keep_embeddings_warm():
        from rate_limiter import RequestPriority
        from services.embeddings import embed_one, TASK_QUERY
        while True:
            try:
                await embed_one("прогрев RAG", task_type=TASK_QUERY, priority=RequestPriority.LOW)
                logger.info("[API] Облачный RAG-эмбеддинг прогрет (SA-токен свеж)")
            except Exception as e:
                logger.warning(f"[API] Keep-warm эмбеддингов не удался: {type(e).__name__}: {e}")
            await asyncio.sleep(LAW_SEARCH_WARMUP_INTERVAL)
    asyncio.create_task(_keep_embeddings_warm())

    # Локальный эмбеддер каталога поиска (FRIDA) — грузим БЛОКИРУЮЩЕ в startup (НЕ в фоне: урок
    # keep-warm выше — фоновый прогрев ненадёжен). init делает warmup-прогон сам. Только при флаге;
    # сбой не роняет api (поиск деградирует до keyword). Vertex keep-warm НЕ трогаем — им ещё живут
    # RAG-советы (law_chunks), которые делят embed_one; поиск на локали от него не зависит.
    law_embedding_worker_task = None
    law_embedding_worker_stop = None
    if config.LAW_CATALOG_EMBED_ENABLED:
        try:
            from services.law_embedder import init_law_embedder
            init_law_embedder(warmup=True)
            logger.info("[API] Локальный эмбеддер каталога поиска (FRIDA) загружен")
            # В штатном compose durable consumer вынесен в отдельный контейнер:
            # перезапуск HTTP API больше не обрывает долгий backfill. Флаг оставляет
            # совместимый single-process режим для локальных/служебных запусков.
            if config.LAW_EMBED_RUN_WORKER:
                from services.law_embedding_service import run_embedding_worker
                law_embedding_worker_stop = asyncio.Event()
                law_embedding_worker_task = asyncio.create_task(
                    run_embedding_worker(law_embedding_worker_stop),
                    name="law-embedding-worker",
                )

                def _log_law_worker_exit(task: asyncio.Task) -> None:
                    if task.cancelled():
                        return
                    exc = task.exception()
                    if exc is not None:
                        logger.critical(
                            "[API] Worker очереди FRIDA остановился; поиск остаётся keyword-safe: %s",
                            exc,
                            exc_info=(type(exc), exc, exc.__traceback__),
                        )

                law_embedding_worker_task.add_done_callback(_log_law_worker_exit)
                logger.info("[API] Worker очереди FRIDA запущен внутри API")
            else:
                logger.info("[API] Consumer очереди FRIDA передан dedicated worker")
        except Exception as e:
            logger.error(f"[API] НЕ удалось загрузить локальный эмбеддер — поиск на keyword: {e}")

    yield

    # Shutdown
    logger.info("[API] Остановка API сервера...")

    if law_embedding_worker_task is not None:
        law_embedding_worker_stop.set()
        try:
            # Даём текущему небольшому batch завершить conditional write, чтобы
            # jobs не оставались в processing до истечения lease после рестарта.
            await asyncio.wait_for(asyncio.shield(law_embedding_worker_task), timeout=10.0)
        except asyncio.TimeoutError:
            law_embedding_worker_task.cancel()
            try:
                await law_embedding_worker_task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[API] Ошибка остановки FRIDA worker: {e}")

    # Drain the bounded quality-log queue while its PostgreSQL pool is alive.
    try:
        from services.law_search_analytics import stop_law_search_logging
        await stop_law_search_logging()
    except Exception as e:
        logger.warning(f"[API] Ошибка остановки журнала law-search: {e}")

    # Закрываем db_manager
    try:
        from database.manager import db_manager
        await db_manager.close()
        logger.info("[API] db_manager закрыт")
    except Exception as e:
        logger.warning(f"[API] Ошибка закрытия db_manager: {e}")

    if _db_pool:
        await _db_pool.close()
        logger.info("[API] Пул соединений с БД закрыт")


# Создание FastAPI приложения
app = FastAPI(
    title="ProdBot REST API",
    description="""
REST API для доступа к функциям юридического бота.

## Возможности

* **QA** — RAG-ответы на юридические вопросы
* **Criminal Case Writer** — создание уголовных дел (интерактивный режим)
* **Complaint Analyzer** — анализ жалоб для сайта (stateless)
* **CMS Migration** — AI-функции для prosecutor-cms1 (миграция с OpenAI)

## Аутентификация

Все эндпоинты требуют API-ключ в заголовке `X-API-Key`.

Типы ключей:
- **Web** — для сайтов, с CORS и привязкой к домену
- **App** — для приложений, без CORS, блокировка браузерных запросов

## Workflow анализа жалобы

1. **POST /v1/complaint/parse** — парсинг жалобы по URL (один раз)
2. **POST /v1/complaint/check-form** — проверка формы (с complaint_text)
3. **POST /v1/complaint/analyze** — анализ сложности и субъекта (с complaint_text)
4. **POST /v1/complaint/extract-facts** — извлечение фактов (с complaint_text)

## Управление настройками сервера

- **GET/PUT /v1/complaint/form-template/{server_id}** — шаблон формы
- **GET/POST/PUT/DELETE /v1/complaint/issue-types/{server_id}** — типы проблем

## Workflow создания уголовного дела

1. **POST /v1/criminal-case/start** — создать сессию
2. **POST /v1/criminal-case/{session_id}/facts** — добавить факты дела
3. **POST /v1/criminal-case/{session_id}/analyze** — получить уточняющие вопросы (опционально)
4. **POST /v1/criminal-case/{session_id}/answer** — отправить ответы на вопросы (опционально)
5. **POST /v1/criminal-case/{session_id}/generate** — сгенерировать часть дела
6. **GET /v1/criminal-case/{session_id}** — получить все данные сессии

## CMS Migration API (prosecutor-cms1)

AI-эндпоинты для миграции с OpenAI на Gemini. Формат ответов 1:1 совместим с OpenAI-версией.

- **POST /v1/cms/full-analysis** — полный анализ жалобы (analyzeComplaintFull)
- **POST /v1/cms/issue-tags** — юридические теги по фактам (getIssueSpottingTags)
- **POST /v1/cms/evidence-labels** — классификация ссылок (getEvidenceTypeLabels)
- **POST /v1/cms/validate-form-ai** — AI-валидация формы (validateFormAI)
- **POST /v1/cms/case-file-draft** — черновик кейс-файла (generateCaseFileDraft)
- **POST /v1/cms/generate-plan** — генерация плана работы (generatePlanAI)
- **POST /v1/cms/adapt-plan** — адаптация плана (adaptPlanAI)
- **POST /v1/cms/passport-ocr** — OCR паспорта (extractPassportData)
- **POST /v1/cms/resolve-passport-url** — определение паспорта среди ссылок (resolvePassportUrlFromCandidates)
""",
    version=API_VERSION,
    lifespan=lifespan,
    docs_url="/verblud",
    redoc_url="/verblud/redoc",
    openapi_url="/verblud/openapi.json"
)


# === MIDDLEWARE ===

# Docs protection (должен быть первым!)
app.add_middleware(DocsProtectionMiddleware, db_pool_getter=get_pool)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# Request logging
app.add_middleware(RequestLoggingMiddleware)

# Dynamic CORS (для web-ключей)
app.add_middleware(DynamicCORSMiddleware, db_pool_getter=get_pool)


# === EXCEPTION HANDLERS ===

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Обработчик HTTP исключений."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "code": f"HTTP_{exc.status_code}"
        },
        headers=getattr(exc, 'headers', None)
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Обработчик ошибок валидации."""
    errors = []
    for error in exc.errors():
        loc = ".".join(str(l) for l in error["loc"])
        errors.append(f"{loc}: {error['msg']}")

    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation error",
            "detail": "; ".join(errors),
            "code": "VALIDATION_ERROR"
        }
    )


# === ВНУТРЕННИЙ ЭНДПОИНТ (docker-сеть): горячая смена сервис-аккаунта ===

@app.post("/internal/reload-vertex")
async def internal_reload_vertex(request: Request):
    """Переинициализировать genai-клиент ЭТОГО процесса из активного SA-файла.
    Дёргается ботом после смены сервис-аккаунта (services/sa_admin). Защищён общим
    внутренним секретом; доступен только внутри docker-сети (порт наружу — через Caddy/CF
    не проксируется на /internal). Файл SA к этому моменту уже перезаписан ботом."""
    import config as _cfg
    secret = request.headers.get("X-Internal-Secret", "")
    if not secret or secret != _cfg.INTERNAL_API_SECRET:
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    from services.sa_admin import reload_local_vertex, load_active_bucket
    bucket = await load_active_bucket()   # подхватить новый бакет (смена проекта)
    ok, msg = reload_local_vertex()
    if ok:
        return {"status": "ok", "project_id": msg, "bucket": bucket}
    return JSONResponse(status_code=500, content={"error": msg})


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Обработчик всех остальных исключений."""
    logger.error(f"[API] Необработанное исключение: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "code": "INTERNAL_ERROR"
        }
    )


# === ROUTERS ===

app.include_router(qa_router)
app.include_router(criminal_case_router)
app.include_router(complaint_analyzer_router)
app.include_router(cms_migration_router)
app.include_router(overlay_router)
app.include_router(overlay_laws_router)
app.include_router(voice_router)
app.include_router(auth_router)
app.include_router(license_router)
app.include_router(web_auth_router)
app.include_router(web_tools_router)
app.include_router(web_assistant_router)
app.include_router(web_account_router)


# === HEALTH CHECK ===

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Health check",
    description="Проверка состояния API сервера."
)
async def health_check():
    """Эндпоинт для проверки здоровья сервиса."""
    return HealthResponse(
        status="ok",
        version=API_VERSION,
        timestamp=datetime.utcnow()
    )


@app.get(
    "/",
    tags=["System"],
    summary="Root",
    description="Корневой эндпоинт с информацией об API."
)
async def root():
    """Корневой эндпоинт."""
    return {
        "name": "ProdBot REST API",
        "version": API_VERSION,
        "status": "running",
        "auth": "API key required in X-API-Key header"
    }


# END OF FILE api/main.py
