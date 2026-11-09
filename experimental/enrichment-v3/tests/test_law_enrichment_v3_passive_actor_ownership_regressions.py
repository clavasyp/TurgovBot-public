from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_relation_truths,
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


PASSIVE_ACTOR_CASES = (
    (
        "Организация Альфа выдаёт лицензию.",
        "Лицензия выдаётся, как правило, организацией Альфа",
        "Лицензия выдаётся, как правило, организацией Бета",
        "license",
        "организаци альф",
        "организаци бет",
    ),
    (
        "Организация Альфа выдаёт лицензию.",
        "Лицензия, как правило, выдаётся организацией Альфа",
        "Лицензия, как правило, выдаётся организацией Бета",
        "license",
        "организаци альф",
        "организаци бет",
    ),
    (
        "Организация Альфа выдаёт лицензию.",
        "Лицензия выдаётся в установленном порядке организацией Альфа",
        "Лицензия выдаётся в установленном порядке организацией Бета",
        "license",
        "организаци альф",
        "организаци бет",
    ),
    (
        "Организация Альфа выдаёт лицензию.",
        "Лицензия выдаётся (как правило) организацией Альфа",
        "Лицензия выдаётся (как правило) организацией Бета",
        "license",
        "организаци альф",
        "организаци бет",
    ),
    (
        "Суд Альфа отзывает пропуск.",
        "Пропуск отзывается, как правило, судом Альфа",
        "Пропуск отзывается, как правило, судом Бета",
        "access_pass",
        "суд альф",
        "суд бет",
    ),
    (
        "Организация Альфа выдаёт паспорт.",
        "Паспорт выдаётся в установленном порядке организацией Альфа",
        "Паспорт выдаётся в установленном порядке организацией Бета",
        "passport",
        "организаци альф",
        "организаци бет",
    ),
    (
        "Суд Альфа выписывает ордер.",
        "Ордер выписывается (как правило) судом Альфа",
        "Ордер выписывается (как правило) судом Бета",
        "warrant",
        "суд альф",
        "суд бет",
    ),
)


@pytest.mark.parametrize(
    ("source", "supported", "changed", "_object", "_actor", "_changed_actor"),
    PASSIVE_ACTOR_CASES,
    ids=(
        "post-action-discourse",
        "pre-action-discourse",
        "legal-manner",
        "parenthetical-discourse",
        "revocation",
        "passport",
        "warrant",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_passive_neutral_modifier_keeps_exact_instrumental_actor(
    source: str,
    supported: str,
    changed: str,
    _object: str,
    _actor: str,
    _changed_actor: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
    with pytest.raises(ValueError):
        validate(source, changed)


@pytest.mark.parametrize(
    ("_source", "supported", "changed", "direct_object", "actor", "changed_actor"),
    PASSIVE_ACTOR_CASES,
    ids=(
        "post-action-discourse",
        "pre-action-discourse",
        "legal-manner",
        "parenthetical-discourse",
        "revocation",
        "passport",
        "warrant",
    ),
)
def test_v3_passive_neutral_modifier_does_not_turn_actor_into_object(
    _source: str,
    supported: str,
    changed: str,
    direct_object: str,
    actor: str,
    changed_actor: str,
) -> None:
    supported_truths = _extract_relation_truths(supported)
    changed_truths = _extract_relation_truths(changed)

    assert any(
        truth.direct_object == direct_object
        and truth.operative_actor == actor
        for truth in supported_truths
    )
    assert any(
        truth.direct_object == direct_object
        and truth.operative_actor == changed_actor
        for truth in changed_truths
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_passive_neutral_modifier_cannot_invent_specific_actor(
    validate: RelationValidator,
) -> None:
    source = "Организация выдаёт лицензию."
    output = "Лицензия выдаётся, как правило, организацией Альфа"

    with pytest.raises(ValueError):
        validate(source, output)


def test_v3_passive_neutral_mask_preserves_condition_and_oblique_fence() -> None:
    conditioned = _extract_relation_truths(
        "Лицензия выдаётся (при чрезвычайном положении) "
        "организацией Альфа"
    )
    oblique = _extract_relation_truths(
        "Лицензия выдаётся при поддержке организацией Бета"
    )

    assert any(
        truth.direct_object == "license"
        and truth.operative_actor == "организаци альф"
        and "condition:чрезвычайн положен" in truth.condition
        for truth in conditioned
    )
    assert all(not truth.operative_actor for truth in oblique)
