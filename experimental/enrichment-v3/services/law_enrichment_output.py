"""Semantic integrity fingerprints for persisted law enrichment output.

The article/context hashes prove which source material was reviewed.  They do
not prove that the title, penalty or auto-synonym rows still contain the
reviewed result: an older writer can mutate those fields without knowing about
the newer freshness contract.  This module hashes the persisted semantic
output so the normal loader can detect mixed-version writes and rollbacks.

Database row ids are intentionally excluded.  Replacing a row with the same
semantic value is not output drift; changing its term, state, weight or owner
is.  The version string is part of the payload so future contract changes
invalidate old fingerprints fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping, Optional

from services.law_enrichment_artifact import normalize_penalty_json


ENRICHMENT_OUTPUT_HASH_VERSION = "law-enrichment-output-v1"


def foreign_auto_ownership_article_ids(
    *,
    server_id: str,
    auto_rows: Iterable[Mapping[str, Any]],
) -> tuple[int, ...]:
    """Return articles carrying auto rows with a foreign denormalized owner.

    The output fingerprint deliberately preserves ``row.server_id`` so an
    ordinary legacy mutation changes its digest.  Digest equality itself must
    never attest ownership, though: a corrupt row remains foreign even if
    somebody recomputed and persisted a matching hash over that corruption.
    """

    if not isinstance(server_id, str) or not server_id:
        raise ValueError("server_id must be non-empty text")
    foreign: set[int] = set()
    for row in auto_rows:
        if not isinstance(row, Mapping):
            raise ValueError("auto synonym rows must be objects")
        if row.get("source") != "auto":
            raise ValueError("ownership check accepts only auto rows")
        article_id = row.get("article_id")
        if isinstance(article_id, bool) or not isinstance(article_id, int):
            raise ValueError("auto synonym article_id must be an integer")
        row_server_id = row.get("server_id")
        if not isinstance(row_server_id, str) or not row_server_id:
            raise ValueError(
                "auto synonym server_id must be non-empty text"
            )
        if row_server_id != server_id:
            foreign.add(article_id)
    return tuple(sorted(foreign))


def _optional_text(value: Any, *, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text or null")
    return value


def _normalized_weight(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("auto synonym weight must be a finite number")
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "auto synonym weight must be a finite number"
        ) from exc
    if not math.isfinite(weight):
        raise ValueError("auto synonym weight must be a finite number")
    # PostgreSQL ``real`` values are exposed as Python floats.  A canonical
    # decimal string avoids JSON encoder/version differences while retaining
    # every bit Python received from the database.
    return format(weight, ".17g")


def enrichment_output_payload(
    *,
    server_id: str,
    article_id: int,
    title: Any,
    title_provenance: Any,
    penalty_summary: Any,
    penalty_json: Any,
    auto_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the canonical semantic output represented by persisted rows."""

    if not isinstance(server_id, str) or not server_id:
        raise ValueError("server_id must be non-empty text")
    if isinstance(article_id, bool) or not isinstance(article_id, int):
        raise ValueError("article_id must be an integer")
    normalized_auto: list[dict[str, Any]] = []
    for row in auto_rows:
        if not isinstance(row, Mapping):
            raise ValueError("auto synonym rows must be objects")
        row_server_id = row.get("server_id")
        row_article_id = row.get("article_id")
        if not isinstance(row_server_id, str) or not row_server_id:
            raise ValueError(
                "auto synonym server_id must be non-empty text"
            )
        if row_article_id != article_id:
            raise ValueError(
                "auto synonym row belongs to another article"
            )
        if row.get("source") != "auto":
            raise ValueError("output fingerprint accepts only auto rows")
        suggested_by = row.get("suggested_by")
        if (
            suggested_by is not None
            and (
                isinstance(suggested_by, bool)
                or not isinstance(suggested_by, int)
            )
        ):
            raise ValueError(
                "auto synonym suggested_by must be an integer or null"
            )
        term = row.get("term")
        status = row.get("status")
        if not isinstance(term, str) or not term:
            raise ValueError("auto synonym term must be non-empty text")
        if not isinstance(status, str) or not status:
            raise ValueError("auto synonym status must be non-empty text")
        normalized_auto.append({
            # Preserve corrupt foreign ownership in the semantic fingerprint.
            # A current writer never creates it.  Consumers independently
            # reject it through ``foreign_auto_ownership_article_ids``; keeping
            # the value here makes forensic/output drift deterministic but a
            # matching digest never attests ownership.
            "server_id": row_server_id,
            "term": term,
            "weight": _normalized_weight(row.get("weight")),
            "status": status,
            "suggested_by": suggested_by,
        })
    normalized_auto.sort(
        key=lambda row: (
            row["term"],
            row["server_id"],
            row["status"],
            row["weight"],
            row["suggested_by"] is not None,
            row["suggested_by"] or 0,
        )
    )
    return {
        "version": ENRICHMENT_OUTPUT_HASH_VERSION,
        "server_id": server_id,
        "article_id": article_id,
        "title": _optional_text(title, field="title"),
        "title_provenance": _optional_text(
            title_provenance,
            field="title_provenance",
        ),
        "penalty_summary": _optional_text(
            penalty_summary,
            field="penalty_summary",
        ),
        "penalty_json": normalize_penalty_json(penalty_json),
        "auto_rows": normalized_auto,
    }


def build_enrichment_output_hash(
    *,
    server_id: str,
    article_id: int,
    title: Any,
    title_provenance: Any,
    penalty_summary: Any,
    penalty_json: Any,
    auto_rows: Iterable[Mapping[str, Any]],
) -> str:
    """Hash one article's complete persisted enrichment output."""

    encoded = json.dumps(
        enrichment_output_payload(
            server_id=server_id,
            article_id=article_id,
            title=title,
            title_provenance=title_provenance,
            penalty_summary=penalty_summary,
            penalty_json=penalty_json,
            auto_rows=auto_rows,
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
