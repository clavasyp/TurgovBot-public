from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"


def _deploy_text() -> str:
    return DEPLOY.read_text(encoding="utf-8")


def test_old_bot_and_api_are_drained_before_schema_migration() -> None:
    workflow = _deploy_text()

    stop = workflow.index("docker compose stop -t 120 bot api")
    migrate = workflow.index("docker compose run --rm migrate")
    recreate = workflow.index(
        "docker compose up -d --remove-orphans --wait --wait-timeout 300"
    )

    assert stop < migrate < recreate


def test_failed_transactional_migration_restores_both_old_writers() -> None:
    workflow = _deploy_text()
    failure_branch = workflow[
        workflow.index("if ! docker compose run --rm migrate;"):
        workflow.index(
            "# Поднимаем сервисы.",
        )
    ]

    assert "docker compose start bot api || true" in failure_branch
    assert "docker compose start bot || true" not in failure_branch


def test_success_path_never_explicitly_restarts_an_old_writer() -> None:
    workflow = _deploy_text()
    after_migration = workflow[
        workflow.index("# Поднимаем сервисы."):]

    assert "docker compose start bot" not in after_migration
    assert "docker compose start api" not in after_migration
