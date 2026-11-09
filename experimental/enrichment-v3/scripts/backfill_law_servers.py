#!/usr/bin/env python3
"""Безопасный последовательный Phase A backfill Overlay-каталога.

Этот CLI предназначен только для явного списка игровых ``server_id`` из
``law_sources_data.PROJECTS_DATA``. Он намеренно не поддерживает ``--all`` и не
принимает namespace сводов правил.

Пайплайн одного сервера:

1. fail-closed preflight исходных ``law_documents`` и частичного Overlay baseline;
2. обязательный catalog dry-run;
3. существующий ``build_catalog_for_server(enrich=False)`` без генеративной модели;
4. если ручное Codex-enrichment ещё не применено — durable checkpoint
   ``awaiting_enrichment`` с командами export/review/apply и безопасный выход;
5. после применения артефактов повторный запуск проверяет enrichment, ожидает полный
   совместимый FRIDA-корпус и завершение related rebuild;
6. durable JSON-checkpoint в ``awaiting_smoke``; ``complete`` разрешён только после
   отдельного настоящего HTTP smoke и повторной структурной проверки.

Логика сегментации, catalog reconcile, enrichment и FRIDA здесь не
дублируется: CLI только оркестрирует общие сервисы и проверяет их результат.

Этот workflow намеренно Overlay-only. Он не строит и не изменяет ``law_chunks``,
не инициализирует Vertex/Gemini и не вызывает ``gemini-embedding-001``. Старый
RAG-корпус советов/ролей/чек-листов здесь вообще не читается и не является
условием успеха. Векторы поиска статей создаёт только dedicated worker локальной
FRIDA.

Рекомендуемый production-запуск после отдельного проверенного backup (не выполнять
из CI автоматически):

    docker exec turgovbot-bot-1 \
      python /app/scripts/backfill_law_servers.py las_vegas

Write-run принимает ровно один сервер. Он никогда не вызывает Gemini/Vertex.
На ``awaiting_enrichment`` оператор выгружает небольшие source-batch
через ``export_law_enrichment_batches.py``, Codex вручную готовит reviewed JSON,
официальный offline builder собирает все batch в единый native-v3 envelope, и только
этот envelope применяется через общий ``_apply_claude_enrich.py``. Повтор этой команды продолжает
FRIDA. Затем оператор запускает
``smoke_law_search_http.py las_vegas``, сохраняет его JSON и тем же per-server
checkpoint выполняет ``backfill_law_servers.py las_vegas --finalize-smoke FILE``.
Только после ``complete`` можно начинать следующий server_id.

У bot-контейнера ``/app/data`` смонтирован на host, поэтому checkpoint переживает
перезапуск контейнера. Скрипт не синхронизирует форум, не пишет GCS и не занимается
Phase B/аддитивными синонимами.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from database.manager import db_manager
from law_sources_data import get_flat_servers
from services.law_smoke_contract import (
    LAW_SMOKE_CLOCK_SKEW_SECONDS,
    LAW_SMOKE_CASE_NAMES,
    LAW_SMOKE_CASES,
    LAW_SMOKE_MAX_AGE_SECONDS,
    LAW_SMOKE_MIN_SCORE,
    LAW_SMOKE_SCHEMA_VERSION,
    recompute_case_result,
)
from services.law_source_snapshot import law_source_fingerprint
from services.law_catalog_truth import (
    LawCatalogTruthError,
    build_expected_law_catalog_rows,
    verify_law_documents_catalog_truth,
)

log = logging.getLogger("backfill_law_servers")

CHECKPOINT_VERSION = 5
DEFAULT_CHECKPOINT = Path(config.DATA_DIR) / "law_phase_a_checkpoint.json"
MANUAL_ENRICHMENT_MODE = "manual_codex_artifacts"
HEADER_WARNING_AUDIT_SCHEMA = "law-header-warning-audit-v1"
LEGACY_RAG_OBSERVATION = {
    "required": False,
    "mutated": False,
    "observed": False,
}
_RULE_NAMESPACES = {
    "majestic_rules",
    "gta5rp_rules_project",
    "gta5rp_rules_davis",
}


class PhaseAError(RuntimeError):
    """Fail-closed остановка Phase A."""


class HeaderWarningAuditError(PhaseAError):
    """Stored header-warning approval is missing, malformed, or stale."""


def _checkpoint_bool(
    details: Mapping[str, Any],
    field: str,
    *,
    missing: bool | None,
) -> bool | None:
    """Read a checkpoint bool without accepting truthy strings/integers."""

    if field not in details:
        return missing
    value = details[field]
    if type(value) is not bool:
        raise HeaderWarningAuditError(
            f"checkpoint {field} must be an exact bool"
        )
    return value


def _canonical_source_fingerprint(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the exact durable source identity used by audit contracts."""

    expected_fields = {
        "algorithm",
        "snapshot_version",
        "documents",
        "value",
    }
    if set(value) != expected_fields:
        raise HeaderWarningAuditError(
            "header-warning audit source_fingerprint has invalid fields"
        )
    algorithm = value.get("algorithm")
    snapshot_version = value.get("snapshot_version")
    documents = value.get("documents")
    digest = value.get("value")
    if algorithm != "sha256":
        raise HeaderWarningAuditError(
            "header-warning audit source_fingerprint algorithm is invalid"
        )
    if (
        type(snapshot_version) is not int
        or snapshot_version <= 0
        or type(documents) is not int
        or documents < 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise HeaderWarningAuditError(
            "header-warning audit source_fingerprint is malformed"
        )
    return {
        "algorithm": algorithm,
        "snapshot_version": snapshot_version,
        "documents": documents,
        "value": digest,
    }


def _header_warning_audit_contract(
    source_fingerprint: Mapping[str, Any],
    warnings: Sequence[str],
) -> dict[str, Any]:
    """Bind one explicit warning approval to one source and warning set."""

    if not isinstance(source_fingerprint, Mapping):
        raise HeaderWarningAuditError(
            "header-warning audit requires source_fingerprint"
        )
    warning_values = list(warnings)
    if any(
        not isinstance(warning, str) or not warning.strip()
        for warning in warning_values
    ):
        raise HeaderWarningAuditError(
            "header-warning audit warnings must be non-empty strings"
        )
    payload = {
        "schema": HEADER_WARNING_AUDIT_SCHEMA,
        "source_fingerprint": _canonical_source_fingerprint(
            source_fingerprint
        ),
        "warnings": sorted(set(warning_values)),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **payload,
        "contract_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _validated_header_warning_audit(
    value: Any,
) -> dict[str, Any]:
    """Validate a persisted audit contract, including its own digest."""

    if not isinstance(value, Mapping):
        raise HeaderWarningAuditError(
            "checkpoint with allow_header_warnings=True has no bound "
            "header_warning_audit; use --reset-checkpoint and re-audit"
        )
    expected_fields = {
        "schema",
        "source_fingerprint",
        "warnings",
        "contract_sha256",
    }
    if set(value) != expected_fields:
        raise HeaderWarningAuditError(
            "checkpoint header_warning_audit has invalid fields; "
            "use --reset-checkpoint and re-audit"
        )
    if value.get("schema") != HEADER_WARNING_AUDIT_SCHEMA:
        raise HeaderWarningAuditError(
            "checkpoint header_warning_audit schema is unsupported; "
            "use --reset-checkpoint and re-audit"
        )
    warnings = value.get("warnings")
    if (
        not isinstance(warnings, list)
        or any(
            not isinstance(warning, str) or not warning.strip()
            for warning in warnings
        )
        or warnings != sorted(set(warnings))
    ):
        raise HeaderWarningAuditError(
            "checkpoint header_warning_audit warnings are malformed; "
            "use --reset-checkpoint and re-audit"
        )
    rebuilt = _header_warning_audit_contract(
        value.get("source_fingerprint"),
        warnings,
    )
    if dict(value) != rebuilt:
        raise HeaderWarningAuditError(
            "checkpoint header_warning_audit digest is invalid; "
            "use --reset-checkpoint and re-audit"
        )
    return rebuilt


def _checkpoint_header_warning_state(
    details: Mapping[str, Any],
    *,
    missing_allow: bool | None,
) -> tuple[bool | None, dict[str, Any] | None]:
    """Return a strict bool plus its required source-bound audit contract."""

    allow = _checkpoint_bool(
        details,
        "allow_header_warnings",
        missing=missing_allow,
    )
    raw_audit = details.get("header_warning_audit")
    if allow is True:
        return allow, _validated_header_warning_audit(raw_audit)
    if raw_audit is not None:
        raise HeaderWarningAuditError(
            "checkpoint has header_warning_audit without "
            "allow_header_warnings=True; use --reset-checkpoint and re-audit"
        )
    return allow, None


def _resume_header_warning_audit(
    entry: Any,
    *,
    requested_allow: bool,
) -> dict[str, Any] | None:
    """Validate an active checkpoint contract before any resume work."""

    if not isinstance(entry, Mapping) or not entry:
        return None
    details = entry.get("details")
    if not isinstance(details, Mapping):
        details = {}
    stored_allow, stored_audit = _checkpoint_header_warning_state(
        details,
        missing_allow=None,
    )
    if stored_allow is None:
        if requested_allow:
            raise HeaderWarningAuditError(
                "legacy active checkpoint has no source-bound "
                "header-warning audit; use --reset-checkpoint and re-audit"
            )
        return None
    if stored_allow != requested_allow:
        required_flag = (
            "с --allow-header-warnings"
            if stored_allow
            else "без --allow-header-warnings"
        )
        raise HeaderWarningAuditError(
            "checkpoint allow_header_warnings="
            f"{stored_allow!r}, requested={requested_allow!r}; продолжи "
            f"{required_flag} или используй --reset-checkpoint"
        )
    return stored_audit


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc_timestamp(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PhaseAError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PhaseAError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _new_smoke_challenge(*, now: datetime | None = None) -> dict[str, Any]:
    issued = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "run_nonce": secrets.token_hex(32),
        "issued_at": issued.isoformat(),
        "expires_at": (
            issued + timedelta(seconds=LAW_SMOKE_MAX_AGE_SECONDS)
        ).isoformat(),
    }


def validate_explicit_targets(raw_server_ids: Sequence[str]) -> list[str]:
    """Разрешает только уникальные игровые server_id, явно перечисленные оператором."""
    if not raw_server_ids:
        raise PhaseAError("нужен явный список server_id; режим --all не поддерживается")
    configured = get_flat_servers()
    targets: list[str] = []
    seen: set[str] = set()
    for raw in raw_server_ids:
        server_id = str(raw or "").strip()
        if not server_id:
            raise PhaseAError("пустой server_id")
        if server_id in seen:
            raise PhaseAError(f"server_id указан повторно: {server_id}")
        if server_id in _RULE_NAMESPACES or server_id not in configured:
            raise PhaseAError(
                f"server_id не является настроенным игровым сервером: {server_id}"
            )
        seen.add(server_id)
        targets.append(server_id)
    return targets


class CheckpointStore:
    """Атомарный JSON-checkpoint без секретов и больших source payload."""

    def __init__(self, path: Path, targets: Sequence[str], *, reset: bool = False):
        self.path = Path(path)
        self.targets = list(targets)
        self.data = self._load_or_create(reset=reset)

    def _fresh(self) -> dict[str, Any]:
        now = _utc_now()
        return {
            "version": CHECKPOINT_VERSION,
            "targets": self.targets,
            "created_at": now,
            "updated_at": now,
            "servers": {},
        }

    def _load_or_create(self, *, reset: bool) -> dict[str, Any]:
        if reset or not self.path.exists():
            data = self._fresh()
            self._write(data)
            return data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PhaseAError(f"checkpoint повреждён: {self.path}: {exc}") from exc
        if data.get("version") != CHECKPOINT_VERSION:
            raise PhaseAError(
                f"неподдерживаемая версия checkpoint: {data.get('version')}"
            )
        if data.get("targets") != self.targets:
            raise PhaseAError(
                "список targets не совпадает с checkpoint; используй тот же порядок "
                "или отдельный --checkpoint"
            )
        if not isinstance(data.get("servers"), dict):
            raise PhaseAError("checkpoint не содержит корректный объект servers")
        return data

    def reload(self) -> None:
        """Refresh an existing checkpoint after the cross-process global lock."""

        if not self.path.exists():
            raise PhaseAError(f"checkpoint disappeared: {self.path}")
        self.data = self._load_or_create(reset=False)

    def _write(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.tmp.{os.getpid()}")
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass

    def mark(
        self,
        server_id: str,
        *,
        status: str,
        stage: str,
        details: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = _utc_now()
        entry = dict(self.data["servers"].get(server_id) or {})
        entry.update(
            {
                "status": status,
                "stage": stage,
                "updated_at": now,
            }
        )
        if details is not None:
            entry["details"] = dict(details)
        if error:
            entry["error"] = str(error)[:1000]
        else:
            entry.pop("error", None)
        self.data["servers"][server_id] = entry
        self.data["updated_at"] = now
        self._write(self.data)


@dataclass(frozen=True)
class CatalogState:
    expected_rows: int
    expected_articles: int
    actual_rows: int
    actual_articles: int
    mismatched_rows: int
    missing_rows: int = 0
    unexpected_rows: int = 0
    duplicate_rows: int = 0
    ownership_mismatches: int = 0

    @property
    def complete(self) -> bool:
        return (
            self.expected_rows > 0
            and self.expected_articles > 0
            and self.actual_rows == self.expected_rows
            and self.actual_articles == self.expected_articles
            and self.mismatched_rows == 0
            and self.missing_rows == 0
            and self.unexpected_rows == 0
            and self.duplicate_rows == 0
            and self.ownership_mismatches == 0
        )

    @property
    def baseline(self) -> str:
        if self.actual_rows == 0:
            return "empty"
        return "complete" if self.complete else "partial"


@dataclass(frozen=True)
class EnrichmentState:
    articles: int
    enriched: int

    @property
    def complete(self) -> bool:
        return self.articles > 0 and self.enriched == self.articles


def _row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


_CHAPTER_PRIMARY_PREAMBLE_LIMIT = 2_048
_CHAPTER_PRIMARY_MIN_BODY_CHARS = 120
_CHAPTER_PRIMARY_MIN_BODY_LETTERS = 60
_SOURCE_FORMATTING_TRANSLATION = str.maketrans(
    {
        "\u200b": None,
        "\u200c": None,
        "\u200d": None,
        "\u2060": None,
        "\ufeff": None,
    }
)


def _visible_source_text(value: Any) -> str:
    """Collapse forum-only whitespace without changing the source itself."""

    text = str(value or "").translate(_SOURCE_FORMATTING_TRANSLATION)
    return re.sub(r"\s+", " ", text).strip()


def _chapter_primary_code_value(code: Any) -> tuple[str, int] | None:
    """Return one comparable numeric/Roman chapter code, fail-closed."""

    from services.law_structural_code import canonical_roman_value

    text = str(code or "").strip()
    if text.isdigit():
        return "numeric", int(text)
    roman_value = canonical_roman_value(text)
    if roman_value is not None:
        return "roman", roman_value
    return None


def _is_chapter_primary_document(
    content: str,
    rows: Sequence[Mapping[str, Any]],
    header_plan: Any,
    *,
    max_unstructured_chars: int,
) -> bool:
    """Accept an audited law whose only primary legal units are chapters.

    This is deliberately only a preflight classification.  It neither changes
    parser/catalog rows nor promotes chapters to articles.  The proof is
    document-wide and source-offset based so a long empty header cannot imitate
    a substantive chapter body.
    """

    from services.law_catalog_service import normalize_header

    chapter_rows = [row for row in rows if row["kind"] == "chapter"]
    other_rows = [row for row in rows if row["kind"] == "other"]
    if (
        any(row["kind"] == "article" for row in rows)
        or header_plan.structured_article_count != 0
        or len(chapter_rows) < 2
        or any(row["kind"] not in {"other", "chapter"} for row in rows)
        or len(other_rows) > 1
        or (
            other_rows
            and other_rows[0].get("source_order") != 0
        )
        or any(
            len(_visible_source_text(row.get("full_text")))
            >= min(
                max_unstructured_chars,
                _CHAPTER_PRIMARY_PREAMBLE_LIMIT,
            )
            for row in other_rows
        )
        or header_plan.legacy_bare_candidates
        or header_plan.mixed_special_candidates
        or header_plan.judicial_material_candidates
        or header_plan.recovered_parent_articles
        or header_plan.recovery_unresolved_bare_candidates
        or header_plan.article_dialect_status != "not_applicable"
        or header_plan.fallback_mode is not None
    ):
        return False

    code_values = [
        _chapter_primary_code_value(row.get("code"))
        for row in chapter_rows
    ]
    if (
        any(value is None for value in code_values)
        or len({value[0] for value in code_values if value is not None}) != 1
        or any(
            previous[1] >= following[1]
            for previous, following in zip(
                code_values,
                code_values[1:],
            )
            if previous is not None and following is not None
        )
    ):
        return False

    boundaries = sorted(header_plan.matches, key=lambda match: match.start())
    if (
        len(boundaries) != len(chapter_rows)
        or any(
            normalize_header(boundary.group(1))[0] != "chapter"
            for boundary in boundaries
        )
    ):
        return False

    for index, boundary in enumerate(boundaries):
        body_end = (
            boundaries[index + 1].start()
            if index + 1 < len(boundaries)
            else len(content)
        )
        if boundary.end() > body_end:
            return False
        body = _visible_source_text(content[boundary.end() : body_end])
        if (
            len(body) < _CHAPTER_PRIMARY_MIN_BODY_CHARS
            or sum(character.isalpha() for character in body)
            < _CHAPTER_PRIMARY_MIN_BODY_LETTERS
        ):
            return False
    return True


def _analyse_headers(
    docs: Sequence[Mapping[str, Any]],
    *,
    max_unstructured_chars: int,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Аудитирует форматы через те же regex/splitter/catalog parser, что и core."""
    from services.indexing_service import (
        _header_match_plan,
    )
    from services.law_catalog_service import _catalog_rows

    totals = {
        "documents": len(docs),
        "source_chars": 0,
        "structured_article_headers": 0,
        "bare_header_candidates": 0,
        "mixed_bare_header_candidates": 0,
        "accepted_mixed_bare_headers": 0,
        "mixed_bare_underfilled_chapters": 0,
        "recovered_parent_articles": 0,
        "recovery_unresolved_bare_headers": 0,
        "judicial_material_candidates": 0,
        "accepted_judicial_materials": 0,
        "expected_catalog_rows": 0,
        "expected_articles": 0,
        "documents_using_bare_fallback": 0,
        "documents_using_mixed_bare_fallback": 0,
        "documents_using_parent_recovery": 0,
        "documents_using_judicial_materials": 0,
        "documents_using_article_dialect": 0,
        "documents_using_article_dialect_true_nm": 0,
        "documents_using_article_dialect_global": 0,
        "documents_using_article_dialect_chapter_local": 0,
        "documents_with_ambiguous_article_dialect": 0,
        "documents_using_section_primary": 0,
        "documents_using_chapter_primary": 0,
        "documents_without_articles": 0,
    }
    blockers: list[str] = []
    warnings: list[str] = []
    for doc in docs:
        doc_id = int(doc["id"])
        content = str(doc.get("content") or "")
        totals["source_chars"] += len(content)
        header_plan = _header_match_plan(content)
        structured_articles = header_plan.structured_article_count
        bare = header_plan.legacy_bare_candidates
        qualified_mixed = header_plan.mixed_special_candidates
        accepted_mixed = header_plan.accepted_mixed_special_candidates
        recovered_parent_articles = header_plan.recovered_parent_articles
        recovery_unresolved_bare = (
            header_plan.recovery_unresolved_bare_candidates
        )
        judicial_candidates = header_plan.judicial_material_candidates
        accepted_judicial = header_plan.accepted_judicial_materials
        article_dialect_status = header_plan.article_dialect_status
        article_dialect_mode = header_plan.article_dialect_mode
        article_dialect_reasons = header_plan.article_dialect_reasons
        rows = _catalog_rows(content)
        articles = sum(1 for row in rows if row["kind"] == "article")
        chapter_rows = [row for row in rows if row["kind"] == "chapter"]
        section_rows = [row for row in rows if row["kind"] == "section"]
        other_rows = [row for row in rows if row["kind"] == "other"]
        section_primary = (
            articles == 0
            and structured_articles == 0
            and len(chapter_rows) >= 2
            and len(section_rows) >= 3
            and len(other_rows) <= 1
            and all(
                row.get("code")
                for row in chapter_rows + section_rows
            )
            and all(
                len(str(row.get("full_text") or "")) < max_unstructured_chars
                for row in other_rows
            )
            and not bare
            and not qualified_mixed
            and not judicial_candidates
            and article_dialect_status == "not_applicable"
            and header_plan.fallback_mode is None
            and all(
                row["kind"] in {"other", "chapter", "section"}
                for row in rows
            )
        )
        chapter_primary = _is_chapter_primary_document(
            content,
            rows,
            header_plan,
            max_unstructured_chars=max_unstructured_chars,
        )
        totals["structured_article_headers"] += structured_articles
        totals["bare_header_candidates"] += len(bare)
        totals["mixed_bare_header_candidates"] += len(qualified_mixed)
        totals["accepted_mixed_bare_headers"] += len(accepted_mixed)
        totals["recovered_parent_articles"] += len(
            recovered_parent_articles
        )
        totals["recovery_unresolved_bare_headers"] += len(
            recovery_unresolved_bare
        )
        totals["judicial_material_candidates"] += len(judicial_candidates)
        totals["accepted_judicial_materials"] += len(accepted_judicial)
        totals["expected_catalog_rows"] += len(rows)
        totals["expected_articles"] += articles
        if article_dialect_status == "resolved":
            totals["documents_using_article_dialect"] += 1
            totals[
                f"documents_using_article_dialect_{article_dialect_mode}"
            ] += 1
        elif article_dialect_status == "ambiguous":
            totals["documents_with_ambiguous_article_dialect"] += 1
        if header_plan.fallback_mode == "bare":
            totals["documents_using_bare_fallback"] += 1
        elif header_plan.fallback_mode in {
            "mixed_special",
            "mixed_classified_chapter_tail",
        }:
            totals["documents_using_mixed_bare_fallback"] += 1
        elif header_plan.fallback_mode == "mixed_parent_recovery":
            totals["documents_using_parent_recovery"] += 1
        elif header_plan.fallback_mode == "judicial_materials":
            totals["documents_using_judicial_materials"] += 1
        elif section_primary:
            totals["documents_using_section_primary"] += 1
        elif chapter_primary:
            totals["documents_using_chapter_primary"] += 1
        if articles == 0:
            totals["documents_without_articles"] += 1
            if (
                header_plan.fallback_mode != "judicial_materials"
                and not section_primary
                and not chapter_primary
            ):
                warnings.append(
                    f"doc={doc_id}: parser produced no kind='article'; "
                    "manual header audit is required"
                )

        for reason in header_plan.judicial_rejection_reasons:
            blockers.append(f"doc={doc_id}: {reason}")
        if article_dialect_status == "ambiguous":
            details = "; ".join(article_dialect_reasons)
            suffix = f"; {details}" if details else ""
            blockers.append(
                f"doc={doc_id}: numeric article dialect is ambiguous{suffix}"
            )

        # Core намеренно включает bare fallback только с тремя уверенными
        # кандидатами. Один-два кандидата означают неразрешённый формат.
        if (
            header_plan.fallback_mode != "judicial_materials"
            and structured_articles == 0
            and 0 < len(bare) < 3
        ):
            blockers.append(
                f"doc={doc_id}: найдено bare N.M={len(bare)}, но fallback требует >=3"
            )
        for group in header_plan.mixed_special_groups:
            if group.accepted:
                continue
            totals["mixed_bare_underfilled_chapters"] += 1
            blockers.append(
                f"doc={doc_id}: Глава {group.chapter_major}: найдено "
                f"qualified mixed bare={len(group.candidates)}, но fallback "
                "требует >=3 на главу"
            )
        # Большой единый other-блок обычно означает неизвестный формат заголовков.
        if (
            len(content) >= max_unstructured_chars
            and len(rows) == 1
            and rows[0]["kind"] == "other"
        ):
            blockers.append(
                f"doc={doc_id}: {len(content)} символов без распознанной структуры"
            )
        # В документах со словом «Статья» bare N.M часто являются частями статьи.
        # Не переопределяем решение core, но сохраняем сигнал для ручного аудита.
        if recovery_unresolved_bare:
            warnings.append(
                f"doc={doc_id}: parent recovery rejected: "
                f"unresolved bare N.M={len(recovery_unresolved_bare)}"
            )
        elif (
            structured_articles > 0
            and len(bare) >= 3
            and not accepted_mixed
            and not recovered_parent_articles
        ):
            warnings.append(
                f"doc={doc_id}: mixed headers: Статья={structured_articles}, "
                f"bare N.M={len(bare)}, qualified={len(qualified_mixed)}"
                " "
                "(mixed fallback не разрешён)"
            )
    if totals["expected_catalog_rows"] <= 0:
        blockers.append("сегментация не создала ни одной строки каталога")
    if totals["expected_articles"] <= 0:
        blockers.append("сегментация не создала ни одной kind='article'")
    return totals, blockers, warnings


async def _fetch_source_documents(server_id: str) -> list[dict[str, Any]]:
    rows = await db_manager.fetch(
        """
        SELECT id, server_id, source_thread_id, title, content, content_hash,
               derived_sync_pending
        FROM law_documents
        WHERE server_id = $1
        ORDER BY id
        """,
        server_id,
    )
    return [_row_dict(row) for row in rows]


def _source_fingerprint(
    docs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compatibility wrapper around the shared Phase A/HTTP-smoke identity."""

    return law_source_fingerprint(docs)


def _source_health_blockers(
    docs: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Validate the DB source used by Overlay."""

    blockers: list[str] = []
    if not docs:
        blockers.append("law_documents пуст")
    for doc in docs:
        doc_id = int(doc["id"])
        if not str(doc.get("content") or "").strip():
            blockers.append(f"doc={doc_id}: пустой content")
        if not str(doc.get("content_hash") or "").strip():
            blockers.append(f"doc={doc_id}: пустой content_hash")
        if doc.get("source_thread_id") is None:
            blockers.append(f"doc={doc_id}: zombie source_thread_id IS NULL")
        if doc.get("derived_sync_pending") is True:
            blockers.append(f"doc={doc_id}: derived_sync_pending=TRUE")
    return blockers


async def _assert_source_snapshot(
    server_id: str,
    expected_fingerprint: Mapping[str, Any],
) -> list[dict[str, Any]]:
    current = await _fetch_source_documents(server_id)
    blockers = _source_health_blockers(current)
    actual_fingerprint = _source_fingerprint(current)
    if actual_fingerprint != dict(expected_fingerprint):
        blockers.append(
            "source snapshot изменился во время Phase A: "
            f"expected={expected_fingerprint}, actual={actual_fingerprint}"
        )
    if blockers:
        raise PhaseAError(
            f"{server_id}: source revalidation failed: " + "; ".join(blockers)
        )
    return current


def _expected_catalog_rows(
    docs: Sequence[Mapping[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    result: dict[tuple[int, int], dict[str, Any]] = {}
    server_ids = {
        str(doc.get("server_id") or "")
        for doc in docs
    }
    if len(server_ids) != 1 or "" in server_ids:
        raise PhaseAError(
            "source documents do not have one exact server owner"
        )
    server_id = next(iter(server_ids))
    try:
        expected_rows = build_expected_law_catalog_rows(
            server_id,
            docs,
        )
    except LawCatalogTruthError as exc:
        raise PhaseAError(
            f"{server_id}: shared parser contract cannot build expected "
            "catalog"
        ) from exc
    for row in expected_rows:
        key = (
            int(row["law_document_id"]),
            int(row["source_order"]),
        )
        result[key] = {
            "server_id": str(row["server_id"]),
            "article_ref": row.get("article_ref"),
            "kind": str(row["kind"]),
            "code": row.get("code"),
            "title": row.get("title"),
            "doc_title": row.get("doc_title"),
            "full_text": str(row["full_text"]),
            "article_hash": str(row["article_hash"]),
        }
    return result


def _safe_catalog_fallback_title(value: Any) -> bool:
    """Accept absent or bounded non-junk LLM title when parser found no title."""

    from services.law_catalog_service import _is_junk_title

    if value is None:
        return True
    if not isinstance(value, str):
        return False
    title = value.strip()
    if (
        len(title) < 2
        or len(title) > 300
        or "\x00" in title
        or any(ord(char) < 32 for char in title)
    ):
        return False
    return not _is_junk_title(title)


def _catalog_row_matches(
    actual: Mapping[str, Any],
    wanted: Mapping[str, Any],
) -> bool:
    """Compare every source-derived field, with the one intentional title exception."""

    if any(
        (
            str(actual.get("server_id") or "") != wanted["server_id"],
            actual.get("article_ref") != wanted["article_ref"],
            str(actual.get("kind") or "") != wanted["kind"],
            actual.get("code") != wanted["code"],
            actual.get("doc_title") != wanted["doc_title"],
            str(actual.get("full_text") or "") != wanted["full_text"],
            str(actual.get("article_hash") or "") != wanted["article_hash"],
        )
    ):
        return False
    parser_title = wanted["title"]
    if parser_title is not None:
        return actual.get("title") == parser_title
    return _safe_catalog_fallback_title(actual.get("title"))


async def _catalog_state(
    server_id: str,
    docs: Sequence[Mapping[str, Any]],
) -> CatalogState:
    expected = _expected_catalog_rows(docs)
    rows = await db_manager.fetch(
        """
        SELECT a.id, a.law_document_id, a.server_id, a.source_order,
               a.article_ref, a.kind, a.code, a.title, a.doc_title,
               a.full_text, a.article_hash, a.title_provenance,
               d.server_id AS parent_server_id
        FROM law_articles a
        LEFT JOIN law_documents d ON d.id = a.law_document_id
        WHERE a.server_id = $1 OR d.server_id = $1
        ORDER BY a.law_document_id, a.source_order, a.id
        """,
        server_id,
    )
    actual_by_key: dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    ownership_mismatches = 0
    for row in rows:
        key = (int(row["law_document_id"]), int(row["source_order"]))
        actual_by_key.setdefault(key, []).append(row)
        if (
            str(row.get("server_id") or "") != server_id
            or str(row.get("parent_server_id") or "") != server_id
        ):
            ownership_mismatches += 1

    expected_keys = set(expected)
    actual_keys = set(actual_by_key)
    missing_keys = expected_keys - actual_keys
    unexpected_keys = actual_keys - expected_keys
    duplicate_rows = sum(
        max(0, len(group) - 1) for group in actual_by_key.values()
    )
    mismatched = 0
    for key in expected_keys & actual_keys:
        group = actual_by_key[key]
        if len(group) != 1:
            continue
        if not _catalog_row_matches(group[0], expected[key]):
            mismatched += 1
    preliminary_complete = not any((
        mismatched,
        len(missing_keys),
        len(unexpected_keys),
        duplicate_rows,
        ownership_mismatches,
    ))
    if preliminary_complete:
        try:
            verify_law_documents_catalog_truth(
                server_id,
                docs,
                rows,
            )
        except LawCatalogTruthError:
            # Keep Phase A's existing reconcile flow: a truth-contract
            # failure makes the catalog incomplete, so the normal catalog
            # builder gets one chance to repair it.  Final verification calls
            # this same proof again and remains fail-closed.
            mismatched += 1
    return CatalogState(
        expected_rows=len(expected),
        expected_articles=sum(
            1 for row in expected.values() if row["kind"] == "article"
        ),
        actual_rows=len(rows),
        actual_articles=sum(
            1 for row in rows if str(row.get("kind") or "") == "article"
        ),
        mismatched_rows=mismatched,
        missing_rows=len(missing_keys),
        unexpected_rows=len(unexpected_keys),
        duplicate_rows=duplicate_rows,
        ownership_mismatches=ownership_mismatches,
    )


async def _enrichment_state(server_id: str) -> EnrichmentState:
    from services.law_enrichment_completion import (
        evaluate_law_enrichment_completion,
    )

    state = await evaluate_law_enrichment_completion(
        db_manager,
        server_id,
    )
    return EnrichmentState(
        articles=state.articles,
        enriched=state.enriched,
    )


async def preflight_server(
    server_id: str,
    *,
    max_unstructured_chars: int,
    allow_header_warnings: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Полный read-only preflight одного server_id."""
    configured = get_flat_servers()[server_id]
    server_row = await db_manager.fetchrow(
        """
        SELECT s.server_id, s.project_id, s.is_disabled AS server_disabled,
               p.project_id AS existing_project_id,
               p.is_disabled AS project_disabled
        FROM servers s
        LEFT JOIN projects p ON p.project_id = s.project_id
        WHERE s.server_id = $1
        """,
        server_id,
    )
    blockers: list[str] = []
    warnings: list[str] = []
    if not server_row:
        raise PhaseAError(f"{server_id}: отсутствует строка servers")
    expected_project = configured.get("project_id")
    if server_row["project_id"] != expected_project:
        blockers.append(
            f"project_id БД={server_row['project_id']!r}, config={expected_project!r}"
        )
    if server_row["existing_project_id"] is None:
        blockers.append("project row отсутствует в БД")
    if bool(server_row["server_disabled"]):
        blockers.append("сервер выключен в БД")
    if bool(server_row["project_disabled"]):
        blockers.append("проект выключен в БД")

    docs = await _fetch_source_documents(server_id)
    blockers.extend(_source_health_blockers(docs))

    header_stats, header_blockers, header_warnings = _analyse_headers(
        docs,
        max_unstructured_chars=max_unstructured_chars,
    )
    blockers.extend(header_blockers)
    warnings.extend(header_warnings)
    if header_warnings and not allow_header_warnings:
        blockers.append(
            "header audit warnings require explicit --allow-header-warnings: "
            + "; ".join(header_warnings)
        )

    catalog = await _catalog_state(server_id, docs)
    source_fingerprint = _source_fingerprint(docs)
    summary = {
        "project_id": expected_project,
        "documents": len(docs),
        "legacy_derived_sync_pending": sum(
            1 for doc in docs if doc.get("derived_sync_pending") is True
        ),
        "source_fingerprint": source_fingerprint,
        "headers": header_stats,
        "legacy_rag_observation": dict(LEGACY_RAG_OBSERVATION),
        "catalog": {**asdict(catalog), "baseline": catalog.baseline},
        "warnings": warnings,
    }
    if blockers:
        raise PhaseAError(f"{server_id}: preflight blocked: " + "; ".join(blockers))
    return docs, summary

def validate_catalog_dry_run(
    server_id: str,
    summary: Mapping[str, Any],
    *,
    expected_documents: int,
    expected_rows: int,
) -> None:
    """Запрещает ambiguous/destructive/partial dry-run."""
    if int(summary.get("documents") or 0) != expected_documents:
        raise PhaseAError(
            f"{server_id}: catalog dry-run documents="
            f"{summary.get('documents')}/{expected_documents}"
        )
    if int(summary.get("articles") or 0) != expected_rows:
        raise PhaseAError(
            f"{server_id}: catalog dry-run rows="
            f"{summary.get('articles')}/{expected_rows}"
        )
    if int(summary.get("ambiguous") or 0) > 0:
        raise PhaseAError(
            f"{server_id}: catalog dry-run ambiguous={summary.get('ambiguous')}"
        )
    # Phase A — первичная/возобновляемая сборка. Не разрешаем ей незаметно
    # превращаться в destructive rebuild изменившегося production-каталога.
    if int(summary.get("deleted") or 0) > 0:
        raise PhaseAError(
            f"{server_id}: catalog dry-run хочет удалить "
            f"{summary.get('deleted')} строк; нужен отдельный аудит"
        )


async def _ensure_catalog_and_enrichment_locked(
    server_id: str,
    docs: Sequence[Mapping[str, Any]],
) -> tuple[CatalogState, EnrichmentState, dict[str, Any]]:
    """Dry-run → catalog reconcile → mandatory manual Codex enrichment gate."""
    from services.law_catalog_service import _build_catalog_for_server

    before = await _catalog_state(server_id, docs)
    dry_run = await _build_catalog_for_server(
        server_id,
        enrich=False,
        dry_run=True,
    )
    validate_catalog_dry_run(
        server_id,
        dry_run,
        expected_documents=len(docs),
        expected_rows=before.expected_rows,
    )

    # Completion deliberately refuses a catalog that cannot be replayed from
    # the current source.  It is therefore meaningful only after catalog truth
    # is established.  If Phase A found drift, let the normal reconciler repair
    # it before invoking the completion gate.
    enrich_before: EnrichmentState | None = None
    if before.complete:
        enrich_before = await _enrichment_state(server_id)
    build_summary: dict[str, Any] = {}
    if not before.complete:
        build_summary = await _build_catalog_for_server(
            server_id,
            enrich=False,
            dry_run=False,
        )
        if int((build_summary or {}).get("documents") or 0) != len(docs):
            raise PhaseAError(
                f"{server_id}: catalog build обработал не все документы: "
                f"{(build_summary or {}).get('documents')}/{len(docs)}"
            )
        if int((build_summary or {}).get("failed_documents") or 0) != 0:
            raise PhaseAError(
                f"{server_id}: catalog build failed_documents="
                f"{build_summary.get('failed_documents')}"
            )
    elif enrich_before is not None and enrich_before.complete:
        log.info("[%s] каталог и enrichment уже полные", server_id)

    catalog = await _catalog_state(server_id, docs)
    if not catalog.complete:
        raise PhaseAError(
            f"{server_id}: catalog verification failed: {asdict(catalog)}"
        )

    enrichment = await _enrichment_state(server_id)
    return catalog, enrichment, {
        "dry_run": dict(dry_run),
        "build": dict(build_summary),
        "enrichment_mode": MANUAL_ENRICHMENT_MODE,
        "awaiting_enrichment": not enrichment.complete,
        "cloud_calls_allowed": False,
    }


async def ensure_catalog_and_enrichment(
    server_id: str,
    docs: Sequence[Mapping[str, Any]],
) -> tuple[CatalogState, EnrichmentState, dict[str, Any]]:
    """Self-owned plan/mutation over one immutable source generation."""

    from services.law_document_lock import law_document_server_lock
    from services.law_embedding_service import catalog_server_lock

    async with law_document_server_lock(server_id, manager=db_manager):
        async with catalog_server_lock(server_id):
            return await _ensure_catalog_and_enrichment_locked(
                server_id,
                docs,
            )


async def _vector_state(server_id: str):
    # Это ровно runtime guard поиска; отдельную формулу совместимости не копируем.
    from services.law_embedding_service import active_space_id
    from services.law_search_service import _vector_corpus_state

    return await _vector_corpus_state(server_id, active_space_id())


async def _related_queue_state(server_id: str) -> dict[str, int]:
    from services.law_embedding_service import active_space_id

    max_attempts = int(getattr(config, "LAW_EMBED_JOB_MAX_ATTEMPTS", 5))
    row = await db_manager.fetchrow(
        """
        SELECT count(*)::int AS total,
               count(*) FILTER (
                   WHERE status = 'failed' AND attempts >= $3
               )::int AS terminal
        FROM law_related_rebuild_jobs
        WHERE server_id = $1 AND space_id = $2
        """,
        server_id,
        active_space_id(),
        max_attempts,
    )
    embedding_jobs = await db_manager.fetchrow(
        """
        SELECT count(*)::int AS total,
               count(*) FILTER (
                   WHERE status IN ('pending', 'processing')
               )::int AS active,
               count(*) FILTER (
                   WHERE status = 'failed' AND attempts >= $3
               )::int AS terminal
        FROM law_embedding_jobs
        WHERE server_id = $1 AND space_id = $2
        """,
        server_id,
        active_space_id(),
        max_attempts,
    )
    return {
        "total": int(row["total"] or 0),
        "terminal": int(row["terminal"] or 0),
        "embedding_jobs": int(embedding_jobs["total"] or 0),
        "active_embeddings": int(embedding_jobs["active"] or 0),
        "terminal_embeddings": int(embedding_jobs["terminal"] or 0),
    }


def _serializable_vector_state(state: Any) -> dict[str, Any]:
    return {
        "total": int(state.total),
        "ready": int(state.ready),
        "queued": int(state.queued),
        "failed": int(state.failed),
        "mismatched": int(state.mismatched),
        "complete": bool(state.complete),
        "compatible": bool(state.compatible),
        "recipe_version": state.recipe_version,
        "recipe_compatible": (
            state.recipe_version == config.LAW_CATALOG_EMBED_RECIPE_VERSION
        ),
        "runtime_compatible": bool(state.runtime_compatible),
    }


async def wait_for_frida_and_related(
    server_id: str,
    *,
    expected_articles: int,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    """Только наблюдает durable worker; не запускает FRIDA inference в CLI."""
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("FRIDA timeout/poll должны быть положительными")
    deadline = time.monotonic() + timeout_seconds
    last_signature: tuple[Any, ...] | None = None
    while True:
        vector = await _vector_state(server_id)
        related = await _related_queue_state(server_id)
        state = _serializable_vector_state(vector)
        if state["total"] != expected_articles:
            raise PhaseAError(
                f"{server_id}: FRIDA total={state['total']}, "
                f"catalog articles={expected_articles}"
            )
        if (
            not state["compatible"]
            or not state["recipe_compatible"]
            or not state["runtime_compatible"]
        ):
            raise PhaseAError(
                f"{server_id}: несовместимый FRIDA passport/runtime: {state}"
            )
        if related["terminal"] or related["terminal_embeddings"]:
            raise PhaseAError(
                f"{server_id}: terminal FRIDA/related jobs: {related}"
            )
        if (
            state["complete"]
            and related["total"] == 0
            and related["embedding_jobs"] == 0
        ):
            return {"vector": state, "related": related}

        signature = (
            state["ready"],
            state["total"],
            state["queued"],
            state["failed"],
            state["mismatched"],
            related["total"],
            related["embedding_jobs"],
        )
        if signature != last_signature:
            log.info(
                "[%s] ждём FRIDA/related: ready=%d/%d queued=%d failed=%d "
                "mismatched=%d related=%d",
                server_id,
                state["ready"],
                state["total"],
                state["queued"],
                state["failed"],
                state["mismatched"],
                related["total"],
            )
            last_signature = signature
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PhaseAError(
                f"{server_id}: timeout ожидания FRIDA/related: "
                f"vector={state}, related={related}"
            )
        await asyncio.sleep(min(poll_seconds, remaining))


async def final_verify(
    server_id: str,
    docs: Sequence[Mapping[str, Any]],
    *,
    expected_source_fingerprint: Mapping[str, Any],
) -> dict[str, Any]:
    await _assert_source_snapshot(server_id, expected_source_fingerprint)
    catalog = await _catalog_state(server_id, docs)
    enrichment = await _enrichment_state(server_id)
    vector = await _vector_state(server_id)
    related = await _related_queue_state(server_id)
    vector_data = _serializable_vector_state(vector)
    failures: list[str] = []
    if not catalog.complete:
        failures.append(f"catalog={asdict(catalog)}")
    if not enrichment.complete:
        failures.append(f"enrichment={asdict(enrichment)}")
    if (
        vector_data["total"] != catalog.actual_articles
        or not vector_data["complete"]
        or not vector_data["compatible"]
        or not vector_data["recipe_compatible"]
        or not vector_data["runtime_compatible"]
    ):
        failures.append(f"FRIDA={vector_data}")
    if (
        related["total"]
        or related["terminal"]
        or related["embedding_jobs"]
        or related["terminal_embeddings"]
    ):
        failures.append(f"related={related}")
    if failures:
        raise PhaseAError(f"{server_id}: final verification failed: " + "; ".join(failures))
    return {
        "legacy_rag_observation": dict(LEGACY_RAG_OBSERVATION),
        "catalog": asdict(catalog),
        "enrichment": asdict(enrichment),
        "vector": vector_data,
        "related": related,
        "source_fingerprint": dict(expected_source_fingerprint),
    }


@dataclass(frozen=True)
class DriverOptions:
    max_unstructured_chars: int = 12_000
    frida_timeout: float = 10_800.0
    poll_interval: float = 10.0
    preflight_only: bool = False
    allow_header_warnings: bool = False

    @property
    def enrichment_mode(self) -> str:
        return MANUAL_ENRICHMENT_MODE


def _manual_enrichment_next_step(
    server_id: str,
    *,
    allow_header_warnings: bool,
) -> dict[str, Any]:
    output_dir = f"/app/data/law-enrichment/{server_id}-RUN_ID"
    reviewed_dir = output_dir
    envelope_path = f"{output_dir}/reviewed-all-envelope-v3.json"
    evidence_path = f"{output_dir}/reviewed-all-evidence-v3.json"
    resume_command = (
        f"python /app/scripts/backfill_law_servers.py {server_id}"
    )
    if allow_header_warnings:
        resume_command += " --allow-header-warnings"
    return {
        "action": "export_review_apply_codex_artifacts",
        "allow_header_warnings": bool(allow_header_warnings),
        "explanation": (
            "Каталог готов, но не все статьи имеют reviewed enrichment. "
            "Выгрузи source batches, подготовь все reviewed batches вручную "
            "через Codex, собери их официальным offline builder в один "
            "source/catalog/context-bound native-v3 envelope и примени его одной атомарной "
            "операцией. Затем повтори Phase A с тем же checkpoint."
        ),
        "export_command": (
            "python /app/scripts/export_law_enrichment_batches.py "
            f"{server_id} --output-dir {output_dir}"
        ),
        "aggregate_command_template": (
            "python /app/scripts/build_law_enrichment_apply_plan.py "
            f"{server_id} --export-dir {output_dir} "
            f"--reviewed-dir {reviewed_dir} "
            f"--output-plan {envelope_path} "
            f"--output-evidence {evidence_path}"
        ),
        "apply_command": (
            "python /app/scripts/_apply_claude_enrich.py "
            f"{server_id} {envelope_path}"
        ),
        "resume_command": resume_command,
    }


async def process_server(
    server_id: str,
    *,
    store: CheckpointStore,
    options: DriverOptions,
) -> None:
    from services.law_document_lock import law_document_server_lock
    from services.law_embedding_service import catalog_server_lock

    enrichment_mode = options.enrichment_mode
    requested_allow_header_warnings = bool(options.allow_header_warnings)
    stored_header_warning_audit = _resume_header_warning_audit(
        store.data["servers"].get(server_id),
        requested_allow=requested_allow_header_warnings,
    )

    # Match the normal forum-sync lock order: source generation first, catalog
    # second.  This freezes one exact source snapshot through structural
    # verification without inventing a one-off production namespace.
    async with law_document_server_lock(server_id, manager=db_manager):
        try:
            docs, preflight = await preflight_server(
                server_id,
                max_unstructured_chars=options.max_unstructured_chars,
                allow_header_warnings=options.allow_header_warnings,
            )
        except Exception as exc:
            if stored_header_warning_audit is not None:
                raise HeaderWarningAuditError(
                    f"{server_id}: current preflight cannot prove the stored "
                    "header-warning audit; preserve the checkpoint and "
                    "re-audit with --reset-checkpoint"
                ) from exc
            raise
        source_fingerprint = preflight["source_fingerprint"]
        header_warning_audit = (
            _header_warning_audit_contract(
                source_fingerprint,
                preflight["warnings"],
            )
            if requested_allow_header_warnings
            else None
        )
        if (
            stored_header_warning_audit is not None
            and stored_header_warning_audit != header_warning_audit
        ):
            raise HeaderWarningAuditError(
                f"{server_id}: source fingerprint or exact header-warning "
                "set changed since the audited checkpoint; use "
                "--reset-checkpoint and re-audit"
            )
        run_contract = {
            "enrichment_mode": enrichment_mode,
            "allow_header_warnings": requested_allow_header_warnings,
            "header_warning_audit": header_warning_audit,
        }
        store.mark(
            server_id,
            status="in_progress",
            stage="preflight",
            details={
                **preflight,
                **run_contract,
            },
        )
        log.info(
            "[%s] preflight: docs=%d, legacy RAG untouched, "
            "catalog=%s, expected articles=%d",
            server_id,
            preflight["documents"],
            preflight["catalog"]["baseline"],
            preflight["headers"]["expected_articles"],
        )
        for warning in preflight["warnings"]:
            log.warning("[%s] preflight warning: %s", server_id, warning)
        if options.preflight_only:
            store.mark(
                server_id,
                status="preflight_ok",
                stage="preflight",
                details={
                    **preflight,
                    **run_contract,
                },
            )
            return

        # Keep one catalog lock across the audited plan and its mutation.  It
        # must be released before waiting: the related worker takes this same
        # lock while rebuilding the graph.
        async with catalog_server_lock(server_id):
            catalog, enrichment, catalog_run = (
                await _ensure_catalog_and_enrichment_locked(
                    server_id,
                    docs,
                )
            )
            store.mark(
                server_id,
                status="in_progress",
                stage="catalog",
                details={
                    "catalog": asdict(catalog),
                    "enrichment": asdict(enrichment),
                    "run": catalog_run,
                    "source_fingerprint": source_fingerprint,
                    **run_contract,
                },
            )
            await _assert_source_snapshot(server_id, source_fingerprint)

        if not enrichment.complete:
            next_step = _manual_enrichment_next_step(
                server_id,
                allow_header_warnings=options.allow_header_warnings,
            )
            store.mark(
                server_id,
                status="awaiting_enrichment",
                stage="awaiting_enrichment",
                details={
                    "catalog": asdict(catalog),
                    "enrichment": asdict(enrichment),
                    "run": catalog_run,
                    "source_fingerprint": source_fingerprint,
                    **run_contract,
                    "next_step": next_step,
                },
            )
            log.warning(
                "[%s] AWAITING_ENRICHMENT: %d/%d current; export: %s",
                server_id,
                enrichment.enriched,
                enrichment.articles,
                next_step["export_command"],
            )
            return

        frida = await wait_for_frida_and_related(
            server_id,
            expected_articles=catalog.actual_articles,
            timeout_seconds=options.frida_timeout,
            poll_seconds=options.poll_interval,
        )
        store.mark(
            server_id,
            status="in_progress",
            stage="frida",
            details={
                **frida,
                "source_fingerprint": source_fingerprint,
                **run_contract,
            },
        )

        # Briefly reacquire after the worker drains.  The document lock still
        # freezes the source generation, and this final catalog lock prevents a
        # manual catalog writer from racing the acceptance snapshot.
        async with catalog_server_lock(server_id):
            result = await final_verify(
                server_id,
                docs,
                expected_source_fingerprint=source_fingerprint,
            )
            result["catalog_run"] = catalog_run
            result.update(run_contract)
            result["smoke_challenge"] = _new_smoke_challenge()
            store.mark(
                server_id,
                status="awaiting_smoke",
                stage="awaiting_smoke",
                details=result,
            )

    log.info(
        "[%s] AWAITING_SMOKE: articles=%d enriched=%d FRIDA vectors=%d; "
        "legacy RAG untouched",
        server_id,
        result["catalog"]["actual_articles"],
        result["enrichment"]["enriched"],
        result["vector"]["ready"],
    )
    log.info(
        "[%s] smoke command requires --run-nonce %s (valid for %d minutes)",
        server_id,
        result["smoke_challenge"]["run_nonce"],
        LAW_SMOKE_MAX_AGE_SECONDS // 60,
    )


async def run(
    targets: Sequence[str],
    *,
    store: CheckpointStore,
    options: DriverOptions,
) -> None:
    if not options.preflight_only and len(targets) != 1:
        raise PhaseAError(
            "write run accepts exactly one server; run HTTP smoke and finalize "
            "its checkpoint before starting the next server"
        )
    for index, server_id in enumerate(targets, 1):
        entry = store.data["servers"].get(server_id)
        if options.preflight_only:
            # A preflight is allowed to refresh only its own prior result.
            # Never erase an active/manual/smoke/completed write lifecycle;
            # operators can choose a separate --checkpoint or explicitly reset
            # after the global lock has been acquired.
            if (
                isinstance(entry, Mapping)
                and entry
                and entry.get("status") != "preflight_ok"
            ):
                raise PhaseAError(
                    f"{server_id}: preflight refuses to overwrite checkpoint "
                    f"status={entry.get('status')!r}; use a separate "
                    "--checkpoint or explicit --reset-checkpoint"
                )
            _resume_header_warning_audit(
                entry,
                requested_allow=bool(options.allow_header_warnings),
            )
        else:
            status = entry.get("status") if isinstance(entry, Mapping) else None
            details = (
                entry.get("details")
                if isinstance(entry, Mapping)
                and isinstance(entry.get("details"), Mapping)
                else {}
            )
            stored_mode = details.get("enrichment_mode")
            if status == "complete":
                log.info("[%s] checkpoint уже complete; повторный запуск не нужен", server_id)
                continue
            if status == "awaiting_smoke":
                raise PhaseAError(
                    f"{server_id}: checkpoint awaiting_smoke; запусти "
                    "--finalize-smoke с сохранённым evidence или используй "
                    "--reset-checkpoint для отдельного нового запуска"
                )
            if stored_mode and stored_mode != options.enrichment_mode:
                raise PhaseAError(
                    f"{server_id}: checkpoint enrichment_mode={stored_mode!r}, "
                    f"requested={options.enrichment_mode!r}; продолжи в прежнем "
                    "режиме или используй --reset-checkpoint"
                )
            _resume_header_warning_audit(
                entry,
                requested_allow=bool(options.allow_header_warnings),
            )
        log.info("[%d/%d] ===== %s =====", index, len(targets), server_id)
        try:
            await process_server(server_id, store=store, options=options)
        except HeaderWarningAuditError:
            # Audit drift is checked after a fresh read-only preflight but
            # before any checkpoint/catalog write. Preserve the exact active
            # checkpoint so the operator can inspect and explicitly re-audit.
            raise
        except Exception as exc:
            store.mark(
                server_id,
                status="failed",
                stage="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            # Fail closed: не начинать следующий server_id.
            raise


def validate_smoke_evidence(
    server_id: str,
    source_fingerprint: Mapping[str, Any],
    challenge: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Re-evaluate a fresh, one-time HTTP-smoke artifact fail-closed."""

    if (
        type(evidence.get("schema_version")) is not int
        or evidence.get("schema_version") != LAW_SMOKE_SCHEMA_VERSION
    ):
        raise PhaseAError("unsupported or missing smoke evidence schema_version")
    if evidence.get("server_id") != server_id:
        raise PhaseAError(
            f"smoke evidence server_id={evidence.get('server_id')!r}, "
            f"expected={server_id!r}"
        )
    expected_fingerprint = dict(source_fingerprint)
    before = evidence.get("source_fingerprint")
    after = evidence.get("source_fingerprint_after")
    if not isinstance(before, Mapping) or dict(before) != expected_fingerprint:
        raise PhaseAError("smoke evidence belongs to a different source snapshot")
    if not isinstance(after, Mapping) or dict(after) != expected_fingerprint:
        raise PhaseAError("smoke evidence ended on a different source snapshot")
    if evidence.get("source_stable") is not True:
        raise PhaseAError("smoke evidence does not attest a stable source")

    expected_nonce = challenge.get("run_nonce")
    actual_nonce = evidence.get("run_nonce")
    if (
        not isinstance(expected_nonce, str)
        or len(expected_nonce) != 64
        or not isinstance(actual_nonce, str)
        or not secrets.compare_digest(expected_nonce, actual_nonce)
    ):
        raise PhaseAError("smoke evidence nonce does not match awaiting_smoke")
    if challenge.get("consumed_at") is not None:
        raise PhaseAError("smoke challenge was already consumed")

    issued_at = _parse_utc_timestamp(
        challenge.get("issued_at"),
        field="smoke_challenge.issued_at",
    )
    expires_at = _parse_utc_timestamp(
        challenge.get("expires_at"),
        field="smoke_challenge.expires_at",
    )
    canonical_expires = issued_at + timedelta(
        seconds=LAW_SMOKE_MAX_AGE_SECONDS
    )
    if expires_at != canonical_expires:
        raise PhaseAError("smoke challenge has a non-canonical expiry")
    generated_at = _parse_utc_timestamp(
        evidence.get("generated_at"),
        field="smoke.generated_at",
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    skew = timedelta(seconds=LAW_SMOKE_CLOCK_SKEW_SECONDS)
    if generated_at < issued_at - skew:
        raise PhaseAError("smoke evidence predates its awaiting_smoke challenge")
    if generated_at > current + skew:
        raise PhaseAError("smoke evidence timestamp is in the future")
    if generated_at > expires_at or current > expires_at:
        raise PhaseAError("smoke evidence/challenge is stale")
    if current - generated_at > timedelta(seconds=LAW_SMOKE_MAX_AGE_SECONDS):
        raise PhaseAError("smoke evidence exceeded maximum freshness")

    cases = evidence.get("cases")
    if not isinstance(cases, list) or len(cases) != LAW_SMOKE_CASES:
        raise PhaseAError(
            f"smoke evidence must contain exactly {LAW_SMOKE_CASES} cases"
        )
    if (
        type(evidence.get("http_searches")) is not int
        or evidence.get("http_searches") != LAW_SMOKE_CASES
    ):
        raise PhaseAError("smoke http_searches counter is inconsistent")
    if (
        type(evidence.get("max_score")) is not int
        or evidence.get("max_score") != LAW_SMOKE_CASES
    ):
        raise PhaseAError("smoke max_score counter is inconsistent")

    score = 0
    failures: list[str] = []
    normalized: list[dict[str, Any]] = []
    manual_review: list[str] = []
    for index, raw in enumerate(cases):
        if not isinstance(raw, Mapping):
            failures.append(f"case[{index}] is not an object")
            continue
        try:
            computed = recompute_case_result(raw, index)
        except (KeyError, TypeError, ValueError) as exc:
            failures.append(f"case[{index}] contract: {exc}")
            continue
        for claim in ("technical_full", "status", "point"):
            if (
                type(raw.get(claim)) is not type(computed[claim])
                or raw.get(claim) != computed[claim]
            ):
                failures.append(
                    f"case[{index}] {claim}={raw.get(claim)!r} does not match "
                    f"recomputed={computed[claim]!r}"
                )
        if computed["technical_full"] is not True:
            failures.append(
                f"case[{index}] is not HTTP 200/full/non-degraded/warning-free"
            )
        score += int(computed["point"])
        if computed["status"] == "manual_review":
            manual_review.append(str(raw.get("name") or ""))
        item = dict(raw)
        item.update(computed)
        normalized.append(item)
    if failures:
        raise PhaseAError("invalid smoke evidence: " + "; ".join(failures))
    if (
        type(evidence.get("score")) is not int
        or evidence.get("score") != score
    ):
        raise PhaseAError("smoke score counter is inconsistent with cases")
    if score < LAW_SMOKE_MIN_SCORE:
        raise PhaseAError(
            f"smoke quality {score}/{LAW_SMOKE_CASES} is below "
            f"{LAW_SMOKE_MIN_SCORE}/{LAW_SMOKE_CASES}"
        )
    if evidence.get("manual_review") != manual_review:
        raise PhaseAError("smoke manual_review counter is inconsistent with cases")
    if evidence.get("technical_full") is not True:
        raise PhaseAError("smoke technical_full counter is inconsistent with cases")
    if evidence.get("passed") is not True:
        raise PhaseAError("smoke producer did not accept this run")
    return {
        "schema_version": LAW_SMOKE_SCHEMA_VERSION,
        "server_id": server_id,
        "source_fingerprint": expected_fingerprint,
        "generated_at": generated_at.isoformat(),
        "nonce_sha256": hashlib.sha256(actual_nonce.encode("ascii")).hexdigest(),
        "cases": normalized,
        "score": score,
        "max_score": LAW_SMOKE_CASES,
        "technical_full": True,
        "source_stable": True,
        "passed": True,
    }


def load_smoke_evidence(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise PhaseAError(f"cannot read smoke evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PhaseAError("smoke evidence root must be an object")
    return value


async def finalize_smoke(
    server_id: str,
    *,
    store: CheckpointStore,
    evidence: Mapping[str, Any],
) -> None:
    """Promote awaiting_smoke only for the same current source generation."""

    from services.law_document_lock import law_document_server_lock
    from services.law_embedding_service import catalog_server_lock

    # The production caller holds the cross-process global lock here. Reload
    # after acquiring it so a second finalizer cannot consume a stale in-memory
    # awaiting_smoke challenge that the first finalizer already completed.
    store.reload()
    entry = store.data["servers"].get(server_id)
    if not isinstance(entry, Mapping) or entry.get("status") != "awaiting_smoke":
        raise PhaseAError(f"{server_id}: checkpoint is not awaiting_smoke")
    details = entry.get("details")
    if not isinstance(details, Mapping):
        raise PhaseAError(f"{server_id}: checkpoint has no structural details")
    source_fingerprint = details.get("source_fingerprint")
    if not isinstance(source_fingerprint, Mapping):
        raise PhaseAError(f"{server_id}: checkpoint has no source fingerprint")
    allow_header_warnings, header_warning_audit = (
        _checkpoint_header_warning_state(
            details,
            missing_allow=False,
        )
    )
    if (
        header_warning_audit is not None
        and header_warning_audit["source_fingerprint"]
        != _canonical_source_fingerprint(source_fingerprint)
    ):
        raise HeaderWarningAuditError(
            f"{server_id}: header-warning audit is bound to another source; "
            "use --reset-checkpoint and re-audit"
        )
    challenge = details.get("smoke_challenge")
    if not isinstance(challenge, Mapping):
        raise PhaseAError(f"{server_id}: checkpoint has no smoke challenge")
    smoke = validate_smoke_evidence(
        server_id,
        source_fingerprint,
        challenge,
        evidence,
    )

    async with law_document_server_lock(server_id, manager=db_manager):
        docs = await _assert_source_snapshot(server_id, source_fingerprint)
        async with catalog_server_lock(server_id):
            structural = await final_verify(
                server_id,
                docs,
                expected_source_fingerprint=source_fingerprint,
            )
            # Lock acquisition and structural verification may take long
            # enough for otherwise valid evidence to expire. Re-evaluate at
            # the commit point while both source/catalog generations remain
            # frozen; never promote stale evidence to ``complete``.
            smoke = validate_smoke_evidence(
                server_id,
                source_fingerprint,
                challenge,
                evidence,
            )
            structural["enrichment_mode"] = details.get(
                "enrichment_mode",
                MANUAL_ENRICHMENT_MODE,
            )
            structural["allow_header_warnings"] = allow_header_warnings
            structural["header_warning_audit"] = header_warning_audit
            structural["smoke"] = smoke
            structural["smoke_challenge"] = {
                "issued_at": challenge.get("issued_at"),
                "expires_at": challenge.get("expires_at"),
                "consumed_at": _utc_now(),
                "nonce_sha256": smoke["nonce_sha256"],
            }
            store.mark(
                server_id,
                status="complete",
                stage="complete",
                details=structural,
            )
    log.info(
        "[%s] COMPLETE after HTTP smoke: %d/%d correct, all full",
        server_id,
        smoke["score"],
        smoke["max_score"],
    )


def default_checkpoint_path(targets: Sequence[str]) -> Path:
    if len(targets) == 1:
        return Path(config.DATA_DIR) / f"law_phase_a_{targets[0]}_checkpoint.json"
    return DEFAULT_CHECKPOINT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sequential fail-closed Overlay catalog/FRIDA backfill"
    )
    parser.add_argument(
        "servers",
        nargs="+",
        help="явные configured игровые server_id; --all намеренно отсутствует",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="durable JSON checkpoint (default: per-server path under DATA_DIR)",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="создать новый checkpoint для того же явного запуска",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="только read-only preflight, без catalog/FRIDA ожидания",
    )
    parser.add_argument(
        "--allow-header-warnings",
        action="store_true",
        help="explicitly acknowledge mixed/unsupported header warnings for this run",
    )
    parser.add_argument(
        "--finalize-smoke",
        type=Path,
        help="validate HTTP smoke JSON and promote awaiting_smoke to complete",
    )
    parser.add_argument(
        "--max-unstructured-chars",
        type=int,
        default=12_000,
        help="fail-closed порог большого единого other-блока",
    )
    parser.add_argument(
        "--frida-timeout",
        type=float,
        default=10_800.0,
        help="общий deadline ожидания dedicated FRIDA/related worker",
    )
    parser.add_argument("--poll-interval", type=float, default=10.0)
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    targets = validate_explicit_targets(args.servers)
    finalize_path = getattr(args, "finalize_smoke", None)
    if not args.preflight_only and len(targets) != 1:
        raise PhaseAError("write/finalize run accepts exactly one server")
    if finalize_path and args.preflight_only:
        raise PhaseAError("--finalize-smoke cannot be combined with --preflight-only")
    if finalize_path and args.reset_checkpoint:
        raise PhaseAError("--finalize-smoke cannot reset its awaiting checkpoint")
    if args.max_unstructured_chars <= 0:
        raise PhaseAError("--max-unstructured-chars должен быть > 0")
    if args.frida_timeout <= 0 or args.poll_interval <= 0:
        raise PhaseAError("--frida-timeout и --poll-interval должны быть > 0")

    checkpoint_path = args.checkpoint or default_checkpoint_path(targets)
    options = DriverOptions(
        max_unstructured_chars=args.max_unstructured_chars,
        frida_timeout=args.frida_timeout,
        poll_interval=args.poll_interval,
        preflight_only=args.preflight_only,
        allow_header_warnings=getattr(args, "allow_header_warnings", False),
    )
    await db_manager.connect()
    try:
        from services.law_backfill_lock import law_backfill_global_lock

        # Even preflight writes a durable checkpoint. Keep creation/reset and
        # every later mark under the same cross-process slot so a losing
        # --reset-checkpoint invocation cannot erase an active run.
        async with law_backfill_global_lock(manager=db_manager):
            store = CheckpointStore(
                checkpoint_path,
                targets,
                reset=args.reset_checkpoint,
            )
            store.reload()
            if finalize_path:
                await finalize_smoke(
                    targets[0],
                    store=store,
                    evidence=load_smoke_evidence(finalize_path),
                )
            else:
                await run(targets, store=store, options=options)
    finally:
        await db_manager.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        log.error("остановлено оператором; checkpoint сохранён")
        raise SystemExit(130)
    except Exception:
        log.exception("Phase A остановлена fail-closed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
