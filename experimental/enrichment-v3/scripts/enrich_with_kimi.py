#!/usr/bin/env python3
"""Kimi K3 adapter over the DEPLOYED enrichment contract.

The first version of this script copied only the prompt and invented its own
payload and its own shallow checks.  The oversight audit
(``docs/reviews/LAW_ENRICHMENT_OVERSIGHT_20260803.md``) correctly rejected that:
prompt, context projection and validator are ONE contract.  This version reuses
the deployed contract pieces verbatim — the same ones the four accepted servers
(las_vegas/new_york/detroit/chicago) went through:

* ``_build_auto_enrichment_payload``  — payload with same-document
  ``contrast_context`` and current synonyms (fixes finding F-03);
* ``_ENRICH_PROMPT`` / ``_ENRICH_REVIEW_PROMPT`` — generation + independent
  review pass (fixes F-05);
* ``_validate_auto_enrichment_response`` — the full per-batch quality gate:
  exact field contract, lengths, 12-15 floors, canonical uniqueness,
  batch-level collisions and source-owned tombstones (fixes F-06/F-08);
* readiness is defined by validation, and resume revalidates existing output
  instead of trusting the file name (fixes F-10/F-11).

The adapter itself only supplies transport (OpenAI-compatible streaming call to
TokenRouter) and identity binding: ``id``/``article_hash``/
``generation_input_hash`` are copied from the immutable source batch and the
model never sees them.

Kimi K3 operational profile (measured): free pool ~9-10 tok/s, ceiling of 5-7
concurrent requests, ~25% of connections die silently.  Hence streaming with an
IDLE timeout, one global chunk queue behind a single semaphore, and retries.

The script writes ``reviewed-batch-*.json`` files only.  Database writes go
through the separate deployed ``validate_law_enrichment_batch.py`` →
``_apply_claude_enrich.py`` path and are NOT performed here.

Usage inside the production container::

    TOKENROUTER_API_KEY=... python -m scripts.enrich_with_kimi \\
        --batches-dir /app/data/kimi-enrichment/<server>-src \\
        --out-dir /app/data/kimi-enrichment/<server>-out
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_BASE_URL = "https://api.tokenrouter.com/v1"
DEFAULT_MODEL = "moonshotai/kimi-k3-free"

_IDENTITY_FIELDS = ("id", "article_hash", "generation_input_hash")
_MODEL_FIELDS = ("title", "penalty_summary", "penalty", "synonyms")


class KimiEnrichError(RuntimeError):
    """Fatal generation error for one chunk/batch."""


def _log(message: str) -> None:
    print(f"    {time.strftime('%H:%M:%S')} {message}", flush=True)


# --------------------------------------------------------------------------
# Deployed contract access (lazy: lives inside the application image)
# --------------------------------------------------------------------------


def _deployed():
    """Import the deployed shared contract pieces.

    One accessor so tests can monkeypatch the whole contract at once and the
    import error message names every required symbol.
    """

    try:
        from services.law_catalog_service import (
            _AUTO_ENRICH_QUALITY_ATTEMPTS,
            _ENRICH_PROMPT,
            _ENRICH_REVIEW_PROMPT,
            _build_auto_enrichment_payload,
            _salvage_auto_enrichment_response,
            _validate_auto_enrichment_response,
            project_context_for_server,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        raise KimiEnrichError(
            "cannot import the deployed enrichment contract from "
            f"services.law_catalog_service (run inside the app image): {exc}"
        ) from exc
    return {
        "quality_attempts": int(_AUTO_ENRICH_QUALITY_ATTEMPTS),
        "prompt": _ENRICH_PROMPT,
        "review_prompt": _ENRICH_REVIEW_PROMPT,
        "build_payload": _build_auto_enrichment_payload,
        "validate": _validate_auto_enrichment_response,
        "salvage": _salvage_auto_enrichment_response,
        "project_context": project_context_for_server,
    }


# --------------------------------------------------------------------------
# Source batches → contract-shaped article rows
# --------------------------------------------------------------------------


def _adapt_source_article(article: Mapping[str, Any]) -> dict[str, Any]:
    """Map the immutable export projection onto the field names the deployed
    payload builder and validator expect (DB-row shape)."""

    source = article["source"]
    return {
        "id": int(source["id"]),
        "law_document_id": source.get("law_document_id"),
        "source_order": source.get("source_order"),
        "code": source.get("code"),
        "article_ref": source.get("article_ref"),
        "title": source.get("catalog_title"),
        "doc_title": source.get("document"),
        "full_text": source.get("text") or "",
        "penalty_summary": source.get("current_penalty_summary"),
        "penalty_json": source.get("current_penalty"),
        "current_auto_synonyms": list(source.get("current_auto_synonyms") or ()),
        # identity, carried through to the reviewed artifact
        "article_hash": str(article["article_hash"]),
        "generation_input_hash": str(article["generation_input_hash"]),
    }


def _load_server_rows(
    batches_dir: Path,
) -> tuple[str, list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Read every source batch once: returns (server_id, all_rows,
    rows_by_source_file).  ``all_rows`` feeds neighbor context."""

    rows_by_file: dict[str, list[dict[str, Any]]] = {}
    all_rows: list[dict[str, Any]] = []
    server_id: Optional[str] = None
    for path in sorted(batches_dir.glob("source-batch-*.json")):
        batch = json.loads(path.read_text(encoding="utf-8"))
        if server_id is None:
            server_id = str(batch["server_id"])
        elif server_id != str(batch["server_id"]):
            raise KimiEnrichError(
                f"{path.name}: server_id {batch['server_id']!r} differs "
                f"from {server_id!r} in the same directory"
            )
        adapted = [_adapt_source_article(a) for a in batch["articles"]]
        rows_by_file[path.name] = adapted
        all_rows.extend(adapted)
    if server_id is None:
        raise KimiEnrichError(f"no source-batch-*.json under {batches_dir}")
    return server_id, all_rows, rows_by_file


