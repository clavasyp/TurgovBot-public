from __future__ import annotations

import pytest

from tests.test_law_enrichment_v3_closed_uk_list_regressions import (
    ALL_OUTPUT_PATHS as BASE_OUTPUT_PATHS,
    RelationValidator,
)
from services.law_enrichment_validation import validate_penalty_source_evidence


def _validate_penalty_summary(source: str, output: str) -> None:
    validate_penalty_source_evidence(
        "Наказание: " + source,
        output,
        None,
    )


def _validate_penalty_other(source: str, output: str) -> None:
    validate_penalty_source_evidence(
        "Наказание: " + source,
        None,
        {"other": output},
    )


ALL_OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    *BASE_OUTPUT_PATHS[:3],
    _validate_penalty_summary,
    _validate_penalty_other,
)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Прокуратура аннулировала лицензию.",
            "Прокуратура аннулировала",
        ),
        (
            "Суд выдал лицензию прокуратуре.",
            "Суд выдал",
        ),
    ),
    ids=("revocation", "issue"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_transitive_issue_and_revocation_cannot_drop_patient(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "unsafe",
    (
        "Суд аннулировал организацию",
        "Прокуратура аннулировала обыск",
        "Организация аннулировала бюджет",
    ),
    ids=("organization", "search", "budget"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_hard_invalidation_rejects_impossible_known_patient_family(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура аннулировала лицензию, суд закрыл организацию, "
        "суд назначил обыск, а организация утвердила бюджет."
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Организацию закрыл суд.",
            "Суд закрыл организацию",
        ),
        (
            "Обыск назначил суд.",
            "Суд назначил обыск",
        ),
        (
            "Бюджет утвердила организация.",
            "Организация утвердила бюджет",
        ),
        (
            "Документ аннулировал суд.",
            "Суд аннулировал документ",
        ),
        (
            "Акт аннулировал суд.",
            "Суд аннулировал акт",
        ),
        (
            "Лицензию аннулировала прокуратура.",
            "Прокуратура аннулировала лицензию",
        ),
    ),
    ids=(
        "close-organization",
        "appoint-search",
        "approve-budget",
        "invalidate-document",
        "invalidate-act",
        "invalidate-license",
    ),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_occurrence_gate_preserves_supported_predicate_patient_pairs(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_nested_owner_parser_variant_does_not_poison_valid_patient(
    validate: RelationValidator,
) -> None:
    source = "Лицензию организации аннулировал суд."
    faithful = "Суд аннулировал лицензию организации"
    validate(source, faithful)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_factual_nominal_executor_keeps_instrumental_case(
    validate: RelationValidator,
) -> None:
    source = (
        "Произошло аннулирование лицензии федеральной прокуратурой."
    )
    faithful = (
        "Аннулирование лицензии федеральной прокуратурой осуществлено"
    )
    unsafe = "Аннулирование лицензии федеральная прокуратура осуществлено"
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_unresolved_actor_group_cannot_use_empty_actor_fallback(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд совместно с федеральной прокуратурой аннулировал лицензию."
    )
    unsafe = (
        "Суд совместно с, федеральной прокуратурой аннулировал лицензию"
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)
