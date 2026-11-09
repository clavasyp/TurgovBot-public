from __future__ import annotations

import pytest

from services.law_enrichment_ownership import (
    is_safe_enrichment_title_ownership,
)


@pytest.mark.parametrize(
    ("article", "source_title", "tombstone", "expected"),
    [
        (
            {"title": "Parser title", "title_provenance": "source"},
            "Parser title",
            False,
            True,
        ),
        (
            {"title": "Different", "title_provenance": "source"},
            "Parser title",
            False,
            False,
        ),
        (
            {"title": "Parser title", "title_provenance": "generated"},
            "Parser title",
            False,
            False,
        ),
        (
            {"title": "Generated", "title_provenance": "generated"},
            None,
            False,
            True,
        ),
        (
            {"title": None, "title_provenance": None},
            None,
            False,
            True,
        ),
        (
            {"title": "Legacy", "title_provenance": "legacy_unknown"},
            None,
            False,
            False,
        ),
        (
            {"title": "Unattested", "title_provenance": None},
            None,
            False,
            False,
        ),
        (
            {"title": None, "title_provenance": "generated"},
            None,
            False,
            False,
        ),
        (
            {"title": "", "title_provenance": "generated"},
            None,
            False,
            False,
        ),
        (
            {"title": "   ", "title_provenance": "generated"},
            None,
            False,
            False,
        ),
        (
            {"title": 123, "title_provenance": "generated"},
            None,
            False,
            False,
        ),
        (
            {"title": None, "title_provenance": None},
            "Printed article heading",
            True,
            True,
        ),
        (
            {"title": "Old repeal title", "title_provenance": "generated"},
            None,
            True,
            False,
        ),
    ],
)
def test_parser_backed_title_ownership_contract(
    article,
    source_title,
    tombstone,
    expected,
):
    assert is_safe_enrichment_title_ownership(
        article,
        source_title,
        tombstone,
    ) is expected


def test_invalid_call_shapes_fail_closed():
    assert is_safe_enrichment_title_ownership(
        {"title": None, "title_provenance": None},
        123,  # type: ignore[arg-type]
        False,
    ) is False
    assert is_safe_enrichment_title_ownership(
        {"title": None, "title_provenance": None},
        None,
        1,  # type: ignore[arg-type]
    ) is False
    assert is_safe_enrichment_title_ownership(
        None,  # type: ignore[arg-type]
        None,
        False,
    ) is False
