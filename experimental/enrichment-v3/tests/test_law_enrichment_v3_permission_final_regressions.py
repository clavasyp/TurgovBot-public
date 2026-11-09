from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_relation_truths,
    _normalized_text,
    _relation_permission_spans,
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


def _supported_source_surface(source: str) -> str:
    return source.rstrip().removesuffix(".")


def _assert_supported_in_all_output_paths(
    source: str,
    output: str,
) -> None:
    for validate in RELATION_VALIDATORS:
        validate(source, output)
    validate_synonym_source_evidence(
        source,
        [_supported_source_surface(source), output],
    )


def _assert_rejected_in_all_output_paths(
    source: str,
    unsafe: str,
    *,
    supported: str | None = None,
) -> None:
    for validate in RELATION_VALIDATORS:
        with pytest.raises(ValueError):
            validate(source, unsafe)
    supported_output = supported or _supported_source_surface(source)
    validate_synonym_source_evidence(source, [supported_output])
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [supported_output, unsafe],
        )


def _permission_arguments(source: str) -> tuple[object, ...]:
    return tuple(
        argument
        for truth in _extract_relation_truths(_normalized_text(source))
        for argument in truth.arguments
        if argument.role in {
            "permission_expression",
            "permission_requirement",
        }
    )


@pytest.mark.parametrize(
    "source",
    (
        "С разрешения суда Альфа организация Центр может отозвать пропуск.",
        "Организация Центр может отозвать пропуск с разрешения суда Альфа.",
        "Организация Центр с разрешения суда Альфа может отозвать пропуск.",
        "Организация Центр может с разрешения суда Альфа отозвать пропуск.",
        "Организация Центр может отозвать пропуск (с разрешения суда Альфа).",
    ),
    ids=(
        "leading",
        "trailing",
        "actor-interposed",
        "modal-interposed",
        "parenthesized",
    ),
)
def test_permission_position_controls_keep_requirement(source: str) -> None:
    honest = (
        "Организация Центр с разрешения суда Альфа может отозвать пропуск"
    )
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
        supported=honest,
    )


def test_permission_between_predicate_and_object_keeps_both_roles() -> None:
    source = (
        "Организация Центр может отозвать с разрешения суда Альфа пропуск."
    )
    honest = (
        "Организация Центр с разрешения суда Альфа может отозвать пропуск"
    )
    truths = [
        truth
        for truth in _extract_relation_truths(_normalized_text(source))
        if truth.predicate == "regulated_revocation"
    ]
    assert truths
    assert all(truth.direct_object == "access_pass" for truth in truths)
    assert all(
        any(
            argument.role == "permission_requirement"
            for argument in truth.arguments
        )
        for truth in truths
    )
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
        supported=honest,
    )


@pytest.mark.parametrize("opener,closer", (("(", ")"), ("[", "]")))
def test_post_object_parenthesized_relative_permission_is_bound(
    opener: str,
    closer: str,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск "
        f"{opener}после того, как получит разрешение суда Альфа{closer}."
    )
    honest = (
        "Организация Центр после получения разрешения суда Альфа "
        "может отозвать пропуск"
    )
    assert _relation_permission_spans(source)
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
        supported=honest,
    )


@pytest.mark.parametrize(
    "relative_clause",
    (
        "суд Альфа даст разрешение",
        "суд Альфа подпишет разрешение",
    ),
    ids=("give", "sign"),
)
def test_relative_active_issuer_synonyms_cannot_disappear(
    relative_clause: str,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"{relative_clause}."
    )
    assert _relation_permission_spans(source)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
    )


def test_negative_relative_active_issuer_preserves_polarity() -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        "суд Альфа не даст разрешение."
    )
    positive = (
        "Организация Центр после получения разрешения суда Альфа "
        "может отозвать пропуск"
    )
    arguments = _permission_arguments(source)
    assert arguments
    assert all(argument.polarity == "negative" for argument in arguments)
    _assert_rejected_in_all_output_paths(
        source,
        positive,
    )
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
    )


