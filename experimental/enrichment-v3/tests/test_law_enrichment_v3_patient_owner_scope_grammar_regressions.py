from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _closed_uk_object_first_owner_surface_contract,
    _relation_anytime_complete_patient_surface,
    _relation_patient_owner_tail_contract,
    _relation_patient_segment_contract,
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
    source_surface = source.rstrip(".")
    validate_synonym_source_evidence(
        source,
        [source_surface, output]
        if output != source_surface
        else [source_surface],
    )


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
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
    _validate_penalty_summary,
    _validate_penalty_other,
)


def test_v3_scope_contract_accepts_only_typed_grammatical_surfaces() -> None:
    weapon = ("scope:weapon",)
    territory = ("scope:territory",)

    for adjective in (
        "оружейная",
        "оружейную",
        "оружейной",
        "оружейные",
        "оружейных",
    ):
        assert _relation_patient_segment_contract(
            adjective,
            zone="pre",
            direct_object="license",
        ) == weapon
    for adjective in (
        "территориальный",
        "территориальную",
        "территориальной",
        "территориальные",
        "территориальных",
    ):
        assert _relation_patient_segment_contract(
            adjective,
            zone="pre",
            direct_object="access_pass",
        ) == territory

    assert _relation_patient_segment_contract(
        "на огнестрельное оружие",
        zone="post",
        direct_object="license",
    ) == weapon
    assert _relation_patient_segment_contract(
        "для огнестрельного оружия",
        zone="post",
        direct_object="license",
    ) == weapon
    assert _relation_patient_segment_contract(
        "на эту территорию",
        zone="post",
        direct_object="access_pass",
    ) == territory
    assert _relation_patient_segment_contract(
        "к этой территории",
        zone="post",
        direct_object="access_pass",
    ) == territory

    for lookalike, expected_scope in (
        ("оружейником", "scope:weapon"),
        ("оружейничество", "scope:weapon"),
        ("территориальность", "scope:territory"),
        ("территориально", "scope:territory"),
    ):
        assert expected_scope not in _relation_patient_segment_contract(
            lookalike,
            zone="pre",
            direct_object=(
                "license"
                if expected_scope == "scope:weapon"
                else "access_pass"
            ),
        )

    assert "scope:weapon" not in _relation_patient_segment_contract(
        "оружейный",
        zone="pre",
        direct_object="access_pass",
    )
    assert "scope:weapon" not in _relation_patient_segment_contract(
        "на оружие",
        zone="post",
        direct_object="access_pass",
    )
    assert "scope:territory" not in _relation_patient_segment_contract(
        "территориальная",
        zone="pre",
        direct_object="license",
    )
    assert "scope:territory" not in _relation_patient_segment_contract(
        "на территорию",
        zone="post",
        direct_object="license",
    )
    assert "scope:territory" not in _relation_patient_segment_contract(
        "на этой территории",
        zone="post",
        direct_object="access_pass",
    )


def test_v3_weapon_scope_keeps_every_extra_and_its_operator() -> None:
    assert _relation_patient_segment_contract(
        "на оружие и боеприпасы",
        zone="post",
        direct_object="license",
    ) == (
        "scope:weapon",
        "scope-op:and",
        "scope:ammunition",
    )
    assert _relation_patient_segment_contract(
        "для оружия либо спецсредств",
        zone="post",
        direct_object="license",
    ) == (
        "scope:weapon",
        "scope-op:or",
        "scope:special-means",
    )
    assert _relation_patient_segment_contract(
        "на оружие и боеприпасов",
        zone="post",
        direct_object="license",
    ) != (
        "scope:weapon",
        "scope-op:and",
        "scope:ammunition",
    )


def test_v3_owner_contract_proves_known_genitive_heads_and_agreement() -> None:
    for owner in (
        "частных владельцев",
        "местных организаций",
        "частных компаний",
        "региональных отделов",
        "федеральных судей",
        "частной компании",
        "федерального суда",
        "владельца Альфа",
        "владельца «Альфа»",
        "гражданки Ивановой",
        "частной гражданки",
        "частных гражданок",
    ):
        assert _closed_uk_object_first_owner_surface_contract(owner)

    for not_genitive in (
        "частные владельцы",
        "федеральные суды",
        "частного компании",
        "частной владельца",
        "гражданки",
        "гражданка Иванова",
        "гражданке Ивановой",
    ):
        assert (
            _closed_uk_object_first_owner_surface_contract(not_genitive)
            is None
        )


def test_v3_unknown_owner_requires_unambiguous_genitive_morphology() -> None:
    assert _closed_uk_object_first_owner_surface_contract(
        "частных архитекторов"
    )
    assert _closed_uk_object_first_owner_surface_contract(
        "старшего уполномоченного"
    )
    assert (
        _closed_uk_object_first_owner_surface_contract(
            "частного архитектора"
        )
        is None
    )
    assert (
        _closed_uk_object_first_owner_surface_contract("музей") is None
    )