# --------------------------------------------------------------------------
# Transport (streaming with IDLE timeout)
# --------------------------------------------------------------------------


def _post_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float,
    first_byte_timeout: float = 420.0,
    max_tokens: int = 32_000,
) -> str:
    """Stream the completion with a TWO-PHASE idle timeout.

    Measured on the free pool: TTFT on small prompts already wandered up to
    120 s+, and the contract-context prompts are 13-28K chars, so the first
    byte routinely arrives later than a single short idle timeout — one 60 s
    knob killed every large request before the stream even started.  Hence:
    generous ``first_byte_timeout`` while waiting for the stream to begin, then
    the strict ``timeout`` between chunks so dead connections (~25% of free-tier
    requests) still fail fast once a response should be flowing.

    The post-connect switch uses the CPython socket attribute when reachable
    and silently keeps the generous timeout otherwise — correctness first.
    """

    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты возвращаешь ТОЛЬКО валидный JSON-массив без "
                        "пояснений, без markdown-ограждений и без текста "
                        "вокруг."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            # У K3 max_tokens ограничивает reasoning+content ВМЕСТЕ, а дефолт
            # релея неизвестен. Без явного лимита на больших промптах весь бюджет
            # может уйти в размышления → «model returned empty content». Доки K3
            # рекомендуют >=16000 для многошаговых задач.
            "max_tokens": max_tokens,
            "stream": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    def _tighten_timeout(response: Any) -> None:
        """После первого байта сузить сокет-таймаут до строгого idle."""
        try:
            sock = response.fp.raw._sock  # CPython http.client internals
            sock.settimeout(timeout)
        except Exception:
            pass  # не дотянулись — остаёмся на щедром таймауте, это безопасно

    parts: list[str] = []
    first_line_seen = False
    with urllib.request.urlopen(request, timeout=first_byte_timeout) as response:
        for raw_line in response:
            if not first_line_seen:
                first_line_seen = True
                _tighten_timeout(response)
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                chunk = delta.get("content")
                if isinstance(chunk, str):
                    parts.append(chunk)
    content = "".join(parts)
    if not content.strip():
        raise KimiEnrichError("model returned empty content")
    return content


