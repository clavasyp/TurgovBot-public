from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_relation_truths,
    _normalized_text,
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
    validate_synonym_source_evidence(
        source,
        [source.rstrip("."), output],
    )


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)


def _assert_all_rejected(
    validate: RelationValidator,
    source: str,
    unsafe_outputs: tuple[str, ...],
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
    ("source", "honest_outputs", "unsafe_outputs"),
    (
        (
            "SANG или FIB/USSS могут отозвать пропуск.",
            (
                "SANG может отозвать пропуск",
                "FIB/USSS могут отозвать пропуск",
            ),
            (
                "FIB может отозвать пропуск",
                "USSS может отозвать пропуск",
            ),
        ),
        (
            "FIB/USSS или SANG могут отозвать пропуск.",
            (
                "FIB/USSS могут отозвать пропуск",
                "SANG может отозвать пропуск",
            ),
            (
                "FIB может отозвать пропуск",
                "USSS может отозвать пропуск",
            ),
        ),
        (
            "(FIB или USSS)/SANG могут отозвать пропуск.",
            ("(USSS или FIB)/SANG могут отозвать пропуск",),
            (
                "FIB может отозвать пропуск",
                "USSS может отозвать пропуск",
                "SANG может отозвать пропуск",
                "FIB или USSS могут отозвать пропуск",
            ),
        ),
    ),
    ids=("or-then-slash", "slash-then-or", "outer-slash-is-atomic"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_mixed_slash_and_or_actor_expressions_keep_atomic_branches(
    source: str,
    honest_outputs: tuple[str, ...],
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    for honest in honest_outputs:
        validate(source, honest)
    _assert_all_rejected(validate, source, unsafe_outputs)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_permission_comma_list_with_final_or_exposes_each_branch(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр с разрешения суда Альфа, с согласия "
        "прокуратуры Бета или с одобрения губернатора Гамма может "
        "отозвать пропуск."
    )
    for permission in (
        "с разрешения суда Альфа",
        "с согласия прокуратуры Бета",
        "с одобрения губернатора Гамма",
    ):
        validate(
            source,
            f"Организация Центр {permission} может отозвать пропуск",
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_plain_permission_comma_list_is_conservative_joint(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр с разрешения суда Альфа, с согласия "
        "прокуратуры Бета, с одобрения губернатора Гамма может "
        "отозвать пропуск."
    )
    validate(
        source,
        "Организация Центр с одобрения губернатора Гамма и с согласия "
        "прокуратуры Бета и с разрешения суда Альфа может отозвать пропуск",
    )
    _assert_all_rejected(
        validate,
        source,
        tuple(
            f"Организация Центр {permission} может отозвать пропуск"
            for permission in (
                "с разрешения суда Альфа",
                "с согласия прокуратуры Бета",
                "с одобрения губернатора Гамма",
            )
        ),
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_permission_and_has_precedence_over_or(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр с разрешения суда Альфа и с согласия "
        "прокуратуры Бета или с одобрения губернатора Гамма может "
        "отозвать пропуск."
    )
    validate(
        source,
        "Организация Центр с согласия прокуратуры Бета и с разрешения "
        "суда Альфа может отозвать пропуск",
    )
    validate(
        source,
        "Организация Центр с одобрения губернатора Гамма может "
        "отозвать пропуск",
    )
    _assert_all_rejected(
        validate,
        source,
        tuple(
            f"Организация Центр {permission} может отозвать пропуск"
            for permission in (
                "с разрешения суда Альфа",
                "с согласия прокуратуры Бета",
            )
        ),
    )


@pytest.mark.parametrize(
    ("relative_clause", "honest", "unsafe_outputs"),
    (
        (
            "суд Альфа выдаст организации разрешение",
            "Организация Центр после получения разрешения суда Альфа "
            "может отозвать пропуск",
            (
                "Организация Центр может отозвать пропуск после того, как "
                "суд Альфа выдаст разрешение",
                "Организация Центр может отозвать пропуск после того, как "
                "суд Альфа выдаст заявителю разрешение",
            ),
        ),
        (
            "организации будет выдано разрешение судом Альфа",
            "Организация Центр может отозвать пропуск после того, как "
            "организации будет выдано разрешение судом Альфа",
            (
                "Организация Центр может отозвать пропуск после того, как "
                "будет выдано разрешение судом Альфа",
                "Организация Центр может отозвать пропуск после того, как "
                "заявителю будет выдано разрешение судом Альфа",
            ),
        ),
        (
            "суд Альфа выдаст организации Бета разрешение",
            "Организация Центр может отозвать пропуск после того, как "
            "суд Альфа выдаст организации Бета разрешение",
            (
                "Организация Центр может отозвать пропуск после того, как "
                "суд Альфа выдаст разрешение",
                "Организация Центр может отозвать пропуск после того, как "
                "суд Альфа выдаст организации Гамма разрешение",
            ),
        ),
        (
            "организации Бета будет выдано разрешение судом Альфа",
            "Организация Центр может отозвать пропуск после того, как "
            "организации Бета будет выдано разрешение судом Альфа",
            (
                "Организация Центр может отозвать пропуск после того, как "
                "будет выдано разрешение судом Альфа",
                "Организация Центр может отозвать пропуск после того, как "
                "организации Гамма будет выдано разрешение судом Альфа",
            ),
        ),
    ),
    ids=(
        "active-issuer-bare-current-holder",
        "passive-issue-bare-current-holder",
        "active-issuer-named-holder",
        "passive-issue-named-holder",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_permission_recipient_is_preserved_across_issuer_voices(
    relative_clause: str,
    honest: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"{relative_clause}."
    )
    validate(source, honest)
    _assert_all_rejected(validate, source, unsafe_outputs)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_calendar_surface_inside_quoted_actor_is_identity_not_date(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация «Суд 1 мая 2026 года» может отозвать пропуск."
    )
    validate(source, source.rstrip("."))
    truths = _extract_relation_truths(_normalized_text(source))
    assert truths
    assert all(
        argument.role != "calendar_date"
        for truth in truths
        for argument in truth.arguments
    )
    # Existing search compatibility intentionally allows a source-backed
    # named institution to answer through its generic family.  What must not
    # happen is laundering one specific identity into another one.
    validate(source, "Организация может отозвать пропуск")
    _assert_all_rejected(
        validate,
        source,
        (
            "Организация «Суд 2 мая 2026 года» может отозвать пропуск",
        ),
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_numbered_court_keeps_number_as_actor_identity(
    validate: RelationValidator,
) -> None:
    source = "Суд №1 может отозвать пропуск."
    validate(source, "Суд № 1 может отозвать пропуск")
    validate(source, "Суд может отозвать пропуск")
    _assert_all_rejected(
        validate,
        source,
        (
            "Суд №2 может отозвать пропуск",
        ),
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_opaque_coactor_cannot_be_erased_or_replaced(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд может отозвать пропуск совместно с комиссией Альфа."
    )
    validate(source, source.rstrip("."))
    _assert_all_rejected(
        validate,
        source,
        (
            "Суд может отозвать пропуск",
            "Суд совместно с комиссией Бета может отозвать пропуск",
        ),
    )


@pytest.mark.parametrize(
    ("source", "unsafe_outputs"),
    (
        (
            "Суд может отозвать пропуск совместно с.",
            ("Суд может отозвать пропуск",),
        ),
        (
            "Суд может отозвать пропуск совместно с, прокуратурой.",
            (
                "Суд может отозвать пропуск",
                "Суд совместно с прокуратурой может отозвать пропуск",
            ),
        ),
    ),
    ids=("missing-coactor", "empty-coactor-before-comma"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_malformed_or_empty_coactor_fails_closed(
    source: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    _assert_all_rejected(validate, source, unsafe_outputs)