@pytest.mark.parametrize(
    "source",
    (
        "Организация Центр может отозвать пропуск после выдачи "
        "разрешения судом Альфа.",
        "Организация Центр может отозвать пропуск после предоставления "
        "согласия прокуратурой Бета.",
        "Организация Центр может отозвать пропуск после одобрения "
        "суда Альфа.",
        "Организация Центр может отозвать пропуск на основании "
        "разрешения суда Альфа.",
        "Организация Центр может отозвать пропуск, когда суд Альфа "
        "даст разрешение.",
    ),
    ids=(
        "after-issue",
        "after-provision",
        "after-approval",
        "based-on-permission",
        "when-issued",
    ),
)
def test_adjacent_permission_prerequisite_forms_cannot_disappear(
    source: str,
) -> None:
    assert _relation_permission_spans(source)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
    )


@pytest.mark.parametrize(
    "honest",
    (
        "Организация Центр с разрешения суда Альфа может отозвать пропуск",
        "Организация Центр с согласия прокуратуры Бета может отозвать пропуск",
    ),
    ids=("permission-branch", "consent-branch"),
)
def test_alternative_permission_exposes_each_sufficient_branch(
    honest: str,
) -> None:
    source = (
        "Организация Центр с разрешения суда Альфа или с согласия "
        "прокуратуры Бета может отозвать пропуск."
    )
    _assert_supported_in_all_output_paths(source, honest)


def test_joint_permission_never_exposes_one_insufficient_branch() -> None:
    source = (
        "Организация Центр с разрешения суда Альфа и с согласия "
        "прокуратуры Бета может отозвать пропуск."
    )
    honest = (
        "Организация Центр с согласия прокуратуры Бета и с разрешения "
        "суда Альфа может отозвать пропуск"
    )
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр с разрешения суда Альфа может отозвать пропуск",
        supported=honest,
    )


def test_permission_parentheses_match_equivalent_dnf() -> None:
    source = (
        "Организация Центр (с разрешения суда Альфа или с согласия "
        "прокуратуры Бета) и с одобрения губернатора Гамма может "
        "отозвать пропуск."
    )
    dnf = (
        "Организация Центр с разрешения суда Альфа и с одобрения "
        "губернатора Гамма или с согласия прокуратуры Бета и с одобрения "
        "губернатора Гамма может отозвать пропуск"
    )
    first_branch = (
        "Организация Центр с разрешения суда Альфа и с одобрения "
        "губернатора Гамма может отозвать пропуск"
    )
    second_branch = (
        "Организация Центр с согласия прокуратуры Бета и с одобрения "
        "губернатора Гамма может отозвать пропуск"
    )
    for honest in (dnf, first_branch, second_branch):
        _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр с разрешения суда Альфа может отозвать пропуск",
        supported=dnf,
    )
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр с разрешения суда Альфа или с согласия "
        "прокуратуры Бета и с одобрения губернатора Гамма может "
        "отозвать пропуск",
        supported=dnf,
    )


@pytest.mark.parametrize(
    ("relative_clause", "honest"),
    (
        (
            "она получит разрешение суда Альфа",
            "Организация Центр после получения разрешения суда Альфа "
            "может отозвать пропуск",
        ),
        (
            "суд Альфа выдаст организации разрешение",
            "Организация Центр после получения разрешения суда Альфа "
            "может отозвать пропуск",
        ),
        (
            "разрешение будет выдано судом Альфа",
            "Организация Центр после выдачи разрешения судом Альфа "
            "может отозвать пропуск",
        ),
        (
            "организация приобретёт разрешение суда Альфа",
            "Организация Центр после получения разрешения суда Альфа "
            "может отозвать пропуск",
        ),
    ),
    ids=(
        "anaphoric-holder",
        "active-issuer-dative-recipient",
        "passive-issue",
        "acquisition-yo",
    ),
)
def test_relative_permission_voices_share_one_canonical_requirement(
    relative_clause: str,
    honest: str,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"{relative_clause}."
    )
    assert _relation_permission_spans(source)
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
        supported=honest,
    )


def test_named_relative_holder_cannot_be_replaced_by_current_organization() -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        "заявитель получит разрешение суда Альфа."
    )
    honest = (
        "Организация Центр может отозвать пропуск после того, как "
        "заявитель получит разрешение суда Альфа"
    )
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр после получения разрешения суда Альфа "
        "может отозвать пропуск",
        supported=honest,
    )