_FENCED_BLOCK_RE = re.compile(r"```(?:[a-zA-Z]*)\s*(.*?)```", re.DOTALL)
_MAX_BRACKET_SCANS = 2000


def _extract_json_array(text: str) -> list[Any]:
    """Pull the JSON array out of verbose output (K3 always reasons).

    Strategy, most-specific first: whole text → fenced blocks (последний
    предпочтительнее: ответ идёт после рассуждений) → ``raw_decode`` от каждой
    ``[`` (жадный regex здесь опасен: «[17.2]» в рассуждении до ответа делал
    захват от первой скобки до последней и ломал парсинг).  Любая ошибка
    разбора — ретраябельный ``KimiEnrichError``, а не сырой ``ValueError``,
    который пробил бы retry-цикл.
    """

    def _try(value: str) -> Optional[list[Any]]:
        try:
            parsed = json.loads(value.strip())
        except (json.JSONDecodeError, RecursionError):
            return None
        return parsed if isinstance(parsed, list) else None

    parsed = _try(text)
    if parsed is not None:
        return parsed

    for block in reversed(_FENCED_BLOCK_RE.findall(text)):
        parsed = _try(block)
        if parsed is not None:
            return parsed

    decoder = json.JSONDecoder()
    best: Optional[list[Any]] = None
    for scan, match in enumerate(re.finditer(r"\[", text)):
        if scan >= _MAX_BRACKET_SCANS:
            break
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(value, list) and any(
            isinstance(item, dict) for item in value
        ):
            best = value  # последний валидный список объектов = финальный ответ
    if best is not None:
        return best
    raise KimiEnrichError("no JSON array found in model output")


# --------------------------------------------------------------------------
# Generation: deployed prompt → Kimi → deployed validator (+ review pass)
# --------------------------------------------------------------------------


def _contract_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    """Strict projection of a REVIEWED-artifact item onto the response contract.

    Only for revalidating our own artifact files (identity fields + model
    fields).  Uses indexing, not ``.get``: a missing model field means the
    artifact is malformed and must fail revalidation, not become ``null``.

    Model responses are deliberately NOT projected: they go to the deployed
    validator verbatim, so a missing/extra field is a schema violation that
    triggers the feedback retry — exactly like the deployed Gemini loop.  The
    first version quietly turned missing keys into ``null`` here, which let a
    schema-drifted answer wipe a real penalty (review finding P0).
    """

    return {field: item[field] for field in ("id",) + _MODEL_FIELDS}


