from __future__ import annotations

from services.law_enrichment_validation import (
    _RELATION_PERMISSION_DNF_MAX_BRANCHES,
    _RELATION_PERMISSION_DNF_OVERFLOW_QUALIFIER,
    _RelationActorExpression,
    _RelationArgument,
    _relation_actor_expression_node,
    _relation_permission_atom_digest,
    _relation_permission_expression_argument,
    _relation_permission_expression_dnf,
    _relation_permission_formula_supported,
    _relation_permission_spans,
)


def _leaf(key: str) -> _RelationActorExpression:
    return _RelationActorExpression(operator="leaf", key=key)


def _node(
    operator: str,
    left: _RelationActorExpression,
    right: _RelationActorExpression,
) -> _RelationActorExpression:
    return _relation_actor_expression_node(
        operator,
        left,
        right,
        "explicit_alternative" if operator == "alternative" else "joint",
    )


def _binary_choice(index: int) -> _RelationActorExpression:
    return _node(
        "alternative",
        _leaf(f"a{index}"),
        _leaf(f"b{index}"),
    )


def _joint_binary_choices(count: int) -> _RelationActorExpression:
    expression = _binary_choice(0)
    for index in range(1, count):
        expression = _node("joint", expression, _binary_choice(index))
    return expression


def _requirement(value: str) -> _RelationArgument:
    return _RelationArgument(
        role="permission_requirement",
        value=value,
        marker="consent",
    )


def _overflow_expression() -> _RelationArgument:
    return _RelationArgument(
        role="permission_expression",
        value="opaque-overflow",
        marker="mixed",
        qualifiers=frozenset({_RELATION_PERMISSION_DNF_OVERFLOW_QUALIFIER}),
    )


def test_permission_dnf_accepts_exact_branch_limit() -> None:
    branches = _relation_permission_expression_dnf(_joint_binary_choices(6))

    assert branches is not None
    assert len(branches) == _RELATION_PERMISSION_DNF_MAX_BRANCHES == 64


def test_permission_dnf_fails_closed_above_branch_limit() -> None:
    assert _relation_permission_expression_dnf(_joint_binary_choices(7)) is None


def test_permission_parser_emits_only_overflow_sentinel_above_limit() -> None:
    groups = [
        f"(с разрешения суда Alpha{index} или с разрешения суда Beta{index})"
        for index in range(7)
    ]
    source = (
        "Организация Центр "
        + " и ".join(groups)
        + " может отозвать пропуск."
    )
    permissions = _relation_permission_spans(source)

    assert len(permissions) == 14
    expression = _relation_permission_expression_argument(source, permissions)
    assert expression is not None
    assert expression.qualifiers == frozenset(
        {_RELATION_PERMISSION_DNF_OVERFLOW_QUALIFIER}
    )
    assert not any(
        qualifier.startswith("dnf:") for qualifier in expression.qualifiers
    )


def test_permission_dnf_keeps_distributive_equivalence() -> None:
    left = _node(
        "joint",
        _node("alternative", _leaf("a"), _leaf("b")),
        _leaf("c"),
    )
    right = _node(
        "alternative",
        _node("joint", _leaf("a"), _leaf("c")),
        _node("joint", _leaf("b"), _leaf("c")),
    )

    assert _relation_permission_expression_dnf(left) == (
        ("a", "c"),
        ("b", "c"),
    )
    assert _relation_permission_expression_dnf(left) == (
        _relation_permission_expression_dnf(right)
    )


def test_output_overflow_cannot_pass_by_opaque_equality() -> None:
    atoms = frozenset({_requirement(f"authority-{index}") for index in range(14)})
    overflow = _overflow_expression()

    assert not _relation_permission_formula_supported(
        atoms | {overflow},
        atoms | {overflow},
    )


def test_source_overflow_requires_conservative_all_atom_proof() -> None:
    atoms = frozenset({_requirement(f"authority-{index}") for index in range(14)})
    source = atoms | {_overflow_expression()}

    assert _relation_permission_formula_supported(atoms, source)
    assert not _relation_permission_formula_supported(
        frozenset(tuple(atoms)[:-1]),
        source,
    )


def test_source_overflow_rejects_or_of_all_atoms() -> None:
    atoms = frozenset({_requirement(f"authority-{index}") for index in range(14)})
    source = atoms | {_overflow_expression()}
    output_or = _RelationArgument(
        role="permission_expression",
        value="all-atoms-as-alternatives",
        marker="alternative",
        qualifiers=frozenset(
            f"dnf:{_relation_permission_atom_digest(atom)}" for atom in atoms
        ),
    )

    assert not _relation_permission_formula_supported(
        atoms | {output_or},
        source,
    )
