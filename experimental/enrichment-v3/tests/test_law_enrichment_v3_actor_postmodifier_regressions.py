"""The postmodifier of an institutional actor name is part of its identity.

``организацией Центр при губернаторе`` and ``организацией Центр при шерифе``
are two different bodies.  The open ``(?:за|при) ...`` ground scanner used to
close the actor audit right before that postmodifier, while the ground facet
itself only records a closed vocabulary of typed grounds.  A lowercase office
inside the postmodifier therefore vanished from every facet and could be
swapped, dropped or invented for free.  A typed ground (``при задержании``,
``за мошенничество``) and a parsed condition (``при наличии оснований``) are
recorded facets and must keep behaving as facets.
"""

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
    validate_synonym_source_evidence(
        source,
        [source_surface, output]
        if output != source_surface
        else [source_surface],
    )


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)


ACTOR_POSTMODIFIER_SWAP_CASES: tuple[tuple[str, str, str, str], ...] = (
    (
        "governor-to-sheriff",
        "Пропуск был отозван организацией Центр при губернаторе.",
        "Пропуск был отозван организацией Центр при шерифе",
        "Пропуск был отозван организацией Центр при губернаторе",
    ),
    (
        "may-governor-to-sheriff",
        "Пропуск может быть отозван организацией Центр при губернаторе.",
        "Пропуск может быть отозван организацией Центр при шерифе",
        "Пропуск может быть отозван организацией Центр при губернаторе",
    ),
    (
        "prosecutor-to-judge",
        "Пропуск был отозван организацией Центр при прокуроре.",
        "Пропуск был отозван организацией Центр при судье",
        "Пропуск был отозван организацией Центр при прокуроре",
    ),
    (
        "parliament-to-government",
        "Пропуск был отозван организацией Штаб при парламенте.",
        "Пропуск был отозван организацией Штаб при правительстве",
        "Пропуск был отозван организацией Штаб при парламенте",
    ),
    (
        "vice-governor-to-governor",
        "Пропуск был отозван организацией Центр при вице-губернаторе.",
        "Пропуск был отозван организацией Центр при губернаторе",
        "Пропуск был отозван организацией Центр при вице-губернаторе",
    ),
    (
        "chief-to-director",
        "Пропуск был отозван организацией Центр при начальнике.",
        "Пропуск был отозван организацией Центр при директоре",
        "Пропуск был отозван организацией Центр при начальнике",
    ),
    (
        "police-to-prosecutors",
        "Пропуск был отозван организацией Центр при полиции.",
        "Пропуск был отозван организацией Центр при прокуратуре",
        "Пропуск был отозван организацией Центр при полиции",
    ),
    (
        "licence-governor-to-sheriff",
        "Лицензия была аннулирована организацией Центр при губернаторе.",
        "Лицензия была аннулирована организацией Центр при шерифе",
        "Лицензия была аннулирована организацией Центр при губернаторе",
    ),
    (
        "may-licence-mayor-to-minister",
        "Лицензия может быть аннулирована организацией Центр при мэре.",
        "Лицензия может быть аннулирована организацией Центр при министре",
        "Лицензия может быть аннулирована организацией Центр при мэре",
    ),
)


ACTOR_POSTMODIFIER_DROP_CASES: tuple[tuple[str, str, str], ...] = (
    (
        "drop-governor",
        "Пропуск был отозван организацией Центр при губернаторе.",
        "Пропуск был отозван организацией Центр",
    ),
    (
        "may-drop-governor",
        "Пропуск может быть отозван организацией Центр при губернаторе.",
        "Пропуск может быть отозван организацией Центр",
    ),
)


ACTOR_POSTMODIFIER_INVENTION_CASES: tuple[tuple[str, str, str], ...] = (
    (
        "invent-governor",
        "Пропуск был отозван организацией Центр.",
        "Пропуск был отозван организацией Центр при губернаторе",
    ),
    (
        "may-invent-mayor",
        "Пропуск может быть отозван организацией Совет.",
        "Пропуск может быть отозван организацией Совет при мэре",
    ),
)


RECORDED_FACET_CASES: tuple[tuple[str, str], ...] = (
    (
        "typed-ground-fraud",
        "Пропуск был отозван судом при мошенничестве.",
    ),
    (
        "typed-ground-detention",
        "Лицензия была изъята судом при задержании.",
    ),
    (
        "typed-ground-lateness",
        "Пропуск был отозван судом за опоздание.",
    ),
    (
        "typed-ground-law-violation",
        "Лицензия была аннулирована судом за нарушение закона.",
    ),
    (
        "parsed-condition",
        "Пропуск был отозван судом при наличии оснований.",
    ),
)


@pytest.mark.parametrize(
    ("case_id", "source", "changed", "supported"),
    ACTOR_POSTMODIFIER_SWAP_CASES,
    ids=[case[0] for case in ACTOR_POSTMODIFIER_SWAP_CASES],
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_actor_postmodifier_office_cannot_be_swapped(
    case_id: str,
    source: str,
    changed: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    del case_id
    with pytest.raises(ValueError):
        validate(source, changed)
    validate(source, supported)


@pytest.mark.parametrize(
    ("case_id", "source", "changed"),
    ACTOR_POSTMODIFIER_DROP_CASES,
    ids=[case[0] for case in ACTOR_POSTMODIFIER_DROP_CASES],
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_actor_postmodifier_cannot_be_dropped(
    case_id: str,
    source: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    del case_id
    with pytest.raises(ValueError):
        validate(source, changed)


@pytest.mark.parametrize(
    ("case_id", "source", "changed"),
    ACTOR_POSTMODIFIER_INVENTION_CASES,
    ids=[case[0] for case in ACTOR_POSTMODIFIER_INVENTION_CASES],
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_actor_postmodifier_cannot_be_invented(
    case_id: str,
    source: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    del case_id
    with pytest.raises(ValueError):
        validate(source, changed)


@pytest.mark.parametrize(
    ("case_id", "source"),
    RECORDED_FACET_CASES,
    ids=[case[0] for case in RECORDED_FACET_CASES],
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_recorded_facet_tail_still_supports_its_own_retelling(
    case_id: str,
    source: str,
    validate: RelationValidator,
) -> None:
    del case_id
    validate(source, source.rstrip("."))


@pytest.mark.parametrize(
    ("source", "expected_actor"),
    (
        (
            "Пропуск был отозван организацией Центр при губернаторе.",
            "организаци центр губернатор",
        ),
        (
            "Пропуск может быть отозван организацией Центр при губернаторе.",
            "организаци центр губернатор",
        ),
        (
            "Пропуск был отозван организацией Центр.",
            "организаци центр",
        ),
    ),
    ids=("past-passive", "modal", "no-postmodifier"),
)
def test_v3_actor_key_keeps_the_office_named_in_its_postmodifier(
    source: str,
    expected_actor: str,
) -> None:
    actors = {truth.operative_actor for truth in _extract_relation_truths(source)}
    assert actors == {expected_actor}


@pytest.mark.parametrize(
    ("source", "expected_ground"),
    (
        ("Пропуск был отозван судом при задержании.", "ground:detention"),
        ("Пропуск был отозван судом за опоздание.", "ground:lateness"),
    ),
    ids=("detention", "lateness"),
)
def test_v3_typed_ground_tail_is_still_recorded_as_a_ground(
    source: str,
    expected_ground: str,
) -> None:
    grounds: set[str] = set()
    for truth in _extract_relation_truths(source):
        grounds |= set(truth.grounds)
    assert expected_ground in grounds
