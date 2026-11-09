# START OF FILE tests/test_enrich_with_kimi.py

"""Тесты Kimi-адаптера (scripts/enrich_with_kimi.py) v2 — поверх задеплоенного контракта.

Проверяется то, что адаптер гарантирует САМ: адаптация source-батчей к форме
контракта, неподменяемость identity, ретраи с quality-feedback, второй проход,
перепроверка существующих файлов при resume. Сам контракт (payload builder,
валидатор, промпты) здесь подменяется фейками — его качество доказано
задеплоенными серверами и своими тестами проекта.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "enrich_with_kimi", _ROOT / "scripts" / "enrich_with_kimi.py"
)
kimi = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(kimi)


def _src_article(article_id: int, text: str = "Статья. Текст нормы.") -> dict:
    return {
        "id": article_id,
        "article_hash": f"{article_id:064x}",
        "generation_input_hash": f"{article_id + 1000:064x}",
        "source": {
            "id": article_id,
            "law_document_id": 7,
            "source_order": article_id,
            "code": f"1.{article_id}",
            "article_ref": f"1.{article_id}",
            "catalog_title": "Название",
            "document": "Уголовный кодекс",
            "text": text,
            "current_penalty_summary": None,
            "current_penalty": None,
            "current_auto_synonyms": ["старый синоним"],
        },
        "review": {"status": "pending", "required_output_fields": []},
    }


def _write_source_batch(path: Path, server_id: str, ids: list[int]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "v1",
                "server_id": server_id,
                "source_fingerprint": {"value": "f" * 64},
                "batch_number": 1,
                "batch_hash": "h" * 64,
                "articles": [_src_article(i) for i in ids],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# --- разбор ответа модели -------------------------------------------------


def test_extract_json_from_fenced_and_verbose_output():
    raw = 'Рассуждаю...\n```json\n[{"id": 1}]\n```\nГотово.'
    assert kimi._extract_json_array(raw) == [{"id": 1}]


def test_extract_survives_bracket_in_reasoning_before_fence():
    """P1-фикс: «[17.2]» в рассуждении до ответа ломал жадный regex."""
    raw = 'Статья [17.2] подходит...\n```json\n[{"id": 5, "synonyms": []}]\n```'
    assert kimi._extract_json_array(raw) == [{"id": 5, "synonyms": []}]


def test_extract_prefers_last_object_array_in_plain_reasoning():
    """Без ограждений: пример массива в рассуждении, финальный ответ — после."""
    raw = ('Черновик: [{"id": 1, "synonyms": ["старый"]}] — нет, исправлю.\n'
           'Итог: [{"id": 1, "synonyms": ["новый"]}]')
    assert kimi._extract_json_array(raw) == [{"id": 1, "synonyms": ["новый"]}]


def test_extract_raises_retriable_error_not_valueerror():
    """P1-фикс: сырой JSONDecodeError пробивал retry-цикл."""
    with pytest.raises(kimi.KimiEnrichError):
        kimi._extract_json_array('Смотри: [1, 2, оборвалось')


def test_extract_rejects_garbage():
    with pytest.raises(kimi.KimiEnrichError):
        kimi._extract_json_array("никакого джейсона")


def test_ask_retries_on_mid_stream_connection_death(monkeypatch):
    """P1-фикс: обрыв соединения после начала стрима — ретраябельный шум."""
    import http.client as hc
    rows = [kimi._adapt_source_article(_src_article(1))]
    attempts = {"n": 0}

    def flaky_post(**kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionResetError("peer reset")
        if attempts["n"] == 2:
            raise hc.IncompleteRead(b"partial")
        return json.dumps([_ok_item(1)], ensure_ascii=False)

    monkeypatch.setattr(kimi, "_post_chat", flaky_post)
    monkeypatch.setattr(kimi.time, "sleep", lambda s: None)
    out = kimi._generate_chunk_sync(
        contract=_contract_stub(), server_id="srv", chunk_rows=rows,
        all_rows=rows, args=_args(no_review=True), api_key="k",
    )
    assert out == [_ok_item(1)]
    assert attempts["n"] == 3


# --- адаптация source → форма контракта -----------------------------------


def test_adapt_source_article_maps_contract_fields():
    row = kimi._adapt_source_article(_src_article(5, text="Полный текст"))
    assert row["id"] == 5
    assert row["full_text"] == "Полный текст"
    assert row["title"] == "Название"          # catalog_title → title
    assert row["doc_title"] == "Уголовный кодекс"  # document → doc_title
    assert row["law_document_id"] == 7
    assert row["current_auto_synonyms"] == ["старый синоним"]
    assert row["article_hash"] == f"{5:064x}"


def test_load_server_rows_rejects_mixed_servers(tmp_path):
    _write_source_batch(tmp_path / "source-batch-00001.json", "alpha", [1])
    _write_source_batch(tmp_path / "source-batch-00002.json", "beta", [2])
    with pytest.raises(kimi.KimiEnrichError, match="differs"):
        kimi._load_server_rows(tmp_path)


def test_load_server_rows_aggregates_all_articles(tmp_path):
    _write_source_batch(tmp_path / "source-batch-00001.json", "alpha", [1, 2])
    _write_source_batch(tmp_path / "source-batch-00002.json", "alpha", [3])
    server_id, all_rows, by_file = kimi._load_server_rows(tmp_path)
    assert server_id == "alpha"
    assert [r["id"] for r in all_rows] == [1, 2, 3]
    assert set(by_file) == {"source-batch-00001.json", "source-batch-00002.json"}


# --- identity: модель не поставляет и не может подменить -------------------


def test_model_response_reaches_gate_verbatim(monkeypatch):
    """P0-фикс: ответ модели уходит в гейт БЕЗ предобработки — пропущенное поле
    должно дойти до валидатора как пропущенное (schema violation → ретрай),
    а не превратиться молча в null."""
    rows = [kimi._adapt_source_article(_src_article(1))]
    incomplete = [{"id": 1, "synonyms": ["ф1"]}]  # нет title/penalty*
    monkeypatch.setattr(
        kimi, "_post_chat", lambda **kw: json.dumps(incomplete, ensure_ascii=False)
    )
    seen = []

    def spy_validate(items, batch):
        seen.append(items)
        return items

    kimi._generate_chunk_sync(
        contract=_contract_stub(spy_validate), server_id="srv",
        chunk_rows=rows, all_rows=rows, args=_args(no_review=True), api_key="k",
    )
    # Валидатор увидел ровно то, что вернула модель — без добавленных null-полей.
    assert seen[0] == incomplete
    assert "title" not in seen[0][0]


def test_contract_projection_fails_on_missing_model_field():
    """Перепроверка артефакта: отсутствие обязательного поля — malformed, не null."""
    with pytest.raises(KeyError):
        kimi._contract_projection({"id": 1, "synonyms": []})


def test_bind_identity_takes_identity_from_source():
    rows = [kimi._adapt_source_article(_src_article(9))]
    validated = {9: {"id": 9, "title": "X", "penalty_summary": None,
                     "penalty": None, "synonyms": ["ф"]}}
    bound = kimi._bind_identity(rows, validated)[0]
    assert bound["article_hash"] == f"{9:064x}"
    assert bound["generation_input_hash"] == f"{9 + 1000:064x}"
    assert bound["title"] == "X"


# --- resume: файл перепроверяется, а не принимается по имени ---------------


def _contract_stub(validate=lambda items, batch: items, salvage=None):
    def default_salvage(
        items,
        batch,
        *,
        server_id,
        stage,
        exact_plan=None,
        stats=None,
        allow_article_drop=True,
    ):
        # Заглушка держит главный инвариант правки: последнее слово всегда за
        # тем же строгим гейтом, спасать поштучно тут нечего.
        return validate(items, batch), list(batch), {"applied": False}

    salvage = default_salvage if salvage is None else salvage

    return {
        "quality_attempts": 2,
        "prompt": "{project_context}|{quality_feedback}|{articles_json}",
        "review_prompt": "{project_context}|{quality_feedback}|{articles_json}|{candidate_json}",
        "build_payload": lambda batch, all_rows: [{"id": r["id"]} for r in batch],
        "validate": validate,
        "salvage": salvage,
        "project_context": lambda server_id: f"srv={server_id}",
    }


def _reviewed_file(tmp_path: Path, rows, mutate=None) -> Path:
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "article_hash": r["article_hash"],
            "generation_input_hash": r["generation_input_hash"],
            "title": "T", "penalty_summary": None, "penalty": None,
            "synonyms": ["ф1", "ф2"],
        })
    if mutate:
        mutate(items)
    p = tmp_path / "reviewed-batch-00001.json"
    p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return p


def test_revalidate_accepts_current_valid_file(tmp_path):
    rows = [kimi._adapt_source_article(_src_article(1))]
    p = _reviewed_file(tmp_path, rows)
    assert kimi._revalidate_existing(p, rows, _contract_stub()) is True


def test_revalidate_rejects_stale_hash(tmp_path):
    rows = [kimi._adapt_source_article(_src_article(1))]
    p = _reviewed_file(
        tmp_path, rows,
        mutate=lambda items: items[0].update({"article_hash": "0" * 64}),
    )
    assert kimi._revalidate_existing(p, rows, _contract_stub()) is False


def test_revalidate_rejects_coverage_mismatch(tmp_path):
    rows = [kimi._adapt_source_article(_src_article(i)) for i in (1, 2)]
    p = _reviewed_file(tmp_path, rows[:1])  # файл покрывает только id=1
    assert kimi._revalidate_existing(p, rows, _contract_stub()) is False


def test_revalidate_rejects_when_gate_fails(tmp_path):
    rows = [kimi._adapt_source_article(_src_article(1))]
    p = _reviewed_file(tmp_path, rows)

    def failing_validate(items, batch):
        raise ValueError("нарушение контракта")

    assert kimi._revalidate_existing(p, rows, _contract_stub(failing_validate)) is False


def test_quarantine_moves_file(tmp_path):
    p = tmp_path / "reviewed-batch-00001.json"
    p.write_text("[]", encoding="utf-8")
    kimi._quarantine(p)
    assert not p.exists()
    moved = list((tmp_path / "quarantine").iterdir())
    assert len(moved) == 1 and moved[0].name.startswith("reviewed-batch-00001.json")


# --- цикл генерации: гейт, ретраи с feedback, второй проход ----------------


def _args(**over):
    base = dict(
        base_url="http://x", model="m", chunk=5, concurrency=1,
        attempts=3, timeout=1.0, limit=0, no_review=False,
        batches_dir=Path("."), out_dir=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _ok_item(article_id: int) -> dict:
    return {"id": article_id, "title": "T", "penalty_summary": None,
            "penalty": None, "synonyms": ["ф1"]}


def test_generate_chunk_passes_generation_and_review(monkeypatch):
    rows = [kimi._adapt_source_article(_src_article(1))]
    calls = []

    def fake_post(**kw):
        calls.append(kw["prompt"])
        return json.dumps([_ok_item(1)], ensure_ascii=False)

    monkeypatch.setattr(kimi, "_post_chat", fake_post)
    out = kimi._generate_chunk_sync(
        contract=_contract_stub(), server_id="srv", chunk_rows=rows,
        all_rows=rows, args=_args(), api_key="k",
    )
    assert out == [_ok_item(1)]
    assert len(calls) == 2  # генерация + контроль


def test_generate_chunk_retries_with_quality_feedback(monkeypatch):
    rows = [kimi._adapt_source_article(_src_article(1))]
    prompts = []

    def fake_post(**kw):
        prompts.append(kw["prompt"])
        return json.dumps([_ok_item(1)], ensure_ascii=False)

    attempts = {"n": 0}

    def flaky_validate(items, batch):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("мало синонимов")
        return items

    monkeypatch.setattr(kimi, "_post_chat", fake_post)
    out = kimi._generate_chunk_sync(
        contract=_contract_stub(flaky_validate), server_id="srv",
        chunk_rows=rows, all_rows=rows, args=_args(no_review=True), api_key="k",
    )
    assert out == [_ok_item(1)]
    # Второй запрос генерации должен нести текст ошибки как feedback.
    assert "мало синонимов" in prompts[1]


def test_generate_chunk_fails_closed_when_gate_never_passes(monkeypatch):
    rows = [kimi._adapt_source_article(_src_article(1))]
    monkeypatch.setattr(
        kimi, "_post_chat",
        lambda **kw: json.dumps([_ok_item(1)], ensure_ascii=False),
    )

    def always_fail(items, batch):
        raise ValueError("коллизия")

    with pytest.raises(kimi.KimiEnrichError, match="quality gate"):
        kimi._generate_chunk_sync(
            contract=_contract_stub(always_fail), server_id="srv",
            chunk_rows=rows, all_rows=rows, args=_args(no_review=True),
            api_key="k",
        )

def test_merged_batch_gate_triggers_full_regen(tmp_path, monkeypatch):
    """P0-фикс: коллизия МЕЖДУ чанками ловится сквозным гейтом склеенного батча
    и лечится одной перегенерацией батча целиком."""
    _write_source_batch(tmp_path / "source-batch-00001.json", "alpha", [1, 2, 3, 4])
    calls = {"chunk_sizes": [], "merged_fails": 0}

    def fake_generate(**kw):
        rows = kw["chunk_rows"]
        calls["chunk_sizes"].append(len(rows))
        return [_ok_item(int(r["id"])) for r in rows]

    def counting_validate(items, batch):
        # Первый сквозной прогон полного батча (4 статьи) имитирует
        # межчанковую коллизию; после перегенерации целиком — чисто.
        if len(batch) == 4 and calls["merged_fails"] == 0:
            calls["merged_fails"] += 1
            raise ValueError("канонические группы пересекаются между статьями")
        return items

    monkeypatch.setattr(kimi, "_deployed", lambda: _contract_stub(counting_validate))
    monkeypatch.setattr(kimi, "_generate_chunk_sync", fake_generate)
    monkeypatch.setenv("TOKENROUTER_API_KEY", "k")

    rc = asyncio.run(kimi._async_main(_args(
        batches_dir=tmp_path, out_dir=tmp_path / "out", chunk=2,
    )))
    assert rc == 0
    # Два чанка по 2 + одна перегенерация целого батча из 4.
    assert calls["chunk_sizes"] == [2, 2, 4]
    out = json.loads(
        (tmp_path / "out" / "reviewed-batch-00001.json").read_text(encoding="utf-8")
    )
    assert [i["id"] for i in out] == [1, 2, 3, 4]
    assert out[0]["article_hash"] == f"{1:064x}"  # identity из источника


def test_merged_batch_gate_salvages_per_item_instead_of_regenerating(
    tmp_path, monkeypatch,
):
    """Один негодный синоним больше не отправляет весь батч на перегенерацию.

    Порядок сохранён: сначала неизменённый гейт, и только при его отказе —
    поштучная приёмка, за которой ТОТ ЖЕ гейт выносит последнее слово.
    Статья при этом выпасть не имеет права: офлайн-артефакт обязан покрывать
    батч точно.
    """

    _write_source_batch(
        tmp_path / "source-batch-00001.json", "alpha", [1, 2, 3, 4],
    )
    calls = {"chunk_sizes": [], "salvaged": 0}

    def fake_generate(**kw):
        rows = kw["chunk_rows"]
        calls["chunk_sizes"].append(len(rows))
        return [_ok_item(int(r["id"])) for r in rows]

    def merged_fails(items, batch):
        if len(batch) == 4:
            raise ValueError("источник не подтверждает синоним 3 у статьи 2")
        return items

    def salvage(
        items, batch, *, server_id, stage, exact_plan=None, stats=None,
        allow_article_drop=True,
    ):
        assert allow_article_drop is False, (
            "офлайн-артефакт обязан покрывать батч точно"
        )
        assert stage == "offline-merged"
        calls["salvaged"] += 1
        trimmed = [
            {**item, "synonyms": list(item["synonyms"])[:-1]}
            for item in items
        ]
        return trimmed, list(batch), {
            "applied": True, "synonyms_dropped": len(trimmed),
            "articles_dropped": 0,
        }

    monkeypatch.setattr(
        kimi, "_deployed",
        lambda: _contract_stub(merged_fails, salvage=salvage),
    )
    monkeypatch.setattr(kimi, "_generate_chunk_sync", fake_generate)
    monkeypatch.setenv("TOKENROUTER_API_KEY", "k")

    rc = asyncio.run(kimi._async_main(_args(
        batches_dir=tmp_path, out_dir=tmp_path / "out", chunk=2,
    )))

    assert rc == 0
    assert calls["salvaged"] == 1
    # Перегенерации целого батча НЕ было: только два исходных чанка.
    assert calls["chunk_sizes"] == [2, 2]
    out = json.loads(
        (tmp_path / "out" / "reviewed-batch-00001.json").read_text(
            encoding="utf-8")
    )
    assert [i["id"] for i in out] == [1, 2, 3, 4]


# END OF FILE tests/test_enrich_with_kimi.py