def test_v3_owner_tail_never_folds_nominative_plural_to_genitive() -> None:
    genitive = _relation_patient_owner_tail_contract(
        "частных владельцев"
    )
    assert genitive.startswith("owner_surface_exact:")
    assert _relation_patient_owner_tail_contract(
        "частные владельцы"
    ).startswith("unresolved:")


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
def test_v3_weapon_scope_equivalence_and_extra_scope_are_fail_closed(
    validate: RelationValidator,
) -> None:
    source = (
        "Оружейная лицензия была аннулирована прокуратурой."
    )
    validate(
        source,
        "Прокуратура аннулировала лицензию на оружие",
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "Прокуратура аннулировала лицензию "
            "на оружие и боеприпасы",
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
def test_v3_weapon_authorization_keeps_the_same_bounded_scope(
    validate: RelationValidator,
) -> None:
    source = "Оружейное разрешение было аннулировано прокуратурой."
    validate(
        source,
        "Прокуратура аннулировала разрешение на оружие",
    )
    validate(
        source,
        "Прокуратура аннулировала разрешение оружейное",
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "Прокуратура аннулировала разрешение оружейной",
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
def test_v3_territorial_scope_equivalence_rejects_noun_lookalike(
    validate: RelationValidator,
) -> None:
    source = (
        "Территориальный пропуск был аннулирован прокуратурой."
    )
    validate(
        source,
        "Прокуратура аннулировала пропуск на территорию",
    )
    validate(
        source,
        "Прокуратура аннулировала пропуск территориальный",
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "Прокуратура аннулировала пропуск территориальность",
        )
    with pytest.raises(ValueError):
        validate(
            source,
            "Прокуратура аннулировала пропуск территориальной",
        )


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Оружейный пропуск был аннулирован прокуратурой.",
            "Прокуратура аннулировала пропуск на оружие",
        ),
        (
            "Территориальная лицензия была аннулирована прокуратурой.",
            "Прокуратура аннулировала лицензию на территорию",
        ),
        (
            "Прокуратура аннулировала пропуск на этой территории.",
            "Прокуратура аннулировала территориальный пропуск",
        ),
    ),
    ids=("weapon-pass", "territorial-license", "action-location"),
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
def test_v3_scope_equivalence_is_owned_by_the_exact_document_family(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
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
def test_v3_plural_genitive_owner_survives_voice_but_not_case_change(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензии частных владельцев были аннулированы прокуратурой."
    )
    validate(
        source,
        "Прокуратура аннулировала лицензии частных владельцев",
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "Прокуратура аннулировала лицензии частные владельцы",
        )


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Лицензия гражданки Ивановой была аннулирована прокуратурой.",
            "Прокуратура аннулировала лицензию гражданки Ивановой",
            "Прокуратура аннулировала лицензию гражданка Иванова",
        ),
        (
            "Лицензии частных гражданок были аннулированы прокуратурой.",
            "Прокуратура аннулировала лицензии частных гражданок",
            "Прокуратура аннулировала лицензии частным гражданкам",
        ),
    ),
    ids=("feminine-singular", "feminine-plural"),
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
def test_v3_feminine_citizen_owner_keeps_genitive_case_and_number(
    source: str,
    faithful: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


def test_v3_anytime_patient_scope_consumes_only_exact_grammar() -> None:
    assert _relation_anytime_complete_patient_surface(
        "лицензия на огнестрельное оружие",
        direct_object="license",
    )
    assert _relation_anytime_complete_patient_surface(
        "лицензия для оружия и боеприпасов",
        direct_object="license",
    )
    for lookalike in (
        "лицензия на территориальность",
        "лицензия для оружийника",
        "лицензия на оружище",
    ):
        assert not _relation_anytime_complete_patient_surface(
            lookalike,
            direct_object="license",
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
def test_v3_closed_list_never_masks_scope_lookalike_as_weapon(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензию на оружие правоохранители обязаны аннулировать и "
        "изъять при задержании только в случае нарушения любой из "
        "статей: 6.1.1; 6.2; 6.3; 7.1 УК."
    )
    faithful = (
        "лицензию на оружие правоохранители обязаны аннулировать и "
        "изъять при задержании за любую статью из списка УК"
    )
    validate(source, faithful)
    for unsafe in (
        faithful.replace("на оружие", "оружейником"),
        faithful.replace("на оружие", "на оружие оружейником"),
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
def test_v3_closed_list_postpositive_weapon_scope_uses_actual_number(
    validate: RelationValidator,
) -> None:
    source = (
        "Оружейные лицензии правоохранители обязаны аннулировать и "
        "изъять при задержании только в случае нарушения любой из "
        "статей: 6.1.1; 6.2; 6.3; 7.1 УК."
    )
    faithful = (
        "Лицензии оружейные правоохранители обязаны аннулировать и "
        "изъять при задержании за любую статью из списка УК"
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(
            source,
            faithful.replace("оружейные", "оружейной"),
        )
