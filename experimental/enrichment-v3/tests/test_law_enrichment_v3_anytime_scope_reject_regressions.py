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

EXACT_ANYTIME_OUTPUT = "можно отозвать пропуск №1 без ожидания срока"


@pytest.mark.parametrize(
    "approximation",
    ("почти", "практически"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_approximate_anytime_does_not_prove_exact_universal_scope(
    approximation: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 может быть отозван "
        f"{approximation} в любое время."
    )
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "source",
    (
        "Пропуск №1 может быть отозван не почти в любое время.",
        "Пропуск №1 может быть отозван не практически в любое время.",
    ),
    ids=("not-almost", "not-practically"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_negated_approximate_anytime_is_not_positive_evidence(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "source",
    (
        "Пропуск №1 может быть отозван без ограничений в любое время.",
        (
            "Пропуск №1 может быть отозван без предварительного "
            "уведомления в любое время."
        ),
        "Пропуск №1 может быть отозван без ожидания срока.",
    ),
    ids=("without-restrictions", "without-notice", "without-waiting"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_positive_without_adjunct_does_not_negate_anytime_scope(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "source",
    (
        "Пропуск №1 не продлевается и отзывается в любое время.",
        "Пропуск №1 не продлевается, но отзывается в любое время.",
        (
            "Пропуск №1 может быть отозван не только после решения суда, "
            "но и в любое время."
        ),
        (
            "Пропуск №1 не продлевается. Пропуск №1 может быть отозван "
            "в любое время."
        ),
        (
            "Пропуск №1 не продлевается; Пропуск №1 может быть отозван "
            "в любое время."
        ),
    ),
    ids=(
        "coordinated-and",
        "contrast-but",
        "not-only-but-also",
        "separate-sentence",
        "separate-semicolon",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_unrelated_or_contrasted_negation_does_not_poison_anytime(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_quoted_anytime_identity_is_opaque_but_real_scope_is_kept(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск «В любое время» может быть отозван судом в любое время."
    )
    validate(
        source,
        (
            "суд может отозвать пропуск «В любое время» "
            "без ожидания срока"
        ),
    )
    with pytest.raises(ValueError):
        validate(
            "Пропуск «В любое время» может быть отозван судом.",
            (
                "суд может отозвать пропуск «В любое время» "
                "без ожидания срока"
            ),
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_exact_positive_anytime_baseline_remains_available(
    validate: RelationValidator,
) -> None:
    validate(
        "Пропуск №1 может быть отозван в любое время.",
        EXACT_ANYTIME_OUTPUT,
    )


@pytest.mark.parametrize(
    "lookalike",
    ("любимое", "любопытное"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_anytime_requires_exact_legal_morphology(
    lookalike: str,
    validate: RelationValidator,
) -> None:
    source = f"Пропуск №1 может быть отозван в {lookalike} время."
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "source",
    (
        "Пропуск №1 может быть отозван в любое время рабочего дня.",
        "Пропуск №1 может быть отозван в любое время приёма граждан.",
        "Пропуск №1 может быть отозван в любое время действия разрешения.",
        "Пропуск №1 может быть отозван в любое время в рабочие часы.",
        (
            "Пропуск №1 может быть отозван в любое время, "
            "но только по пятницам."
        ),
        (
            "Пропуск №1 может быть отозван в любое время "
            "только после решения суда."
        ),
        (
            "Пропуск №1 может быть отозван в любое время "
            "до истечения лицензии."
        ),
        (
            "Пропуск №1 может быть отозван в любое время, "
            "кроме выходных."
        ),
        "Пропуск №1 может быть отозван без ожидания срока давности.",
    ),
    ids=(
        "working-day",
        "reception-hours",
        "authorization-lifetime",
        "working-hours",
        "fridays-only",
        "only-after-decision",
        "before-license-expiry",
        "except-weekends",
        "limitation-period",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_bounded_time_window_does_not_prove_unrestricted_anytime(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_any_time_of_day_remains_an_unrestricted_positive_scope(
    validate: RelationValidator,
) -> None:
    validate(
        "Пропуск №1 может быть отозван в любое время суток.",
        EXACT_ANYTIME_OUTPUT,
    )


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            (
                "Пропуск №1 может быть отозван, а доступ по нему — "
                "в любое время."
            ),
            EXACT_ANYTIME_OUTPUT,
        ),
        (
            (
                "Пропуск №1 может быть отозван судом, а доступ по нему — "
                "в любое время."
            ),
            "суд может отозвать пропуск №1 без ожидания срока",
        ),
        (
            (
                "Приём граждан — в любое время, а пропуск №1 может быть "
                "отозван."
            ),
            EXACT_ANYTIME_OUTPUT,
        ),
    ),
    ids=(
        "scope-after-revocation",
        "scope-after-qualified-revocation",
        "scope-before-revocation",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_anytime_scope_does_not_cross_an_elliptical_clause_boundary(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    "source",
    (
        "Суд в любое время может отозвать пропуск №1.",
        "Суд в любое время суток может отозвать пропуск №1.",
        "В любое время суд может отозвать пропуск №1.",
        "Суд может в любое время отозвать пропуск №1.",
        "Пропуск №1 в любое время может быть отозван судом.",
    ),
    ids=(
        "actor-before-scope",
        "actor-before-time-of-day-scope",
        "scope-before-actor",
        "scope-after-modal",
        "passive-before-modal",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_anytime_scope_preserves_the_full_actor(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(
        source,
        "суд может отозвать пропуск №1 без ожидания срока",
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "полиция может отозвать пропуск №1 без ожидания срока",
        )


@pytest.mark.parametrize(
    "connector",
    ("а", "однако", "зато", "а также"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_shared_passive_patient_survives_bounded_contrast_connectors(
    connector: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Пропуск №1 не продлевается, {connector} отзывается в любое время."
    )
    validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "connector",
    ("или", "либо не продлевается, либо"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_shared_passive_patient_is_not_recovered_across_alternatives(
    connector: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Пропуск №1 не продлевается, {connector} отзывается в любое время."
    )
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_shared_passive_patient_recovery_preserves_polarity(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Пропуск №1 не продлевается, но не отзывается в любое время.",
            EXACT_ANYTIME_OUTPUT,
        )


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            (
                "Пропуск №1 не продлевается, а суд отзывает в любое "
                "время."
            ),
            "суд может отозвать пропуск №1 без ожидания срока",
        ),
        (
            (
                "Пропуск №1 не продлевается, а пропуск №2 отзывается "
                "в любое время."
            ),
            EXACT_ANYTIME_OUTPUT,
        ),
        (
            (
                "Пропуск №1 не продлевается директора отзывается "
                "в любое время."
            ),
            EXACT_ANYTIME_OUTPUT,
        ),
    ),
    ids=("fresh-actor", "fresh-patient", "connector-word-tail"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_shared_patient_recovery_requires_a_standalone_connector(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_correlative_anytime_branch_rejects_unread_predicate_residue(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 может быть отозван не только после решения суда, "
        "но и в любое время доступен владельцу."
    )
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Пропуск №1 может быть отозван в любое время, "
            "но лишь по пятницам."
        ),
        (
            "Пропуск №1 может быть отозван в любое время "
            "исключительно по пятницам."
        ),
        (
            "Пропуск №1 может быть отозван в любое время "
            "в течение рабочего дня."
        ),
        (
            "Пропуск №1 может быть отозван в любое время "
            "в период действия разрешения."
        ),
        "Пропуск №1 может быть отозван в любое время с 9 до 18 часов.",
        "Пропуск №1 может быть отозван в любое время в будние дни.",
        "Пропуск №1 может быть отозван в любое время не ранее пятницы.",
        "Пропуск №1 может быть отозван в любое время начиная с пятницы.",
        (
            "Пропуск №1 может быть отозван в любое время "
            "в пределах рабочего дня."
        ),
        (
            "Пропуск №1 может быть отозван в любое время, "
            "пока действует разрешение."
        ),
        (
            "Пропуск №1 может быть отозван в любое время "
            "между 9 и 18 часами."
        ),
        "Пропуск №1 может быть отозван без ожидания срока проверки.",
        "Пропуск №1 может быть отозван без ожидания срока оплаты.",
        (
            "Пропуск №1 может быть отозван без ожидания срока, "
            "но только по пятницам."
        ),
        (
            "Пропуск №1 может быть отозван без ожидания срока, "
            "в течение рабочего дня."
        ),
        (
            "Пропуск №1 может быть отозван без ожидания срока, "
            "начиная с пятницы."
        ),
        (
            "Пропуск №1 может быть отозван в любое время без "
            "предварительного уведомления, но только по пятницам."
        ),
        (
            "Пропуск №1 может быть отозван в любое время, в том числе "
            "ночью, но только по пятницам."
        ),
    ),
    ids=(
        "but-only-fridays",
        "exclusively-fridays",
        "during-working-day",
        "during-authorization-lifetime",
        "numeric-hours",
        "weekdays",
        "not-before",
        "starting-from",
        "within-working-day",
        "while-authorization-valid",
        "between-hours",
        "review-period",
        "payment-period",
        "waiting-period-plus-only-fridays",
        "waiting-period-plus-working-day",
        "waiting-period-plus-start-date",
        "neutral-notice-before-limiter",
        "including-night-before-limiter",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_structurally_bounded_residual_is_not_unrestricted_anytime(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Пропуск №1 может быть отозван в любое время "
            "без предварительного уведомления."
        ),
        "Пропуск №1 может быть отозван в любое время дня и ночи.",
        (
            "Пропуск №1 может быть отозван в любое время, "
            "в том числе ночью."
        ),
    ),
    ids=("post-scope-no-notice", "day-and-night", "including-night"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_unrestricted_positive_anytime_residuals_remain_supported(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Пропуск №1 может быть отозван в любое время судом.",
            "суд может отозвать пропуск №1 без ожидания срока",
        ),
        (
            (
                "Пропуск №1 может быть отозван в любое время "
                "при нарушении режима."
            ),
            (
                "при нарушении режима, можно отозвать пропуск №1 "
                "без ожидания срока"
            ),
        ),
        (
            (
                "Пропуск №1 может быть отозван в любое время "
                "с разрешения суда Альфа."
            ),
            (
                "с разрешения суда Альфа можно отозвать пропуск №1 "
                "без ожидания срока"
            ),
        ),
        (
            (
                "Пропуск №1 может быть отозван в любое время, а лицензия "
                "может быть отозвана только после решения суда."
            ),
            EXACT_ANYTIME_OUTPUT,
        ),
        (
            (
                "Без предварительного уведомления пропуск №1 может быть "
                "отозван в любое время."
            ),
            EXACT_ANYTIME_OUTPUT,
        ),
    ),
    ids=(
        "postposed-passive-actor",
        "preserved-condition",
        "preserved-permission",
        "independent-relation-after-connector",
        "leading-neutral-absence",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_anytime_accepts_only_completely_owned_branch_roles(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Пропуск №1 может быть отозван в любое время "
            "при нарушении режима."
        ),
        (
            "Пропуск №1 может быть отозван в любое время "
            "с разрешения суда Альфа."
        ),
    ),
    ids=("dropped-condition", "dropped-permission"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_anytime_does_not_erase_an_owned_branch_role(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Пропуск №1 может быть отозван в любое время не без "
            "предварительного уведомления."
        ),
        (
            "Пропуск №1 может быть отозван в любое время только без "
            "предварительного уведомления."
        ),
        "Пропуск №1 может быть отозван в любое время без согласия суда.",
        "Пропуск №1 может быть отозван в любое время по понедельникам.",
        (
            "Пропуск №1 может быть отозван в любое время на протяжении "
            "проверки."
        ),
        "Пропуск №1 может быть отозван в любое время вне выходных.",
        (
            "Пропуск №1 может быть отозван в любое время в установленный "
            "начальником период."
        ),
    ),
    ids=(
        "not-without-notice",
        "only-without-notice",
        "unknown-without-consent",
        "mondays",
        "throughout-review",
        "outside-weekends",
        "authority-defined-period",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_anytime_rejects_unconsumed_open_vocabulary_limits(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_neutral_absence_alone_does_not_invent_anytime_scope(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            (
                "Пропуск №1 может быть отозван без предварительного "
                "уведомления."
            ),
            EXACT_ANYTIME_OUTPUT,
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_later_independent_predicate_does_not_hide_an_earlier_limiter(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 может быть отозван в любое время, но только по "
        "пятницам, а лицензия может быть отозвана судом."
    )
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_and_starts_a_new_anytime_branch_only_with_a_fresh_owner(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 может быть отозван в любое время, и директор может "
        "отозвать пропуск №2."
    )
    validate(source, EXACT_ANYTIME_OUTPUT)
    with pytest.raises(ValueError):
        validate(
            source,
            "директор может отозвать пропуск №2 без ожидания срока",
        )


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд может отозвать в любое время пропуск №1.",
            "суд может отозвать пропуск №1 без ожидания срока",
        ),
        (
            (
                "Суд может отозвать в любое время пропуск организации "
                "«Альфа»."
            ),
            (
                "суд может отозвать пропуск организации «Альфа» "
                "без ожидания срока"
            ),
        ),
    ),
    ids=("numbered-patient", "owned-named-patient"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_post_scope_patient_must_be_completely_consumed(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_post_scope_patient_cannot_hide_a_trailing_limiter(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд может отозвать в любое время пропуск №2 только по пятницам."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "суд может отозвать пропуск №2 без ожидания срока",
        )


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            (
                "Пропуск №1 в любое время при нарушении режима может быть "
                "отозван судом."
            ),
            (
                "при нарушении режима, суд может отозвать пропуск №1 "
                "без ожидания срока"
            ),
        ),
        (
            (
                "Пропуск №1 в любое время с разрешения суда Альфа может "
                "быть отозван."
            ),
            (
                "с разрешения суда Альфа можно отозвать пропуск №1 "
                "без ожидания срока"
            ),
        ),
    ),
    ids=("condition-before-action", "permission-before-action"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_pre_action_anytime_bridge_consumes_proved_roles(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Пропуск №1 в любое время только по пятницам может быть "
            "отозван."
        ),
        (
            "Лицензия №2 в любое время может быть отозвана судом, а "
            "пропуск №1 может быть отозван."
        ),
    ),
    ids=("limiter-before-modal", "scope-owned-by-preceding-patient"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_pre_action_scope_cannot_cross_unowned_syntax(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "qualifier",
    ("временный", "федеральный", "региональный"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_post_scope_patient_consumes_only_shared_typed_qualifiers(
    qualifier: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Суд может отозвать в любое время {qualifier} пропуск №1."
    )
    validate(
        source,
        (
            f"суд может отозвать {qualifier} пропуск №1 "
            "без ожидания срока"
        ),
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "суд может отозвать пропуск №1 без ожидания срока",
        )
    wrong_qualifier = (
        "региональный" if qualifier != "региональный" else "федеральный"
    )
    with pytest.raises(ValueError):
        validate(
            source,
            (
                f"суд может отозвать {wrong_qualifier} пропуск №1 "
                "без ожидания срока"
            ),
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_post_scope_patient_rejects_an_untyped_adjective_prefix(
    validate: RelationValidator,
) -> None:
    source = "Суд может отозвать в любое время любимый пропуск №1."
    with pytest.raises(ValueError):
        validate(
            source,
            "суд может отозвать любимый пропуск №1 без ожидания срока",
        )


@pytest.mark.parametrize(
    ("source_actor", "output_actor"),
    (
        ("судом", "суд может"),
        ("судом округа Блейн", "суд округа Блейн может"),
        ("судом и прокурором", "суд и прокурор могут"),
    ),
    ids=("single", "qualified", "coordinated"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_post_scope_actor_is_consumed_as_one_complete_role(
    source_actor: str,
    output_actor: str,
    validate: RelationValidator,
) -> None:
    validate(
        (
            "Пропуск №1 может быть отозван в любое время "
            f"{source_actor}."
        ),
        (
            f"{output_actor} отозвать пропуск №1 "
            "без ожидания срока"
        ),
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_post_scope_actor_cannot_absorb_a_later_time_limit(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 может быть отозван в любое время судом в рабочие дни."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "суд может отозвать пропуск №1 без ожидания срока",
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_actor_tail_offsets_remain_correct_in_a_later_soft_clause(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия действует, а пропуск №1 может быть отозван в любое "
        "время судом."
    )
    validate(
        source,
        "суд может отозвать пропуск №1 без ожидания срока",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_later_soft_clause_actor_cannot_absorb_a_time_limit(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия действует, а пропуск №1 может быть отозван в любое "
        "время судом в рабочие дни."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "суд может отозвать пропуск №1 без ожидания срока",
        )


@pytest.mark.parametrize(
    ("source_actor", "output_actor"),
    (
        ("суд", "суд"),
        ("суд округа Блейн", "суд округа Блейн"),
        ("директор LSPD", "директор LSPD"),
    ),
    ids=("proved-head", "qualified-court", "open-management-identity"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_pre_action_bridge_accepts_a_complete_bounded_actor(
    source_actor: str,
    output_actor: str,
    validate: RelationValidator,
) -> None:
    validate(
        f"Пропуск №1 в любое время {source_actor} может отозвать.",
        (
            f"{output_actor} может отозвать пропуск №1 "
            "без ожидания срока"
        ),
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_pre_action_actor_cannot_absorb_a_later_time_limit(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 в любое время сотрудник FIB в будни может отозвать."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "сотрудник FIB может отозвать пропуск №1 "
                "без ожидания срока"
            ),
        )


@pytest.mark.parametrize(
    "unowned_prefix",
    (
        "только по пятницам",
        "в рабочие дни",
        "с 9 до 18",
        "после решения суда",
        "на основании приказа",
        "по слухам",
        "не во всех случаях",
    ),
    ids=(
        "fridays-only",
        "working-days",
        "business-hours",
        "after-decision",
        "under-order",
        "rumor",
        "not-all-cases",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_post_action_anytime_scope_consumes_the_left_branch_tail(
    unowned_prefix: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 может быть отозван "
        f"{unowned_prefix} в любое время."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "Пропуск №1 может быть отозван без ожидания срока",
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_post_action_anytime_scope_cannot_escape_an_or_branch(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 может быть отозван либо после решения суда, "
        "либо в любое время."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "Пропуск №1 может быть отозван без ожидания срока",
        )


@pytest.mark.parametrize(
    "unowned_suffix",
    (
        "только по пятницам",
        "в рабочие дни",
        "с 9 до 18",
        "после решения суда",
        "на основании приказа",
        "по слухам",
        "не во всех случаях",
        "предположительно",
    ),
    ids=(
        "fridays-only",
        "working-days",
        "business-hours",
        "after-decision",
        "under-order",
        "rumor",
        "not-all-cases",
        "supposedly",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_pre_action_anytime_scope_consumes_the_right_branch_tail(
    unowned_suffix: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 в любое время суд может отозвать "
        f"{unowned_suffix}."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "суд может отозвать пропуск №1 без ожидания срока",
        )


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд может отозвать пропуск №1 в любое время.",
            "суд может отозвать пропуск №1 без ожидания срока",
        ),
        (
            "Пропуск №1 может быть отозван судом в любое время.",
            "суд может отозвать пропуск №1 без ожидания срока",
        ),
        (
            "Пропуск №1 суд может отозвать в любое время.",
            "суд может отозвать пропуск №1 без ожидания срока",
        ),
        (
            "Суд в любое время может отозвать пропуск №1.",
            "суд может отозвать пропуск №1 без ожидания срока",
        ),
        (
            "Суд может аннулировать и изъять пропуск №1 в любое время.",
            (
                "суд может аннулировать и изъять пропуск №1 "
                "без ожидания срока"
            ),
        ),
        (
            (
                "Пропуск №1 может быть аннулирован и изъят судом "
                "в любое время."
            ),
            (
                "суд может аннулировать и изъять пропуск №1 "
                "без ожидания срока"
            ),
        ),
    ),
    ids=(
        "active-svo",
        "passive-patient-actor",
        "active-osv",
        "pre-action-svo",
        "active-shared-patient",
        "passive-shared-patient",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_bidirectional_anytime_branch_keeps_safe_role_orders(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    "unowned_suffix",
    (
        "только по пятницам",
        "после решения суда",
        "в отдельных случаях",
        "предположительно",
    ),
    ids=("fridays-only", "after-decision", "selected-cases", "supposedly"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_passive_pre_action_anytime_consumes_the_post_action_tail(
    unowned_suffix: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 в любое время может быть отозван "
        f"{unowned_suffix}."
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "Пропуск №1 может быть отозван без ожидания срока",
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_shared_patient_cannot_hide_a_pre_scope_limiter(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 не продлевается, но отзывается только по пятницам "
        "в любое время."
    )
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


@pytest.mark.parametrize(
    "source",
    (
        "Пропуск №1 отзывается либо изымается в любое время.",
        "Пропуск №1 отзывается и не изымается в любое время.",
        "Пропуск №1 отзывается и изымается в любое время.",
    ),
    ids=("alternative", "negated-sibling", "different-co-action"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_sibling_action_mask_cannot_lend_its_anytime_scope(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, EXACT_ANYTIME_OUTPUT)


_RIGHT_OF_ACTOR_ANYTIME_SOURCE = (
    "Статья 5. Пропуск, выданный руководителем или заместителем "
    "руководителя государственной организации, может быть им отозван "
    "в любое время."
)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_anytime_branch_consumes_a_complete_right_of_actor_prefix(
    validate: RelationValidator,
) -> None:
    validate(
        _RIGHT_OF_ACTOR_ANYTIME_SOURCE,
        "право руководителя отозвать пропуск в любое время",
    )


@pytest.mark.parametrize(
    "output",
    (
        "право полиции отозвать пропуск в любое время",
        "право руководителя отозвать пропуск только по пятницам в любое время",
        "право руководителя отозвать пропуск в любое время только по пятницам",
    ),
    ids=("changed-holder", "pre-scope-limit", "post-scope-limit"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_right_of_actor_prefix_cannot_hide_changed_roles_or_scope(
    output: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(_RIGHT_OF_ACTOR_ANYTIME_SOURCE, output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_right_of_actor_prefix_cannot_invent_anytime(
    validate: RelationValidator,
) -> None:
    source = "Руководитель имеет право отозвать пропуск по пятницам."
    with pytest.raises(ValueError):
        validate(
            source,
            "право руководителя отозвать пропуск в любое время",
        )


@pytest.mark.parametrize(
    "unowned_holder_tail",
    (
        "по слухам",
        "возможно",
        "предположительно",
        "после решения суда",
        "только по пятницам",
        "лишь условно",
        "теоретически",
        "при необходимости",
        "в отдельных случаях",
        "после уведомления",
        "до уведомления",
        "без решения суда",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_right_holder_cannot_absorb_an_external_semantic_role(
    unowned_holder_tail: str,
    validate: RelationValidator,
) -> None:
    output = (
        f"право руководителя {unowned_holder_tail} "
        "отозвать пропуск в любое время"
    )
    with pytest.raises(ValueError):
        validate(_RIGHT_OF_ACTOR_ANYTIME_SOURCE, output)


@pytest.mark.parametrize(
    "unowned_holder_tail",
    (
        "после решения суда",
        "только по пятницам",
        "лишь условно",
        "теоретически",
        "при необходимости",
        "в отдельных случаях",
        "после уведомления",
        "до уведомления",
        "без решения суда",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_source_right_holder_cannot_lend_a_swallowed_anytime_scope(
    unowned_holder_tail: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Право руководителя {unowned_holder_tail} "
        "отозвать пропуск в любое время."
    )
    with pytest.raises(ValueError):
        validate(source, "можно отозвать пропуск без ожидания срока")


@pytest.mark.parametrize(
    "output",
    (
        "право руководителя отозвать пропуск в любое время",
        (
            "право заместителя руководителя государственной организации "
            "отозвать пропуск в любое время"
        ),
    ),
    ids=("bare-manager", "complete-deputy-manager"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_right_holder_parser_preserves_complete_supported_identities(
    output: str,
    validate: RelationValidator,
) -> None:
    validate(_RIGHT_OF_ACTOR_ANYTIME_SOURCE, output)


@pytest.mark.parametrize(
    "output",
    (
        "право руководителя на отзыв пропуска в любое время",
        "право руководителя на аннулирование пропуска в любое время",
        "право руководителя на отмену пропуска в любое время",
        "право руководителя на отзыв пропуска, в любое время",
        "право руководителя на отзыв пропуска — в любое время",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_entitlement_has_structured_anytime_ownership(
    output: str,
    validate: RelationValidator,
) -> None:
    validate(
        "Руководитель вправе отозвать пропуск в любое время.",
        output,
    )


_NOMINAL_ANYTIME_UNSUPPORTED_SOURCE = (
    "Наказание: руководитель имеет право отозвать пропуск только по пятницам."
)


@pytest.mark.parametrize(
    "output",
    (
        "право руководителя на отзыв пропуска в любое время",
        "право руководителя на аннулирование пропуска в любое время",
        "право руководителя на отмену пропуска в любое время",
        "право руководителя на отзыв пропуска, в любое время",
        "право руководителя на отзыв пропуска — в любое время",
        "право руководителя на отзыв пропуска; в любое время",
        "право руководителя на отзыв пропуска. В любое время",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_revocation_cannot_invent_anytime_scope(
    output: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(_NOMINAL_ANYTIME_UNSUPPORTED_SOURCE, output)


@pytest.mark.parametrize(
    "output",
    (
        "право руководителя на отзыв пропуска в любое время",
        "право руководителя на аннулирование пропуска в любое время",
        "право руководителя на отмену пропуска в любое время",
        "право руководителя на отзыв пропуска; в любое время",
        "право руководителя на отзыв пропуска. В любое время",
    ),
)
def test_v3_penalty_nominal_revocation_cannot_invent_anytime_scope(
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            _NOMINAL_ANYTIME_UNSUPPORTED_SOURCE,
            output,
            None,
        )
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            _NOMINAL_ANYTIME_UNSUPPORTED_SOURCE,
            None,
            {"other": output},
        )


@pytest.mark.parametrize(
    "separator",
    ("—", "-", ":"),
    ids=("em-dash", "hyphen", "colon"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_right_holder_rejects_unowned_punctuation_boundaries(
    separator: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            _RIGHT_OF_ACTOR_ANYTIME_SOURCE,
            f"право руководителя {separator} отозвать пропуск в любое время",
        )


@pytest.mark.parametrize(
    "epistemic_holder",
    ("возможного руководителя", "вероятного руководителя"),
    ids=("possible", "probable"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_right_holder_cannot_hide_an_epistemic_modifier(
    epistemic_holder: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            _RIGHT_OF_ACTOR_ANYTIME_SOURCE,
            f"право {epistemic_holder} отозвать пропуск в любое время",
        )


def _synthetic_entitlement_holder(length: int, *, changed: bool = False) -> str:
    prefix = "руководителя организации «"
    suffix = "»"
    payload_length = length - len(prefix) - len(suffix)
    first = "Б" if changed else "А"
    return prefix + first + ("А" * (payload_length - 1)) + suffix


@pytest.mark.parametrize("holder_length", (96, 97, 180))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_right_holder_keeps_supported_identity_at_length_boundaries(
    holder_length: int,
    validate: RelationValidator,
) -> None:
    holder = _synthetic_entitlement_holder(holder_length)
    source = f"Право {holder} отозвать пропуск в любое время."
    validate(
        source,
        f"право {holder} отозвать пропуск в любое время",
    )
    changed_holder = _synthetic_entitlement_holder(
        holder_length,
        changed=True,
    )
    with pytest.raises(ValueError):
        validate(
            source,
            (
                f"право {changed_holder} отозвать пропуск "
                "в любое время"
            ),
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_right_holder_longer_than_180_characters_fails_closed(
    validate: RelationValidator,
) -> None:
    holder = _synthetic_entitlement_holder(181)
    with pytest.raises(ValueError):
        validate(
            f"Право {holder} отозвать пропуск в любое время.",
            f"право {holder} отозвать пропуск в любое время",
        )


_ISSUER_BOUND_ANYTIME_SOURCE = (
    "Пропуск, выданный руководителем организации, "
    "может быть им отозван в любое время."
)
_WRONG_ISSUER_ANYTIME_SOURCE = (
    "Пропуск, выданный судом, может быть отозван "
    "руководителем организации в любое время."
)
_BARE_MANAGER_ANYTIME_SOURCE = (
    "Руководитель вправе отозвать пропуск в любое время."
)


def _typed_issuer_entitlement(*, nominal: bool, anytime: bool) -> str:
    action = "на отзыв пропуска" if nominal else "отозвать пропуск"
    scope = " в любое время" if anytime else ""
    return f"право выдавшего руководителя организации {action}{scope}"


@pytest.mark.parametrize("nominal", (False, True), ids=("prospective", "nominal"))
@pytest.mark.parametrize("anytime", (False, True), ids=("without-anytime", "with-anytime"))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_typed_issuer_entitlement_accepts_proven_same_issuer(
    nominal: bool,
    anytime: bool,
    validate: RelationValidator,
) -> None:
    validate(
        _ISSUER_BOUND_ANYTIME_SOURCE,
        _typed_issuer_entitlement(
            nominal=nominal,
            anytime=anytime,
        ),
    )


@pytest.mark.parametrize(
    "unsupported_source",
    (
        _WRONG_ISSUER_ANYTIME_SOURCE,
        _BARE_MANAGER_ANYTIME_SOURCE,
    ),
    ids=("different-proven-issuer", "no-issuance-history"),
)
@pytest.mark.parametrize("nominal", (False, True), ids=("prospective", "nominal"))
@pytest.mark.parametrize("anytime", (False, True), ids=("without-anytime", "with-anytime"))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_typed_issuer_entitlement_rejects_unsupported_issuer_history(
    unsupported_source: str,
    nominal: bool,
    anytime: bool,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            unsupported_source,
            _typed_issuer_entitlement(
                nominal=nominal,
                anytime=anytime,
            ),
        )


@pytest.mark.parametrize(
    "participle",
    ("подписавшего", "отозвавшего", "сообщившего"),
    ids=("signed", "revoked", "reported"),
)
@pytest.mark.parametrize("anytime", (False, True), ids=("without-anytime", "with-anytime"))
@pytest.mark.parametrize(
    "source",
    (_ISSUER_BOUND_ANYTIME_SOURCE, _BARE_MANAGER_ANYTIME_SOURCE),
    ids=("issued-pass", "bare-manager"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_entitlement_does_not_erase_untyped_participles(
    participle: str,
    anytime: bool,
    source: str,
    validate: RelationValidator,
) -> None:
    scope = " в любое время" if anytime else ""
    output = (
        f"право {participle} руководителя организации "
        f"на отзыв пропуска{scope}"
    )
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_bare_manager_nominal_entitlement_remains_supported(
    validate: RelationValidator,
) -> None:
    validate(
        _BARE_MANAGER_ANYTIME_SOURCE,
        "право руководителя на отзыв пропуска в любое время",
    )
