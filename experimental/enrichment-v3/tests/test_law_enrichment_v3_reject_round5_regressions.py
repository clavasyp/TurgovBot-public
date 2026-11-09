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
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд может отозвать лицензию за нарушение закона, кроме "
            "убийства.",
            "Отзыв лицензии судом за нарушение закона",
        ),
        (
            "Кроме убийства, суд может отозвать лицензию за нарушение "
            "закона.",
            "Отзыв лицензии судом за нарушение закона",
        ),
        (
            "Если нарушен закон (кроме убийства), суд может отозвать "
            "лицензию.",
            "Отзыв лицензии судом за нарушение закона",
        ),
    ),
    ids=("post-action", "leading", "nested-condition"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_revocation_cannot_drop_ground_exclusion(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Полиция с разрешения суда Альфа может отозвать лицензию.",
            "Отзыв лицензии полицией",
        ),
        (
            "Полиция с разрешения суда Альфа и прокуратуры Бета может "
            "отозвать лицензию.",
            "Отзыв лицензии полицией с разрешения суда Альфа",
        ),
    ),
    ids=("drop-permission", "shorten-joint-permission"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_revocation_cannot_drop_or_shorten_permission(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Суд может отозвать лицензию за нарушение закона, кроме "
            "убийства.",
            "Отзыв лицензии судом за нарушение закона, кроме убийства",
        ),
        (
            "Кроме убийства, суд может отозвать лицензию за нарушение "
            "закона.",
            "Отзыв лицензии судом за нарушение закона, кроме убийства",
        ),
        (
            "Если нарушен закон (кроме убийства), суд может отозвать "
            "лицензию.",
            "Отзыв лицензии судом за нарушение закона, кроме убийства",
        ),
        (
            "Полиция с разрешения суда Альфа может отозвать лицензию.",
            "Отзыв лицензии полицией с разрешения суда Альфа",
        ),
        (
            "Полиция с разрешения суда Альфа и прокуратуры Бета может "
            "отозвать лицензию.",
            "Отзыв лицензии полицией с разрешения суда Альфа и прокуратуры "
            "Бета",
        ),
        (
            "Полиция может отозвать лицензию.",
            "Отзыв лицензии полицией",
        ),
    ),
    ids=(
        "same-post-action-exclusion",
        "same-leading-exclusion",
        "same-nested-exclusion",
        "same-permission",
        "same-joint-permission",
        "broad-unconditional-nominal",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_supported_nominal_revocation_facets_remain_searchable(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)


@pytest.mark.parametrize("modifier", ("затем", "лично"))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_permission_boundary_preserves_modifier_before_ascii_actor(
    modifier: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"С разрешения суда Альфа, {modifier} FIB может отозвать пропуск."
    )
    supported = (
        f"{modifier.capitalize()} FIB с разрешения суда Альфа может "
        "отозвать пропуск"
    )
    validate(source, supported)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Лицензия изымается за нарушение закона, кроме того — "
            "гражданин читает закон."
        ),
        (
            "Лицензия изымается за нарушение закона, кроме того "
            "(это важно), гражданин читает закон."
        ),
    ),
    ids=("dash", "parenthetical-aside"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_krome_togo_discourse_variants_do_not_create_exclusion(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, "Лицензия изымается за нарушение закона")


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Кроме убийства, если нарушены правила, лицензия изымается.",
            "Если нарушены правила, кроме убийства, лицензия изымается",
        ),
        (
            "За исключением убийства, в случае нарушения правил лицензия "
            "изымается.",
            "В случае нарушения правил, кроме убийства, лицензия изымается",
        ),
        (
            "За вычетом убийства, при нарушении правил лицензия изымается.",
            "При нарушении правил, кроме убийства, лицензия изымается",
        ),
    ),
    ids=("if", "in-case", "on-condition"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_leading_exclusion_can_move_inside_same_condition(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
