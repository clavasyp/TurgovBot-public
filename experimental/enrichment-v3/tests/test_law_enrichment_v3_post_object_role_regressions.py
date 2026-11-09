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


ACTOR_SOURCE = (
    "Глава и заместитель главы исполнительной власти вправе "
    "аннулировать пропуск, если лицо нарушает установленный режим."
)
ACQUISITION_SOURCE = (
    "Каждый государственный служащий может получить пропуск от "
    "руководителя или заместителя руководителя государственной "
    "организации. Руководитель или его заместитель вправе выдавать "
    "пропуск только к территории своей организации."
)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_post_object_actor_expression_is_source_bound(
    validate: RelationValidator,
) -> None:
    validate(
        ACTOR_SOURCE,
        "аннулирование пропуска главой или заместителем главы за "
        "нарушение режима",
    )
    with pytest.raises(ValueError):
        validate(
            ACTOR_SOURCE,
            "аннулирование пропуска главой или директором за нарушение "
            "режима",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    "source",
    (
        (
            "Глава Альфа и заместитель главы Бета исполнительной власти "
            "вправе аннулировать пропуск, если лицо нарушает установленный "
            "режим."
        ),
        (
            "Глава и заместитель главы исполнительной власти «Альфа» вправе "
            "аннулировать пропуск, если лицо нарушает установленный режим."
        ),
    ),
    ids=("different-identities", "named-shared-scope"),
)
def test_v3_generic_head_group_cannot_drop_source_actor_identity(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "аннулирование пропуска главой или заместителем главы за "
            "нарушение режима",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_unknown_instrumental_actor_cannot_disappear_into_patient(
    validate: RelationValidator,
) -> None:
    source = "Суд вправе аннулировать пропуск."
    with pytest.raises(ValueError):
        validate(source, "аннулирование пропуска неизвестным инспектором")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_acquisition_recipient_and_issuer_are_bound_together(
    validate: RelationValidator,
) -> None:
    validate(
        ACQUISITION_SOURCE,
        "выдача пропуска государственному служащему",
    )
    validate(
        ACQUISITION_SOURCE,
        "выдача допуска госслужащему заместителем руководителя",
    )
    with pytest.raises(ValueError):
        validate(
            ACQUISITION_SOURCE,
            "выдача пропуска муниципальному служащему",
        )
    with pytest.raises(ValueError):
        validate(
            ACQUISITION_SOURCE,
            "выдача допуска госслужащему директором",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_acquisition_recipient_keeps_complete_expression(
    validate: RelationValidator,
) -> None:
    source = (
        "Государственный служащий или гражданин может получить пропуск от "
        "руководителя. Руководитель вправе выдавать пропуск."
    )
    validate(
        source,
        "выдача пропуска гражданину или государственному служащему",
    )

    scoped_source = (
        "Государственный служащий частной организации может получить "
        "пропуск от руководителя. Руководитель вправе выдавать пропуск."
    )
    validate(
        scoped_source,
        "выдача пропуска государственному служащему частной организации",
    )
    with pytest.raises(ValueError):
        validate(
            scoped_source,
            "выдача пропуска государственному служащему государственной "
            "организации",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_acquisition_evidence_survives_issuer_document_scope(
    validate: RelationValidator,
) -> None:
    source = (
        "Каждый государственный служащий может получить пропуск от "
        "руководителя или заместителей руководителя государственной "
        "организации. Руководитель или его заместитель вправе выдавать "
        "пропуск только к той территории, за которой закреплена его "
        "организация."
    )
    validate(source, "выдача пропуска государственному служащему")
    validate(
        source,
        "выдача пропуска госслужащему заместителем руководителя",
    )
    with pytest.raises(ValueError):
        validate(source, "выдача пропуска гражданину руководителем")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_oblique_post_object_role_is_not_an_actor(
    validate: RelationValidator,
) -> None:
    source = (
        "В период вакантности должности руководителя государственной "
        "организации все пропуски прекращают свое действие."
    )
    validate(source, "прекращение пропусков при вакантном руководителе")
    with pytest.raises(ValueError):
        validate(
            "Пропуск отозван судом.",
            "отзыв пропуска при вакантном руководителе",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_actorless_coordinated_passive_actions_share_patient(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия должна быть аннулирована и изъята при задержании "
        "за нарушение закона."
    )
    validate(
        source,
        "аннулирование и изъятие лицензии при задержании за нарушение закона",
    )
    with pytest.raises(ValueError):
        validate(
            "Лицензия должна быть аннулирована при задержании.",
            "изъятие лицензии при задержании",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_acquisition_recipient_cannot_pool_with_unrelated_issuer(
    validate: RelationValidator,
) -> None:
    source = (
        "Каждый государственный служащий может получить пропуск от суда. "
        "Руководитель вправе выдавать пропуск."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "выдача пропуска государственному служащему руководителем",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            ACQUISITION_SOURCE,
            "выдача пропуска госслужащему частной организации",
        ),
        (
            ACQUISITION_SOURCE,
            "выдача пропуска госслужащему или гражданину",
        ),
        (
            (
                "Каждый государственный служащий может получить пропуск от "
                "заместителя руководителя. Руководитель вправе выдавать "
                "пропуск."
            ),
            "выдача пропуска государственному служащему руководителем",
        ),
        (
            (
                "Каждый государственный служащий может получить пропуск от "
                "верховного суда. Суд вправе выдавать пропуск."
            ),
            "выдача пропуска государственному служащему судом",
        ),
        (
            (
                "Каждый государственный служащий может получить пропуск от "
                "суда совместно с прокуратурой. Суд вправе выдавать пропуск."
            ),
            "выдача пропуска государственному служащему судом",
        ),
        (
            (
                "Каждый государственный служащий может получить пропуск от "
                "руководителя если утверждён список. Руководитель вправе "
                "выдавать пропуск."
            ),
            "выдача пропуска государственному служащему руководителем",
        ),
        (
            (
                "Каждый соответствующий государственный служащий может "
                "получить пропуск от руководителя. Руководитель вправе "
                "выдавать пропуск."
            ),
            "выдача пропуска государственному служащему руководителем",
        ),
    ),
    ids=(
        "recipient-owner",
        "recipient-alternative",
        "leader-vs-deputy",
        "court-vs-supreme-court",
        "joint-origin-is-atomic",
        "conditional-origin",
        "referential-recipient-qualifier",
    ),
)
def test_v3_acquisition_bridge_preserves_complete_roles(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_acquisition_only_source_fails_closed_until_projected(
    validate: RelationValidator,
) -> None:
    source = (
        "Каждый государственный служащий может получить пропуск от "
        "руководителя."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "выдача пропуска государственному служащему руководителем",
        )


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Пропуск №1 может быть аннулирован судом.",
            "аннулирование пропуска №1 судом",
        ),
        (
            "Пропуск «Главой или заместителем» может быть аннулирован "
            "судом.",
            "аннулирование пропуска «Главой или заместителем» судом",
        ),
        (
            "Пропуск частной организации может быть аннулирован судом.",
            "аннулирование пропуска частной организации судом",
        ),
        (
            "Пропуск второй категории может быть аннулирован судом.",
            "аннулирование пропуска второй категории судом",
        ),
    ),
    ids=("number", "quoted-role-words", "genitive-owner", "category"),
)
@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_patient_identity_before_role_is_preserved(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_role_parser_does_not_erase_changed_patient_identity(
    validate: RelationValidator,
) -> None:
    source = "Пропуск второй категории может быть аннулирован судом."
    with pytest.raises(ValueError):
        validate(source, "аннулирование пропуска первой категории судом")
    with pytest.raises(ValueError):
        validate(source, "аннулирование пропуска «Главой судом")


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Пропуск аннулирован судом.", "Пропуска аннулированы судом"),
        ("Пропуска аннулированы судом.", "Пропуск аннулирован судом"),
        (
            "Пропуск аннулирован судом.",
            "Территориальный пропуск аннулирован судом",
        ),
        (
            "Территориальный пропуск аннулирован судом.",
            "Пропуск аннулирован судом",
        ),
        ("Пропуск №1 аннулирован судом.", "Пропуск №2 аннулирован судом"),
        (
            "Лицензия адвоката Иванова аннулирована судом.",
            "Лицензия адвоката Петрова аннулирована судом",
        ),
        (
            "Лицензия «Альфа» аннулирована судом.",
            "Лицензия «Бета» аннулирована судом",
        ),
        (
            "Сертификат адвоката Иванова отозван судом.",
            "Сертификат адвоката Петрова отозван судом",
        ),
        ("Ордер CS отозван судом.", "Ордер CT отозван судом"),
        ("Паспорт №1 выдан судом.", "Паспорт №2 выдан судом"),
    ),
    ids=(
        "invent-plural",
        "drop-plural",
        "invent-scope",
        "drop-scope",
        "change-number",
        "license-owner",
        "license-name",
        "certificate-owner",
        "warrant-code",
        "passport-number",
    ),
)
@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_finite_document_patient_identity_is_exact(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Лицензия адвоката Иванова аннулирована судом.",
            "аннулирование лицензии адвоката Петрова судом",
        ),
        (
            "Сертификат «Альфа» отозван судом.",
            "отзыв сертификата «Бета» судом",
        ),
        ("Ордер №1 отозван судом.", "отзыв ордера №2 судом"),
    ),
    ids=("license-owner", "certificate-name", "warrant-number"),
)
@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_finite_to_nominal_keeps_document_identity(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Лицензия адвоката Иванова аннулирована судом.",
            "Суд аннулировал лицензию адвоката Иванова",
        ),
        (
            "Сертификат «Альфа: Один» отозван судом.",
            "Суд отозвал сертификат «Альфа: Один»",
        ),
        ("Ордер №1 выдан судом.", "Суд выдал ордер №1"),
        ("Паспорт №1 выдан судом.", "Суд выдал паспорт №1"),
    ),
    ids=("license-owner", "certificate-exact-name", "warrant", "passport"),
)
@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_exact_document_patient_survives_voice_change(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_generic_plural_document_rule_keeps_topical_singular(
    validate: RelationValidator,
) -> None:
    source = (
        "Полномочия и обязанности мэра:\n"
        "Выдавать лицензии коллекционера."
    )
    validate(source, "мэр выдаёт лицензию коллекционера")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_coordinated_passive_never_crosses_distinct_patients(
    validate: RelationValidator,
) -> None:
    source = "Лицензия аннулирована и изъят пропуск."
    with pytest.raises(ValueError):
        validate(source, "изъятие лицензии")
    validate(source, "аннулирование лицензии")
    validate(source, "изъятие пропуска")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_coordinated_passive_keeps_true_shared_patient(
    validate: RelationValidator,
) -> None:
    source = "Лицензия должна быть аннулирована и изъята судом."
    validate(source, "лицензия должна быть изъята судом")


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Организация «Альфа: Один» отозвала лицензию.",
            "Организация «Альфа Один» отозвала лицензию",
        ),
        (
            "Суд выдал пропуск гражданину «Альфа, Один».",
            "Суд выдал пропуск гражданину «Альфа Один»",
        ),
        (
            "Пропуск «Рога и Копыта» аннулирован судом.",
            "аннулирование пропуска «Рога Копыта» судом",
        ),
        (
            "Акт «Музей: Альфа» является основанием для "
            "аннулирования сертификата.",
            "Акт «Музей Альфа» — основание для аннулирования "
            "сертификата",
        ),
    ),
    ids=("actor", "recipient", "patient-glue", "explicit-ground"),
)
@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_exact_identity_keeps_internal_punctuation_and_glue(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source_role", "unsafe_role"),
    (
        ("при поддержке FIB", "при поддержке USSS"),
        ("по просьбе FIB", "по просьбе USSS"),
        ("через представителя прокуратуры", "через представителя FIB"),
        ("в интересах FIB", "в интересах USSS"),
        ("в Верховном суде", "в окружном суде"),
        ("при вакантном руководителе", "при действующем руководителе"),
    ),
    ids=("support", "request", "representative", "interest", "court", "manager"),
)
@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_post_action_oblique_role_is_source_bound(
    source_role: str,
    unsafe_role: str,
    validate: RelationValidator,
) -> None:
    source = f"Лицензия отозвана {source_role}."
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, f"Лицензия отозвана {unsafe_role}")
    with pytest.raises(ValueError):
        validate("Лицензия отозвана.", f"отзыв лицензии {source_role}")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Лицензия, ранее выданная адвокату Иванову, отозвана судом.",
            "Лицензия, ранее выданная адвокату Петрову, отозвана судом",
        ),
        (
            "Ордер, подписанный судом, отозван прокуратурой.",
            "Ордер, полученный FIB, отозван прокуратурой",
        ),
    ),
    ids=("recipient", "action-and-issuer"),
)
def test_v3_document_history_cannot_launder_role_details(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_document_history_is_exact_or_safely_omitted(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия, ранее выданная адвокату Иванову, отозвана судом."
    )
    validate(
        source,
        "Лицензия, ранее выданная адвокату Иванову, отозвана судом",
    )
    validate(source, "Лицензия отозвана судом")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Лицензия адвоката Иванова, серия А, №1 отозвана судом.",
            "Лицензия адвоката Иванова, серия Б, №2 отозвана судом",
        ),
        (
            "Сертификат, именуемый «Альфа», отозван судом.",
            "Сертификат, именуемый «Бета», отозван судом",
        ),
        (
            "Сертификат адвоката Иванова отозван судом.",
            "Сертификат адвоката Ивановой отозван судом",
        ),
        (
            "Лицензия владельца Орлова отозвана судом.",
            "Лицензия владельца Орловой отозвана судом",
        ),
        (
            "Лицензия организации Альфа отозвана судом.",
            "Лицензия организации Альфы отозвана судом",
        ),
    ),
    ids=(
        "comma-series-number",
        "comma-exact-name",
        "lawyer-name",
        "owner-name",
        "organization-name",
    ),
)
def test_v3_document_patient_keeps_apposition_and_proper_identity(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "unsafe", "supported"),
    (
        (
            "Лицензию №1 отозвал суд.",
            "Пропуск №2 отозвал суд",
            "Суд отозвал лицензию №1",
        ),
        (
            "Лицензию №1 суд отозвал.",
            "Пропуск №2 суд отозвал",
            "Суд отозвал лицензию №1",
        ),
        (
            "Ордер №1 выдал суд.",
            "Паспорт №2 выдал суд",
            "Суд выдал ордер №1",
        ),
    ),
    ids=("ovs-revocation", "osv-revocation", "ovs-issue"),
)
def test_v3_active_object_first_keeps_complete_patient(
    source: str,
    unsafe: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд выдал ордер, номер 1, на задержание Иванова.",
            "Суд выдал ордер, номер 2, на задержание Петрова",
        ),
        (
            "Суд выдал ордер на обыск у Иванова.",
            "Суд выдал ордер на обыск у Петрова",
        ),
        (
            "Суд выдал ордер на обыск через представителя Иванова.",
            "Суд выдал ордер на обыск через представителя Петрова",
        ),
    ),
    ids=("comma-number-target", "u-target", "representative-target"),
)
def test_v3_warrant_keeps_complete_bounded_purpose(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_coordinated_passive_rebinds_explicit_second_patient(
    validate: RelationValidator,
) -> None:
    shared = "Лицензия №1 аннулирована и изъята судом."
    validate(shared, "Лицензия №1 изъята судом")

    distinct = (
        "Лицензия №1 аннулирована и изъята лицензия №2 судом."
    )
    with pytest.raises(ValueError):
        validate(distinct, "Лицензия №1 изъята судом")
    validate(distinct, "Лицензия №2 изъята судом")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_mixed_patient_alternative_never_becomes_definite(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия №1 аннулирована либо изъят пропуск №2 судом."
    )
    with pytest.raises(ValueError):
        validate(source, "Пропуск №2 изъят судом")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_actorless_passive_cannot_invent_open_actor(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Лицензия отозвана.",
            "Департамент юстиции отозвал лицензию",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "supported", "unsafe"),
    (
        (
            "Лицензия отозвана Департаментом юстиции.",
            "Департамент юстиции отозвал лицензию",
            "Департамент финансов отозвал лицензию",
        ),
        (
            "Лицензия отозвана Департаментом «Альфа».",
            "Департамент «Альфа» отозвал лицензию",
            "Комитет «Бета» отозвал лицензию",
        ),
        (
            "Лицензия отозвана Комитетом Альфа.",
            "Комитет Альфа отозвал лицензию",
            "Комитет Бета отозвал лицензию",
        ),
    ),
    ids=("department", "exact-name", "committee"),
)
def test_v3_open_passive_actor_is_complete_and_exact(
    source: str,
    supported: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Лицензия отозвана при поддержке Департамента юстиции.",
            "Лицензия отозвана при поддержке Департамента финансов",
        ),
        (
            "Лицензия отозвана при поддержке dea alpha.",
            "Лицензия отозвана при поддержке dea beta",
        ),
        (
            "Лицензия отозвана по просьбе FIB.",
            "Департамент юстиции отозвал лицензию",
        ),
        (
            "Лицензия отозвана при поддержке суда Альфа.",
            "Комитет Бета отозвал лицензию",
        ),
    ),
    ids=("department-role", "lower-ascii", "role-promotion", "other-promotion"),
)
def test_v3_open_oblique_role_is_exact_and_never_promoted(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_oblique_role_is_position_independent(
    validate: RelationValidator,
) -> None:
    canonical = "Суд отозвал лицензию по просьбе FIB"
    for source in (
        "По просьбе FIB суд отозвал лицензию.",
        "Суд по просьбе FIB отозвал лицензию.",
        "Суд отозвал по просьбе FIB лицензию.",
        "Суд отозвал лицензию по просьбе FIB.",
    ):
        validate(source, canonical)

    with pytest.raises(ValueError):
        validate("Суд отозвал лицензию.", canonical)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    "source",
    (
        "Лицензия отозвана судом при поддержке FIB.",
        "Лицензия отозвана при поддержке FIB судом.",
        "Лицензия отозвана при поддержке FIB, судом.",
        "Лицензия отозвана при поддержке FIB именно судом.",
    ),
)
def test_v3_true_passive_actor_is_separate_from_oblique_role(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, "Суд отозвал лицензию при поддержке FIB")
    with pytest.raises(ValueError):
        validate(source, "Суд поддержки FIB отозвал лицензию")
    with pytest.raises(ValueError):
        validate(source, "Прокуратура отозвала лицензию при поддержке FIB")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_dedicated_permission_owns_its_role_once(
    validate: RelationValidator,
) -> None:
    leading = (
        "С разрешения суда Альфа организация может отозвать пропуск."
    )
    post = (
        "Организация может отозвать пропуск с разрешения суда Альфа"
    )
    validate(leading, post)
    validate(post + ".", leading.rstrip("."))
    with pytest.raises(ValueError):
        validate(leading, post.replace("Альфа", "Бета"))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize("wrapper", (("(", ")"), ("[", "]")))