def test_fresh_permission_keeps_shared_subject_and_local_scope() -> None:
    source = (
        "С разрешения суда Альфа организация Центр может выдать лицензию "
        "и с разрешения прокуратуры Бета отозвать пропуск."
    )
    issue = (
        "Организация Центр с разрешения суда Альфа может выдать лицензию"
    )
    revocation = (
        "Организация Центр с разрешения прокуратуры Бета может "
        "отозвать пропуск"
    )
    _assert_supported_in_all_output_paths(source, issue)
    _assert_supported_in_all_output_paths(source, revocation)
    validate_synonym_source_evidence(source, [issue, revocation])
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр с разрешения суда Альфа может отозвать пропуск",
        supported=revocation,
    )
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
        supported=revocation,
    )


def test_permission_scope_with_modifiers_keeps_shared_subject() -> None:
    source = (
        "С разрешения суда Альфа организация Центр может выдать лицензию, "
        "а также при необходимости повторно отозвать пропуск."
    )
    honest = (
        "Организация Центр с разрешения суда Альфа может при необходимости "
        "повторно отозвать пропуск"
    )
    unsafe = (
        "Организация Центр может при необходимости повторно отозвать пропуск"
    )
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        unsafe,
        supported=honest,
    )


@pytest.mark.parametrize(
    "relative_clause",
    (
        "организация получит разрешение суда Альфа",
        "разрешение будет получено от суда Альфа",
        "суд Альфа выдаст разрешение",
    ),
    ids=("active-receipt", "passive-receipt", "active-issuer"),
)
def test_supported_relative_permission_voice_controls(
    relative_clause: str,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"{relative_clause}."
    )
    honest = (
        "Организация Центр после получения разрешения суда Альфа "
        "может отозвать пропуск"
    )
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
        supported=honest,
    )


def test_permission_authority_and_polarity_controls_remain_exact() -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как не "
        "получит разрешение суда Альфа."
    )
    honest = (
        "Организация Центр может отозвать пропуск после того, как не "
        "получит разрешение суда Альфа"
    )
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск после того, как "
        "получит разрешение суда Альфа",
        supported=honest,
    )
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск после того, как не "
        "получит разрешение суда Бета",
        supported=honest,
    )


def test_overwindow_relative_permission_remains_fail_closed() -> None:
    holder = ("очень важная региональная " * 40) + "организация"
    source = (
        "Организация Центр может отозвать пропуск после того, как "
        f"{holder} получит разрешение суда Альфа."
    )
    assert _relation_permission_spans(source)
    _assert_rejected_in_all_output_paths(
        source,
        "Организация Центр может отозвать пропуск",
    )


@pytest.mark.parametrize(
    ("new_subject", "verb"),
    (
        ("организация Бета", "отозвала"),
        ("суд Бета", "отозвал"),
        ("заявитель", "отозвал"),
        ("владелец", "отозвал"),
    ),
)
def test_leading_permission_does_not_cross_a_fresh_subject(
    new_subject: str,
    verb: str,
) -> None:
    source = (
        "С разрешения суда Альфа организация Центр выдала лицензию, "
        f"а также {new_subject} {verb} пропуск."
    )
    honest = f"{new_subject.capitalize()} {verb} пропуск"
    _assert_supported_in_all_output_paths(source, honest)
    _assert_rejected_in_all_output_paths(
        source,
        f"{new_subject.capitalize()} с разрешения суда Альфа {verb} пропуск",
        supported=honest,
    )


@pytest.mark.parametrize(
    "source",
    (
        (
            "С разрешения суда Альфа проводится проверка. "
            "Организация Центр может отозвать лицензию."
        ),
        (
            "С разрешения суда Альфа проводится проверка; "
            "организация Центр может отозвать лицензию."
        ),
        (
            "С разрешения суда Альфа проводится проверка\n"
            "Организация Центр может отозвать лицензию."
        ),
        (
            "Организация Центр может отозвать лицензию. "
            "Документ хранится с разрешения суда Альфа."
        ),
        (
            "Организация Центр может отозвать лицензию; "
            "документ хранится с разрешения суда Альфа."
        ),
        (
            "Организация Центр может отозвать лицензию\n"
            "Документ хранится с разрешения суда Альфа."
        ),
    ),
    ids=(
        "leading-period",
        "leading-semicolon",
        "leading-newline",
        "trailing-period",
        "trailing-semicolon",
        "trailing-newline",
    ),
)
def test_permission_cannot_cross_a_hard_clause_boundary(source: str) -> None:
    _assert_supported_in_all_output_paths(
        source,
        "Организация Центр может отозвать лицензию",
    )
