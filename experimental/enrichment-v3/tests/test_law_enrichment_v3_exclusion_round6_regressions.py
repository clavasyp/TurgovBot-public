from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    validate_enrichment_title_source_evidence,
    validate_synonym_source_evidence,
)


RelationValidator = Callable[[str, str], None]


def _validate_title(source: str, output: str) -> None:
    validate_enrichment_title_source_evidence(
        source,
        output,
        source_title=None,
    )


def _validate_single_synonym(source: str, output: str) -> None:
    validate_synonym_source_evidence(source, [output])


def _validate_full_set(source: str, output: str) -> None:
    source_surface = source.rstrip(".")
    synonyms = [source_surface]
    if output != source_surface:
        synonyms.append(output)
    validate_synonym_source_evidence(source, synonyms)


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)


def _assert_rejected(
    validate: RelationValidator,
    source: str,
    *unsafe_outputs: str,
) -> None:
    unexpectedly_supported: list[str] = []
    for unsafe in unsafe_outputs:
        try:
            validate(source, unsafe)
        except ValueError:
            continue
        unexpectedly_supported.append(unsafe)
    assert not unexpectedly_supported, (
        "source-unsupported outputs were accepted: "
        f"{unexpectedly_supported!r}"
    )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Лицензия изымается за любое нарушение, кроме того случая, "
            "когда нарушение устранено."
        ),
        (
            "Лицензия изымается за любое нарушение, кроме этого нарушения, "
            "если оно устранено."
        ),
    ),
    ids=("governed-case", "governed-violation"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_governed_krome_togo_is_a_real_exclusion(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        "Лицензия изымается за любое нарушение",
    )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Лицензия изымается за нарушение закона, "
            "кроме (особо тяжкого убийства)."
        ),
        (
            "Лицензия изымается за нарушение закона "
            "(кроме особо тяжкого убийства)."
        ),
    ),
    ids=("parenthetical-np", "parenthetical-exclusion"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_balanced_adjectival_parenthetical_exclusion_is_preserved(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(
        source,
        (
            "Лицензия изымается за нарушение закона, "
            "кроме особо тяжкого убийства"
        ),
    )
    _assert_rejected(
        validate,
        source,
        "Лицензия изымается за нарушение закона",
        "Лицензия изымается за нарушение закона, кроме убийства",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_malformed_parenthetical_exclusion_fails_closed(
    validate: RelationValidator,
) -> None:
    source = (
        "Если нарушены правила, кроме (особо тяжкого убийства, "
        "лицензия изымается."
    )
    _assert_rejected(
        validate,
        source,
        "Если нарушены правила, лицензия изымается",
        (
            "Если нарушены правила, кроме особо тяжкого убийства, "
            "лицензия изымается"
        ),
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_exclusion_list_stays_complete_but_is_order_invariant(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия изымается за нарушение правил, кроме убийства, "
        "мошенничества и коррупции."
    )
    validate(
        source,
        (
            "Лицензия изымается за нарушение правил, кроме коррупции, "
            "убийства и мошенничества"
        ),
    )
    _assert_rejected(
        validate,
        source,
        "Лицензия изымается за нарушение правил, кроме убийства",
        (
            "Лицензия изымается за нарушение правил, кроме убийства, "
            "мошенничества и кражи"
        ),
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_leading_actor_roster_exclusion_remains_roster_owned(
    validate: RelationValidator,
) -> None:
    source = (
        "Кроме LSPD и LSSD, полиция может выдавать лицензию."
    )
    validate(
        source,
        "Полиция, кроме LSSD и LSPD, может выдавать лицензию",
    )
    _assert_rejected(
        validate,
        source,
        "Полиция может выдавать лицензию",
        "LSPD может выдавать лицензию",
        "LSSD может выдавать лицензию",
    )


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            (
                "Суд может отозвать лицензию за нарушение закона, "
                "кроме тяжкого убийства."
            ),
            (
                "Суд может отозвать лицензию за нарушение закона, "
                "кроме тяжкого убийства"
            ),
        ),
        (
            (
                "Если нарушен закон (кроме тяжкого убийства), "
                "суд может отозвать лицензию."
            ),
            (
                "Если нарушен закон, кроме тяжкого убийства, "
                "суд может отозвать лицензию"
            ),
        ),
    ),
    ids=("post-action", "nested-condition"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_post_action_and_nested_exclusion_cannot_drop_or_change(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
    _assert_rejected(
        validate,
        source,
        supported.replace(", кроме тяжкого убийства", ""),
        supported.replace("тяжкого убийства", "мошенничества"),
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_leading_exclusion_reorders_only_with_same_condition(
    validate: RelationValidator,
) -> None:
    source = (
        "Кроме убийства, если нарушены правила хранения оружия, "
        "лицензия изымается."
    )
    supported = (
        "Если нарушены правила хранения оружия, кроме убийства, "
        "лицензия изымается"
    )
    validate(source, supported)
    _assert_rejected(
        validate,
        source,
        supported.replace("хранения оружия", "перевозки оружия"),
        supported.replace("убийства", "мошенничества"),
        supported.replace(", кроме убийства", ""),
    )
