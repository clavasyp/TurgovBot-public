from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_bounded_nominal_document_truths,
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


def _assert_rejected(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Губернатор и вице-губернатор вправе аннулировать пропуск, "
            "если лицо препятствует работе государственной организации.",
            "аннулирование пропуска вице-губернатором за помехи работе "
            "госорганизации",
        ),
        (
            "Глава и заместитель главы исполнительной власти вправе "
            "аннулировать пропуск, если лицо нарушает установленный режим.",
            "аннулирование пропуска главой или заместителем главы за "
            "нарушение режима",
        ),
        (
            "Пропуск отозван судом или прокуратурой.",
            "отзыв пропуска судом",
        ),
        (
            "Пропуск может быть отозван судом.",
            "отзыв пропуска судом",
        ),
        (
            "Пропуск может быть аннулирован судом в качестве "
            "дополнительной меры наказания.",
            "аннулирование пропуска судом как дополнительная мера наказания",
        ),
        (
            "Служащие должны изъять лицензию на оружие у гражданина за "
            "правонарушения с использованием огнестрельного оружия.",
            "изъятие оружейной лицензии за правонарушение",
        ),
    ),
    ids=(
        "sf-organization-interference",
        "sf-established-regime",
        "plain-alternative-actor",
        "plain-may-revocation",
        "court-additional-penalty",
        "typed-weapon-offense",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_existing_nominal_compatibility_controls_remain_supported(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)


@pytest.mark.parametrize(
    "invented",
    (
        "Отзыв пропуска судом за убийство",
        "Отзыв пропуска судом за обычную жалобу",
    ),
    ids=("typed-murder", "open-vocabulary-complaint"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_cannot_invent_positive_ground_without_source_evidence(
    invented: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(
        validate,
        "Суд может отозвать пропуск.",
        invented,
    )


@pytest.mark.parametrize(
    ("source", "shortened"),
    (
        (
            "Если нарушен закон о хранении опасных материалов, суд может "
            "отозвать пропуск.",
            "Отзыв пропуска судом за нарушение закона",
        ),
        (
            "Если нарушен закон о хранении опасных материалов, суд может "
            "отозвать лицензию.",
            "Отзыв лицензии судом за нарушение закона",
        ),
    ),
    ids=("access-pass", "license"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_cannot_shorten_open_vocabulary_condition_ground(
    source: str,
    shortened: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, shortened)


@pytest.mark.parametrize(
    ("source", "supported", "invented"),
    (
        (
            "Губернатор и вице-губернатор вправе аннулировать пропуск, "
            "если лицо препятствует работе государственной организации.",
            "Аннулирование пропуска вице-губернатором за помехи работе "
            "госорганизации",
            "Аннулирование пропуска вице-губернатором за кражу",
        ),
        (
            "Глава и заместитель главы исполнительной власти вправе "
            "аннулировать пропуск, если лицо нарушает установленный режим.",
            "Аннулирование пропуска главой или заместителем главы за "
            "нарушение режима",
            "Аннулирование пропуска главой или заместителем главы за "
            "обычную жалобу",
        ),
    ),
    ids=("organization-interference", "established-regime"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_sf_nominal_ground_paraphrase_is_exact_not_an_open_bypass(
    source: str,
    supported: str,
    invented: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
    _assert_rejected(validate, source, invented)


@pytest.mark.parametrize(
    ("source", "output", "actor"),
    (
        (
            "Пропуск может быть отозван судом За Права.",
            "Отзыв пропуска судом За Права",
            "суд за прав",
        ),
        (
            "Пропуск может быть отозван судом При Правительстве.",
            "Отзыв пропуска судом При Правительстве",
            "суд правительств",
        ),
    ),
    ids=("za-rights", "pri-government"),
)
def test_v3_unquoted_connector_actor_identity_does_not_collapse_to_bare_court(
    source: str,
    output: str,
    actor: str,
) -> None:

    source_relations = _extract_relation_truths(source)
    output_relations = _extract_bounded_nominal_document_truths(output)

    assert any(
        relation.direct_object == "access_pass"
        and relation.operative_actor == actor
        for relation in source_relations
    )
    assert any(
        relation.direct_object == "access_pass"
        and relation.operative_actor == actor
        for relation in output_relations
    )


@pytest.mark.parametrize(
    ("source", "supported", "changed"),
    (
        (
            "Пропуск может быть отозван судом За Права.",
            "Отзыв пропуска судом За Права",
            "Отзыв пропуска судом За Обязанности",
        ),
        (
            "Пропуск может быть отозван судом При Правительстве.",
            "Отзыв пропуска судом При Правительстве",
            "Отзыв пропуска судом При Парламенте",
        ),
    ),
    ids=("za-rights", "pri-government"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_unquoted_connector_actor_identity_is_preserved_and_compared_exactly(
    source: str,
    supported: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
    _assert_rejected(validate, source, changed)


def test_v3_additional_penalty_is_not_part_of_passive_court_identity() -> None:
    source = (
        "Пропуск может быть аннулирован судом в качестве дополнительной "
        "меры наказания."
    )
    output = "Аннулирование пропуска судом как дополнительная мера наказания"

    source_relations = _extract_relation_truths(source)
    output_relations = _extract_bounded_nominal_document_truths(output)

    assert any(
        relation.direct_object == "access_pass"
        and relation.operative_actor == "суд"
        for relation in source_relations
    )
    assert any(
        relation.direct_object == "access_pass"
        and relation.operative_actor == "суд"
        for relation in output_relations
    )


SF_PASS_ISSUE_SOURCE = (
    "Каждый государственный служащий может получить пропуск от "
    "руководителя или заместителя руководителя государственной "
    "организации. Руководитель или его заместитель вправе выдавать "
    "пропуск только к территории своей организации."
)


@pytest.mark.parametrize(
    "supported",
    (
        "выдача пропуска государственному служащему",
        "выдача допуска госслужащему заместителем руководителя",
    ),
    ids=("source-recipient", "source-management-role"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_sf_pass_issue_source_faithful_replacements_remain_supported(
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(SF_PASS_ISSUE_SOURCE, supported)


@pytest.mark.parametrize(
    "unsafe",
    (
        "оформление пропуска сотруднику своей госструктуры",
        "выдача допуска госслужащему заместителем начальника",
    ),
    ids=("invented-recipient-ownership", "changed-management-role"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_sf_pass_issue_cannot_invent_recipient_or_management_identity(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, SF_PASS_ISSUE_SOURCE, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_additional_penalty_keeps_exact_named_court(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом Альфа в качестве "
        "дополнительной меры наказания."
    )
    validate(
        source,
        "Аннулирование пропуска судом Альфа как дополнительная мера "
        "наказания",
    )
    _assert_rejected(
        validate,
        source,
        "Аннулирование пропуска судом Бета как дополнительная мера "
        "наказания",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_alternative_actor_leaf_plain_topical_control(
    validate: RelationValidator,
) -> None:
    source = "Пропуск отозван судом или прокуратурой."
    validate(source, "Отзыв пропуска судом")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_alternative_actor_leaf_does_not_prove_a_factual_nominal(
    validate: RelationValidator,
) -> None:
    source = "Пропуск отозван судом или прокуратурой."
    _assert_rejected(validate, source, "Факт: отзыв пропуска судом")


@pytest.mark.parametrize(
    "faceted",
    (
        "Отзыв пропуска судом за убийство",
        "Отзыв пропуска судом с разрешения суда Альфа",
    ),
    ids=("invented-ground", "invented-permission"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_alternative_actor_leaf_exception_cannot_carry_new_facets(
    faceted: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(
        validate,
        "Пропуск отозван судом или прокуратурой.",
        faceted,
    )