def _generate_chunk_sync(
    *,
    contract: Mapping[str, Any],
    server_id: str,
    chunk_rows: Sequence[Mapping[str, Any]],
    all_rows: Sequence[Mapping[str, Any]],
    args: argparse.Namespace,
    api_key: str,
) -> list[dict[str, Any]]:
    """One chunk through the full deployed contract.

    Mirrors the deployed auto-enrich loop: generation with quality-feedback
    retries, then an independent review pass, both judged by
    ``_validate_auto_enrichment_response``.
    """

    ids = [row["id"] for row in chunk_rows]
    label = f"чанк {ids[0]}..{ids[-1]} ({len(ids)} ст.)"
    payload = json.dumps(
        contract["build_payload"](chunk_rows, all_rows), ensure_ascii=False
    )
    project_context = contract["project_context"](server_id)

    def _ask(prompt: str, stage: str) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(1, args.attempts + 1):
            started = time.monotonic()
            try:
                _log(f"{label} {stage} попытка {attempt}: запрос ({len(prompt)} симв.)")
                raw = _post_chat(
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.model,
                    prompt=prompt,
                    timeout=args.timeout,
                    first_byte_timeout=getattr(
                        args, "first_byte_timeout", 420.0
                    ),
                )
                _log(
                    f"{label} {stage} ответ за "
                    f"{time.monotonic() - started:.0f}с ({len(raw)} симв.)"
                )
                return _extract_json_array(raw)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (408, 409, 425, 429, 500, 502, 503, 504):
                    time.sleep(min(60.0, 2.0 ** attempt))
                    continue
                raise KimiEnrichError(
                    f"HTTP {exc.code}: {exc.read()[:300]!r}"
                ) from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                KimiEnrichError,
                # Смерть соединения ПОСЛЕ начала стрима urllib не оборачивает в
                # URLError: из итерации тела летят сырые ConnectionResetError /
                # ssl.SSLError (обе — OSError) и http.client.IncompleteRead
                # (HTTPException). На бесплатном тире ~25% соединений умирают —
                # это ретраябельный шум, а не фатальный сбой батча.
                OSError,
                http.client.HTTPException,
            ) as exc:
                last_error = exc
                _log(f"{label} {stage} отклонён ({type(exc).__name__}: {exc}), пауза")
                time.sleep(min(30.0, 2.0 ** attempt))
                continue
        raise KimiEnrichError(
            f"{stage} failed after {args.attempts} attempts: {last_error}"
        )

    # --- generation with quality feedback, judged by the deployed gate ---
    quality_feedback = ""
    items: Optional[list[dict[str, Any]]] = None
    for quality_attempt in range(contract["quality_attempts"]):
        prompt = contract["prompt"].format(
            project_context=project_context,
            quality_feedback=quality_feedback,
            articles_json=payload,
        )
        # Ответ модели уходит в гейт БЕЗ предобработки: пропущенное или лишнее
        # поле — нарушение схемы, которое обязано пойти в feedback-ретрай, как в
        # задеплоенном цикле, а не превратиться молча в null (находка P0 ревью).
        raw_items = _ask(prompt, "генерация")
        if quality_attempt + 1 >= contract["quality_attempts"]:
            # Попытки исчерпаны: принимаем ПОШТУЧНО, а не теряем весь чанк.
            # Статьи здесь выпадать не имеют права: офлайн-артефакт обязан
            # покрывать батч точно, поэтому спасаются только синонимы.
            try:
                items, _rows, report = contract["salvage"](
                    raw_items,
                    chunk_rows,
                    server_id=server_id,
                    stage="offline-generation",
                    allow_article_drop=False,
                )
            except ValueError as exc:
                raise KimiEnrichError(
                    f"{label}: quality gate: {exc}"
                ) from exc
            if report.get("applied"):
                _log(
                    f"{label}: поштучная приёмка отбросила "
                    f"{report['synonyms_dropped']} синонимов"
                )
            break
        try:
            items = contract["validate"](raw_items, chunk_rows)
            break
        except ValueError as exc:
            _log(f"{label} отклонён гейтом качества, повтор: {exc}")
            quality_feedback = (
                "ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН ДО ЗАПИСИ. "
                f"Исправь весь пакет: {exc}\n"
            )
    assert items is not None

    if args.no_review:
        return items

    # --- independent review pass, same judge ---
    review_feedback = ""
    candidate_json = json.dumps(items, ensure_ascii=False)
    for review_attempt in range(contract["quality_attempts"]):
        review_prompt = contract["review_prompt"].format(
            project_context=project_context,
            quality_feedback=review_feedback,
            articles_json=payload,
            candidate_json=candidate_json,
        )
        raw_items = _ask(review_prompt, "контроль")
        if review_attempt + 1 >= contract["quality_attempts"]:
            try:
                reviewed, _rows, report = contract["salvage"](
                    raw_items,
                    chunk_rows,
                    server_id=server_id,
                    stage="offline-review",
                    allow_article_drop=False,
                )
            except ValueError as exc:
                raise KimiEnrichError(
                    f"{label}: review gate: {exc}"
                ) from exc
            if report.get("applied"):
                _log(
                    f"{label}: поштучная приёмка на контроле отбросила "
                    f"{report['synonyms_dropped']} синонимов"
                )
            return reviewed
        try:
            return contract["validate"](raw_items, chunk_rows)
        except ValueError as exc:
            _log(f"{label} контроль отклонён, повтор: {exc}")
            review_feedback = (
                "ПРЕДЫДУЩАЯ ПРОВЕРКА ОТКЛОНЕНА ДО ЗАПИСИ. "
                f"Исправь весь пакет: {exc}\n"
            )
    raise KimiEnrichError(f"{label}: review pass exhausted")  # pragma: no cover