def test_v3_balanced_oblique_role_uses_same_exact_contract(
    wrapper: tuple[str, str],
    validate: RelationValidator,
) -> None:
    opening, closing = wrapper
    source = f"Лицензия отозвана {opening}по просьбе FIB{closing}."
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(
            source,
            f"Лицензия отозвана {opening}по просьбе USSS{closing}",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_oblique_role_keeps_bounded_apposition(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия отозвана при поддержке организации, а именно DEA Alpha."
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, source.replace("DEA Alpha", "DEA Beta").rstrip("."))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_overwindow_oblique_role_fails_closed(
    validate: RelationValidator,
) -> None:
    modifiers = "официальной " * 30
    source = f"Лицензия отозвана при поддержке {modifiers}FIB."
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, source.replace("FIB", "USSS").rstrip("."))
    with pytest.raises(ValueError):
        validate(
            "Лицензия отозвана.",
            source.rstrip("."),
        )


def test_v3_oblique_role_belongs_to_nearest_action() -> None:
    source = (
        "Суд отозвал лицензию при поддержке FIB и затем отозвал пропуск "
        "при поддержке USSS."
    )
    license_output = "Суд отозвал лицензию при поддержке FIB"
    pass_output = "Суд отозвал пропуск при поддержке USSS"
    _validate_title(source, license_output)
    _validate_title(source, pass_output)
    validate_synonym_source_evidence(
        source,
        [license_output, pass_output],
    )
    _validate_full_set(source, license_output)
    _validate_full_set(source, pass_output)
    for validate in OUTPUT_PATHS:
        with pytest.raises(ValueError):
            validate(source, "Суд отозвал лицензию при поддержке USSS")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_leading_oblique_role_preserves_open_actor(
    validate: RelationValidator,
) -> None:
    source = "По просьбе FIB Совет Альфа отозвал лицензию."
    validate(source, "Совет Альфа отозвал лицензию по просьбе FIB")
    with pytest.raises(ValueError):
        validate(source, "Совет Бета отозвал лицензию по просьбе FIB")
    with pytest.raises(ValueError):
        validate(source, "FIB отозвал лицензию")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_explicit_joint_actor_is_not_an_oblique_role(
    validate: RelationValidator,
) -> None:
    passive = "Лицензия отозвана прокуратурой сообща с судом."
    validate(passive, "Прокуратура сообща с судом отозвала лицензию")
    with pytest.raises(ValueError):
        validate(passive, "Прокуратура отозвала лицензию")

    active = "Прокуратура сообща с судом отозвала лицензию."
    validate(active, "Прокуратура отозвала лицензию сообща с судом")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "canonical"),
    (
        (
            "При поддержке FIB лицензия №1 отозвана судом.",
            "Лицензия №1 отозвана судом при поддержке FIB",
        ),
        (
            "По просьбе FIB лицензию №1 отозвал суд.",
            "Суд отозвал лицензию №1 по просьбе FIB",
        ),
    ),
    ids=("passive", "ovs"),
)
def test_v3_leading_oblique_role_preserves_document_patient(
    source: str,
    canonical: str,
    validate: RelationValidator,
) -> None:
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace("№1", "№2"))
    with pytest.raises(ValueError):
        validate(source, canonical.replace(" №1", ""))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_oblique_role_stops_before_dative_recipient(
    validate: RelationValidator,
) -> None:
    source = "Суд выдал лицензию по просьбе FIB заявителю Иванову."
    canonical = "Суд выдал лицензию заявителю Иванову по просьбе FIB"
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace("Иванову", "Петрову"))
    with pytest.raises(ValueError):
        validate(source, canonical.replace("FIB", "USSS"))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_oblique_role_stops_before_non_document_patient(
    validate: RelationValidator,
) -> None:
    source = "Суд задержал по просьбе FIB гражданина Иванова."
    canonical = "Суд задержал гражданина Иванова по просьбе FIB"
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace("FIB", "USSS"))
    with pytest.raises(ValueError):
        validate(source, canonical.replace("Иванова", "Петрова"))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_oblique_role_and_permission_keep_separate_owners(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация может отозвать лицензию по просьбе FIB "
        "с разрешения прокуратуры."
    )
    canonical = (
        "Организация с разрешения прокуратуры может отозвать лицензию "
        "по просьбе FIB"
    )
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace("прокуратуры", "суда"))
    with pytest.raises(ValueError):
        validate(source, canonical.replace("FIB", "USSS"))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_two_oblique_roles_are_disjoint_and_reorderable(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд отозвал лицензию по просьбе FIB при поддержке USSS."
    )
    canonical = (
        "Суд отозвал лицензию при поддержке USSS по просьбе FIB"
    )
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace("USSS", "SANG"))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_oblique_role_does_not_consume_following_condition(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд отозвал лицензию по просьбе FIB при нарушении режима."
    )
    canonical = (
        "При нарушении режима суд отозвал лицензию по просьбе FIB"
    )
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace("режима", "договора"))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_internal_identity_preposition_is_not_a_nested_role(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд отозвал лицензию при поддержке отдела по защите FIB."
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(
            source,
            source.replace("по защите", "по контролю").rstrip("."),
        )


def test_v3_joint_oblique_role_stays_local_before_next_action() -> None:
    source = (
        "Суд отозвал лицензию при поддержке FIB и USSS и затем "
        "выдал пропуск."
    )
    exact_first = "Суд отозвал лицензию при поддержке FIB и USSS"
    unsafe_first = "Суд отозвал лицензию при поддержке FIB"
    _validate_title(source, exact_first)
    with pytest.raises(ValueError):
        _validate_title(source, unsafe_first)
    validate_synonym_source_evidence(
        source,
        [source.rstrip("."), exact_first],
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [source.rstrip("."), unsafe_first],
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_joint_oblique_role_on_second_action_keeps_inherited_actor(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд отозвал лицензию, и затем при поддержке FIB и USSS "
        "выдал пропуск."
    )
    canonical = (
        "Суд отозвал лицензию, и затем выдал пропуск "
        "при поддержке FIB и USSS"
    )
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace(" и USSS", ""))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_mandatory_leading_oblique_role_uses_shared_condition_path(
    validate: RelationValidator,
) -> None:
    source = "При поддержке FIB суд должен отозвать лицензию №1."
    canonical = "Суд должен отозвать лицензию №1 при поддержке FIB"
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace("FIB", "USSS"))
    with pytest.raises(ValueError):
        validate(source, canonical.replace("№1", "№2"))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    "source",
    (
        "При нарушении режима по просьбе FIB суд отозвал лицензию.",
        "При нарушении режима суд по просьбе FIB отозвал лицензию.",
        "Суд по просьбе FIB при нарушении режима отозвал лицензию.",
        "Суд при нарушении режима по просьбе FIB отозвал лицензию.",
        "Суд отозвал лицензию при нарушении режима по просьбе FIB.",
        "Суд отозвал лицензию по просьбе FIB при нарушении режима.",
    ),
)
def test_v3_condition_and_oblique_role_are_position_independent(
    source: str,
    validate: RelationValidator,
) -> None:
    canonical = (
        "При нарушении режима суд отозвал лицензию по просьбе FIB"
    )
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace("FIB", "USSS"))
    with pytest.raises(ValueError):
        validate(source, canonical.replace("режима", "лицензии"))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    "source, mutation",
    (
        (
            "Суд При Нарушении Альфа отозвал лицензию.",
            "Суд При Нарушении Бета отозвал лицензию",
        ),
        (
            "Суд «При Нарушении» отозвал лицензию.",
            "Суд «При Исполнении» отозвал лицензию",
        ),
    ),
)
def test_v3_condition_words_inside_actor_identity_remain_identity(
    source: str,
    mutation: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, mutation)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_mandatory_generic_patient_keeps_exact_person_identity(
    validate: RelationValidator,
) -> None:
    source = "Суд обязан задержать гражданина Иванова."
    canonical = source.rstrip(".")
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, canonical.replace("Иванова", "Петрова"))
    with pytest.raises(ValueError):
        validate(source, canonical.replace(" Иванова", ""))


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    "source, canonical, mutation",
    (
        (
            "Правительство обязано ликвидировать организацию Альфа.",
            "Правительство обязано ликвидировать компанию Альфа",
            "Правительство обязано ликвидировать компанию Бета",
        ),
        (
            "Правительство обязано ликвидировать организацию «Альфа».",
            "Правительство обязано ликвидировать компанию «Альфа»",
            "Правительство обязано ликвидировать компанию «Бета»",
        ),
        (
            "Правительство обязано ликвидировать банк Альфа.",
            "Правительство обязано ликвидировать банк Альфа",
            "Правительство обязано ликвидировать банк Бета",
        ),
    ),
)
def test_v3_mandatory_generic_patient_keeps_organization_identity(
    source: str,
    canonical: str,
    mutation: str,
    validate: RelationValidator,
) -> None:
    validate(source, canonical)
    with pytest.raises(ValueError):
        validate(source, mutation)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Допуск оформляется письменным приказом.",
            "Письменный приказ оформляет допуск",
        ),
        (
            "Лицензия оформлена электронным способом.",
            "Электронный способ оформил лицензию",
        ),
    ),
    ids=("qualified-document-form", "manner-adjunct"),
)
def test_v3_qualified_instrumental_non_actor_cannot_become_actor(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_open_human_instrumental_actor_remains_supported(
    validate: RelationValidator,
) -> None:
    validate(
        "Допуск оформляется неизвестным сотрудником.",
        "Неизвестный сотрудник оформляет допуск",
    )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_warrant_osv_actor_cannot_be_reused_as_purpose_owner(
    validate: RelationValidator,
) -> None:
    source = "Ордер на обыск дома прокурор оформляет."
    with pytest.raises(ValueError):
        validate(
            source,
            "Прокурор оформляет ордер на обыск дома прокурора",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_warrant_osv_actor_boundary_remains_reorderable(
    validate: RelationValidator,
) -> None:
    validate(
        "Ордер на обыск дома прокурор оформляет.",
        "Прокурор оформляет ордер на обыск дома",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_warrant_ovs_genitive_target_defers_to_postposed_actor(
    validate: RelationValidator,
) -> None:
    source = (
        "Ордер на задержание верховного судьи выписывает губернатор "
        "штата."
    )

    validate(
        source,
        "Губернатор выдаёт ордер на задержание верховного судьи",
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "Верховный судья выдаёт ордер на задержание верховного судьи",
        )
    with pytest.raises(ValueError):
        validate(
            source,
            "Губернатор выдаёт ордер на задержание верховного прокурора",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_generic_oblique_header_cannot_consume_actor_modifier(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия выдана через представителя опытным прокурором "
        "Джоном Смитом."
    )
    with pytest.raises(ValueError):
        validate(source, "Прокурор Джон Смит выдал лицензию")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_modified_passive_actor_after_generic_oblique_header_is_complete(
    validate: RelationValidator,
) -> None:
    validate(
        (
            "Лицензия выдана через представителя опытным прокурором "
            "Джоном Смитом."
        ),
        "Опытный прокурор Джон Смит выдал лицензию",
    )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_nominative_generic_person_before_modal_remains_actor(
    validate: RelationValidator,
) -> None:
    source = "Сотрудник должен выдать лицензию."
    with pytest.raises(ValueError):
        validate(source, "Гражданин должен выдать лицензию")
    validate(source, source.rstrip("."))
