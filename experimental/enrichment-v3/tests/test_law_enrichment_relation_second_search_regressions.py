from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    validate_enrichment_title_source_evidence,
    validate_synonym_source_evidence,
)


Validator = Callable[[str, str], None]


def _title(source: str, output: str) -> None:
    validate_enrichment_title_source_evidence(
        source,
        output,
        source_title=None,
    )


def _synonym(source: str, output: str) -> None:
    validate_synonym_source_evidence(source, [output])


VALIDATORS: tuple[Validator, ...] = (_title, _synonym)


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_explicit_revocation_ground_cannot_be_dropped(
    validate: Validator,
) -> None:
    source = "Суд аннулирует лицензию только за нарушение закона."
    with pytest.raises(ValueError):
        validate(source, "Суд аннулирует лицензию")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_past_revocation_ground_cannot_be_dropped(
    validate: Validator,
) -> None:
    source = "Суд аннулировал лицензию за нарушение закона."
    with pytest.raises(ValueError):
        validate(source, "Суд аннулировал лицензию")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_decision_authority_cannot_be_dropped_into_unconditional_issue(
    validate: Validator,
) -> None:
    source = "Прокуратура выдаёт ордер только по решению суда."
    with pytest.raises(ValueError):
        validate(source, "Прокуратура выдаёт ордер")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_actor_qualification_cannot_be_dropped(
    validate: Validator,
) -> None:
    source = "Директор FIB выдаёт допуск."
    with pytest.raises(ValueError):
        validate(source, "Директор выдаёт допуск")


