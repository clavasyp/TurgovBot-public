"""Canonical source identity for one generation of ``law_documents``.

Backfill orchestration and the independent HTTP smoke must bind their evidence
to exactly the same source generation.  Keeping the canonical serialization in
one service avoids subtly different "same source" definitions in two CLIs.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping


SOURCE_FINGERPRINT_VERSION = 2
SOURCE_CONTENT_FINGERPRINT_VERSION = 3
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _positive_int(value: Any, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)


def law_source_fingerprint(
    rows: Iterable[Mapping[str, Any]],
    *,
    require_content_hash: bool = False,
) -> dict[str, Any]:
    """Return a compact, order-independent fingerprint of derived inputs.

    Source text is represented by its durable ``content_hash`` so evidence does
    not copy the legal corpus.  Only fields that define the legal source are
    included.  GCS URI and timestamps belong to operational lifecycles and must
    not invalidate an Overlay-only catalog/enrichment/FRIDA run.  The generic
    derived-sync marker is enforced separately as a fail-closed readiness gate
    rather than folded into source identity.
    """

    if not isinstance(require_content_hash, bool):
        raise TypeError("require_content_hash must be bool")
    normalized = []
    for row in rows:
        content_hash = row.get("content_hash")
        if require_content_hash and (
            not isinstance(content_hash, str)
            or not content_hash.strip()
        ):
            raise ValueError(
                "source fingerprint requires a non-empty content_hash for "
                f"law_document {row.get('id')!r}"
            )
        normalized.append({
            "id": _positive_int(
                row.get("id"),
                field="law source id",
            ),
            "server_id": str(row.get("server_id") or ""),
            "source_thread_id": _optional_positive_int(
                row.get("source_thread_id"),
                field="law source_thread_id",
            ),
            "title": str(row.get("title") or ""),
            "content_hash": str(content_hash or ""),
        })
    normalized.sort(key=lambda row: row["id"])
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "algorithm": "sha256",
        "snapshot_version": SOURCE_FINGERPRINT_VERSION,
        "documents": len(normalized),
        "value": hashlib.sha256(payload).hexdigest(),
    }


def verify_law_source_content_hashes(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Prove every legacy stored MD5/SHA digest against exact source text.

    Historical export-v1 evidence contains only the durable ``content_hash``.
    The explicit compatibility bridge may trust that evidence only after the
    same stored digest is recomputed from the live exact ``content``.
    """

    verified: list[dict[str, Any]] = []
    for row in rows:
        normalized = dict(row)
        content = normalized.get("content")
        stored = normalized.get("content_hash")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                "source content verification requires non-empty content for "
                f"law_document {normalized.get('id')!r}"
            )
        if not isinstance(stored, str) or not stored.strip():
            raise ValueError(
                "source content verification requires non-empty content_hash "
                f"for law_document {normalized.get('id')!r}"
            )
        digest = stored.strip()
        encoded = content.encode("utf-8")
        if _MD5_RE.fullmatch(digest):
            actual = hashlib.md5(
                encoded,
                usedforsecurity=False,
            ).hexdigest()
        elif _SHA256_RE.fullmatch(digest):
            actual = hashlib.sha256(encoded).hexdigest()
        else:
            raise ValueError(
                "source content_hash must be an MD5 or SHA-256 digest for "
                f"law_document {normalized.get('id')!r}"
            )
        if digest.casefold() != actual:
            raise ValueError(
                "source content_hash does not match exact content for "
                f"law_document {normalized.get('id')!r}"
            )
        verified.append(normalized)
    return verified


def law_source_content_fingerprint(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fingerprint exact source content without trusting the legacy hash.

    This v3 identity is used by new enrichment envelopes.  It hashes the
    actual text locally and therefore detects a changed source even when a
    nullable/stale/incorrect ``content_hash`` was not maintained.
    """

    normalized = []
    for row in rows:
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                "source content fingerprint requires non-empty content for "
                f"law_document {row.get('id')!r}"
            )
        normalized.append({
            "id": _positive_int(
                row.get("id"),
                field="law source id",
            ),
            "server_id": str(row.get("server_id") or ""),
            "source_thread_id": _optional_positive_int(
                row.get("source_thread_id"),
                field="law source_thread_id",
            ),
            "title": str(row.get("title") or ""),
            "content_sha256": hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest(),
        })
    normalized.sort(key=lambda row: row["id"])
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "algorithm": "sha256",
        "snapshot_version": SOURCE_CONTENT_FINGERPRINT_VERSION,
        "documents": len(normalized),
        "value": hashlib.sha256(payload).hexdigest(),
    }
