from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    validate_enrichment_title_source_evidence,
    validate_penalty_source_evidence,
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


def _validate_penalty_summary(source: str, output: str) -> None:
    validate_penalty_source_evidence(source, output, None)


def _validate_penalty_other(source: str, output: str) -> None:
    validate_penalty_source_evidence(source, None, {"other": output})


ALL_OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
    _validate_penalty_summary,
    _validate_penalty_other,
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
def test_v3_negative_past_passive_accepts_structural_adverb_bridge(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация «Альфа» не была официально закрыта прокуратурой."
    )
    faithful = (
        "Организация «Альфа» официально не была закрыта прокуратурой"
    )
    validate(source, faithful)
    for unsafe in (
        "Организация «Альфа» была официально закрыта прокуратурой",
        "Организация «Альфа» не была фактически закрыта прокуратурой",
        "Организация «Альфа» не была закрыта прокуратурой",
    ):
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
def test_v3_action_status_modifier_survives_active_passive_paraphrase(
    validate: RelationValidator,
) -> None:
    source = "Лицензия была временно аннулирована прокуратурой."
    faithful = "Прокуратура временно аннулировала лицензию"
    validate(source, faithful)
    for unsafe in (
        "Прокуратура окончательно аннулировала лицензию",
        "Прокуратура аннулировала лицензию",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)