def _bind_identity(
    batch_rows: Sequence[Mapping[str, Any]],
    validated: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Reviewed artifact items: identity from the immutable source, content
    from the validated model output.  The model never supplies identity."""

    out: list[dict[str, Any]] = []
    for row in batch_rows:
        item = validated[int(row["id"])]
        bound: dict[str, Any] = {
            "id": int(row["id"]),
            "article_hash": row["article_hash"],
            "generation_input_hash": row["generation_input_hash"],
        }
        for field in _MODEL_FIELDS:
            bound[field] = item.get(field)
        out.append(bound)
    return out


# --------------------------------------------------------------------------
# Resume: existing output is revalidated, never trusted by name (F-11)
# --------------------------------------------------------------------------


def _revalidate_existing(
    path: Path,
    batch_rows: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> bool:
    """True when the existing reviewed file still satisfies the full contract:
    exact ID/identity coverage against the CURRENT source batch and a clean
    pass through the deployed response validator."""

    try:
        items = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            return False
        by_id = {int(i["id"]): i for i in items if isinstance(i, dict)}
        want = {int(r["id"]): r for r in batch_rows}
        if set(by_id) != set(want) or len(items) != len(want):
            return False
        for article_id, row in want.items():
            got = by_id[article_id]
            if (
                str(got.get("article_hash")) != str(row["article_hash"])
                or str(got.get("generation_input_hash"))
                != str(row["generation_input_hash"])
            ):
                return False
        contract["validate"](
            [_contract_projection(item) for item in items], batch_rows
        )
        return True
    except Exception:
        return False


def _quarantine(path: Path) -> None:
    target_dir = path.parent / "quarantine"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / f"{path.name}.{int(time.time())}"
    path.replace(target)
    _log(f"{path.name} устарел/невалиден → {target.relative_to(path.parent)}")


# --------------------------------------------------------------------------
# Campaign over one server directory
# --------------------------------------------------------------------------


async def _async_main(args: argparse.Namespace) -> int:
    api_key = os.getenv("TOKENROUTER_API_KEY", "").strip()
    if not api_key:
        print("TOKENROUTER_API_KEY не задан", file=sys.stderr)
        return 2

    contract = _deployed()
    server_id, all_rows, rows_by_file = _load_server_rows(args.batches_dir)
    out_dir = args.out_dir or args.batches_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    source_files = sorted(rows_by_file)
    if args.limit:
        source_files = source_files[: args.limit]

    semaphore = asyncio.Semaphore(args.concurrency)

    async def run_chunk(chunk_rows: Sequence[Mapping[str, Any]]):
        async with semaphore:
            return await asyncio.to_thread(
                _generate_chunk_sync,
                contract=contract,
                server_id=server_id,
                chunk_rows=chunk_rows,
                all_rows=all_rows,
                args=args,
                api_key=api_key,
            )

    async def process_batch(name: str) -> tuple[str, str]:
        batch_rows = rows_by_file[name]
        out_path = out_dir / name.replace("source-batch-", "reviewed-batch-")
        if out_path.exists():
            if _revalidate_existing(out_path, batch_rows, contract):
                return (name, "перепроверен: валиден, пропускаю")
            _quarantine(out_path)

        chunks = [
            batch_rows[i : i + args.chunk]
            for i in range(0, len(batch_rows), args.chunk)
        ]
        results = await asyncio.gather(*(run_chunk(c) for c in chunks))
        validated: dict[int, Mapping[str, Any]] = {}
        for part in results:
            for item in part:
                validated[int(item["id"])] = item

        # Сквозной гейт по СКЛЕЕННОМУ батчу: чанковая валидация не видит
        # канонических коллизий между чанками (находка P0 ревью — contrast-радиус
        # пересекает границу чанков, и соседи в параллельных чанках могут получить
        # одинаковую группу). При провале — одна перегенерация батча ЦЕЛИКОМ, где
        # модель видит все статьи вместе, а гейт судит весь набор.
        try:
            merged = [validated[int(row["id"])] for row in batch_rows]
            # Поштучность нужна и здесь.  Сквозной гейт судит СКЛЕЕННЫЙ батч,
            # и один негодный синоним отправлял в перегенерацию весь батч
            # целиком — вторая, самая дорогая копия той же беды, что и в
            # проде.  Порядок прежний: сначала неизменённый гейт (лучший
            # случай не платит ничего), и только при отказе — приёмка, за
            # которой ТОТ ЖЕ гейт выносит последнее слово.
            #
            # ``allow_article_drop=False``: офлайн-артефакт обязан покрывать
            # батч ТОЧНО (его читают писатель, план применения и повторная
            # валидация), поэтому здесь спасаются только синонимы, а
            # выпадение статьи остаётся отказом и уходит в перегенерацию.
            merged, _rows, report = await asyncio.to_thread(
                contract["salvage"],
                merged,
                batch_rows,
                server_id=server_id,
                stage="offline-merged",
                allow_article_drop=False,
            )
            if report.get("applied"):
                _log(
                    f"{name}: сквозная поштучная приёмка отбросила "
                    f"{report['synonyms_dropped']} синонимов вместо "
                    "перегенерации всего батча"
                )
                validated = {int(item["id"]): item for item in merged}
        except (ValueError, KeyError) as exc:
            _log(
                f"{name}: сквозной гейт батча не пройден "
                f"({type(exc).__name__}: {exc}), перегенерация батча целиком"
            )
            whole = await run_chunk(batch_rows)
            validated = {int(item["id"]): item for item in whole}

        reviewed = _bind_identity(batch_rows, validated)
        tmp_path = out_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(reviewed, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp_path.replace(out_path)
        density = sum(len(i["synonyms"] or []) for i in reviewed) / max(
            1, len(reviewed)
        )
        return (name, f"ok: {len(reviewed)} статей, {density:.1f} син/статью")

    results = await asyncio.gather(
        *(process_batch(name) for name in source_files), return_exceptions=True
    )

    failures = 0
    for name, result in zip(source_files, results):
        if isinstance(result, BaseException):
            failures += 1
            print(f"[{name}] FAIL: {result}", file=sys.stderr, flush=True)
        else:
            print(f"[{result[0]}] {result[1]}", flush=True)
    print(
        f"готово: {len(source_files) - failures}/{len(source_files)} батчей "
        f"(валидация задеплоенным гейтом, сервер {server_id})",
        flush=True,
    )
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--chunk",
        type=int,
        default=5,
        help="статей на запрос (мелкие чанки снижают дрейф схемы у K3)",
    )
    # Замерено на бесплатном тире TokenRouter: проходит ~5-7 одновременных
    # запросов, сверх — 429; ~25% соединений умирают молча.
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument(
        "--timeout",
        type=float,
        default=75.0,
        help="таймаут ПРОСТОЯ потока ПОСЛЕ первого байта, сек",
    )
    parser.add_argument(
        "--first-byte-timeout",
        type=float,
        default=420.0,
        help="ожидание ПЕРВОГО байта, сек (TTFT на больших промптах доходит до минут)",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--no-review",
        action="store_true",
        help="пропустить второй проход-контролёр (только для отладки)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.chunk <= 0 or args.chunk > 25:
        print("--chunk должен быть в диапазоне 1..25", file=sys.stderr)
        return 2
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
