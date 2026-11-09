import json
import time

from scripts import run_law_embedding_worker as worker


def _configure_health(monkeypatch, tmp_path):
    path = tmp_path / "worker-health.json"
    monkeypatch.setenv("LAW_EMBED_WORKER_HEALTH_PATH", str(path))
    monkeypatch.setenv("LAW_EMBED_WORKER_HEALTH_MAX_AGE_SECONDS", "10")
    return path


def test_healthcheck_accepts_fresh_running_heartbeat(monkeypatch, tmp_path):
    path = _configure_health(monkeypatch, tmp_path)
    worker._write_health(path, "running", "test")
    assert worker._healthcheck() == 0


def test_healthcheck_accepts_disabled_idle_worker(monkeypatch, tmp_path):
    path = _configure_health(monkeypatch, tmp_path)
    worker._write_health(path, "disabled", "feature off")
    assert worker._healthcheck() == 0


def test_healthcheck_rejects_starting_stale_or_failed_heartbeat(monkeypatch, tmp_path):
    path = _configure_health(monkeypatch, tmp_path)
    worker._write_health(path, "starting", "loading model")
    assert worker._healthcheck() == 1

    path.write_text(
        json.dumps({"state": "running", "heartbeat_at": time.time() - 30}),
        encoding="utf-8",
    )
    assert worker._healthcheck() == 1

    worker._write_health(path, "failed", "passport mismatch")
    assert worker._healthcheck() == 1
