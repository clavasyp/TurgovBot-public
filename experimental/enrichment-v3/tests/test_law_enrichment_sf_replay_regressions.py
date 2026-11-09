from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _validate_relation_source_evidence,
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
    _validate_relation_source_evidence(
        source,
        output,
        field="synonym 0",
    )


RELATION_VALIDATORS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_synonym,
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_multiline_authority_roster_keeps_specific_deputy_office(
    validate: RelationValidator,
) -> None:
    source = (
        "Список лиц, уполномоченных выписывать ордера:\n"
        "— Губернатор\n"
        "—\n"
        "Вице-Губернатор\n"
        "—\n"
        "Генеральный Прокурор\n"
        "— Заместитель Генерального прокурора\n"
        "— Председатель Верховного суда\n"
        "— Верховный Судья"
    )
    validate(source, "заместитель генерального прокурора выдает ордер")
    with pytest.raises(ValueError):
        validate(source, "полиция выдает ордер")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_appositive_governor_roster_keeps_typed_revocation_grounds(
    validate: RelationValidator,
) -> None:
    source = (
        "Губернатор и вице-губернатор, как глава и заместитель главы "
        "исполнительной власти штата, вправе аннулировать пропуск, если "
        "лица нарушают нормальный режим функционирования организации, "
        "препятствуют её работе или инициируют конфликтные ситуации."
    )
    validate(
        source,
        "губернатор вправе аннулировать пропуск нарушителю режима",
    )
    validate(
        source,
        "могут отозвать допуск за провоцирование конфликта",
    )
    with pytest.raises(ValueError):
        validate(source, "губернатор вправе аннулировать пропуск")
    with pytest.raises(ValueError):
        validate(
            source,
            "губернатор вправе аннулировать пропуск за обычную жалобу",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_anytime_revocation_paraphrase_requires_exact_temporal_evidence(
    validate: RelationValidator,
) -> None:
    output = "можно отозвать пропуск без ожидания срока"
    validate(
        (
            "Пропуск, выданный руководителем организации, может быть им "
            "отозван в любое время."
        ),
        output,
    )
    with pytest.raises(ValueError, match="any-time"):
        validate(
            "Пропуск может быть отозван руководителем организации.",
            output,
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_criminal_use_is_a_bounded_access_pass_revocation_ground(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом как дополнительная мера "
        "наказания, когда преступление совершено с использованием "
        "такого пропуска."
    )
    validate(
        source,
        (
            "суд может отозвать допуск как дополнительное наказание "
            "за преступное применение"
        ),
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "суд может отозвать допуск за обычную жалобу",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_active_attributive_revocation_participle_is_context_only(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск не может быть выписан повторно без разрешения лица, "
        "его аннулировавшего."
    )
    validate(source, "согласие аннулировавшего на восстановление допуска")
    with pytest.raises(ValueError):
        validate(source, "суд аннулировал пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_issue_decision_form_keeps_its_access_pass_object(
    validate: RelationValidator,
) -> None:
    source = (
        "Решение о выдаче допуска оформляется в виде приказа или "
        "распоряжения и регистрируется в установленном порядке."
    )
    validate(source, "решение о допуске оформляется приказом и регистрируется")
    with pytest.raises(ValueError):
        validate(source, "решение о допуске оформляется договором")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_long_mayor_power_heading_preserves_office_head(
    validate: RelationValidator,
) -> None:
    source = (
        "7.2.\n"
        "Полномочия и обязанности мэра агломерации Los Santos & "
        "Blaine County:\n"
        "Проводить культурные мероприятия;\n"
        "Выдавать лицензии коллекционера.\n"
        "7.3. Заместитель действует во время отсутствия мэра."
    )
    validate(source, "мэр выдаёт лицензию коллекционера")
    with pytest.raises(ValueError):
        validate(source, "губернатор выдаёт лицензию коллекционера")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_passive_police_seizure_condition_is_covered_by_typed_grounds(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия должна быть аннулирована и изъята при задержании "
        "сотрудниками правоохранительных органов только в случае "
        "нарушения любой из указанных статей УК."
    )
    validate(
        source,
        (
            "правоохранители обязаны изъять лицензию при задержании "
            "по указанным статьям ук"
        ),
    )
    with pytest.raises(ValueError):
        validate(
            source,
            "правоохранители обязаны изъять лицензию при задержании",
        )
    with pytest.raises(ValueError):
        validate(
            source,
            (
                "правоохранители обязаны изъять лицензию при задержании "
                "за обычную жалобу"
            ),
        )


BAD_TO_SOURCE_BACKED_SF_REWRITES = (
    (
        (
            "Губернатор и вице-губернатор вправе аннулировать пропуск, "
            "если лицо препятствует работе государственной организации."
        ),
        "вице-губернатор вправе отменить территориальный допуск",
        (
            "аннулирование пропуска вице-губернатором за помехи "
            "работе госорганизации"
        ),
    ),
    (
        (
            "Глава и заместитель главы исполнительной власти вправе "
            "аннулировать пропуск, если лицо нарушает установленный "
            "режим."
        ),
        "право исполнительной власти отменить территориальный пропуск",
        (
            "аннулирование пропуска главой или заместителем главы "
            "за нарушение режима"
        ),
    ),
    (
        (
            "Пропуск, выданный руководителем организации, может быть "
            "им отозван в любое время."
        ),
        "право выдавшего начальника отменить разрешение",
        "право выдавшего руководителя отозвать пропуск",
    ),
    (
        (
            "Пропуск, выданный руководителем или заместителем руководителя "
            "государственной организации, может быть им отозван в любое время."
        ),
        "заместитель начальника может отменить свой территориальный допуск",
        "заместитель руководителя может отозвать пропуск",
    ),
    (
        (
            "Пропуск, выданный руководителем или заместителем руководителя "
            "государственной организации, может быть им отозван в любое время."
        ),
        "начальник может отозвать допуск в любое время",
        "руководитель может отозвать пропуск в любое время",
    ),
    (
        (
            "Сотрудники правоохранительных органов вправе обыскивать "
            "лиц на территории, обладающей особым статусом."
        ),
        "полиция обыскивает посетителей особой территории",
        "право полиции на обыск лиц на территории особого статуса",
    ),
    (
        (
            "При наличии ордера сотрудник дорожной полиции имеет право "
            "на обыск транспортного средства."
        ),
        "ордер разрешает копу обыскать транспорт",
        "право дорожной полиции на обыск транспорта при наличии ордера",
    ),
    (
        (
            "Правоохранительные органы вправе проводить обыск граждан, "
            "нарушающих установленный режим."
        ),
        "правоохранители обыскивают нарушителей установленного режима",
        (
            "право правоохранителей на обыск граждан, нарушающих "
            "установленный режим"
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "bad", "replacement"),
    BAD_TO_SOURCE_BACKED_SF_REWRITES,
)
def test_sf_replay_replaces_overbroad_synonyms_without_weakening_validator(
    source: str,
    bad: str,
    replacement: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [bad])
    assert len(replacement) <= 80
    validate_synonym_source_evidence(source, [replacement])
