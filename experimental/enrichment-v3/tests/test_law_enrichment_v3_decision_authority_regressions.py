from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_relation_truths,
    _relation_decision_authority_contract,
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
    ("source_role", "changed_role"),
    (
        (
            "по судебному решению суда Альфа",
            "по судебному решению суда Бета",
        ),
        (
            "по окончательному решению суда Альфа",
            "по окончательному решению суда Бета",
        ),
        (
            "по совместному решению суда Альфа",
            "по совместному решению суда Бета",
        ),
        (
            "на основании вступившего в силу решения суда Альфа",
            "на основании вступившего в силу решения суда Бета",
        ),
        (
            "согласно окончательному постановлению суда Альфа",
            "согласно окончательному постановлению суда Бета",
        ),
        (
            "по решению федерального суда Альфа",
            "по решению федерального суда Бета",
        ),
        (
            "по решению окружного суда Альфа",
            "по решению окружного суда Бета",
        ),
        (
            "в соответствии с решением суда Альфа",
            "в соответствии с решением суда Бета",
        ),
        (
            "по решению, принятому судом Альфа",
            "по решению, принятому судом Бета",
        ),
        ("по решению FIB Alpha", "по решению FIB Beta"),
        ("по решению LSPD Alpha", "по решению LSPD Beta"),
        (
            "по решению правительства Альфа",
            "по решению правительства Бета",
        ),
        (
            "по решению парламента Альфа",
            "по решению парламента Бета",
        ),
        (
            "по решению организации Альфа",
            "по решению организации Бета",
        ),
        ("по решению DEA Alpha", "по решению DEA Beta"),
    ),
    ids=(
        "judicial-modifier",
        "final-modifier",
        "joint-decision-modifier",
        "entered-into-force",
        "modified-resolution",
        "federal-court",
        "district-court",
        "in-accordance-with",
        "relative-participle",
        "fib",
        "lspd",
        "government",
        "parliament",
        "organization",
        "unknown-ascii",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_decision_forms_preserve_complete_authority_identity(
    source_role: str,
    changed_role: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Организация может отозвать пропуск только "
        f"{source_role}."
    )
    supported = source.rstrip(".")
    changed = (
        "Организация может отозвать пропуск только "
        f"{changed_role}"
    )

    validate(source, supported)
    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    ("generic_role", "invented_role"),
    (
        ("по решению FIB", "по решению FIB Alpha"),
        (
            "по решению правительства",
            "по решению правительства Альфа",
        ),
        (
            "по решению парламента",
            "по решению парламента Альфа",
        ),
        (
            "по решению организации",
            "по решению организации Альфа",
        ),
        ("по решению DEA", "по решению DEA Alpha"),
    ),
    ids=("fib", "government", "parliament", "organization", "unknown-ascii"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_generic_decision_authority_cannot_invent_specific_identity(
    generic_role: str,
    invented_role: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Организация может отозвать пропуск только "
        f"{generic_role}."
    )
    invented = (
        "Организация может отозвать пропуск только "
        f"{invented_role}"
    )

    _assert_rejected(validate, source, invented)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_explicit_or_decision_authority_keeps_proven_branches(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация может отозвать пропуск по окончательному решению "
        "FIB Alpha или LSPD Beta."
    )

    validate(
        source,
        "Организация может отозвать пропуск по окончательному решению "
        "FIB Alpha",
    )
    validate(
        source,
        "Организация может отозвать пропуск по окончательному решению "
        "LSPD Beta",
    )
    _assert_rejected(
        validate,
        source,
        "Организация может отозвать пропуск по окончательному решению "
        "FIB Gamma",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_joint_decision_authority_is_atomic(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация может отозвать пропуск по совместному решению "
        "FIB Alpha совместно с LSPD Beta."
    )
    supported = (
        "Организация может отозвать пропуск по совместному решению "
        "LSPD Beta совместно с FIB Alpha"
    )

    validate(source, supported)
    for unsafe in (
        "Организация может отозвать пропуск по совместному решению FIB Alpha",
        (
            "Организация может отозвать пропуск по совместному решению "
            "FIB Alpha или LSPD Beta"
        ),
        (
            "Организация может отозвать пропуск по совместному решению "
            "FIB Alpha совместно с LSPD Gamma"
        ),
    ):
        _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "temporal",
    (
        "после уведомления владельца",
        "после вступления постановления в силу",
        "до окончания проверки",
        "до истечения срока",
    ),
    ids=("after-notice", "after-entry", "before-end", "before-expiry"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_decision_trailing_temporal_facet_is_not_identity_or_droppable(
    temporal: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Организация может отозвать пропуск по решению суда Альфа "
        f"{temporal}."
    )
    supported = source.rstrip(".")

    validate(source, supported)
    _assert_rejected(
        validate,
        source,
        "Организация может отозвать пропуск по решению суда Альфа",
    )
    _assert_rejected(
        validate,
        source,
        (
            "Организация может отозвать пропуск по решению суда Бета "
            f"{temporal}"
        ),
    )


@pytest.mark.parametrize(
    ("surface", "expected"),
    (
        ("по решению FIB Alpha", "fib alpha"),
        ("по решению LSPD Alpha", "lspd alpha"),
        ("по решению правительства Альфа", "правительств альф"),
        ("по решению парламента Альфа", "парламент альф"),
        ("по решению организации Альфа", "организаци альф"),
        ("по решению DEA Alpha", "dea alpha"),
        ("по решению федерального суда Альфа", "федеральн суд альф"),
    ),
)
def test_v3_decision_contract_uses_shared_complete_actor_identity(
    surface: str,
    expected: str,
) -> None:
    authority, alternatives = _relation_decision_authority_contract(surface)

    assert authority == expected
    assert alternatives == ()


def test_v3_decision_role_never_becomes_the_operative_actor() -> None:
    source = (
        "Организация может отозвать пропуск только по решению FIB Alpha."
    )
    relations = _extract_relation_truths(source)

    assert any(
        relation.direct_object == "access_pass"
        and relation.operative_actor == "организаци"
        and relation.decision_authority == "fib alpha"
        for relation in relations
    )
