from __future__ import annotations

import services.law_enrichment_validation as validation


class _CountedPattern:
    """Compiled-regex proxy that measures deterministic searched windows."""

    def __init__(self, pattern) -> None:
        self._pattern = pattern
        self.scanned_characters = 0

    def _end(self, value: str, endpos: int | None) -> int:
        return len(value) if endpos is None else endpos

    def search(self, value: str, pos: int = 0, endpos: int | None = None):
        end = self._end(value, endpos)
        self.scanned_characters += max(0, end - pos)
        return self._pattern.search(value, pos, end)

    def finditer(
        self,
        value: str,
        pos: int = 0,
        endpos: int | None = None,
    ):
        end = self._end(value, endpos)
        self.scanned_characters += max(0, end - pos)
        return self._pattern.finditer(value, pos, end)

    def __getattr__(self, name: str):
        return getattr(self._pattern, name)

    def reset(self) -> None:
        self.scanned_characters = 0


def test_permission_authority_scan_work_is_clause_bounded(monkeypatch) -> None:
    """Doubling independent clauses must not rescan every older suffix."""

    original_scan = validation._relation_delimited_scan
    scanned_characters = 0

    def counted_scan(value: str):
        nonlocal scanned_characters
        scanned_characters += len(value)
        return original_scan(value)

    monkeypatch.setattr(validation, "_relation_delimited_scan", counted_scan)

    unit = (
        "С разрешения Суда Альфа, организация Центр может выдать лицензию; "
    )

    def parse_and_measure(clause_count: int) -> int:
        nonlocal scanned_characters
        source = unit * clause_count
        validation._relation_permission_spans.cache_clear()
        scanned_characters = 0
        spans = validation._relation_permission_spans(source)
        assert len(spans) == clause_count
        return scanned_characters

    small_work = parse_and_measure(8)
    large_work = parse_and_measure(16)

    # Linear work is close to 2x.  Leave headroom for fixed parser passes,
    # while rejecting the former repeated full-suffix scan (>6x here).
    assert large_work <= 3 * small_work


def test_permission_comma_actor_probe_never_crosses_semicolon(
    monkeypatch,
) -> None:
    """A later action must not be rescanned for every earlier permission."""

    modal = _CountedPattern(validation._RELATION_MODAL_TOKEN_RE)
    predicate = _CountedPattern(validation._RELATION_PREDICATE_RE)
    monkeypatch.setattr(validation, "_RELATION_MODAL_TOKEN_RE", modal)
    monkeypatch.setattr(validation, "_RELATION_PREDICATE_RE", predicate)

    unit = "С разрешения суда Альфа, Организация Центр; "

    def parse_and_measure(unit_count: int) -> int:
        source = unit * unit_count + "FIB может отозвать пропуск."
        validation._relation_permission_spans.cache_clear()
        modal.reset()
        predicate.reset()
        validation._relation_permission_spans(source)
        return modal.scanned_characters + predicate.scanned_characters

    small_work = parse_and_measure(32)
    large_work = parse_and_measure(64)
    assert large_work <= 3 * small_work


def test_ground_exclusion_comma_list_action_scan_is_linear(monkeypatch) -> None:
    """One long exclusion list must scan actions once, not per comma."""

    action = _CountedPattern(validation._RELATION_ACTION_RE)
    monkeypatch.setattr(validation, "_RELATION_ACTION_RE", action)

    def parse_and_measure(item_count: int) -> int:
        source = (
            "Лицензия изымается за нарушение закона, за вычетом "
            + ", ".join(
                f"деяние Альфа{index}" for index in range(item_count)
            )
        )
        validation._relation_ground_exclusion_spans.cache_clear()
        action.reset()
        syntax = validation._relation_quote_syntax_surface(source)
        action_match = next(action.finditer(syntax))
        validation._relation_ground_exclusion_spans(
            source,
            action_match.start("action"),
            action_match.end("action"),
        )
        return action.scanned_characters

    small_work = parse_and_measure(128)
    large_work = parse_and_measure(256)
    assert large_work <= 3 * small_work


def test_leading_ground_exclusion_patient_probe_is_bounded(monkeypatch) -> None:
    """An action-first exclusion list must not reparse every comma suffix."""

    original = validation._relation_prefix_without_adjuncts
    scanned_characters = 0

    def counted_prefix(value: str) -> str:
        nonlocal scanned_characters
        scanned_characters += len(value)
        return original(value)

    monkeypatch.setattr(
        validation,
        "_relation_prefix_without_adjuncts",
        counted_prefix,
    )

    def parse_and_measure(item_count: int) -> int:
        nonlocal scanned_characters
        source = (
            "За вычетом "
            + ", ".join(
                f"убийство Альфа{index}" for index in range(item_count)
            )
            + ", изымается лицензия."
        )
        syntax = validation._relation_quote_syntax_surface(source)
        action = next(
            match
            for match in validation._RELATION_ACTION_RE.finditer(syntax)
            if validation._relation_event(match.group("action")) != "nominal"
        )
        validation._relation_ground_exclusion_spans.cache_clear()
        scanned_characters = 0
        spans = validation._relation_ground_exclusion_spans(
            source,
            action.start("action"),
            action.end("action"),
        )
        assert len(spans) == 1
        assert "убийств" in spans[0].value
        return scanned_characters

    small_work = parse_and_measure(128)
    large_work = parse_and_measure(256)

    # Only the bounded possible patient/actor tail is inspected.  Doubling an
    # already-over-window exclusion list therefore cannot recreate the old
    # progressively-long O(n^2) comma-suffix work.
    assert large_work <= 2 * small_work


def test_relative_permission_boundary_work_is_linear(monkeypatch) -> None:
    """Every relative prerequisite shares clause and next-marker indexes."""

    clause_boundary = _CountedPattern(
        validation._RELATION_PERMISSION_CLAUSE_BOUNDARY_RE
    )
    hard_boundary = _CountedPattern(
        validation._RELATION_PERMISSION_HARD_BOUNDARY_RE
    )
    requirement = _CountedPattern(
        validation._RELATION_PERMISSION_REQUIREMENT_RE
    )
    monkeypatch.setattr(
        validation,
        "_RELATION_PERMISSION_CLAUSE_BOUNDARY_RE",
        clause_boundary,
    )
    monkeypatch.setattr(
        validation,
        "_RELATION_PERMISSION_HARD_BOUNDARY_RE",
        hard_boundary,
    )
    monkeypatch.setattr(
        validation,
        "_RELATION_PERMISSION_REQUIREMENT_RE",
        requirement,
    )

    unit = (
        "после того как организация получит разрешение суда Альфа, "
    )

    def parse_and_measure(requirement_count: int) -> int:
        source = (
            unit * requirement_count
            + "организация может выдать лицензию."
        )
        validation._relation_permission_spans.cache_clear()
        clause_boundary.reset()
        hard_boundary.reset()
        requirement.reset()
        spans = validation._relation_permission_spans(source)
        assert len(spans) == requirement_count
        return (
            clause_boundary.scanned_characters
            + hard_boundary.scanned_characters
            + requirement.scanned_characters
        )

    small_work = parse_and_measure(32)
    large_work = parse_and_measure(64)

    # Three deterministic regex windows now cover the source a constant
    # number of times instead of once per relative intro.
    assert large_work <= 3 * small_work
