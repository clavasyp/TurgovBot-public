"""Apply a reviewed full-enrichment artifact through the normal shared path.

Input is a JSON array whose every object contains the generation-time
``id`` and ``article_hash`` plus title/penalty/synonyms.  The complete artifact
is CAS-validated before the first write.  A current DB hash is never substituted
for a missing generation-time hash.  New full-enrichment writes require the
accepted 12/12 physical/effective coverage contract and zero collision
rejections; otherwise the complete JSON transaction is rolled back.  Exact
post-write state evidence remains readable for idempotent replay of historical
commits created under the older partial-acceptance policy.  Explicit corrections
to an already-current article keep the shared additive coverage contract; the
stricter 12/12 candidate floor applies only to initial full enrichment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager
from services.law_backfill_lock import law_backfill_global_lock
from services.law_catalog_service import _write_enrichment_in_transaction
from services.law_document_lock import law_document_server_lock
from services.law_embedding_service import catalog_server_lock
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
    reviewed_enrichment_artifact_hash,
)
from services.law_synonym_service import (
    MAX_TERM_LENGTH,
    MIN_REPLACEMENT_EFFECTIVE,
    MIN_REPLACEMENT_PHYSICAL,
    MIN_TERM_LENGTH,
    SynonymWriteError,
    acquire_synonym_write_lock,
    load_exact_signature_registry,
)

_REQUIRED_REVIEW_FIELDS = {
    "id",
    "article_hash",
    "title",
    "penalty_summary",
    "penalty",
    "synonyms",
    "generation_input_hash",
}
_PENALTY_FIELDS = {"fine", "prison", "arrest", "wanted_level", "other"}
_MAX_TITLE_LENGTH = 300
_MAX_PENALTY_SUMMARY_LENGTH = 1_000
_MAX_PENALTY_VALUE_LENGTH = 500
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FULL_ENRICH_MIN_PHYSICAL = 12
_FULL_ENRICH_MIN_EFFECTIVE = 12


def _exact_reviewed_artifact_hash(item: dict[str, Any]) -> str:
    return reviewed_enrichment_artifact_hash({
        field: item[field]
        for field in _REQUIRED_REVIEW_FIELDS
    })


async def _artifact_commit_outcomes(
    conn,
    server_id: str,
    items: Sequence[dict[str, Any]],
) -> dict[tuple[int, str], set[str]]:
    """Load durable outcomes bound to these exact reviewed artifacts."""

    if not items:
        return {}
    artifact_hashes = [
        _exact_reviewed_artifact_hash(item)
        for item in items
    ]
    rows = await conn.fetch(
        """
        WITH expected AS (
          SELECT *
          FROM unnest(
                 $2::int[], $3::text[], $4::text[],
                 $5::text[], $6::text[]
               )
               AS value(
                 article_id, article_hash, generation_input_hash,
                 artifact_hash, post_commit_generation_hash
               )
        )
        SELECT evidence.article_id, evidence.artifact_hash,
               evidence.post_commit_generation_hash
        FROM law_enrichment_artifact_commits evidence
        JOIN expected
          ON expected.article_id = evidence.article_id
         AND expected.article_hash = evidence.article_hash
         AND expected.generation_input_hash =
               evidence.generation_input_hash
         AND expected.artifact_hash = evidence.artifact_hash
         AND (
               expected.post_commit_generation_hash = ''
               OR expected.post_commit_generation_hash =
                    evidence.post_commit_generation_hash
             )
        WHERE evidence.server_id = $1
        FOR UPDATE OF evidence
        """,
        server_id,
        [int(item["id"]) for item in items],
        [str(item["article_hash"]) for item in items],
        [str(item["generation_input_hash"]) for item in items],
        artifact_hashes,
        [
            str(item.get("_post_commit_generation_hash") or "")
            for item in items
        ],
    )
    outcomes: dict[tuple[int, str], set[str]] = {}
    for row in rows:
        outcomes.setdefault(
            (int(row["article_id"]), str(row["artifact_hash"])),
            set(),
        ).add(str(row["post_commit_generation_hash"]))
    return outcomes


async def _record_artifact_commit_outcomes(
    conn,
    server_id: str,
    items: Sequence[dict[str, Any]],
) -> None:
    """Persist post-write state hashes in the caller's open transaction."""

    if not items:
        return
    artifact_hashes = [
        _exact_reviewed_artifact_hash(item)
        for item in items
    ]
    await conn.execute(
        """
        WITH incoming AS (
          SELECT *
          FROM unnest(
                 $2::int[], $3::text[], $4::text[],
                 $5::text[], $6::text[]
               )
               AS value(
                 article_id, article_hash, generation_input_hash,
                 artifact_hash, post_commit_generation_hash
               )
        )
        INSERT INTO law_enrichment_artifact_commits
            (server_id, article_id, article_hash, generation_input_hash,
             artifact_hash, post_commit_generation_hash)
        SELECT $1, article_id, article_hash, generation_input_hash,
               artifact_hash, post_commit_generation_hash
        FROM incoming
        ON CONFLICT (
          server_id, article_id, article_hash, artifact_hash,
          post_commit_generation_hash
        ) DO NOTHING
        """,
        server_id,
        [int(item["id"]) for item in items],
        [str(item["article_hash"]) for item in items],
        [str(item["generation_input_hash"]) for item in items],
        artifact_hashes,
        [
            str(item["_post_commit_generation_hash"])
            for item in items
        ],
    )
    found = await _artifact_commit_outcomes(conn, server_id, items)
    missing = [
        int(item["id"])
        for item, artifact_hash in zip(items, artifact_hashes)
        if str(item["_post_commit_generation_hash"])
        not in found.get((int(item["id"]), artifact_hash), set())
    ]
    if missing:
        raise SynonymWriteError(
            "durable full-enrichment commit evidence was not recorded for "
            f"article ids: {missing}"
        )


