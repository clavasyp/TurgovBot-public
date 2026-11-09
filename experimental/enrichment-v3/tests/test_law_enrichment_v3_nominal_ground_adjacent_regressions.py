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
    synonyms = [source_surface]
    if output != source_surface:
        synonyms.append(output)
    validate_synonym_source_evidence(source, synonyms)


def _validate_safe_seed_set(source: str, output: str) -> None:
    validate_synonym_source_evidence(
        source,
        [
            "название не должно нарушать законодательство штата",
            output,
        ],
    )


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)

SAFE_SEED_OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_safe_seed_set,
)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд может отозвать лицензию за правонарушение с использованием "
            "взрывчатки.",
            "отзыв лицензии судом за правонарушение",
        ),
        (
            "Суд может отозвать лицензию за правонарушение с использованием "
            "поддельных документов.",
            "отзыв лицензии судом за правонарушение",
        ),
        (
            "Суд может отозвать пропуск за правонарушение с использованием "
            "огнестрельного оружия.",
            "отзыв пропуска судом за правонарушение",
        ),
        (
            "Губернатор вправе аннулировать пропуск, если лицо препятствует "
            "работе государственной организации.",
            "аннулирование пропуска губернатором за помехи работе",
        ),
        (
            "Губернатор вправе аннулировать пропуск, если лицо препятствует "
            "работе государственной организации в здании суда.",
            "аннулирование пропуска губернатором за помехи работе "
            "госорганизации",
        ),
        (
            "Губернатор вправе аннулировать пропуск, если лицо препятствует "
            "работе государственной организации.",
            "аннулирование пропуска губернатором за помехи работе "
            "госорганизации в тюрьме",
        ),
    ),
    ids=(
        "license-does-not-drop-explosives",
        "license-does-not-drop-forged-documents",
        "firearm-abstraction-is-license-only",
        "sf-obstruction-does-not-drop-object",
        "sf-obstruction-does-not-drop-location",
        "sf-obstruction-does-not-invent-location",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_ground_abstraction_stays_bounded(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_condition_ground_paraphrase_does_not_weaken_finite_output(
    validate: RelationValidator,
) -> None:
    source = (
        "Губернатор вправе аннулировать пропуск, если лицо препятствует "
        "работе государственной организации в здании суда."
    )
    unsafe = (
        "Губернатор вправе аннулировать пропуск за помехи работе "
        "госорганизации."
    )

    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_supports_exact_enumerated_violation(
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, "
        "за нарушение любого из положений статьи последует отзыв лицензии."
    )

    validate(
        source,
        "за нарушение законодательства штата последует отзыв лицензии",
    )


@pytest.mark.parametrize(
    "output",
    (
        "за нарушение законодательства штата последует отзыв лицензии",
        "отзыв лицензии за нарушение законодательства штата",
    ),
    ids=("leading-ground", "postposed-ground"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_supports_real_modal_enumeration(
    output: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название, атрибутика, печать и реклама не должны быть плагиатом, "
        "иметь сходство с зарегистрированными брендами, нарушать моральные "
        "нормы, нарушать в той или иной мере законодательство штата, за "
        "нарушением любого из положений статьи последует отказ в регистрации "
        "и/или отзыв лицензии."
    )

    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_can_name_an_earlier_governed_list_item(
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, оскорблять "
        "чувства верующих, за нарушение любого из положений статьи последует "
        "отзыв лицензии."
    )

    validate(
        source,
        "за нарушение законодательства штата последует отзыв лицензии",
    )


@pytest.mark.parametrize(
    "unsafe",
    (
        "за нарушение федерального законодательства последует отзыв лицензии",
        "за нарушение санитарных норм последует отзыв лицензии",
        "за нарушение законодательства другого штата последует отзыв лицензии",
    ),
    ids=("invented-kind", "invented-ground", "changed-identity"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_does_not_invent_a_violation(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, "
        "за нарушение любого из положений статьи последует отзыв лицензии."
    )

    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_requires_an_explicit_violation_clause(
    validate: RelationValidator,
) -> None:
    source = (
        "Название регулируется законодательством штата, а символика не должна "
        "нарушать моральные нормы, за нарушение любого из положений статьи "
        "последует отзыв лицензии."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "source",
    (
        (
            "В примере гражданин нарушает законодательство штата. Название "
            "не должно нарушать моральные нормы, за нарушение любого из "
            "положений статьи последует отзыв лицензии."
        ),
        (
            "Организация «Нарушение законодательства штата» зарегистрирована. "
            "Название не должно нарушать моральные нормы, за нарушение любого "
            "из положений статьи последует отзыв лицензии."
        ),
        (
            "Название не должно нарушать моральные нормы при применении "
            "законодательства штата, за нарушение любого из положений статьи "
            "последует отзыв лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "положений статьи последует отзыв лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата в сфере "
            "рекламы, за нарушение любого из положений статьи последует отзыв "
            "лицензии."
        ),
        (
            "Название не должно грубо нарушать законодательство штата, за "
            "нарушение любого из положений статьи последует отзыв лицензии."
        ),
        (
            "Название не должно позволять лицам нарушать законодательство "
            "штата, за нарушение любого из положений статьи последует отзыв "
            "лицензии."
        ),
        (
            "Название не должно не нарушать законодательство штата, за "
            "нарушение любого из положений статьи последует отзыв лицензии."
        ),
        (
            "Название не должно быть плагиатом, но может нарушать "
            "законодательство штата, за нарушение любого из положений статьи "
            "последует отзыв лицензии."
        ),
        (
            "Название не должно, как правило, нарушать законодательство "
            "штата, за нарушение любого из положений статьи последует отзыв "
            "лицензии."
        ),
        (
            "Название не должно, при наличии решения суда, нарушать "
            "законодательство штата, за нарушение любого из положений статьи "
            "последует отзыв лицензии."
        ),
        (
            "Название не должно, по решению губернатора, нарушать "
            "законодательство штата, за нарушение любого из положений статьи "
            "последует отзыв лицензии."
        ),
        (
            "Название не должно «только при ЧП» нарушать законодательство "
            "штата, за нарушение любого из положений статьи последует отзыв "
            "лицензии."
        ),
        (
            "Название не должно позволять лицам, нарушать законодательство "
            "штата, за нарушение любого из положений статьи последует отзыв "
            "лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи, а последует отзыв лицензии."
        ),
    ),
    ids=(
        "descriptive-earlier-sentence",
        "quoted-name",
        "unrelated-oblique-tail",
        "no-universal-quantifier",
        "required-tail-dropped",
        "severity-dropped",
        "indirect-infinitive",
        "double-negation",
        "fresh-modal-after-soft-separator",
        "comma-parenthetical-modifier",
        "comma-condition-modifier",
        "comma-authority-modifier",
        "quoted-modifier",
        "comma-control-infinitive",
        "wrong-consequence-separator",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_bridge_is_not_laundered(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует запись «Отзыв Лицензии»."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует регистрация записи "
            "(«отзыв лицензии»)."
        ),
    ),
    ids=("quoted-consequence-label", "parenthetical-consequence-label"),
)
@pytest.mark.parametrize(
    "validate",
    SAFE_SEED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "safe-seed-set"),
)
def test_v3_article_catchall_requires_a_real_consequence_action(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует обсуждение возможного отзыва "
            "лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует запись: отзыв лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует отчет об отзыве лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует предупреждение о возможном "
            "отзыве лицензии."
        ),
    ),
    ids=(
        "discussion-topic",
        "record-label",
        "report-topic",
        "warning-topic",
    ),
)
@pytest.mark.parametrize(
    "validate",
    SAFE_SEED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "safe-seed-set"),
)
def test_v3_article_catchall_consequence_is_not_a_topic_label(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Название не должно нарушать законодательство штата, но не за "
            "нарушение любого из положений статьи последует отзыв лицензии, "
            "а за мошенничество."
        ),
        (
            "Название не должно нарушать законодательство штата, однако "
            "отнюдь не за нарушение любого из положений статьи последует "
            "отзыв лицензии, а за мошенничество."
        ),
    ),
    ids=("negative-focus", "emphatic-negative-focus"),
)
@pytest.mark.parametrize(
    "validate",
    SAFE_SEED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "safe-seed-set"),
)
def test_v3_article_catchall_preserves_ground_polarity(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "prefix",
    ("но вряд ли", "но возможно", "но по слухам"),
    ids=("unlikely", "possible", "hearsay"),
)
@pytest.mark.parametrize(
    "validate",
    SAFE_SEED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "safe-seed-set"),
)
def test_v3_article_catchall_must_start_its_clause_item(
    prefix: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, "
        f"{prefix} за нарушение любого из положений статьи последует отзыв "
        "лицензии."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "validate",
    SAFE_SEED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "safe-seed-set"),
)
def test_v3_article_catchall_keeps_additive_not_only_ground(
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, не только за "
        "нарушение любого из положений статьи последует отзыв лицензии, но и "
        "за мошенничество."
    )

    validate(
        source,
        "за нарушение законодательства штата последует отзыв лицензии",
    )


