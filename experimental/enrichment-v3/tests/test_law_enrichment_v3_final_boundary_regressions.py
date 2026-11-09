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
    validate_synonym_source_evidence(
        source,
        [source.rstrip("."), output],
    )


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_dotted_permission_authority_stays_in_literal_clause(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск с разрешения "
        "суда «St. Mary»."
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, "Организация Центр может отозвать пропуск")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_literal_permission_boundary_does_not_cross_real_sentence(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр может выдать лицензию. "
        "С разрешения суда Альфа организация Центр может отозвать пропуск."
    )
    validate(source, "Организация Центр может выдать лицензию")
    with pytest.raises(ValueError):
        validate(
            source,
            "Организация Центр с разрешения суда Альфа может выдать лицензию",
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_slash_permission_expression_remains_atomic(
    validate: RelationValidator,
) -> None:
    source = (
        "С разрешения суда Альфа / с согласия прокуратуры Бета "
        "организация Центр может отозвать пропуск."
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(
            source,
            "Организация Центр с разрешения суда Альфа может "
            "отозвать пропуск",
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_unknown_explicit_coactor_cannot_be_erased(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд может отозвать пропуск совместно с комиссией Альфа."
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, "Суд может отозвать пропуск")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_leading_date_scopes_tight_coordinated_actions(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд 01.05.2026 может выдать лицензию и отозвать пропуск."
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, "Суд может отозвать пропуск")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_later_date_does_not_leak_to_previous_action(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд выдал лицензию и 01.05.2026 отозвал пропуск."
    )
    validate(source, "Суд выдал лицензию")


@pytest.mark.parametrize(
    ("source", "specific", "unsafe"),
    (
        (
            "В случае нарушения уголовной статьи о разглашении "
            "государственной тайны лицензия парамедика изымается.",
            "Лицензия парамедика изымается за нарушение уголовной "
            "статьи о разглашении государственной тайны",
            "Лицензия парамедика изымается за нарушение уголовной статьи",
        ),
        (
            "В случае нарушения уголовной статьи о незаконном "
            "проникновении лицензия парамедика изымается.",
            "Лицензия парамедика изымается за нарушение уголовной "
            "статьи о незаконном проникновении",
            "Лицензия парамедика изымается за нарушение уголовной статьи",
        ),
    ),
    ids=("state-secret", "trespass"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_unknown_ground_detail_cannot_collapse_to_broad_violation(
    source: str,
    specific: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, specific)
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Лицензия парамедика изымается за все нарушения, "
            "кроме разглашения тайны.",
            "Лицензия парамедика изымается за все нарушения",
        ),
        (
            "Лицензия парамедика изымается за нарушение правил, "
            "кроме разглашения тайны.",
            "Лицензия парамедика изымается за нарушение правил",
        ),
    ),
    ids=("all-violations", "rule-violation"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_general_ground_exclusion_cannot_disappear(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Полиция, кроме отдела внутренних расследований, "
            "может выдавать лицензию.",
            "Полиция может выдавать лицензию",
        ),
        (
            "Организация, кроме филиала Альфа, может выдавать лицензию.",
            "Организация может выдавать лицензию",
        ),
        (
            "Суды, кроме мировых коллегий, могут отзывать пропуск.",
            "Суды могут отзывать пропуск",
        ),
    ),
    ids=("police-department", "organization-branch", "court-panel"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_unknown_actor_subclass_exclusion_cannot_disappear(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "recipient",
    ("заявителю", "владельцу", "гражданину"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_permission_issuer_recipient_cannot_be_rebound_to_main_actor(
    recipient: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"суд Альфа выдаст {recipient} разрешение."
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(
            source,
            "Организация Центр после получения разрешения суда Альфа "
            "может отозвать пропуск",
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nonacquisition_permission_noun_keeps_operative_actor(
    validate: RelationValidator,
) -> None:
    source = (
        "После обсуждения разрешения суд Альфа отозвал пропуск."
    )
    validate(
        source,
        "Суд Альфа отозвал пропуск после обсуждения разрешения",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_atomic_slash_branch_does_not_hide_explicit_or_branch(
    validate: RelationValidator,
) -> None:
    source = "(FIB/USSS) или SANG могут отозвать пропуск."
    validate(source, "SANG может отозвать пропуск")
    with pytest.raises(ValueError):
        validate(source, "FIB может отозвать пропуск")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_permission_comma_list_inherits_final_or(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр с разрешения суда Альфа, с согласия "
        "прокуратуры Бета или с одобрения губернатора Гамма может "
        "отозвать пропуск."
    )
    validate(
        source,
        "Организация Центр с согласия прокуратуры Бета может "
        "отозвать пропуск",
    )

