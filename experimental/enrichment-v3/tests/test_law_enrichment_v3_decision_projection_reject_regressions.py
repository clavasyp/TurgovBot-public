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


@pytest.mark.parametrize(
    "source",
    (
        "Суд, возможно, может вынести решение об аннулировании лицензии.",
        "Суд может, по слухам, вынести решение об аннулировании лицензии.",
    ),
    ids=("inline-possibly", "inline-rumour"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nonassertive_governing_decision_cannot_prove_positive_action(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "суд может аннулировать лицензию")


@pytest.mark.parametrize(
    "decision_scope",
    (
        "об отказе в аннулировании",
        "о запрете аннулирования",
        "о предотвращении аннулирования",
        "о недопущении аннулирования",
    ),
    ids=("refusal", "prohibition", "prevention", "non-admission"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_reversal_decision_owns_its_nominal_complement(
    decision_scope: str,
    validate: RelationValidator,
) -> None:
    source = f"Суд вынес решение {decision_scope} лицензии."
    with pytest.raises(ValueError):
        validate(source, "суд аннулирует лицензию")


@pytest.mark.parametrize(
    ("modal", "output_modal"),
    (("должен", "должен"), ("обязан", "обязан")),
    ids=("must", "obliged"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_mandatory_decision_projects_actor_and_modality(
    modal: str,
    output_modal: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Суд {modal} вынести решение об аннулировании лицензии."
    )
    validate(source, f"суд {output_modal} аннулировать лицензию")


@pytest.mark.parametrize("modal", ("не должен", "не обязан"))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_negative_mandatory_decision_cannot_prove_positive_action(
    modal: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Суд {modal} вынести решение об аннулировании лицензии."
    )
    with pytest.raises(ValueError):
        validate(source, "суд должен аннулировать лицензию")


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд округа Блейн может вынести решение об аннулировании лицензии.",
            "суд округа Блейн может аннулировать лицензию",
        ),
        (
            "Суд округа Блейн может повторно лично вынести окончательное "
            "решение об аннулировании лицензии.",
            "суд округа Блейн может аннулировать лицензию",
        ),
        (
            "Суд округа Блейн лично повторно вынес окончательное решение "
            "об аннулировании лицензии.",
            "суд округа Блейн аннулирует лицензию",
        ),
    ),
    ids=("full-actor", "optional-modifiers", "asserted-modifiers"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_decision_projection_preserves_full_actor_and_modifiers(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)
    with pytest.raises(ValueError):
        validate(source, output.replace("округа Блейн", "округа Лос-Сантос"))


@pytest.mark.parametrize(
    ("nominal", "optional_output", "definite_output"),
    (
        ("аннулировании", "суд может аннулировать лицензию", "суд аннулирует лицензию"),
        ("отзыве", "суд может отозвать лицензию", "суд отзывает лицензию"),
        (
            "приостановлении",
            "суд может приостановить лицензию",
            "суд приостанавливает лицензию",
        ),
        ("изъятии", "суд может изъять лицензию", "суд изымает лицензию"),
    ),
    ids=("cancellation", "revocation", "suspension", "seizure"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_optional_decision_projects_the_same_action_family_everywhere(
    nominal: str,
    optional_output: str,
    definite_output: str,
    validate: RelationValidator,
) -> None:
    source = f"Суд может вынести решение о {nominal} лицензии."
    validate(source, optional_output)
    with pytest.raises(ValueError):
        validate(source, definite_output)


@pytest.mark.parametrize(
    "source",
    (
        "Суд обсудил решение об аннулировании лицензии.",
        "Суд запланировал решение об аннулировании лицензии.",
        "Суд отказался вынести решение об аннулировании лицензии.",
    ),
    ids=("discussion", "plan", "refused-to-decide"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_arbitrary_decision_context_does_not_prove_action(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "суд аннулирует лицензию")


_CONDITIONAL_APPEAL_SUSPENSION_SOURCE = (
    "В случае подачи апелляционной или кассационной жалобы на судебный акт, "
    "подлежащий исполнению, течение сроков и порядок исполнения такого акта "
    "приостанавливаются по праву до момента вынесения решения по жалобе "
    "вышестоящей судебной инстанцией."
)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_conditional_appeal_suspension_cannot_become_unconditional(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            _CONDITIONAL_APPEAL_SUSPENSION_SOURCE,
            "апелляция автоматически приостанавливает исполнение акта",
        )


@pytest.mark.parametrize(
    "output",
    (
        "при подаче апелляции исполнение судебного акта приостанавливается",
        "исполнение акта приостанавливается при подаче кассационной жалобы",
    ),
    ids=("appeal", "cassation"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_conditional_appeal_suspension_keeps_its_trigger(
    output: str,
    validate: RelationValidator,
) -> None:
    validate(_CONDITIONAL_APPEAL_SUSPENSION_SOURCE, output)
