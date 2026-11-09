"""Shared synonym normalization, collision and additive-write safeguards."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from services import law_synonym_service as synonyms


def test_runtime_canonical_expansion_keeps_surface_and_live_companion():
    groups = synonyms.expand_synonym_terms(["  МЕНТА   ИЗБИЛИ  ", "что будет за убийство"])

    assert groups["сотрудник телесные повреждения"] == {
        "мента избили",
        "сотрудник телесные повреждения",
    }
    assert groups["убийство"] == {"что будет за убийство", "убийство"}


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


class _Connection:
    def __init__(self, *, article_rows, existing_synonyms=()):
        self.article_rows = article_rows
        self.existing_synonyms = list(existing_synonyms)
        self.backfill_runs = {}
        self.calls = []
        self.next_id = 1000

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

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        normalized = " ".join(query.split())
        if "SELECT id, article_hash FROM law_articles" in normalized:
            requested, server_id = args
            return [
                row for row in self.article_rows
                if row["id"] in requested
                and row["server_id"] == server_id
                and row["kind"] == "article"
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
            server_id, candidate_ids, candidate_terms = args
            matches = []
            for candidate_id, candidate_term in zip(
                candidate_ids, candidate_terms
            ):
                for row in self.existing_synonyms:
                    if (
                        row["server_id"] == server_id
                        and row.get("status", "approved") == "approved"
                        and int(row["article_id"]) != int(candidate_id)
                        and row["term"] == candidate_term
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
                    "article_id": article_id,
                    "source": "auto",
                    "status": "approved",
                    "term": term,
                    "server_id": server_id,
                })
            return rows
        raise AssertionError(f"unexpected fetch: {normalized}")


class _Db:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def transaction(self):
        yield self.conn


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
        if "SELECT article_id, term, source, status FROM law_synonyms" in normalized:
            self.calls.append(("fetch", query, args))
            return [
                {
                    "article_id": row["article_id"],
                    "term": row["term"],
                    "source": row["source"],
                    "status": row.get("status", "approved"),
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
