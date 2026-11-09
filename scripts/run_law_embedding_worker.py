#!/usr/bin/env python3
"""Standalone consumer durable-очереди локальных FRIDA embeddings.

Процесс намеренно отделён от FastAPI: HTTP redeploy не должен прерывать долгий
backfill. При выключенной feature процесс остаётся лёгким healthy-idle, чтобы
Docker restart policy не создавала цикл мгновенных перезапусков.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_DEFAULT_HEALTH_PATH = "/tmp/law-embedding-worker-health.json"
_HEALTHY_STATES = {"running", "disabled"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("law_embedding_worker")


def _health_path() -> Path:
    return Path(
        os.getenv("LAW_EMBED_WORKER_HEALTH_PATH", _DEFAULT_HEALTH_PATH).strip()
        or _DEFAULT_HEALTH_PATH
    )


def _health_max_age() -> float:
    try:
        heartbeat = max(
            1.0, float(os.getenv("LAW_EMBED_WORKER_HEARTBEAT_SECONDS", "5")),
        )
        configured = float(os.getenv("LAW_EMBED_WORKER_HEALTH_MAX_AGE_SECONDS", "45"))
        return max(heartbeat * 3, configured)
    except (TypeError, ValueError):
        return 45.0


def _write_health(path: Path, state: str, detail: str = "") -> None:
    """Atomic heartbeat: healthcheck никогда не читает наполовину записанный JSON."""
    payload: Dict[str, Any] = {
        "state": state,
        "detail": detail[:300],
        "heartbeat_at": time.time(),
        "pid": os.getpid(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _healthcheck() -> int:
    path = _health_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = str(payload.get("state") or "")
        heartbeat_at = float(payload["heartbeat_at"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"unhealthy: heartbeat unreadable: {type(exc).__name__}")
        return 1

    age = max(0.0, time.time() - heartbeat_at)
    if state not in _HEALTHY_STATES:
        print(f"unhealthy: state={state or 'unknown'} age={age:.1f}s")
        return 1
    if age > _health_max_age():
        print(f"unhealthy: stale heartbeat state={state} age={age:.1f}s")
        return 1
    print(f"healthy: state={state} age={age:.1f}s")
    return 0


async def _heartbeat_loop(
    path: Path,
    state: Dict[str, str],
    finished: asyncio.Event,
    interval: float,
) -> None:
    while not finished.is_set():
        try:
            _write_health(path, state["name"], state.get("detail", ""))
        except Exception:
            # Docker сам по себе не перезапускает merely-unhealthy контейнер.
            # Потерянный heartbeat поэтому является fatal liveness failure.
            logger.exception("Не удалось обновить heartbeat FRIDA worker")
            os._exit(1)
        try:
            await asyncio.wait_for(finished.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(signal_name: str) -> None:
        if not stop_event.is_set():
            logger.info("Получен %s; завершаем текущий batch", signal_name)
            stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop, sig.name)
        except (NotImplementedError, RuntimeError):
            signal.signal(
                sig,
                lambda _signum, _frame, name=sig.name: loop.call_soon_threadsafe(
                    request_stop, name,
                ),
            )


async def _run_worker() -> int:
    import config
    from database.manager import db_manager

    stop_event = asyncio.Event()
    heartbeat_finished = asyncio.Event()
    state = {"name": "starting", "detail": "bootstrap"}
    path = Path(config.LAW_EMBED_WORKER_HEALTH_PATH)
    _install_signal_handlers(stop_event)
    _write_health(path, state["name"], state["detail"])
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            path,
            state,
            heartbeat_finished,
            float(config.LAW_EMBED_WORKER_HEARTBEAT_SECONDS),
        ),
        name="law-embedding-heartbeat",
    )
    db_connected = False
    failed = False

    try:
        if not config.LAW_CATALOG_EMBED_ENABLED or not config.LAW_EMBED_RUN_WORKER:
            reasons = []
            if not config.LAW_CATALOG_EMBED_ENABLED:
                reasons.append("LAW_CATALOG_EMBED_ENABLED=false")
            if not config.LAW_EMBED_RUN_WORKER:
                reasons.append("LAW_EMBED_RUN_WORKER=false")
            state.update(name="disabled", detail=", ".join(reasons))
            _write_health(path, state["name"], state["detail"])
            logger.info("FRIDA worker выключен: %s; остаёмся healthy-idle", state["detail"])
            await stop_event.wait()
            return 0

        state.update(name="starting", detail="loading pinned FRIDA model")
        _write_health(path, state["name"], state["detail"])
        from services.law_embedder import init_law_embedder

        try:
            await asyncio.wait_for(
                asyncio.to_thread(init_law_embedder, warmup=True),
                timeout=float(config.LAW_EMBED_WORKER_INIT_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            # asyncio cannot cancel a running torch thread. A hard process exit is
            # the only way to guarantee restart instead of hanging in executor shutdown.
            state.update(name="failed", detail="FRIDA model initialization timed out")
            _write_health(path, state["name"], state["detail"])
            logger.critical(state["detail"])
            os._exit(1)

        state.update(name="starting", detail="validating database passport")
        await db_manager.connect()
        db_connected = True

        from services.law_embedding_service import (
            run_embedding_worker,
            validate_active_space_passport,
        )

        await validate_active_space_passport()
        state.update(name="running", detail="durable queue consumer active")
        _write_health(path, state["name"], state["detail"])
        logger.info("Pinned FRIDA model/passport validated; queue consumer started")

        await run_embedding_worker(stop_event)
        if not stop_event.is_set():
            raise RuntimeError("FRIDA queue consumer returned unexpectedly")
        return 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failed = True
        state.update(name="failed", detail=f"{type(exc).__name__}: {exc}")
        _write_health(path, state["name"], state["detail"])
        logger.exception("FRIDA worker failed during startup/runtime")
        # The timed-out asyncio.to_thread still owns a native torch call. Normal
        # interpreter shutdown would wait for that thread and keep a false-live
        # container forever; only a hard process exit guarantees Docker restart.
        from services.law_embedder import LawEmbeddingInferenceTimeout
        if isinstance(exc, LawEmbeddingInferenceTimeout):
            os._exit(1)
        return 1
    finally:
        if not failed:
            state.update(name="stopping", detail="graceful shutdown")
            _write_health(path, state["name"], state["detail"])
        if db_connected:
            try:
                await db_manager.close()
            except Exception:
                logger.exception("Не удалось закрыть DB pool FRIDA worker")
        heartbeat_finished.set()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--healthcheck":
        return _healthcheck()
    if len(sys.argv) != 1:
        print("usage: run_law_embedding_worker.py [--healthcheck]", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run_worker())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
