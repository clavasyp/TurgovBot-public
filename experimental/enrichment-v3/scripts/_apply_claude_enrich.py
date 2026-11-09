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
from copy import deepcopy
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
from services.law_catalog_service import (
    _desired_auto_enrichment_title_state,
    _write_auto_enrichment_server_in_transaction,
    derive_catalog_fields,
    source_article_hash,
    source_catalog_title,
)
from services.law_document_lock import law_document_server_lock
from services.law_embedding_service import catalog_server_lock
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
    reviewed_enrichment_artifact_hash,
    validate_law_enrichment_apply_envelope,
)
from services.law_catalog_truth import (
    LawCatalogTruthError,
    load_verified_law_catalog_truth,
)
# Kept as a public compatibility import for operational/tests callers that
# inspect the exact planner through this legacy script module.
from services.law_enrichment_exact_duplicates import (
    build_exact_duplicate_plan,
)
from services.law_enrichment_review_contract import (
    ReviewSourceContractError,
    build_review_source_projection,
    validate_reviewed_source_projection,
)
from services.law_synonym_service import (
    SynonymWriteError,
    acquire_synonym_write_lock,
    canonical_synonym_term,
    is_explicitly_empty_or_repealed_article,
    load_auto_replacement_generation_state_for_targets_in_transaction,
    normalize_synonym_term,
    validate_runtime_canonical_synonym_claims,
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
_FULL_ENRICH_MAX_PHYSICAL = 15


def _exact_reviewed_artifact_hash(item: dict[str, Any]) -> str:
    return reviewed_enrichment_artifact_hash({
        field: item[field]
        for field in _REQUIRED_REVIEW_FIELDS
    })


def _validate_reviewed_source_contract(
    generation_state: dict[str, Any],
    items: Sequence[dict[str, Any]],
) -> str:
    """Recheck source evidence and complete exact groups at the live writer."""

    catalog_rows = generation_state.get("catalog_rows")
    if not isinstance(catalog_rows, list):
        raise SynonymWriteError(
            "full-enrichment generation state lacks catalog rows"
        )
    try:
        projection = build_review_source_projection(
            catalog_rows,
            [int(item["id"]) for item in items],
            catalog_fields_resolver=derive_catalog_fields,
            article_hash_resolver=source_article_hash,
            is_tombstone=is_explicitly_empty_or_repealed_article,
            canonicalize_scope=canonical_synonym_term,
        )
        validate_reviewed_source_projection(
            projection,
            items,
            catalog_fields_resolver=derive_catalog_fields,
            article_hash_resolver=source_article_hash,
            is_tombstone=is_explicitly_empty_or_repealed_article,
            canonicalize_scope=canonical_synonym_term,
            canonicalize_suffix=canonical_synonym_term,
        )
    except ReviewSourceContractError as exc:
        raise SynonymWriteError(
            f"full-enrichment reviewed-source contract failed: {exc}"
        ) from exc
    return str(projection["projection_hash"])


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
            normalized = normalize_synonym_term(value)
            if normalized is None:
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
    normalized_claims = validate_runtime_canonical_synonym_claims(result)
    for item in result:
        item["synonyms"] = normalized_claims[int(item["id"])]
        if len(item["synonyms"]) > _FULL_ENRICH_MAX_PHYSICAL:
            raise SynonymWriteError(
                f"article {item['id']} has more than "
                f"{_FULL_ENRICH_MAX_PHYSICAL} distinct auto synonyms"
            )
    return result


async def _load_live_source_catalog_truth(conn, server_id: str):
    """Replay the active parser under the full live writer lock order."""

    try:
        return await load_verified_law_catalog_truth(
            conn,
            server_id,
            lock_rows=True,
        )
    except LawCatalogTruthError as exc:
        raise SynonymWriteError(
            f"{server_id}: live source/catalog parser binding is unsafe; run "
            "normal catalog reconcile before apply"
        ) from exc


async def _load_apply_selection_state(
    conn,
    server_id: str,
    envelope: Mapping[str, Any],
) -> tuple[dict[str, Any], Any]:
    """Reprove pending/explicit target closure in the live apply snapshot."""

    article_ids = [int(item["id"]) for item in envelope["articles"]]
    selection_mode = envelope["selection_mode"]
    completion = None
    if selection_mode == "pending_enrichment":
        from services.law_enrichment_completion import (
            evaluate_law_enrichment_completion,
        )

        completion = await evaluate_law_enrichment_completion(
            conn,
            server_id,
        )
        base_target_ids = sorted(
            set(completion.incomplete_article_ids)
        )
        # A fully successful prior apply has no remaining pending target.
        # Capture the reviewed rows for durable no-op verification, but never
        # allow this branch to write them again.
        loader_target_ids = base_target_ids or article_ids
    elif selection_mode == "explicit_articles":
        requested_ids = envelope.get("requested_article_ids")
        if not isinstance(requested_ids, list) or not requested_ids:
            raise SynonymWriteError(
                "explicit apply evidence lacks requested article ids"
            )
        base_target_ids = list(requested_ids)
        loader_target_ids = base_target_ids
    else:  # pragma: no cover - envelope validator owns this invariant
        raise SynonymWriteError("unsupported apply selection mode")

    generation_state = (
        await load_auto_replacement_generation_state_for_targets_in_transaction(
            conn,
            server_id,
            loader_target_ids,
            lock_articles=True,
        )
    )
    live_closure = list(
        generation_state["snapshot"]["required_target_ids"]
    )
    if selection_mode == "pending_enrichment" and not base_target_ids:
        # Empty authoritative selection is a possible idempotent no-op only.
        # Its explicit loader closure is not a new pending-selection proof.
        return generation_state, completion
    if live_closure != sorted(article_ids):
        raise SynonymWriteError(
            "live enrichment selection/exact-group closure differs from "
            f"the reviewed envelope: live={live_closure}, "
            f"reviewed={sorted(article_ids)}"
        )
    return generation_state, completion


async def apply_items(server_id: str, artifact: Any) -> dict:
    """Validate and atomically apply exactly one native-v3 envelope."""

    try:
        envelope = validate_law_enrichment_apply_envelope(
            artifact,
            expected_server_id=server_id,
        )
    except ValueError as exc:
        raise SynonymWriteError(
            f"full-enrichment apply envelope is invalid: {exc}"
        ) from exc
    validated = _validated_items(envelope["articles"])
    ids = [int(item["id"]) for item in validated]
    expected_context_hashes = envelope["generation_context_hashes"]
    expected_source_fingerprint = envelope["source_fingerprint"]
    expected_source_catalog_binding = envelope[
        "source_catalog_binding"
    ]
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
                    live_truth = await _load_live_source_catalog_truth(
                        conn,
                        server_id,
                    )
                    live_source_fingerprint = live_truth.source_fingerprint
                    if live_source_fingerprint != expected_source_fingerprint:
                        raise SynonymWriteError(
                            "complete server source fingerprint changed after "
                            "review; refusing the whole apply"
                        )
                    if (
                        live_truth.binding
                        != expected_source_catalog_binding
                    ):
                        raise SynonymWriteError(
                            "complete source-to-catalog parser binding changed "
                            "after review; refusing the whole apply"
                        )
                    (
                        generation_state,
                        pending_completion,
                    ) = await _load_apply_selection_state(
                        conn,
                        server_id,
                        envelope,
                    )
                    if (
                        generation_state["snapshot"].get(
                            "source_catalog_binding"
                        )
                        != live_truth.binding
                    ):
                        raise SynonymWriteError(
                            "generation selection is detached from the live "
                            "source/catalog parser binding"
                        )
                    changed_context = sorted(
                        article_id
                        for article_id in ids
                        if generation_state["context_hashes"].get(
                            article_id
                        )
                        != expected_context_hashes[article_id]
                    )
                    if changed_context:
                        raise SynonymWriteError(
                            "generation-time enrichment context changed for "
                            f"article ids: {changed_context}"
                        )
                    live_review_source_projection_hash = (
                        _validate_reviewed_source_contract(
                            generation_state,
                            validated,
                        )
                    )
                    expected_review_source_projection_hash = envelope.get(
                        "review_source_projection_hash"
                    )
                    if (
                        expected_review_source_projection_hash is not None
                        and live_review_source_projection_hash
                        != expected_review_source_projection_hash
                    ):
                        raise SynonymWriteError(
                            "live reviewed-source projection differs from "
                            "the offline native-v3 review contract"
                        )
                    rows = await conn.fetch(
                        """
                        SELECT id, law_document_id, source_order, article_hash,
                               article_ref, kind, code, title, doc_title,
                               full_text, penalty_summary, penalty_json,
                               enrich_hash, title_provenance,
                               enrichment_context_hash,
                               enrichment_output_hash
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
                        if envelope["selection_mode"] == "pending_enrichment":
                            if (
                                pending_completion is None
                                or not pending_completion.complete
                            ):
                                raise SynonymWriteError(
                                    "pending artifact durable replay is not "
                                    "current under authoritative completion"
                                )
                        else:
                            from services.law_enrichment_completion import (
                                evaluate_law_enrichment_completion,
                            )

                            scoped_completion = (
                                await evaluate_law_enrichment_completion(
                                    conn,
                                    server_id,
                                    article_ids=ids,
                                )
                            )
                            if not scoped_completion.complete:
                                raise SynonymWriteError(
                                    "explicit artifact durable replay is not "
                                    "current under authoritative completion"
                                )
                        return {
                            "written": 0,
                            "already_applied": len(already_applied),
                            "synonyms": 0,
                            "stale": 0,
                        }
                    if (
                        envelope["selection_mode"] == "pending_enrichment"
                        and pending_completion is not None
                        and pending_completion.complete
                    ):
                        raise SynonymWriteError(
                            "pending envelope has no live incomplete targets "
                            "and is not a fully proven durable no-op"
                        )

                    write_articles = [
                        current[int(item["id"])]
                        for item in validated
                    ]
                    for item in validated:
                        article_id = int(item["id"])
                        article = current[article_id]
                        # Fail before the shared bulk writer if title ownership
                        # is not provable.  The writer repeats this same check
                        # under its metadata CAS.
                        _desired_auto_enrichment_title_state(
                            article,
                            item["title"],
                        )
                    write_stats = (
                        await _write_auto_enrichment_server_in_transaction(
                            conn,
                            server_id,
                            write_articles,
                            validated,
                            {
                                **generation_state,
                                # The equality check above proves these values
                                # are current.  Pass the immutable envelope
                                # values explicitly so the writer persists
                                # exactly what was reviewed, never a silently
                                # substituted digest.
                                "context_hashes": {
                                    **deepcopy(
                                        generation_state[
                                            "context_hashes"
                                        ]
                                    ),
                                    **expected_context_hashes,
                                },
                            },
                            synonym_lock_already_held=True,
                        )
                    )
                    written_items = list(validated)

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
                               article_ref, kind, code, title, doc_title,
                               full_text, penalty_summary, penalty_json,
                               enrich_hash, title_provenance,
                               enrichment_context_hash,
                               enrichment_output_hash
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
                        "written": len(written_items),
                        "already_applied": 0,
                        "synonyms": write_stats["synonyms"],
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
        artifact = json.load(stream)

    await db_manager.connect()
    try:
        result = await apply_items(args.server_id, artifact)
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
