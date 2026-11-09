"""Shared immutable source contract for reviewed law enrichment artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize_penalty_json(value: Any) -> dict[str, Any] | None:
    """Normalize asyncpg's default JSON string and already-decoded objects."""

    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("penalty_json is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("penalty_json must contain an object or null")
    return dict(value)


def enrichment_generation_source(
    article: Mapping[str, Any],
    auto_synonyms: Iterable[str],
) -> dict[str, Any]:
    """Return the exact current-state payload bound by a reviewed artifact."""

    return {
        "id": int(article["id"]),
        "article_hash": str(article["article_hash"]),
        "law_document_id": int(article["law_document_id"]),
        "source_order": int(article["source_order"]),
        "code": article.get("code"),
        "catalog_title": article.get("title"),
        "document": article.get("doc_title"),
        "text": article.get("full_text"),
        "current_penalty_summary": article.get("penalty_summary"),
        "current_penalty": normalize_penalty_json(
            article.get("penalty_json")
        ),
        "current_auto_synonyms": sorted(
            str(term) for term in auto_synonyms
        ),
    }


def enrichment_generation_input_hash(
    article: Mapping[str, Any],
    auto_synonyms: Iterable[str],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            enrichment_generation_source(article, auto_synonyms)
        )
    ).hexdigest()


def reviewed_enrichment_artifact_hash(
    item: Mapping[str, Any],
) -> str:
    """Bind replay evidence to the exact validated reviewed artifact."""

    return hashlib.sha256(_canonical_json(dict(item))).hexdigest()
