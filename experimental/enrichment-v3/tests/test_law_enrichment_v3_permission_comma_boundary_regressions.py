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
        [source_surface, output],
    )


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


@pytest.mark.parametrize("modifier", ("затем", "лично"))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_permission_comma_preserves_russian_actor_after_neutral_modifier(
    modifier: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"С разрешения суда Альфа, {modifier} полиция может отозвать "
        "пропуск."
    )
    supported = (
        f"{modifier.capitalize()} полиция с разрешения суда Альфа может "
        "отозвать пропуск"
    )

    validate(source, supported)


@pytest.mark.parametrize(
    "unsupported_prefix",
    (
        "вероятно",
        "затем,",
    ),
    ids=("unknown-word", "malformed-extra-comma"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_permission_comma_does_not_guess_unsupported_actor_prefix(
    unsupported_prefix: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"С разрешения суда Альфа, {unsupported_prefix} полиция может "
        "отозвать пропуск."
    )
    guessed = (
        "Полиция с разрешения суда Альфа может отозвать пропуск"
    )

    _assert_rejected(validate, source, guessed)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_permission_comma_keeps_genitive_joint_authority_list(
    validate: RelationValidator,
) -> None:
    source = (
        "С разрешения суда Альфа, прокуратуры Бета и губернатора Гамма, "
        "затем полиция может отозвать пропуск."
    )
    supported = (
        "Затем полиция с разрешения суда Альфа, прокуратуры Бета и "
        "губернатора Гамма может отозвать пропуск"
    )

    validate(source, supported)


@pytest.mark.parametrize(
    "unsafe",
    (
        "Затем полиция с разрешения суда Бета может отозвать пропуск",
        "Затем шерифы с разрешения суда Альфа могут отозвать пропуск",
        "Затем шерифы с разрешения суда Бета могут отозвать пропуск",
    ),
    ids=("different-authority", "different-actor", "both-different"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_permission_comma_keeps_authority_and_operative_actor_separate(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = (
        "С разрешения суда Альфа, затем полиция может отозвать пропуск."
    )

    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("opener", "closer"),
    (("«", "»"), ("(", ")")),
    ids=("quoted", "parenthesized"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_permission_comma_does_not_split_balanced_authority_identity(
    opener: str,
    closer: str,
    validate: RelationValidator,
) -> None:
    authority = f"суд {opener}Альфа, затем полиция{closer}"
    source = (
        f"С разрешения {authority}, затем прокуратура может отозвать "
        "пропуск."
    )
    supported = (
        f"Затем прокуратура с разрешения {authority} может отозвать "
        "пропуск"
    )
    truncated_or_changed = (
        f"Затем прокуратура с разрешения суда {opener}Альфа, затем "
        f"шерифы{closer} может отозвать пропуск"
    )

    validate(source, supported)
    _assert_rejected(validate, source, truncated_or_changed)
