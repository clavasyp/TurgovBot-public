"""Shared synonym normalization, collision and additive-write safeguards."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest

from services import law_synonym_service as synonyms
from services import law_enrichment_completion as completion_service
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    LAW_SOURCE_CATALOG_BINDING_SCHEMA,
    VerifiedLawCatalogTruth,
)


class _FlippingCatalogRow(Mapping):
    """Return one honest materialization, then a forged view."""

    def __init__(self, honest, forged):
        self._honest = honest
        self._forged = forged
        self._honest_reads = 1
        self._active = honest

    def __iter__(self):
        if self._honest_reads:
            self._active = self._honest
            self._honest_reads -= 1
        else:
            self._active = self._forged
        return iter(self._active)

    def __len__(self):
        return len(self._active)

    def __getitem__(self, key):
        return self._active[key]


def test_catalog_binding_hashes_and_returns_the_same_materialized_rows():
    honest = {
        "id": 1,
        "kind": "article",
        "full_text": "honest source",
    }
    forged = {
        **honest,
        "full_text": "forged source",
    }
    snapshot = {
        **synonyms.build_auto_replacement_catalog_binding(
            "memphis",
            [honest],
        ),
        "catalog_rows": 1,
    }

    materialized = synonyms.validate_auto_replacement_catalog_binding(
        "memphis",
        snapshot,
        [_FlippingCatalogRow(honest, forged)],
    )

    assert materialized == [honest]


def test_catalog_binding_rejects_reorder_count_hash_and_nan():
    first = {
        "id": 1,
        "kind": "article",
        "full_text": "first source",
        "penalty_json": None,
    }
    second = {
        "id": 2,
        "kind": "article",
        "full_text": "second source",
        "penalty_json": None,
    }
    rows = [first, second]
    snapshot = {
        **synonyms.build_auto_replacement_catalog_binding(
            "memphis",
            rows,
        ),
        "catalog_rows": 2,
    }
    invalid = (
        (snapshot, list(reversed(rows))),
        ({**snapshot, "catalog_rows": True}, rows),
        ({**snapshot, "catalog_rows": 1}, rows),
        ({**snapshot, "catalog_binding_hash": "0" * 64}, rows),
        ({"catalog_rows": 2}, rows),
    )

    for candidate_snapshot, candidate_rows in invalid:
        with pytest.raises(synonyms.SynonymWriteError):
            synonyms.validate_auto_replacement_catalog_binding(
                "memphis",
                candidate_snapshot,
                candidate_rows,
            )

    nan_rows = deepcopy(rows)
    nan_rows[0]["penalty_json"] = {"fine": float("nan")}
    with pytest.raises(
        synonyms.SynonymWriteError,
        match="deterministically serialized",
    ):
        synonyms.build_auto_replacement_catalog_binding(
            "memphis",
            nan_rows,
        )


def test_runtime_canonical_expansion_keeps_surface_and_live_companion():
    groups = synonyms.expand_synonym_terms(["  МЕНТА   ИЗБИЛИ  ", "что будет за убийство"])

    assert groups["сотрудник телесные повреждения"] == {
        "мента избили",
        "сотрудник телесные повреждения",
    }
    assert groups["убийство"] == {"что будет за убийство", "убийство"}


def test_query_reachable_companion_uses_one_runtime_step_not_fixed_point():
    surface = (
        "отказ оплатить штраф за маску ведет к статье семнадцать семь"
    )
    canonical1 = synonyms.canonical_synonym_term(surface)
    canonical2 = synonyms.canonical_synonym_term(canonical1)

    assert canonical1 == "отказ оплатить штраф маску ведет к статье семь"
    assert canonical2 == "отказ оплатить штраф маску ведет к статье"
    assert canonical1 != canonical2
    assert synonyms._query_reachable_canonical_terms(
        [surface, canonical1]
    ) == {canonical1}
    assert synonyms._query_reachable_canonical_terms([canonical1]) == set()

    fixed_point = "точная правовая формулировка"
    assert synonyms.canonical_synonym_term(fixed_point) == fixed_point
    assert synonyms._query_reachable_canonical_terms(
        [fixed_point]
    ) == {fixed_point}


def test_term_normalization_is_shared_and_bounded():
    assert synonyms.normalize_synonym_term("  ЁЛКА\t  В   ДВОРЕ ") == "елка в дворе"
    assert synonyms.normalize_synonym_term("x") is None
    assert synonyms.normalize_synonym_term("я" * 81) is None


def test_empty_repealed_exception_is_narrow_and_does_not_accept_short_real_norm():
    positives = (
        "Статья 1. Утратила силу.",
        "Статья 2",
        "10.5 Утратила силу",
        "ГЛАВА IV. Утратила силу",
        "Статья 4.8.1. Утратило силу",
        "Раздел III. Утратили силу",
        "Статья 9.6. Упразднена.",
        "Статья 16. Упразднено.",
        'Статья 1.11.\n"Упразднена"',
        "Статья 1.12.\n«Утратила силу»",
        "Статья 1.13.\n“Отменена”",
        "Статья 16.15 от ★ до ★★★★★ "
        "[Федеральная/Региональная] Утратила силу.",
        "11.3* (ФИНАНСОВЫЙ) Утратила силу.",
        "2.9 УТРАТИЛА СИЛУ \u200b",
        (
            "Статья 23. Утратила силу в связи с решением "
            "Конгресса от 01.02.2026 г."
        ),
        (
            "Статья 10. Утратила силу решением "
            "Сената от 13.07.2026."
        ),
        (
            "Статья 11.\n"
            "Утратила силу в связи с решением Верховного суда "
            "по исковому заявлению №2003 от 27.01.2026\n"
            "Вступает в юридическую силу после подписания губернатором "
            "штата San Andreas Morted Monolite.\n"
            "Нормативный правовой акт был подписан губернатором штата "
            "San Andreas Morted Momolite 13.07.2026.\n\u200b"
        ),
    )
    negatives = (
        "Статья 3. Запрещено применять оружие.",
        "10.5 Утратила силу, но применяется к прежним нарушениям.",
        "Глава IV. Порядок, который ранее утратил силу, заменён новым.",
        "Статья 4. Исключено из реестра.",
        "Статья 16. Упразднено частично.",
        'Статья 1.11.\n"Упразднена частично"',
        "Статья 1.12.\n«Запрещено применять оружие»",
        "Утратила силу только часть настоящей статьи.",
        "Статья 23. Утратила силу в связи с решением Конгресса.",
        "Статья 10. Утратила силу решением Сената.",
        (
            "Статья 10. Утратила силу решением Сената "
            "от 13.07.2026, но применяется к прежним нарушениям."
        ),
        (
            "Статья 23. Утратила силу в связи с решением суда "
            "от 01.02.2026 г., но применяется к прежним нарушениям."
        ),
        (
            "Статья 23. Утратила силу в связи с решением Конгресса "
            "за исключением части 2 от 01.02.2026 г."
        ),
        (
            "Статья 23. Утратила силу в связи с решением суда "
            "кроме пункта 3 от 01.02.2026 г."
        ),
        (
            "Статья 23. Утратила силу в связи с решением Конгресса "
            "в части пункта 2 от 01.02.2026 г."
        ),
        (
            "Статья 23. Утратила силу в связи с решением Конгресса "
            "но продолжает действовать от 01.02.2026 г."
        ),
        (
            "Статья 11.\n"
            "Утратила силу в связи с решением Верховного суда "
            "по исковому заявлению №2003.\n"
            "Вступает в юридическую силу после подписания губернатором "
            "штата San Andreas Morted Monolite."
        ),
        (
            "Статья 11.\n"
            "Утратила силу в связи с решением Верховного суда "
            "по исковому заявлению №2003 от 27.01.2026.\n"
            "Часть 2 продолжает действовать.\n"
            "Нормативный правовой акт был подписан губернатором штата "
            "San Andreas Morted Momolite 13.07.2026."
        ),
        (
            "Статья 11. Действующая норма.\n"
            "Вступает в юридическую силу после подписания губернатором "
            "штата San Andreas Morted Monolite.\n"
            "Нормативный правовой акт был подписан губернатором штата "
            "San Andreas Morted Momolite 13.07.2026."
        ),
        (
            "Статья 11.\n"
            "Вступает в юридическую силу после подписания губернатором "
            "штата San Andreas Morted Monolite.\n"
            "Нормативный правовой акт был подписан губернатором штата "
            "San Andreas Morted Momolite 13.07.2026."
        ),
        (
            "Статья 11.\n"
            "Вступает в юридическую силу после подписания губернатором "
            "штата San Andreas Morted Monolite."
        ),
        (
            "Статья 11.\n"
            "Нормативный правовой акт был подписан губернатором штата "
            "San Andreas Morted Momolite 13.07.2026."
        ),
    )
    rejected_positives = [
        value
        for value in positives
        if not synonyms.is_explicitly_empty_or_repealed_article(value)
    ]
    accepted_negatives = [
        value
        for value in negatives
        if synonyms.is_explicitly_empty_or_repealed_article(value)
    ]
    assert rejected_positives == []
    assert accepted_negatives == []


def test_registry_does_not_count_legacy_cross_article_collision_as_effective():
    signature = synonyms.ExactLegSignature(("оруж",), False, False, False, False)
    key = ("exact", signature)
    registry = synonyms.ExactSignatureRegistry()

    registry.add(1, "auto", "оружие", key)
    assert registry.effective_count(1) == 1
    registry.add(2, "auto", "оружие", key)
    assert registry.effective_count(1) == 0
    assert registry.effective_count(2) == 0

    registry.remove(2, "auto", "оружие")
    assert registry.effective_count(1) == 1


def test_registry_rebuilds_non_fixed_companion_when_surface_changes():
    surface = (
        "отказ оплатить штраф за маску ведет к статье семнадцать семь"
    )
    canonical1 = synonyms.canonical_synonym_term(surface)
    assert canonical1 is not None
    surface_key = ("short", surface)
    canonical_key = ("short", canonical1)
    registry = synonyms.ExactSignatureRegistry()

    registry.add(1, "auto", canonical1, canonical_key)
    assert registry.effective_count(1) == 0
    assert registry.active_records == set()

    registry.add(1, "auto", surface, surface_key)
    assert registry.effective_count(1) == 1
    assert registry.active_records == {(1, "auto", canonical1)}

    registry.remove(1, "auto", surface)
    assert registry.effective_count(1) == 0
    assert registry.active_records == set()
    assert registry.records == {(1, "auto", canonical1): canonical_key}


def test_auto_synonym_state_fingerprint_detects_same_term_reinsert():
    row = {
        "id": 701,
        "article_id": 1,
        "term": "одинаковая формулировка",
        "weight": 1.0,
        "server_id": "memphis",
        "source": "auto",
        "status": "approved",
        "suggested_by": None,
    }
    reinserted = dict(row, id=702)

    assert synonyms.auto_synonym_state_fingerprint(
        1,
        [row],
    ) != synonyms.auto_synonym_state_fingerprint(
        1,
        [reinserted],
    )


class _Connection:
    def __init__(self, *, article_rows, existing_synonyms=()):
        self.article_rows = article_rows
        self.existing_synonyms = []
        for offset, source_row in enumerate(existing_synonyms, start=1):
            row = dict(source_row)
            row.setdefault("id", 100 + offset)
            row.setdefault("weight", 1.0)
            row.setdefault("suggested_by", None)
            self.existing_synonyms.append(row)
        self.backfill_runs = {}
        self.calls = []
        self.next_id = 1000
        self._transaction_depth = 0

    def is_in_transaction(self):
        return self._transaction_depth > 0

    @asynccontextmanager
    async def transaction(self):
        before_rows = deepcopy(self.existing_synonyms)
        before_articles = deepcopy(self.article_rows)
        before_next_id = self.next_id
        self._transaction_depth += 1
        try:
            yield self
        except BaseException:
            self.existing_synonyms = before_rows
            self.article_rows = before_articles
            self.next_id = before_next_id
            raise
        finally:
            self._transaction_depth -= 1

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        normalized = " ".join(query.split())
        if "INSERT INTO law_synonym_backfill_runs" in normalized:
            run_id, server_id, plan_hash, manifest_hash = args
            self.backfill_runs.setdefault(
                run_id,
                {
                    "server_id": server_id,
                    "plan_hash": plan_hash,
                    "manifest_hash": manifest_hash,
                },
            )
        return "OK"

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        normalized = " ".join(query.split())
        if "FROM law_synonym_backfill_runs" in normalized:
            return self.backfill_runs.get(args[0])
        return None

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        normalized = " ".join(query.split())
        if (
            normalized.startswith("UPDATE law_articles")
            and "SET enrichment_output_hash = $2" in normalized
        ):
            (
                article_id,
                desired_output_hash,
                server_id,
                article_hash,
                title,
                title_provenance,
                penalty_summary,
                penalty_json,
                enrich_hash,
                enrichment_context_hash,
                old_output_hash,
            ) = args
            article = next(
                (
                    row
                    for row in self.article_rows
                    if int(row["id"]) == int(article_id)
                ),
                None,
            )
            if article is None:
                return None
            expected_penalty = synonyms.normalize_penalty_json(penalty_json)
            actual_penalty = synonyms.normalize_penalty_json(
                article.get("penalty_json")
            )
            if any((
                article.get("server_id") != server_id,
                article.get("kind") != "article",
                article.get("article_hash") != article_hash,
                article.get("title") != title,
                article.get("title_provenance") != title_provenance,
                article.get("penalty_summary") != penalty_summary,
                actual_penalty != expected_penalty,
                article.get("enrich_hash") != enrich_hash,
                article.get("enrichment_context_hash")
                != enrichment_context_hash,
                article.get("enrichment_output_hash") != old_output_hash,
            )):
                return None
            article["enrichment_output_hash"] = desired_output_hash
            return int(article_id)
        raise AssertionError(f"unexpected fetchval: {normalized}")

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        normalized = " ".join(query.split())
        if "COUNT(*)::bigint AS approved_count" in normalized:
            server_id = args[0]
            article_ids = {
                int(row["id"])
                for row in self.article_rows
                if row["server_id"] == server_id
                and row["kind"] == "article"
            }
            grouped = {}
            for row in self.existing_synonyms:
                if (
                    int(row["article_id"]) in article_ids
                    and row.get("status", "approved") == "approved"
                ):
                    key = (int(row["article_id"]), row["server_id"])
                    grouped[key] = grouped.get(key, 0) + 1
            return [
                {
                    "article_id": article_id,
                    "synonym_server_id": synonym_server_id,
                    "approved_count": count,
                }
                for (article_id, synonym_server_id), count in sorted(
                    grouped.items()
                )
            ]
        if (
            "SELECT id, law_document_id, source_order, article_ref, kind, code,"
            in normalized
            and "WHERE server_id = $1" in normalized
            and (
                "ORDER BY id" in normalized
                or "ORDER BY law_document_id, source_order, id" in normalized
            )
        ):
            server_id = args[0]
            return [
                {
                    "id": row["id"],
                    "law_document_id": row.get(
                        "law_document_id",
                        10,
                    ),
                    "source_order": row.get("source_order", row["id"]),
                    "article_ref": row.get("article_ref"),
                    "kind": row["kind"],
                    "code": row.get("code"),
                    "title": row.get("title"),
                    "doc_title": row.get("doc_title", "Документ"),
                    "full_text": row.get(
                        "full_text",
                        "Действующая содержательная норма.",
                    ),
                    "article_hash": row.get(
                        "article_hash",
                        f"hash-{row['id']}",
                    ),
                    "enrich_hash": row.get("enrich_hash"),
                    "penalty_summary": row.get("penalty_summary"),
                    "penalty_json": row.get("penalty_json"),
                    "title_provenance": row.get("title_provenance"),
                    "enrichment_context_hash": row.get(
                        "enrichment_context_hash"
                    ),
                    "enrichment_output_hash": row.get(
                        "enrichment_output_hash"
                    ),
                }
                for row in sorted(
                    self.article_rows,
                    key=lambda value: int(value["id"]),
                )
                if row["server_id"] == server_id
            ]
        if (
            "SELECT id, title, title_provenance, penalty_summary, penalty_json,"
            in normalized
            and "enrichment_output_hash" in normalized
            and "WHERE id = ANY($1::int[])" in normalized
        ):
            requested, server_id = args
            return [
                {
                    "id": row["id"],
                    "title": row.get("title"),
                    "title_provenance": row.get("title_provenance"),
                    "penalty_summary": row.get("penalty_summary"),
                    "penalty_json": row.get("penalty_json"),
                    "enrich_hash": row.get("enrich_hash"),
                    "enrichment_context_hash": row.get(
                        "enrichment_context_hash"
                    ),
                    "enrichment_output_hash": row.get(
                        "enrichment_output_hash"
                    ),
                }
                for row in self.article_rows
                if row["id"] in requested
                and row["server_id"] == server_id
                and row["kind"] == "article"
            ]
        if (
            (
                "SELECT s.id, s.server_id, s.article_id, s.term, s.weight,"
                in normalized
                or "SELECT s.server_id, s.article_id, s.term, s.weight,"
                in normalized
            )
            and "s.source = 'auto'" in normalized
        ):
            server_id = args[0]
            article_ids = {
                int(row["id"])
                for row in self.article_rows
                if row["server_id"] == server_id
                and row["kind"] == "article"
            }
            return [
                dict(row)
                for row in self.existing_synonyms
                if int(row["article_id"]) in article_ids
                and row["source"] == "auto"
            ]
        if "SELECT s.article_id, s.term" in normalized:
            server_id = args[0]
            article_ids = {
                int(row["id"])
                for row in self.article_rows
                if row["server_id"] == server_id
                and row["kind"] == "article"
            }
            return [
                row
                for row in self.existing_synonyms
                if int(row["article_id"]) in article_ids
                and row["server_id"] == server_id
                and row.get("status", "approved") == "approved"
            ]
        if (
            "SELECT id, article_hash FROM law_articles" in normalized
            or "SELECT id, article_hash, enrich_hash FROM law_articles"
            in normalized
            or "SELECT id, article_hash, enrich_hash, full_text "
            "FROM law_articles" in normalized
        ):
            requested, server_id = args
            return [
                {
                    **row,
                    "enrich_hash": row.get("enrich_hash"),
                    "full_text": row.get(
                        "full_text",
                        "Действующая содержательная норма.",
                    ),
                }
                for row in self.article_rows
                if row["id"] in requested
                and row["server_id"] == server_id
                and row["kind"] == "article"
            ]
        if (
            (
                "SELECT article_id, term, source, status FROM law_synonyms"
                in normalized
                or "SELECT id, server_id, article_id, term, weight, source, "
                "status, suggested_by FROM law_synonyms" in normalized
            )
            and "article_id = ANY" in normalized
        ):
            requested = set(args[0])
            return [
                {
                    "id": row["id"],
                    "server_id": row["server_id"],
                    "article_id": row["article_id"],
                    "term": row["term"],
                    "weight": row["weight"],
                    "source": row["source"],
                    "status": row.get("status", "approved"),
                    "suggested_by": row["suggested_by"],
                }
                for row in self.existing_synonyms
                if int(row["article_id"]) in requested
                and (
                    "source = 'auto'" not in normalized
                    or row["source"] == "auto"
                )
            ]
        if "SELECT s.article_id, s.source, s.term" in normalized:
            server_id = args[0]
            return [
                row for row in self.existing_synonyms
                if row["server_id"] == server_id
                and row.get("status", "approved") == "approved"
            ]
        if "FROM unnest($1::text[]) AS candidate(term)" in normalized:
            return [
                {
                    "term": term,
                    "lexemes": sorted(set(term.split())),
                    "has_without": "без" in term.split(),
                    "has_not": "не" in term.split(),
                    "has_without_purpose": "без цели" in term,
                    "has_with_purpose": "с целью" in term,
                }
                for term in args[0]
            ]
        if "WITH candidate_input AS" in normalized:
            server_id, candidate_ids, candidate_terms = args[:3]
            excluded_auto_ids = (
                {int(article_id) for article_id in args[3]}
                if len(args) > 3
                else set()
            )

            def exact_signature(term):
                tokens = term.split()
                if len(term) < 4 or not tokens:
                    return ("short", term)
                return (
                    "exact",
                    tuple(sorted(set(tokens))),
                    "без" in tokens,
                    "не" in tokens,
                    "без цели" in term,
                    "с целью" in term,
                )

            matches = []
            for candidate_id, candidate_term in zip(
                candidate_ids, candidate_terms
            ):
                for row in self.existing_synonyms:
                    if (
                        row["server_id"] == server_id
                        and row.get("status", "approved") == "approved"
                        and int(row["article_id"]) != int(candidate_id)
                        and not (
                            row["source"] == "auto"
                            and int(row["article_id"])
                            in excluded_auto_ids
                        )
                        and exact_signature(row["term"])
                        == exact_signature(candidate_term)
                    ):
                        matches.append({
                            "candidate_article_id": candidate_id,
                            "candidate_term": candidate_term,
                            "owner_article_id": row["article_id"],
                            "owner_term": row["term"],
                        })
            return matches
        if "WITH candidate AS" in normalized:
            server_id, candidate_ids, candidate_terms = args
            matches = []
            for candidate_id, candidate_term in zip(
                candidate_ids, candidate_terms
            ):
                if any(
                    int(row["article_id"]) == int(candidate_id)
                    and row["term"] == candidate_term
                    and (
                        row["server_id"] != server_id
                        or row.get("status", "approved") != "approved"
                    )
                    for row in self.existing_synonyms
                ):
                    matches.append({
                        "candidate_article_id": candidate_id,
                        "candidate_term": candidate_term,
                    })
            return matches
        if (
            normalized.startswith("DELETE FROM law_synonyms")
            and "article_id = ANY" in normalized
        ):
            article_ids, server_id = args
            article_ids = {int(article_id) for article_id in article_ids}
            deleted = [
                row
                for row in self.existing_synonyms
                if (
                    int(row["article_id"]) in article_ids
                    and row["server_id"] == server_id
                    and row["source"] == "auto"
                    and row.get("status", "approved")
                    in {"approved", "stale"}
                )
            ]
            self.existing_synonyms = [
                row
                for row in self.existing_synonyms
                if row not in deleted
            ]
            return [dict(row) for row in deleted]
        if "INSERT INTO law_synonyms" in normalized:
            article_ids, _hashes, terms, server_id = args
            rows = []
            for article_id, term in zip(article_ids, terms):
                if any(
                    int(row["article_id"]) == int(article_id)
                    and row["term"] == term
                    for row in self.existing_synonyms
                ):
                    continue
                self.next_id += 1
                rows.append({
                    "id": self.next_id,
                    "article_id": article_id,
                    "term": term,
                    "source": "auto",
                    "status": "approved",
                    "weight": 1.0,
                    "suggested_by": None,
                })
                self.existing_synonyms.append({
                    "id": self.next_id,
                    "article_id": article_id,
                    "source": "auto",
                    "status": "approved",
                    "term": term,
                    "server_id": server_id,
                    "weight": 1.0,
                    "suggested_by": None,
                })
            return rows
        raise AssertionError(f"unexpected fetch: {normalized}")


class _Db:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def transaction(self):
        async with self.conn.transaction():
            yield self.conn


def _isolated_catalog_truth(conn, server_id: str) -> VerifiedLawCatalogTruth:
    """Adapt synthetic synonym-unit rows to the new verified-loader boundary.

    These tests intentionally use non-parser hashes/bodies to isolate synonym
    collision and atomicity behavior.  Production parser replay is exercised
    separately in ``test_law_catalog_truth.py`` and native-v3 integration
    regressions.
    """

    catalog_rows = []
    for row in sorted(
        conn.article_rows,
        key=lambda value: int(value["id"]),
    ):
        if row["server_id"] != server_id:
            continue
        catalog_rows.append({
            "id": row["id"],
            "server_id": server_id,
            "parent_server_id": server_id,
            "law_document_id": row.get("law_document_id", 10),
            "source_order": row.get("source_order", row["id"]),
            "article_ref": row.get("article_ref"),
            "kind": row["kind"],
            "code": row.get("code"),
            "title": row.get("title"),
            "doc_title": row.get("doc_title", "Документ"),
            "full_text": row.get(
                "full_text",
                "Действующая содержательная норма.",
            ),
            "article_hash": row.get(
                "article_hash",
                f"hash-{row['id']}",
            ),
            "enrich_hash": row.get("enrich_hash"),
            "penalty_summary": row.get("penalty_summary"),
            "penalty_json": row.get("penalty_json"),
            "title_provenance": row.get("title_provenance"),
            "enrichment_context_hash": row.get(
                "enrichment_context_hash"
            ),
            "enrichment_output_hash": row.get(
                "enrichment_output_hash"
            ),
        })
    fingerprint = {
        "algorithm": "sha256",
        "snapshot_version": 3,
        "documents": 1,
        "value": "a" * 64,
    }
    binding = {
        "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
        "parser_contract_version": LAW_CATALOG_PARSER_CONTRACT_VERSION,
        "server_id": server_id,
        "source_fingerprint": fingerprint,
        "documents": 1,
        "catalog_rows": len(catalog_rows),
        "article_rows": sum(
            row["kind"] == "article" for row in catalog_rows
        ),
        "tombstone_rows": 0,
        "source_projection_hash": "b" * 64,
        "catalog_projection_hash": "c" * 64,
        "value": "d" * 64,
    }
    return VerifiedLawCatalogTruth(
        server_id=server_id,
        source_documents=({
            "id": 10,
            "server_id": server_id,
            "source_thread_id": 1,
            "title": "Документ",
            "content": "synthetic unit-test source",
            "derived_sync_pending": False,
        },),
        catalog_rows=tuple(catalog_rows),
        binding=binding,
    )


@pytest.fixture(autouse=True)
def _verified_catalog_boundary_for_synthetic_unit_rows(monkeypatch):
    async def load(conn, server_id, **_kwargs):
        return _isolated_catalog_truth(conn, server_id)

    monkeypatch.setattr(
        synonyms,
        "load_verified_law_catalog_truth",
        load,
    )
    monkeypatch.setattr(
        completion_service,
        "load_verified_law_catalog_truth",
        load,
    )


@pytest.mark.asyncio
async def test_registry_load_counts_query_reachable_non_fixed_companion():
    surface = (
        "отказ оплатить штраф за маску ведет к статье семнадцать семь"
    )
    canonical1 = synonyms.canonical_synonym_term(surface)
    assert canonical1 is not None
    conn = _Connection(
        article_rows=[{
            "id": 1,
            "article_hash": "hash-1",
            "server_id": "memphis",
            "kind": "article",
        }],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": surface,
                "server_id": "memphis",
            },
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": canonical1,
                "server_id": "memphis",
            },
        ],
    )

    registry = await synonyms.load_exact_signature_registry(conn, "memphis")

    assert registry.effective_count(1) == 1
    assert set(registry.records) == {
        (1, "auto", surface),
        (1, "auto", canonical1),
    }
    assert registry.active_records == {(1, "auto", canonical1)}


@pytest.mark.asyncio
async def test_registry_load_requires_same_article_reachability():
    surface = (
        "отказ оплатить штраф за маску ведет к статье семнадцать семь"
    )
    canonical1 = synonyms.canonical_synonym_term(surface)
    assert canonical1 is not None
    conn = _Connection(
        article_rows=[
            {
                "id": 1,
                "article_hash": "hash-1",
                "server_id": "memphis",
                "kind": "article",
            },
            {
                "id": 2,
                "article_hash": "hash-2",
                "server_id": "memphis",
                "kind": "article",
            },
            {
                "id": 3,
                "article_hash": "hash-3",
                "server_id": "memphis",
                "kind": "article",
            },
        ],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": canonical1,
                "server_id": "memphis",
            },
            {
                "article_id": 2,
                "source": "auto",
                "status": "approved",
                "term": surface,
                "server_id": "memphis",
            },
            {
                "article_id": 3,
                "source": "owner",
                "status": "approved",
                "term": "точная правовая формулировка",
                "server_id": "memphis",
            },
        ],
    )

    registry = await synonyms.load_exact_signature_registry(conn, "memphis")

    assert registry.effective_count(1) == 0
    assert registry.effective_count(2) == 0
    assert registry.effective_count(3) == 1
    assert registry.active_records == {
        (3, "owner", "точная правовая формулировка")
    }


@pytest.mark.asyncio
async def test_preview_and_write_activate_existing_non_fixed_companion():
    surface = (
        "отказ оплатить штраф за маску ведет к статье семнадцать семь"
    )
    canonical1 = synonyms.canonical_synonym_term(surface)
    assert canonical1 is not None
    assert synonyms.canonical_synonym_term(canonical1) != canonical1
    conn = _Connection(
        article_rows=[{
            "id": 1,
            "article_hash": "hash-1",
            "server_id": "memphis",
            "kind": "article",
        }],
        existing_synonyms=[{
            "article_id": 1,
            "source": "owner",
            "status": "approved",
            "term": canonical1,
            "server_id": "memphis",
        }],
    )
    registry = await synonyms.load_exact_signature_registry(conn, "memphis")
    item = {
        "id": 1,
        "article_hash": "hash-1",
        "synonyms": [surface],
    }

    preview = await synonyms.preview_auto_synonyms_bulk_in_transaction(
        conn,
        "memphis",
        [item],
        registry=registry,
    )
    assert preview.effective_before == {1: 0}
    assert preview.effective_after == {1: 1}
    assert registry.effective_count(1) == 0

    result = await synonyms.add_auto_synonyms_bulk_in_transaction(
        conn,
        "memphis",
        [item],
        registry=registry,
    )

    assert result.inserted == 1
    assert {row["term"] for row in result.inserted_rows} == {surface}
    assert result.effective_before == {1: 0}
    assert result.effective_after == {1: 1}
    assert registry.effective_count(1) == 1
    assert registry.active_records == {(1, "owner", canonical1)}


class _PostInsertCollisionConnection(_Connection):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.injected_collision = False

    async def fetch(self, query, *args):
        rows = await super().fetch(query, *args)
        if (
            not self.injected_collision
            and "INSERT INTO law_synonyms" in " ".join(query.split())
            and rows
        ):
            self.injected_collision = True
            self.next_id += 1
            self.existing_synonyms.append({
                "id": self.next_id,
                "article_id": 3,
                "source": "owner",
                "status": "approved",
                "term": rows[0]["term"],
                "server_id": "memphis",
                "weight": 1.0,
                "suggested_by": None,
            })
        return rows


@pytest.mark.asyncio
async def test_bulk_add_fails_closed_on_server_kind_or_hash_mismatch(monkeypatch):
    conn = _Connection(article_rows=[
        {"id": 7, "article_hash": "new", "server_id": "other", "kind": "article"},
    ])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    with pytest.raises(synonyms.SynonymWriteError, match="CAS failed"):
        await synonyms.add_auto_synonyms_bulk(
            "memphis",
            [{"id": 7, "article_hash": "old", "synonyms": ["точная фраза"]}],
            run_id="11111111-1111-1111-1111-111111111111",
            plan_hash="a" * 64,
            manifest_hash="b" * 64,
        )

    assert conn.calls[0][0] == "execute"
    assert "pg_advisory_xact_lock" in conn.calls[0][1]
    assert conn.calls[1][0] == "fetch"
    assert "FOR UPDATE" in conn.calls[1][1]
    assert not any(
        kind == "fetch" and "INSERT INTO law_synonyms" in query
        for kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
async def test_bulk_add_expands_canonical_rejects_new_collision_and_logs_exact_rows(monkeypatch):
    conn = _Connection(
        article_rows=[
            {"id": 1, "article_hash": "hash-1", "server_id": "memphis", "kind": "article"},
            {"id": 2, "article_hash": "hash-2", "server_id": "memphis", "kind": "article"},
        ],
        existing_synonyms=[{
            "article_id": 2,
            "source": "auto",
            "term": "сотрудник телесные повреждения",
            "server_id": "memphis",
        }],
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    run_id = "11111111-1111-1111-1111-111111111111"

    result = await synonyms.add_auto_synonyms_bulk(
        "memphis",
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["мента избили", "купил ствол с рук"],
        }],
        # Simulate a long-running backfill snapshot that predates the current
        # row on article 2.  The under-lock candidate query must still reject.
        registry=synonyms.ExactSignatureRegistry(),
        run_id=run_id,
        plan_hash="a" * 64,
        manifest_hash="b" * 64,
    )

    inserted_terms = {row["term"] for row in result.inserted_rows}
    assert inserted_terms == {"купил ствол с рук", "купил оружие с рук"}
    assert set(result.rejected_terms[1]) == {
        "мента избили",
        "сотрудник телесные повреждения",
    }
    assert all(row["run_id"] == run_id for row in result.inserted_rows)
    assert all(row["article_hash"] == "hash-1" for row in result.inserted_rows)
    assert any(
        kind == "execute" and "law_synonym_backfill_inserts" in query
        for kind, query, _args in conn.calls
    )
    assert not any(
        "UPDATE law_articles" in query
        or "law_article_embeddings" in query
        or "law_embedding_jobs" in query
        for _kind, query, _args in conn.calls
    )


def _article_with_trusted_output(
    article_id: int,
    auto_rows,
) -> dict:
    article = {
        "id": article_id,
        "article_hash": f"hash-{article_id}",
        "server_id": "memphis",
        "kind": "article",
        "title": None,
        "title_provenance": None,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": f"hash-{article_id}",
        "enrichment_context_hash": "c" * 64,
    }
    article["enrichment_output_hash"] = (
        synonyms.build_enrichment_output_hash(
            server_id="memphis",
            article_id=article_id,
            title=None,
            title_provenance=None,
            penalty_summary=None,
            penalty_json=None,
            auto_rows=auto_rows,
        )
    )
    return article


def _computed_output_hash(conn, article: Mapping[str, object]) -> str:
    article_id = article["id"]
    return synonyms.build_enrichment_output_hash(
        server_id=str(article["server_id"]),
        article_id=article_id,
        title=article.get("title"),
        title_provenance=article.get("title_provenance"),
        penalty_summary=article.get("penalty_summary"),
        penalty_json=article.get("penalty_json"),
        auto_rows=[
            row
            for row in conn.existing_synonyms
            if row["source"] == "auto"
            and row["article_id"] == article_id
        ],
    )


@pytest.mark.asyncio
async def test_trusted_additive_write_atomically_refreshes_output_fingerprint(
    monkeypatch,
):
    initial_auto = [{
        "id": 101,
        "server_id": "memphis",
        "article_id": 1,
        "term": "исходная точная формулировка",
        "weight": 1.0,
        "source": "auto",
        "status": "approved",
        "suggested_by": None,
    }]
    article = _article_with_trusted_output(1, initial_auto)
    old_output_hash = article["enrichment_output_hash"]
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=initial_auto,
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    result = await synonyms.add_auto_synonyms_bulk(
        "memphis",
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["новая точная формулировка нарушения"],
        }],
    )

    assert result.inserted > 0
    assert article["enrichment_output_hash"] != old_output_hash
    assert article["enrichment_output_hash"] == (
        synonyms.build_enrichment_output_hash(
            server_id="memphis",
            article_id=1,
            title=None,
            title_provenance=None,
            penalty_summary=None,
            penalty_json=None,
            auto_rows=[
                row
                for row in conn.existing_synonyms
                if row["source"] == "auto"
            ],
        )
    )


@pytest.mark.asyncio
async def test_trusted_additive_write_remains_authoritatively_complete(
    monkeypatch,
):
    from services.law_enrichment_completion import (
        evaluate_law_enrichment_completion,
    )

    initial_auto = [
        {
            "id": 101 + index,
            "server_id": "memphis",
            "article_id": 1,
            "term": term,
            "weight": 1.0,
            "source": "auto",
            "status": "approved",
            "suggested_by": None,
        }
        for index, term in enumerate(
            _twelve_terms("trusted completion")[:8]
        )
    ]
    article = _article_with_trusted_output(1, initial_auto)
    article["enrichment_context_hash"] = None
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=initial_auto,
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation = (
        await synonyms.load_auto_replacement_generation_state_in_transaction(
            conn,
            "memphis",
            only_stale=False,
        )
    )
    article["enrichment_context_hash"] = generation["context_hashes"][1]

    result = await synonyms.add_auto_synonyms_bulk(
        "memphis",
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": [
                "trusted additive extra unique legal search phrase"
            ],
        }],
    )
    completion = await evaluate_law_enrichment_completion(
        conn,
        "memphis",
    )

    assert result.inserted > 0
    assert completion.complete is True
    assert completion.incomplete_article_ids == ()
    assert completion.article_states[0].reasons == ()
    assert completion.article_states[0].physical_synonyms >= 8
    assert completion.article_states[0].effective_synonyms >= 8


@pytest.mark.asyncio
async def test_additive_writer_cannot_legalize_foreign_auto_ownership(
    monkeypatch,
):
    auto_rows = [
        {
            "id": 101 + index,
            "server_id": "memphis",
            "article_id": 1,
            "term": term,
            "weight": 1.0,
            "source": "auto",
            "status": "approved",
            "suggested_by": None,
        }
        for index, term in enumerate(
            _twelve_terms("foreign additive guard")[:8]
        )
    ]
    auto_rows.append({
        "id": 999,
        "server_id": "another-server",
        "article_id": 1,
        "term": "foreign auto row with matching persisted hash",
        "weight": 1.0,
        "source": "auto",
        "status": "approved",
        "suggested_by": None,
    })
    article = _article_with_trusted_output(1, auto_rows)
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=auto_rows,
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    with pytest.raises(
        synonyms.SynonymWriteError,
        match=r"foreign-server auto synonym ownership.*\[1\]",
    ):
        await synonyms.add_auto_synonyms_bulk(
            "memphis",
            [{
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": ["new trusted phase b phrase"],
            }],
        )

    assert not any(
        "INSERT INTO law_synonyms" in query
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
async def test_additive_write_does_not_launder_unknown_legacy_output(
    monkeypatch,
):
    article = {
        "id": 1,
        "article_hash": "hash-1",
        "server_id": "memphis",
        "kind": "article",
        "enrichment_output_hash": None,
    }
    conn = _Connection(article_rows=[article])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    result = await synonyms.add_auto_synonyms_bulk(
        "memphis",
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["новая точная формулировка нарушения"],
        }],
    )

    assert result.inserted > 0
    assert article["enrichment_output_hash"] is None


@pytest.mark.asyncio
async def test_additive_write_rejects_a_stale_output_fingerprint_before_insert(
    monkeypatch,
):
    initial_auto = [{
        "id": 101,
        "server_id": "memphis",
        "article_id": 1,
        "term": "исходная точная формулировка",
        "weight": 1.0,
        "source": "auto",
        "status": "approved",
        "suggested_by": None,
    }]
    article = _article_with_trusted_output(1, initial_auto)
    article["enrichment_output_hash"] = "f" * 64
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=initial_auto,
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="fingerprint is stale or invalid",
    ):
        await synonyms.add_auto_synonyms_bulk(
            "memphis",
            [{
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": ["новая точная формулировка нарушения"],
            }],
        )

    assert conn.existing_synonyms == initial_auto
    assert not any(
        kind == "fetch" and "INSERT INTO law_synonyms" in query
        for kind, query, _args in conn.calls
    )


class _OutputCasFailureConnection(_Connection):
    async def fetchval(self, query, *args):
        if "SET enrichment_output_hash = $2" in " ".join(query.split()):
            self.calls.append(("fetchval", query, args))
            return None
        return await super().fetchval(query, *args)


@pytest.mark.asyncio
async def test_additive_output_cas_failure_rolls_back_rows_and_registry(
    monkeypatch,
):
    initial_auto = [{
        "id": 101,
        "server_id": "memphis",
        "article_id": 1,
        "term": "исходная точная формулировка",
        "weight": 1.0,
        "source": "auto",
        "status": "approved",
        "suggested_by": None,
    }]
    article = _article_with_trusted_output(1, initial_auto)
    original_hash = article["enrichment_output_hash"]
    conn = _OutputCasFailureConnection(
        article_rows=[article],
        existing_synonyms=initial_auto,
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    registry = await synonyms.load_exact_signature_registry(conn, "memphis")
    registry_before = registry.clone()

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="metadata CAS failed",
    ):
        await synonyms.add_auto_synonyms_bulk(
            "memphis",
            [{
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": ["новая точная формулировка нарушения"],
            }],
            registry=registry,
        )

    assert conn.existing_synonyms == initial_auto
    assert article["enrichment_output_hash"] == original_hash
    assert registry.records == registry_before.records
    assert registry.active_records == registry_before.active_records


@pytest.mark.asyncio
async def test_backfill_run_id_cannot_be_rebound_to_another_plan():
    conn = _Connection(article_rows=[])
    run_id = "11111111-1111-1111-1111-111111111111"
    await synonyms._bind_backfill_run(
        conn,
        "memphis",
        (run_id, "a" * 64, "b" * 64),
    )

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="already bound",
    ):
        await synonyms._bind_backfill_run(
            conn,
            "memphis",
            (run_id, "c" * 64, "b" * 64),
        )


def test_project_context_is_server_aware_without_server_specific_ranking_rules():
    assert "МВД" in synonyms.project_context_for_server("rmrp")
    assert "LSPD" in synonyms.project_context_for_server("memphis")


@pytest.mark.asyncio
async def test_surface_collision_rejects_its_canonical_companion(monkeypatch):
    conn = _Connection(
        article_rows=[
            {"id": 1, "article_hash": "hash-1", "server_id": "memphis", "kind": "article"},
            {"id": 2, "article_hash": "hash-2", "server_id": "memphis", "kind": "article"},
        ],
        existing_synonyms=[{
            "article_id": 2,
            "source": "auto",
            "term": "купил ствол с рук",
            "server_id": "memphis",
        }],
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    result = await synonyms.add_auto_synonyms_bulk(
        "memphis",
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["купил ствол с рук"],
        }],
    )

    assert result.inserted == 0
    assert set(result.rejected_terms[1]) == {
        "купил ствол с рук",
        "купил оружие с рук",
    }


@pytest.mark.asyncio
async def test_intra_plan_ambiguous_claim_is_rejected_for_every_article(monkeypatch):
    conn = _Connection(article_rows=[
        {"id": 1, "article_hash": "hash-1", "server_id": "memphis", "kind": "article"},
        {"id": 2, "article_hash": "hash-2", "server_id": "memphis", "kind": "article"},
    ])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    result = await synonyms.add_auto_synonyms_bulk(
        "memphis",
        [
            {"id": 1, "article_hash": "hash-1", "synonyms": ["общая точная фраза"]},
            {"id": 2, "article_hash": "hash-2", "synonyms": ["общая точная фраза"]},
        ],
    )

    assert result.inserted == 0
    assert result.rejected_terms == {
        1: ["общая точная фраза"],
        2: ["общая точная фраза"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("pending", "rejected"))
async def test_preview_and_write_do_not_count_same_article_nonapproved_unique_row(
    status,
):
    conn = _Connection(
        article_rows=[{
            "id": 1,
            "article_hash": "hash-1",
            "server_id": "memphis",
            "kind": "article",
        }],
        existing_synonyms=[{
            "article_id": 1,
            "source": "user",
            "status": status,
            "term": "купил ствол с рук",
            "server_id": "memphis",
        }],
    )
    item = {
        "id": 1,
        "article_hash": "hash-1",
        "synonyms": ["купил ствол с рук"],
    }

    preview = await synonyms.preview_auto_synonyms_bulk_in_transaction(
        conn,
        "memphis",
        [item],
    )
    result = await synonyms.add_auto_synonyms_bulk_in_transaction(
        conn,
        "memphis",
        [item],
    )

    expected_rejected = {
        "купил ствол с рук",
        "купил оружие с рук",
    }
    assert set(preview.rejected_terms[1]) == expected_rejected
    assert preview.accepted_terms == {}
    assert preview.effective_after[1] == 0
    assert set(result.rejected_terms[1]) == expected_rejected
    assert result.inserted == 0
    assert result.effective_after[1] == 0


class _ReplaceConnection(_Connection):
    def __init__(self, *, article_state, existing_synonyms):
        super().__init__(
            article_rows=[{
                "id": article_state["id"],
                "article_hash": article_state["article_hash"],
                "server_id": article_state["server_id"],
                "kind": "article",
            }],
            existing_synonyms=existing_synonyms,
        )
        self.article_state = article_state

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "SELECT id, article_hash, enrich_hash, full_text" in query:
            return self.article_state
        raise AssertionError(query)

    async def fetch(self, query, *args):
        normalized = " ".join(query.split())
        if (
            "SELECT id, server_id, article_id, term, weight, source, status,"
            in normalized
            and "WHERE article_id = $1" in normalized
        ):
            self.calls.append(("fetch", query, args))
            return [
                {
                    "id": row["id"],
                    "server_id": row["server_id"],
                    "article_id": row["article_id"],
                    "term": row["term"],
                    "weight": row["weight"],
                    "source": row["source"],
                    "status": row.get("status", "approved"),
                    "suggested_by": row["suggested_by"],
                }
                for row in self.existing_synonyms
                if row["article_id"] == args[0]
            ]
        if normalized.startswith("DELETE FROM law_synonyms"):
            self.calls.append(("fetch", query, args))
            raise AssertionError("same-generation enrichment must be additive")
        return await super().fetch(query, *args)


def _rich_registry(article_id: int, terms: list[str]):
    registry = synonyms.ExactSignatureRegistry()
    for term in terms:
        signature = synonyms.ExactLegSignature(
            tuple(term.split()), False, False, False, False
        )
        registry.add(article_id, "auto", term, ("exact", signature))
    return registry


@pytest.mark.asyncio
async def test_poor_same_generation_replacement_is_rolled_back_without_registry_mutation():
    old_terms = [f"старая точная фраза {index}" for index in range(8)]
    existing = [
        {
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": term,
            "server_id": "memphis",
        }
        for term in old_terms
    ]
    conn = _ReplaceConnection(
        article_state={
            "id": 1,
            "article_hash": "hash-1",
            "enrich_hash": "hash-1",
            "full_text": "Статья 1. Действующая содержательная норма.",
            "server_id": "memphis",
        },
        existing_synonyms=existing,
    )
    registry = _rich_registry(1, old_terms)
    before = registry.clone()

    with pytest.raises(synonyms.SynonymWriteError, match="quality gate"):
        await synonyms.replace_auto_synonyms(
            conn,
            "memphis",
            1,
            "hash-1",
            ["одна бедная фраза"],
            registry=registry,
            prior_enrichment_current=True,
        )

    assert registry.records == before.records
    assert registry.owners == before.owners
    assert registry.effective_counts == before.effective_counts
    assert not any(
        kind == "fetch" and query.lstrip().startswith("DELETE FROM law_synonyms")
        for kind, query, _args in conn.calls
    )


def _preview_article(article_id: int) -> dict:
    return {
        "id": article_id,
        "law_document_id": 10,
        "source_order": article_id,
        "article_ref": None,
        "code": str(article_id),
        "title": f"Статья {article_id}",
        "title_provenance": "generated",
        "enrichment_context_hash": None,
        "enrichment_output_hash": None,
        "doc_title": "Тестовый кодекс",
        "full_text": f"Статья {article_id}. Действующая содержательная норма.",
        "article_hash": f"hash-{article_id}",
        "enrich_hash": None,
        "penalty_summary": None,
        "penalty_json": None,
        "server_id": "memphis",
        "kind": "article",
    }


def _exact_duplicate_catalog(*, tombstone: bool = False) -> list[dict]:
    body = "Утратила силу." if tombstone else (
        "Запрещено единое доказанное деяние с одинаковым составом."
    )
    first = _preview_article(1)
    first.update({
        "source_order": 2,
        "article_ref": "Статья 1",
        "full_text": f"Статья 1\n{body}",
        "title": body.rstrip(".") if not tombstone else None,
        "title_provenance": "source" if not tombstone else None,
    })
    second = _preview_article(2)
    second.update({
        "source_order": 4,
        "article_ref": "Статья 2",
        "full_text": f"Статья 2\n{body}",
        "enrich_hash": "hash-2",
        "title": body.rstrip(".") if not tombstone else None,
        "title_provenance": "source" if not tombstone else None,
    })
    return [
        {
            "id": 101,
            "law_document_id": 10,
            "source_order": 1,
            "article_ref": "Глава I",
            "kind": "chapter",
            "code": "I",
            "title": "Первый источник состава",
            "doc_title": "Тестовый кодекс",
            "full_text": "Глава I\nПервый источник состава.",
            "article_hash": "chapter-hash-1",
            "enrich_hash": None,
            "server_id": "memphis",
        },
        first,
        {
            "id": 102,
            "law_document_id": 10,
            "source_order": 3,
            "article_ref": "Глава II",
            "kind": "chapter",
            "code": "II",
            "title": "Второй источник состава",
            "doc_title": "Тестовый кодекс",
            "full_text": "Глава II\nВторой источник состава.",
            "article_hash": "chapter-hash-2",
            "enrich_hash": None,
            "server_id": "memphis",
        },
        second,
    ]


def _captured_replacement_items(conn, items):
    captured_items = []
    for source_item in items:
        item = dict(source_item)
        article_id = int(item["id"])
        article = next(
            row
            for row in conn.article_rows
            if int(row["id"]) == article_id
        )
        item.setdefault(
            "expected_enrich_hash",
            article.get("enrich_hash"),
        )
        item.setdefault(
            "expected_auto_synonym_fingerprint",
            synonyms.auto_synonym_state_fingerprint(
                article_id,
                [
                    row
                    for row in conn.existing_synonyms
                    if int(row["article_id"]) == article_id
                    and row["source"] == "auto"
                ],
            ),
        )
        captured_items.append(item)
    return captured_items


async def _capture_after_persisting_current_contexts(
    conn,
    *,
    only_stale=True,
):
    """Model the complete state persisted by a validated writer.

    The production loader deliberately treats ``enrich_hash == article_hash``
    plus a NULL context/output fingerprint or sub-minimum searchable coverage
    as incomplete.  Older fixtures used that shape merely as a shorthand for a
    genuinely current row, so they must first receive ordinary reviewed
    synonyms and then explicitly persist both digests before asserting
    freshness.  Tombstones intentionally remain at 0/0.
    """

    for row in conn.article_rows:
        article_id = int(row["id"])
        if (
            row.get("server_id") != "memphis"
            or row.get("kind") != "article"
            or row.get("enrich_hash") != row.get("article_hash")
            or synonyms.is_explicitly_empty_or_repealed_article(
                row.get("full_text")
            )
        ):
            continue
        current_approved = {
            synonyms.normalize_synonym_term(existing.get("term"))
            for existing in conn.existing_synonyms
            if int(existing["article_id"]) == article_id
            and existing.get("source") == "auto"
            and existing.get("status", "approved") == "approved"
        }
        current_approved.discard(None)
        if len(current_approved) >= synonyms.MIN_REPLACEMENT_PHYSICAL:
            continue
        for term in _twelve_terms(f"validated current {article_id}"):
            conn.next_id += 1
            conn.existing_synonyms.append({
                "id": conn.next_id,
                "server_id": "memphis",
                "article_id": article_id,
                "term": term,
                "weight": 1.0,
                "source": "auto",
                "status": "approved",
                "suggested_by": None,
            })

    initial = await synonyms.capture_auto_replacement_generation_state(
        "memphis",
        only_stale=only_stale,
    )
    for row in conn.article_rows:
        article_id = int(row["id"])
        if (
            row.get("server_id") == "memphis"
            and row.get("kind") == "article"
            and row.get("enrich_hash") == row.get("article_hash")
        ):
            if row.get("enrichment_context_hash") is None:
                row["enrichment_context_hash"] = initial["context_hashes"][
                    article_id
                ]
            if row.get("enrichment_output_hash") is None:
                row["enrichment_output_hash"] = _computed_output_hash(
                    conn,
                    row,
                )
    return await synonyms.capture_auto_replacement_generation_state(
        "memphis",
        only_stale=only_stale,
    )


@pytest.mark.asyncio
async def test_generation_loader_binds_exact_project_and_title_policy_inputs(
    monkeypatch,
):
    conn = _Connection(article_rows=[_preview_article(1)])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    original = synonyms.build_enrichment_context_digests
    seen = {}

    def capture_digest_kwargs(rows, **kwargs):
        seen.update(kwargs)
        return original(rows, **kwargs)

    resolver = lambda article_ref, _text: article_ref
    monkeypatch.setattr(
        synonyms,
        "build_enrichment_context_digests",
        capture_digest_kwargs,
    )
    monkeypatch.setattr(
        synonyms,
        "project_context_for_server",
        lambda _server_id: " exact project context ",
    )
    monkeypatch.setattr(
        synonyms,
        "_project_family_for_server",
        lambda _server_id: "majestic_rp",
    )
    monkeypatch.setattr(
        synonyms,
        "_catalog_source_title_resolver",
        lambda: resolver,
    )

    await synonyms.capture_auto_replacement_generation_state("memphis")

    assert seen["project_context"] == " exact project context "
    assert seen["project_family"] == "majestic_rp"
    assert seen["source_title_resolver"] is resolver


async def _preview_replacements(
    monkeypatch,
    conn,
    items,
    *,
    registry=None,
    generation_state=None,
):
    captured_items = _captured_replacement_items(conn, items)
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    if generation_state is None:
        generation_state = (
            await _capture_after_persisting_current_contexts(
                conn,
            )
        )
    if registry is not None:
        async def load_registry(_queryable, _server_id):
            return registry

        monkeypatch.setattr(
            synonyms,
            "load_exact_signature_registry",
            load_registry,
        )
    return await synonyms.preview_auto_synonym_replacements(
        "memphis",
        captured_items,
        generation_snapshot=generation_state["snapshot"],
    )


async def _replacement_plan(monkeypatch, conn, items):
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await _capture_after_persisting_current_contexts(conn)
    )
    articles = {
        int(article["id"]): article
        for article in generation_state["articles"]
    }
    captured_items = []
    for source_item in items:
        item = dict(source_item)
        article_id = int(item["id"])
        item.setdefault(
            "expected_enrich_hash",
            articles[article_id]["enrich_hash"],
        )
        item.setdefault(
            "expected_auto_synonym_fingerprint",
            generation_state["auto_fingerprints"][article_id],
        )
        captured_items.append(item)
    return generation_state, captured_items


def _twelve_terms(prefix):
    return [
        f"{prefix} уникальная правовая формулировка marker{suffix}"
        for suffix in "abcdefghijkl"
    ]


@pytest.mark.asyncio
async def test_replacement_preview_is_order_independent_and_rejects_both_staged_owners(
    monkeypatch,
):
    article_rows = [_preview_article(1), _preview_article(2)]
    forward_items = [
        {
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["общая точная фраза"],
        },
        {
            "id": 2,
            "article_hash": "hash-2",
            "synonyms": ["общая точная фраза"],
        },
    ]
    forward_conn = _Connection(article_rows=article_rows)
    forward = await _preview_replacements(
        monkeypatch,
        forward_conn,
        forward_items,
    )
    reverse_conn = _Connection(article_rows=article_rows)
    reverse = await _preview_replacements(
        monkeypatch,
        reverse_conn,
        list(reversed(forward_items)),
    )

    expected_rejected = {
        1: ["общая точная фраза"],
        2: ["общая точная фраза"],
    }
    assert forward.rejected_terms == expected_rejected
    assert reverse.rejected_terms == expected_rejected
    assert forward.accepted_terms == reverse.accepted_terms == {1: [], 2: []}
    assert forward.physical_after == reverse.physical_after == {1: 0, 2: 0}
    assert forward.effective_after == reverse.effective_after == {1: 0, 2: 0}
    assert forward.conflicts == reverse.conflicts == [
        synonyms.SynonymConflict(
            candidate_article_id=1,
            candidate_term="общая точная фраза",
            reason="staged_candidate_owner",
            owner_article_id=2,
            owner_term="общая точная фраза",
        ),
        synonyms.SynonymConflict(
            candidate_article_id=2,
            candidate_term="общая точная фраза",
            reason="staged_candidate_owner",
            owner_article_id=1,
            owner_term="общая точная фраза",
        ),
    ]
    assert any(
        kind == "execute" and "pg_advisory_xact_lock" in query
        for kind, query, _args in forward_conn.calls
    )
    assert not any(
        keyword in " ".join(query.split()).upper()
        for kind, query, _args in forward_conn.calls
        if kind in {"execute", "fetch", "fetchrow"}
        for keyword in ("INSERT ", "UPDATE ", "DELETE ")
    )


@pytest.mark.asyncio
async def test_replacement_preview_ignores_approved_auto_of_every_scheduled_target(
    monkeypatch,
):
    registry = _rich_registry(1, ["общая точная фраза"])
    before = registry.clone()
    conn = _Connection(
        article_rows=[_preview_article(1), _preview_article(2)],
        existing_synonyms=[{
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": "общая точная фраза",
            "server_id": "memphis",
        }],
    )

    result = await _preview_replacements(
        monkeypatch,
        conn,
        [
            {
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": ["новая отдельная формулировка"],
            },
            {
                "id": 2,
                "article_hash": "hash-2",
                "synonyms": ["общая точная фраза"],
            },
        ],
        registry=registry,
    )

    assert result.rejected_terms == {}
    assert result.conflicts == []
    assert result.accepted_terms == {
        1: ["новая отдельная формулировка"],
        2: ["общая точная фраза"],
    }
    assert result.physical_after == {1: 1, 2: 1}
    assert result.effective_after == {1: 1, 2: 1}
    assert registry.records == before.records
    assert registry.owners == before.owners
    assert registry.effective_counts == before.effective_counts


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ("owner", "user"))
async def test_replacement_preview_keeps_owner_rows_as_current_blockers(
    monkeypatch,
    source,
):
    conn = _Connection(
        article_rows=[_preview_article(1), _preview_article(2)],
        existing_synonyms=[{
            "article_id": 1,
            "source": source,
            "status": "approved",
            "term": "общая точная фраза",
            "server_id": "memphis",
        }],
    )

    result = await _preview_replacements(
        monkeypatch,
        conn,
        [
            {
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": ["новая отдельная формулировка"],
            },
            {
                "id": 2,
                "article_hash": "hash-2",
                "synonyms": ["общая точная фраза"],
            },
        ],
    )

    assert result.rejected_terms == {2: ["общая точная фраза"]}
    assert result.accepted_terms[2] == []
    assert result.physical_after == {1: 2, 2: 0}
    assert result.conflicts == [
        synonyms.SynonymConflict(
            candidate_article_id=2,
            candidate_term="общая точная фраза",
            reason="current_approved_owner",
            owner_article_id=1,
            owner_term="общая точная фраза",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("pending", "rejected"))
async def test_replacement_preview_does_not_ignore_nonreplaceable_auto_row(
    monkeypatch,
    status,
):
    conn = _Connection(
        article_rows=[_preview_article(1)],
        existing_synonyms=[{
            "article_id": 1,
            "source": "auto",
            "status": status,
            "term": "повторяемая точная фраза",
            "server_id": "memphis",
        }],
    )

    result = await _preview_replacements(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["повторяемая точная фраза"],
        }],
    )

    assert result.accepted_terms == {1: []}
    assert result.rejected_terms == {1: ["повторяемая точная фраза"]}
    assert result.physical_after == {1: 0}
    assert result.effective_after == {1: 0}
    assert result.conflicts == [
        synonyms.SynonymConflict(
            candidate_article_id=1,
            candidate_term="повторяемая точная фраза",
            reason="current_nonapproved_exact_row",
            owner_article_id=1,
            owner_term="повторяемая точная фраза",
        ),
    ]


@pytest.mark.asyncio
async def test_replacement_preview_allows_reusing_replaceable_stale_auto_row(
    monkeypatch,
):
    conn = _Connection(
        article_rows=[_preview_article(1)],
        existing_synonyms=[{
            "article_id": 1,
            "source": "auto",
            "status": "stale",
            "term": "повторяемая точная фраза",
            "server_id": "memphis",
        }],
    )

    result = await _preview_replacements(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["повторяемая точная фраза"],
        }],
    )

    assert result.rejected_terms == {}
    assert result.accepted_terms == {1: ["повторяемая точная фраза"]}


@pytest.mark.asyncio
async def test_replacement_preview_exposes_postgres_effective_collapse_below_twelve(
    monkeypatch,
):
    conn = _Connection(article_rows=[_preview_article(1)])
    proposed = [
        "судебный контроль",
        "контроль судебный",
        "проверка лицензии",
        "надзор прокуратуры",
        "порядок обыска",
        "полномочия суда",
        "жалоба гражданина",
        "арест имущества",
        "регистрация оружия",
        "служебное расследование",
        "выдача документа",
        "исполнение решения",
    ]

    result = await _preview_replacements(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": proposed,
        }],
    )

    assert result.rejected_terms == {}
    assert result.physical_after == {1: 12}
    assert result.effective_after == {1: 11}
    assert len(result.accepted_terms[1]) == 12
    assert result.conflicts == []


@pytest.mark.asyncio
async def test_replacement_preview_fails_closed_on_article_hash_cas(monkeypatch):
    conn = _Connection(article_rows=[_preview_article(1)])

    with pytest.raises(synonyms.SynonymWriteError, match="CAS failed"):
        await _preview_replacements(
            monkeypatch,
            conn,
            [{
                "id": 1,
                "article_hash": "stale-hash",
                "synonyms": ["точная формулировка"],
            }],
        )

    assert not any(
        "WITH candidate_input AS" in query
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_part", ("enrich_hash", "stale_auto_row"))
async def test_replacement_preview_rejects_server_snapshot_race(
    monkeypatch,
    changed_part,
):
    article = _preview_article(1)
    conn = _Connection(article_rows=[article])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await synonyms.capture_auto_replacement_generation_state("memphis")
    )
    if changed_part == "enrich_hash":
        article["enrich_hash"] = "hash-1"
    else:
        conn.existing_synonyms.append({
            "id": 701,
            "article_id": 1,
            "source": "auto",
            "status": "stale",
            "term": "чужая новая строка",
            "weight": 1.0,
            "suggested_by": None,
            "server_id": "memphis",
        })

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="server generation snapshot changed",
    ):
        await _preview_replacements(
            monkeypatch,
            conn,
            [{
                "id": 1,
                "article_hash": "hash-1",
                "expected_enrich_hash": None,
                "expected_auto_synonym_fingerprint": (
                    synonyms.auto_synonym_state_fingerprint(1, ())
                ),
                "synonyms": ["точная новая формулировка"],
            }],
            generation_state=generation_state,
        )

    assert not any(
        "WITH candidate_input AS" in query
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
async def test_replacement_preview_rechecks_per_article_auto_fingerprint(
    monkeypatch,
):
    conn = _Connection(
        article_rows=[_preview_article(1)],
        existing_synonyms=[{
            "id": 701,
            "article_id": 1,
            "source": "auto",
            "status": "stale",
            "term": "чужая новая строка",
            "weight": 1.0,
            "suggested_by": None,
            "server_id": "memphis",
        }],
    )

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="generation-state CAS failed",
    ):
        await _preview_replacements(
            monkeypatch,
            conn,
            [{
                "id": 1,
                "article_hash": "hash-1",
                "expected_enrich_hash": None,
                "expected_auto_synonym_fingerprint": (
                    synonyms.auto_synonym_state_fingerprint(1, ())
                ),
                "synonyms": ["точная новая формулировка"],
            }],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "changed_value"),
    (
        ("title", "Новый заголовок соседа"),
        ("code", "99.9"),
        ("doc_title", "Другой документ"),
        ("law_document_id", 777),
        ("source_order", 999),
        ("article_hash", "changed-neighbor-hash"),
        ("full_text", "Изменённый текст соседа без обновления hash"),
    ),
)
async def test_generation_snapshot_catches_neighbor_prompt_context_race(
    monkeypatch,
    field,
    changed_value,
):
    target = _preview_article(1)
    neighbor = _preview_article(2)
    neighbor["enrich_hash"] = neighbor["article_hash"]
    conn = _Connection(article_rows=[target, neighbor])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await _capture_after_persisting_current_contexts(conn)
    )
    items = _captured_replacement_items(
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["точная новая формулировка"],
        }],
    )

    neighbor[field] = changed_value

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="server generation snapshot changed",
    ):
        await synonyms.preview_auto_synonym_replacements(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )


@pytest.mark.asyncio
async def test_generation_capture_returns_exact_prompt_rows_and_auto_only(
    monkeypatch,
):
    target = _preview_article(1)
    other_server = _preview_article(9)
    other_server["server_id"] = "other"
    conn = _Connection(
        article_rows=[target, other_server],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "stale",
                "term": "старая строка цели",
                "server_id": "memphis",
            },
            {
                "article_id": 1,
                "source": "owner",
                "status": "approved",
                "term": "ручная строка",
                "server_id": "memphis",
            },
            {
                "article_id": 9,
                "source": "auto",
                "status": "approved",
                "term": "строка другого сервера",
                "server_id": "other",
            },
        ],
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    state = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )

    assert state["snapshot"]["stale_target_ids"] == [1]
    assert state["snapshot"]["required_target_ids"] == [1]
    assert state["snapshot"]["selection_mode"] == "stale"
    assert state["snapshot"]["catalog_rows"] == 1
    assert state["snapshot"]["article_rows"] == 1
    assert state["snapshot"]["auto_rows"] == 1
    assert state["catalog_rows"][0]["kind"] == "article"
    assert {
        key: state["snapshot"][key]
        for key in (
            "catalog_binding_schema",
            "catalog_binding_hash",
        )
    } == synonyms.build_auto_replacement_catalog_binding(
        "memphis",
        state["catalog_rows"],
    )
    assert state["articles"][0]["full_text"] == target["full_text"]
    assert [row["term"] for row in state["auto_rows"]] == [
        "старая строка цели"
    ]
    assert state["auto_fingerprints"][1] == (
        synonyms.auto_synonym_state_fingerprint(
            1,
            state["auto_rows"],
        )
    )
    assert "output_hashes" not in state
    detached_legacy_snapshot = deepcopy(state["snapshot"])
    detached_legacy_snapshot.pop("catalog_binding_schema")
    detached_legacy_snapshot.pop("catalog_binding_hash")
    with pytest.raises(
        synonyms.SynonymWriteError,
        match="fields differ from the service contract",
    ):
        synonyms._validated_auto_replacement_generation_snapshot(
            "memphis",
            detached_legacy_snapshot,
        )


@pytest.mark.asyncio
async def test_generation_capture_normalizes_and_hashes_penalty_state(
    monkeypatch,
):
    article = _preview_article(1)
    article.update({
        "penalty_summary": "fine $100 and 2 wanted stars",
        "penalty_json": '{"stars": 2, "fine": "$100"}',
    })
    conn = _Connection(article_rows=[article])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    captured = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )

    assert captured["articles"][0]["penalty_summary"] == (
        "fine $100 and 2 wanted stars"
    )
    assert captured["articles"][0]["penalty_json"] == {
        "fine": "$100",
        "stars": 2,
    }

    # jsonb key order is not state.  A different textual representation of
    # the same object must keep the generation snapshot stable.
    article["penalty_json"] = '{"fine":"$100","stars":2}'
    equivalent = (
        await synonyms.capture_auto_replacement_generation_state("memphis")
    )
    assert equivalent["snapshot"] == captured["snapshot"]

    # A real penalty-only correction is generation state and must invalidate
    # every reviewed result that was produced from the earlier snapshot.
    article["penalty_json"] = '{"fine":"$200","stars":2}'
    changed = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )
    assert changed["snapshot"]["value"] != captured["snapshot"]["value"]


@pytest.mark.asyncio
async def test_current_safe_enrichment_without_context_is_stale_until_regenerated(
    monkeypatch,
):
    article = _preview_article(1)
    article["enrich_hash"] = article["article_hash"]
    conn = _Connection(article_rows=[article])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    state = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )

    assert state["snapshot"]["article_hash_stale_ids"] == []
    assert state["snapshot"]["context_unknown_stale_ids"] == [1]
    assert state["snapshot"]["context_stale_ids"] == [1]
    assert state["snapshot"]["output_unknown_stale_ids"] == [1]
    assert state["snapshot"]["output_stale_ids"] == [1]
    assert state["snapshot"]["stale_target_ids"] == [1]
    assert state["snapshot"]["required_target_ids"] == [1]
    assert article["enrichment_context_hash"] is None


@pytest.mark.asyncio
async def test_current_matching_context_with_unknown_output_is_stale(
    monkeypatch,
):
    article = _preview_article(1)
    article["enrich_hash"] = article["article_hash"]
    conn = _Connection(article_rows=[article])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    initial = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )
    article["enrichment_context_hash"] = initial["context_hashes"][1]

    state = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )

    assert state["snapshot"]["article_hash_stale_ids"] == []
    assert state["snapshot"]["context_stale_ids"] == []
    assert state["snapshot"]["output_unknown_stale_ids"] == [1]
    assert state["snapshot"]["output_mismatch_stale_ids"] == []
    assert state["snapshot"]["required_target_ids"] == [1]


@pytest.mark.asyncio
async def test_authoritative_quality_incomplete_state_becomes_generation_target(
    monkeypatch,
):
    from services import law_enrichment_completion as completion

    article = _preview_article(1)
    article["enrich_hash"] = article["article_hash"]
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=[
            {
                "server_id": "memphis",
                "article_id": 1,
                "term": f"quality phrase {index}",
                "source": "auto",
                "status": "approved",
            }
            # На единицу ниже аудита завершённости — ровно то, что
            # делает статью неполной и возвращает её в цели генерации.
            for index in range(synonyms.MIN_REPLACEMENT_PHYSICAL - 1)
        ],
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    class _Registry:
        def effective_count(self, article_id):
            return sum(
                int(row["article_id"]) == int(article_id)
                and row.get("status", "approved") == "approved"
                for row in conn.existing_synonyms
            )

    async def registry(_queryable, _server_id):
        return _Registry()

    monkeypatch.setattr(
        completion,
        "load_exact_signature_registry",
        registry,
    )

    initial = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )
    article["enrichment_context_hash"] = initial["context_hashes"][1]
    article["enrichment_output_hash"] = _computed_output_hash(conn, article)

    quality_stale = (
        await synonyms.capture_auto_replacement_generation_state("memphis")
    )

    assert quality_stale["snapshot"]["selection_mode"] == "explicit"
    assert quality_stale["snapshot"]["article_hash_stale_ids"] == []
    assert quality_stale["snapshot"]["context_stale_ids"] == []
    assert quality_stale["snapshot"]["output_stale_ids"] == []
    assert quality_stale["snapshot"]["required_target_ids"] == [1]

    conn.existing_synonyms.append({
        "id": 999,
        "server_id": "memphis",
        "article_id": 1,
        "term": "quality phrase 8",
        "weight": 1.0,
        "source": "auto",
        "status": "approved",
        "suggested_by": None,
    })
    article["enrichment_output_hash"] = synonyms.build_enrichment_output_hash(
        server_id="memphis",
        article_id=1,
        title=article.get("title"),
        title_provenance=article.get("title_provenance"),
        penalty_summary=article.get("penalty_summary"),
        penalty_json=article.get("penalty_json"),
        auto_rows=conn.existing_synonyms,
    )

    repaired = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )

    assert repaired["snapshot"]["selection_mode"] == "stale"
    assert repaired["snapshot"]["required_target_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("title", "Legacy writer changed title"),
        ("penalty_summary", "Legacy writer changed penalty"),
    ),
)
async def test_legacy_metadata_mutation_without_context_change_is_stale(
    monkeypatch,
    field,
    value,
):
    article = _preview_article(1)
    article["enrich_hash"] = article["article_hash"]
    conn = _Connection(article_rows=[article])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    await _capture_after_persisting_current_contexts(conn)

    article[field] = value
    if field == "title":
        # Model a legacy writer which changes output but does not know either
        # the context or output fingerprint columns.
        article["title_provenance"] = "generated"
    state = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )

    assert state["snapshot"]["context_stale_ids"] == []
    assert state["snapshot"]["output_unknown_stale_ids"] == []
    assert state["snapshot"]["output_mismatch_stale_ids"] == [1]
    assert state["snapshot"]["required_target_ids"] == [1]


@pytest.mark.asyncio
async def test_legacy_auto_synonym_mutation_without_context_change_is_stale(
    monkeypatch,
):
    article = _preview_article(1)
    article["enrich_hash"] = article["article_hash"]
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=[{
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": "original reviewed synonym",
            "server_id": "memphis",
        }],
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    await _capture_after_persisting_current_contexts(conn)

    conn.existing_synonyms[0]["term"] = "legacy-mutated synonym"
    state = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )

    assert state["snapshot"]["context_stale_ids"] == []
    assert state["snapshot"]["output_mismatch_stale_ids"] == [1]
    assert state["snapshot"]["required_target_ids"] == [1]


@pytest.mark.asyncio
async def test_normal_and_attestation_loaders_block_foreign_auto_ownership(
    monkeypatch,
):
    article = _preview_article(1)
    article["enrich_hash"] = article["article_hash"]
    auto_rows = [
        {
            "server_id": "memphis",
            "article_id": 1,
            "term": term,
            "source": "auto",
            "status": "approved",
        }
        for term in _twelve_terms("foreign loader guard")[:8]
    ]
    auto_rows.append({
        "server_id": "another-server",
        "article_id": 1,
        "term": "foreign auto row with recomputed output",
        "source": "auto",
        "status": "approved",
    })
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=auto_rows,
    )
    article["enrichment_output_hash"] = (
        synonyms.build_enrichment_output_hash(
            server_id="memphis",
            article_id=1,
            title=article["title"],
            title_provenance=article["title_provenance"],
            penalty_summary=None,
            penalty_json=None,
            auto_rows=conn.existing_synonyms,
        )
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    with pytest.raises(
        synonyms.SynonymWriteError,
        match=r"foreign-server auto synonym ownership.*\[1\]",
    ):
        await synonyms.capture_auto_replacement_generation_state("memphis")
    with pytest.raises(
        synonyms.SynonymWriteError,
        match=r"foreign-server auto synonym ownership.*\[1\]",
    ):
        await synonyms.load_enrichment_state_for_attestation_in_transaction(
            conn,
            "memphis",
        )


@pytest.mark.asyncio
async def test_unattested_current_enrichment_blocks_normal_capture_but_attester_can_read(
    monkeypatch,
):
    article = _preview_article(1)
    article.update({
        "enrich_hash": article["article_hash"],
        "title_provenance": "legacy_unknown",
        # Unsafe ownership must be rejected even when a malicious/legacy row
        # carries a syntactically valid non-NULL context token.
        "enrichment_context_hash": "a" * 64,
        "enrichment_output_hash": "b" * 64,
    })
    conn = _Connection(article_rows=[article])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    with pytest.raises(
        synonyms.SynonymWriteError,
        match=r"title ownership is not safely attested.*\[1\]",
    ):
        await synonyms.capture_auto_replacement_generation_state("memphis")

    state = (
        await synonyms.load_enrichment_state_for_attestation_in_transaction(
            conn,
            "memphis",
        )
    )
    assert state["snapshot"]["selection_mode"] == "all"
    assert state["snapshot"]["context_unknown_stale_ids"] == []
    assert state["snapshot"]["context_stale_ids"] == [1]
    assert state["snapshot"]["unsafe_provenance_ids"] == [1]
    assert state["snapshot"]["required_target_ids"] == [1]
    assert article["enrichment_context_hash"] == "a" * 64


@pytest.mark.asyncio
async def test_attestation_snapshot_counts_every_approved_synonym_source():
    article = _preview_article(1)
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=[
            {
                "server_id": "memphis",
                "article_id": 1,
                "term": "auto approved",
                "source": "auto",
                "status": "approved",
            },
            {
                "server_id": "memphis",
                "article_id": 1,
                "term": "owner approved",
                "source": "owner",
                "status": "approved",
            },
            {
                "server_id": "memphis",
                "article_id": 1,
                "term": "user approved",
                "source": "user",
                "status": "approved",
            },
            {
                "server_id": "memphis",
                "article_id": 1,
                "term": "owner pending",
                "source": "owner",
                "status": "pending",
            },
        ],
    )

    state = await synonyms.load_enrichment_state_for_attestation_in_transaction(
        conn,
        "memphis",
    )

    assert state["approved_synonym_counts"] == {1: 3}
    count_queries = [
        query
        for kind, query, _args in conn.calls
        if kind == "fetch" and "COUNT(*)::bigint AS approved_count" in query
    ]
    assert len(count_queries) == 1
    assert "s.source =" not in count_queries[0]
    assert "s.status = 'approved'" in count_queries[0]


@pytest.mark.asyncio
async def test_attestation_snapshot_rejects_foreign_approved_synonym_owner():
    article = _preview_article(1)
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=[{
            "server_id": "foreign",
            "article_id": 1,
            "term": "foreign owner synonym",
            "source": "owner",
            "status": "approved",
        }],
    )

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="approved synonym has foreign server ownership",
    ):
        await synonyms.load_enrichment_state_for_attestation_in_transaction(
            conn,
            "memphis",
        )


@pytest.mark.asyncio
async def test_generation_capture_closes_stale_target_over_exact_group(
    monkeypatch,
):
    conn = _Connection(article_rows=_exact_duplicate_catalog())
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    state = await _capture_after_persisting_current_contexts(conn)

    assert state["snapshot"]["selection_mode"] == "stale"
    assert state["snapshot"]["catalog_rows"] == 4
    assert state["snapshot"]["article_rows"] == 2
    assert state["snapshot"]["stale_target_ids"] == [1]
    assert state["snapshot"]["required_target_ids"] == [1, 2]
    assert [row["kind"] for row in state["catalog_rows"]] == [
        "article",
        "article",
        "chapter",
        "chapter",
    ]
    assert [article["id"] for article in state["articles"]] == [1, 2]
    assert set(state["auto_fingerprints"]) == {1, 2}


@pytest.mark.asyncio
async def test_explicit_reviewed_targets_close_exact_group_against_full_snapshot(
    monkeypatch,
):
    conn = _Connection(article_rows=_exact_duplicate_catalog())
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    state = (
        await synonyms.load_auto_replacement_generation_state_for_targets_in_transaction(
            conn,
            "memphis",
            [1],
            lock_articles=True,
        )
    )

    assert state["snapshot"]["selection_mode"] == "explicit"
    assert state["snapshot"]["required_target_ids"] == [1, 2]
    assert state["snapshot"]["catalog_rows"] == 4
    assert set(state["auto_fingerprints"]) == {1, 2}


@pytest.mark.asyncio
async def test_explicit_reviewed_subset_ignores_unrelated_stale_target_but_cas_all_state(
    monkeypatch,
):
    first = _preview_article(1)
    unrelated = _preview_article(2)
    conn = _Connection(article_rows=[first, unrelated])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    state = (
        await synonyms.load_auto_replacement_generation_state_for_targets_in_transaction(
            conn,
            "memphis",
            [1],
        )
    )
    items = _captured_replacement_items(
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": _twelve_terms("явная correction"),
        }],
    )

    assert state["snapshot"]["stale_target_ids"] == [1, 2]
    assert state["snapshot"]["required_target_ids"] == [1]
    result = await synonyms.preview_auto_synonym_replacements(
        "memphis",
        items,
        generation_snapshot=state["snapshot"],
    )
    assert result.physical_after == {1: 12}


@pytest.mark.asyncio
async def test_replacement_preview_allows_current_exact_group_counterpart(
    monkeypatch,
):
    conn = _Connection(article_rows=_exact_duplicate_catalog())
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await _capture_after_persisting_current_contexts(conn)
    )

    result = await _preview_replacements(
        monkeypatch,
        conn,
        [
            {
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": _twelve_terms("первый scope"),
            },
            {
                "id": 2,
                "article_hash": "hash-2",
                "synonyms": _twelve_terms("второй scope"),
            },
        ],
        generation_state=generation_state,
    )

    assert set(result.accepted_terms) == {1, 2}
    assert result.physical_after == {1: 12, 2: 12}
    assert result.effective_after == {1: 12, 2: 12}


@pytest.mark.asyncio
async def test_replacement_preview_rejects_partial_exact_group(
    monkeypatch,
):
    conn = _Connection(article_rows=_exact_duplicate_catalog())
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await _capture_after_persisting_current_contexts(conn)
    )
    partial_items = _captured_replacement_items(
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": _twelve_terms("частичный scope"),
        }],
    )

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="required targets differ from staged article ids",
    ):
        await synonyms.preview_auto_synonym_replacements(
            "memphis",
            partial_items,
            generation_snapshot=generation_state["snapshot"],
        )


@pytest.mark.asyncio
async def test_generation_snapshot_catches_structural_parent_race(
    monkeypatch,
):
    catalog = _exact_duplicate_catalog()
    conn = _Connection(article_rows=catalog)
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await _capture_after_persisting_current_contexts(conn)
    )
    items = _captured_replacement_items(
        conn,
        [
            {
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": _twelve_terms("первый structural"),
            },
            {
                "id": 2,
                "article_hash": "hash-2",
                "synonyms": _twelve_terms("второй structural"),
            },
        ],
    )
    catalog[0]["title"] = "Изменённый первый источник состава"

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="server generation snapshot changed",
    ):
        await synonyms.preview_auto_synonym_replacements(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )


@pytest.mark.asyncio
async def test_existing_structural_drift_triggers_complete_exact_group(
    monkeypatch,
):
    catalog = _exact_duplicate_catalog()
    for row in catalog:
        if row["kind"] == "article":
            row["enrich_hash"] = row["article_hash"]
    approved = [
        {
            "server_id": "memphis",
            "article_id": article_id,
            "term": term,
            "source": "auto",
            "status": "approved",
        }
        for article_id in (1, 2)
        for term in _twelve_terms(f"stable structural {article_id}")
    ]
    conn = _Connection(
        article_rows=catalog,
        existing_synonyms=approved,
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    initial = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )
    assert initial["snapshot"]["context_unknown_stale_ids"] == [1, 2]
    assert initial["snapshot"]["context_stale_ids"] == [1, 2]
    assert initial["snapshot"]["required_target_ids"] == [1, 2]
    for row in catalog:
        if row["kind"] == "article":
                row["enrichment_context_hash"] = initial["context_hashes"][
                    row["id"]
                ]
                row["enrichment_output_hash"] = _computed_output_hash(
                    conn,
                    row,
                )

    stable = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )
    assert stable["snapshot"]["context_stale_ids"] == []
    assert stable["snapshot"]["required_target_ids"] == []

    catalog[0]["title"] = "Переименованный источник состава"
    drifted = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )

    assert drifted["snapshot"]["article_hash_stale_ids"] == []
    assert drifted["snapshot"]["context_stale_ids"] == [1, 2]
    assert drifted["snapshot"]["required_target_ids"] == [1, 2]


@pytest.mark.asyncio
async def test_exact_group_removal_refreshes_surviving_current_member(
    monkeypatch,
):
    catalog = _exact_duplicate_catalog()
    for row in catalog:
        if row["kind"] == "article":
            row["enrich_hash"] = row["article_hash"]
    conn = _Connection(article_rows=catalog)
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    initial = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )
    first = next(row for row in catalog if row["id"] == 1)
    first["enrichment_context_hash"] = initial["context_hashes"][1]

    # The other source norm disappeared through normal catalog reconcile.
    catalog[:] = [row for row in catalog if row["id"] not in {2, 102}]
    refreshed = await synonyms.capture_auto_replacement_generation_state(
        "memphis"
    )

    assert refreshed["snapshot"]["article_hash_stale_ids"] == []
    assert refreshed["snapshot"]["context_stale_ids"] == [1]
    assert refreshed["snapshot"]["required_target_ids"] == [1]


@pytest.mark.asyncio
async def test_generation_all_mode_requires_every_article_and_allows_current(
    monkeypatch,
):
    first = _preview_article(1)
    second = _preview_article(2)
    first["enrich_hash"] = first["article_hash"]
    second["enrich_hash"] = second["article_hash"]
    conn = _Connection(article_rows=[first, second])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = await _capture_after_persisting_current_contexts(
        conn,
        only_stale=False,
    )

    assert generation_state["snapshot"]["selection_mode"] == "all"
    assert generation_state["snapshot"]["stale_target_ids"] == []
    assert generation_state["snapshot"]["required_target_ids"] == [1, 2]
    result = await _preview_replacements(
        monkeypatch,
        conn,
        [
            {
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": _twelve_terms("первый allmode"),
            },
            {
                "id": 2,
                "article_hash": "hash-2",
                "synonyms": _twelve_terms("второй allmode"),
            },
        ],
        generation_state=generation_state,
    )
    assert result.effective_after == {1: 12, 2: 12}


@pytest.mark.asyncio
async def test_tombstone_exact_bodies_do_not_close_stale_targets(
    monkeypatch,
):
    conn = _Connection(
        article_rows=_exact_duplicate_catalog(tombstone=True)
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))

    state = await _capture_after_persisting_current_contexts(conn)

    assert state["snapshot"]["stale_target_ids"] == [1]
    assert state["snapshot"]["required_target_ids"] == [1]


@pytest.mark.asyncio
async def test_generation_snapshot_catches_unrelated_server_article_auto_row(
    monkeypatch,
):
    target = _preview_article(1)
    neighbor = _preview_article(2)
    neighbor["enrich_hash"] = neighbor["article_hash"]
    conn = _Connection(article_rows=[target, neighbor])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await _capture_after_persisting_current_contexts(conn)
    )
    items = _captured_replacement_items(
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["точная новая формулировка"],
        }],
    )
    conn.existing_synonyms.append({
        "id": 801,
        "article_id": 2,
        "source": "auto",
        "status": "approved",
        "term": "новая строка другого материала",
        "server_id": "memphis",
        "weight": 1.0,
        "suggested_by": None,
    })

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="server generation snapshot changed",
    ):
        await synonyms.preview_auto_synonym_replacements(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ("new_stale", "removed_stale"))
async def test_generation_snapshot_catches_stale_target_set_transition(
    monkeypatch,
    transition,
):
    first = _preview_article(1)
    second = _preview_article(2)
    if transition == "new_stale":
        second["enrich_hash"] = second["article_hash"]
        raw_items = [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["первая точная формулировка"],
        }]
    else:
        raw_items = [
            {
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": ["первая точная формулировка"],
            },
            {
                "id": 2,
                "article_hash": "hash-2",
                "synonyms": ["вторая точная формулировка"],
            },
        ]
    conn = _Connection(article_rows=[first, second])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await _capture_after_persisting_current_contexts(conn)
        if transition == "new_stale"
        else await synonyms.capture_auto_replacement_generation_state(
            "memphis"
        )
    )
    items = _captured_replacement_items(conn, raw_items)

    second["enrich_hash"] = (
        None
        if transition == "new_stale"
        else second["article_hash"]
    )

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="server generation snapshot changed",
    ):
        await synonyms.preview_auto_synonym_replacements(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )


@pytest.mark.asyncio
async def test_empty_replacement_still_rechecks_new_stale_target(
    monkeypatch,
):
    article = _preview_article(1)
    article["enrich_hash"] = article["article_hash"]
    conn = _Connection(article_rows=[article])
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await _capture_after_persisting_current_contexts(conn)
    )
    article["enrich_hash"] = None

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="server generation snapshot changed",
    ):
        await synonyms.preview_auto_synonym_replacements(
            "memphis",
            [],
            generation_snapshot=generation_state["snapshot"],
        )


@pytest.mark.asyncio
async def test_replacement_pipeline_rejects_partial_stale_target_set(
    monkeypatch,
):
    conn = _Connection(
        article_rows=[_preview_article(1), _preview_article(2)],
    )
    monkeypatch.setattr(synonyms, "db_manager", _Db(conn))
    generation_state = (
        await synonyms.capture_auto_replacement_generation_state("memphis")
    )
    partial_items = _captured_replacement_items(
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": ["частичная формулировка"],
        }],
    )

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="required targets differ from staged article ids",
    ):
        await synonyms.preview_auto_synonym_replacements(
            "memphis",
            partial_items,
            generation_snapshot=generation_state["snapshot"],
        )


@pytest.mark.asyncio
async def test_replacement_preview_rejects_current_target_outside_required_set(
    monkeypatch,
):
    article = _preview_article(1)
    article["enrich_hash"] = article["article_hash"]
    conn = _Connection(article_rows=[article])

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="required targets differ from staged article ids",
    ):
        await _preview_replacements(
            monkeypatch,
            conn,
            [{
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": ["точная новая формулировка"],
            }],
        )

    assert not any(
        "WITH candidate_input AS" in query
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
async def test_replacement_preview_bounds_large_staged_collision_details(
    monkeypatch,
):
    article_count = 128
    article_rows = [
        _preview_article(article_id)
        for article_id in range(1, article_count + 1)
    ]
    items = [
        {
            "id": article_id,
            "article_hash": f"hash-{article_id}",
            "synonyms": ["общая точная фраза"],
        }
        for article_id in range(1, article_count + 1)
    ]

    result = await _preview_replacements(
        monkeypatch,
        _Connection(article_rows=article_rows),
        items,
    )

    assert len(result.conflicts) == article_count
    assert set(result.rejected_terms) == set(range(1, article_count + 1))
    assert all(
        conflict.reason == "staged_candidate_owner"
        for conflict in result.conflicts
    )


@pytest.mark.asyncio
async def test_bulk_write_includes_current_exact_group_counterpart(
    monkeypatch,
):
    conn = _Connection(article_rows=_exact_duplicate_catalog())
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [
            {
                "id": 1,
                "article_hash": "hash-1",
                "synonyms": _twelve_terms("первый exactwrite"),
            },
            {
                "id": 2,
                "article_hash": "hash-2",
                "synonyms": _twelve_terms("второй exactwrite"),
            },
        ],
    )

    result = await synonyms.replace_auto_synonyms_bulk(
        "memphis",
        items,
        generation_snapshot=generation_state["snapshot"],
    )

    assert generation_state["snapshot"]["stale_target_ids"] == [1]
    assert generation_state["snapshot"]["required_target_ids"] == [1, 2]
    assert result.inserted == 24
    assert result.physical_after == {1: 12, 2: 12}
    assert result.effective_after == {1: 12, 2: 12}


@pytest.mark.asyncio
async def test_bulk_replacement_writes_two_article_cycle_order_independently(
    monkeypatch,
):
    old_a = _twelve_terms("старый альфа")
    old_b = _twelve_terms("старый бета")
    existing = [
        {
            "article_id": article_id,
            "source": "auto",
            "status": "approved",
            "term": term,
            "server_id": "memphis",
        }
        for article_id, terms in ((1, old_a), (2, old_b))
        for term in terms
    ]
    raw_items = [
        {
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": old_b,
        },
        {
            "id": 2,
            "article_hash": "hash-2",
            "synonyms": old_a,
        },
    ]

    outcomes = []
    for staged in (raw_items, list(reversed(raw_items))):
        conn = _Connection(
            article_rows=[_preview_article(1), _preview_article(2)],
            existing_synonyms=existing,
        )
        generation_state, items = await _replacement_plan(
            monkeypatch,
            conn,
            staged,
        )
        caller_registry = synonyms.ExactSignatureRegistry()

        result = await synonyms.replace_auto_synonyms_bulk(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
            registry=caller_registry,
        )

        final_terms = {
            article_id: {
                row["term"]
                for row in conn.existing_synonyms
                if int(row["article_id"]) == article_id
                and row["source"] == "auto"
                and row["status"] == "approved"
            }
            for article_id in (1, 2)
        }
        outcomes.append((
            result.accepted_terms,
            result.physical_after,
            result.effective_after,
            final_terms,
        ))
        assert result.inserted == 24
        assert caller_registry.effective_count(1) >= 12
        assert caller_registry.effective_count(2) >= 12
        assert not any(
            "UPDATE law_articles" in query
            or "law_article_embeddings" in query
            or "law_embedding_jobs" in query
            for _kind, query, _args in conn.calls
        )

    assert outcomes[0] == outcomes[1]


@pytest.mark.asyncio
async def test_bulk_replacement_reuses_stale_but_preserves_pending_auto(
    monkeypatch,
):
    reused = _twelve_terms("сохраненный stale")
    pending_term = "запрещенная ожидающая строка"
    existing = [
        {
            "article_id": 1,
            "source": "auto",
            "status": "stale",
            "term": term,
            "server_id": "memphis",
        }
        for term in reused
    ] + [{
        "article_id": 1,
        "source": "auto",
        "status": "pending",
        "term": pending_term,
        "server_id": "memphis",
    }]
    conn = _Connection(
        article_rows=[_preview_article(1)],
        existing_synonyms=existing,
    )
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": reused,
        }],
    )

    result = await synonyms.replace_auto_synonyms_bulk(
        "memphis",
        items,
        generation_snapshot=generation_state["snapshot"],
    )

    assert result.inserted == 12
    assert any(
        row["term"] == pending_term
        and row["status"] == "pending"
        for row in conn.existing_synonyms
    )
    assert not any(
        row["status"] == "stale"
        for row in conn.existing_synonyms
    )


@pytest.mark.asyncio
async def test_bulk_replacement_rechecks_state_after_generation_before_delete(
    monkeypatch,
):
    old = _twelve_terms("старый concurrent")
    conn = _Connection(
        article_rows=[_preview_article(1)],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": term,
                "server_id": "memphis",
            }
            for term in old
        ],
    )
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": _twelve_terms("новый concurrent"),
        }],
    )
    conn.existing_synonyms[0]["status"] = "stale"
    state_after_concurrent_write = deepcopy(conn.existing_synonyms)

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="server generation snapshot changed",
    ):
        await synonyms.replace_auto_synonyms_bulk(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )

    assert conn.existing_synonyms == state_after_concurrent_write
    assert not any(
        query.lstrip().startswith("DELETE FROM law_synonyms")
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    (
        ("penalty_summary", "newer penalty-only correction"),
        ("penalty_json", '{"fine":"$250"}'),
    ),
)
async def test_bulk_replacement_rejects_penalty_only_generation_race(
    monkeypatch,
    changed_field,
    changed_value,
):
    old = _twelve_terms("old penalty race")
    article = _preview_article(1)
    article.update({
        "penalty_summary": "fine $100",
        "penalty_json": '{"fine":"$100"}',
    })
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": term,
                "server_id": "memphis",
            }
            for term in old
        ],
    )
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": _twelve_terms("new penalty race"),
        }],
    )

    article[changed_field] = changed_value
    state_after_concurrent_write = deepcopy(conn.article_rows)
    synonyms_after_concurrent_write = deepcopy(conn.existing_synonyms)

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="server generation snapshot changed",
    ):
        await synonyms.replace_auto_synonyms_bulk(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )

    assert conn.article_rows == state_after_concurrent_write
    assert conn.existing_synonyms == synonyms_after_concurrent_write
    assert not any(
        query.lstrip().startswith("DELETE FROM law_synonyms")
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "row_server"),
    (
        ("pending", "memphis"),
        ("rejected", "memphis"),
        ("stale", "corrupt-server"),
        ("approved", "corrupt-server"),
    ),
)
async def test_bulk_replacement_keeps_nonreplaceable_auto_blocker(
    monkeypatch,
    status,
    row_server,
):
    blocker = "точная заблокированная формулировка"
    conn = _Connection(
        article_rows=[_preview_article(1)],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": term,
                "server_id": "memphis",
            }
            for term in _twelve_terms("старый blocker")
        ] + [{
            "article_id": 1,
            "source": "auto",
            "status": status,
            "term": blocker,
            "server_id": row_server,
        }],
    )
    before = deepcopy(conn.existing_synonyms)
    if row_server != "memphis":
        with pytest.raises(
            synonyms.SynonymWriteError,
            match=r"foreign-server auto synonym ownership.*\[1\]",
        ):
            await _replacement_plan(
                monkeypatch,
                conn,
                [{
                    "id": 1,
                    "article_hash": "hash-1",
                    "synonyms": [blocker] + _twelve_terms("новый blocker"),
                }],
            )
        assert conn.existing_synonyms == before
        return
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": [blocker] + _twelve_terms("новый blocker"),
        }],
    )

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="rejected exact-leg collisions",
    ):
        await synonyms.replace_auto_synonyms_bulk(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )

    assert conn.existing_synonyms == before
    assert any(
        row["term"] == blocker
        and row["status"] == status
        and row["server_id"] == row_server
        for row in conn.existing_synonyms
    )


@pytest.mark.asyncio
async def test_bulk_replacement_tombstone_accepts_exact_zero_dictionary(
    monkeypatch,
):
    article = _preview_article(1)
    article["full_text"] = "Статья 1. Утратила силу."
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=[{
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": "старая ненужная формулировка",
            "server_id": "memphis",
        }],
    )
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": [],
        }],
    )

    result = await synonyms.replace_auto_synonyms_bulk(
        "memphis",
        items,
        generation_snapshot=generation_state["snapshot"],
    )

    assert result.inserted == 0
    assert result.physical_after == {1: 0}
    assert result.effective_after == {1: 0}
    assert conn.existing_synonyms == []


@pytest.mark.asyncio
async def test_bulk_replacement_tombstone_fails_on_retained_approved_owner(
    monkeypatch,
):
    article = _preview_article(1)
    article["full_text"] = "Статья 1. Утратила силу."
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=[{
            "article_id": 1,
            "source": "owner",
            "status": "approved",
            "term": "immutable owner tombstone term",
            "server_id": "memphis",
        }, {
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": "old replaceable tombstone term",
            "server_id": "memphis",
        }],
    )
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": [],
        }],
    )
    before = deepcopy(conn.existing_synonyms)

    with pytest.raises(
        synonyms.SynonymWriteError,
        match=r"tombstone physical=1!=0",
    ):
        await synonyms.replace_auto_synonyms_bulk(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )

    assert conn.existing_synonyms == before
    assert not any(
        query.lstrip().startswith("DELETE FROM law_synonyms")
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "row_server"),
    (
        ("pending", "memphis"),
        ("rejected", "memphis"),
        ("approved", "corrupt-server"),
        ("stale", "corrupt-server"),
    ),
)
async def test_bulk_replacement_tombstone_fails_on_any_immutable_auto_row(
    monkeypatch,
    status,
    row_server,
):
    article = _preview_article(1)
    article["full_text"] = "Статья 1. Утратила силу."
    conn = _Connection(
        article_rows=[article],
        existing_synonyms=[{
            "article_id": 1,
            "source": "auto",
            "status": status,
            "term": "immutable tombstone row",
            "server_id": row_server,
        }],
    )
    before = deepcopy(conn.existing_synonyms)
    if row_server != "memphis":
        with pytest.raises(
            synonyms.SynonymWriteError,
            match=r"foreign-server auto synonym ownership.*\[1\]",
        ):
            await _replacement_plan(
                monkeypatch,
                conn,
                [{
                    "id": 1,
                    "article_hash": "hash-1",
                    "synonyms": [],
                }],
            )
        assert conn.existing_synonyms == before
        return
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": [],
        }],
    )

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="tombstone retains 1 immutable synonym rows",
    ):
        await synonyms.replace_auto_synonyms_bulk(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )

    assert conn.existing_synonyms == before


@pytest.mark.asyncio
async def test_bulk_replacement_rejects_pg_effective_below_floor_before_delete(
    monkeypatch,
):
    old = _twelve_terms("старый надежный")
    conn = _Connection(
        article_rows=[_preview_article(1)],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": term,
                "server_id": "memphis",
            }
            for term in old
        ],
    )
    floor = synonyms.BULK_REPLACEMENT_EFFECTIVE
    # Первая пара — одна каноническая группа в двух поверхностях: строк
    # на одну больше, ДОСТИЖИМЫХ форм на одну меньше.  Предмет теста —
    # именно это различие, а не конкретное число.
    collapsed = (
        ["судебный контроль", "контроль судебный"]
        + [
            "проверка лицензии",
            "надзор прокуратуры",
            "порядок обыска",
            "полномочия суда",
            "жалоба гражданина",
            "арест имущества",
            "регистрация оружия",
            "служебное расследование",
            "выдача документа",
            "исполнение решения",
        ][: max(0, floor - 2)]
    )
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": collapsed,
        }],
    )
    before = deepcopy(conn.existing_synonyms)

    with pytest.raises(
        synonyms.SynonymWriteError,
        match=rf"effective={floor - 1}<{floor}",
    ):
        await synonyms.replace_auto_synonyms_bulk(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )

    assert conn.existing_synonyms == before
    assert not any(
        query.lstrip().startswith("DELETE FROM law_synonyms")
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
async def test_bulk_replacement_floor_cannot_be_lowered_by_the_caller(
    monkeypatch,
):
    old = _twelve_terms("old autonomous floor")
    conn = _Connection(
        article_rows=[_preview_article(1)],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": term,
                "server_id": "memphis",
            }
            for term in old
        ],
    )
    floor = synonyms.BULK_REPLACEMENT_PHYSICAL
    starved = floor - 1
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": _twelve_terms("new autonomous floor")[:starved],
        }],
    )
    before = deepcopy(conn.existing_synonyms)

    with pytest.raises(
        synonyms.SynonymWriteError,
        match=rf"physical={starved}<{floor}",
    ):
        await synonyms.replace_auto_synonyms_bulk(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
            # Пол просят НИЖЕ настроенного — и поблажки не получают.
            minimum_physical=2,
            minimum_effective=2,
        )

    assert conn.existing_synonyms == before
    assert not any(
        query.lstrip().startswith("DELETE FROM law_synonyms")
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
async def test_bulk_replacement_post_insert_collision_rolls_back_everything(
    monkeypatch,
):
    old = _twelve_terms("старый rollback")
    target = _preview_article(1)
    unrelated = _preview_article(3)
    unrelated["enrich_hash"] = unrelated["article_hash"]
    conn = _PostInsertCollisionConnection(
        article_rows=[target, unrelated],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": term,
                "server_id": "memphis",
            }
            for term in old
        ],
    )
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": _twelve_terms("новый collision"),
        }],
    )
    before_rows = deepcopy(conn.existing_synonyms)
    caller_registry = _rich_registry(99, ["контрольный маркер"])
    registry_before = caller_registry.clone()

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="post-insert exact collision",
    ):
        await synonyms.replace_auto_synonyms_bulk(
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
            registry=caller_registry,
        )

    assert conn.existing_synonyms == before_rows
    assert caller_registry.records == registry_before.records
    assert caller_registry.owners == registry_before.owners
    assert caller_registry.effective_counts == registry_before.effective_counts


@pytest.mark.asyncio
async def test_bulk_replacement_core_requires_an_outer_transaction(
    monkeypatch,
):
    conn = _Connection(article_rows=[_preview_article(1)])
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": _twelve_terms("requires outer transaction"),
        }],
    )
    conn.calls.clear()

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="requires an active caller-owned transaction",
    ):
        await synonyms.replace_auto_synonyms_bulk_in_transaction(
            conn,
            "memphis",
            items,
            generation_snapshot=generation_state["snapshot"],
        )

    assert conn.calls == []
    assert conn.existing_synonyms == []


@pytest.mark.asyncio
async def test_bulk_replacement_core_savepoint_survives_caught_write_error(
    monkeypatch,
):
    old = _twelve_terms("old savepoint")
    conn = _PostInsertCollisionConnection(
        article_rows=[_preview_article(1), {
            **_preview_article(3),
            "enrich_hash": "hash-3",
        }],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": term,
                "server_id": "memphis",
            }
            for term in old
        ],
    )
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": _twelve_terms("new savepoint"),
        }],
    )
    before = deepcopy(conn.existing_synonyms)

    async with synonyms.db_manager.transaction() as transaction_conn:
        await synonyms.acquire_synonym_write_lock(
            transaction_conn,
            "memphis",
        )
        with pytest.raises(
            synonyms.SynonymWriteError,
            match="post-insert exact collision",
        ):
            await synonyms.replace_auto_synonyms_bulk_in_transaction(
                transaction_conn,
                "memphis",
                items,
                generation_snapshot=generation_state["snapshot"],
                lock_already_held=True,
            )
        # The caller deliberately catches the service error.  Its outer
        # transaction can still commit without preserving partial writes.
        assert conn.existing_synonyms == before

    assert conn.existing_synonyms == before


@pytest.mark.asyncio
async def test_bulk_replacement_core_outer_rollback_and_lock_reuse(
    monkeypatch,
):
    old = _twelve_terms("старый outer")
    conn = _Connection(
        article_rows=[_preview_article(1)],
        existing_synonyms=[
            {
                "article_id": 1,
                "source": "auto",
                "status": "approved",
                "term": term,
                "server_id": "memphis",
            }
            for term in old
        ],
    )
    generation_state, items = await _replacement_plan(
        monkeypatch,
        conn,
        [{
            "id": 1,
            "article_hash": "hash-1",
            "synonyms": _twelve_terms("новый outer"),
        }],
    )
    before_rows = deepcopy(conn.existing_synonyms)
    external_registry = _rich_registry(99, ["внешний маркер"])
    registry_before = external_registry.clone()
    conn.calls.clear()

    with pytest.raises(RuntimeError, match="outer metadata failure"):
        async with synonyms.db_manager.transaction() as transaction_conn:
            await synonyms.acquire_synonym_write_lock(
                transaction_conn,
                "memphis",
            )
            outcome = (
                await synonyms.replace_auto_synonyms_bulk_in_transaction(
                    transaction_conn,
                    "memphis",
                    items,
                    generation_snapshot=generation_state["snapshot"],
                    lock_already_held=True,
                )
            )
            assert outcome.result.inserted == 12
            assert outcome.final_registry.effective_count(1) >= 12
            assert external_registry.records == registry_before.records
            raise RuntimeError("outer metadata failure")

    lock_calls = [
        query
        for kind, query, _args in conn.calls
        if kind == "execute"
        and "pg_advisory_xact_lock" in query
    ]
    assert len(lock_calls) == 1
    assert conn.existing_synonyms == before_rows
    assert external_registry.records == registry_before.records
    assert external_registry.owners == registry_before.owners


@pytest.mark.asyncio
async def test_good_same_generation_enrichment_merges_additively_without_delete():
    old_terms = [f"старая точная фраза {index}" for index in range(8)]
    existing = [
        {
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": term,
            "server_id": "memphis",
        }
        for term in old_terms
    ]
    conn = _ReplaceConnection(
        article_state={
            "id": 1,
            "article_hash": "hash-1",
            "enrich_hash": "hash-1",
            "full_text": "Статья 1. Действующая содержательная норма.",
            "server_id": "memphis",
        },
        existing_synonyms=existing,
    )
    registry = _rich_registry(1, old_terms)
    generated = [
        "тайно забрал чужое имущество",
        "похитил принадлежащую другому вещь",
        "незаконно присвоил чужую собственность",
        "совершил хищение личного имущества",
        "украл имущество у владельца",
        "завладел чужой вещью тайно",
        "обратил чужую собственность в свою пользу",
        "лишил владельца принадлежащей ему вещи",
    ]

    result = await synonyms.replace_auto_synonyms(
        conn,
        "memphis",
        1,
        "hash-1",
        generated,
        registry=registry,
        prior_enrichment_current=True,
    )

    assert result.inserted >= 8
    assert result.effective_after[1] >= result.effective_before[1]
    assert not any(
        kind == "fetch" and query.lstrip().startswith("DELETE FROM law_synonyms")
        for kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
async def test_replacement_writer_counts_non_fixed_canonical_companion():
    old_terms = [
        f"старая точная фраза {index}"
        for index in range(8)
    ]
    existing = [
        {
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": term,
            "server_id": "memphis",
        }
        for term in old_terms
    ]
    conn = _ReplaceConnection(
        article_state={
            "id": 1,
            "article_hash": "hash-1",
            "enrich_hash": "hash-1",
            "full_text": "Статья 1. Действующая содержательная норма.",
            "server_id": "memphis",
        },
        existing_synonyms=existing,
    )
    registry = _rich_registry(1, old_terms)
    surface = (
        "отказ оплатить штраф за маску ведет к статье семнадцать семь"
    )
    canonical1 = synonyms.canonical_synonym_term(surface)
    assert canonical1 is not None
    assert synonyms.canonical_synonym_term(canonical1) != canonical1
    generated = [surface] + [
        f"новая уникальная правовая формулировка marker{suffix}"
        for suffix in "abcdefg"
    ]

    result = await synonyms.replace_auto_synonyms(
        conn,
        "memphis",
        1,
        "hash-1",
        generated,
        registry=registry,
        prior_enrichment_current=True,
    )

    assert result.inserted == 9
    assert result.effective_after[1] == result.effective_before[1] + 8
    assert registry.active_records.issuperset({
        (1, "auto", canonical1),
    })
    assert (1, "auto", surface) in registry.records
    assert (1, "auto", surface) not in registry.active_records


@pytest.mark.asyncio
async def test_caller_can_raise_replacement_physical_floor_without_lowering_shared_floor():
    old_terms = [f"старая точная фраза {index}" for index in range(8)]
    existing = [
        {
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": term,
            "server_id": "memphis",
        }
        for term in old_terms
    ]
    conn = _ReplaceConnection(
        article_state={
            "id": 1,
            "article_hash": "hash-1",
            "enrich_hash": "hash-1",
            "full_text": "Статья 1. Действующая содержательная норма.",
            "server_id": "memphis",
        },
        existing_synonyms=existing,
    )
    registry = _rich_registry(1, old_terms)
    generated = [
        "тайно забрал чужое имущество",
        "похитил принадлежащую другому вещь",
        "незаконно присвоил чужую собственность",
        "совершил хищение личного имущества",
        "украл имущество у владельца",
        "завладел чужой вещью тайно",
        "обратил чужую собственность в свою пользу",
        "лишил владельца принадлежащей ему вещи",
    ]

    with pytest.raises(
        synonyms.SynonymWriteError,
        match=r"candidate physical=.*<12",
    ):
        await synonyms.replace_auto_synonyms(
            conn,
            "memphis",
            1,
            "hash-1",
            generated,
            registry=registry,
            prior_enrichment_current=True,
            minimum_physical=12,
            minimum_effective=8,
        )


class _UnexpectedInsertConflictConnection(_ReplaceConnection):
    async def fetch(self, query, *args):
        rows = await super().fetch(query, *args)
        if "INSERT INTO law_synonyms" not in " ".join(query.split()) or not rows:
            return rows
        skipped = rows.pop()
        self.existing_synonyms = [
            row
            for row in self.existing_synonyms
            if not (
                int(row["article_id"]) == int(skipped["article_id"])
                and row["term"] == skipped["term"]
            )
        ]
        return rows


class _ChangedGenerationConnection(_ReplaceConnection):
    async def fetch(self, query, *args):
        if "DELETE FROM law_synonyms" in " ".join(query.split()):
            self.calls.append(("fetch", query, args))
            article_id = int(args[0])
            deleted = [
                row
                for row in self.existing_synonyms
                if int(row["article_id"]) == article_id
                and row["source"] == "auto"
            ]
            self.existing_synonyms = [
                row
                for row in self.existing_synonyms
                if row not in deleted
            ]
            return deleted
        return await super().fetch(query, *args)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_generation", (True, False))
async def test_single_replacement_hard_blocks_foreign_auto_before_mutation(
    same_generation,
):
    foreign_term = "foreign auto ownership row"
    conn = _ChangedGenerationConnection(
        article_state={
            "id": 1,
            "article_hash": "hash-1",
            "enrich_hash": "hash-1" if same_generation else "old-hash",
            "full_text": "Статья 1. Действующая содержательная норма.",
            "server_id": "memphis",
        },
        existing_synonyms=[{
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": foreign_term,
            "server_id": "another-server",
        }],
    )
    registry = _rich_registry(1, [foreign_term])
    rows_before = deepcopy(conn.existing_synonyms)
    registry_before = registry.clone()

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="foreign-server auto synonym ownership",
    ):
        await synonyms.replace_auto_synonyms(
            conn,
            "memphis",
            1,
            "hash-1",
            _twelve_terms("replacement"),
            registry=registry,
            prior_enrichment_current=same_generation,
        )

    assert conn.existing_synonyms == rows_before
    assert registry.records == registry_before.records
    assert registry.owners == registry_before.owners
    assert not any(
        (
            kind == "fetch"
            and (
                query.lstrip().startswith("DELETE FROM law_synonyms")
                or "INSERT INTO law_synonyms" in query
            )
        )
        for kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("old_status", ("rejected", "stale"))
async def test_changed_generation_can_replace_its_own_nonapproved_auto_row(
    old_status,
):
    generated = [
        "тайно забрал чужое имущество",
        "похитил принадлежащую другому вещь",
        "незаконно присвоил чужую собственность",
        "совершил хищение личного имущества",
        "украл имущество у владельца",
        "завладел чужой вещью тайно",
        "обратил чужую собственность в свою пользу",
        "лишил владельца принадлежащей ему вещи",
    ]
    conn = _ChangedGenerationConnection(
        article_state={
            "id": 1,
            "article_hash": "hash-1",
            "enrich_hash": "old-hash",
            "full_text": "Статья 1. Действующая содержательная норма.",
            "server_id": "memphis",
        },
        existing_synonyms=[{
            "article_id": 1,
            "source": "auto",
            "status": old_status,
            "term": generated[0],
            "server_id": "memphis",
        }],
    )
    registry = synonyms.ExactSignatureRegistry()

    result = await synonyms.replace_auto_synonyms(
        conn,
        "memphis",
        1,
        "hash-1",
        generated,
        registry=registry,
        prior_enrichment_current=False,
    )

    assert result.rejected_collisions == 0
    assert result.inserted >= 8
    assert result.effective_after[1] >= 8


@pytest.mark.asyncio
async def test_post_write_floor_rejects_unexpected_on_conflict_skip():
    old_terms = [
        f"старая точная фраза {index}"
        for index in range(synonyms.MIN_REPLACEMENT_PHYSICAL)
    ]
    existing = [
        {
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": term,
            "server_id": "memphis",
        }
        for term in old_terms
    ]
    generated = [
        "тайно забрал чужое имущество",
        "похитил принадлежащую другому вещь",
        "незаконно присвоил чужую собственность",
        "совершил хищение личного имущества",
        "украл имущество у владельца",
        "завладел чужой вещью тайно",
        "обратил чужую собственность в свою пользу",
        "лишил владельца принадлежащей ему вещи",
    ][: synonyms.MIN_REPLACEMENT_PHYSICAL]
    conn = _UnexpectedInsertConflictConnection(
        article_state={
            "id": 1,
            "article_hash": "hash-1",
            "enrich_hash": "hash-1",
            "full_text": "Статья 1. Действующая содержательная норма.",
            "server_id": "memphis",
        },
        existing_synonyms=existing,
    )
    registry = _rich_registry(1, old_terms)
    before = registry.clone()

    with pytest.raises(
        synonyms.SynonymWriteError,
        match="post-write quality gate",
    ):
        await synonyms.replace_auto_synonyms(
            conn,
            "memphis",
            1,
            "hash-1",
            generated,
            registry=registry,
            prior_enrichment_current=True,
        )

    assert registry.records == before.records
    assert registry.owners == before.owners
    assert registry.effective_counts == before.effective_counts


@pytest.mark.asyncio
async def test_collision_heavy_same_generation_response_keeps_old_registry_unchanged():
    old_terms = [f"старая точная фраза {index}" for index in range(8)]
    generated = [f"чужая занятая фраза {index}" for index in range(8)]
    existing = [
        {
            "article_id": 1,
            "source": "auto",
            "status": "approved",
            "term": term,
            "server_id": "memphis",
        }
        for term in old_terms
    ] + [
        {
            "article_id": 2,
            "source": "auto",
            "status": "approved",
            "term": term,
            "server_id": "memphis",
        }
        for term in generated
    ]
    conn = _ReplaceConnection(
        article_state={
            "id": 1,
            "article_hash": "hash-1",
            "enrich_hash": "hash-1",
            "full_text": "Статья 1. Действующая содержательная норма.",
            "server_id": "memphis",
        },
        existing_synonyms=existing,
    )
    registry = _rich_registry(1, old_terms)
    before = registry.clone()

    with pytest.raises(synonyms.SynonymWriteError, match="quality gate"):
        await synonyms.replace_auto_synonyms(
            conn,
            "memphis",
            1,
            "hash-1",
            generated,
            registry=registry,
            prior_enrichment_current=True,
        )

    assert registry.records == before.records
    assert registry.owners == before.owners
