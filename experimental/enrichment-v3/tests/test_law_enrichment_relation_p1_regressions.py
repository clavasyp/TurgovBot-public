from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_relation_truths,
    _normalized_text,
    _relation_actor_key,
    _relation_complete_postposed_actor_key,
    _relation_mask_confirmed_actor_identity_payloads,
    _relation_permission_authority_key,
    _relation_permission_spans,
    _relation_syntax_surface,
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


def _validate_synonym(source: str, output: str) -> None:
    validate_synonym_source_evidence(source, [output])


RELATION_VALIDATORS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_synonym,
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_past_source_cannot_be_laundered_into_current_event(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд выдал лицензию.",
            "Суд выдаёт лицензию",
        )


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд выдал лицензию.",
            "Лицензия выдана судом",
        ),
        (
            "Суд выдаёт лицензию.",
            "Лицензия выдаётся судом",
        ),
    ),
    ids=("past-active-to-passive", "current-active-to-passive"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_active_and_passive_paraphrases_in_same_time_class_remain_supported(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд может выдать лицензию или ордер.",
            "Суд может выдать ордер",
        ),
        (
            "Суд обязан выдать лицензию и разрешение.",
            "Суд обязан выдать разрешение",
        ),
    ),
    ids=("or-second-object", "and-second-object"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_each_coordinated_direct_object_is_a_supported_source_truth(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд может выдать лицензию или ордер.",
            "Суд может выдать паспорт",
        ),
        (
            "Суд обязан выдать лицензию и разрешение.",
            "Суд обязан выдать сертификат",
        ),
    ),
    ids=("or-unknown-third-object", "and-unknown-third-object"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_coordinated_direct_objects_do_not_authorize_an_unknown_third_object(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    "output",
    (
        "Суд обязан закрыть организацию",
        "Прокуратура обязана закрыть организацию",
    ),
    ids=("first-actor", "second-actor"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_each_coordinated_actor_is_a_supported_source_truth(
    validate: RelationValidator,
    output: str,
) -> None:
    validate(
        "Суд и прокуратура обязаны закрыть организацию.",
        output,
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_coordinated_actors_do_not_authorize_an_unknown_third_actor(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд и прокуратура обязаны закрыть организацию.",
            "Полиция обязана закрыть организацию",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_information_and_data_alias_keeps_the_source_actor_and_recipient(
    validate: RelationValidator,
) -> None:
    validate(
        "Полиция сообщает FIB информацию.",
        "Полиция сообщает FIB данные",
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_information_and_data_alias_cannot_reverse_actor_and_recipient(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Полиция сообщает FIB информацию.",
            "FIB сообщает данные полиции",
        )


SF_PRIVATE_LAWYER_GROUNDS = (
    "Причины для аннулирования сертификата частного адвоката: "
    "нарушение закона и наличие судимости."
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_sf_85047_explicit_source_ground_remains_supported(
    validate: RelationValidator,
) -> None:
    validate(
        SF_PRIVATE_LAWYER_GROUNDS,
        (
            "судимость — основание для аннулирования сертификата "
            "частного адвоката"
        ),
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_sf_85047_cannot_invent_an_annulment_ground(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            SF_PRIVATE_LAWYER_GROUNDS,
            (
                "бедность — основание для аннулирования сертификата "
                "частного адвоката"
            ),
        )


SF_FIB_RAID_WARRANT = (
    "В случае подписания ордера на совершение рейда от Федерального бюро "
    "на организацию её лицензия автоматически замораживается."
)


@pytest.mark.parametrize(
    "output",
    (
        "подписание ордера федерального бюро на рейд организации",
        (
            "заморозка лицензии при подписании ордера FIB "
            "на рейд организации"
        ),
    ),
    ids=("warrant-event", "license-freeze"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_sf_85401_nominal_warrant_relation_remains_supported(
    validate: RelationValidator,
    output: str,
) -> None:
    validate(SF_FIB_RAID_WARRANT, output)


@pytest.mark.parametrize(
    "output",
    (
        "подписание ордера полиции на рейд организации",
        "подписание ордера федерального бюро на рейд жилого дома",
    ),
    ids=("wrong-issuer", "wrong-raid-target"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_sf_85401_cannot_invent_warrant_issuer_or_raid_target(
    validate: RelationValidator,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(SF_FIB_RAID_WARRANT, output)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        ("Суд выдаёт ордер.", "Ордер выдаёт суд"),
        ("Ордер выдан судом.", "Ордер выдан судом"),
    ),
    ids=("active-inverted", "passive-instrumental"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_open_vocabulary_actor_projection_keeps_faithful_authority(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        ("Суд выдаёт ордер.", "Ордер выдаёт адвокат"),
        ("Суд выдаёт ордер.", "Ордер выдаёт врач"),
        ("Суд выдаёт ордер.", "Ордер выдаёт гражданин"),
        ("Ордер выдан судом.", "Ордер выдан адвокатом"),
        ("Ордер выдан судом.", "Ордер выдан гражданином"),
    ),
    ids=(
        "active-lawyer",
        "active-doctor",
        "active-citizen",
        "passive-lawyer",
        "passive-citizen",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_open_vocabulary_actor_cannot_bypass_authority_check(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_unknown_object_alias_cannot_disable_mandatory_actor_check(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд всегда закрывает организацию.",
            "Прокуратура всегда закрывает предприятие",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_objectless_mandatory_legal_event_is_not_claimless(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Настоящая статья устанавливает общий порядок.",
            "Предприятие всегда закрывается",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_same_actor_topical_object_alias_remains_supported(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд всегда закрывает организацию.",
        "Суд всегда закрывает предприятие",
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_automatic_adverb_is_not_misclassified_as_vehicle(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд обязан закрыть организацию.",
        "Суд автоматически закрывает организацию",
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_open_finite_mandatory_predicate_requires_source(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Настоящая статья устанавливает общий порядок.",
            "Предприятие всегда ликвидируется",
        )


@pytest.mark.parametrize(
    ("output", "supported"),
    (
        ("Суд всегда ликвидирует предприятие", True),
        ("Прокуратура всегда ликвидирует предприятие", False),
    ),
    ids=("same-actor", "wrong-actor"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_open_finite_source_backing_keeps_actor_identity(
    validate: RelationValidator,
    output: str,
    supported: bool,
) -> None:
    source = "Суд всегда ликвидирует предприятие."
    if supported:
        validate(source, output)
    else:
        with pytest.raises(ValueError):
            validate(source, output)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Полиция всегда обыскивает дом.",
            "Дом всегда обыскивает полиция",
        ),
        (
            "Полиция всегда обыскивает автомобиль.",
            "Автомобиль всегда обыскивает полиция",
        ),
        (
            "Дом всегда обыскивает полиция.",
            "Полиция всегда обыскивает дом",
        ),
        (
            "Автомобиль всегда обыскивает полиция.",
            "Полиция всегда обыскивает автомобиль",
        ),
        (
            "Суд всегда закрывает организацию.",
            "Организацию всегда закрывает суд",
        ),
        (
            "Организацию всегда закрывает суд.",
            "Суд всегда закрывает организацию",
        ),
    ),
    ids=(
        "house-direct-to-inverted",
        "vehicle-direct-to-inverted",
        "house-inverted-to-direct",
        "vehicle-inverted-to-direct",
        "organization-direct-to-inverted",
        "organization-inverted-to-direct",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_known_inverted_patient_is_derived_from_object_projection(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_known_inverted_patient_does_not_hide_wrong_actor(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Полиция всегда обыскивает дом.",
            "Дом всегда обыскивает прокуратура",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_coordinated_predicates_share_their_trailing_object(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд обязан закрыть и опечатать организацию.",
        "Суд обязан закрыть организацию",
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_coordinated_predicate_shared_object_does_not_authorize_wrong_object(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд обязан закрыть и опечатать организацию.",
            "Суд обязан закрыть дом",
        )


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            (
                "Суд рассматривает жалобу, а также полиция обязана "
                "обыскать дом."
            ),
            "Полиция обязана обыскать дом",
        ),
        (
            (
                "Прокуратура изучает материалы, а также губернатор обязан "
                "закрыть организацию."
            ),
            "Губернатор обязан закрыть организацию",
        ),
        (
            (
                "Суд рассматривает жалобу, причём полиция обязана "
                "обыскать дом."
            ),
            "Полиция обязана обыскать дом",
        ),
        (
            (
                "Прокуратура изучает материалы, тогда как губернатор "
                "обязан закрыть организацию."
            ),
            "Губернатор обязан закрыть организацию",
        ),
    ),
    ids=("also-police", "also-governor", "whereas-police", "while-governor"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_soft_connector_starts_new_explicit_subject_clause(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_soft_connector_without_new_subject_keeps_same_actor(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд обязан рассмотреть жалобу, а также закрыть организацию.",
        "Суд обязан закрыть организацию",
    )


@pytest.mark.parametrize(
    "output",
    (
        "При выявлении нарушения закона суд обязан закрыть организацию",
        "При выявлении нарушения закона, суд обязан закрыть организацию",
    ),
    ids=("without-comma", "with-comma"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_trailing_condition_can_be_reordered_but_not_dropped(
    validate: RelationValidator,
    output: str,
) -> None:
    source = (
        "Суд обязан закрыть организацию, только если выявлено "
        "нарушение закона."
    )
    validate(source, output)
    with pytest.raises(ValueError):
        validate(source, "Суд обязан закрыть организацию")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_reordered_condition_does_not_authorize_a_different_actor(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            (
                "Суд обязан закрыть организацию, только если выявлено "
                "нарушение закона."
            ),
            (
                "При выявлении нарушения закона прокуратура обязана "
                "закрыть организацию"
            ),
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_unnumbered_heading_stops_previous_actor_inheritance(
    validate: RelationValidator,
) -> None:
    source = (
        "Полиция обязана:\n"
        "— уведомлять граждан.\n"
        "Общие положения:\n"
        "Губернатор может закрыть организацию."
    )
    validate(source, "Губернатор может закрыть организацию")
    with pytest.raises(ValueError):
        validate(source, "Полиция обязана закрыть организацию")


@pytest.mark.parametrize(
    "output",
    (
        "Суд выдал лицензию",
        "Суд подписал лицензию",
    ),
    ids=("first-predicate", "second-predicate"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_coordinated_passive_predicates_share_actor_and_object(
    validate: RelationValidator,
    output: str,
) -> None:
    validate(
        "Лицензия выдана и подписана судом.",
        output,
    )


@pytest.mark.parametrize(
    "output",
    (
        "Прокуратура выдала лицензию",
        "Суд выдал ордер",
    ),
    ids=("wrong-actor", "wrong-object"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_coordinated_passive_roles_cannot_be_replaced(
    validate: RelationValidator,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Лицензия выдана и подписана судом.",
            output,
        )


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Лицензия выдана, и ордер подписан судом.",
            "Суд выдал лицензию",
        ),
        (
            "Лицензия выдана или подписана судом.",
            "Суд выдал лицензию",
        ),
    ),
    ids=("comma-new-clause", "alternative-not-definite"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_coordinated_passive_role_sharing_stays_bounded(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    ("source", "head_output", "deputy_output"),
    (
        (
            (
                "Руководитель или его заместитель вправе выдавать "
                "территориальный пропуск."
            ),
            "руководитель вправе выдавать территориальный пропуск",
            (
                "заместитель руководителя вправе выдавать "
                "территориальный пропуск"
            ),
        ),
        (
            "Директор или его заместитель вправе оформлять пропуск.",
            "директор вправе оформлять пропуск",
            "заместитель директора вправе оформлять пропуск",
        ),
        (
            "Начальник или его заместитель вправе выдавать разрешение.",
            "начальник вправе выдавать разрешение",
            "заместитель начальника вправе выдавать разрешение",
        ),
    ),
    ids=("manager", "director", "chief"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_possessive_coordinated_authorities_are_projected_separately(
    validate: RelationValidator,
    source: str,
    head_output: str,
    deputy_output: str,
) -> None:
    validate(source, head_output)
    validate(source, deputy_output)
    with pytest.raises(ValueError):
        validate(source, "прокурор вправе выдавать пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_may_actor_coordination_does_not_rebind_passive_patient(
    validate: RelationValidator,
) -> None:
    validate(
        "Пропуск может быть аннулирован судом.",
        "суд может аннулировать пропуск",
    )
    with pytest.raises(ValueError):
        validate(
            "Пропуск может быть аннулирован судом.",
            "пропуск может аннулировать суд",
        )
    with pytest.raises(ValueError):
        validate(
            "Пропуск может быть аннулирован судом.",
            "прокурор может аннулировать пропуск",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_unambiguous_accusative_object_first_remains_supported(
    validate: RelationValidator,
) -> None:
    validate(
        "Лицензия может быть аннулирована судом.",
        "лицензию может аннулировать суд",
    )


@pytest.mark.parametrize(
    ("source", "ambiguous_output"),
    (
        (
            "Суд может аннулировать данный пропуск.",
            "данный пропуск может немедленно аннулировать суд",
        ),
        (
            "Суд может аннулировать территориальный пропуск.",
            "территориальный пропуск может аннулировать суд",
        ),
        (
            "Суд может аннулировать пропуск.",
            "пропуск также может аннулировать суд",
        ),
        (
            "Суд может аннулировать пропуск.",
            "пропуск может законно аннулировать суд",
        ),
        (
            "Суд может аннулировать разрешение.",
            "разрешение может аннулировать суд",
        ),
        (
            "Суд может аннулировать удостоверение.",
            "удостоверение может аннулировать суд",
        ),
    ),
    ids=(
        "determiner-and-adverb",
        "arbitrary-adjective",
        "particle-before-modal",
        "adverb-after-modal",
        "neuter-authorization",
        "neuter-certificate",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_ambiguous_premodal_role_order_cannot_bypass_guard(
    validate: RelationValidator,
    source: str,
    ambiguous_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, ambiguous_output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_two_line_authority_roster_projects_each_listed_actor(
    validate: RelationValidator,
) -> None:
    source = (
        "Лица, уполномоченные выдавать допуск\n"
        "Право на выдачу допуска имеют следующие должностные лица:\n"
        "Губернатор штата и его заместители.\n"
        "Генеральный прокурор штата и его заместители."
    )
    validate(
        source,
        "губернатор вправе выдавать допуск",
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "мэр вправе выдавать допуск",
        )


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    (
        (
            (
                "Повторное получение сертификата после аннулирования "
                "допускается не ранее чем через 14 дней. В случае выдачи "
                "ранее истечения 14-дневного срока ответственность несёт "
                "лицо, выдавшее сертификат."
            ),
            "выдали сертификат раньше четырнадцати дней",
            (
                "выдача сертификата раньше четырнадцати дней "
                "влечёт ответственность"
            ),
        ),
        (
            (
                "Руководитель или его заместитель вправе выдавать пропуск "
                "только к той территории, за которой закреплена организация."
            ),
            "заместитель руководителя оформляет территориальный допуск",
            (
                "заместитель руководителя вправе выдавать "
                "территориальный пропуск"
            ),
        ),
        (
            (
                "Всякий раз, когда делается ссылка на часть кодекса, "
                "ссылка применяется ко всем изменениям и дополнениям."
            ),
            "поправки автоматически применяются к кодексной ссылке",
            (
                "кодексная ссылка автоматически применяется "
                "ко всем поправкам"
            ),
        ),
    ),
    ids=(
        "conditional-certificate-issue",
        "may-is-not-definite",
        "apply-direction",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_sf_relation_corrections_preserve_source_roles_and_modality(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)
    validate(source, supported_output)


@pytest.mark.parametrize(
    "value",
    (
        (
            "Суд освобождает заключённого не в обязательном "
            "порядке."
        ),
        (
            "Суд освобождает заключённого и рассматривает жалобу "
            "в обязательном порядке."
        ),
        (
            "Суд освобождает заключённого, комиссия рассматривает "
            "жалобу в обязательном порядке."
        ),
    ),
    ids=("negated-marker", "second-finite-and", "second-finite-comma"),
)
def test_trailing_mandatory_marker_stays_with_its_nearest_predicate(
    value: str,
) -> None:
    release = next(
        relation
        for relation in _extract_relation_truths(value)
        if relation.predicate == "освобод"
    )
    assert release.modality == "definite"


def test_trailing_mandatory_marker_upgrades_its_exact_predicate() -> None:
    release = next(
        relation
        for relation in _extract_relation_truths(
            (
                "Суд освобождает заключённого "
                "в обязательном порядке."
            )
        )
        if relation.predicate == "освобод"
    )
    assert release.modality == "mandatory"


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_right_of_actor_grammar_preserves_may_and_genitive_role(
    validate: RelationValidator,
) -> None:
    source = (
        "Руководитель вправе выдавать территориальные пропуски."
    )
    validate(
        source,
        "право руководителя выдавать территориальные пропуски",
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "право начальника выдавать территориальные пропуски",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_authority_roster_preserves_source_acronym_qualification(
    validate: RelationValidator,
) -> None:
    source = (
        "Лица, уполномоченные выдавать допуск\n"
        "Право на выдачу допуска имеют следующие должностные лица:\n"
        "Директор Федерального бюро расследований (FIB)."
    )
    validate(source, "директор fib выдаёт допуск")
    with pytest.raises(ValueError):
        validate(source, "директор cia выдаёт допуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_instrumental_document_form_is_not_operative_actor(
    validate: RelationValidator,
) -> None:
    source = "Допуск оформляется в виде приказа."
    validate(source, "допуск оформляется приказом")
    with pytest.raises(ValueError):
        validate(source, "допуск оформляется прокурором")


def test_access_pass_genitive_is_not_permission_modality() -> None:
    relation = next(
        relation
        for relation in _extract_relation_truths(
            "Решение о выдаче допуска оформляется в виде приказа."
        )
        if relation.event == "present_passive"
    )
    assert relation.modality == "definite"


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_validity_condition_cannot_be_laundered_into_absolute_duty(
    validate: RelationValidator,
) -> None:
    source = (
        "Документ, предоставляющий допуск, считается действительным "
        "при соблюдении условия: подписан уполномоченным должностным "
        "лицом."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "допуск должен быть подписан уполномоченным лицом",
        )
    validate(
        source,
        "допуск действителен при подписи уполномоченного лица",
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_notification_exception_cannot_be_dropped_from_mandatory_claim(
    validate: RelationValidator,
) -> None:
    source = (
        "Агент обязан уведомить граждан об основаниях входа, "
        "за исключением случаев, если промедление создаёт угрозу."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "агент обязан сообщить основание входа",
        )
    validate(
        source,
        "порядок уведомления об основании входа",
    )


def test_revocation_reason_query_has_no_fake_actor(
) -> None:
    source = (
        "Причины для аннулирования сертификата частного адвоката: "
        "нарушение закона и наличие судимости."
    )
    output = "за что аннулируют сертификат адвоката"
    validate_synonym_source_evidence(source, [output])
    relation = next(iter(_extract_relation_truths(output)))
    assert relation.predicate == "regulated_revocation"
    assert relation.direct_object == "certificate"
    assert relation.assertive is True
    assert relation.operative_actor == ""


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "output",
    (
        "суд аннулирует сертификат адвоката",
        "за что суд аннулирует сертификат адвоката",
        "за что суд аннулировал сертификат адвоката",
        "за что аннулировали сертификат адвоката",
        "за что обязательно аннулируют сертификат адвоката",
        "аннулируют сертификат адвоката за обычную жалобу",
    ),
)
def test_reason_query_support_does_not_bless_declarative_revocation(
    validate: RelationValidator,
    output: str,
) -> None:
    source = (
        "Причины для аннулирования сертификата частного адвоката: "
        "нарушение закона и наличие судимости."
    )
    with pytest.raises(ValueError):
        validate(source, output)


PASSIVE_ISSUER_POSITIVE_CASES: tuple[tuple[str, str], ...] = (
    (
        (
            "Пропуск, выданный руководителем или заместителем руководителя "
            "государственной организации, может быть им отозван в любое "
            "время."
        ),
        "заместитель руководителя может отозвать пропуск",
    ),
    (
        (
            "Пропуск, выданный руководителем или заместителем руководителя "
            "государственной организации, может быть им отозван в любое "
            "время."
        ),
        "руководитель может отозвать пропуск",
    ),
    (
        (
            "Пропуск, выданный руководителем государственной организации, "
            "может быть им отозван в любое время."
        ),
        "руководитель может отозвать выданный им пропуск",
    ),
    (
        "Допуск, оформленный прокуратурой, может быть ею отменён.",
        "прокуратура может отменить допуск",
    ),
    (
        "Пропуск может быть отозван судом или прокуратурой.",
        "суд может отозвать пропуск",
    ),
    (
        "Пропуск может быть отозван судом или прокуратурой.",
        "прокуратура может отозвать пропуск",
    ),
    (
        "Пропуск отозван судом и прокуратурой.",
        "суд отозвал пропуск",
    ),
    (
        "Пропуск отозван судом и прокуратурой.",
        "прокуратура отозвала пропуск",
    ),
    (
        "Пропуск отозван судом или прокуратурой.",
        "Пропуск отозван судом или прокуратурой.",
    ),
    (
        "Пропуск, выданный директором FIB, может быть им отозван.",
        "директор FIB может отозвать пропуск",
    ),
    (
        "Пропуск, выданный руководителем, не может быть им отозван.",
        "руководитель не может отозвать пропуск",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(("source", "output"), PASSIVE_ISSUER_POSITIVE_CASES)
def test_passive_issuer_and_actor_choice_positive_matrix(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


def test_actorless_title_keeps_an_unresolved_passive_actor_choice() -> None:
    _validate_title(
        "Пропуск отозван судом или прокуратурой.",
        "пропуск отозван",
    )


PASSIVE_ISSUER_NEGATIVE_CASES: tuple[tuple[str, str], ...] = (
    (
        (
            "Пропуск, выданный руководителем государственной организации, "
            "может быть им отозван в любое время."
        ),
        "руководитель отозвал пропуск",
    ),
    (
        (
            "Пропуск, выданный руководителем или заместителем руководителя "
            "государственной организации, может быть им отозван в любое "
            "время."
        ),
        "заместитель руководителя отозвал пропуск",
    ),
    (
        (
            "Пропуск, выданный руководителем или заместителем руководителя "
            "государственной организации, может быть им отозван в любое "
            "время."
        ),
        "руководитель отзывает пропуск",
    ),
    (
        (
            "Пропуск, выданный руководителем или заместителем руководителя "
            "государственной организации, может быть им отозван в любое "
            "время."
        ),
        "заместитель руководителя обязан отозвать пропуск",
    ),
    (
        "Пропуск, выданный руководителем, отозван судом.",
        "руководитель отозвал пропуск",
    ),
    (
        "Пропуск, выданный руководителем, может быть отозван судом.",
        "руководитель может отозвать пропуск",
    ),
    (
        (
            "Пропуск, выданный руководителем или заместителем руководителя "
            "государственной организации, может быть им отозван в любое "
            "время."
        ),
        "заместитель начальника может отозвать территориальный пропуск",
    ),
    (
        (
            "Пропуск, выданный руководителем после согласования с "
            "прокуратурой, может быть им отозван."
        ),
        "прокуратура может отозвать пропуск",
    ),
    (
        (
            "Лицензия, выданная руководителем, может быть им отозвана. "
            "Пропуск действует бессрочно."
        ),
        "руководитель может отозвать пропуск",
    ),
    (
        "Пропуск, выданный директором FIB, может быть им отозван.",
        "директор может отозвать пропуск",
    ),
    (
        "Пропуск отозван судом или прокуратурой.",
        "суд отозвал пропуск",
    ),
    (
        "Пропуск отозван судом или прокуратурой.",
        "прокуратура отозвала пропуск",
    ),
    (
        "Пропуск должен быть отозван судом или прокуратурой.",
        "суд обязан отозвать пропуск",
    ),
    (
        "Пропуск должен быть отозван судом или прокуратурой.",
        "прокуратура обязана отозвать пропуск",
    ),
    (
        "Пропуск отозван судом или прокуратурой.",
        "Пропуск отозван судом или полицией.",
    ),
    (
        "Пропуск отозван судом или прокуратурой.",
        "верховный суд отозвал пропуск",
    ),
    (
        "Пропуск, выданный руководителем, не может быть им отозван.",
        "руководитель может отозвать пропуск",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(("source", "output"), PASSIVE_ISSUER_NEGATIVE_CASES)
def test_passive_issuer_and_actor_choice_negative_matrix(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


FACTUAL_NOMINAL_VARIANTS: tuple[str, ...] = (
    "факт отзыва пропуска судом",
    "состоявшийся отзыв пропуска судом",
    "произошёл отзыв пропуска судом",
    "имел место отзыв пропуска судом",
    "отзыв пропуска осуществлён судом",
    "аннулирование пропуска произведено судом",
    "подтверждён факт отзыва пропуска судом",
    "факт: отзыв пропуска судом",
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize("output", FACTUAL_NOMINAL_VARIANTS)
def test_unresolved_actor_choice_does_not_prove_single_actor_factual_nominal(
    validate: RelationValidator,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Пропуск отозван судом или прокуратурой.",
            output,
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "output",
    (
        "факт отзыва пропуска судом",
        "произошёл отзыв пропуска судом",
        "отзыв пропуска осуществлён судом",
    ),
)
def test_possible_revocation_does_not_prove_completed_factual_nominal(
    validate: RelationValidator,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate("Пропуск может быть отозван судом.", output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize("output", FACTUAL_NOMINAL_VARIANTS)
def test_completed_single_actor_source_supports_factual_nominal_variants(
    validate: RelationValidator,
    output: str,
) -> None:
    validate("Пропуск отозван судом.", output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "source",
    (
        "Пропуск отозван судом или прокуратурой.",
        "Пропуск может быть отозван судом.",
    ),
)
def test_plain_revocation_nominal_remains_a_topical_query(
    validate: RelationValidator,
    source: str,
) -> None:
    validate(source, "отзыв пропуска судом")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_full_actor_choice_factual_nominal_remains_supported(
    validate: RelationValidator,
) -> None:
    validate(
        "Пропуск отозван судом или прокуратурой.",
        "факт отзыва пропуска судом или прокуратурой",
    )


def test_actorless_factual_title_is_entailed_by_completed_revocation() -> None:
    _validate_title(
        "Пропуск отозван судом или прокуратурой.",
        "факт отзыва пропуска",
    )


def test_actorless_factual_synonym_keeps_critical_authority_coverage() -> None:
    with pytest.raises(ValueError, match="critical authority"):
        _validate_synonym(
            "Пропуск отозван судом или прокуратурой.",
            "факт отзыва пропуска",
        )


ACTOR_CHOICE_BOUNDARY_NEGATIVE_CASES: tuple[tuple[str, str], ...] = (
    (
        "Суд или прокуратура отозвали пропуск.",
        "суд отозвал пропуск",
    ),
    (
        "Суд или прокуратура отозвали пропуск.",
        "прокуратура отозвала пропуск",
    ),
    (
        "Суд или прокуратура отзывают пропуск.",
        "суд отзывает пропуск",
    ),
    (
        "Суд или прокуратура обязаны отозвать пропуск.",
        "суд обязан отозвать пропуск",
    ),
    (
        "Пропуск отозван судом и/или прокуратурой.",
        "суд отозвал пропуск",
    ),
    (
        "Пропуск отозван судом либо прокуратурой.",
        "прокуратура отозвала пропуск",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "output"),
    ACTOR_CHOICE_BOUNDARY_NEGATIVE_CASES,
)
def test_actor_choice_cannot_be_rewritten_as_one_definite_actor(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд или прокуратура могут отозвать пропуск.",
            "суд может отозвать пропуск",
        ),
        (
            "Суд или прокуратура могут отозвать пропуск.",
            "прокуратура может отозвать пропуск",
        ),
        (
            "Суд и прокуратура обязаны отозвать пропуск.",
            "суд обязан отозвать пропуск",
        ),
        (
            "Суд и прокуратура обязаны отозвать пропуск.",
            "прокуратура обязана отозвать пропуск",
        ),
        (
            "Суд или прокуратура отозвали пропуск.",
            "Пропуск отозван прокуратурой или судом.",
        ),
    ),
)
def test_actor_choice_preserves_safe_branch_and_full_choice_paraphrases(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


MIXED_REVOCATION_SOURCE = (
    "Пропуск может быть отозван судом. "
    "Пропуск отозван прокуратурой."
)


@pytest.mark.parametrize(
    "output",
    ("суд отозвал пропуск", "суд отзывает пропуск"),
)
def test_title_cannot_pool_actor_time_and_modality_from_different_truths(
    output: str,
) -> None:
    with pytest.raises(ValueError):
        _validate_title(MIXED_REVOCATION_SOURCE, output)


@pytest.mark.parametrize(
    "synonyms",
    (
        ["суд отозвал пропуск", "прокуратура отозвала пропуск"],
        ["суд отзывает пропуск", "прокуратура отозвала пропуск"],
    ),
)
def test_synonym_set_cannot_hide_cross_truth_revocation_pooling(
    synonyms: list[str],
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(MIXED_REVOCATION_SOURCE, synonyms)


def test_mixed_source_keeps_each_source_bound_revocation_truth() -> None:
    _validate_title(MIXED_REVOCATION_SOURCE, "суд может отозвать пропуск")
    _validate_title(MIXED_REVOCATION_SOURCE, "прокуратура отозвала пропуск")
    validate_synonym_source_evidence(
        MIXED_REVOCATION_SOURCE,
        [
            "суд может отозвать пропуск",
            "прокуратура отозвала пропуск",
        ],
    )


QUALIFIED_ALIAS_POOLING_CASES: tuple[tuple[str, str, str], ...] = (
    (
        MIXED_REVOCATION_SOURCE,
        "верховный суд отозвал пропуск",
        "прокуратура отозвала пропуск",
    ),
    (
        MIXED_REVOCATION_SOURCE,
        "верховный суд отзывает пропуск",
        "прокуратура отозвала пропуск",
    ),
    (
        (
            "Пропуск отозван судом. "
            "Пропуск может быть отозван прокуратурой."
        ),
        "генеральная прокуратура отозвала пропуск",
        "суд отозвал пропуск",
    ),
)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    QUALIFIED_ALIAS_POOLING_CASES,
)
def test_title_qualifier_alias_cannot_bypass_cross_truth_coherence(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    del supported_output
    with pytest.raises(ValueError):
        _validate_title(source, bad_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    QUALIFIED_ALIAS_POOLING_CASES,
)
def test_synonym_set_qualifier_alias_cannot_hide_cross_truth_coherence(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


ACTION_ATTACHED_AUTHORITY_POOLING_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        (
            "Пропуск может быть отозван губернатором. "
            "Пропуск отозван судом."
        ),
        "вице-губернатор отозвал пропуск",
        "суд отозвал пропуск",
    ),
    (
        (
            "Пропуск может быть отозван губернатором. "
            "Пропуск отозван судом."
        ),
        "вице-губернатор отзывает пропуск",
        "суд отозвал пропуск",
    ),
    (
        (
            "Пропуск может быть отозван губернатором. "
            "Пропуск отозван судом."
        ),
        "пропуск отозван вице-губернатором",
        "суд отозвал пропуск",
    ),
    (
        (
            "Пропуск может быть отозван губернатором. "
            "Пропуск отозван судом."
        ),
        "факт отзыва пропуска вице-губернатором",
        "суд отозвал пропуск",
    ),
    (
        (
            "Пропуск может быть отозван вице-губернатором. "
            "Пропуск отозван судом."
        ),
        "губернатор отозвал пропуск",
        "суд отозвал пропуск",
    ),
    (
        (
            "Пропуск может быть отозван полицией. "
            "Пропуск отозван судом."
        ),
        "пропуск отозван сотрудниками LSPD",
        "суд отозвал пропуск",
    ),
    (
        (
            "Пропуск может быть отозван полицией. "
            "Пропуск отозван судом."
        ),
        "пропуск отозван сотрудниками LSSD",
        "суд отозвал пропуск",
    ),
    (
        (
            "Пропуск может быть отозван полицией. "
            "Пропуск отозван судом."
        ),
        (
            "пропуск отозван сотрудниками "
            "Los Santos Police Department"
        ),
        "суд отозвал пропуск",
    ),
    (
        (
            "Пропуск может быть отозван губернатором. "
            "Пропуск отозван судом."
        ),
        "пропуск отозван кабинетом губернатора",
        "суд отозвал пропуск",
    ),
)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    ACTION_ATTACHED_AUTHORITY_POOLING_CASES,
)
def test_title_action_attached_authority_cannot_pool_source_truths(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    del supported_output
    with pytest.raises(ValueError):
        _validate_title(source, bad_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    ACTION_ATTACHED_AUTHORITY_POOLING_CASES,
)
def test_synonym_action_attached_authority_cannot_pool_source_truths(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Пропуск отозван вице-губернатором.",
            "вице-губернатор отозвал пропуск",
        ),
        (
            "Пропуск отозван вице-губернатором.",
            "факт отзыва пропуска вице-губернатором",
        ),
        (
            "Пропуск отозван сотрудниками LSPD.",
            "пропуск отозван сотрудниками LSPD",
        ),
        (
            "Пропуск отозван кабинетом губернатора.",
            "пропуск отозван кабинетом губернатора",
        ),
    ),
)
def test_exact_action_attached_authority_surface_remains_supported(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_police_department_identity_is_not_a_family_wide_alias(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Пропуск отозван сотрудниками LSPD.",
            "пропуск отозван сотрудниками LSSD",
        )


PASSIVE_QUALIFIED_ACTOR_POOLING_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        (
            "Пропуск может быть отозван генеральной прокуратурой. "
            "Пропуск отозван окружной прокуратурой."
        ),
        "пропуск отозван генеральной прокуратурой",
        "пропуск отозван окружной прокуратурой",
    ),
    (
        (
            "Пропуск может быть отозван федеральным судом. "
            "Пропуск отозван региональным судом."
        ),
        "пропуск отозван федеральным судом",
        "пропуск отозван региональным судом",
    ),
    (
        (
            "Пропуск может быть отозван дорожной полицией. "
            "Пропуск отозван военной полицией."
        ),
        "пропуск отозван дорожной полицией",
        "пропуск отозван военной полицией",
    ),
    (
        (
            "Пропуск может быть отозван федеральным лицензирующим органом. "
            "Пропуск отозван региональным лицензирующим органом."
        ),
        "пропуск отозван федеральным лицензирующим органом",
        "пропуск отозван региональным лицензирующим органом",
    ),
    (
        (
            "Пропуск может быть отозван федеральным министром юстиции. "
            "Пропуск отозван региональным министром юстиции."
        ),
        "пропуск отозван федеральным министром юстиции",
        "пропуск отозван региональным министром юстиции",
    ),
    (
        (
            "Пропуск может быть отозван коммерческой организацией. "
            "Пропуск отозван общественной организацией."
        ),
        "пропуск отозван коммерческой организацией",
        "пропуск отозван общественной организацией",
    ),
)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    PASSIVE_QUALIFIED_ACTOR_POOLING_CASES,
)
def test_title_passive_actor_qualifier_cannot_pool_source_truths(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    del supported_output
    with pytest.raises(ValueError):
        _validate_title(source, bad_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    PASSIVE_QUALIFIED_ACTOR_POOLING_CASES,
)
def test_synonym_set_passive_actor_qualifier_cannot_pool_source_truths(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


PASSIVE_QUALIFIED_ACTOR_IDENTITY_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        "окружной прокуратурой",
        "генеральной прокуратурой",
        "прокуратурой",
    ),
    ("региональным судом", "федеральным судом", "судом"),
    ("военной полицией", "дорожной полицией", "полицией"),
    (
        "региональным лицензирующим органом",
        "федеральным лицензирующим органом",
        "лицензирующим органом",
    ),
    (
        "региональным министром юстиции",
        "федеральным министром юстиции",
        "министром юстиции",
    ),
    (
        "общественной организацией",
        "коммерческой организацией",
        "организацией",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source_actor", "other_actor", "generic_actor"),
    PASSIVE_QUALIFIED_ACTOR_IDENTITY_CASES,
)
def test_passive_actor_qualifier_cannot_be_replaced(
    validate: RelationValidator,
    source_actor: str,
    other_actor: str,
    generic_actor: str,
) -> None:
    del generic_actor
    with pytest.raises(ValueError):
        validate(
            f"Пропуск отозван {source_actor}.",
            f"пропуск отозван {other_actor}",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source_actor", "other_actor", "generic_actor"),
    PASSIVE_QUALIFIED_ACTOR_IDENTITY_CASES,
)
def test_generic_passive_actor_cannot_gain_an_invented_qualifier(
    validate: RelationValidator,
    source_actor: str,
    other_actor: str,
    generic_actor: str,
) -> None:
    del source_actor
    with pytest.raises(ValueError):
        validate(
            f"Пропуск отозван {generic_actor}.",
            f"пропуск отозван {other_actor}",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source_actor", "other_actor", "generic_actor"),
    PASSIVE_QUALIFIED_ACTOR_IDENTITY_CASES,
)
def test_exact_passive_actor_qualifier_remains_supported(
    validate: RelationValidator,
    source_actor: str,
    other_actor: str,
    generic_actor: str,
) -> None:
    del other_actor, generic_actor
    validate(
        f"Пропуск отозван {source_actor}.",
        f"пропуск отозван {source_actor}",
    )


PASSIVE_ACTIVE_QUALIFIED_ACTOR_CASES: tuple[tuple[str, str], ...] = (
    ("генеральной прокуратурой", "генеральная прокуратура"),
    ("федеральным судом", "федеральный суд"),
    ("дорожной полицией", "дорожная полиция"),
    (
        "федеральным лицензирующим органом",
        "федеральный лицензирующий орган",
    ),
    (
        "федеральным министром юстиции",
        "федеральный министр юстиции",
    ),
    ("коммерческой организацией", "коммерческая организация"),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("instrumental_actor", "active_actor"),
    PASSIVE_ACTIVE_QUALIFIED_ACTOR_CASES,
)
def test_qualified_actor_identity_survives_active_passive_case_change(
    validate: RelationValidator,
    instrumental_actor: str,
    active_actor: str,
) -> None:
    validate(
        f"Пропуск отозван {instrumental_actor}.",
        f"{active_actor} отозвал пропуск",
    )
    validate(
        f"{active_actor.capitalize()} отозвал пропуск.",
        f"пропуск отозван {instrumental_actor}",
    )


PASSIVE_POST_HEAD_IDENTITY_POOLING_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        (
            "Пропуск может быть отозван судом округа Блейн. "
            "Пропуск отозван судом штата Сан-Андреас."
        ),
        "пропуск отозван судом округа Блейн",
        "пропуск отозван судом штата Сан-Андреас",
    ),
    (
        (
            "Пропуск может быть отозван прокуратурой штата Сан-Андреас. "
            "Пропуск отозван прокуратурой округа Блейн."
        ),
        "пропуск отозван прокуратурой штата Сан-Андреас",
        "пропуск отозван прокуратурой округа Блейн",
    ),
    (
        (
            "Пропуск может быть отозван полицией города Лос-Сантос. "
            "Пропуск отозван полицией округа Блейн."
        ),
        "пропуск отозван полицией города Лос-Сантос",
        "пропуск отозван полицией округа Блейн",
    ),
    (
        (
            "Пропуск может быть отозван организацией Альфа. "
            "Пропуск отозван организацией Бета."
        ),
        "пропуск отозван организацией Альфа",
        "пропуск отозван организацией Бета",
    ),
)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    PASSIVE_POST_HEAD_IDENTITY_POOLING_CASES,
)
def test_title_passive_post_head_identity_cannot_pool_source_truths(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    del supported_output
    with pytest.raises(ValueError):
        _validate_title(source, bad_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    PASSIVE_POST_HEAD_IDENTITY_POOLING_CASES,
)
def test_synonym_set_passive_post_head_identity_cannot_pool_source_truths(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("generic_actor", "qualified_actor"),
    (
        ("судом", "судом округа Блейн"),
        ("организацией", "организацией Альфа"),
    ),
)
def test_generic_passive_actor_cannot_gain_post_head_identity(
    validate: RelationValidator,
    generic_actor: str,
    qualified_actor: str,
) -> None:
    with pytest.raises(ValueError):
        validate(
            f"Пропуск отозван {generic_actor}.",
            f"пропуск отозван {qualified_actor}",
        )


PASSIVE_POST_HEAD_ACTIVE_CASES: tuple[tuple[str, str], ...] = (
    ("судом округа Блейн", "суд округа Блейн"),
    (
        "прокуратурой штата Сан-Андреас",
        "прокуратура штата Сан-Андреас",
    ),
    ("полицией города Лос-Сантос", "полиция города Лос-Сантос"),
    ("организацией Альфа", "организация Альфа"),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("instrumental_actor", "active_actor"),
    PASSIVE_POST_HEAD_ACTIVE_CASES,
)
def test_post_head_actor_identity_survives_active_passive_case_change(
    validate: RelationValidator,
    instrumental_actor: str,
    active_actor: str,
) -> None:
    validate(
        f"Пропуск отозван {instrumental_actor}.",
        f"{active_actor} отозвал пропуск",
    )
    validate(
        f"{active_actor.capitalize()} отозвал пропуск.",
        f"пропуск отозван {instrumental_actor}",
    )


PASSIVE_FOUR_MODIFIER_POOLING_CASES: tuple[
    tuple[str, str, str, str], ...
] = (
    (
        (
            "Пропуск может быть отозван первым федеральным окружным "
            "апелляционным судом. Пропуск отозван вторым федеральным "
            "окружным апелляционным судом."
        ),
        "пропуск отозван первым федеральным окружным апелляционным судом",
        "пропуск отозван вторым федеральным окружным апелляционным судом",
        "федеральным окружным апелляционным судом",
    ),
    (
        (
            "Пропуск может быть отозван первой крупной частной коммерческой "
            "организацией. Пропуск отозван второй крупной частной "
            "коммерческой организацией."
        ),
        "пропуск отозван первой крупной частной коммерческой организацией",
        "пропуск отозван второй крупной частной коммерческой организацией",
        "крупной частной коммерческой организацией",
    ),
)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output", "generic_actor"),
    PASSIVE_FOUR_MODIFIER_POOLING_CASES,
)
def test_title_cannot_drop_the_first_passive_actor_modifier(
    source: str,
    bad_output: str,
    supported_output: str,
    generic_actor: str,
) -> None:
    del supported_output, generic_actor
    with pytest.raises(ValueError):
        _validate_title(source, bad_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output", "generic_actor"),
    PASSIVE_FOUR_MODIFIER_POOLING_CASES,
)
def test_synonym_set_cannot_drop_the_first_passive_actor_modifier(
    source: str,
    bad_output: str,
    supported_output: str,
    generic_actor: str,
) -> None:
    del generic_actor
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output", "generic_actor"),
    PASSIVE_FOUR_MODIFIER_POOLING_CASES,
)
def test_generic_passive_actor_cannot_gain_a_fourth_modifier(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
    generic_actor: str,
) -> None:
    del source, supported_output
    with pytest.raises(ValueError):
        validate(
            f"Пропуск отозван {generic_actor}.",
            bad_output,
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output", "generic_actor"),
    PASSIVE_FOUR_MODIFIER_POOLING_CASES,
)
def test_exact_four_modifier_passive_actor_remains_supported(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
    generic_actor: str,
) -> None:
    del source, bad_output, generic_actor
    validate(
        supported_output.capitalize() + ".",
        supported_output,
    )


PASSIVE_MULTI_TOKEN_IDENTITY_POOLING_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        (
            "Пропуск может быть отозван судом штата Сан Андреас. "
            "Пропуск отозван судом округа Блейн Каунти."
        ),
        "пропуск отозван судом штата Сан Андреас",
        "пропуск отозван судом округа Блейн Каунти",
    ),
    (
        (
            "Пропуск может быть отозван полицией города Лос Сантос. "
            "Пропуск отозван полицией округа Блейн Каунти."
        ),
        "пропуск отозван полицией города Лос Сантос",
        "пропуск отозван полицией округа Блейн Каунти",
    ),
    (
        (
            "Пропуск может быть отозван организацией Красный Крест. "
            "Пропуск отозван организацией Синий Щит."
        ),
        "пропуск отозван организацией Красный Крест",
        "пропуск отозван организацией Синий Щит",
    ),
    (
        (
            "Пропуск может быть отозван организацией «Альфа». "
            "Пропуск отозван организацией «Бета»."
        ),
        "пропуск отозван организацией «Альфа»",
        "пропуск отозван организацией «Бета»",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    PASSIVE_MULTI_TOKEN_IDENTITY_POOLING_CASES,
)
def test_multi_token_or_quoted_passive_identity_cannot_pool_source_truths(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)
    validate(source, supported_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    PASSIVE_MULTI_TOKEN_IDENTITY_POOLING_CASES,
)
def test_multi_token_identity_cannot_hide_in_a_complete_synonym_set(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source_actor", "invented_actor"),
    (
        ("судом", "судом штата Сан Андреас"),
        ("судом штата Нью Йорк", "судом штата Сан Андреас"),
        ("организацией", "организацией Красный Крест"),
        ("организацией Синий Щит", "организацией Красный Крест"),
        ("организацией", "организацией «Альфа»"),
        ("организацией «Бета»", "организацией «Альфа»"),
    ),
)
def test_passive_actor_cannot_gain_or_replace_a_complete_identity(
    validate: RelationValidator,
    source_actor: str,
    invented_actor: str,
) -> None:
    with pytest.raises(ValueError):
        validate(
            f"Пропуск отозван {source_actor}.",
            f"пропуск отозван {invented_actor}",
        )


PASSIVE_COMPLETE_IDENTITY_ACTIVE_CASES: tuple[tuple[str, str], ...] = (
    ("судом штата Сан Андреас", "суд штата Сан Андреас"),
    ("судом округа Блейн Каунти", "суд округа Блейн Каунти"),
    ("полицией города Лос Сантос", "полиция города Лос Сантос"),
    ("полицией округа Блейн Каунти", "полиция округа Блейн Каунти"),
    ("организацией Красный Крест", "организация Красный Крест"),
    ("организацией Синий Щит", "организация Синий Щит"),
    ("организацией «Альфа»", "организация «Альфа»"),
    ("организацией «Бета»", "организация «Бета»"),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("instrumental_actor", "active_actor"),
    PASSIVE_COMPLETE_IDENTITY_ACTIVE_CASES,
)
def test_complete_actor_identity_survives_active_passive_case_change(
    validate: RelationValidator,
    instrumental_actor: str,
    active_actor: str,
) -> None:
    active_source_actor = active_actor[0].upper() + active_actor[1:]
    validate(
        f"Пропуск отозван {instrumental_actor}.",
        f"{active_actor} отозвал пропуск",
    )
    validate(
        f"{active_source_actor} отозвал пропуск.",
        f"пропуск отозван {instrumental_actor}",
    )


ACTIVE_LONG_ACTOR_SOURCE = (
    "Первый новый местный районный мировой суд может отозвать пропуск. "
    "Второй новый местный районный мировой суд отозвал пропуск."
)
ACTIVE_LONG_ACTOR_BAD_OUTPUT = (
    "первый новый местный районный мировой суд отозвал пропуск"
)
ACTIVE_LONG_ACTOR_SUPPORTED_OUTPUT = (
    "второй новый местный районный мировой суд отозвал пропуск"
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_active_actor_key_cannot_drop_a_leading_modifier(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(ACTIVE_LONG_ACTOR_SOURCE, ACTIVE_LONG_ACTOR_BAD_OUTPUT)


def test_active_long_actor_cannot_hide_in_a_complete_synonym_set() -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            ACTIVE_LONG_ACTOR_SOURCE,
            [
                ACTIVE_LONG_ACTOR_BAD_OUTPUT,
                ACTIVE_LONG_ACTOR_SUPPORTED_OUTPUT,
            ],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_generic_active_actor_cannot_gain_a_long_qualified_identity(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд отозвал пропуск.",
            ACTIVE_LONG_ACTOR_BAD_OUTPUT,
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_exact_long_active_actor_remains_supported(
    validate: RelationValidator,
) -> None:
    validate(
        ACTIVE_LONG_ACTOR_SUPPORTED_OUTPUT.capitalize() + ".",
        ACTIVE_LONG_ACTOR_SUPPORTED_OUTPUT,
    )
    validate(
        "Пропуск отозван вторым новым местным районным мировым судом.",
        ACTIVE_LONG_ACTOR_SUPPORTED_OUTPUT,
    )
    validate(
        ACTIVE_LONG_ACTOR_SUPPORTED_OUTPUT.capitalize() + ".",
        "пропуск отозван вторым новым местным районным мировым судом",
    )


@pytest.mark.parametrize(
    ("source", "expected_actor"),
    (
        (
            "Пропуск отозван судом округа Блейн Каунти.",
            "суд округ блейн каунт",
        ),
        (
            "Пропуск отозван организацией Синий Щит.",
            "организаци син щит",
        ),
        (
            (
                "Пропуск отозван организацией Красный Крест "
                "после решения суда."
            ),
            "организаци красн крест",
        ),
    ),
)
def test_complete_passive_actor_key_keeps_the_entire_identity(
    source: str,
    expected_actor: str,
) -> None:
    actors = {
        truth.operative_actor
        for truth in _extract_relation_truths(source)
        if truth.predicate == "regulated_revocation"
    }
    assert expected_actor in actors


@pytest.mark.parametrize(
    "unsafe_actor_tail",
    (
        "организацией Красный Крест отказалась",
        "организацией «Альфа",
        (
            "организацией Альфа Бета Гамма Дельта "
            "Эпсилон Дзета Эта"
        ),
    ),
)
def test_incomplete_passive_identity_never_falls_back_to_bare_organization(
    unsafe_actor_tail: str,
) -> None:
    actors = {
        truth.operative_actor
        for truth in _extract_relation_truths(
            f"Пропуск отозван {unsafe_actor_tail}."
        )
        if truth.predicate == "regulated_revocation"
    }
    assert "организаци" not in actors
    assert any(
        actor.startswith("__unresolved_actor__")
        for actor in actors
    )


def test_truly_generic_passive_organization_remains_generic() -> None:
    actors = {
        truth.operative_actor
        for truth in _extract_relation_truths(
            "Пропуск отозван организацией."
        )
        if truth.predicate == "regulated_revocation"
    }
    assert actors == {"организаци"}


POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        (
            "Суд штата Сан Андреас может отозвать пропуск. "
            "Суд округа Блейн Каунти отозвал пропуск."
        ),
        "пропуск отозвал суд штата Сан Андреас",
        "пропуск отозвал суд округа Блейн Каунти",
    ),
    (
        (
            "Полиция города Лос Сантос может отозвать пропуск. "
            "Полиция округа Блейн Каунти отозвала пропуск."
        ),
        "пропуск отозвала полиция города Лос Сантос",
        "пропуск отозвала полиция округа Блейн Каунти",
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск."
        ),
        "пропуск отозвала организация Красный Крест",
        "пропуск отозвала организация Синий Щит",
    ),
    (
        (
            "Организация «Альфа» может отозвать пропуск. "
            "Организация «Бета» отозвала пропуск."
        ),
        "пропуск отозвала организация «Альфа»",
        "пропуск отозвала организация «Бета»",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES,
)
def test_postposed_complete_identity_cannot_pool_source_truths(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)
    validate(source, supported_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES,
)
def test_postposed_identity_cannot_hide_in_a_complete_synonym_set(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "invented_output"),
    (
        (
            "Суд отозвал пропуск.",
            "пропуск отозвал суд штата Сан Андреас",
        ),
        (
            "Организация отозвала пропуск.",
            "пропуск отозвала организация Красный Крест",
        ),
        (
            "Организация «Бета» отозвала пропуск.",
            "пропуск отозвала организация «Альфа»",
        ),
    ),
)
def test_postposed_actor_cannot_gain_or_replace_identity(
    validate: RelationValidator,
    source: str,
    invented_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, invented_output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("active_actor", "verb"),
    (
        ("суд штата Сан Андреас", "отозвал"),
        ("полиция города Лос Сантос", "отозвала"),
        ("организация Красный Крест", "отозвала"),
        ("организация Синий Щит", "отозвала"),
        ("организация «Альфа»", "отозвала"),
    ),
)
def test_complete_actor_identity_survives_active_postposed_order(
    validate: RelationValidator,
    active_actor: str,
    verb: str,
) -> None:
    active_source_actor = active_actor[0].upper() + active_actor[1:]
    validate(
        f"{active_source_actor} {verb} пропуск.",
        f"пропуск {verb} {active_actor}",
    )
    validate(
        f"Пропуск {verb} {active_actor}.",
        f"{active_actor} {verb} пропуск",
    )


@pytest.mark.parametrize(
    "unsafe_actor_tail",
    (
        "организация Красный Крест отказалась",
        "организация «Альфа",
        "организация Альфа Бета Гамма Дельта Эпсилон Дзета Эта",
    ),
)
def test_incomplete_postposed_identity_never_falls_back_to_bare_organization(
    unsafe_actor_tail: str,
) -> None:
    output = f"Пропуск отозвала {unsafe_actor_tail}."
    actors = {
        truth.operative_actor
        for truth in _extract_relation_truths(output)
        if truth.predicate == "regulated_revocation"
    }
    assert "организаци" not in actors
    assert any(
        actor.startswith("__unresolved_actor__")
        for actor in actors
    )
    with pytest.raises(ValueError):
        _validate_title("Организация отозвала пропуск.", output)
    with pytest.raises(ValueError):
        _validate_synonym("Организация отозвала пропуск.", output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_long_active_actor_survives_postposed_order_without_pooling(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            ACTIVE_LONG_ACTOR_SOURCE,
            (
                "пропуск отозвал первый новый местный районный "
                "мировой суд"
            ),
        )
    validate(
        ACTIVE_LONG_ACTOR_SUPPORTED_OUTPUT.capitalize() + ".",
        (
            "пропуск отозвал второй новый местный районный "
            "мировой суд"
        ),
    )


POSTPOSED_NEUTRAL_PREFIX_POOLING_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        (
            "Суд штата Сан Андреас может отозвать пропуск. "
            "Суд округа Блейн Каунти отозвал пропуск."
        ),
        "пропуск отозвал вчера суд штата Сан Андреас",
        "пропуск отозвал вчера суд округа Блейн Каунти",
    ),
    (
        (
            "Полиция города Лос Сантос может отозвать пропуск. "
            "Полиция округа Блейн Каунти отозвала пропуск."
        ),
        "пропуск отозвала лично полиция города Лос Сантос",
        "пропуск отозвала лично полиция округа Блейн Каунти",
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск."
        ),
        "пропуск отозвала затем организация Красный Крест",
        "пропуск отозвала затем организация Синий Щит",
    ),
    (
        (
            "Организация «Альфа» может отозвать пропуск. "
            "Организация «Бета» отозвала пропуск."
        ),
        "пропуск отозвала повторно организация «Альфа»",
        "пропуск отозвала повторно организация «Бета»",
    ),
    (
        (
            "Суд штата Сан Андреас может отозвать пропуск. "
            "Суд округа Блейн Каунти отозвал пропуск."
        ),
        "пропуск отозвал немедленно суд штата Сан Андреас",
        "пропуск отозвал немедленно суд округа Блейн Каунти",
    ),
    (
        (
            "Организация «Альфа» может отозвать пропуск. "
            "Организация «Бета» отозвала пропуск."
        ),
        "пропуск отозвала именно организация «Альфа»",
        "пропуск отозвала именно организация «Бета»",
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск."
        ),
        "пропуск отозвала сама организация Красный Крест",
        "пропуск отозвала сама организация Синий Щит",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    POSTPOSED_NEUTRAL_PREFIX_POOLING_CASES,
)
def test_neutral_prefix_before_postposed_actor_cannot_erase_its_identity(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)
    validate(source, supported_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    POSTPOSED_NEUTRAL_PREFIX_POOLING_CASES,
)
def test_prefixed_postposed_actor_cannot_hide_in_full_synonym_set(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "invented_output"),
    (
        (
            "Суд отозвал пропуск.",
            "пропуск отозвал вчера суд штата Сан Андреас",
        ),
        (
            "Организация отозвала пропуск.",
            "пропуск отозвала сама организация Синий Щит",
        ),
        (
            "Организация отозвала пропуск.",
            "пропуск отозвала именно организация «Альфа»",
        ),
    ),
)
def test_neutral_prefix_cannot_enable_specific_actor_invention(
    validate: RelationValidator,
    source: str,
    invented_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, invented_output)


ASCII_POSTPOSED_POOLING_CASES: tuple[tuple[str, str, str], ...] = tuple(
    (
        (
            f"{agency} Alpha может отозвать пропуск. "
            f"{agency} Beta отозвал пропуск."
        ),
        f"пропуск отозвал {agency} Alpha",
        f"пропуск отозвал {agency} Beta",
    )
    for agency in ("LSPD", "FIB", "USSS", "SANG")
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    ASCII_POSTPOSED_POOLING_CASES,
)
def test_ascii_agency_identity_is_not_truncated(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)
    validate(source, supported_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    ASCII_POSTPOSED_POOLING_CASES,
)
def test_ascii_agency_identity_cannot_hide_in_full_synonym_set(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "unsafe_output"),
    (
        ("LSPD отозвал пропуск.", "пропуск отозвал LSPD «Alpha"),
        ("FIB отозвал пропуск.", "пропуск отозвал FIB «Alpha"),
        ("USSS отозвал пропуск.", "пропуск отозвал USSS «Alpha"),
        ("SANG отозвал пропуск.", "пропуск отозвал SANG «Alpha"),
        ("Полиция отозвала пропуск.", "пропуск отозвала полиция «Альфа"),
        ("Суд отозвал пропуск.", "пропуск отозвал суд «Альфа"),
        (
            "Прокурор отозвал пропуск.",
            "пропуск отозвал прокурор Джон Смит",
        ),
    ),
)
def test_postposed_institution_cannot_gain_or_malformed_direct_identity(
    validate: RelationValidator,
    source: str,
    unsafe_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe_output)


@pytest.mark.parametrize(
    ("source", "unsafe_output", "supported_output"),
    (
        (
            "LSPD отозвал пропуск.",
            "пропуск отозвал LSPD «Alpha",
            "пропуск отозвал LSPD",
        ),
        (
            "FIB отозвал пропуск.",
            "пропуск отозвал FIB «Alpha",
            "пропуск отозвал FIB",
        ),
        (
            "USSS отозвал пропуск.",
            "пропуск отозвал USSS «Alpha",
            "пропуск отозвал USSS",
        ),
        (
            "SANG отозвал пропуск.",
            "пропуск отозвал SANG «Alpha",
            "пропуск отозвал SANG",
        ),
        (
            "Полиция отозвала пропуск.",
            "пропуск отозвала полиция «Альфа",
            "пропуск отозвала полиция",
        ),
        (
            "Суд отозвал пропуск.",
            "пропуск отозвал суд «Альфа",
            "пропуск отозвал суд",
        ),
        (
            "Прокурор отозвал пропуск.",
            "пропуск отозвал прокурор Джон Смит",
            "пропуск отозвал прокурор",
        ),
    ),
)
def test_direct_identity_invention_cannot_hide_in_full_synonym_set(
    source: str,
    unsafe_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [unsafe_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд штата отозвал пропуск.",
            "пропуск отозвал суд штата",
        ),
        (
            "Организация Белый Лотос отозвала пропуск.",
            "пропуск отозвала организация Белый Лотос",
        ),
        (
            (
                "Организация Альфа Бета Гамма Дельта Эпсилон Дзета "
                "отозвала пропуск."
            ),
            (
                "пропуск отозвала организация Альфа Бета Гамма "
                "Дельта Эпсилон Дзета"
            ),
        ),
        (
            "Прокурор Джон Смит отозвал пропуск.",
            "пропуск отозвал прокурор Джон Смит",
        ),
        (
            "LSPD Beta отозвал пропуск.",
            "пропуск отозвал LSPD Beta",
        ),
        (
            "FIB Beta отозвал пропуск.",
            "пропуск отозвал FIB Beta",
        ),
        (
            "USSS Beta отозвал пропуск.",
            "пропуск отозвал USSS Beta",
        ),
        (
            "SANG Beta отозвал пропуск.",
            "пропуск отозвал SANG Beta",
        ),
    ),
)
def test_exact_postposed_direct_identity_remains_supported(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


MANDATORY_POSTPOSED_ACTOR_CASES: tuple[
    tuple[str, str, str, str], ...
] = (
    (
        (
            "Суд штата Сан Андреас может отозвать лицензию. "
            "Суд округа Блейн Каунти обязан отозвать лицензию."
        ),
        "лицензию обязан отозвать суд штата Сан Андреас",
        "лицензию обязан отозвать суд округа Блейн Каунти",
        "суд штата Сан Андреас может отозвать лицензию",
    ),
    (
        (
            "Полиция города Лос Сантос может отозвать лицензию. "
            "Полиция округа Блейн Каунти обязана отозвать лицензию."
        ),
        "лицензию обязана отозвать полиция города Лос Сантос",
        "лицензию обязана отозвать полиция округа Блейн Каунти",
        "полиция города Лос Сантос может отозвать лицензию",
    ),
    (
        (
            "Суд штата Сан Андреас может отозвать лицензию. "
            "Суд округа Блейн Каунти обязан отозвать лицензию."
        ),
        (
            "лицензию обязан отозвать по итогам проверки, "
            "суд штата Сан Андреас"
        ),
        (
            "лицензию обязан отозвать по итогам проверки, "
            "суд округа Блейн Каунти"
        ),
        "суд штата Сан Андреас может отозвать лицензию",
    ),
)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output", "may_output"),
    MANDATORY_POSTPOSED_ACTOR_CASES,
)
def test_object_first_mandatory_relation_uses_post_action_actor(
    source: str,
    bad_output: str,
    supported_output: str,
    may_output: str,
) -> None:
    del may_output
    with pytest.raises(ValueError):
        _validate_title(source, bad_output)
    _validate_title(source, supported_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output", "may_output"),
    MANDATORY_POSTPOSED_ACTOR_CASES,
)
def test_mandatory_postposed_actor_cannot_hide_in_full_synonym_set(
    source: str,
    bad_output: str,
    supported_output: str,
    may_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [may_output, bad_output, supported_output],
        )
    validate_synonym_source_evidence(
        source,
        [may_output, supported_output],
    )


GOVERNMENT_TERRITORIAL_IDENTITY_SOURCE = (
    "Правительство штата Сан Андреас может отозвать пропуск. "
    "Правительство штата Нью Йорк отозвало пропуск."
)
GOVERNMENT_TERRITORIAL_BAD_OUTPUT = (
    "пропуск отозвало правительство штата Сан Андреас"
)
GOVERNMENT_TERRITORIAL_SUPPORTED_OUTPUT = (
    "пропуск отозвало правительство штата Нью Йорк"
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_government_territorial_identity_is_not_consumed_by_family_head(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            GOVERNMENT_TERRITORIAL_IDENTITY_SOURCE,
            GOVERNMENT_TERRITORIAL_BAD_OUTPUT,
        )
    validate(
        GOVERNMENT_TERRITORIAL_IDENTITY_SOURCE,
        GOVERNMENT_TERRITORIAL_SUPPORTED_OUTPUT,
    )


def test_government_territory_cannot_hide_in_full_synonym_set() -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            GOVERNMENT_TERRITORIAL_IDENTITY_SOURCE,
            [
                GOVERNMENT_TERRITORIAL_BAD_OUTPUT,
                GOVERNMENT_TERRITORIAL_SUPPORTED_OUTPUT,
            ],
        )


GOVERNMENT_DIRECT_IDENTITY_POOLING_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        (
            "Правительство Альфа может отозвать пропуск. "
            "Правительство Бета отозвало пропуск."
        ),
        "пропуск отозвало правительство Альфа",
        "пропуск отозвало правительство Бета",
    ),
    (
        (
            "Парламент Альфа может отозвать пропуск. "
            "Парламент Бета отозвал пропуск."
        ),
        "пропуск отозвал парламент Альфа",
        "пропуск отозвал парламент Бета",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    GOVERNMENT_DIRECT_IDENTITY_POOLING_CASES,
)
def test_government_direct_identity_cannot_pool_source_truths(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)
    validate(source, supported_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    GOVERNMENT_DIRECT_IDENTITY_POOLING_CASES,
)
def test_government_direct_identity_cannot_hide_in_full_synonym_set(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize("family", ("правительство", "парламент"))
@pytest.mark.parametrize(
    "invented_tail",
    (
        "Альфа",
        "«Альфа»",
        "«Альфа",
        "Альфа Бета Гамма Дельта Эпсилон Дзета Эта",
    ),
)
def test_generic_government_cannot_gain_direct_identity(
    validate: RelationValidator,
    family: str,
    invented_tail: str,
) -> None:
    with pytest.raises(ValueError):
        validate(
            f"{family.capitalize()} может отозвать пропуск.",
            f"пропуск отозвало {family} {invented_tail}",
        )


@pytest.mark.parametrize("family", ("правительство", "парламент"))
@pytest.mark.parametrize(
    "invented_tail",
    (
        "Альфа",
        "«Альфа»",
        "«Альфа",
        "Альфа Бета Гамма Дельта Эпсилон Дзета Эта",
    ),
)
def test_government_identity_invention_cannot_hide_in_full_synonym_set(
    family: str,
    invented_tail: str,
) -> None:
    source = f"{family.capitalize()} может отозвать пропуск."
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [
                f"пропуск отозвало {family} {invented_tail}",
                f"{family} может отозвать пропуск",
            ],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_generic_government_cannot_gain_named_territory(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Правительство штата отозвало пропуск.",
            GOVERNMENT_TERRITORIAL_BAD_OUTPUT,
        )


@pytest.mark.parametrize(
    ("source", "expected_actor"),
    (
            (
                "Пропуск отозвало правительство штата Сан Андреас.",
                "правительство штат сан андреас",
        ),
        (
            "Пропуск отозвал парламент штата Нью Йорк.",
            "парламент штат нью йорк",
        ),
            (
                "Пропуск отозвало правительство штата.",
                "правительство штат",
        ),
    ),
)
def test_postposed_government_keeps_named_or_bare_territory(
    source: str,
    expected_actor: str,
) -> None:
    actors = {
        truth.operative_actor
        for truth in _extract_relation_truths(source)
        if truth.predicate == "regulated_revocation"
    }
    assert expected_actor in actors


POSTPOSED_PREPOSITIONAL_ADJUNCT_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал при этом суд штата Сан Андреас",
        "пропуск отозвал при этом суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал по итогам проверки суд штата Сан Андреас",
        "пропуск отозвал по итогам проверки суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал в тот же день суд штата Сан Андреас",
        "пропуск отозвал в тот же день суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал у входа суд штата Сан Андреас",
        "пропуск отозвал у входа суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал после проверки суд штата Сан Андреас",
        "пропуск отозвал после проверки суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал согласно протоколу суд штата Сан Андреас",
        "пропуск отозвал согласно протоколу суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал на заседании суд штата Сан Андреас",
        "пропуск отозвал на заседании суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал через представителя суд штата Сан Андреас",
        "пропуск отозвал через представителя суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал для проверки суд штата Сан Андреас",
        "пропуск отозвал для проверки суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал с участием суд штата Сан Андреас",
        "пропуск отозвал с участием суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        (
            "пропуск отозвал по итогам очень долгой тщательной "
            "внутренней служебной проверки суд штата Сан Андреас"
        ),
        (
            "пропуск отозвал по итогам очень долгой тщательной "
            "внутренней служебной проверки суд округа Блейн Каунти"
        ),
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[0][0],
        "пропуск отозвал по итогам проверки, суд штата Сан Андреас",
        "пропуск отозвал по итогам проверки, суд округа Блейн Каунти",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[2][0],
        "пропуск отозвала при этом организация Красный Крест",
        "пропуск отозвала при этом организация Синий Щит",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[3][0],
        "пропуск отозвала при этом организация «Альфа»",
        "пропуск отозвала при этом организация «Бета»",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[2][0],
        "пропуск отозвала на заседании организация Красный Крест",
        "пропуск отозвала на заседании организация Синий Щит",
    ),
    (
        POSTPOSED_COMPLETE_IDENTITY_POOLING_CASES[2][0],
        "пропуск отозвала по итогам проверки, организация Красный Крест",
        "пропуск отозвала по итогам проверки, организация Синий Щит",
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск при участии полиции."
        ),
        (
            "пропуск отозвала при участии полиции именно организация "
            "Красный Крест"
        ),
        (
            "пропуск отозвала при участии полиции именно организация "
            "Синий Щит"
        ),
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск у Министра."
        ),
        "пропуск отозвала у Министра именно организация Красный Крест",
        "пропуск отозвала у Министра именно организация Синий Щит",
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск согласно решению "
            "правительства."
        ),
        (
            "пропуск отозвала согласно решению правительства именно "
            "организация Красный Крест"
        ),
        (
            "пропуск отозвала согласно решению правительства именно "
            "организация Синий Щит"
        ),
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск в присутствии "
            "губернатора."
        ),
        (
            "пропуск отозвала в присутствии губернатора именно организация "
            "Красный Крест"
        ),
        (
            "пропуск отозвала в присутствии губернатора именно организация "
            "Синий Щит"
        ),
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск через представителя "
            "полиции."
        ),
        (
            "пропуск отозвала через представителя полиции именно организация "
            "Красный Крест"
        ),
        (
            "пропуск отозвала через представителя полиции именно организация "
            "Синий Щит"
        ),
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск при участии FIB."
        ),
        (
            "пропуск отозвала при участии FIB именно организация "
            "Красный Крест"
        ),
        (
            "пропуск отозвала при участии FIB именно организация "
            "Синий Щит"
        ),
    ),
    (
        (
            "Организация Красный Крест может отозвать пропуск. "
            "Организация Синий Щит отозвала пропуск через представителя "
            "LSPD."
        ),
        (
            "пропуск отозвала через представителя LSPD именно организация "
            "Красный Крест"
        ),
        (
            "пропуск отозвала через представителя LSPD именно организация "
            "Синий Щит"
        ),
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    POSTPOSED_PREPOSITIONAL_ADJUNCT_CASES,
)
def test_complete_prepositional_adjunct_does_not_erase_postposed_actor(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)
    validate(source, supported_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    POSTPOSED_PREPOSITIONAL_ADJUNCT_CASES,
)
def test_prepositional_adjunct_actor_cannot_hide_in_full_synonym_set(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_prepositional_adjunct_cannot_enable_specific_actor_invention(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Организация отозвала пропуск.",
            "пропуск отозвал в тот же день суд штата Сан Андреас",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "unsafe_output",
    (
        "суд отозвал пропуск",
        "пропуск отозвал суд",
    ),
)
def test_decision_authority_cannot_become_operative_actor(
    validate: RelationValidator,
    unsafe_output: str,
) -> None:
    source = "Организация отозвала пропуск по решению суда."
    with pytest.raises(ValueError):
        validate(source, unsafe_output)
    validate(source, "организация отозвала пропуск по решению суда")


def test_decision_actor_inversion_cannot_hide_in_full_synonym_set() -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            "Организация отозвала пропуск по решению суда.",
            [
                "суд отозвал пропуск",
                "организация отозвала пропуск по решению суда",
            ],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_unrelated_decision_alternative_cannot_unlock_role_inversion(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация отозвала пропуск по решению суда. "
        "Лицензия может быть отозвана самостоятельно или по решению "
        "комиссии."
    )
    with pytest.raises(ValueError):
        validate(source, "суд отозвал пропуск")


def test_unrelated_decision_alternative_cannot_unlock_full_synonym_set() -> None:
    source = (
        "Организация отозвала пропуск по решению суда. "
        "Лицензия может быть отозвана самостоятельно или по решению "
        "комиссии."
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [
                "суд отозвал пропуск",
                "организация отозвала пропуск по решению суда",
            ],
        )


PASSIVE_MANAGEMENT_UNIT_POOLING_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        (
            "Пропуск может быть отозван начальником отдела кадров. "
            "Пропуск был отозван начальником отдела безопасности."
        ),
        "пропуск был отозван начальником отдела кадров",
        "пропуск был отозван начальником отдела безопасности",
    ),
    (
        (
            "Пропуск может быть отозван директором департамента кадров. "
            "Пропуск был отозван директором департамента безопасности."
        ),
        "пропуск был отозван директором департамента кадров",
        "пропуск был отозван директором департамента безопасности",
    ),
    (
        (
            "Пропуск может быть отозван директором департамента по кадрам. "
            "Пропуск был отозван директором департамента по безопасности."
        ),
        "пропуск был отозван директором департамента по кадрам",
        "пропуск был отозван директором департамента по безопасности",
    ),
    (
        (
            "Пропуск может быть отозван директором кадрового департамента. "
            "Пропуск был отозван директором правового департамента."
        ),
        "пропуск был отозван директором кадрового департамента",
        "пропуск был отозван директором правового департамента",
    ),
    (
        (
            "Пропуск может быть отозван руководителем государственной "
            "организации. Пропуск был отозван руководителем частной "
            "организации."
        ),
        "пропуск был отозван руководителем государственной организации",
        "пропуск был отозван руководителем частной организации",
    ),
    (
        (
            "Пропуск может быть отозван руководителем государственной "
            "организации. Пропуск был отозван директором частной "
            "организации."
        ),
        "пропуск был отозван руководителем государственной организации",
        "пропуск был отозван директором частной организации",
    ),
    (
        (
            "Пропуск может быть отозван директором департамента по работе "
            "с кадрами. Пропуск был отозван директором департамента по "
            "работе с клиентами."
        ),
        "пропуск был отозван директором департамента по работе с кадрами",
        (
            "пропуск был отозван директором департамента по работе "
            "с клиентами"
        ),
    ),
    (
        (
            "Пропуск может быть отозван начальником отдела кадров и "
            "обучения. Пропуск был отозван начальником отдела кадров и "
            "безопасности."
        ),
        "пропуск был отозван начальником отдела кадров и обучения",
        "пропуск был отозван начальником отдела кадров и безопасности",
    ),
    (
        (
            "Пропуск может быть отозван директором главного центрального "
            "регионального кадрового департамента. Пропуск был отозван "
            "директором главного центрального регионального правового "
            "департамента."
        ),
        (
            "пропуск был отозван директором главного центрального "
            "регионального кадрового департамента"
        ),
        (
            "пропуск был отозван директором главного центрального "
            "регионального правового департамента"
        ),
    ),
    (
        (
            "Пропуск может быть отозван руководителем крупной "
            "государственной центральной региональной организации. "
            "Пропуск был отозван руководителем крупной частной центральной "
            "региональной организации."
        ),
        (
            "пропуск был отозван руководителем крупной государственной "
            "центральной региональной организации"
        ),
        (
            "пропуск был отозван руководителем крупной частной центральной "
            "региональной организации"
        ),
    ),
    (
        (
            "Пропуск может быть отозван руководителем государственной "
            "организации Альфа. Пропуск был отозван руководителем "
            "государственной организации Бета."
        ),
        (
            "пропуск был отозван руководителем государственной "
            "организации Альфа"
        ),
        (
            "пропуск был отозван руководителем государственной "
            "организации Бета"
        ),
    ),
    (
        (
            "Пропуск может быть отозван руководителем государственной "
            "организации «Альфа». Пропуск был отозван руководителем "
            "государственной организации «Бета»."
        ),
        (
            "пропуск был отозван руководителем государственной "
            "организации «Альфа»"
        ),
        (
            "пропуск был отозван руководителем государственной "
            "организации «Бета»"
        ),
    ),
    (
        (
            "Пропуск может быть отозван руководителем государственной "
            "организации имени Альфа. Пропуск был отозван руководителем "
            "государственной организации имени Бета."
        ),
        (
            "пропуск был отозван руководителем государственной "
            "организации имени Альфа"
        ),
        (
            "пропуск был отозван руководителем государственной "
            "организации имени Бета"
        ),
    ),
    (
        (
            "Пропуск может быть отозван руководителем организации Рога и "
            "Копыта. Пропуск был отозван руководителем организации Рога и "
            "Крылья."
        ),
        (
            "пропуск был отозван руководителем организации Рога и Копыта"
        ),
        (
            "пропуск был отозван руководителем организации Рога и Крылья"
        ),
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    PASSIVE_MANAGEMENT_UNIT_POOLING_CASES,
)
def test_passive_management_unit_identity_cannot_pool_source_truths(
    validate: RelationValidator,
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)
    validate(source, supported_output)


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output"),
    PASSIVE_MANAGEMENT_UNIT_POOLING_CASES,
)
def test_management_unit_identity_cannot_hide_in_full_synonym_set(
    source: str,
    bad_output: str,
    supported_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [bad_output, supported_output],
        )


MANAGEMENT_IDENTITY_INVENTION_CASES = (
    "руководителем государственной организации «Альфа",
    (
        "директором первого второго третьего четвертого пятого шестого "
        "седьмого департамента"
    ),
    (
        "руководителем первой второй третьей четвертой пятой шестой "
        "седьмой организации"
    ),
    (
        "директором первого второго третьего четвертого пятого шестого "
        "седьмого восьмого девятого десятого одиннадцатого двенадцатого "
        "тринадцатого департамента"
    ),
    (
        "руководителем первой второй третьей четвертой пятой шестой "
        "седьмой восьмой девятой десятой одиннадцатой двенадцатой "
        "тринадцатой организации"
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize("invented_actor", MANAGEMENT_IDENTITY_INVENTION_CASES)
def test_generic_manager_cannot_gain_unbounded_or_malformed_identity(
    validate: RelationValidator,
    invented_actor: str,
) -> None:
    source = "Пропуск был отозван руководителем."
    with pytest.raises(ValueError):
        validate(source, f"пропуск был отозван {invented_actor}")


@pytest.mark.parametrize("invented_actor", MANAGEMENT_IDENTITY_INVENTION_CASES)
def test_invalid_management_identity_cannot_hide_in_full_synonym_set(
    invented_actor: str,
) -> None:
    source = "Пропуск был отозван руководителем."
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [
                f"пропуск был отозван {invented_actor}",
                "пропуск был отозван руководителем",
            ],
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_unresolved_unrelated_issue_does_not_poison_proven_revocation(
    validate: RelationValidator,
) -> None:
    source = (
        "После третьего предупреждения лицензия становится "
        "недействительной. Лицензия должна быть оформлена заново у "
        "Министра Финансов штата SA."
    )
    validate(source, "третье предупреждение аннулирует лицензию")
    with pytest.raises(ValueError):
        validate(source, "третье предупреждение аннулирует бизнес-лицензию")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_unresolved_same_relation_actor_remains_fail_closed(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Лицензия должна быть оформлена организацией «Альфа.",
            "организация Альфа должна оформить лицензию",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_exact_management_unit_survives_passive_active_case_change(
    validate: RelationValidator,
) -> None:
    validate(
        "Пропуск был отозван начальником отдела безопасности.",
        "начальник отдела безопасности отозвал пропуск",
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "output",
    (
        "руководитель государственной организации может отозвать пропуск",
        "пропуск может отозвать руководитель государственной организации",
    ),
)
def test_scoped_management_actor_survives_passive_issuer_paraphrase(
    validate: RelationValidator,
    output: str,
) -> None:
    validate(
        (
            "Пропуск, выданный руководителем государственной организации, "
            "может быть им отозван."
        ),
        output,
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_generic_management_actor_cannot_gain_organization_scope(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Руководитель может отозвать пропуск.",
            (
                "руководитель государственной организации может "
                "отозвать пропуск"
            ),
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_object_first_mandatory_management_unit_keeps_scope(
    validate: RelationValidator,
) -> None:
    source = (
        "Директор департамента кадров может отозвать лицензию. "
        "Директор департамента безопасности обязан отозвать лицензию."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "лицензию обязан отозвать директор департамента кадров",
        )
    validate(
        source,
        "лицензию обязан отозвать директор департамента безопасности",
    )


def test_mandatory_management_unit_cannot_hide_in_full_synonym_set() -> None:
    source = (
        "Директор департамента кадров может отозвать лицензию. "
        "Директор департамента безопасности обязан отозвать лицензию."
    )
    may_output = "директор департамента кадров может отозвать лицензию"
    bad_output = "лицензию обязан отозвать директор департамента кадров"
    supported_output = (
        "лицензию обязан отозвать директор департамента безопасности"
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [may_output, bad_output, supported_output],
        )
    validate_synonym_source_evidence(
        source,
        [may_output, supported_output],
    )


@pytest.mark.parametrize(
    "value",
    (
        "у Министра",
        "у нового Министра",
        "по решению суда",
        "при участии полиции",
    ),
)
def test_pure_prepositional_role_is_not_a_postposed_actor(value: str) -> None:
    assert _relation_complete_postposed_actor_key(value) is None


def test_prepositional_actor_uses_character_not_token_bound() -> None:
    actor = _relation_complete_postposed_actor_key(
        "по итогам "
        + " ".join(f"проверки{index}" for index in range(17))
        + " суд штата Сан Андреас"
    )
    assert actor == "суд штат сан андреас"

    overbound = _relation_complete_postposed_actor_key(
        "по итогам "
        + " ".join(f"проверки{index}" for index in range(100))
        + " суд штата Сан Андреас"
    )
    assert overbound is not None
    assert overbound.startswith("__unresolved_actor__:")


@pytest.mark.parametrize(
    ("source", "bad_output", "supported_output", "may_output"),
    MANDATORY_POSTPOSED_ACTOR_CASES,
)
def test_mandatory_actor_survives_prepositional_adjunct(
    source: str,
    bad_output: str,
    supported_output: str,
    may_output: str,
) -> None:
    bad_prefixed = bad_output.replace(
        "отозвать ",
        "отозвать при этом ",
        1,
    )
    supported_prefixed = supported_output.replace(
        "отозвать ",
        "отозвать при этом ",
        1,
    )
    with pytest.raises(ValueError):
        _validate_title(source, bad_prefixed)
    _validate_title(source, supported_prefixed)
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [may_output, bad_prefixed, supported_prefixed],
        )
    validate_synonym_source_evidence(
        source,
        [may_output, supported_prefixed],
    )


# Final native-v3 independent-review matrix.  Keep this section isolated from
# the historical regressions above: every row is a production-length boundary
# that was discovered only after the previously green E1FD candidate froze.
FINAL_V3_IDENTITY_POOLING_CASES: tuple[
    tuple[str, str, str, str, str], ...
] = (
    (
        "management-soft-ot",
        (
            "Пропуск может быть отозван начальником отдела по защите от "
            "угроз. Пропуск был отозван начальником отдела по защите от "
            "рисков."
        ),
        "пропуск был отозван начальником отдела по защите от угроз",
        "пропуск был отозван начальником отдела по защите от рисков",
        "пропуск может быть отозван начальником отдела по защите от угроз",
    ),
    (
        "management-soft-pri",
        (
            "Пропуск может быть отозван директором департамента по контролю "
            "при губернаторе. Пропуск был отозван директором департамента "
            "по контролю при парламенте."
        ),
        (
            "пропуск был отозван директором департамента по контролю "
            "при губернаторе"
        ),
        (
            "пропуск был отозван директором департамента по контролю "
            "при парламенте"
        ),
        (
            "пропуск может быть отозван директором департамента по контролю "
            "при губернаторе"
        ),
    ),
    (
        "management-soft-posle",
        (
            "Пропуск может быть отозван начальником отдела контроля после "
            "проверки. Пропуск был отозван начальником отдела контроля "
            "после аудита."
        ),
        "пропуск был отозван начальником отдела контроля после проверки",
        "пропуск был отозван начальником отдела контроля после аудита",
        "пропуск может быть отозван начальником отдела контроля после проверки",
    ),
    (
        "management-soft-do",
        (
            "Пропуск может быть отозван начальником отдела допуска до "
            "объектов. Пропуск был отозван начальником отдела допуска до "
            "архивов."
        ),
        "пропуск был отозван начальником отдела допуска до объектов",
        "пропуск был отозван начальником отдела допуска до архивов",
        "пропуск может быть отозван начальником отдела допуска до объектов",
    ),
    (
        "management-fifth-connector",
        (
            "Пропуск может быть отозван директором отдела по работе с "
            "кадрами в городе при губернаторе для комиссии. Пропуск был "
            "отозван директором отдела по работе с кадрами в городе при "
            "губернаторе для парламента."
        ),
        (
            "пропуск был отозван директором отдела по работе с кадрами "
            "в городе при губернаторе для комиссии"
        ),
        (
            "пропуск был отозван директором отдела по работе с кадрами "
            "в городе при губернаторе для парламента"
        ),
        (
            "пропуск может быть отозван директором отдела по работе с "
            "кадрами в городе при губернаторе для комиссии"
        ),
    ),
    (
        "management-org-fourth-connector",
        (
            "Пропуск может быть отозван руководителем организации Центр по "
            "работе с кадрами при губернаторе для комиссии. Пропуск был "
            "отозван руководителем организации Центр по работе с кадрами "
            "при губернаторе для парламента."
        ),
        (
            "пропуск был отозван руководителем организации Центр по работе "
            "с кадрами при губернаторе для комиссии"
        ),
        (
            "пропуск был отозван руководителем организации Центр по работе "
            "с кадрами при губернаторе для парламента"
        ),
        (
            "пропуск может быть отозван руководителем организации Центр по "
            "работе с кадрами при губернаторе для комиссии"
        ),
    ),
    (
        "direct-org-and",
        (
            "Пропуск может быть отозван организацией Рога и Копыта. "
            "Пропуск был отозван организацией Рога и Крылья."
        ),
        "пропуск был отозван организацией Рога и Копыта",
        "пропуск был отозван организацией Рога и Крылья",
        "пропуск может быть отозван организацией Рога и Копыта",
    ),
    (
        "direct-court-territory",
        (
            "Пропуск может быть отозван судом округа Блейн в городе Палето. "
            "Пропуск был отозван судом округа Блейн в городе Дэвис."
        ),
        "пропуск был отозван судом округа Блейн в городе Палето",
        "пропуск был отозван судом округа Блейн в городе Дэвис",
        "пропуск может быть отозван судом округа Блейн в городе Палето",
    ),
    (
        "direct-police-connector",
        (
            "Пропуск может быть отозван полицией города Лос Сантос и округа "
            "Блейн. Пропуск был отозван полицией города Лос Сантос и округа "
            "Дэвис."
        ),
        "пропуск был отозван полицией города Лос Сантос и округа Блейн",
        "пропуск был отозван полицией города Лос Сантос и округа Дэвис",
        "пропуск может быть отозван полицией города Лос Сантос и округа Блейн",
    ),
    (
        "direct-org-center-prep",
        (
            "Пропуск может быть отозван организацией Центр по защите "
            "граждан. Пропуск был отозван организацией Центр при губернаторе."
        ),
        "пропуск был отозван организацией Центр по защите граждан",
        "пропуск был отозван организацией Центр при губернаторе",
        "пропуск может быть отозван организацией Центр по защите граждан",
    ),
    (
        "direct-org-house-prep",
        (
            "Пропуск может быть отозван организацией Дом в Лесу. Пропуск "
            "был отозван организацией Дом с Башней."
        ),
        "пропуск был отозван организацией Дом в Лесу",
        "пропуск был отозван организацией Дом с Башней",
        "пропуск может быть отозван организацией Дом в Лесу",
    ),
    (
        "direct-lspd-prep",
        (
            "Пропуск может быть отозван LSPD по работе с гражданами. "
            "Пропуск был отозван LSPD по работе с кадрами."
        ),
        "пропуск был отозван LSPD по работе с гражданами",
        "пропуск был отозван LSPD по работе с кадрами",
        "пропуск может быть отозван LSPD по работе с гражданами",
    ),
    (
        "unknown-ascii-dea",
        (
            "DEA Alpha может отозвать пропуск. DEA Beta отозвал пропуск."
        ),
        "пропуск отозвал DEA Alpha",
        "пропуск отозвал DEA Beta",
        "DEA Alpha может отозвать пропуск",
    ),
    (
        "balanced-parenthetical",
        (
            "LSPD (Alpha) может отозвать пропуск. LSPD (Beta) отозвал "
            "пропуск."
        ),
        "пропуск отозвал LSPD (Alpha)",
        "пропуск отозвал LSPD (Beta)",
        "LSPD (Alpha) может отозвать пропуск",
    ),
)


@pytest.mark.parametrize(
    ("case_id", "source", "unsafe_past", "honest_past", "honest_may"),
    FINAL_V3_IDENTITY_POOLING_CASES,
    ids=[case[0] for case in FINAL_V3_IDENTITY_POOLING_CASES],
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_complete_identity_cannot_pool_source_truths(
    case_id: str,
    source: str,
    unsafe_past: str,
    honest_past: str,
    honest_may: str,
    validate: RelationValidator,
) -> None:
    del case_id
    with pytest.raises(ValueError):
        validate(source, unsafe_past)
    validate(source, honest_past)
    validate(source, honest_may)


@pytest.mark.parametrize(
    ("case_id", "source", "unsafe_past", "honest_past", "honest_may"),
    FINAL_V3_IDENTITY_POOLING_CASES,
    ids=[case[0] for case in FINAL_V3_IDENTITY_POOLING_CASES],
)
def test_final_v3_identity_cannot_hide_in_full_synonym_set(
    case_id: str,
    source: str,
    unsafe_past: str,
    honest_past: str,
    honest_may: str,
) -> None:
    del case_id
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [honest_may, unsafe_past, honest_past],
        )
    validate_synonym_source_evidence(source, [honest_may, honest_past])


FINAL_V3_IDENTITY_INVENTION_CASES: tuple[
    tuple[str, str, str, str], ...
] = (
    (
        "balanced-parenthetical",
        "Пропуск отозвал LSPD.",
        "пропуск отозвал LSPD (Alpha)",
        "пропуск отозвал LSPD",
    ),
    (
        "malformed-parenthetical",
        "Пропуск отозвал LSPD.",
        "пропуск отозвал LSPD (",
        "пропуск отозвал LSPD",
    ),
    (
        "unknown-ascii",
        "DEA отозвал пропуск.",
        "пропуск отозвал DEA Alpha",
        "пропуск отозвал DEA",
    ),
    (
        "direct-connector-name",
        "Пропуск был отозван организацией.",
        "пропуск был отозван организацией Дом в Лесу",
        "пропуск был отозван организацией",
    ),
    (
        "management-connector-name",
        "Пропуск был отозван руководителем организации.",
        (
            "пропуск был отозван руководителем организации Центр по работе "
            "с кадрами при губернаторе для комиссии"
        ),
        "пропуск был отозван руководителем организации",
    ),
)


@pytest.mark.parametrize(
    ("case_id", "source", "unsafe", "honest"),
    FINAL_V3_IDENTITY_INVENTION_CASES,
    ids=[case[0] for case in FINAL_V3_IDENTITY_INVENTION_CASES],
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_generic_actor_cannot_gain_identity(
    case_id: str,
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    del case_id
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


@pytest.mark.parametrize(
    ("case_id", "source", "unsafe", "honest"),
    FINAL_V3_IDENTITY_INVENTION_CASES,
    ids=[case[0] for case in FINAL_V3_IDENTITY_INVENTION_CASES],
)
def test_final_v3_identity_invention_cannot_hide_in_full_set(
    case_id: str,
    source: str,
    unsafe: str,
    honest: str,
) -> None:
    del case_id
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [unsafe, honest])
    validate_synonym_source_evidence(source, [honest])


FINAL_V3_OBLIQUE_ROLE_CASES: tuple[
    tuple[str, str, str, str, str], ...
] = (
    ("support-fib", "revoke", "при поддержке FIB", "FIB", "отозвал"),
    ("request-usss", "issue", "по запросу USSS", "USSS", "выдал"),
    ("plea-sang", "revoke", "по просьбе SANG", "SANG", "отозвал"),
    ("initiative-lspd", "issue", "по инициативе LSPD", "LSPD", "выдал"),
    ("instruction-lssd", "revoke", "по указанию LSSD", "LSSD", "отозвал"),
    ("permission-lscsd", "issue", "с разрешения LSCSD", "LSCSD", "выдал"),
    (
        "consent-named-fib",
        "revoke",
        "с согласия FIB Alpha",
        "FIB Alpha",
        "отозвал",
    ),
    (
        "intermediary-named-usss",
        "issue",
        "через посредника USSS Alpha",
        "USSS Alpha",
        "выдал",
    ),
    (
        "representative-named-sang",
        "revoke",
        "через представителя SANG Alpha",
        "SANG Alpha",
        "отозвал",
    ),
    (
        "participation-named-lspd",
        "issue",
        "при участии LSPD Alpha",
        "LSPD Alpha",
        "выдал",
    ),
    (
        "control-named-lssd",
        "revoke",
        "при контроле LSSD Alpha",
        "LSSD Alpha",
        "отозвал",
    ),
    (
        "funding-named-lscsd",
        "issue",
        "при финансировании LSCSD Alpha",
        "LSCSD Alpha",
        "выдал",
    ),
    (
        "interests-coordinated",
        "revoke",
        "в интересах FIB и USSS",
        "FIB и USSS",
        "отозвали",
    ),
    (
        "on-behalf-coordinated",
        "issue",
        "от имени SANG и LSPD",
        "SANG и LSPD",
        "выдали",
    ),
    (
        "assignment-coordinated",
        "revoke",
        "по поручению LSSD и LSCSD",
        "LSSD и LSCSD",
        "отозвали",
    ),
    (
        "according-coordinated",
        "issue",
        "согласно решению FIB и LSCSD",
        "FIB и LSCSD",
        "выдали",
    ),
)


def _final_v3_oblique_source(
    kind: str,
    layout: str,
    role: str,
) -> tuple[str, str, str]:
    if kind == "issue":
        unsafe_object = "лицензию"
        if layout == "postposed":
            source = f"Лицензию выдала {role} организация Бета."
            honest = source.removesuffix(".")
        elif layout == "subject-first":
            source = f"Организация Бета выдала лицензию {role}."
            honest = source.removesuffix(".")
        else:
            source = f"Лицензия должна выдаваться {role}."
            honest = source.removesuffix(".")
    else:
        unsafe_object = "пропуск"
        if layout == "postposed":
            source = f"Пропуск отозвала {role} организация Бета."
            honest = source.removesuffix(".")
        elif layout == "subject-first":
            source = f"Организация Бета отозвала пропуск {role}."
            honest = source.removesuffix(".")
        else:
            source = f"Пропуск отзывается {role}."
            honest = source.removesuffix(".")
    return source, honest, unsafe_object


@pytest.mark.parametrize(
    ("case_id", "kind", "role", "unsafe_actor", "unsafe_verb"),
    FINAL_V3_OBLIQUE_ROLE_CASES,
    ids=[case[0] for case in FINAL_V3_OBLIQUE_ROLE_CASES],
)
@pytest.mark.parametrize(
    "layout",
    ("postposed", "subject-first", "actorless-legacy"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_oblique_role_cannot_become_operative_actor(
    case_id: str,
    kind: str,
    role: str,
    unsafe_actor: str,
    unsafe_verb: str,
    layout: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source, honest, unsafe_object = _final_v3_oblique_source(
        kind,
        layout,
        role,
    )
    unsafe = f"{unsafe_actor} {unsafe_verb} {unsafe_object}"
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)

    if layout != "actorless-legacy":
        predicate = (
            "regulated_issue" if kind == "issue" else "regulated_revocation"
        )
        actors = {
            truth.operative_actor
            for truth in _extract_relation_truths(source)
            if truth.predicate == predicate
        }
        assert "организаци бет" in actors
        assert _relation_actor_key(
            unsafe_actor,
            preserve_complete=True,
        ) not in actors


@pytest.mark.parametrize(
    ("case_id", "kind", "role", "unsafe_actor", "unsafe_verb"),
    FINAL_V3_OBLIQUE_ROLE_CASES,
    ids=[case[0] for case in FINAL_V3_OBLIQUE_ROLE_CASES],
)
@pytest.mark.parametrize(
    "layout",
    ("postposed", "subject-first", "actorless-legacy"),
)
def test_final_v3_oblique_role_cannot_hide_in_full_synonym_set(
    case_id: str,
    kind: str,
    role: str,
    unsafe_actor: str,
    unsafe_verb: str,
    layout: str,
) -> None:
    del case_id
    source, honest, unsafe_object = _final_v3_oblique_source(
        kind,
        layout,
        role,
    )
    unsafe = f"{unsafe_actor} {unsafe_verb} {unsafe_object}"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [unsafe, honest])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "output"),
    (
        ("Лицензия должна выдаваться FIB.", "FIB должен выдать лицензию"),
        ("Пропуск отзывается FIB.", "FIB отзывает пропуск"),
        (
            "Лицензия должна выдаваться FIB Alpha.",
            "FIB Alpha должен выдать лицензию",
        ),
        (
            "FIB совместно с USSS выдали лицензию.",
            "FIB совместно с USSS выдали лицензию",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_real_ascii_actor_remains_supported(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_unrelated_malformed_same_core_does_not_poison_topical_claim(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть отозван судом. "
        "Пропуск отозван организацией «Альфа."
    )
    validate(source, "отзыв пропуска судом")
    with pytest.raises(ValueError):
        validate(source, "организация Альфа отозвала пропуск")


def test_final_v3_unrelated_malformed_same_core_full_set_is_local() -> None:
    source = (
        "Пропуск может быть отозван судом. "
        "Пропуск отозван организацией «Альфа."
    )
    honest = "отзыв пропуска судом"
    unsafe = "организация Альфа отозвала пропуск"
    validate_synonym_source_evidence(source, [honest])
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [unsafe, honest])


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "LSPD отозвал пропуск.",
            "LSPD ( отозвал пропуск",
            "LSPD отозвал пропуск",
        ),
        (
            "Руководитель отозвал пропуск.",
            "Руководитель (организации Альфа отозвал пропуск",
            "Руководитель отозвал пропуск",
        ),
        (
            "DEA отозвал пропуск.",
            "DEA Alpha отозвал пропуск",
            "DEA отозвал пропуск",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_active_identity_is_also_fail_closed(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "Лицензия должна выдаваться.",
            "FIB должен выдать лицензию",
            "лицензия должна выдаваться",
        ),
        (
            "Пропуск отзывается.",
            "DEA отзывает пропуск",
            "пропуск отзывается",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_actorless_source_cannot_gain_ascii_actor(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


@pytest.mark.parametrize(
    "value",
    (
        "при поддержке FIB",
        "по запросу DEA Alpha",
        "с разрешения LSCSD",
        "alpha",
    ),
)
def test_final_v3_oblique_or_lowercase_ascii_is_not_postposed_actor(
    value: str,
) -> None:
    assert _relation_complete_postposed_actor_key(value) is None


# Native-v3 delimiter/attachment rereview matrix.  These are shared parser
# boundaries, never server/article exceptions: a may/past fact may only pool
# when the complete institutional identity is the same.
FINAL_V3_DELIMITED_IDENTITY_POOLING_CASES: tuple[
    tuple[str, str, str, str, str], ...
] = (
    (
        "quoted-external-words",
        (
            "Пропуск может быть отозван организацией «Центр по решению конфликтов». "
            "Пропуск был отозван организацией «Центр по решению споров»."
        ),
        "пропуск был отозван организацией «Центр по решению конфликтов»",
        "пропуск был отозван организацией «Центр по решению споров»",
        "пропуск может быть отозван организацией «Центр по решению конфликтов»",
    ),
    (
        "parenthesized-external-words",
        (
            "Пропуск может быть отозван организацией (Центр по решению конфликтов). "
            "Пропуск был отозван организацией (Центр по решению споров)."
        ),
        "пропуск был отозван организацией (Центр по решению конфликтов)",
        "пропуск был отозван организацией (Центр по решению споров)",
        "пропуск может быть отозван организацией (Центр по решению конфликтов)",
    ),
    (
        "unquoted-organization-connector",
        (
            "Пропуск может быть отозван организацией Центр по разрешению споров Альфа. "
            "Пропуск был отозван организацией Центр по разрешению споров Бета."
        ),
        "пропуск был отозван организацией Центр по разрешению споров Альфа",
        "пропуск был отозван организацией Центр по разрешению споров Бета",
        "пропуск может быть отозван организацией Центр по разрешению споров Альфа",
    ),
    (
        "unquoted-court-connector",
        (
            "Пропуск может быть отозван судом по разрешению споров Альфа. "
            "Пропуск был отозван судом по разрешению споров Бета."
        ),
        "пропуск был отозван судом по разрешению споров Альфа",
        "пропуск был отозван судом по разрешению споров Бета",
        "пропуск может быть отозван судом по разрешению споров Альфа",
    ),
    (
        "attached-ascii-hyphen",
        (
            "DEA-Alpha может отозвать пропуск. "
            "DEA-Beta отозвал пропуск."
        ),
        "DEA-Alpha отозвал пропуск",
        "DEA-Beta отозвал пропуск",
        "DEA-Alpha может отозвать пропуск",
    ),
    (
        "attached-ascii-dot",
        (
            "DEA.Alpha может отозвать пропуск. "
            "DEA.Beta отозвал пропуск."
        ),
        "DEA.Alpha отозвал пропуск",
        "DEA.Beta отозвал пропуск",
        "DEA.Alpha может отозвать пропуск",
    ),
    (
        "attached-russian-hyphen",
        (
            "Организация-Альфа может отозвать пропуск. "
            "Организация-Бета отозвала пропуск."
        ),
        "Организация-Альфа отозвала пропуск",
        "Организация-Бета отозвала пропуск",
        "Организация-Альфа может отозвать пропуск",
    ),
    (
        "balanced-single-quotes",
        (
            "LSPD 'Alpha' может отозвать пропуск. "
            "LSPD 'Beta' отозвал пропуск."
        ),
        "LSPD 'Alpha' отозвал пропуск",
        "LSPD 'Beta' отозвал пропуск",
        "LSPD 'Alpha' может отозвать пропуск",
    ),
    (
        "balanced-square-brackets",
        (
            "LSPD [Alpha] может отозвать пропуск. "
            "LSPD [Beta] отозвал пропуск."
        ),
        "LSPD [Alpha] отозвал пропуск",
        "LSPD [Beta] отозвал пропуск",
        "LSPD [Alpha] может отозвать пропуск",
    ),
    (
        "quoted-russian-exact-ending",
        (
            "Организация «Мира» может отозвать пропуск. "
            "Организация «Мир» отозвала пропуск."
        ),
        "Организация «Мира» отозвала пропуск",
        "Организация «Мир» отозвала пропуск",
        "Организация «Мира» может отозвать пропуск",
    ),
    (
        "parenthesized-russian-exact-ending",
        (
            "LSPD (Мира) может отозвать пропуск. "
            "LSPD (Мир) отозвал пропуск."
        ),
        "LSPD (Мира) отозвал пропуск",
        "LSPD (Мир) отозвал пропуск",
        "LSPD (Мира) может отозвать пропуск",
    ),
)


@pytest.mark.parametrize(
    ("case_id", "source", "unsafe_past", "honest_past", "honest_may"),
    FINAL_V3_DELIMITED_IDENTITY_POOLING_CASES,
    ids=[case[0] for case in FINAL_V3_DELIMITED_IDENTITY_POOLING_CASES],
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_delimited_identity_does_not_pool_other_event(
    case_id: str,
    source: str,
    unsafe_past: str,
    honest_past: str,
    honest_may: str,
    validate: RelationValidator,
) -> None:
    del case_id
    with pytest.raises(ValueError):
        validate(source, unsafe_past)
    validate(source, honest_past)
    validate(source, honest_may)


@pytest.mark.parametrize(
    ("case_id", "source", "unsafe_past", "honest_past", "honest_may"),
    FINAL_V3_DELIMITED_IDENTITY_POOLING_CASES,
    ids=[case[0] for case in FINAL_V3_DELIMITED_IDENTITY_POOLING_CASES],
)
def test_final_v3_delimited_identity_does_not_hide_in_full_set(
    case_id: str,
    source: str,
    unsafe_past: str,
    honest_past: str,
    honest_may: str,
) -> None:
    del case_id
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [honest_may, unsafe_past, honest_past],
        )
    validate_synonym_source_evidence(source, [honest_may, honest_past])


@pytest.mark.parametrize(
    "malformed",
    (
        "LSPD »Alpha отозвал пропуск",
        "LSPD )Alpha отозвал пропуск",
        "LSPD 'Alpha отозвал пропуск",
        "LSPD [Alpha отозвал пропуск",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_malformed_delimiter_never_falls_back_to_bare_actor(
    malformed: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate("LSPD отозвал пропуск.", malformed)


@pytest.mark.parametrize(
    ("source", "honest"),
    (
        (
            "Лицензия должна выдаваться благодаря FIB.",
            "лицензия должна выдаваться благодаря FIB",
        ),
        (
            "Пропуск отзывается посредством DEA.",
            "пропуск отзывается посредством DEA",
        ),
        (
            "Лицензия должна выдаваться вопреки USSS.",
            "лицензия должна выдаваться вопреки USSS",
        ),
        (
            "Лицензия должна выдаваться при поддержке организации (FIB).",
            "лицензия должна выдаваться при поддержке организации (FIB)",
        ),
        (
            "Лицензия должна выдаваться при поддержке "
            + " ".join(["регионального"] * 17)
            + " FIB.",
            "лицензия должна выдаваться при поддержке "
            + " ".join(["регионального"] * 17)
            + " FIB",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_general_oblique_role_remains_honestly_searchable(
    source: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Лицензия должна выдаваться благодаря FIB.", "FIB должен выдать лицензию"),
        ("Пропуск отзывается посредством DEA.", "DEA отозвал пропуск"),
        ("Лицензия должна выдаваться вопреки USSS.", "USSS должен выдать лицензию"),
        (
            "Лицензия должна выдаваться при поддержке организации (FIB).",
            "FIB должен выдать лицензию",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_general_oblique_role_cannot_become_actor(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_territorial_court_decision_keeps_actor_clean(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд округа Блейн принял решение аннулировать лицензию.",
        "Суд округа Блейн аннулирует лицензию",
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_ascii_slash_group_cannot_be_reduced_to_one_actor(
    validate: RelationValidator,
) -> None:
    source = "FIB/USSS могут отозвать пропуск."
    with pytest.raises(ValueError):
        validate(source, "FIB может отозвать пропуск")
    validate(source, "FIB/USSS могут отозвать пропуск")


def test_final_v3_ascii_slash_group_cannot_hide_in_full_set() -> None:
    source = "FIB/USSS могут отозвать пропуск."
    honest = "FIB/USSS могут отозвать пропуск"
    unsafe = "FIB может отозвать пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "Директор может отозвать пропуск. Начальник отозвал пропуск.",
            "Директор отозвал пропуск",
            "Начальник отозвал пропуск",
        ),
        (
            "Директор может отозвать пропуск. Начальник обязан отозвать пропуск.",
            "Директор обязан отозвать пропуск",
            "Начальник обязан отозвать пропуск",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_distinct_bare_management_roles_do_not_pool(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "Директор может отозвать пропуск. Начальник отозвал пропуск.",
            "Директор отозвал пропуск",
            "Начальник отозвал пропуск",
        ),
        (
            "Директор может отозвать пропуск. Начальник обязан отозвать пропуск.",
            "Директор обязан отозвать пропуск",
            "Начальник обязан отозвать пропуск",
        ),
    ),
)
def test_final_v3_distinct_bare_management_roles_do_not_hide_in_full_set(
    source: str,
    unsafe: str,
    honest: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [unsafe, honest])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "Начальник отозвал пропуск.",
            "Директор отозвал пропуск",
            "Начальник отозвал пропуск",
        ),
        (
            "Директор обязан отозвать лицензию.",
            "Руководитель обязан отозвать лицензию",
            "Директор обязан отозвать лицензию",
        ),
    ),
    ids=("chief-is-not-director", "director-is-not-manager"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_single_bare_management_role_is_exact(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "Начальник отозвал пропуск.",
            "Директор отозвал пропуск",
            "Начальник отозвал пропуск",
        ),
        (
            "Директор обязан отозвать лицензию.",
            "Руководитель обязан отозвать лицензию",
            "Директор обязан отозвать лицензию",
        ),
    ),
    ids=("chief-is-not-director", "director-is-not-manager"),
)
def test_final_v3_single_management_role_cannot_hide_in_full_set(
    source: str,
    unsafe: str,
    honest: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "FIB/USSS могут отозвать пропуск.",
            "FIB совместно с USSS могут отозвать пропуск",
            "USSS/FIB могут отозвать пропуск",
        ),
        (
            "FIB совместно с USSS могут отозвать пропуск.",
            "FIB/USSS могут отозвать пропуск",
            "USSS совместно с FIB могут отозвать пропуск",
        ),
        (
            "FIB или USSS могут отозвать пропуск.",
            "FIB и USSS могут отозвать пропуск",
            "USSS/FIB могут отозвать пропуск",
        ),
    ),
    ids=("slash-is-not-joint", "joint-is-not-slash", "or-is-not-joint"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_ascii_actor_group_keeps_connector_semantics(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


def test_final_v3_ascii_group_connector_change_cannot_hide_in_full_set() -> None:
    source = "FIB/USSS могут отозвать пропуск."
    honest = "USSS/FIB могут отозвать пропуск"
    unsafe = "FIB совместно с USSS могут отозвать пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    "source",
    (
        "Пропуск отозвал после проверки, именно LSPD.",
        (
            "Пропуск отозвал после "
            + " ".join(["длительной"] * 17)
            + " проверки, именно LSPD."
        ),
        "Пропуск отозвал вопреки решению суда, именно LSPD.",
    ),
    ids=("comma-focus", "long-comma-focus", "rare-preposition"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_closed_oblique_adjunct_keeps_later_ascii_actor(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, "LSPD отозвал пропуск")
    with pytest.raises(ValueError):
        validate(source, "USSS отозвал пропуск")


def test_final_v3_closed_oblique_ascii_actor_cannot_hide_in_full_set() -> None:
    source = "Пропуск отозвал после проверки, именно LSPD."
    honest = "LSPD отозвал пропуск"
    unsafe = "USSS отозвал пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


FINAL_V3_ATTACHED_INSTITUTION_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        "Суд-Альфа может отозвать пропуск. Суд-Бета отозвал пропуск.",
        "Суд-Альфа отозвал пропуск",
        "Суд-Бета отозвал пропуск",
    ),
    (
        (
            "Полиция-Альфа может отозвать пропуск. "
            "Полиция-Бета отозвала пропуск."
        ),
        "Полиция-Альфа отозвала пропуск",
        "Полиция-Бета отозвала пропуск",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    FINAL_V3_ATTACHED_INSTITUTION_CASES,
    ids=("court", "police"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_attached_institution_identity_does_not_pool(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    FINAL_V3_ATTACHED_INSTITUTION_CASES,
    ids=("court", "police"),
)
def test_final_v3_attached_institution_cannot_hide_in_full_set(
    source: str,
    unsafe: str,
    honest: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "honest"),
    (
        (
            "Организация «Выдающий Свет» может отозвать пропуск.",
            "Организация «Выдающий Свет» может отозвать пропуск",
        ),
        (
            "Организация «Лицензия Альфа» обязана отозвать пропуск.",
            "Организация «Лицензия Альфа» обязана отозвать пропуск",
        ),
        (
            "LSPD [Отзыв лицензии] может отозвать пропуск.",
            "LSPD [Отзыв лицензии] может отозвать пропуск",
        ),
    ),
    ids=("participle-name", "object-name", "ascii-bracket-name"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_exact_delimited_legal_words_remain_identity(
    source: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_exact_delimited_legal_name_does_not_pool(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация «Лицензия Альфа» может отозвать пропуск. "
        "Организация «Лицензия Бета» отозвала пропуск."
    )
    unsafe = "Организация «Лицензия Альфа» отозвала пропуск"
    honest = "Организация «Лицензия Бета» отозвала пропуск"
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


def test_final_v3_exact_delimited_action_token_is_not_deleted_from_key() -> None:
    assert _relation_actor_key(
        "организация «Выдающий Свет»",
        preserve_complete=True,
    ) != _relation_actor_key(
        "организация «Свет»",
        preserve_complete=True,
    )


def test_final_v3_exact_delimited_name_cannot_hide_in_full_set() -> None:
    source = (
        "Организация «Лицензия Альфа» может отозвать пропуск. "
        "Организация «Лицензия Бета» отозвала пропуск."
    )
    honest = "Организация «Лицензия Бета» отозвала пропуск"
    unsafe = "Организация «Лицензия Альфа» отозвала пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            (
                "Пропуск может быть отозван Судом-Альфа. "
                "Пропуск отозван Судом-Бета."
            ),
            "Суд-Альфа отозвал пропуск",
            "Суд-Бета отозвал пропуск",
        ),
        (
            (
                "Пропуск может быть отозван Полицией-Альфа. "
                "Пропуск отозван Полицией-Бета."
            ),
            "Полиция-Альфа отозвала пропуск",
            "Полиция-Бета отозвала пропуск",
        ),
    ),
    ids=("court-passive", "police-passive"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_attached_institution_survives_active_passive_case(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд-Альфа отозвал пропуск.",
            "Пропуск отозван Судом-Альфа",
        ),
        (
            "Полиция-Альфа отозвала пропуск.",
            "Пропуск отозван Полицией-Альфа",
        ),
        (
            "Организация «Лицензия Альфа» может отозвать пропуск.",
            (
                "Пропуск может быть отозван организацией "
                "«Лицензия Альфа»"
            ),
        ),
        (
            "LSPD [Отзыв лицензии] может отозвать пропуск.",
            "Пропуск может быть отозван LSPD [Отзыв лицензии]",
        ),
    ),
    ids=(
        "attached-court",
        "attached-police",
        "delimited-organization",
        "delimited-ascii",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_exact_identity_survives_active_passive_paraphrase(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


def test_final_v3_passive_attached_identity_cannot_hide_in_full_set() -> None:
    source = (
        "Пропуск может быть отозван Судом-Альфа. "
        "Пропуск отозван Судом-Бета."
    )
    honest = "Суд-Бета отозвал пропуск"
    unsafe = "Суд-Альфа отозвал пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Организация-Альфа отозвала пропуск.",
            "Пропуск отозван Организацией-Альфа",
        ),
        (
            "Руководитель-Альфа отозвал пропуск.",
            "Пропуск отозван Руководителем-Альфа",
        ),
    ),
    ids=("organization", "management"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_attached_identity_normalization_is_family_generic(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_attached_management_identity_does_not_pool(
    validate: RelationValidator,
) -> None:
    source = (
        "Руководитель-Альфа может отозвать пропуск. "
        "Руководитель-Бета отозвал пропуск."
    )
    with pytest.raises(ValueError):
        validate(source, "Руководитель-Альфа отозвал пропуск")
    validate(source, "Руководитель-Бета отозвал пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_legal_sentence_inside_actor_name_is_opaque(
    validate: RelationValidator,
) -> None:
    source = "Организация «Выдала лицензию» может отозвать пропуск."
    validate(source, source.rstrip("."))
    validate(
        source,
        "Пропуск может быть отозван организацией «Выдала лицензию»",
    )


def test_final_v3_legal_sentence_inside_name_creates_no_inner_truth() -> None:
    truths = _extract_relation_truths(
        "Организация «Выдала лицензию» может отозвать пропуск."
    )
    assert not any(
        truth.predicate == "regulated_issue"
        and truth.direct_object == "license"
        for truth in truths
    )
    assert any(
        truth.predicate == "regulated_revocation"
        and truth.operative_actor
        == _relation_actor_key(
            "Организация «Выдала лицензию»",
            preserve_complete=True,
        )
        for truth in truths
    )


def test_final_v3_opaque_legal_name_cannot_hide_in_full_set() -> None:
    source = (
        "Организация «Выдала лицензию Альфа» может отозвать пропуск. "
        "Организация «Выдала лицензию Бета» отозвала пропуск."
    )
    honest = "Организация «Выдала лицензию Бета» отозвала пропуск"
    unsafe = "Организация «Выдала лицензию Альфа» отозвала пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


def test_final_v3_lowercase_legal_aside_is_not_masked_as_actor_name() -> None:
    source = (
        "Решение об аннулировании пропуска может быть обжаловано в суде "
        "вышестоящей инстанции (решения по аннулированию пропусков "
        "обжалуются в нём же). Организация (например: SANG) действует."
    )
    masked = _relation_mask_confirmed_actor_identity_payloads(source)
    assert "решения по аннулированию пропусков" in masked
    assert "например: SANG" in masked


# Native-v3 final-rereview matrix.  Every case below is a shared grammatical
# boundary rather than a server/article exception.
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_completed_name_stays_opaque_before_unrelated_malformed_tail(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация «Лицензия выдана» может отозвать пропуск. "
        "Примечание ("
    )
    honest = "Организация «Лицензия выдана» может отозвать пропуск"
    validate(source, honest)
    with pytest.raises(ValueError):
        validate(source, "Лицензия выдана")


def test_final_v3_unrelated_malformed_tail_cannot_hide_in_full_set() -> None:
    source = (
        "Организация «Пропуск отозван» может выдать лицензию. "
        "Примечание ("
    )
    honest = "Организация «Пропуск отозван» может выдать лицензию"
    unsafe = "Пропуск отозван"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "LSPD (шт. SA) может отозвать пропуск.",
            "Пропуск может быть отозван LSPD (шт. SA)",
        ),
        (
            "Организация «St. Mary» может отозвать пропуск.",
            "Пропуск может быть отозван организацией «St. Mary»",
        ),
        (
            "Организация «Альфа, а суд выдаёт лицензию» может отозвать пропуск.",
            (
                "Пропуск может быть отозван организацией "
                "«Альфа, а суд выдаёт лицензию»"
            ),
        ),
    ),
    ids=("state-abbreviation", "dotted-name", "punctuated-legal-name"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_exact_name_uses_one_offset_preserving_syntax_surface(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


def test_final_v3_punctuated_exact_name_creates_no_inner_relation() -> None:
    truths = _extract_relation_truths(
        "Организация «Альфа, а суд выдаёт лицензию» может отозвать пропуск."
    )
    assert not any(
        truth.predicate == "regulated_issue"
        and truth.direct_object == "license"
        for truth in truths
    )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_identity_window_whitespace_is_not_a_real_boundary(
    validate: RelationValidator,
) -> None:
    first = "A" * 59
    second = "B" * 58
    source = (
        f"LSPD {first} {second} Alpha может отозвать пропуск. "
        f"LSPD {first} {second} Beta отозвал пропуск."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            f"LSPD {first} {second} Alpha отозвал пропуск",
        )


FINAL_V3_COMPLETE_ACTOR_GROUP_CASES: tuple[
    tuple[str, str, str], ...
] = (
    (
        "FIB или USSS обязаны отозвать пропуск.",
        "FIB обязан отозвать пропуск",
        "USSS/FIB обязаны отозвать пропуск",
    ),
    (
        "FIB и USSS обязаны отозвать пропуск.",
        "FIB/USSS обязаны отозвать пропуск",
        "FIB обязан отозвать пропуск",
    ),
    (
        "FIB и/или USSS могут отозвать пропуск.",
        "FIB может отозвать пропуск",
        "USSS/FIB могут отозвать пропуск",
    ),
    (
        "FIB, USSS могут отозвать пропуск.",
        "FIB/USSS могут отозвать пропуск",
        "FIB может отозвать пропуск",
    ),
    (
        "LSPD Alpha или LSPD Beta обязаны отозвать пропуск.",
        "LSPD Alpha обязан отозвать пропуск",
        "LSPD Beta или LSPD Alpha обязаны отозвать пропуск",
    ),
    (
        (
            "Суд округа Альфа или Суд округа Бета "
            "обязан отозвать пропуск."
        ),
        "Суд округа Альфа обязан отозвать пропуск",
        "Суд округа Бета или Суд округа Альфа обязан отозвать пропуск",
    ),
    (
        (
            "Организация «Альфа» и Организация «Бета» "
            "могут отозвать пропуск."
        ),
        "Организация «Гамма» может отозвать пропуск",
        "Организация «Альфа» может отозвать пропуск",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    FINAL_V3_COMPLETE_ACTOR_GROUP_CASES,
    ids=(
        "bare-alternative-mandatory",
        "bare-joint-mandatory",
        "and-slash-or",
        "comma-joint",
        "ascii-identities",
        "russian-court-identities",
        "russian-organization-identities",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_complete_actor_group_cannot_select_one_member(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


def test_final_v3_complete_actor_group_cannot_hide_in_full_set() -> None:
    source = "LSPD Alpha или LSPD Beta обязаны отозвать пропуск."
    honest = "LSPD Beta или LSPD Alpha обязаны отозвать пропуск"
    unsafe = "LSPD Alpha обязан отозвать пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "LSPD Alpha или LSPD Beta могут отозвать пропуск.",
            "LSPD Alpha и LSPD Beta могут отозвать пропуск",
            "LSPD Alpha может отозвать пропуск",
        ),
        (
            "Пропуск могут отозвать LSPD Alpha или LSPD Beta.",
            "LSPD Alpha и LSPD Beta могут отозвать пропуск",
            "LSPD Beta может отозвать пропуск",
        ),
    ),
    ids=("active-named-may", "object-first-named-may"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_named_may_group_preserves_branch_search_contract(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "Суд и/или лицензирующий орган могут отозвать пропуск.",
            "Суд может отозвать пропуск",
            "Лицензирующий орган и/или Суд могут отозвать пропуск",
        ),
        (
            "Суд/лицензирующий орган могут отозвать пропуск.",
            "Лицензирующий орган может отозвать пропуск",
            "Лицензирующий орган/Суд могут отозвать пропуск",
        ),
        (
            "Сотрудник полиции и/или прокурор могут отозвать пропуск.",
            "Прокурор может отозвать пропуск",
            "Прокурор и/или сотрудник полиции могут отозвать пропуск",
        ),
    ),
    ids=("licensing-and-or", "licensing-slash", "police-wrapper"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_legacy_actor_group_fallback_is_fail_closed(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


def test_final_v3_legacy_actor_group_cannot_hide_in_full_set() -> None:
    source = "Суд и/или лицензирующий орган могут отозвать пропуск."
    honest = "Лицензирующий орган и/или Суд могут отозвать пропуск"
    unsafe = "Суд может отозвать пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "LSPD Alpha может отозвать пропуск.",
            "Пропуск может быть отозван LSPD Beta",
            "Пропуск может быть отозван LSPD Alpha",
        ),
        (
            "Пропуск может быть отозван DEA-Alpha.",
            "Пропуск может быть отозван DEA-Beta",
            "DEA-Alpha может отозвать пропуск",
        ),
        (
            "Пропуск обязан быть отозван LSPD [Alpha].",
            "Пропуск обязан быть отозван LSPD [Beta]",
            "LSPD [Alpha] обязан отозвать пропуск",
        ),
        (
            "Пропуск может быть отозван FIB/USSS.",
            "Пропуск может быть отозван FIB",
            "Пропуск может быть отозван USSS/FIB",
        ),
    ),
    ids=("active-to-passive", "attached", "bracketed", "group"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_modal_passive_keeps_complete_performer(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


def test_final_v3_modal_passive_identity_cannot_hide_in_full_set() -> None:
    source = "LSPD Alpha может отозвать пропуск."
    honest = "Пропуск может быть отозван LSPD Alpha"
    unsafe = "Пропуск может быть отозван LSPD Beta"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_roster_inside_exact_name_is_not_source_truth(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация «Список лиц, уполномоченных выдавать лицензию: суд» "
        "может отозвать пропуск."
    )
    with pytest.raises(ValueError):
        validate(source, "Суд выдаёт лицензию")
    validate(source, source.rstrip("."))


def test_final_v3_ordinary_parenthetical_clause_keeps_roles_separate(
) -> None:
    source = (
        "Организация может отозвать пропуск "
        "(Суд обязан отозвать лицензию)."
    )
    unsafe = "Организация обязана отозвать лицензию"
    with pytest.raises(ValueError):
        _validate_title(source, unsafe)
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [unsafe])
    _validate_title(source, "Организация может отозвать пропуск")
    _validate_title(source, "Суд обязан отозвать лицензию")
    validate_synonym_source_evidence(
        source,
        [
            "Организация может отозвать пропуск",
            "Суд обязан отозвать лицензию",
        ],
    )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Лицензия должна выдаваться под контролем, "
            "осуществляемым организацией Альфа."
        ),
        (
            "Лицензия должна выдаваться вне контроля, "
            "осуществляемого организацией Альфа."
        ),
    ),
    ids=("under-control", "outside-control"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_oblique_control_actor_cannot_become_issuer(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "Организация Альфа должна выдать лицензию")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_long_oblique_control_keeps_later_real_actor(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск отозвал под длительным контролем независимой комиссии, "
        "именно LSPD."
    )
    validate(source, "LSPD отозвал пропуск")
    with pytest.raises(ValueError):
        validate(source, "USSS отозвал пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_permission_authority_is_relation_evidence(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр с разрешения суда Альфа может отозвать пропуск. "
        "Организация Центр с разрешения суда Бета отозвала пропуск."
    )
    unsafe = (
        "Организация Центр с разрешения суда Альфа отозвала пропуск"
    )
    honest = "Организация Центр с разрешения суда Бета отозвала пропуск"
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


def test_final_v3_permission_authority_cannot_hide_in_full_set() -> None:
    source = (
        "Организация Центр с разрешения суда Альфа может отозвать пропуск. "
        "Организация Центр с разрешения суда Бета отозвала пропуск."
    )
    honest = "Организация Центр с разрешения суда Бета отозвала пропуск"
    unsafe = "Организация Центр с разрешения суда Альфа отозвала пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_permission_requirement_cannot_be_dropped(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр с разрешения суда Альфа отозвала пропуск."
    )
    with pytest.raises(ValueError):
        validate(source, "Организация Центр отозвала пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_lowercase_attached_identity_stays_exact(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть отозван судом-альфа. "
        "Пропуск был отозван судом-бета."
    )
    with pytest.raises(ValueError):
        validate(source, "Пропуск был отозван судом-альфа")
    validate(source, "Пропуск был отозван судом-бета")
    validate("Суд-Альфа отозвал пропуск.", "Пропуск отозван судом-альфа")


@pytest.mark.parametrize(
    "source",
    (
        "Суд округа Блейн принял окончательное решение аннулировать лицензию.",
        "Суд округа Блейн принял решение немедленно аннулировать лицензию.",
        "Суд округа Блейн постановил немедленно аннулировать лицензию.",
    ),
    ids=("decision-modifier", "decision-adverb", "decreed-adverb"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_governing_decision_modifiers_do_not_pollute_actor(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, "Суд округа Блейн аннулирует лицензию")
    with pytest.raises(ValueError):
        validate(source, "Суд округа Лос-Сантос аннулирует лицензию")


@pytest.mark.parametrize(
    "source",
    (
        "Суд (Только при чрезвычайном положении) может отозвать пропуск.",
        "LSPD (При нарушении режима) может отозвать пропуск.",
    ),
    ids=("focused-condition", "leading-condition"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_capitalized_parenthetical_condition_is_not_actor_name(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, source.split("(", 1)[0].strip() + " может отозвать пропуск")


# Native-v3 post-rereview boundary matrix.  These cases intentionally use
# generic grammar shapes so every server and every future model writer shares
# the same protection.
@pytest.mark.parametrize(
    "source",
    (
        "LSPD »Alpha отозвал пропуск.",
        "LSPD )Alpha отозвал пропуск.",
        "LSPD ]Alpha отозвал пропуск.",
        "LSPD (Alpha] отозвал пропуск.",
        "Пропуск был отозван LSPD »Alpha.",
        "Пропуск был отозван LSPD )Alpha.",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_source_local_malformed_identity_is_unresolved(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "LSPD отозвал пропуск")


@pytest.mark.parametrize("tail", ("(", ")", "]"))
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_unrelated_malformed_tail_stays_clause_local(
    tail: str,
    validate: RelationValidator,
) -> None:
    source = (
        "LSPD Alpha может отозвать пропуск. "
        f"Техническое примечание {tail}"
    )
    validate(source, "LSPD Alpha может отозвать пропуск")
    with pytest.raises(ValueError):
        validate(source, "LSPD Beta может отозвать пропуск")


@pytest.mark.parametrize(
    ("source_template", "unsafe_template"),
    (
        (
            "Пропуск может быть отозван LSPD {identity} Alpha. ",
            "Пропуск был отозван LSPD {identity} Alpha",
        ),
        (
            "Пропуск может быть отозван судом округа {identity} Alpha. ",
            "Пропуск был отозван судом округа {identity} Alpha",
        ),
        (
            "Пропуск может быть отозван руководителем отдела {identity} Alpha. ",
            "Пропуск был отозван руководителем отдела {identity} Alpha",
        ),
        (
            "Пропуск может быть отозван LSPD ({identity} Alpha). ",
            "Пропуск был отозван LSPD ({identity} Alpha)",
        ),
    ),
    ids=("direct", "territorial", "management", "delimited"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_identity_window_never_becomes_a_space_boundary(
    source_template: str,
    unsafe_template: str,
    validate: RelationValidator,
) -> None:
    long_identity = "A" * 59 + " " + "B" * 58
    source = source_template.format(identity=long_identity) + (
        "Пропуск был отозван судом округа Бета."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            unsafe_template.format(identity=long_identity),
        )
    validate(source, "Пропуск был отозван судом округа Бета")


def test_final_v3_overwindow_territory_cannot_hide_in_full_set() -> None:
    long_identity = "A" * 59 + " " + "B" * 58
    source = (
        f"Пропуск может быть отозван судом округа {long_identity} Альфа. "
        "Пропуск был отозван судом округа Бета."
    )
    honest = "Пропуск был отозван судом округа Бета"
    unsafe = (
        f"Пропуск был отозван судом округа {long_identity} Альфа"
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "LSPD St. Mary может отозвать пропуск.",
            "Пропуск может быть отозван LSPD St. Mary",
        ),
        (
            "Организация деп. Alpha может отозвать пропуск.",
            "Пропуск может быть отозван организацией деп. Alpha",
        ),
    ),
    ids=("ascii-abbreviation", "russian-abbreviation"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_unquoted_dotted_identity_survives_word_order(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            (
                "LSPD St. Mary может отозвать пропуск. "
                "LSPD St. Joseph отозвал пропуск."
            ),
            "LSPD St. Mary отозвал пропуск",
            "LSPD St. Joseph отозвал пропуск",
        ),
        (
            (
                "Организация деп. Alpha может отозвать пропуск. "
                "Организация деп. Beta отозвала пропуск."
            ),
            "Организация деп. Alpha отозвала пропуск",
            "Организация деп. Beta отозвала пропуск",
        ),
    ),
    ids=("ascii-abbreviation", "russian-abbreviation"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_unquoted_dotted_identity_does_not_pool(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


def test_final_v3_unquoted_dotted_identity_cannot_hide_in_full_set() -> None:
    source = (
        "LSPD St. Mary может отозвать пропуск. "
        "LSPD St. Joseph отозвал пропуск."
    )
    honest = "LSPD St. Joseph отозвал пропуск"
    unsafe = "LSPD St. Mary отозвал пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    ("source", "outputs"),
    (
        (
            "Суд и лицензирующий орган могут отозвать пропуск.",
            (
                "Суд может отозвать пропуск",
                "Лицензирующий орган может отозвать пропуск",
            ),
        ),
        (
            "Пропуск могут отозвать Суд и лицензирующий орган.",
            (
                "Суд может отозвать пропуск",
                "Лицензирующий орган может отозвать пропуск",
            ),
        ),
        (
            "Суд или лицензирующий орган могут отозвать пропуск.",
            (
                "Суд может отозвать пропуск",
                "Лицензирующий орган может отозвать пропуск",
            ),
        ),
        (
            "Сотрудник полиции и прокурор могут отозвать пропуск.",
            (
                "Сотрудник полиции может отозвать пропуск",
                "Прокурор может отозвать пропуск",
            ),
        ),
    ),
    ids=("active-joint", "object-first-joint", "explicit-or", "police-joint"),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_branchable_actor_group_keeps_each_search_path(
    source: str,
    outputs: tuple[str, str],
    validate: RelationValidator,
) -> None:
    for output in outputs:
        validate(source, output)


def test_final_v3_branchable_group_full_set_keeps_both_members() -> None:
    source = "Суд и лицензирующий орган могут отозвать пропуск."
    validate_synonym_source_evidence(
        source,
        [
            "Суд может отозвать пропуск",
            "Лицензирующий орган может отозвать пропуск",
        ],
    )


def test_final_v3_permission_words_inside_actor_name_are_not_a_role() -> None:
    for source in (
        "Организация Центр по разрешению споров Альфа может отозвать пропуск.",
        "Суд по разрешению споров Альфа может отозвать пропуск.",
    ):
        truths = _extract_relation_truths(source)
        assert truths
        assert not any(
            argument.role == "permission_requirement"
            for truth in truths
            for argument in truth.arguments
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_permission_modifier_and_authority_are_required(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр с письменного разрешения суда Альфа "
        "может отозвать пропуск."
    )
    validate(
        source,
        "С письменного разрешения суда Альфа организация Центр "
        "может отозвать пропуск",
    )
    for unsafe in (
        "Организация Центр может отозвать пропуск",
        "Организация Центр с разрешения суда Альфа может отозвать пропуск",
        (
            "Организация Центр с письменного разрешения суда Бета "
            "может отозвать пропуск"
        ),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_negative_permission_gate_preserves_sign(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр при отсутствии письменного разрешения суда Альфа "
        "может отозвать пропуск."
    )
    validate(
        source,
        "При отсутствии письменного разрешения суда Альфа организация Центр "
        "может отозвать пропуск",
    )
    for unsafe in (
        "Организация Центр может отозвать пропуск",
        (
            "Организация Центр при наличии письменного разрешения суда Альфа "
            "может отозвать пропуск"
        ),
        (
            "Организация Центр при отсутствии письменного разрешения суда Бета "
            "может отозвать пропуск"
        ),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


def test_final_v3_permission_coordination_strips_governed_preposition() -> None:
    source = (
        "Организация Центр по предварительному согласованию с судом Альфа "
        "может отозвать пропуск."
    )
    truth = next(
        truth
        for truth in _extract_relation_truths(source)
        if truth.predicate == "regulated_revocation"
    )
    requirements = {
        argument
        for argument in truth.arguments
        if argument.role == "permission_requirement"
    }
    assert requirements == {
        next(iter(_relation_permission_spans(source))).argument
    }
    requirement = next(iter(requirements))
    assert requirement.value == "суд альф"
    assert requirement.marker == "coordination"
    assert requirement.qualifiers == frozenset({"preliminary"})


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_parenthetical_permission_is_identity_bound(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд (С разрешения прокуратуры Альфа) может отозвать пропуск. "
        "Суд (С разрешения прокуратуры Бета) отозвал пропуск."
    )
    unsafe = "Суд (С разрешения прокуратуры Альфа) отозвал пропуск"
    honest = "Суд (С разрешения прокуратуры Бета) отозвал пропуск"
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)
    with pytest.raises(ValueError):
        validate(source, "Суд может отозвать пропуск")


def test_final_v3_parenthetical_permission_cannot_hide_in_full_set() -> None:
    source = (
        "Суд (С разрешения прокуратуры Альфа) может отозвать пропуск. "
        "Суд (С разрешения прокуратуры Бета) отозвал пропуск."
    )
    honest = "Суд (С разрешения прокуратуры Бета) отозвал пропуск"
    unsafe = "Суд (С разрешения прокуратуры Альфа) отозвал пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


def test_final_v3_multiple_permissions_do_not_become_operative_actors() -> None:
    source = (
        "Организация Центр с разрешения суда Альфа и с согласия LSPD "
        "может отозвать пропуск."
    )
    truth = next(
        truth
        for truth in _extract_relation_truths(source)
        if truth.predicate == "regulated_revocation"
    )
    requirements = {
        argument
        for argument in truth.arguments
        if argument.role == "permission_requirement"
    }
    assert truth.operative_actor == "организаци центр"
    assert {argument.value for argument in requirements} == {"суд альф", "lspd"}


def test_final_v3_permission_is_bound_to_its_nearest_coordinated_action() -> None:
    source = (
        "Организация Центр отозвала пропуск и с разрешения суда Бета "
        "выдала лицензию."
    )
    truths = _extract_relation_truths(source)
    revocations = [
        truth for truth in truths if truth.predicate == "regulated_revocation"
    ]
    issues = [truth for truth in truths if truth.predicate == "regulated_issue"]
    assert revocations and issues
    assert all(
        not any(arg.role == "permission_requirement" for arg in truth.arguments)
        for truth in revocations
    )
    assert any(
        any(
            arg.role == "permission_requirement" and arg.value == "суд бет"
            for arg in truth.arguments
        )
        for truth in issues
    )
    _validate_title(source, "Организация Центр отозвала пропуск")


def test_final_v3_permission_opaque_identity_never_uses_truncated_key() -> None:
    shared = " ".join(f"Token{index}" for index in range(40))
    alpha = _relation_permission_authority_key(shared + " Alpha")
    beta = _relation_permission_authority_key(shared + " Beta")
    assert alpha != beta
    assert alpha.startswith("__permission_authority__:")
    assert beta.startswith("__permission_authority__:")
    assert "__unresolved_actor__" not in alpha + beta


def test_final_v3_crlf_roster_preserves_offsets_and_authorities() -> None:
    source = (
        "Лица, уполномоченные выдавать допуск\r\n"
        "Право на выдачу допуска имеют следующие должностные лица:\r\n"
        "Губернатор штата и его заместители.\r\n"
        "Генеральный прокурор штата и его заместители."
    )
    syntax = _relation_syntax_surface(source)
    assert len(syntax) == len(source)
    assert syntax.count("\r\n") == source.count("\r\n")
    for validate in RELATION_VALIDATORS:
        validate(source, "губернатор вправе выдавать допуск")
        with pytest.raises(ValueError):
            validate(source, "мэр вправе выдавать допуск")


def test_final_v3_crlf_roster_inside_exact_name_creates_no_truth() -> None:
    source = (
        "Организация «Список лиц, уполномоченных выдавать лицензию:\r\n"
        "- Суд» может отозвать пропуск."
    )
    truths = _extract_relation_truths(source)
    assert not any(
        truth.predicate == "regulated_issue" and truth.direct_object == "license"
        for truth in truths
    )
    for validate in RELATION_VALIDATORS:
        validate(source, source.rstrip("."))
        with pytest.raises(ValueError):
            validate(source, "Суд выдаёт лицензию")


FINAL_V3_PERMISSION_GRAMMAR_CASES: tuple[tuple[str, str], ...] = (
    ("с согласия владельца", "consent"),
    ("с разрешения владельца", "permission"),
    ("по согласованию с владельцем", "coordination"),
    ("с прямого разрешения суда Альфа", "permission"),
    (
        "с предварительно полученного письменного разрешения суда Альфа",
        "permission",
    ),
    ("с ведома прокуратуры Альфа", "knowledge"),
    ("после получения разрешения суда Альфа", "permission"),
    ("при неполучении письменного разрешения суда Альфа", "permission"),
)


@pytest.mark.parametrize(
    ("permission_surface", "marker"),
    FINAL_V3_PERMISSION_GRAMMAR_CASES,
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_permission_grammar_cannot_be_dropped(
    permission_surface: str,
    marker: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Организация Центр {permission_surface} может отозвать пропуск."
    )
    spans = _relation_permission_spans(source)
    assert spans
    assert spans[0].argument.marker == marker
    with pytest.raises(ValueError):
        validate(source, "Организация Центр может отозвать пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_postposed_missing_permission_cannot_be_dropped(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр, не получив разрешения суда Альфа, "
        "может отозвать пропуск."
    )
    requirement = _relation_permission_spans(source)[0].argument
    assert requirement.polarity == "negative"
    with pytest.raises(ValueError):
        validate(source, "Организация Центр может отозвать пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_permission_expression_preserves_and_or(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр с разрешения суда Альфа и с согласия "
        "прокуратуры Бета может отозвать пропуск."
    )
    unsafe = (
        "Организация Центр с разрешения суда Альфа или с согласия "
        "прокуратуры Бета может отозвать пропуск"
    )
    honest = (
        "Организация Центр с согласия прокуратуры Бета и с разрешения "
        "суда Альфа может отозвать пропуск"
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


def test_final_v3_permission_expression_cannot_hide_in_full_set() -> None:
    source = (
        "Организация Центр с разрешения суда Альфа и с согласия "
        "прокуратуры Бета может отозвать пропуск."
    )
    honest = (
        "Организация Центр с согласия прокуратуры Бета и с разрешения "
        "суда Альфа может отозвать пропуск"
    )
    unsafe = (
        "Организация Центр с разрешения суда Альфа или с согласия "
        "прокуратуры Бета может отозвать пропуск"
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


def test_final_v3_permission_comma_binds_next_action_and_honest_reorder() -> None:
    source = (
        "Организация Центр выдала лицензию, с разрешения суда Альфа "
        "организация Центр отозвала пропуск."
    )
    truths = _extract_relation_truths(source)
    issues = [truth for truth in truths if truth.predicate == "regulated_issue"]
    revocations = [
        truth for truth in truths if truth.predicate == "regulated_revocation"
    ]
    assert issues and revocations
    assert all(
        not any(arg.role == "permission_requirement" for arg in truth.arguments)
        for truth in issues
    )
    assert all(
        any(arg.role == "permission_requirement" for arg in truth.arguments)
        for truth in revocations
    )
    validate_synonym_source_evidence(
        source,
        [
            "Организация Центр выдала лицензию",
            "Организация Центр с разрешения суда Альфа отозвала пропуск",
        ],
    )
    with pytest.raises(ValueError):
        _validate_title(
            source,
            "Организация Центр с разрешения суда Альфа выдала лицензию",
        )


def test_final_v3_permission_first_comma_preserves_actor_and_requirement() -> None:
    source = (
        "С разрешения суда Альфа, организация Центр может отозвать пропуск."
    )
    truth = next(
        truth
        for truth in _extract_relation_truths(source)
        if truth.predicate == "regulated_revocation"
    )
    assert truth.operative_actor == "организаци центр"
    assert any(arg.role == "permission_requirement" for arg in truth.arguments)
    for validate in RELATION_VALIDATORS:
        validate(
            source,
            "Организация Центр с разрешения суда Альфа может отозвать пропуск",
        )


def test_final_v3_trailing_permission_covers_tight_action_group() -> None:
    source = (
        "Суд может выдать или отозвать лицензию только с разрешения "
        "прокуратуры Альфа."
    )
    truths = [
        truth
        for truth in _extract_relation_truths(source)
        if truth.direct_object == "license"
        and truth.predicate in {"regulated_issue", "regulated_revocation"}
    ]
    assert {truth.predicate for truth in truths} == {
        "regulated_issue",
        "regulated_revocation",
    }
    assert all(
        any(arg.role == "permission_requirement" for arg in truth.arguments)
        for truth in truths
    )
    with pytest.raises(ValueError):
        _validate_title(source, "Суд может выдать лицензию")
    validate_synonym_source_evidence(
        source,
        [
            "Суд с разрешения прокуратуры Альфа может выдать лицензию",
            "Суд с разрешения прокуратуры Альфа может отозвать лицензию",
        ],
    )


@pytest.mark.parametrize(
    ("alpha", "beta"),
    (
        (
            "Федерального независимого межведомственного территориального "
            "суда Альфа",
            "Федерального независимого межведомственного территориального "
            "суда Бета",
        ),
        ("суда Лицензия Альфа", "суда Лицензия Бета"),
        (
            "Центра по выдаче лицензий Альфа",
            "Центра по выдаче лицензий Бета",
        ),
        (
            "лицензирующего органа Федерального независимого Альфа",
            "лицензирующего органа Федерального независимого Бета",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_permission_authority_never_truncates_identity(
    alpha: str,
    beta: str,
    validate: RelationValidator,
) -> None:
    source = f"Пропуск может быть отозван с разрешения {alpha}."
    unsafe = f"Пропуск может быть отозван с разрешения {beta}"
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "role",
    (
        "По поручению суда Альфа",
        "В присутствии прокурора Альфа",
        "Под контролем суда Альфа",
        "От имени прокуратуры Альфа",
        "В интересах LSPD",
        "Через представителя суда Альфа",
        "Решение суда Альфа № 1",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_parenthetical_oblique_role_cannot_be_erased(
    role: str,
    validate: RelationValidator,
) -> None:
    source = f"LSPD ({role}) может отозвать пропуск."
    with pytest.raises(ValueError):
        validate(source, "LSPD может отозвать пропуск")
    validate(source, source.rstrip("."))


def test_final_v3_parenthetical_oblique_role_cannot_hide_in_full_set() -> None:
    source = "LSPD (По поручению суда Альфа) может отозвать пропуск."
    honest = source.rstrip(".")
    unsafe = "LSPD может отозвать пропуск"
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    "source",
    (
        "Суд, а также лицензирующий орган могут отозвать пропуск.",
        "Пропуск могут отозвать Суд, а также лицензирующий орган.",
        "Пропуск может быть отозван судом, а также лицензирующим органом.",
        "Как Суд, так и лицензирующий орган могут отозвать пропуск.",
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_extended_joint_connectors_keep_both_search_paths(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, "Суд может отозвать пропуск")
    validate(source, "Лицензирующий орган может отозвать пропуск")


@pytest.mark.parametrize(
    ("opener", "closer"),
    (("„", "“"), ("‹", "›")),
)
def test_final_v3_balanced_delimiter_styles_preserve_exact_identity(
    opener: str,
    closer: str,
) -> None:
    source = (
        f"LSPD {opener}Alpha{closer} может отозвать пропуск. "
        f"LSPD {opener}Beta{closer} отозвал пропуск."
    )
    unsafe = f"LSPD {opener}Alpha{closer} отозвал пропуск"
    honest = f"LSPD {opener}Beta{closer} отозвал пропуск"
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, unsafe)
        validate(source, honest)
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [honest, unsafe])


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "LSPD SA. Вчера USSS отозвал пропуск.",
            "LSPD SA отозвал пропуск",
            "USSS отозвал пропуск",
        ),
        (
            "Организация Лицензия Альфа может отозвать пропуск. "
            "Организация Лицензия Бета отозвала пропуск.",
            "Организация Лицензия Альфа отозвала пропуск",
            "Организация Лицензия Бета отозвала пропуск",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_identity_boundary_cannot_pool_different_actors(
    source: str,
    unsafe: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
    validate(source, honest)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Организация Альфа выдаёт ордер.",
            "Организация Бета выдаёт ордер",
        ),
        ("Суд Альфа выдаёт ордер.", "Суд Бета выдаёт ордер"),
        (
            "Прокуратура Альфа выдаёт ордер.",
            "Прокуратура Бета выдаёт ордер",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_qualitative_family_cannot_launder_specific_identity(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_final_v3_explicit_joint_phrase_is_atomic(
    validate: RelationValidator,
) -> None:
    source = "Суд совместно с прокуратурой отозвали пропуск."
    with pytest.raises(ValueError):
        validate(source, "Прокуратура отозвала пропуск")
    validate(source, "Суд совместно с прокуратурой отозвали пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "output",
    (
        "Губернатор выдаёт ордер на задержание министра юстиции",
        "Верховный судья выдаёт ордер на задержание министра юстиции",
    ),
)
def test_final_v3_present_issue_alternative_lists_each_authority(
    validate: RelationValidator,
    output: str,
) -> None:
    source = (
        "Ордер на задержание министра юстиции выписывает губернатор "
        "штата или верховный судья."
    )
    validate(source, output)


def test_final_v3_roster_dotted_ascii_actor_has_qualitative_authority() -> None:
    source = (
        "Лица, уполномоченные выдавать лицензию\r\n"
        "Право на выдачу лицензии имеют следующие лица:\r\n"
        "- LSPD St. Mary"
    )
    for validate in RELATION_VALIDATORS:
        validate(source, "LSPD St. Mary выдаёт лицензию")


def test_final_v3_roster_exclusion_parenthesis_is_not_alias() -> None:
    source = (
        "Лица, уполномоченные выдавать допуск\r\n"
        "Право на выдачу допуска имеют следующие лица:\r\n"
        "Руководитель организации, кроме (FIB)."
    )
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, "Руководитель FIB вправе выдавать допуск")


def _assert_final_v3_rejected_in_all_output_paths(
    source: str,
    unsafe: str,
    *,
    honest: str | None = None,
) -> None:
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, unsafe)
    supported = honest if honest is not None else source.rstrip(".")
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [supported, unsafe])
    validate_synonym_source_evidence(source, [supported])


def test_final_v3_permission_covers_also_coordinated_action() -> None:
    source = (
        "С разрешения суда Альфа организация может выдать лицензию, "
        "а также отозвать пропуск."
    )
    unsafe = "Организация может отозвать пропуск"
    honest = (
        "Организация с разрешения суда Альфа может отозвать пропуск"
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        unsafe,
        honest=honest,
    )
    for validate in RELATION_VALIDATORS:
        validate(
            source,
            "Организация с разрешения суда Альфа может выдать лицензию",
        )


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "Суд обязан совместно с прокуратурой отозвать пропуск.",
            "Суд обязан отозвать пропуск",
            "Суд совместно с прокуратурой обязан отозвать пропуск",
        ),
        (
            "Суд сообща с прокуратурой отозвали пропуск.",
            "Суд отозвал пропуск",
            "Суд совместно с прокуратурой отозвали пропуск",
        ),
        (
            "Суд и прокуратура совместно отозвали пропуск.",
            "Суд отозвал пропуск",
            "Прокуратура и Суд совместно отозвали пропуск",
        ),
        (
            "Суд и прокуратура вместе отзывают пропуск.",
            "Прокуратура отзывает пропуск",
            "Прокуратура и Суд вместе отзывают пропуск",
        ),
    ),
)
def test_final_v3_explicit_joint_surface_is_atomic_in_every_path(
    source: str,
    unsafe: str,
    honest: str,
) -> None:
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        unsafe,
        honest=honest,
    )


def test_final_v3_joint_adverb_is_not_an_inform_predicate() -> None:
    source = "Суд и прокуратура сообща отозвали пропуск."
    normalized = _normalized_text(source)
    assert all(
        truth.predicate != "inform"
        for truth in _extract_relation_truths(normalized)
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Суд отозвал пропуск",
        honest="Прокуратура и Суд сообща отозвали пропуск",
    )
    assert any(
        truth.predicate == "inform"
        for truth in _extract_relation_truths(
            _normalized_text(
                "Суд сообщил прокуратуре информацию."
            )
        )
    )


def test_final_v3_or_particle_is_a_fail_closed_alternative() -> None:
    source = "Суд или же прокуратура отозвали пропуск."
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Суд отозвал пропуск",
        honest="Прокуратура или Суд отозвали пропуск",
    )


def test_final_v3_mixed_actor_expression_preserves_grouping() -> None:
    source = (
        "Суд совместно с прокуратурой или губернатор может отозвать "
        "пропуск."
    )
    unsafe = (
        "Суд или прокуратура совместно с губернатором может отозвать "
        "пропуск"
    )
    honest = (
        "Губернатор или прокуратура совместно с Судом может отозвать "
        "пропуск"
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        unsafe,
        honest=honest,
    )


def test_final_v3_permission_authority_expression_preserves_grouping() -> None:
    source = (
        "Организация Центр с разрешения суда совместно с прокуратурой "
        "или губернатора может отозвать пропуск."
    )
    unsafe = (
        "Организация Центр с разрешения суда или прокуратуры совместно "
        "с губернатором может отозвать пропуск"
    )
    _assert_final_v3_rejected_in_all_output_paths(source, unsafe)


@pytest.mark.parametrize(
    "exclusion",
    (
        "Правоохранители, кроме полиции",
        "Правоохранители, за исключением полиции",
        "Правоохранители, но не полиция",
        "Правоохранители (кроме полиции)",
    ),
)
def test_final_v3_roster_never_authorizes_excluded_family(
    exclusion: str,
) -> None:
    source = (
        "Лица, уполномоченные выдавать лицензию\r\n"
        "Право на выдачу лицензии имеют следующие лица:\r\n"
        f"- {exclusion}"
    )
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, "Полиция выдаёт лицензию")


def test_final_v3_roster_keeps_nonexcluded_family() -> None:
    source = (
        "Лица, уполномоченные выдавать лицензию\r\n"
        "Право на выдачу лицензии имеют следующие лица:\r\n"
        "- Правоохранители, кроме прокуратуры"
    )
    for validate in RELATION_VALIDATORS:
        validate(source, "Полиция выдаёт лицензию")
        with pytest.raises(ValueError):
            validate(source, "Прокуратура выдаёт лицензию")


@pytest.mark.parametrize(
    "output",
    (
        "Губернатор выдаёт ордер",
        "Верховный судья выдаёт ордер",
    ),
)
def test_final_v3_passive_present_issue_alternative_lists_authorities(
    output: str,
) -> None:
    source = "Ордер выдаётся губернатором или верховным судьёй."
    for validate in RELATION_VALIDATORS:
        validate(source, output)


def test_final_v3_passive_present_revocation_choice_stays_unresolved() -> None:
    source = "Пропуск отзывается губернатором или верховным судьёй."
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Губернатор отзывает пропуск",
        honest="Пропуск отзывается губернатором или верховным судьёй",
    )


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("Суд № 1", "Суд № 2"),
        ("LSPD Unit 1", "LSPD Unit 2"),
        ("Организация «Альфа 1»", "Организация «Альфа 2»"),
    ),
)
def test_final_v3_numeric_identity_cannot_pool_other_event(
    first: str,
    second: str,
) -> None:
    source = (
        f"{first} может отозвать пропуск. "
        f"{second} отозвал пропуск."
    )
    unsafe = f"{first} отозвал пропуск"
    honest = f"{second} отозвал пропуск"
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        unsafe,
        honest=honest,
    )


def test_final_v3_numeric_permission_authority_cannot_pool_other_event() -> None:
    source = (
        "Организация Центр с разрешения суда № 1 может отозвать пропуск. "
        "Организация Центр с разрешения суда № 2 отозвала пропуск."
    )
    unsafe = (
        "Организация Центр с разрешения суда № 1 отозвала пропуск"
    )
    honest = (
        "Организация Центр с разрешения суда № 2 отозвала пропуск"
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        unsafe,
        honest=honest,
    )


def test_final_v3_relative_permission_prerequisite_cannot_be_erased() -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как получит "
        "разрешение суда Альфа."
    )
    unsafe = "Организация Центр может отозвать пропуск"
    _assert_final_v3_rejected_in_all_output_paths(source, unsafe)


def test_final_v3_sequence_continuation_inherits_exact_subject() -> None:
    source = (
        "Организация Центр выдала лицензию, а затем отозвала пропуск."
    )
    for validate in RELATION_VALIDATORS:
        validate(source, "Организация Центр отозвала пропуск")
        with pytest.raises(ValueError):
            validate(source, "Прокуратура отозвала пропуск")
    validate_synonym_source_evidence(
        source,
        [
            "Организация Центр выдала лицензию",
            "Организация Центр отозвала пропуск",
        ],
    )


@pytest.mark.parametrize(
    ("source", "honest"),
    (
        (
            "Суд отзывает совместно с прокуратурой пропуск.",
            "Прокуратура совместно с судом отзывает пропуск",
        ),
        (
            "Совместно с прокуратурой Суд отзывает пропуск.",
            "Суд сообща с прокуратурой отзывает пропуск",
        ),
        (
            "Пропуск отзывается судом совместно с прокуратурой.",
            "Пропуск отзывается прокуратурой совместно с судом",
        ),
    ),
)
def test_final_v3_explicit_joint_is_atomic_in_every_action_position(
    source: str,
    honest: str,
) -> None:
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Суд отзывает пропуск",
        honest=honest,
    )


def test_final_v3_mixed_may_group_exposes_only_direct_branches() -> None:
    source = (
        "Суд совместно с прокуратурой или губернатор может отозвать пропуск."
    )
    for validate in RELATION_VALIDATORS:
        validate(source, "Губернатор может отозвать пропуск")
        validate(
            source,
            "Прокуратура совместно с судом может отозвать пропуск",
        )
        with pytest.raises(ValueError):
            validate(source, "Суд может отозвать пропуск")
    validate_synonym_source_evidence(
        source,
        [
            "Губернатор может отозвать пропуск",
            "Прокуратура совместно с судом может отозвать пропуск",
        ],
    )


def test_final_v3_relative_permission_accepts_bounded_subject_clause() -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как организация "
        "получит письменное разрешение суда Альфа."
    )
    unsafe = "Организация Центр может отозвать пропуск"
    honest = (
        "Организация Центр после получения письменного разрешения суда Альфа "
        "может отозвать пропуск"
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        unsafe,
        honest=honest,
    )


def test_final_v3_numeric_identity_normalizes_number_sign_spacing() -> None:
    assert _relation_actor_key(
        "Суд № 1",
        preserve_complete=True,
    ) == _relation_actor_key(
        "Суд №1",
        preserve_complete=True,
    )


def test_final_v3_malformed_semantic_group_never_reopens_first_actor() -> None:
    malformed = _relation_complete_postposed_actor_key("Суд или же")
    assert malformed is not None
    assert malformed.startswith("__unresolved_actor__:")
    assert _relation_complete_postposed_actor_key(
        "Организация Рога и Копыта"
    ) == "организаци рог и копыт"


@pytest.mark.parametrize(
    ("source", "unsafe", "honest"),
    (
        (
            "Суд отзывает пропуск совместно с прокуратурой.",
            "Суд отзывает пропуск",
            "Прокуратура совместно с судом отзывает пропуск",
        ),
        (
            "Суд обязан отозвать пропуск совместно с прокуратурой.",
            "Суд обязан отозвать пропуск",
            "Суд совместно с прокуратурой обязан отозвать пропуск",
        ),
        (
            "Совместно с прокуратурой пропуск отзывается судом.",
            "Суд отзывает пропуск",
            "Пропуск отзывается судом совместно с прокуратурой",
        ),
        (
            "Суд и прокуратура отозвали пропуск совместно.",
            "Суд отозвал пропуск",
            "Прокуратура и Суд совместно отозвали пропуск",
        ),
    ),
    ids=(
        "after-object",
        "mandatory-after-object",
        "before-passive-patient",
        "after-object-focus",
    ),
)
def test_final_v3_explicit_joint_role_is_position_independent(
    source: str,
    unsafe: str,
    honest: str,
) -> None:
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        unsafe,
        honest=honest,
    )


def test_final_v3_malformed_explicit_joint_never_reopens_one_actor() -> None:
    source = "Суд совместно с, прокуратура отзывают пропуск."
    unsafe = "Суд отзывает пропуск"
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, unsafe)
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [unsafe])


def test_final_v3_correlative_passive_choice_preserves_both_branches() -> None:
    issue = "Ордер выдаётся либо губернатором, либо верховным судьёй."
    for validate in RELATION_VALIDATORS:
        validate(issue, "Губернатор выдаёт ордер")
        validate(issue, "Верховный судья выдаёт ордер")
    validate_synonym_source_evidence(
        issue,
        ["Губернатор выдаёт ордер", "Верховный судья выдаёт ордер"],
    )

    revocation = (
        "Пропуск отзывается либо губернатором, либо верховным судьёй."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        revocation,
        "Губернатор отзывает пропуск",
        honest=(
            "Пропуск отзывается либо верховным судьёй, либо губернатором"
        ),
    )


@pytest.mark.parametrize(
    "adverb",
    (
        "немедленно",
        "повторно",
        "лично",
    ),
)
def test_final_v3_permission_scope_crosses_only_neutral_action_adverb(
    adverb: str,
) -> None:
    source = (
        "С разрешения суда Альфа организация может выдать лицензию, "
        f"а также {adverb} отозвать пропуск."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Организация может отозвать пропуск",
        honest=(
            "Организация с разрешения суда Альфа может отозвать пропуск"
        ),
    )


def test_final_v3_permission_scope_has_no_neutral_adverb_count_fail_open() -> None:
    source = (
        "С разрешения суда Альфа организация может выдать лицензию, "
        "а также немедленно повторно лично непосредственно именно "
        "отозвать пропуск."
    )
    unsafe = "Организация может отозвать пропуск"
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, unsafe)
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [unsafe])


def test_final_v3_relative_permission_preserves_holder_identity() -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        "заявитель получит разрешение суда Альфа."
    )
    unsafe = (
        "Организация Центр может отозвать пропуск после того, как "
        "организация получит разрешение суда Альфа"
    )
    _assert_final_v3_rejected_in_all_output_paths(source, unsafe)


def test_final_v3_relative_permission_preserves_finite_negation() -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как не "
        "получит разрешение суда Альфа."
    )
    unsafe = (
        "Организация Центр может отозвать пропуск после того, как "
        "получит разрешение суда Альфа"
    )
    _assert_final_v3_rejected_in_all_output_paths(source, unsafe)


@pytest.mark.parametrize(
    "relative_clause",
    (
        (
            "уполномоченная региональная государственная организация "
            "Центр поддержки граждан штата получит разрешение суда Альфа"
        ),
        "разрешение будет получено от суда Альфа",
    ),
    ids=("long-subject", "passive-receipt"),
)
def test_final_v3_relative_permission_never_fails_open(
    relative_clause: str,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"{relative_clause}."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
    )


def test_final_v3_overwindow_relative_permission_is_unresolved_not_erased() -> None:
    holder = ("уполномоченная региональная " * 32) + "организация Центр"
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"{holder} получит разрешение суда Альфа."
    )
    unsafe = "Организация Центр может отозвать пропуск"
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, unsafe)
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [unsafe])


def test_final_v3_explicit_calendar_date_cannot_be_dropped() -> None:
    source = "Суд 1 мая 2026 года может отозвать пропуск."
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Суд может отозвать пропуск",
    )


def test_final_v3_roster_generic_output_preserves_exact_exclusion() -> None:
    source = (
        "Лица, уполномоченные выдавать лицензию\r\n"
        "Право на выдачу лицензии имеют следующие лица:\r\n"
        "- Полиция, кроме LSPD"
    )
    unsafe = "Полиция выдаёт лицензию"
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, unsafe)
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [source, unsafe])


def test_final_v3_sequence_inheritance_keeps_plain_joint_search_paths() -> None:
    source = (
        "Суд и прокуратура выдали лицензию, а затем отозвали пропуск."
    )
    for validate in RELATION_VALIDATORS:
        validate(source, "Суд отозвал пропуск")
        validate(source, "Прокуратура отозвала пропуск")
    validate_synonym_source_evidence(
        source,
        ["Суд отозвал пропуск", "Прокуратура отозвала пропуск"],
    )


def test_final_v3_sequence_neutral_adverb_inherits_exact_subject() -> None:
    source = "Организация Центр выдала лицензию, и лично отозвала пропуск."
    for validate in RELATION_VALIDATORS:
        validate(source, "Организация Центр отозвала пропуск")
        with pytest.raises(ValueError):
            validate(source, "Прокуратура отозвала пропуск")


def test_final_v3_sequence_explicit_joint_remains_atomic() -> None:
    source = (
        "Суд совместно с прокуратурой выдали лицензию, а затем отозвали "
        "пропуск."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Суд отозвал пропуск",
        honest="Прокуратура совместно с Судом отозвали пропуск",
    )


def test_final_v3_sequence_factual_or_remains_atomic() -> None:
    source = (
        "Суд или прокуратура выдали лицензию, а затем отозвали пропуск."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Суд отозвал пропуск",
        honest="Прокуратура или Суд отозвали пропуск",
    )


def test_final_v3_distant_joint_clause_does_not_rebind_prior_action() -> None:
    source = (
        "Суд отозвал пропуск. Прокуратура действует совместно с "
        "губернатором."
    )
    for validate in RELATION_VALIDATORS:
        validate(source, "Суд отозвал пропуск")


@pytest.mark.parametrize(
    "honest",
    (
        "Суд совместно с прокуратурой может отозвать пропуск",
        "Суд совместно с губернатором может отозвать пропуск",
    ),
)
def test_final_v3_nested_joint_alternative_exposes_valid_combinations(
    honest: str,
) -> None:
    source = (
        "Суд совместно с (прокуратурой или губернатором) может "
        "отозвать пропуск."
    )
    for validate in RELATION_VALIDATORS:
        validate(source, honest)
        with pytest.raises(ValueError):
            validate(source, "Суд может отозвать пропуск")


def test_final_v3_mixed_permission_expression_is_logically_canonical() -> None:
    source = (
        "Организация Центр с разрешения суда Альфа и с согласия "
        "прокуратуры Бета или с одобрения губернатора Гамма может "
        "отозвать пропуск."
    )
    honest = (
        "Организация Центр с одобрения губернатора Гамма или с согласия "
        "прокуратуры Бета и с разрешения суда Альфа может отозвать пропуск"
    )
    for validate in RELATION_VALIDATORS:
        validate(source, honest)
    validate_synonym_source_evidence(source, [honest])


def test_final_v3_nested_paired_joint_keeps_expression_tree() -> None:
    source = (
        "(Как Суд, так и прокуратура) или губернатор может отозвать "
        "пропуск."
    )
    honest = (
        "Губернатор или прокуратура и Суд может отозвать пропуск"
    )
    for validate in RELATION_VALIDATORS:
        validate(source, honest)
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    "joint_surface",
    (
        " совместно с прокуратурой",
        ", совместно с прокуратурой",
        " (совместно с прокуратурой)",
        " — совместно с прокуратурой",
    ),
    ids=("plain", "comma", "parenthesized", "dash"),
)
def test_final_v3_explicit_joint_survives_qualified_object_tail(
    joint_surface: str,
) -> None:
    source = (
        "Суд отзывает территориальный пропуск на территорию тюрьмы"
        f"{joint_surface}."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Суд отзывает пропуск",
    )


@pytest.mark.parametrize("modifier_count", (40, 100), ids=("bounded", "overwindow"))
def test_final_v3_relative_permission_signal_has_no_gap_fail_open(
    modifier_count: int,
) -> None:
    modifiers = "важное " * modifier_count
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"заявитель получит {modifiers}разрешение суда Альфа."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
    )


def test_final_v3_relative_acquisition_is_a_permission_prerequisite() -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        "заявитель приобретёт разрешение суда Альфа."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
    )


def test_final_v3_explicit_joint_stops_before_qualified_patient() -> None:
    source = (
        "Суд отзывает совместно с прокуратурой ранее выданный "
        "пропуск."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Суд отзывает ранее выданный пропуск",
    )


def test_final_v3_leading_passive_joint_crosses_bounded_adjunct() -> None:
    source = (
        "Совместно с прокуратурой после проверки пропуск отзывается "
        "судом."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "После проверки пропуск отзывается судом",
    )


def test_final_v3_empty_explicit_joint_partner_is_unresolved() -> None:
    source = "Суд отзывает пропуск совместно с."
    unsafe = "Суд отзывает пропуск"
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, unsafe)
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [unsafe])


@pytest.mark.parametrize(
    "modifier",
    (
        "автоматически",
        "временно",
        "одновременно",
        "дополнительно",
        "своевременно",
        "сразу же",
        "ещё раз",
        "единолично",
        "самостоятельно",
        "в дальнейшем",
        "при необходимости",
        "в установленный срок",
    ),
)
def test_final_v3_permission_scope_uses_syntax_not_adverb_whitelist(
    modifier: str,
) -> None:
    source = (
        "С разрешения суда Альфа организация может выдать лицензию, "
        f"а также {modifier} отозвать пропуск."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        f"Организация может {modifier} отозвать пропуск",
    )


def test_final_v3_permission_does_not_cross_a_new_explicit_subject() -> None:
    source = (
        "С разрешения суда Альфа организация Центр выдала лицензию, "
        "а организация Бета отозвала пропуск."
    )
    for validate in RELATION_VALIDATORS:
        validate(source, "Организация Бета отозвала пропуск")


@pytest.mark.parametrize(
    "relative_clause",
    (
        "суд выдаст разрешение",
        "суд предоставит согласие",
    ),
    ids=("issue-permission", "provide-consent"),
)
def test_final_v3_relative_permission_supports_active_issuer_voice(
    relative_clause: str,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"{relative_clause}."
    )
    _assert_final_v3_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
    )


def test_final_v3_nested_joint_matches_equivalent_dnf() -> None:
    source = (
        "Суд совместно с (прокуратурой или губернатором) может "
        "отозвать пропуск."
    )
    honest = (
        "(Суд совместно с прокуратурой) или "
        "(Суд совместно с губернатором) может отозвать пропуск"
    )
    for validate in RELATION_VALIDATORS:
        validate(source, honest)
    validate_synonym_source_evidence(source, [honest])


@pytest.mark.parametrize(
    "honest",
    (
        "Суд совместно с прокуратурой может отозвать пропуск",
        "Суд совместно с губернатором может отозвать пропуск",
    ),
)
def test_final_v3_post_object_nested_joint_exposes_valid_branches(
    honest: str,
) -> None:
    source = (
        "Суд может отозвать пропуск совместно с "
        "(прокуратурой или губернатором)."
    )
    for validate in RELATION_VALIDATORS:
        validate(source, honest)


@pytest.mark.parametrize(
    "source",
    (
        "Организация Центр выдала лицензию, затем лично отозвала пропуск.",
        "Организация Центр выдала лицензию, после чего отозвала пропуск.",
    ),
    ids=("then", "after-which"),
)
def test_final_v3_natural_sequence_connector_inherits_subject(
    source: str,
) -> None:
    for validate in RELATION_VALIDATORS:
        validate(source, "Организация Центр отозвала пропуск")


def test_final_v3_parenthesized_permission_conjunction_is_equivalent() -> None:
    source = (
        "Организация Центр (с разрешения суда Альфа и с согласия "
        "прокуратуры Бета) может отозвать пропуск."
    )
    honest = (
        "Организация Центр с согласия прокуратуры Бета и с "
        "разрешения суда Альфа может отозвать пропуск"
    )
    for validate in RELATION_VALIDATORS:
        validate(source, honest)
    validate_synonym_source_evidence(source, [honest])


def test_final_v3_source_condition_supports_lexically_owned_ground_reorder() -> None:
    source = (
        "В случае нарушения уголовных статей, подразумевающих насилие в "
        "какой-либо форме: теракт, похищение, убийство - лицензия парамедика "
        "изымается."
    )
    honest = (
        "Лицензия парамедика изымается за нарушение уголовной статьи об "
        "убийстве"
    )
    for validate in RELATION_VALIDATORS:
        validate(source, honest)
        with pytest.raises(ValueError):
            validate(
                source,
                "Лицензия парамедика изымается за нарушение уголовной статьи о "
                "краже",
            )
    validate_synonym_source_evidence(source, [honest])
