"""Canonical source identity for one generation of ``law_documents``.

Backfill orchestration and the independent HTTP smoke must bind their evidence
to exactly the same source generation.  Keeping the canonical serialization in
one service avoids subtly different "same source" definitions in two CLIs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


SOURCE_FINGERPRINT_VERSION = 2


def law_source_fingerprint(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a compact, order-independent fingerprint of derived inputs.

    Source text is represented by its durable ``content_hash`` so evidence does
    not copy the legal corpus.  Only fields that define the legal source are
    included.  GCS URI and timestamps belong to operational lifecycles and must
    not invalidate an Overlay-only catalog/enrichment/FRIDA run.  The generic
    derived-sync marker is enforced separately as a fail-closed readiness gate
    rather than folded into source identity.
    """

    normalized = [
        {
            "id": int(row["id"]),
            "server_id": str(row.get("server_id") or ""),
            "source_thread_id": row.get("source_thread_id"),
            "title": str(row.get("title") or ""),
            "content_hash": str(row.get("content_hash") or ""),
        }
        for row in rows
    ]
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
