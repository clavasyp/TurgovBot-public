"""Offline tests for the production HTTP law-search smoke evidence."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from scripts import smoke_law_search_http as smoke


RUN_NONCE = "a" * 64


def _full_payload(top: dict) -> dict:
    return {
        "query": "test",
        "results": [top],
        "related": [],
        "degraded": False,
        "search_mode": "full",
        "warnings": [],
    }


@pytest.mark.parametrize(
    "name,card",
    [
        (
            "homicide",
            {
                "title": "Убийство сотрудника полиции",
                "snippet": "Умышленное причинение смерти сотруднику полиции.",
            },
        ),
        (
            "residential_theft",
            {
                "title": "Кража",
                "snippet": "Тайное хищение чужого имущества из жилища.",
            },
        ),
        (
            "illicit_weapon_purchase",
            {
                "title": "Незаконный оборот оружия",
                "snippet": "Незаконное приобретение и передача огнестрельного оружия.",
            },
        ),
        (
            "drug_possession",
            {
                "title": "Хранение наркотических средств",
                "snippet": "Незаконное хранение наркотиков при себе.",
            },
        ),
        (
            "assault_on_employee",
            {
                "title": "Нападение на сотрудника полиции",
                "snippet": "Насилие в отношении представителя власти.",
            },
        ),
        (
            "obscene_language",
            {
                "title": "Мелкое хулиганство",
                "snippet": "Нарушение общественного порядка нецензурной бранью.",
            },
        ),
    ],
)
def test_semantic_validators_accept_strong_editorial_evidence(name, card):
    rule = next(rule for rule in smoke.SLANG_RULES if rule.name == name)
    status, _reason = smoke.validate_semantic_card(rule, card)
    assert status == "pass"


def test_obscene_language_accepts_direct_legal_title():
    rule = next(rule for rule in smoke.SLANG_RULES if rule.name == "obscene_language")

    status, _reason = smoke.validate_semantic_card(
        rule,
        {
            "title": "Использование нецензурной лексики в общественных местах",
            "snippet": "Статья 5.3.",
        },
    )

    assert status == "pass"


def test_obscene_language_accepts_detroit_compact_card():
    rule = next(
        rule
        for rule in smoke.SLANG_RULES
        if rule.name == "obscene_language"
    )

    status, _reason = smoke.validate_semantic_card(
        rule,
        {
            "title": "Мелкое хулиганство,",
            "article_ref": "Статья 9.",
            "snippet": (
                "Мелкое хулиганство, то есть нарушение общественного "
                "порядка, выражающее явное неуважение к обществу."
            ),
        },
    )

    assert status == "pass"


@pytest.mark.parametrize(
    "card",
    [
        {
            "title": "Хулиганство с применением оружия",
            "snippet": "Опасное вооруженное нападение.",
        },
        {
            "title": "Мелкое хулиганство",
            "snippet": "Нарушение порядка в частном доме.",
        },
        {
            "title": "Мелкое хулиганство",
            "snippet": (
                "Общественные мероприятия и правила внутреннего порядка."
            ),
        },
        {
            "title": "Мелкое нарушение общественного порядка",
            "snippet": "Административный состав не установлен.",
        },
    ],
)
def test_obscene_language_public_order_fallback_keeps_negative_contrasts(card):
    rule = next(
        rule
        for rule in smoke.SLANG_RULES
        if rule.name == "obscene_language"
    )

    status, _reason = smoke.validate_semantic_card(rule, card)

    assert status == "manual_review"


def test_assault_accepts_declined_authority_representative_from_real_law():
    rule = next(
        rule for rule in smoke.SLANG_RULES
        if rule.name == "assault_on_employee"
    )

    status, _reason = smoke.validate_semantic_card(
        rule,
        {
            "title": "Нанесение телесных повреждений",
            "snippet": (
                "Нанесение телесных повреждений в отношении "
                "представителя власти."
            ),
        },
    )

    assert status == "pass"


@pytest.mark.parametrize(
    "snippet",
    [
        "Нанесение телесных повреждений представителю средств массовой информации.",
        "Полномочия представителя власти при рассмотрении документов.",
    ],
)
def test_assault_declension_fix_rejects_incomplete_role_or_act(snippet):
    rule = next(
        rule for rule in smoke.SLANG_RULES
        if rule.name == "assault_on_employee"
    )

    status, _reason = smoke.validate_semantic_card(
        rule,
        {"title": "Не связанная статья", "snippet": snippet},
    )

    assert status == "manual_review"


@pytest.mark.parametrize(
    "name,card",
    [
        (
            "homicide",
            {
                "title": "Применение насилия в отношении представителя власти",
                "snippet": "Причинение телесных повреждений сотруднику полиции.",
            },
        ),
        (
            "residential_theft",
            {
                "title": "Обыск жилища",
                "snippet": "Порядок осмотра дома в присутствии владельца.",
            },
        ),
        (
            "illicit_weapon_purchase",
            {
                "title": "Получение лицензии на гражданское оружие",
                "snippet": "Законный порядок покупки оружия при наличии лицензии.",
            },
        ),
        (
            "drug_possession",
            {
                "title": "Наркотическое опьянение",
                "snippet": "Нахождение гражданина в состоянии опьянения.",
            },
        ),
        (
            "assault_on_employee",
            {
                "title": "Сотрудник полиции",
                "snippet": "Права и обязанности сотрудника правоохранительного органа.",
            },
        ),
        (
            "obscene_language",
            {
                "title": "Хулиганство с применением оружия",
                "snippet": "Грубое нарушение порядка с использованием оружия.",
            },
        ),
    ],
)
def test_semantic_validators_reject_nearby_but_wrong_concepts(name, card):
    rule = next(rule for rule in smoke.SLANG_RULES if rule.name == name)
    status, reason = smoke.validate_semantic_card(
        rule,
        card,
    )
    assert status == "manual_review"
    assert "does not prove" in reason


def test_homicide_validator_rejects_generic_murder_without_protected_target():
    rule = next(rule for rule in smoke.SLANG_RULES if rule.name == "homicide")
    status, _reason = smoke.validate_semantic_card(
        rule,
        {
            "title": "Убийство",
            "snippet": "Умышленное причинение смерти другому человеку.",
        },
    )
    assert status == "manual_review"


def test_smoke_uses_the_literal_six_handoff_queries():
    assert [rule.query for rule in smoke.SLANG_RULES] == [
        "замочил копа",
        "вынес хату",
        "купил ствол с рук",
        "поймали с закладкой",
        "мента избили",
        "ругается матом",
    ]


def test_dynamic_expected_id_is_required_even_when_response_is_full():
    case = {
        "name": "dynamic_code_doc_1",
        "kind": "exact_code_doc",
        "query": "статья 12.1 УК штата",
        "expected": {
            "id": 77,
            "code": "12.1",
            "title": "Верная",
            "doc_title": "УК штата",
        },
    }
    passed = smoke.evaluate_case(
        case,
        http_status=200,
        payload=_full_payload({
            "id": 77,
            "code": "12.1",
            "title": "Верная",
            "doc_title": "УК штата",
            "score": 0.9,
            "sources": ["code"],
        }),
        elapsed_ms=10,
    )
    failed = smoke.evaluate_case(
        case,
        http_status=200,
        payload=_full_payload({
            "id": 78,
            "code": "12.1",
            "title": "Чужая",
            "doc_title": "Другой документ",
            "score": 0.9,
            "sources": ["code"],
        }),
        elapsed_ms=10,
    )
    assert passed["status"] == "pass" and passed["point"] == 1
    assert failed["status"] == "fail" and failed["point"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {
            "results": [{"id": 1}],
            "search_mode": "partial",
            "degraded": True,
            "warnings": ["vector_timeout"],
        },
        {
            "results": [],
            "search_mode": "full",
            "degraded": False,
            "warnings": [],
        },
        {
            "results": [{"id": 1}],
            "search_mode": "full",
            "degraded": False,
            "warnings": ["related_error"],
        },
    ],
)
def test_technical_contract_is_fail_closed(payload):
    ok, _reason = smoke.technical_full(200, payload)
    assert ok is False


def test_source_fingerprint_is_order_independent_and_overlay_source_only():
    rows = [
        {
            "id": 2,
            "source_thread_id": 20,
            "content_hash": "b",
            "last_updated": "2026-07-28T00:00:00+00:00",
        },
        {
            "id": 1,
            "source_thread_id": 10,
            "content_hash": "a",
            "last_updated": "2026-07-27T00:00:00+00:00",
        },
    ]
    first = smoke.source_fingerprint(rows)
    second = smoke.source_fingerprint(reversed(rows))
    changed = smoke.source_fingerprint([
        {**rows[0], "content_hash": "changed"},
        rows[1],
    ])
    assert first == second
    assert first["value"] != changed["value"]
    assert first["documents"] == 2
    operational_only = smoke.source_fingerprint([
        {
            **rows[0],
            "gemini_file_uri": "gs://other/object",
            "derived_sync_pending": True,
            "last_updated": "2099-01-01T00:00:00+00:00",
        },
        rows[1],
    ])
    assert operational_only == first
    assert first["snapshot_version"] == 2


@pytest.mark.parametrize(
    "code,query",
    [
        ("12.1", "статья двенадцать точка один"),
        ("25", "статья двадцать пять"),
        ("10.15", "статья десять точка пятнадцать"),
    ],
)
def test_voice_query_uses_supported_runtime_number_words(code, query):
    assert smoke.code_to_voice_query(code) == query


@pytest.mark.parametrize("code", ["01", "12.01", "100", "A.1", ""])
def test_voice_query_rejects_codes_runtime_numerals_cannot_reproduce(code):
    assert smoke.code_to_voice_query(code) is None


def test_dynamic_selection_uses_unique_pairs_and_unique_voice_code():
    rows = [
        {
            "id": 1,
            "code": "1",
            "title": "A",
            "doc_title": "Document A",
            "pair_count": 1,
            "code_count": 2,
        },
        {
            "id": 2,
            "code": "2",
            "title": "B",
            "doc_title": "Document B",
            "pair_count": 1,
            "code_count": 2,
        },
        {
            "id": 3,
            "code": "3",
            "title": "C",
            "doc_title": "Document C",
            "pair_count": 1,
            "code_count": 1,
        },
        {
            "id": 4,
            "code": "12.1",
            "title": "D",
            "doc_title": "Document D",
            "pair_count": 1,
            "code_count": 1,
        },
    ]
    cases = smoke.select_dynamic_cases(rows)
    assert len(cases) == 4
    assert [case["expected"]["id"] for case in cases[:3]] == [1, 2, 3]
    assert cases[3]["kind"] == "voice_code"
    assert cases[3]["expected"]["id"] == 4
    assert cases[3]["query"] == "статья двенадцать точка один"


def test_dynamic_selection_can_use_three_unique_pairs_from_one_small_document():
    rows = [
        {
            "id": article_id,
            "code": str(article_id),
            "title": f"Article {article_id}",
            "doc_title": "Only Document",
            "pair_count": 1,
            "code_count": 1,
        }
        for article_id in (1, 2, 3)
    ]
    cases = smoke.select_dynamic_cases(rows)
    assert [case["expected"]["id"] for case in cases[:3]] == [1, 2, 3]
    assert len(cases) == 4


def test_evidence_requires_eight_points_every_technical_case_and_stable_source():
    fingerprint = {
        "algorithm": "sha256",
        "snapshot_version": 2,
        "documents": 2,
        "value": "abc",
    }
    fixed_tops = {
        "homicide": {"title": "Убийство сотрудника полиции"},
        "residential_theft": {"title": "Кража"},
        "illicit_weapon_purchase": {
            "title": "Незаконное приобретение оружия"
        },
        "drug_possession": {"title": "Хранение наркотиков"},
        # These two deliberately remain manual_review, giving 4 + 4 = 8.
        "assault_on_employee": {"title": "Не связанная статья"},
        "obscene_language": {"title": "Не связанная статья"},
    }
    descriptors = smoke._slang_cases() + smoke.select_dynamic_cases(
        [
            {
                "id": article_id,
                "code": code,
                "title": f"Article {article_id}",
                "doc_title": f"Document {article_id}",
                "pair_count": 1,
                "code_count": 1,
            }
            for article_id, code in (
                (101, "1"),
                (102, "2"),
                (103, "3"),
                (104, "12.1"),
            )
        ]
    )
    cases = []
    for index, descriptor in enumerate(descriptors):
        top = (
            fixed_tops[descriptor["name"]]
            if descriptor["kind"] == "slang"
            else {
                "id": descriptor["expected"]["id"],
                "code": descriptor["expected"]["code"],
                "title": descriptor["expected"]["title"],
                "doc_title": descriptor["expected"]["doc_title"],
            }
        )
        cases.append(
            smoke.evaluate_case(
                descriptor,
                http_status=200,
                payload=_full_payload(top),
                elapsed_ms=1,
                index=index,
            )
        )
    evidence = smoke.build_evidence(
        server_id="memphis",
        run_nonce=RUN_NONCE,
        base_url="http://127.0.0.1:8000",
        fingerprint_before=fingerprint,
        fingerprint_after=dict(fingerprint),
        cases=cases,
    )
    assert evidence["score"] == 8
    assert evidence["passed"] is True
    assert evidence["manual_review"] == [
        "assault_on_employee",
        "obscene_language",
    ]
    assert "token" not in json.dumps(evidence).lower()

    cases[0]["http_status"] = 503
    assert smoke.build_evidence(
        server_id="memphis",
        run_nonce=RUN_NONCE,
        base_url="http://127.0.0.1:8000",
        fingerprint_before=fingerprint,
        fingerprint_after=dict(fingerprint),
        cases=cases,
    )["passed"] is False
    cases[0]["http_status"] = 200
    changed = {**fingerprint, "value": "changed"}
    assert smoke.build_evidence(
        server_id="memphis",
        run_nonce=RUN_NONCE,
        base_url="http://127.0.0.1:8000",
        fingerprint_before=fingerprint,
        fingerprint_after=changed,
        cases=cases,
    )["passed"] is False


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://user:password@localhost:8000",
        "http://localhost:8000/path",
        "http://localhost:8000?token=secret",
    ],
)
def test_base_url_refuses_non_local_or_credential_bearing_targets(url):
    with pytest.raises(ValueError):
        smoke.validate_local_base_url(url)


def test_cli_requires_the_awaiting_smoke_nonce():
    with pytest.raises(SystemExit):
        smoke.build_parser().parse_args(["las_vegas"])
    args = smoke.build_parser().parse_args(
        ["las_vegas", "--run-nonce", RUN_NONCE]
    )
    assert args.run_nonce == RUN_NONCE


@pytest.mark.asyncio
async def test_real_smoke_window_holds_global_document_catalog_lock_order(
    monkeypatch,
):
    import services.law_backfill_lock as global_locks
    import services.law_document_lock as document_locks
    import services.law_embedding_service as embedding_locks

    state = {"global": False, "document": False, "catalog": False}
    events = []
    fingerprint = {
        "algorithm": "sha256",
        "snapshot_version": 2,
        "documents": 1,
        "value": "source",
    }
    candidates = [
        {
            "id": article_id,
            "code": code,
            "title": f"Article {article_id}",
            "doc_title": f"Document {article_id}",
            "pair_count": 1,
            "code_count": 1,
        }
        for article_id, code in (
            (101, "1"),
            (102, "2"),
            (103, "3"),
            (104, "12.1"),
        )
    ]
    descriptors = smoke._slang_cases() + smoke.select_dynamic_cases(candidates)
    by_query = {case["query"]: case for case in descriptors}
    fixed_tops = {
        "homicide": {"title": "Убийство сотрудника полиции"},
        "residential_theft": {"title": "Кража"},
        "illicit_weapon_purchase": {
            "title": "Незаконное приобретение оружия"
        },
        "drug_possession": {"title": "Хранение наркотиков"},
        "assault_on_employee": {"title": "Насилие в отношении сотрудника"},
        "obscene_language": {"title": "Нецензурная брань"},
    }

    class FakeDb:
        async def connect(self):
            events.append("db_connect")

        async def close(self):
            events.append("db_close")

        @asynccontextmanager
        async def transaction(self):
            yield object()

    @asynccontextmanager
    async def fake_global_lock(*, manager):
        assert manager is fake_db
        assert state == {"global": False, "document": False, "catalog": False}
        state["global"] = True
        events.append("global_enter")
        try:
            yield
        finally:
            state["global"] = False
            events.append("global_exit")

    @asynccontextmanager
    async def fake_document_lock(_server_id, *, manager):
        assert manager is fake_db
        assert state == {"global": True, "document": False, "catalog": False}
        state["document"] = True
        events.append("document_enter")
        try:
            yield
        finally:
            state["document"] = False
            events.append("document_exit")

    @asynccontextmanager
    async def fake_catalog_lock(_server_id):
        assert state == {"global": True, "document": True, "catalog": False}
        state["catalog"] = True
        events.append("catalog_enter")
        try:
            yield
        finally:
            state["catalog"] = False
            events.append("catalog_exit")

    async def fake_read_contract(_conn, _server_id):
        assert all(state.values())
        events.append("before")
        return fingerprint, candidates

    async def fake_read_fingerprint(_conn, _server_id):
        assert all(state.values())
        events.append("after")
        return dict(fingerprint)

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _path, *, json):
            assert all(state.values())
            descriptor = by_query[json["query"]]
            if descriptor["kind"] == "slang":
                top = fixed_tops[descriptor["name"]]
            else:
                top = {
                    "id": descriptor["expected"]["id"],
                    "code": descriptor["expected"]["code"],
                    "title": descriptor["expected"]["title"],
                    "doc_title": descriptor["expected"]["doc_title"],
                }
            events.append(f"http:{descriptor['name']}")
            return FakeResponse(_full_payload(top))

    fake_db = FakeDb()
    monkeypatch.setattr(smoke, "db_manager", fake_db)
    monkeypatch.setattr(global_locks, "law_backfill_global_lock", fake_global_lock)
    monkeypatch.setattr(
        document_locks,
        "law_document_server_lock",
        fake_document_lock,
    )
    monkeypatch.setattr(
        embedding_locks,
        "catalog_server_lock",
        fake_catalog_lock,
    )
    monkeypatch.setattr(smoke, "_read_contract", fake_read_contract)
    monkeypatch.setattr(smoke, "_read_fingerprint", fake_read_fingerprint)
    monkeypatch.setattr(smoke.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(smoke, "_make_access", lambda *_args: "not-printed")

    evidence = await smoke.run_smoke(
        "las_vegas",
        run_nonce=RUN_NONCE,
    )
    assert evidence["passed"] is True
    assert evidence["score"] == 10
    assert events == [
        "db_connect",
        "global_enter",
        "document_enter",
        "catalog_enter",
        "before",
        *(f"http:{name}" for name in smoke.LAW_SMOKE_CASE_NAMES),
        "after",
        "catalog_exit",
        "document_exit",
        "global_exit",
        "db_close",
    ]
