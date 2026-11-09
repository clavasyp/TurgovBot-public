from __future__ import annotations

import pytest

from tests.test_law_enrichment_v3_closed_uk_list_regressions import (
    ALL_OUTPUT_PATHS,
    RelationValidator,
)


def _assert_non_verbatim(source: str, output: str) -> None:
    assert " ".join(source.rstrip(".").casefold().split()) != " ".join(
        output.rstrip(".").casefold().split()
    )


_ACTIVE_WORD_ORDER_CASES = (
    (
        "Лицензию на оружие аннулировала федеральная прокуратура.",
        "Федеральная прокуратура аннулировала лицензию на оружие",
        "Федеральную прокуратуру аннулировала лицензию на оружие",
        "svo",
    ),
    (
        "Федеральная прокуратура аннулировала лицензию на оружие.",
        "Лицензию на оружие аннулировала федеральная прокуратура",
        "Лицензию на оружие аннулировала федеральную прокуратуру",
        "ovs",
    ),
    (
        "Лицензию на оружие аннулировала федеральная прокуратура.",
        "Федеральная прокуратура лицензию на оружие аннулировала",
        "Федеральную прокуратуру лицензию на оружие аннулировала",
        "sov",
    ),
    (
        "Лицензию на оружие аннулировала федеральная прокуратура.",
        "Аннулировала федеральная прокуратура лицензию на оружие",
        "Аннулировала федеральную прокуратуру лицензию на оружие",
        "vso",
    ),
    (
        "Федеральная прокуратура аннулировала лицензию на оружие.",
        "Аннулировала лицензию на оружие федеральная прокуратура",
        "Аннулировала лицензию на оружие федеральную прокуратуру",
        "vos",
    ),
    (
        "Федеральная прокуратура аннулировала лицензию на оружие.",
        "Лицензию на оружие федеральная прокуратура аннулировала",
        "Лицензию на оружие федеральную прокуратуру аннулировала",
        "osv",
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe", "case_id"),
    _ACTIVE_WORD_ORDER_CASES,
    ids=tuple(case[3] for case in _ACTIVE_WORD_ORDER_CASES),
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
def test_v3_known_active_actor_is_nominative_in_every_word_order(
    source: str,
    faithful: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    _assert_non_verbatim(source, faithful)
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_INVARIANT_AND_PRESENT_MODIFIER_CASES = (
    (
        "Лицензию на оружие аннулирует федеральная прокуратура.",
        "Федеральная прокуратура аннулирует лицензию на оружие",
        "Федеральный прокуратура аннулирует лицензию на оружие",
        "present",
    ),
    (
        "Лицензию на оружие вправе аннулировать федеральная прокуратура.",
        "Федеральная прокуратура вправе аннулировать лицензию на оружие",
        "Федеральный прокуратура вправе аннулировать лицензию на оружие",
        "invariant-modal",
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe", "case_id"),
    _INVARIANT_AND_PRESENT_MODIFIER_CASES,
    ids=tuple(case[3] for case in _INVARIANT_AND_PRESENT_MODIFIER_CASES),
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
def test_v3_actor_modifier_agrees_even_without_gendered_governor(
    source: str,
    faithful: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    _assert_non_verbatim(source, faithful)
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_ACTOR_GROUP_CASES = (
    (
        "Лицензию на оружие аннулировали федеральная прокуратура и "
        "региональный суд.",
        "Федеральная прокуратура и региональный суд аннулировали "
        "лицензию на оружие",
        "Федеральный прокуратура и региональный суд аннулировали "
        "лицензию на оружие",
        "joint",
    ),
    (
        "Лицензию на оружие вправе аннулировать федеральная прокуратура "
        "или региональный суд.",
        "Федеральная прокуратура или региональный суд вправе аннулировать "
        "лицензию на оружие",
        "Федеральная прокуратура или региональная суд вправе аннулировать "
        "лицензию на оружие",
        "alternative",
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe", "case_id"),
    _ACTOR_GROUP_CASES,
    ids=tuple(case[3] for case in _ACTOR_GROUP_CASES),
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
def test_v3_every_known_actor_group_member_has_own_agreement(
    source: str,
    faithful: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    _assert_non_verbatim(source, faithful)
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
def test_v3_subordinate_modal_never_governs_main_actor(
    validate: RelationValidator,
) -> None:
    source = (
        "Если суд должен уведомить владельца, федеральная прокуратура "
        "аннулировала лицензию на оружие."
    )
    faithful = (
        "Федеральная прокуратура, если суд должен уведомить владельца, "
        "аннулировала лицензию на оружие"
    )
    unsafe = faithful.replace("аннулировала", "аннулировал", 1)
    _assert_non_verbatim(source, faithful)
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_PASSIVE_AND_NOMINAL_PERFORMER_CASES = (
    (
        "Оружейная лицензия аннулирована федеральной прокуратурой.",
        "Лицензия на оружие аннулирована федеральной прокуратурой",
        "Лицензия на оружие аннулирована федеральная прокуратура",
        "passive-nominative-performer",
    ),
    (
        "Оружейная лицензия аннулирована федеральной прокуратурой.",
        "Лицензия на оружие аннулирована федеральной прокуратурой",
        "Лицензия на оружие аннулирована федеральным прокуратурой",
        "passive-modifier",
    ),
    (
        "Обязательно аннулирование оружейной лицензии федеральной "
        "прокуратурой.",
        "Обязательно аннулирование лицензии на оружие федеральной "
        "прокуратурой",
        "Обязательно аннулирование лицензии на оружие федеральная "
        "прокуратура",
        "mandatory-nominal",
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe", "case_id"),
    _PASSIVE_AND_NOMINAL_PERFORMER_CASES,
    ids=tuple(case[3] for case in _PASSIVE_AND_NOMINAL_PERFORMER_CASES),
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
def test_v3_passive_and_mandatory_nominal_performer_is_instrumental(
    source: str,
    faithful: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    _assert_non_verbatim(source, faithful)
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_BUREAU_CASES = (
    (
        "Лицензию на оружие аннулировало федеральное бюро.",
        "Федеральное бюро аннулировало лицензию на оружие",
        "Федеральные бюро аннулировало лицензию на оружие",
        "modified-singular",
    ),
    (
        "Лицензию на оружие аннулировали федеральные бюро.",
        "Федеральные бюро аннулировали лицензию на оружие",
        "Федеральное бюро аннулировали лицензию на оружие",
        "modified-plural",
    ),
    (
        "Лицензию на оружие аннулировало бюро.",
        "Бюро аннулировало лицензию на оружие",
        "Бюро аннулировал лицензию на оружие",
        "bare-neuter",
    ),
    (
        "Лицензию на оружие аннулировали бюро.",
        "Бюро аннулировали лицензию на оружие",
        "Бюро аннулировала лицензию на оружие",
        "bare-plural",
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe", "case_id"),
    _BUREAU_CASES,
    ids=tuple(case[3] for case in _BUREAU_CASES),
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
def test_v3_bureau_uses_modifier_number_and_bare_number_neutral_policy(
    source: str,
    faithful: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    _assert_non_verbatim(source, faithful)
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_OPEN_COMPATIBILITY_CASES = (
    (
        "Лицензию на оружие аннулировал FIB.",
        "FIB аннулировал лицензию на оружие",
        "ascii-masculine",
    ),
    (
        "Лицензию на оружие аннулировала FIB.",
        "FIB аннулировала лицензию на оружие",
        "ascii-feminine",
    ),
    (
        "Лицензию на оружие аннулировал неизвестный специалист.",
        "Неизвестный специалист аннулировал лицензию на оружие",
        "open-name",
    ),
    (
        "Лицензию на оружие аннулировал суд совместно с федеральной "
        "прокуратурой.",
        "Суд совместно с федеральной прокуратурой аннулировал лицензию "
        "на оружие",
        "instrumental-joint-partner",
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "case_id"),
    _OPEN_COMPATIBILITY_CASES,
    ids=tuple(case[2] for case in _OPEN_COMPATIBILITY_CASES),
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
def test_v3_open_ascii_and_instrumental_partner_policy_is_preserved(
    source: str,
    faithful: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    _assert_non_verbatim(source, faithful)
    validate(source, faithful)