def _validated_optional_text(
    value: Any,
    *,
    field: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SynonymWriteError(f"{field} must be a string or null")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized or len(normalized) > maximum:
        raise SynonymWriteError(
            f"{field} must be non-empty and at most {maximum} characters"
        )
    return normalized


def _validated_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise SynonymWriteError("artifact must be a JSON array")
    if not items:
        raise SynonymWriteError("artifact must contain at least one article")
    result: list[dict[str, Any]] = []
    ids: list[int] = []
    for item in items:
        if not isinstance(item, dict):
            raise SynonymWriteError("every artifact item must be an object")
        unknown = set(item) - _REQUIRED_REVIEW_FIELDS
        missing = _REQUIRED_REVIEW_FIELDS - set(item)
        if unknown or missing:
            raise SynonymWriteError(
                "reviewed item fields do not match the exact schema: "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        article_id = item.get("id")
        article_hash = item.get("article_hash")
        if isinstance(article_id, bool) or not isinstance(article_id, int):
            raise SynonymWriteError("every item must contain an integer id")
        if not isinstance(article_hash, str) or not article_hash:
            raise SynonymWriteError(
                f"article {article_id} has no generation-time article_hash"
            )
        generation_input_hash = item["generation_input_hash"]
        if (
            not isinstance(generation_input_hash, str)
            or _SHA256_RE.fullmatch(generation_input_hash) is None
        ):
            raise SynonymWriteError(
                f"article {article_id} generation_input_hash is malformed"
            )
        title = _validated_optional_text(
            item["title"], field="title", maximum=_MAX_TITLE_LENGTH
        )
        penalty_summary = _validated_optional_text(
            item["penalty_summary"],
            field="penalty_summary",
            maximum=_MAX_PENALTY_SUMMARY_LENGTH,
        )
        penalty = item["penalty"]
        if penalty is not None:
            if not isinstance(penalty, dict):
                raise SynonymWriteError("penalty must be an object or null")
            unknown_penalty = set(penalty) - _PENALTY_FIELDS
            if unknown_penalty:
                raise SynonymWriteError(
                    "penalty contains unknown fields: "
                    f"{sorted(unknown_penalty)}"
                )
            penalty = {
                key: _validated_optional_text(
                    value,
                    field=f"penalty.{key}",
                    maximum=_MAX_PENALTY_VALUE_LENGTH,
                )
                for key, value in penalty.items()
            }
        raw_synonyms = item["synonyms"]
        if not isinstance(raw_synonyms, list):
            raise SynonymWriteError("synonyms must be a JSON array of strings")
        synonyms: list[str] = []
        for value in raw_synonyms:
            if not isinstance(value, str):
                raise SynonymWriteError(
                    "every synonym must be a string"
                )
            normalized = re.sub(r"\s+", " ", value).strip().lower()
            if not (
                MIN_TERM_LENGTH <= len(normalized) <= MAX_TERM_LENGTH
            ):
                raise SynonymWriteError(
                    "every synonym must be non-empty and within shared bounds"
                )
            synonyms.append(normalized)
        ids.append(article_id)
        result.append(
            {
                "id": article_id,
                "article_hash": article_hash,
                "title": title,
                "penalty_summary": penalty_summary,
                "penalty": penalty,
                "synonyms": synonyms,
                "generation_input_hash": generation_input_hash,
            }
        )
    if len(ids) != len(set(ids)):
        raise SynonymWriteError("artifact contains duplicate article ids")
    return result


async def apply_items(server_id: str, items: Sequence[dict[str, Any]]) -> dict:
    """Validate then apply exactly one server while normal writers are locked."""

    validated = _validated_items(list(items))
    ids = [int(item["id"]) for item in validated]
    async with law_backfill_global_lock():
        async with law_document_server_lock(server_id):
            async with catalog_server_lock(server_id):
                async with db_manager.transaction() as conn:
                    await acquire_synonym_write_lock(conn, server_id)
                    has_pending_documents = await conn.fetchval(
                        """
                        SELECT EXISTS (
                          SELECT 1
                          FROM law_documents
                          WHERE server_id = $1
                            AND derived_sync_pending IS TRUE
                        )
                        """,
                        server_id,
                    )
                    if has_pending_documents:
                        raise SynonymWriteError(
                            "server has unfinished derived document sync"
                        )
                    rows = await conn.fetch(
                        """
                        SELECT id, law_document_id, source_order, article_hash,
                               code, title, doc_title, full_text,
                               penalty_summary, penalty_json, enrich_hash
                        FROM law_articles
                        WHERE server_id = $1 AND kind = 'article'
                          AND id = ANY($2::int[])
                        FOR UPDATE
                        """,
                        server_id,
                        ids,
                    )
                    current = {
                        int(row["id"]): dict(row)
                        for row in rows
                    }
                    stale = [
                        int(item["id"])
                        for item in validated
                        if current.get(int(item["id"])) is None
                        or str(
                            current[int(item["id"])]["article_hash"]
                        ) != item["article_hash"]
                    ]
                    if stale:
                        raise SynonymWriteError(
                            "artifact ownership/kind/article_hash preflight failed "
                            f"for article ids: {stale}"
                        )
                    synonym_rows = await conn.fetch(
                        """
                        SELECT article_id, term
                        FROM law_synonyms
                        WHERE server_id = $1
                          AND article_id = ANY($2::int[])
                          AND source = 'auto'
                          AND status = 'approved'
                        ORDER BY article_id, term
                        """,
                        server_id,
                        ids,
                    )
                    auto_synonyms: dict[int, list[str]] = {}
                    for row in synonym_rows:
                        auto_synonyms.setdefault(
                            int(row["article_id"]), []
                        ).append(str(row["term"]))

                    committed_outcomes = await _artifact_commit_outcomes(
                        conn,
                        server_id,
                        validated,
                    )
                    already_applied: set[int] = set()
                    changed_generation: list[int] = []
                    for item in validated:
                        article_id = int(item["id"])
                        article = current[article_id]
                        try:
                            generation_hash = (
                                enrichment_generation_input_hash(
                                    article,
                                    auto_synonyms.get(article_id, ()),
                                )
                            )
                        except ValueError as exc:
                            raise SynonymWriteError(
                                f"article {article_id} has invalid current "
                                "penalty_json"
                            ) from exc
                        if generation_hash == item["generation_input_hash"]:
                            continue
                        artifact_hash = _exact_reviewed_artifact_hash(item)
                        is_idempotent_replay = bool(
                            article.get("enrich_hash")
                            == article.get("article_hash")
                            and generation_hash
                            in committed_outcomes.get(
                                (article_id, artifact_hash),
                                set(),
                            )
                        )
                        if is_idempotent_replay:
                            already_applied.add(article_id)
                        else:
                            changed_generation.append(article_id)
                    if changed_generation:
                        raise SynonymWriteError(
                            "artifact generation input changed for article ids: "
                            f"{changed_generation}"
                        )
                    if len(already_applied) == len(validated):
                        return {
                            "written": 0,
                            "already_applied": len(already_applied),
                            "synonyms": 0,
                            "stale": 0,
                        }

                    signature_registry = await load_exact_signature_registry(
                        conn, server_id
                    )
                    stats = {"articles": 0, "synonyms": 0}
                    written_items: list[dict[str, Any]] = []
                    for item in validated:
                        article_id = int(item["id"])
                        if article_id in already_applied:
                            continue
                        article = current[article_id]
                        prior_enrichment_current = (
                            str(article.get("enrich_hash") or "")
                            == str(article.get("article_hash") or "")
                        )
                        written = await _write_enrichment_in_transaction(
                            conn,
                            server_id,
                            article_id,
                            item["penalty_summary"],
                            item["penalty"],
                            item["synonyms"],
                            stats,
                            expected_article_hash=str(item["article_hash"]),
                            title=item["title"],
                            signature_registry=signature_registry,
                            synonym_lock_already_held=True,
                            minimum_synonym_physical=(
                                MIN_REPLACEMENT_PHYSICAL
                                if prior_enrichment_current
                                else _FULL_ENRICH_MIN_PHYSICAL
                            ),
                            minimum_synonym_effective=(
                                MIN_REPLACEMENT_EFFECTIVE
                                if prior_enrichment_current
                                else _FULL_ENRICH_MIN_EFFECTIVE
                            ),
                        )
                        if not written:
                            raise SynonymWriteError(
                                f"article {article_id} became stale during apply"
                            )
                        stats["articles"] += 1
                        written_items.append(item)
                    if stats.get("synonym_collisions_rejected", 0):
                        raise SynonymWriteError(
                            "full-enrichment artifact rejected one or more "
                            "PostgreSQL exact-leg signature collisions"
                        )

                    written_ids = [
                        int(item["id"])
                        for item in written_items
                    ]
                    written_hashes = {
                        int(item["id"]): str(item["article_hash"])
                        for item in written_items
                    }
                    post_rows = await conn.fetch(
                        """
                        SELECT id, law_document_id, source_order, article_hash,
                               code, title, doc_title, full_text,
                               penalty_summary, penalty_json, enrich_hash
                        FROM law_articles
                        WHERE server_id = $1 AND kind = 'article'
                          AND id = ANY($2::int[])
                        FOR UPDATE
                        """,
                        server_id,
                        written_ids,
                    )
                    post_current = {
                        int(row["id"]): dict(row)
                        for row in post_rows
                        if int(row["id"]) in written_ids
                    }
                    invalid_post = [
                        article_id
                        for article_id in written_ids
                        if article_id not in post_current
                        or str(post_current[article_id]["article_hash"])
                        != written_hashes[article_id]
                        or post_current[article_id].get("enrich_hash")
                        != post_current[article_id].get("article_hash")
                    ]
                    if invalid_post:
                        raise SynonymWriteError(
                            "full-enrichment post-write state is not current "
                            f"for article ids: {invalid_post}"
                        )
                    post_synonym_rows = await conn.fetch(
                        """
                        SELECT article_id, term
                        FROM law_synonyms
                        WHERE server_id = $1
                          AND article_id = ANY($2::int[])
                          AND source = 'auto'
                          AND status = 'approved'
                        ORDER BY article_id, term
                        """,
                        server_id,
                        written_ids,
                    )
                    post_auto_synonyms: dict[int, list[str]] = {}
                    for row in post_synonym_rows:
                        post_auto_synonyms.setdefault(
                            int(row["article_id"]), []
                        ).append(str(row["term"]))

                    evidence_items: list[dict[str, Any]] = []
                    for item in written_items:
                        article_id = int(item["id"])
                        evidence_item = dict(item)
                        try:
                            evidence_item[
                                "_post_commit_generation_hash"
                            ] = enrichment_generation_input_hash(
                                post_current[article_id],
                                post_auto_synonyms.get(article_id, ()),
                            )
                        except ValueError as exc:
                            raise SynonymWriteError(
                                f"article {article_id} has invalid post-write "
                                "penalty_json"
                            ) from exc
                        evidence_items.append(evidence_item)
                    await _record_artifact_commit_outcomes(
                        conn,
                        server_id,
                        evidence_items,
                    )
                    return {
                        "written": stats["articles"],
                        "already_applied": len(already_applied),
                        "synonyms": stats["synonyms"],
                        "stale": 0,
                    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply one reviewed full-enrichment artifact safely"
    )
    parser.add_argument("server_id")
    parser.add_argument("json_file", type=Path)
    args = parser.parse_args()
    with args.json_file.open(encoding="utf-8") as stream:
        items = json.load(stream)

    await db_manager.connect()
    try:
        result = await apply_items(args.server_id, items)
    except SynonymWriteError as exc:
        parser.exit(1, f"REFUSED WITHOUT STALE SUBSTITUTION: {exc}\n")
    finally:
        await db_manager.close()
    print(
        f"WROTE articles={result['written']} synonyms={result['synonyms']} "
        f"stale={result['stale']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