@pytest.mark.parametrize(
    "modifier",
    ("по слухам", "вероятно", "возможно", "как утверждают свидетели"),
    ids=("hearsay", "probable", "possible", "reported"),
)
@pytest.mark.parametrize(
    "validate",
    SAFE_SEED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "safe-seed-set"),
)
def test_v3_article_catchall_rejects_modifier_comma_island(
    modifier: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, "
        f"{modifier}, за нарушение любого из положений статьи последует отзыв "
        "лицензии."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "controller",
    ("позволять", "разрешать", "требовать", "поручать"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_does_not_escape_controller_owned_list(
    controller: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Название не должно {controller} гражданам следующее: рекламировать "
        "товары, а также нарушать законодательство штата, за нарушение любого "
        "из положений статьи последует отзыв лицензии."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "intro",
    (
        "Только если организация зарегистрирована в штате",
        "Если организация зарегистрирована в штате",
        "Когда организация зарегистрирована в штате",
        "В случае регистрации организации в штате",
        "При условии регистрации организации в штате",
        "За исключением организаций вне штата",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_preserves_leading_prerequisite(
    intro: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"{intro}, название не должно нарушать законодательство штата, за "
        "нарушение любого из положений статьи последует отзыв лицензии."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "tail",
    (
        "только при повторном нарушении",
        "только после решения суда",
        "при условии, что суд подтвердит нарушение",
        "если только суд не решит иначе",
        "по усмотрению суда",
        "кроме случаев чрезвычайного положения",
        "по слухам",
        "не как санкция, а как тема обсуждения",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_preserves_post_action_tail(
    tail: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, за нарушение "
        "любого из положений статьи последует отзыв лицензии "
        f"{tail}."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует отзыв лицензии или "
            "предупреждение."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует отмена отзыва лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует приостановление отзыва "
            "лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует отзыв проекта лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует отзыв не самой лицензии, а "
            "записи."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует отзыв лицензии?"
        ),
    ),
    ids=(
        "alternative",
        "cancellation-container",
        "suspension-container",
        "project-container",
        "negated-patient",
        "questioned-consequence",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_preserves_consequence_topology_and_patient(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "modal",
    (
        "может последовать",
        "мог бы последовать",
        "вправе последовать",
        "должен последовать",
        "обязан последовать",
        "может повлечь",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_grounded_nominal_does_not_fail_open_without_candidate(
    modal: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, за нарушение "
        f"любого из положений статьи {modal} отзыв лицензии."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_preserves_conjunctive_consequence(
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, за нарушение "
        "любого из положений статьи последует отзыв лицензии и предупреждение."
    )

    validate(
        source,
        "за нарушение законодательства штата последует отзыв лицензии",
    )


def test_v3_generic_authorization_keeps_source_owned_pass_use_ground() -> None:
    source = (
        "Пропуск может быть аннулирован судом в качестве дополнительной "
        "меры наказания, когда преступление совершено с его использованием."
    )
    output = "отмена разрешения за совершение преступления с пропуском"

    _validate_title(source, output)
    validate_synonym_source_evidence(
        source,
        [
            output,
            "лишение допуска судом как дополнительное наказание",
        ],
    )
    _validate_full_set(source, output)


@pytest.mark.parametrize(
    "unsafe",
    (
        "отмена разрешения за убийство с пропуском",
        "отмена разрешения за совершение преступления",
        "отмена разрешения за совершение преступления с чужим пропуском",
        "отмена разрешения за его использование",
        "отмена разрешения за преступное применение",
    ),
    ids=(
        "different-offence",
        "drops-pass-use",
        "invents-pass-owner",
        "anaphor-has-no-pass-anchor",
        "generic-application-has-no-pass-anchor",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_generic_authorization_cannot_invent_or_drop_pass_use_ground(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом в качестве дополнительной "
        "меры наказания, когда преступление совершено с его использованием."
    )

    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        "Суд может отозвать пропуск.",
        (
            "Суд может отозвать лицензию за совершение преступления "
            "с использованием пропуска."
        ),
    ),
    ids=("ungrounded-access-pass", "different-document-family"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_generic_authorization_alias_requires_exact_pass_use_contract(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "отзыв разрешения судом за совершение преступления с пропуском",
        )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Причинами для аннулирования сертификата не являются:\n"
            "Нарушение Уголовного Кодекса."
        ),
        (
            "Основаниями для аннулирования сертификата не являются:\n"
            "Нарушение Уголовного Кодекса."
        ),
        (
            "Причинами для аннулирования сертификата не могут являться:\n"
            "Нарушение Уголовного Кодекса."
        ),
        (
            "Причинами для аннулирования сертификата не должны являться:\n"
            "Нарушение Уголовного Кодекса."
        ),
        (
            "Причинами для аннулирования сертификата не обязательно "
            "являются:\nНарушение Уголовного Кодекса."
        ),
    ),
    ids=(
        "negative-reasons-heading",
        "negative-grounds-heading",
        "negative-may-heading",
        "negative-mandatory-heading",
        "negative-epistemic-heading",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_negative_ground_heading_cannot_support_positive_reason(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "Нарушение Уголовного Кодекса — основание для "
                "аннулирования сертификата"
            ),
        )


@pytest.mark.parametrize(
    "heading",
    (
        "Причинами для аннулирования сертификата являются:",
        "Причины для аннулирования сертификата:",
    ),
    ids=("positive-copula", "positive-ellipsis"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_positive_ground_heading_still_supports_exact_reason(
    heading: str,
    validate: RelationValidator,
) -> None:
    source = f"{heading}\nНарушение Уголовного Кодекса."
    validate(
        source,
        (
            "Нарушение Уголовного Кодекса — основание для "
            "аннулирования сертификата"
        ),
    )


@pytest.mark.parametrize(
    "controller",
    (
        "заставлять",
        "обязывать",
        "предписывать",
        "просить",
        "убеждать",
        "склонять",
        "инструктировать",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_rejects_any_dependent_colon_list(
    controller: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Название не должно {controller} гражданам следующее: рекламировать "
        "товары, а также нарушать законодательство штата, за нарушение любого "
        "из положений статьи последует отзыв лицензии."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "intro",
    (
        "Лишь если организация зарегистрирована в штате",
        "При наличии решения суда",
        "До тех пор пока действует регистрация",
        "В отсутствие решения суда",
        "Кроме организаций вне штата",
        "Исключительно после регистрации",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_rejects_unconsumed_premodal_segment(
    intro: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"{intro}, название не должно нарушать законодательство штата, за "
        "нарушение любого из положений статьи последует отзыв лицензии."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "tail",
    (
        "и только после решения суда",
        "и применяется только после решения суда",
        "и то лишь при повторном нарушении",
        "и не всегда",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_requires_complete_conjunctive_consequence(
    tail: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, за нарушение "
        "любого из положений статьи последует отзыв лицензии "
        f"{tail}."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "connector",
    ("и (или)", "и (либо)", "и (или же)"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_keeps_parenthesized_alternative_topology(
    connector: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, за нарушение "
        "любого из положений статьи последует отзыв лицензии "
        f"{connector} предупреждение."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_keeps_long_nominal_subject_list(
    validate: RelationValidator,
) -> None:
    source = (
        "Название, атрибутика, печать, символика и реклама не должны нарушать "
        "законодательство штата, за нарушение любого из положений статьи "
        "последует отзыв лицензии."
    )

    validate(
        source,
        "за нарушение законодательства штата последует отзыв лицензии",
    )


@pytest.mark.parametrize(
    "unsafe",
    (
        "отмена проекта разрешения за совершение преступления с пропуском",
        "отмена разрешения Альфа за совершение преступления с пропуском",
        "отмена разрешения №1 за совершение преступления с пропуском",
        "отмена разрешения на оружие за совершение преступления с пропуском",
        (
            "отмена разрешения владельца Бета за совершение преступления "
            "с пропуском"
        ),
        "отмена чужого разрешения за совершение преступления с пропуском",
        "отмена любого разрешения за совершение преступления с пропуском",
        "отмена всех разрешений за совершение преступления с пропуском",
        (
            "отмена разрешения или лицензии за совершение преступления "
            "с пропуском"
        ),
        (
            "отмена либо разрешения либо лицензии за совершение преступления "
            "с пропуском"
        ),
        (
            "отмена разрешения только за совершение преступления "
            "с пропуском"
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_alias_requires_bare_generic_patient(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом в качестве дополнительной "
        "меры наказания, когда преступление совершено с его использованием."
    )

    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Может ли пропуск быть аннулирован судом в качестве дополнительной "
            "меры наказания, когда преступление совершено с его использованием?"
        ),
        (
            "По слухам, пропуск может быть аннулирован судом, когда преступление "
            "совершено с его использованием."
        ),
        (
            "Возможно, пропуск может быть аннулирован судом, когда преступление "
            "совершено с его использованием."
        ),
    ),
    ids=("question", "hearsay", "possible"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_alias_preserves_source_assertiveness(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "отмена разрешения за совершение преступления с пропуском",
        )


@pytest.mark.parametrize(
    "modifier",
    (
        "не всегда",
        "далеко не всегда",
        "не вполне",
        "не во всех случаях",
        "не при любых обстоятельствах",
        "вряд ли",
        "едва ли",
        "возможно",
        "вероятно",
        "якобы",
        "предположительно",
        "по слухам",
        "как утверждают свидетели",
        "обычно",
        "иногда",
        "как правило",
        "в отдельных случаях",
        "при определенных условиях",
        "могли бы",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_heading_rejects_unconsumed_assertion_modifier(
    modifier: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Причинами для аннулирования сертификата "
        f"{modifier} являются:\nНарушение Уголовного Кодекса."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            (
                "Нарушение Уголовного Кодекса — основание для "
                "аннулирования сертификата"
            ),
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_additive_not_only_heading_still_entails_positive_reason(
    validate: RelationValidator,
) -> None:
    source = (
        "Причинами для аннулирования сертификата не только являются:\n"
        "Нарушение Уголовного Кодекса."
    )
    validate(
        source,
        (
            "Нарушение Уголовного Кодекса — основание для "
            "аннулирования сертификата"
        ),
    )


@pytest.mark.parametrize(
    "unsafe",
    (
        "отмена разрешения № 1 за совершение преступления с пропуском",
        "отмена разрешения «Альфа» за совершение преступления с пропуском",
        "отмена двух разрешений за совершение преступления с пропуском",
        "отмена нескольких разрешений за совершение преступления с пропуском",
        (
            "отмена разрешения и (или) лицензии за совершение преступления "
            "с пропуском"
        ),
        (
            "отмена разрешения лишь за совершение преступления "
            "с пропуском"
        ),
        (
            "отмена разрешения исключительно за совершение преступления "
            "с пропуском"
        ),
        (
            "отмена разрешения Альфа судом за совершение преступления "
            "с пропуском"
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_alias_keeps_adjacent_patient_surface(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом, когда преступление совершено "
        "с его использованием."
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Территориальный пропуск может быть аннулирован судом, когда "
            "преступление совершено с его использованием."
        ),
        (
            "Пропуск № 1 может быть аннулирован судом, когда преступление "
            "совершено с его использованием."
        ),
    ),
    ids=("scoped-pass", "numbered-pass"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_alias_cannot_drop_source_patient_identity(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "отмена разрешения за совершение преступления с пропуском",
        )


@pytest.mark.parametrize(
    "output",
    (
        "отмена разрешения за совершение преступления с пропуском",
        (
            "отмена разрешения за совершение преступления "
            "с использованием пропуска"
        ),
        "отмена разрешения судом за совершение преступления с пропуском",
    ),
)
def test_v3_access_pass_alias_preserves_complete_positive_surfaces(
    output: str,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом, когда преступление совершено "
        "с его использованием."
    )
    _validate_title(source, output)
    validate_synonym_source_evidence(
        source,
        [
            output,
            "отзыв пропуска судом",
        ],
    )
    _validate_full_set(source, output)


def test_v3_local_hearsay_does_not_poison_separate_assertive_candidate(
) -> None:
    source = (
        "По слухам, пропуск может быть аннулирован судом. "
        "Пропуск может быть аннулирован судом, когда преступление совершено "
        "с его использованием."
    )
    output = "отмена разрешения за совершение преступления с пропуском"
    _validate_title(
        source,
        output,
    )
    validate_synonym_source_evidence(
        source,
        [output, "отзыв пропуска судом"],
    )
    _validate_full_set(source, output)


@pytest.mark.parametrize(
    ("effect", "unsafe_effect"),
    (
        ("отказа от аннулирования сертификата", "аннулирования сертификата"),
        ("предотвращения аннулирования сертификата", "аннулирования сертификата"),
        ("условного аннулирования сертификата", "аннулирования сертификата"),
        ("аннулирования сертификата", "аннулирования сертификата частного адвоката"),
        ("аннулирования сертификата частного адвоката", "аннулирования сертификата"),
    ),
    ids=(
        "refusal-is-not-revocation",
        "prevention-is-not-revocation",
        "conditional-is-not-unconditional",
        "generic-cannot-invent-scope",
        "specific-cannot-be-generalized",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_heading_preserves_complete_effect_contract(
    effect: str,
    unsafe_effect: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Причинами для {effect} являются:\n"
        "Нарушение Уголовного Кодекса."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            (
                "Нарушение Уголовного Кодекса — основание для "
                f"{unsafe_effect}"
            ),
        )


@pytest.mark.parametrize(
    "unsafe",
    (
        "отмена (чужого) разрешения за совершение преступления с пропуском",
        "отмена «чужого» разрешения за совершение преступления с пропуском",
        "отмена (№1) разрешения за совершение преступления с пропуском",
        "отмена разрешения (чужого) за совершение преступления с пропуском",
        "отмена разрешения «Альфа» за совершение преступления с пропуском",
    ),
    ids=(
        "parenthesized-before",
        "quoted-before",
        "numbered-before",
        "parenthesized-after",
        "quoted-after",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_alias_preserves_delimited_patient_identity(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом, когда преступление совершено "
        "с его использованием."
    )

    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Название не должно заставлять граждан рекламировать товары, а "
            "также нарушать законодательство штата, за нарушение любого из "
            "положений статьи последует отзыв лицензии."
        ),
        (
            "Название не должно заставлять граждан следующее — рекламировать "
            "товары, а также нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует отзыв лицензии."
        ),
        (
            "Название при наличии решения суда не должно нарушать "
            "законодательство штата, за нарушение любого из положений статьи "
            "последует отзыв лицензии."
        ),
        (
            "Название (при наличии решения суда) не должно нарушать "
            "законодательство штата, за нарушение любого из положений статьи "
            "последует отзыв лицензии."
        ),
        (
            "Только зарегистрированное в штате название не должно нарушать "
            "законодательство штата, за нарушение любого из положений статьи "
            "последует отзыв лицензии."
        ),
        (
            "Возможно название не должно нарушать законодательство штата, за "
            "нарушение любого из положений статьи последует отзыв лицензии."
        ),
        (
            "(По слухам) название не должно нарушать законодательство штата, "
            "за нарушение любого из положений статьи последует отзыв лицензии."
        ),
        (
            "Название не должно нарушать законодательство штата, за нарушение "
            "любого из положений статьи последует отзыв лицензии и в отдельных "
            "случаях предупреждение вместо него."
        ),
    ),
    ids=(
        "controller-complement",
        "controller-dash-complement",
        "inline-prerequisite",
        "parenthesized-prerequisite",
        "focus-restricted-subject",
        "punctuation-free-epistemic",
        "parenthesized-hearsay",
        "conditional-replacement-consequence",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_keeps_complete_clause_semantics(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "за нарушение законодательства штата последует отзыв "
                "лицензии"
            ),
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_preserves_complex_unrestricted_subject(
    validate: RelationValidator,
) -> None:
    source = (
        "Название организации по защите прав граждан и её символика не должны "
        "нарушать законодательство штата, за нарушение любого из положений "
        "статьи последует отзыв лицензии."
    )
    validate(
        source,
        "за нарушение законодательства штата последует отзыв лицензии",
    )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Пропуск, возможно, может быть аннулирован судом, когда "
            "преступление совершено с его использованием."
        ),
        (
            "Суд якобы может аннулировать пропуск, когда преступление "
            "совершено с его использованием."
        ),
        (
            "Не исключено, что пропуск может быть аннулирован судом, когда "
            "преступление совершено с его использованием."
        ),
        (
            "Сообщается, что пропуск может быть аннулирован судом, когда "
            "преступление совершено с его использованием."
        ),
    ),
    ids=("mid-possible", "actor-hearsay", "not-excluded", "reported"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_alias_rejects_all_local_nonassertive_surfaces(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "отмена разрешения за совершение преступления с "
                "пропуском"
            ),
        )


@pytest.mark.parametrize(
    "source",
    (
        (
            "(Территориальный) пропуск может быть аннулирован судом, когда "
            "преступление совершено с его использованием."
        ),
        (
            "(№1) пропуск может быть аннулирован судом, когда преступление "
            "совершено с его использованием."
        ),
        (
            "«Альфа» пропуск может быть аннулирован судом, когда преступление "
            "совершено с его использованием."
        ),
    ),
    ids=("parenthesized-scope", "parenthesized-number", "quoted-name"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_alias_cannot_drop_delimited_source_identity(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "отмена разрешения за совершение преступления с "
                "пропуском"
            ),
        )


@pytest.mark.parametrize(
    "subject",
    (
        "Зарегистрированное в штате название",
        "Название с разрешения суда",
        "Название по решению суда",
        "Название согласно решению суда",
    ),
    ids=(
        "restrictive-participle",
        "permission-role",
        "decision-role",
        "decision-basis",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_cannot_drop_restricted_subject_scope(
    subject: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"{subject} не должно нарушать законодательство штата, за нарушение "
        "любого из положений статьи последует отзыв лицензии."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "tail",
    (
        "и в отдельных случаях",
        "и по решению суда",
        "и с согласия губернатора",
        "и при наличии решения суда",
    ),
    ids=("case-scope", "decision-role", "permission-role", "condition"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_cannot_drop_oblique_consequence_tail(
    tail: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, за нарушение "
        "любого из положений статьи последует отзыв лицензии "
        f"{tail}."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    ("source_effect", "unsafe_effect"),
    (
        ("приостановления сертификата", "аннулирования сертификата"),
        ("заморозки сертификата", "отзыва сертификата"),
        ("изъятия сертификата", "аннулирования сертификата"),
        (
            "аннулирования сертификата «Частный адвокат»",
            "аннулирования сертификата частного адвоката",
        ),
        (
            "аннулирования сертификата (Частного адвоката)",
            "аннулирования сертификата частного адвоката",
        ),
        (
            "аннулирования сертификата частной организации",
            "аннулирования сертификата частной организацией",
        ),
        (
            "аннулирования проекта сертификата",
            "аннулирования сертификата проекта",
        ),
    ),
    ids=(
        "temporary-is-not-invalidation",
        "freeze-is-not-withdrawal",
        "seizure-is-not-invalidation",
        "quoted-name-is-not-owner-description",
        "parenthetical-is-not-owner-description",
        "owner-is-not-instrumental-actor",
        "pre-object-scope-is-not-post-object-owner",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_heading_keeps_effect_family_delimiters_and_roles(
    source_effect: str,
    unsafe_effect: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Причинами для {source_effect} являются:\n"
        "Нарушение Уголовного Кодекса."
    )
    output = (
        "Нарушение Уголовного Кодекса — основание для "
        f"{unsafe_effect}"
    )
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    ("source_effect", "output_effect"),
    (
        ("аннулирования сертификата", "отзыва сертификата"),
        (
            "аннулирования сертификата «Частный адвокат»",
            "отзыва сертификата «Частный адвокат»",
        ),
        (
            "аннулирования сертификата частного адвоката",
            "отзыва сертификата частного адвоката",
        ),
    ),
    ids=("invalidation-alias", "quoted-name", "owner-description"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_heading_preserves_safe_invalidation_aliases(
    source_effect: str,
    output_effect: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Причинами для {source_effect} являются:\n"
        "Нарушение Уголовного Кодекса."
    )
    validate(
        source,
        (
            "Нарушение Уголовного Кодекса — основание для "
            f"{output_effect}"
        ),
    )


_V3_PASS_USE_GROUND = "за совершение преступления с использованием пропуска"


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Пропуск может быть аннулирован судом.",
            "отмена территориального пропуска судом",
        ),
        (
            "Территориальный пропуск может быть аннулирован судом.",
            "отмена пропуска судом",
        ),
        (
            "Пропуск может быть аннулирован судом.",
            "отмена пропуска №1 судом",
        ),
        (
            "Пропуск №1 может быть аннулирован судом.",
            "отмена пропуска судом",
        ),
        (
            "Пропуск может быть аннулирован судом.",
            "отмена пропуска «Альфа» судом",
        ),
        (
            "Пропуск «Альфа» может быть аннулирован судом.",
            "отмена пропуска судом",
        ),
        (
            "Пропуск частной организации может быть аннулирован судом.",
            "отмена пропуска судом",
        ),
        (
            "Пропуск может быть аннулирован частной организацией.",
            "отмена пропуска частной организации",
        ),
    ),
    ids=(
        "invent-scope",
        "drop-scope",
        "invent-number",
        "drop-number",
        "invent-name",
        "drop-name",
        "drop-owner",
        "actor-is-not-owner",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_same_document_nominal_keeps_complete_patient_surface(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source_patient", "output_patient"),
    (
        ("Территориальный пропуск", "территориального пропуска"),
        ("Пропуск №1", "пропуска №1"),
        ("Пропуск «Альфа»", "пропуска «Альфа»"),
        (
            "Пропуск частной организации",
            "пропуска частной организации",
        ),
    ),
    ids=("territorial", "numbered", "quoted", "owned"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_same_document_nominal_keeps_exact_patient_positive(
    source_patient: str,
    output_patient: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"{source_patient} может быть аннулирован судом, когда преступление "
        "совершено с его использованием."
    )
    validate(
        source,
        f"отмена {output_patient} судом {_V3_PASS_USE_GROUND}",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_generic_plural_source_supports_bare_topical_patient(
    validate: RelationValidator,
) -> None:
    source = (
        "Решения по аннулированию пропусков, вынесенные Верховным "
        "Судом, обжалуются в нем же."
    )
    validate(source, "апелляция на судебное аннулирование допуска")


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Решения по аннулированию территориальных пропусков, "
            "вынесенные Верховным Судом, обжалуются в нем же.",
            "апелляция на судебное аннулирование допуска",
        ),
        (
            "Решение по аннулированию пропуска, вынесенное Верховным "
            "Судом, обжалуется в нем же.",
            "апелляция на судебное аннулирование допусков",
        ),
        (
            "Решения по аннулированию пропусков, вынесенные Верховным "
            "Судом, обжалуются в нем же.",
            "апелляция на судебное аннулирование всех допусков",
        ),
    ),
    ids=("drop-plural-scope", "invent-plural", "invent-all"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_generic_plural_abstraction_keeps_patient_restrictions(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Причинами для аннулирования сертификата в реестре являются:\n"
            "Нарушение Уголовного Кодекса.",
            "Нарушение Уголовного Кодекса — основание для аннулирования "
            "сертификата в реестре",
        ),
        (
            "Причинами для аннулирования сертификата адвоката Иванова "
            "являются:\nНарушение Уголовного Кодекса.",
            "Нарушение Уголовного Кодекса — основание для аннулирования "
            "сертификата адвоката Иванова",
        ),
        (
            "Причинами для аннулирования сертификата могут являться:\n"
            "Нарушение Уголовного Кодекса.",
            "Нарушение Уголовного Кодекса может являться основанием для "
            "аннулирования сертификата",
        ),
    ),
    ids=("container", "patient-owner", "may-modality"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_heading_preserves_complete_effect_positive(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Причинами для аннулирования сертификата в реестре являются:\n"
            "Нарушение Уголовного Кодекса.",
            "Нарушение Уголовного Кодекса — основание для аннулирования "
            "сертификата",
        ),
        (
            "Причинами для аннулирования сертификата адвоката Иванова "
            "являются:\nНарушение Уголовного Кодекса.",
            "Нарушение Уголовного Кодекса — основание для аннулирования "
            "сертификата адвоката Петрова",
        ),
        (
            "Причинами для аннулирования сертификата могут являться:\n"
            "Нарушение Уголовного Кодекса.",
            "Нарушение Уголовного Кодекса — основание для аннулирования "
            "сертификата",
        ),
    ),
    ids=("drop-container", "change-owner", "may-to-definite"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_heading_complete_effect_cannot_be_weakened(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Суд может предположительно аннулировать пропуск, когда "
            "преступление совершено с его использованием."
        ),
        (
            "Суд может якобы аннулировать пропуск, когда преступление "
            "совершено с его использованием."
        ),
        (
            "Суд, по слухам, может аннулировать пропуск, когда преступление "
            "совершено с его использованием."
        ),
        (
            "По словам свидетеля суд может аннулировать пропуск, когда "
            "преступление совершено с его использованием."
        ),
        (
            "Эксперты полагают, что суд может аннулировать пропуск, когда "
            "преступление совершено с его использованием."
        ),
        (
            "Говорят, суд может аннулировать пропуск, когда преступление "
            "совершено с его использованием."
        ),
        (
            "Верно ли, что суд может аннулировать пропуск, когда преступление "
            "совершено с его использованием?"
        ),
    ),
    ids=(
        "after-modal-probably",
        "after-modal-allegedly",
        "parenthetical-hearsay",
        "witness-attribution",
        "reported-belief",
        "impersonal-report",
        "embedded-question",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_local_nonassertiveness_cannot_become_a_definite_rule(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "отмена разрешения за совершение преступления с пропуском",
        )


@pytest.mark.parametrize(
    "subject",
    (
        "Название зарегистрированной в штате организации",
        "Название организации, имеющей регистрацию в штате",
        "Название одобренное судом",
        "Название организации вне штата",
        "Название организации из другого штата",
        "Название под условием регистрации",
        "Название на период регистрации",
        "Название по судебному решению",
        "Название на основании судебного решения",
        "Разрешённое судом название",
    ),
    ids=(
        "embedded-participle",
        "relative-participle",
        "postposed-participle",
        "outside-scope",
        "origin-scope",
        "condition-scope",
        "duration-scope",
        "adjectival-decision",
        "adjectival-decision-basis",
        "yo-participle",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_keeps_nested_subject_restrictions(
    subject: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"{subject} не должно нарушать законодательство штата, за нарушение "
        "любого из положений статьи последует отзыв лицензии."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "tail",
    (
        ", а также за отдельные случаи отзыв не последует",
        ", а также за нарушение отдельных положений — предупреждение вместо него",
        ", а также за отдельные случаи только после решения суда",
        " и предупреждение за исключением отдельных случаев",
        " и предупреждение в противном случае",
        " и его применение только после решения суда",
        " и (в качестве альтернативы предупреждение)",
        " и предупреждение как замена",
        " и предупреждение взамен него",
    ),
    ids=(
        "additive-negated-effect",
        "additive-replacement",
        "additive-condition",
        "exception",
        "otherwise",
        "anaphoric-condition",
        "parenthetical-alternative",
        "replacement-noun",
        "replacement-adverb",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_cannot_erase_consequence_semantics(
    tail: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, за нарушение "
        "любого из положений статьи последует отзыв лицензии"
        f"{tail}."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )


@pytest.mark.parametrize(
    "tail",
    (
        " и предупреждение",
        ", а также за мошенничество",
        ", но и за мошенничество",
    ),
    ids=("additional-consequence", "additional-ground", "paired-ground"),
)
@pytest.mark.parametrize(
    "validate",
    (_validate_title, _validate_full_set),
    ids=("title", "full-set"),
)
def test_v3_article_catchall_preserves_safe_additive_consequences(
    tail: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Название не должно нарушать законодательство штата, за нарушение "
        "любого из положений статьи последует отзыв лицензии"
        f"{tail}."
    )
    validate(
        source,
        "за нарушение законодательства штата последует отзыв лицензии",
    )


@pytest.mark.parametrize(
    "source",
    (
        (
            "Название не должно заставлять граждан, рекламировать товары, "
            "нарушать законодательство штата, за нарушение любого из "
            "положений статьи последует отзыв лицензии."
        ),
        (
            "Название не должно требовать от граждан рекламы товаров, а также "
            "нарушать законодательство штата, за нарушение любого из "
            "положений статьи последует отзыв лицензии."
        ),
    ),
    ids=("comma-complement", "recipient-complement"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_article_catchall_does_not_promote_nested_controller_items(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            source,
            "за нарушение законодательства штата последует отзыв лицензии",
        )
