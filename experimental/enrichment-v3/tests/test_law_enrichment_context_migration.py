from __future__ import annotations

import ast
import importlib.util
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "alembic"
    / "versions"
    / "026_add_law_enrichment_context_20260729.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_026_law_enrichment_context",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_postgresql(
    monkeypatch,
    migration: ModuleType,
    function_name: str,
) -> str:
    output = StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, function_name)()
    return output.getvalue()


class _OperationRecorder:
    def __init__(self) -> None:
        self.actions: list[tuple[Any, ...]] = []

    def add_column(self, table: str, column: sa.Column[Any]) -> None:
        self.actions.append(("add_column", table, column))

    def create_check_constraint(
        self,
        name: str,
        table: str,
        condition: str,
        **kwargs: Any,
    ) -> None:
        self.actions.append(
            (
                "create_check_constraint",
                name,
                table,
                condition,
                kwargs,
            )
        )

    def drop_constraint(
        self,
        name: str,
        table: str,
        *,
        type_: str,
    ) -> None:
        self.actions.append(("drop_constraint", name, table, type_))

    def drop_column(self, table: str, column: str) -> None:
        self.actions.append(("drop_column", table, column))


def _constant_strings(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def test_revision_is_the_single_head_after_assistant_case_digest() -> None:
    migration = _load_migration()
    previous = _constant_strings(
        ROOT
        / "alembic"
        / "versions"
        / "025_add_assistant_case_digest_20260728.py"
    )

    assert migration.revision == "026_law_enrichment_context"
    assert migration.down_revision == previous["revision"]
    assert migration.branch_labels is None
    assert migration.depends_on is None

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [migration.revision]


def test_upgrade_columns_match_runtime_contract_without_rewrite(
    monkeypatch,
) -> None:
    migration = _load_migration()
    recorder = _OperationRecorder()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    title_column = recorder.actions[0]
    context_column = recorder.actions[1]
    output_column = recorder.actions[2]
    assert title_column[:2] == ("add_column", "law_articles")
    assert title_column[2].name == "title_provenance"
    assert isinstance(title_column[2].type, sa.Text)
    assert title_column[2].nullable is True
    assert title_column[2].server_default is None

    assert context_column[:2] == ("add_column", "law_articles")
    assert context_column[2].name == "enrichment_context_hash"
    assert isinstance(context_column[2].type, sa.String)
    assert context_column[2].type.length == 64
    assert context_column[2].nullable is True
    assert context_column[2].server_default is None

    assert output_column[:2] == ("add_column", "law_articles")
    assert output_column[2].name == "enrichment_output_hash"
    assert isinstance(output_column[2].type, sa.String)
    assert output_column[2].type.length == 64
    assert output_column[2].nullable is True
    assert output_column[2].server_default is None

    constraint_actions = recorder.actions[3:]
    assert [action[1] for action in constraint_actions] == [
        "ck_law_articles_title_provenance",
        "ck_law_articles_enrichment_context_hash",
        "ck_law_articles_enrichment_output_hash",
    ]
    assert all(
        action[0] == "create_check_constraint"
        and action[2] == "law_articles"
        and action[4] == {"postgresql_not_valid": True}
        for action in constraint_actions
    )
    assert "'source'" in constraint_actions[0][3]
    assert "'generated'" in constraint_actions[0][3]
    assert "'legacy_unknown'" in constraint_actions[0][3]
    assert "'^[0-9a-f]{64}$'" in constraint_actions[1][3]
    assert "'^[0-9a-f]{64}$'" in constraint_actions[2][3]


def test_postgresql_upgrade_is_metadata_only_and_enforces_new_writes(
    monkeypatch,
) -> None:
    migration = _load_migration()
    sql = _render_postgresql(monkeypatch, migration, "upgrade")
    normalized = " ".join(sql.upper().split())

    assert "ADD COLUMN TITLE_PROVENANCE TEXT" in normalized
    assert "ADD COLUMN ENRICHMENT_CONTEXT_HASH VARCHAR(64)" in normalized
    assert "ADD COLUMN ENRICHMENT_OUTPUT_HASH VARCHAR(64)" in normalized
    assert normalized.count(" NOT VALID") == 3
    assert "UPDATE LAW_ARTICLES" not in normalized
    assert "VALIDATE CONSTRAINT" not in normalized
    assert "DEFAULT" not in normalized

    # PostgreSQL enforces a NOT VALID CHECK for every subsequent INSERT/UPDATE;
    # only the redundant scan of pre-existing rows is deferred.  Both new
    # columns are NULL for all old tuples by construction.
    assert (
        "CHECK (TITLE_PROVENANCE IS NULL OR TITLE_PROVENANCE IN "
        "('SOURCE', 'GENERATED', 'LEGACY_UNKNOWN')) NOT VALID"
    ) in normalized
    assert (
        "CHECK (ENRICHMENT_CONTEXT_HASH IS NULL OR "
        "ENRICHMENT_CONTEXT_HASH ~ '^[0-9A-F]{64}$') NOT VALID"
    ) in normalized
    assert (
        "CHECK (ENRICHMENT_OUTPUT_HASH IS NULL OR "
        "ENRICHMENT_OUTPUT_HASH ~ '^[0-9A-F]{64}$') NOT VALID"
    ) in normalized


def test_migration_values_and_columns_match_runtime_service() -> None:
    constants = _constant_strings(ROOT / "services" / "law_catalog_service.py")
    service = (
        ROOT / "services" / "law_catalog_service.py"
    ).read_text(encoding="utf-8")

    assert {
        constants["TITLE_PROVENANCE_SOURCE"],
        constants["TITLE_PROVENANCE_GENERATED"],
        constants["TITLE_PROVENANCE_LEGACY_UNKNOWN"],
    } == {"source", "generated", "legacy_unknown"}
    assert "title_provenance" in service
    assert "enrichment_context_hash" in service
    assert "enrichment_output_hash" in service
    assert 're.fullmatch(r"[0-9a-f]{64}"' in service


def test_downgrade_drops_checks_before_their_columns(monkeypatch) -> None:
    migration = _load_migration()
    recorder = _OperationRecorder()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.actions == [
        (
            "drop_constraint",
            "ck_law_articles_enrichment_output_hash",
            "law_articles",
            "check",
        ),
        (
            "drop_constraint",
            "ck_law_articles_enrichment_context_hash",
            "law_articles",
            "check",
        ),
        (
            "drop_constraint",
            "ck_law_articles_title_provenance",
            "law_articles",
            "check",
        ),
        (
            "drop_column",
            "law_articles",
            "enrichment_output_hash",
        ),
        (
            "drop_column",
            "law_articles",
            "enrichment_context_hash",
        ),
        ("drop_column", "law_articles", "title_provenance"),
    ]


def test_postgresql_downgrade_order_is_dependency_safe(monkeypatch) -> None:
    migration = _load_migration()
    sql = _render_postgresql(monkeypatch, migration, "downgrade").upper()

    context_check = sql.index(
        "DROP CONSTRAINT CK_LAW_ARTICLES_ENRICHMENT_CONTEXT_HASH"
    )
    output_check = sql.index(
        "DROP CONSTRAINT CK_LAW_ARTICLES_ENRICHMENT_OUTPUT_HASH"
    )
    title_check = sql.index("DROP CONSTRAINT CK_LAW_ARTICLES_TITLE_PROVENANCE")
    context_column = sql.index("DROP COLUMN ENRICHMENT_CONTEXT_HASH")
    output_column = sql.index("DROP COLUMN ENRICHMENT_OUTPUT_HASH")
    title_column = sql.index("DROP COLUMN TITLE_PROVENANCE")
    assert (
        output_check
        < context_check
        < title_check
        < output_column
        < context_column
        < title_column
    )