@pytest.mark.parametrize(
    "output",
    (
        "Гражданин всегда арестовывается",
        "Имущество автоматически конфискуется",
        "Организация обязательно распускается",
    ),
    ids=("arrest", "confiscation", "dissolution"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_other_open_mandatory_legal_effect_requires_source(
    validate: Validator,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate("Настоящая статья устанавливает общий порядок.", output)


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_negative_duty_cannot_become_positive(
    validate: Validator,
) -> None:
    source = "Суд не обязан закрывать организацию."
    with pytest.raises(ValueError):
        validate(source, "Суд обязан закрывать организацию")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_positive_duty_cannot_become_negative(
    validate: Validator,
) -> None:
    source = "Суд обязан закрывать организацию."
    with pytest.raises(ValueError):
        validate(source, "Суд не обязан закрывать организацию")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_added_information_recipient_is_rejected(
    validate: Validator,
) -> None:
    source = "Полиция обязана сообщить FIB информацию об угрозе."
    with pytest.raises(ValueError):
        validate(
            source,
            "Полиция обязана сообщить FIB и CIA информацию об угрозе",
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_passive_information_roles_cannot_be_inverted(
    validate: Validator,
) -> None:
    source = "Информация всегда передаётся FIB полицией."
    with pytest.raises(ValueError):
        validate(
            source,
            "FIB всегда передаёт полиции информацию",
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_generic_condition_cannot_be_changed(
    validate: Validator,
) -> None:
    source = (
        "При совершении тяжкого преступления суд обязан "
        "аннулировать лицензию."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "При получении обычной жалобы суд обязан "
                "аннулировать лицензию"
            ),
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_generic_condition_can_be_reordered_faithfully(
    validate: Validator,
) -> None:
    source = (
        "Суд обязан аннулировать лицензию, если совершено "
        "тяжкое преступление."
    )
    validate(
        source,
        (
            "При совершении тяжкого преступления суд обязан "
            "аннулировать лицензию"
        ),
    )


@pytest.mark.parametrize(
    "output",
    (
        "Суд может выдать ордер",
        "Прокуратура может выдать ордер",
    ),
    ids=("first", "second"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_alternative_may_actors_are_each_supported(
    validate: Validator,
    output: str,
) -> None:
    validate("Суд или прокуратура могут выдать ордер.", output)


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_unlisted_instrumental_container_cannot_become_actor(
    validate: Validator,
) -> None:
    source = "Лицензия оформляется указом."
    with pytest.raises(ValueError):
        validate(source, "Указ оформляет лицензию")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_open_object_head_cannot_hide_different_noun(
    validate: Validator,
) -> None:
    source = "Суд всегда ликвидирует частное предприятие."
    with pytest.raises(ValueError):
        validate(source, "Суд всегда ликвидирует частный клуб")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_mandatory_issue_condition_cannot_be_changed(
    validate: Validator,
) -> None:
    source = (
        "При успешной проверке суд обязан выдать лицензию."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "При обычной жалобе суд обязан выдать лицензию",
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_condition_identity_cannot_ignore_words_after_token_twelve(
    validate: Validator,
) -> None:
    source = (
        "Если заявитель своевременно подал все необходимые документы "
        "в установленной форме через официальный портал до истечения "
        "срока, суд обязан закрыть организацию."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "Если заявитель своевременно подал все необходимые "
                "документы в установленной форме через официальный "
                "портал после истечения срока, суд обязан закрыть "
                "организацию"
            ),
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_condition_identity_cannot_collapse_different_laws(
    validate: Validator,
) -> None:
    source = (
        "При выявлении нарушения закона о лицензировании суд обязан "
        "закрыть организацию."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "При выявлении нарушения закона о здравоохранении "
                "суд обязан закрыть организацию"
            ),
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_presence_condition_cannot_drop_second_required_fact(
    validate: Validator,
) -> None:
    source = (
        "При наличии ордера и письменного согласия владельца "
        "полиция вправе обыскать дом."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "При наличии ордера полиция вправе обыскать дом",
        )


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Информация всегда сообщается FIB полицией.",
            "FIB всегда сообщает полиции информацию",
        ),
        (
            "Сведения всегда передаются USSS полицией.",
            "USSS всегда передаёт полиции сведения",
        ),
    ),
    ids=("reported", "transferred-plural"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_passive_information_variants_cannot_invert_roles(
    validate: Validator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    ("container", "subject"),
    (
        ("указом", "Указ"),
        ("законом", "Закон"),
        ("договором", "Договор"),
        ("постановлением", "Постановление"),
    ),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_instrumental_legal_container_family_cannot_become_actor(
    validate: Validator,
    container: str,
    subject: str,
) -> None:
    source = f"Лицензия оформляется {container}."
    with pytest.raises(ValueError):
        validate(source, f"{subject} оформляет лицензию")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_open_passive_roles_cannot_be_reversed(
    validate: Validator,
) -> None:
    source = "Предприятие всегда ликвидируется судом."
    with pytest.raises(ValueError):
        validate(source, "Предприятие всегда ликвидирует суд")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_heading_without_colon_stops_old_actor_inheritance(
    validate: Validator,
) -> None:
    source = (
        "Полиция обязана:\n"
        "— уведомить гражданина.\n"
        "ОБЩИЕ ПОЛОЖЕНИЯ\n"
        "Губернатор обязан закрыть организацию."
    )
    validate(source, "Губернатор обязан закрыть организацию")
    with pytest.raises(ValueError):
        validate(source, "Полиция обязана закрыть организацию")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_each_actor_in_coordinated_duty_heading_is_supported(
    validate: Validator,
) -> None:
    source = (
        "Полиция и прокуратура обязаны:\n"
        "— уведомлять граждан."
    )
    validate(source, "Полиция обязана уведомлять граждан")
    validate(source, "Прокуратура обязана уведомлять граждан")


@pytest.mark.parametrize(
    "connector",
    ("кроме того", "при этом", "вместе с тем"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_common_legal_connector_starts_new_explicit_subject_clause(
    validate: Validator,
    connector: str,
) -> None:
    source = (
        f"Суд рассматривает жалобу, {connector} полиция обязана "
        "обыскать дом."
    )
    validate(source, "Полиция обязана обыскать дом")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_trailing_exception_cannot_be_dropped(
    validate: Validator,
) -> None:
    source = (
        "Суд обязан закрыть организацию, за исключением случаев "
        "действия моратория."
    )
    with pytest.raises(ValueError):
        validate(source, "Суд обязан закрыть организацию")


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_temporal_precondition_cannot_be_dropped(
    validate: Validator,
) -> None:
    source = (
        "Суд обязан закрыть организацию только после вступления "
        "решения в силу."
    )
    with pytest.raises(ValueError):
        validate(source, "Суд обязан закрыть организацию")


@pytest.mark.parametrize(
    "output",
    (
        "Суд обязан закрыть организацию",
        "Прокуратура обязана закрыть организацию",
        "Полиция обязана закрыть организацию",
    ),
    ids=("court", "prosecution", "police"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_comma_coordinated_actors_are_each_supported(
    validate: Validator,
    output: str,
) -> None:
    validate(
        "Суд, прокуратура и полиция обязаны закрыть организацию.",
        output,
    )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_comma_coordinated_actors_do_not_authorize_fourth_actor(
    validate: Validator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд, прокуратура и полиция обязаны закрыть организацию.",
            "Губернатор обязан закрыть организацию",
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_each_actor_in_coordinated_power_heading_is_supported(
    validate: Validator,
) -> None:
    source = (
        "Права полиции и прокуратуры:\n"
        "— обыскивать дом при наличии ордера."
    )
    validate(
        source,
        "Полиция вправе обыскать дом при наличии ордера",
    )
    validate(
        source,
        "Прокуратура вправе обыскать дом при наличии ордера",
    )


@pytest.mark.parametrize(
    ("source", "bad_output"),
    (
        (
            "Суд не вправе выдавать ордер.",
            "Суд вправе выдавать ордер",
        ),
        (
            "Суд не имеет права выдавать ордер.",
            "Суд имеет право выдавать ордер",
        ),
    ),
    ids=("not-entitled", "has-no-right"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_negative_permission_cannot_become_positive(
    validate: Validator,
    source: str,
    bad_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)


@pytest.mark.parametrize(
    "source",
    (
        "Суд выдаёт не ордер, а лицензию.",
        "Суд выдаёт лицензию, но не ордер.",
    ),
    ids=("not-x-but-y", "y-but-not-x"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_negated_direct_object_cannot_become_positive(
    validate: Validator,
    source: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "Суд выдаёт ордер")


@pytest.mark.parametrize(
    ("source", "bad_output"),
    (
        (
            "Полиция обязана задержать подозреваемого.",
            "Полиция обязана задержать свидетеля",
        ),
        (
            "Суд обязан освободить заложника.",
            "Суд обязан освободить преступника",
        ),
        (
            "Полиция обязана обыскать адвоката.",
            "Полиция обязана обыскать судью",
        ),
    ),
    ids=("detain", "release", "search"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_unknown_human_role_object_cannot_be_replaced(
    validate: Validator,
    source: str,
    bad_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_information_topic_cannot_become_recipient(
    validate: Validator,
) -> None:
    source = "Полиция обязана сообщить информацию о FIB."
    with pytest.raises(ValueError):
        validate(
            source,
            "Полиция обязана сообщить FIB информацию",
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_information_origin_cannot_become_recipient(
    validate: Validator,
) -> None:
    source = "Полиция обязана передать информацию от FIB."
    with pytest.raises(ValueError):
        validate(
            source,
            "Полиция обязана передать FIB информацию",
        )


@pytest.mark.parametrize(
    "output",
    (
        "Суд выдал ордер",
        "Прокуратура выдала ордер",
    ),
    ids=("court", "prosecution"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_coordinated_passive_instrumental_actors_are_each_supported(
    validate: Validator,
    output: str,
) -> None:
    validate("Ордер выдан судом и прокуратурой.", output)


@pytest.mark.parametrize(
    "output",
    (
        "Суд выдаёт ордер",
        "Прокуратура выдаёт ордер",
    ),
    ids=("court", "prosecution"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_coordinated_postposed_actors_are_each_supported(
    validate: Validator,
    output: str,
) -> None:
    validate("Ордер выдают суд и прокуратура.", output)


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_past_certificate_revocation_keeps_actor(
    validate: Validator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд аннулировал сертификат.",
            "Прокуратура аннулировала сертификат",
        )


@pytest.mark.parametrize(
    ("source", "bad_output"),
    (
        (
            "Суд выдаёт временную лицензию.",
            "Суд выдаёт постоянную лицензию",
        ),
        (
            "Суд выдаёт региональный пропуск.",
            "Суд выдаёт федеральный пропуск",
        ),
    ),
    ids=("temporary-permanent", "regional-federal"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_regulated_object_qualifier_cannot_be_replaced(
    validate: Validator,
    source: str,
    bad_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_access_pass_territory_cannot_be_replaced(
    validate: Validator,
) -> None:
    source = (
        "Руководитель вправе выдавать пропуск только на территорию "
        "больницы."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "Руководитель вправе выдавать пропуск только на "
                "территорию тюрьмы"
            ),
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_warrant_recipient_cannot_be_replaced(
    validate: Validator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд выдаёт ордер полиции.",
            "Суд выдаёт ордер прокуратуре",
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_license_recipient_cannot_be_replaced(
    validate: Validator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд выдаёт лицензию организации.",
            "Суд выдаёт лицензию гражданину",
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_unlisted_revocation_ground_cannot_be_replaced(
    validate: Validator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд аннулирует лицензию за подделку документов.",
            "Суд аннулирует лицензию за потерю паспорта",
        )


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_unknown_issue_purpose_cannot_be_replaced(
    validate: Validator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд выдаёт лицензию для медицинской деятельности.",
            "Суд выдаёт лицензию для торговли оружием",
        )


@pytest.mark.parametrize(
    ("source", "bad_output"),
    (
        (
            "Лицензия автоматически замораживается.",
            "Лицензия автоматически аннулируется",
        ),
        (
            "Суд приостанавливает лицензию.",
            "Суд отзывает лицензию",
        ),
    ),
    ids=("freeze-to-annul", "suspend-to-revoke"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_temporary_revocation_effect_cannot_become_permanent(
    validate: Validator,
    source: str,
    bad_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)


@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_joint_actor_requirement_cannot_become_single_actor(
    validate: Validator,
) -> None:
    source = (
        "Суд и прокуратура обязаны совместно подписать ордер."
    )
    with pytest.raises(ValueError):
        validate(source, "Прокуратура обязана подписать ордер")


@pytest.mark.parametrize(
    ("bad_output",),
    (
        ("Суд выдаёт лицензию",),
        ("Прокуратура выдаёт ордер",),
    ),
    ids=("court-cross-pair", "prosecution-cross-pair"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_respectively_actor_object_pairs_cannot_be_crossed(
    validate: Validator,
    bad_output: str,
) -> None:
    source = (
        "Суд и прокуратура выдают соответственно ордер и лицензию."
    )
    with pytest.raises(ValueError):
        validate(source, bad_output)


@pytest.mark.parametrize(
    "supported_output",
    (
        "Суд выдаёт ордер",
        "Прокуратура выдаёт лицензию",
    ),
    ids=("court-warrant", "prosecution-license"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_respectively_actor_object_pairs_keep_correct_pair(
    validate: Validator,
    supported_output: str,
) -> None:
    validate(
        "Суд и прокуратура выдают соответственно ордер и лицензию.",
        supported_output,
    )


@pytest.mark.parametrize(
    ("source", "bad_output"),
    (
        (
            "Директор FIB обязан закрыть организацию.",
            "Директор обязан закрыть организацию",
        ),
        (
            "Заместитель губернатора обязан закрыть организацию.",
            "Заместитель обязан закрыть организацию",
        ),
    ),
    ids=("director-fib", "governor-deputy"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_mandatory_actor_qualification_cannot_be_dropped(
    validate: Validator,
    source: str,
    bad_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)


@pytest.mark.parametrize(
    ("source", "bad_output"),
    (
        (
            "Допуск оформляется приказом.",
            "Допуск оформляется протоколом",
        ),
        (
            "Лицензия оформляется в виде постановления.",
            "Лицензия оформляется в виде договора",
        ),
    ),
    ids=("order-to-protocol", "decree-to-contract"),
)
@pytest.mark.parametrize("validate", VALIDATORS, ids=("title", "synonym"))
def test_regulated_document_form_cannot_be_replaced(
    validate: Validator,
    source: str,
    bad_output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)
