# START OF FILE services/law_catalog_service.py

"""
Каталог статей законки (law_articles/law_synonyms/law_article_related) для умного поиска.

Офлайн-слой: наполняется при (пере)индексации, в рантайме поиск читает готовые таблицы
БЕЗ LLM. Пайплайн на документ:

  law_documents.content → split_into_blocks (та же сегментация, что у чанкера law_chunks)
  → нормализация заголовка (kind/code/title) → reconcile строк law_articles с сохранением ID
  → постановка отсутствующих/устаревших FRIDA-векторов в устойчивую DB-очередь
  → [опц.] LLM-обогащение (структура наказания + синонимы/сленг) — только протухшие статьи
  → durable-маркер пересчёта «ещё релевантное»; API-worker строит граф после FRIDA jobs.

Существующая логическая статья обновляется на месте и сохраняет публичный ``id``.
Новые строки вставляются, реально исчезнувшие удаляются; неоднозначное совпадение
намеренно не угадывается, чтобы старое избранное не указывало на чужую норму.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

import config
from database.manager import db_manager
from services.indexing_service import (
    _BARE_HEADER_RE,
    _ISOLATED_ROMAN_FRAGMENT_RE,
    _MIXED_INLINE_CLASSIFICATION_RE,
    _header_match_plan,
    _is_bare_article_reference_continuation,
    _split_article_keyword_boundaries,
    _split_chapter_keyword_boundaries,
    _wrapped_chapter_code_boundaries,
    canonicalize_split_article_keyword_layout,
    canonicalize_split_structural_keyword_layout,
    split_into_blocks,
)
from services.law_embedding_service import (
    active_space_id,
    catalog_server_lock,
    embedding_input_hash,
    ensure_article_embedding_target,
    mark_related_rebuild,
    process_server_until_idle,
)
from services.law_document_lock import law_document_server_lock
from services.law_enrichment_competitors import select_enrichment_competitors
from services.law_enrichment_context import (
    build_source_only_enrichment_projection,
    normalize_enrichment_project_context,
)
from services.law_enrichment_exact_duplicates import (
    ExactDuplicatePlan,
    build_exact_duplicate_plan,
    extract_proven_operative_body,
    validate_exact_duplicate_group_response,
)
from services.law_enrichment_validation import (
    validate_enrichment_source_evidence,
    validate_enrichment_title_source_evidence,
)
from services.law_enrichment_artifact import normalize_penalty_json
from services.law_enrichment_output import (
    build_enrichment_output_hash,
    foreign_auto_ownership_article_ids,
)
from services.law_title_utils import is_rating_metadata
from services.law_structural_code import normalize_roman_structural_token
from services.law_synonym_service import (
    _query_reachable_canonical_terms,
    BULK_REPLACEMENT_EFFECTIVE,
    BULK_REPLACEMENT_PHYSICAL,
    ENRICHMENT_QUALITY_TARGET,
    ExactSignatureRegistry,
    MIN_REPLACEMENT_EFFECTIVE,
    MIN_REPLACEMENT_PHYSICAL,
    SynonymWriteError,
    acquire_synonym_write_lock,
    build_auto_replacement_context_hashes,
    canonical_synonym_term,
    capture_auto_replacement_generation_state,
    capture_auto_replacement_generation_state_for_targets,
    expand_synonym_terms,
    is_explicitly_empty_or_repealed_article,
    load_exact_signature_registry,
    normalize_synonym_term,
    preview_auto_synonym_replacements,
    project_context_for_server,
    replace_auto_synonyms,
    replace_auto_synonyms_bulk_in_transaction,
    validate_auto_replacement_catalog_binding,
    validate_runtime_canonical_synonym_claims,
)

logger = logging.getLogger(__name__)

TITLE_PROVENANCE_SOURCE = "source"
TITLE_PROVENANCE_GENERATED = "generated"
TITLE_PROVENANCE_LEGACY_UNKNOWN = "legacy_unknown"
_TITLE_PROVENANCE_VALUES = frozenset({
    TITLE_PROVENANCE_SOURCE,
    TITLE_PROVENANCE_GENERATED,
    TITLE_PROVENANCE_LEGACY_UNKNOWN,
})


class CatalogParseAmbiguityError(RuntimeError):
    """Raised before catalog mutation when the shared parser is ambiguous."""


class CatalogSourceSnapshotChangedError(RuntimeError):
    """Raised before catalog mutation when the source document changed."""


@dataclass(frozen=True)
class EnrichmentPendingClaim:
    """Pending markers created by one explicit enrichment invalidation."""

    server_id: str
    # id, title, content, content_hash, last_updated
    documents: Tuple[Tuple[int, Any, Any, Any, Any], ...]

    @property
    def document_ids(self) -> Tuple[int, ...]:
        return tuple(row[0] for row in self.documents)


def _assert_catalog_mutation_safe(content: str, *, doc_id: Any) -> None:
    """Fail before a transaction instead of destructively falling back."""

    plan = _header_match_plan(content)
    unresolved = plan.recovery_unresolved_bare_candidates
    if unresolved:
        raise CatalogParseAmbiguityError(
            f"doc={doc_id}: parent recovery is ambiguous; "
            f"unresolved bare N.M={len(unresolved)}"
        )
    if plan.article_dialect_status == "ambiguous":
        details = "; ".join(plan.article_dialect_reasons)
        suffix = f"; {details}" if details else ""
        raise CatalogParseAmbiguityError(
            f"doc={doc_id}: numeric article dialect is ambiguous{suffix}"
        )


_SOURCE_DOCUMENT_SNAPSHOT_FIELDS = (
    "server_id",
    "title",
    "content",
    "content_hash",
    "last_updated",
)


def _assert_locked_source_document_matches(
    doc: Mapping[str, Any],
    current: Optional[Mapping[str, Any]],
) -> None:
    """Prove a caller snapshot against the current row locked for write."""

    if current is None or int(current["id"]) != int(doc["id"]):
        raise CatalogSourceSnapshotChangedError(
            f"law document {doc.get('id')} disappeared before catalog write"
        )
    mismatched = [
        field
        for field in _SOURCE_DOCUMENT_SNAPSHOT_FIELDS
        if current.get(field) != doc.get(field)
    ]
    if mismatched:
        raise CatalogSourceSnapshotChangedError(
            f"law document {doc.get('id')} source snapshot changed: "
            f"{mismatched}"
        )

# --- Нормализация заголовка статьи ---------------------------------------------------------

# «Статья 12.1. Халатность», «Ст. 5 — О чём-то», «Глава IV. Преступления», «§ 3 Название».
# Дефисная поднумерация «Статья 10-1» (УК/УПК RMRP, как в реальном УК РФ) — продолжение номера
# только если дефис прижат к цифре («10 - Название» остаётся заголовком с номером 10).
_NORM_RE = re.compile(
    r'^\s*(статья\.?|ст\.?|глава|раздел|пункт|п\.?|§)\s*[№N]?\s*'
    r'(\d+(?:[.\-–][\d]+)*'
    r'|[IVXLCMІХСМ\u2160-\u217F]+'
    r'(?![A-Za-zА-Яа-яЁё]))\.?'
    r'\s*[-—–:.]?\s*(.*)$',
    re.IGNORECASE,
)

_KIND_MAP = {
    'статья': 'article', 'ст': 'article', 'ст.': 'article',
    'глава': 'chapter',
    'раздел': 'section',
    'пункт': 'clause', 'п': 'clause', 'п.': 'clause',
    '§': 'clause',
}

# Голый номер статьи БЕЗ ключевого слова: «1.1», «3.7», «4.3» (своды вроде УК phoenix). Уровень
# статьи — ровно два числа «N.M»; трёхуровневые «N.M.K» — части статьи, отдельными статьями НЕ идут
# (их отсекает split_into_blocks, а сюда как ref они не приходят). Требуем именно две группы цифр,
# чтобы не спутать со списком «1.» или частью «ч. 1».
_BARE_NUM_RE = re.compile(r'^(\d+\.\d+)\.?\s*(.*)$')


def _normalize_structural_code(kind: str, code: str) -> str:
    """Canonicalize proven Roman chapter/section glyph variants.

    NFKC resolves Unicode Roman numerals, while several forum sources use
    Cyrillic lookalikes.  Two audited sources also use a lowercase ASCII
    ``l`` as a visual replacement for Roman ``I`` (``l`` / ``lX``).  Uppercase
    ``L`` remains the genuine value 50.
    """

    normalized = code.replace('-', '.').replace('–', '.')
    if kind not in {'chapter', 'section'}:
        return normalized

    token = normalize_roman_structural_token(normalized)
    if token is not None:
        return token
    return normalized


def normalize_header(article_ref: Optional[str]) -> Tuple[str, Optional[str], Optional[str]]:
    """Сырой заголовок → (kind, code, title).

    kind: article|chapter|section|clause|other; code: нормализованный номер («12.1», «IV»)
    или None; title: название без номера («Халатность») или None.
    """
    ref = (article_ref or "").strip()
    if not ref:
        return 'other', None, None
    m = _NORM_RE.match(ref)
    if m:
        kw = m.group(1).lower().rstrip('.')
        kind = _KIND_MAP.get(kw) or _KIND_MAP.get(kw + '.') or 'other'
        code = m.group(2).strip().strip('.') or None
        # Канонизация дефисной поднумерации: «10-1» → «10.1» (единый формат с
        # точечными кодами; поиск нормализует запрос так же — см. normalize_query).
        if code:
            code = _normalize_structural_code(kind, code)
        title = m.group(3).strip().strip('"«»\'').strip() or None
        if title:
            title = re.sub(r'\s+', ' ', title)[:300]
        return kind, code, title
    # Голый номер без ключевого слова «Статья» (напр. УК phoenix: «1.1», «3.7» под «Глава N»).
    # Такие своды нумеруют статьи цифрами — классифицируем как article с кодом=номер.
    bm = _BARE_NUM_RE.match(ref)
    if bm:
        code = bm.group(1)
        title = (bm.group(2) or '').strip().strip('"«»\'').strip() or None
        if title:
            title = re.sub(r'\s+', ' ', title)[:300]
        return 'article', code, title
    return 'other', None, ref[:200] or None


def source_article_hash(text: str) -> str:
    """Return the canonical catalog CAS hash for exact source text."""

    return hashlib.md5(text.encode('utf-8')).hexdigest()


def _article_hash(text: str) -> str:
    """Backward-compatible alias for existing catalog callers/tests."""

    return source_article_hash(text)


_ZWSP_RE = re.compile('[​‌‍﻿]')  # zero-width мусор из форумного bbWrapper
# Структурные маркеры начала тела статьи — такая строка НЕ название
_BODY_MARKER_RE = re.compile(r'^(ч\.?\s*\d|\d+\s*[.)]|[а-яa-z]\)|[—–-]\s)', re.IGNORECASE)
_ARTICLE_KEYWORD_ONLY_LINE_RE = re.compile(r'^(?:статья\.?|ст\.)$', re.IGNORECASE)
_ARTICLE_WITH_BASE_ONLY_LINE_RE = re.compile(
    r'^(?:статья\.?|ст\.)\s*[№N]?\s*\d+(?:[.\-–]\d+)*$',
    re.IGNORECASE,
)
_WRAPPED_CODE_BASE_LINE_RE = re.compile(r'^\d+(?:[.\-–]\d+)*$')
_WRAPPED_CODE_SUFFIX_LINE_RE = re.compile(r'^\.\d+(?:\.\d+)*\.?$')
# Декор по краям строки (звёзды розыска, точки, тире) и «скобочные» маркеры юрисдикции.
# Форматы гуляют по серверам: «[Федеральная/Региональная]», «(ФЕДЕРАЛЬНЫЙ)», «★★★★ [Фед./Рег.]»,
# «*», «*.», «-» — всё это НЕ название. Критерий: после среза декора пусто или только скобочный блок.
_DECOR_EDGE_RE = re.compile(r'^[★☆*✱•·.\-–—\s]+|[★☆*✱•·.\-–—\s]+$')
_BRACKET_ONLY_RE = re.compile(r'[\[(].*[\])]')
_HAS_ALNUM_RE = re.compile(r'[0-9a-zа-яё]', re.IGNORECASE)
_NUMERIC_FRAGMENT_TITLE_RE = re.compile(r'^\.?\d+(?:\.\d+)*\.?$')
_ONE_LETTER_FRAGMENT_RE = re.compile(r'^[A-Za-zА-Яа-яЁё]$')
_LEADING_TITLE_DELIMITER_RE = re.compile(r'^\.\s+(?=\S)')
def _is_junk_title(t: str) -> bool:
    """Служебный маркер/декор вместо названия (универсально для всех форматов серверов)."""
    if is_rating_metadata(t):
        return True
    raw = (t or '').strip()
    if _NUMERIC_FRAGMENT_TITLE_RE.fullmatch(raw):
        return True
    core = _DECOR_EDGE_RE.sub('', raw)
    return (
        len(core) < 2
        or bool(_BRACKET_ONLY_RE.fullmatch(core))
        or not _HAS_ALNUM_RE.search(core)
    )


def _title_from_body(body: str) -> Optional[str]:
    """Название статьи со строки, следующей за заголовком.

    Реальные форматы форума:
      «Статья 1.\\n<Название>\\nч. 1 ...»                       — законы
      «Статья 15.6\\n[Федеральная/Региональная]\\nХалатность…»  — уголовка (маркер юрисдикции пропускаем)
    Консервативно: короткая строка, не проза и не структурный маркер;
    иначе None (короткое название потом даст LLM-обогащение)."""
    lines = body.splitlines()
    scan_start = 1
    split_chapter = next(
        (
            boundary
            for boundary in _split_chapter_keyword_boundaries(body)
            if boundary.start() == 0
        ),
        None,
    )
    if split_chapter is not None:
        # Structural chapter bodies retain the exact source bytes.  Skip the
        # keyword fragments, an optional standalone dot and up to three Roman
        # suffix fragments before looking for the human title.
        scan_start = body[:split_chapter.end()].count("\n") + 1
        skipped_roman = 0
        while (
            scan_start < len(lines)
            and skipped_roman < 3
            and _ISOLATED_ROMAN_FRAGMENT_RE.fullmatch(
                _ZWSP_RE.sub("", lines[scan_start]).strip()
            )
        ):
            scan_start += 1
            skipped_roman += 1
    wrapped_chapter = next(
        (
            boundary
            for boundary in _wrapped_chapter_code_boundaries(body)
            if boundary.start() == 0
        ),
        None,
    )
    if wrapped_chapter is not None:
        scan_start = body[:wrapped_chapter.end()].count("\n") + 1
    inline_wrapped_code = (
        bool(lines)
        and _ARTICLE_WITH_BASE_ONLY_LINE_RE.fullmatch(
            _ZWSP_RE.sub('', lines[0]).strip()
        )
        is not None
    )
    keyword_only = (
        bool(lines)
        and _ARTICLE_KEYWORD_ONLY_LINE_RE.fullmatch(
            _ZWSP_RE.sub('', lines[0]).strip()
        )
        is not None
    )
    if (
        len(lines) >= 2
        and inline_wrapped_code
        and _WRAPPED_CODE_SUFFIX_LINE_RE.fullmatch(
            _ZWSP_RE.sub('', lines[1]).strip()
        )
    ):
        # Same malformed header in two physical lines:
        # ``Статья 9`` / ``.3``. Audited sources continue immediately with
        # the provision (sometimes even a word split as ``Р`` / ``азглашать``),
        # not with a separate title. Leave it for reviewed enrichment.
        return None
    if (
        len(lines) >= 3
        and keyword_only
        and _WRAPPED_CODE_BASE_LINE_RE.fullmatch(
            _ZWSP_RE.sub('', lines[1]).strip()
        )
        and _WRAPPED_CODE_SUFFIX_LINE_RE.fullmatch(
            _ZWSP_RE.sub('', lines[2]).strip()
        )
    ):
        # Forum formatting may split ``Статья 19.1.1`` into three physical
        # lines: ``Статья`` / ``19`` / ``.1.1``. The next line in the audited
        # sources is the provision itself, not a separate heading. Returning
        # ``None`` lets reviewed enrichment provide a title instead of giving
        # a short procedural command artificial title weight.
        return None
    for line in lines[scan_start:scan_start + 5]:
        t = _ZWSP_RE.sub('', line).strip()
        if not t:
            continue
        # A number/header is sometimes followed by a delimiter on the title
        # line itself: ``Статья N`` / ``. Название``.
        t = _LEADING_TITLE_DELIMITER_RE.sub('', t)
        # In a raw multiline structural header this is the code continuation,
        # not decorative metadata to skip in search of a title. Returning
        # None avoids promoting the first short sentence of the provision.
        if _NUMERIC_FRAGMENT_TITLE_RE.fullmatch(t):
            return None
        # A lone letter followed by another prose line is a forum word-wrap
        # artifact (for example ``В`` / ``целях ...``), not decoration before
        # a genuine title.  Stop instead of promoting the continuation.
        if _ONE_LETTER_FRAGMENT_RE.fullmatch(t):
            return None
        if _MIXED_INLINE_CLASSIFICATION_RE.match(t):
            # This is jurisdiction/rating metadata plus the disposition, not a
            # concise legal title. Reviewed enrichment supplies the title.
            return None
        if _is_junk_title(t):
            continue
        if len(t) > 90 or _BODY_MARKER_RE.match(t):
            return None
        title = re.sub(r'\s+', ' ', t).strip().rstrip('.').strip()
        return title if title and not _is_junk_title(title) else None
    return None


# --- Пересборка каталога документа ----------------------------------------------------------


def _identity_text(value: Any) -> str:
    """Консервативная нормализация только для identity-сопоставления."""
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold()


def derive_catalog_fields(
    article_ref: Optional[str],
    full_text: str,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Recompute parser-owned catalog fields from the current source bytes.

    ``law_articles.title`` historically stores both parser-derived and
    enrichment-derived values.  Replaying the exact parser rule lets the
    autonomous updater distinguish them without guessing or a data migration:
    a non-null result is source-owned and immutable to Gemini; a null result
    leaves the title enrichment-owned and replaceable under the full snapshot
    CAS.
    """

    kind, code, title = normalize_header(article_ref)
    # A repeal formula is article state, not a public title.  Classify it
    # before the body-title fallback so ``Статья N\nУтратила силу`` cannot
    # create mutually exclusive "preserve this title" / "title must be null"
    # enrichment contracts.
    if kind == "article" and is_explicitly_empty_or_repealed_article(full_text):
        return kind, code, None
    if (title is None or _is_junk_title(title)) and article_ref is not None:
        title = _title_from_body(str(full_text or ""))
    return kind, code, title


def source_catalog_title(
    article_ref: Optional[str],
    full_text: str,
) -> Optional[str]:
    """Return only the parser-owned title from :func:`derive_catalog_fields`."""

    return derive_catalog_fields(article_ref, full_text)[2]


def _catalog_rows(content: str) -> List[Dict[str, Any]]:
    """Парсит документ в упорядоченные строки каталога без обращения к БД."""
    rows: List[Dict[str, Any]] = []
    for source_order, (ref, body) in enumerate(split_into_blocks(content)):
        # Keep catalog construction and later provenance checks on one exact
        # rule.  Do not duplicate or approximate this logic in a writer.
        kind, code, title = derive_catalog_fields(ref, body)
        rows.append({
            "article_ref": ref,
            "kind": kind,
            "code": code,
            "title": title,
            "full_text": body,
            "article_hash": source_article_hash(body),
            "source_order": source_order,
        })
    return rows


def _body_without_header_key(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Сильный ключ для обычного переименования/перенумерации заголовка."""
    text = str(item.get("full_text") or "")
    if item.get("article_ref") is not None:
        _header, separator, tail = text.partition("\n")
        if separator:
            text = tail
    normalized = _identity_text(text)
    # Короткие общие хвосты вроде «утратила силу» нельзя считать identity.
    if len(normalized) < 32:
        return None
    return str(item.get("kind") or "other"), source_article_hash(normalized)


def _match_existing_articles(
    new_rows: List[Dict[str, Any]],
    old_rows: List[Dict[str, Any]],
) -> Tuple[List[Optional[Dict[str, Any]]], List[int]]:
    """Сопоставляет новые блоки со старыми строками, не угадывая неоднозначность.

    В реальных документах один номер/заголовок повторяется до десятков раз, поэтому
    ни ``code``, ни ``article_ref`` не являются natural key. Полностью одинаковые
    строки попарно сопоставляются по сохранённому порядку. Более слабые признаки
    применяются только когда ключ уникален в обеих полных версиях документа.
    """
    matched: List[Optional[Dict[str, Any]]] = [None] * len(new_rows)
    used_old_ids = set()
    exact_pairs: Dict[int, int] = {}

    def groups(items: List[Dict[str, Any]], key_fn) -> Dict[Any, List[int]]:
        result: Dict[Any, List[int]] = {}
        for index, item in enumerate(items):
            key = key_fn(item)
            if key is not None:
                result.setdefault(key, []).append(index)
        return result

    def reserve(new_index: int, old_index: int, *, exact: bool = False) -> None:
        old = old_rows[old_index]
        matched[new_index] = old
        used_old_ids.add(old["id"])
        if exact:
            exact_pairs[new_index] = old_index

    def pair_exact_groups(key_fn) -> None:
        new_groups = groups(new_rows, key_fn)
        old_groups = groups(old_rows, key_fn)
        for key, new_indexes in new_groups.items():
            old_indexes = old_groups.get(key, [])
            if not old_indexes:
                continue
            new_indexes = sorted(
                new_indexes,
                key=lambda i: (int(new_rows[i].get("source_order", i)), i),
            )
            old_indexes = sorted(
                old_indexes,
                key=lambda i: (
                    int(old_rows[i].get("source_order", old_rows[i]["id"])),
                    int(old_rows[i]["id"]),
                ),
            )
            for new_index, old_index in zip(new_indexes, old_indexes):
                if matched[new_index] is None and old_rows[old_index]["id"] not in used_old_ids:
                    reserve(new_index, old_index, exact=True)

    def exact_key(item: Dict[str, Any]) -> Tuple[Any, ...]:
        return (
            item.get("article_hash"),
            _identity_text(item.get("article_ref")),
            str(item.get("kind") or "other"),
            _identity_text(item.get("code")),
        )

    def title_key(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        title = str(item.get("title") or "").strip()
        if not title or _is_junk_title(title):
            return None
        return str(item.get("kind") or "other"), _identity_text(title)

    def ref_key(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        ref = _identity_text(item.get("article_ref"))
        return (str(item.get("kind") or "other"), ref) if ref else None

    def code_key(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        code = _identity_text(item.get("code"))
        return (str(item.get("kind") or "other"), code) if code else None

    def uncoded_singleton_key(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        if item.get("code") is not None:
            return None
        return str(item.get("kind") or "other"), "uncoded-singleton"

    # Полный fingerprint безопасен и для дублей: одинаковые строки эквивалентны,
    # а source_order делает выбор ID детерминированным.
    pair_exact_groups(exact_key)

    def positional_source_key(
        item: Dict[str, Any],
    ) -> Optional[Tuple[int, str, str, str]]:
        """Strong identity for metadata-only parser corrections.

        A chapter ref/title may become canonical while the exact source bytes,
        structural kind, code and position stay unchanged.  Requiring all four
        values keeps this stronger than the later weak title/ref/code matching
        and prevents an equal tombstone/body elsewhere in the document from
        stealing the public ID.
        """

        article_hash = str(item.get("article_hash") or "").strip()
        kind = _identity_text(item.get("kind"))
        code = _identity_text(item.get("code"))
        source_order = item.get("source_order")
        if (
            not article_hash
            or not kind
            or not code
            or isinstance(source_order, bool)
            or not isinstance(source_order, int)
        ):
            return None
        return source_order, article_hash, kind, code

    pair_exact_groups(positional_source_key)

    def article_boundary_key(item: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
        """Strict identity used only inside one exact-anchor interval."""

        kind = _identity_text(item.get("kind"))
        article_ref = _identity_text(item.get("article_ref"))
        code = _identity_text(item.get("code"))
        if kind != "article" or not article_ref or not code:
            return None
        return kind, article_ref, code

    def concatenated_text(
        items: List[Dict[str, Any]],
        indexes: List[int],
    ) -> str:
        return _identity_text(
            canonicalize_split_structural_keyword_layout(
                "\n".join(
                    str(items[index].get("full_text") or "")
                    for index in indexes
                )
            )
        )

    # Исправление ложной границы меняет hash настоящей статьи: удалённый ложный
    # chapter сливается с предыдущей статьёй или, наоборот, новый настоящий chapter
    # отделяется от её хвоста. Повторяющиеся номера не позволяют матчить такую статью
    # глобально по ref/code. Разрешаем один узкий positional fallback только между
    # двумя соседними exact-якорями, которые идут в одном порядке в обеих версиях.
    #
    # Внутри такого интервала обязана быть ровно одна статья с одинаковыми
    # kind/ref/code с каждой стороны. Все остальные строки могут находиться только
    # на одной стороне, обязаны быть non-article, а нормализованная конкатенация
    # *всего* интервала должна совпасть. Так ID сохраняется только когда изменение
    # полностью объясняется одной добавленной/удалённой структурной границей, а не
    # обычной редакцией или перестановкой статей.
    exact_by_new = sorted(exact_pairs.items())
    exact_by_old = sorted(exact_pairs.items(), key=lambda pair: pair[1])
    old_anchor_rank = {
        pair: rank
        for rank, pair in enumerate(exact_by_old)
    }

    # A proven split of the word ``Статья`` used to leave the following real
    # article(s) inside the preceding article body.  Preserve the preceding
    # article ID only when the entire change is explained by the parser's exact
    # split-keyword scanner and is enclosed by two adjacent, ordered exact
    # anchors.  The newly exposed articles deliberately remain inserts.
    split_keyword_inserts: set[int] = set()
    for left_anchor, right_anchor in zip(exact_by_new, exact_by_new[1:]):
        left_new, left_old = left_anchor
        right_new, right_old = right_anchor
        if left_old >= right_old:
            continue
        if old_anchor_rank[right_anchor] != old_anchor_rank[left_anchor] + 1:
            continue

        new_gap = list(range(left_new + 1, right_new))
        old_gap = list(range(left_old + 1, right_old))
        if len(old_gap) != 1 or len(new_gap) < 2:
            continue
        if any(article_boundary_key(new_rows[index]) is None for index in new_gap):
            continue

        old_index = old_gap[0]
        old_key = article_boundary_key(old_rows[old_index])
        if old_key is None:
            continue
        candidate_indexes = [
            index
            for index in new_gap
            if article_boundary_key(new_rows[index]) == old_key
        ]
        if candidate_indexes != [new_gap[0]]:
            continue

        new_index = candidate_indexes[0]
        new_extras = new_gap[1:]
        if (
            any(matched[index] is not None for index in new_gap)
            or old_rows[old_index]["id"] in used_old_ids
        ):
            continue

        old_text = str(old_rows[old_index].get("full_text") or "")
        proven_boundaries = _split_article_keyword_boundaries(old_text)
        expected_refs = [
            _identity_text(boundary.canonical_ref)
            for boundary in proven_boundaries
        ]
        actual_refs = [
            _identity_text(new_rows[index].get("article_ref"))
            for index in new_extras
        ]
        if not expected_refs or expected_refs != actual_refs:
            continue

        old_candidate_text = _identity_text(
            canonicalize_split_article_keyword_layout(old_text)
        )
        new_candidate_text = _identity_text(
            canonicalize_split_article_keyword_layout(
                new_rows[new_index].get("full_text")
            )
        )
        if (
            not old_candidate_text
            or not new_candidate_text
            or not old_candidate_text.startswith(new_candidate_text)
        ):
            continue

        old_interval_text = _identity_text(
            canonicalize_split_article_keyword_layout(
                "\n".join(
                    str(old_rows[index].get("full_text") or "")
                    for index in old_gap
                )
            )
        )
        new_interval_text = _identity_text(
            canonicalize_split_article_keyword_layout(
                "\n".join(
                    str(new_rows[index].get("full_text") or "")
                    for index in new_gap
                )
            )
        )
        if old_interval_text != new_interval_text:
            continue

        reserve(new_index, old_index)
        split_keyword_inserts.update(new_extras)

    # A wrapped numeric citation used to be parsed as one or more additional
    # articles.  Once the shared parser proves those bare boundaries are
    # grammatical continuations, the surviving owner absorbs their exact
    # source bytes and its hash changes.  Repeated article numbers make the
    # normal ref/code keys ambiguous, so preserve the owner positionally only
    # inside two adjacent exact anchors and only when every removed article
    # boundary independently passes the same shared citation proof.
    for left_anchor, right_anchor in zip(exact_by_new, exact_by_new[1:]):
        left_new, left_old = left_anchor
        right_new, right_old = right_anchor
        if left_old >= right_old:
            continue
        if old_anchor_rank[right_anchor] != old_anchor_rank[left_anchor] + 1:
            continue

        new_gap = list(range(left_new + 1, right_new))
        old_gap = list(range(left_old + 1, right_old))
        if len(new_gap) != 1 or len(old_gap) < 2:
            continue

        new_index = new_gap[0]
        old_index = old_gap[0]
        if article_boundary_key(new_rows[new_index]) != article_boundary_key(
            old_rows[old_index]
        ):
            continue
        if (
            new_rows[new_index].get("source_order")
            != old_rows[old_index].get("source_order")
        ):
            continue
        old_extras = old_gap[1:]
        if any(
            _identity_text(old_rows[index].get("kind")) != "article"
            for index in old_extras
        ):
            continue
        if (
            matched[new_index] is not None
            or old_rows[old_index]["id"] in used_old_ids
        ):
            continue

        old_parts = [
            str(old_rows[index].get("full_text") or "")
            for index in old_gap
        ]
        reconstructed = "\n".join(old_parts)
        cursor = len(old_parts[0]) + 1
        proven = True
        for offset, extra_index in enumerate(old_extras, start=1):
            boundary = _BARE_HEADER_RE.match(reconstructed, cursor)
            if (
                boundary is None
                or boundary.start() != cursor
                or _identity_text(boundary.group(1))
                != _identity_text(
                    old_rows[extra_index].get("article_ref")
                )
                or not _is_bare_article_reference_continuation(
                    reconstructed,
                    boundary,
                )
            ):
                proven = False
                break
            cursor += len(old_parts[offset])
            if offset + 1 < len(old_parts):
                cursor += 1
        if not proven:
            continue

        new_candidate_text = _identity_text(
            new_rows[new_index].get("full_text")
        )
        old_candidate_text = _identity_text(
            old_rows[old_index].get("full_text")
        )
        if (
            not new_candidate_text
            or not old_candidate_text
            or not new_candidate_text.startswith(old_candidate_text)
            or concatenated_text(new_rows, new_gap)
            != concatenated_text(old_rows, old_gap)
        ):
            continue

        reserve(new_index, old_index)

    for left_anchor, right_anchor in zip(exact_by_new, exact_by_new[1:]):
        left_new, left_old = left_anchor
        right_new, right_old = right_anchor
        if left_old >= right_old:
            continue
        # No crossed/moved exact match may sit inside the proposed old interval.
        if old_anchor_rank[right_anchor] != old_anchor_rank[left_anchor] + 1:
            continue

        new_gap = list(range(left_new + 1, right_new))
        old_gap = list(range(left_old + 1, right_old))
        if not new_gap or not old_gap:
            continue

        new_articles = [
            index
            for index in new_gap
            if article_boundary_key(new_rows[index]) is not None
        ]
        old_articles = [
            index
            for index in old_gap
            if article_boundary_key(old_rows[index]) is not None
        ]
        if len(new_articles) != 1 or len(old_articles) != 1:
            continue

        new_index = new_articles[0]
        old_index = old_articles[0]
        if article_boundary_key(new_rows[new_index]) != article_boundary_key(
            old_rows[old_index]
        ):
            continue

        new_extras = [index for index in new_gap if index != new_index]
        old_extras = [index for index in old_gap if index != old_index]
        if bool(new_extras) == bool(old_extras):
            continue
        if any(
            _identity_text(new_rows[index].get("kind")) == "article"
            for index in new_extras
        ) or any(
            _identity_text(old_rows[index].get("kind")) == "article"
            for index in old_extras
        ):
            continue

        new_candidate_text = _identity_text(
            canonicalize_split_structural_keyword_layout(
                new_rows[new_index].get("full_text")
            )
        )
        old_candidate_text = _identity_text(
            canonicalize_split_structural_keyword_layout(
                old_rows[old_index].get("full_text")
            )
        )
        if not new_candidate_text or not old_candidate_text:
            continue
        if old_extras:
            prefix_relation = new_candidate_text.startswith(old_candidate_text)
        else:
            prefix_relation = old_candidate_text.startswith(new_candidate_text)
        if not prefix_relation:
            continue
        if concatenated_text(new_rows, new_gap) != concatenated_text(
            old_rows,
            old_gap,
        ):
            continue

        if (
            matched[new_index] is None
            and old_rows[old_index]["id"] not in used_old_ids
        ):
            reserve(new_index, old_index)

    # Даже сильный body-anchor нельзя применять жадно: при копировании текста он может
    # спорить с номером/заголовком другой статьи. Все не-exact признаки рассматриваем
    # одновременно и считаем уникальность по полным старой и новой версиям документа.
    identity_key_fns = (
        _body_without_header_key,
        title_key,
        ref_key,
        code_key,
        uncoded_singleton_key,
    )
    weak_candidates: Dict[int, set[int]] = {}
    for key_fn in identity_key_fns:
        new_groups = groups(new_rows, key_fn)
        old_groups = groups(old_rows, key_fn)
        for key, new_indexes in new_groups.items():
            old_indexes = old_groups.get(key, [])
            if len(new_indexes) != 1 or len(old_indexes) != 1:
                continue
            new_index, old_index = new_indexes[0], old_indexes[0]
            if (
                matched[new_index] is None
                and new_index not in split_keyword_inserts
            ):
                weak_candidates.setdefault(new_index, set()).add(old_index)

    # Принимаем слабое совпадение лишь когда все доступные уникальные признаки ведут
    # к одной старой строке и на эту строку не претендует другой новый блок. В любом
    # конфликте безопаснее выдать новый ID, чем молча перенаправить favorite Overlay.
    claimants: Dict[int, set[int]] = {}
    for new_index, candidates in weak_candidates.items():
        for old_index in candidates:
            claimants.setdefault(old_index, set()).add(new_index)

    unsafe_new_indexes = set()
    for new_index, candidates in weak_candidates.items():
        if len(candidates) != 1:
            unsafe_new_indexes.add(new_index)
            continue
        old_index = next(iter(candidates))
        old_id = old_rows[old_index]["id"]
        if old_id in used_old_ids or len(claimants.get(old_index, ())) != 1:
            unsafe_new_indexes.add(new_index)
            continue
        reserve(new_index, old_index)

    unused_old = [old for old in old_rows if old["id"] not in used_old_ids]
    ambiguous = set(unsafe_new_indexes)
    for index, row in enumerate(new_rows):
        if matched[index] is not None or index in split_keyword_inserts:
            continue
        if any(
            key_fn(row) is not None
            and any(key_fn(old) == key_fn(row) for old in unused_old)
            for key_fn in identity_key_fns
        ):
            ambiguous.add(index)
    return matched, sorted(ambiguous)


async def _discard_stale_inactive_vectors(
    conn,
    article_ids: List[int],
    server_id: str,
) -> None:
    """Удаляет stale-векторы неактивных spaces и инвалидирует их related-граф."""
    if not article_ids:
        return
    active = active_space_id()
    spaces = await conn.fetch(
        """
        SELECT DISTINCT affected.space_id
        FROM (
            SELECT space_id FROM law_article_embeddings
            WHERE article_id = ANY($1::int[]) AND space_id <> $2
            UNION
            SELECT space_id FROM law_embedding_jobs
            WHERE article_id = ANY($1::int[]) AND space_id <> $2
            UNION
            SELECT vector_space AS space_id FROM law_article_related
            WHERE (article_id = ANY($1::int[]) OR related_id = ANY($1::int[]))
              AND vector_space <> $2
        ) AS affected
        WHERE affected.space_id IS NOT NULL
        """,
        article_ids,
        active,
    )
    for space in spaces:
        await mark_related_rebuild(conn, server_id, space["space_id"])
    await conn.execute(
        "DELETE FROM law_embedding_jobs "
        "WHERE article_id = ANY($1::int[]) AND space_id <> $2",
        article_ids,
        active,
    )
    await conn.execute(
        "DELETE FROM law_article_embeddings "
        "WHERE article_id = ANY($1::int[]) AND space_id <> $2",
        article_ids,
        active,
    )

async def build_catalog_for_document(doc: Dict[str, Any]) -> int:
    """Self-owned one-document reconcile for standalone callers."""

    server_id = str(doc.get("server_id") or "").strip()
    if not server_id:
        raise ValueError("document server_id must be non-empty")
    content = doc.get("content") or ""
    if content:
        # Reject deterministic parser ambiguity before reserving either
        # long-lived server lock. The caller-locked core repeats this guard.
        _assert_catalog_mutation_safe(content, doc_id=doc.get("id"))
    async with law_document_server_lock(server_id, manager=db_manager):
        async with catalog_server_lock(server_id):
            return await _build_catalog_for_document_locked(doc)


async def _build_catalog_for_document_locked(doc: Dict[str, Any]) -> int:
    """Caller-locked document reconcile preserving proven stable IDs.

    doc: dict с id, server_id, title, content (как в indexing_service.index_document).
    Возвращает текущее число строк документа.

    Caller must already hold the server document lock and then catalog lock.
    The in-transaction source CAS remains mandatory defence in depth.
    """
    content = doc.get("content") or ""
    doc_id = doc["id"]
    if content:
        _assert_catalog_mutation_safe(content, doc_id=doc_id)
    rows = _catalog_rows(content)

    async with db_manager.transaction() as conn:
        # Global order is document-session lock -> catalog lock -> every
        # affected synonym advisory lock -> source/article row locks.  The
        # preliminary read is deliberately non-locking; after the locks are
        # acquired the authoritative article snapshot below must be a subset
        # or the transaction fails before mutation.
        preliminary_servers = await conn.fetch(
            """
            SELECT DISTINCT server_id
            FROM law_articles
            WHERE law_document_id = $1
            ORDER BY server_id
            """,
            doc_id,
        )
        locked_synonym_servers = {
            str(doc["server_id"])
        }
        for row in preliminary_servers:
            old_server_id = row.get("server_id")
            if not isinstance(old_server_id, str) or not old_server_id:
                raise SynonymWriteError(
                    f"doc={doc_id}: existing article server ownership "
                    "cannot be validated safely"
                )
            locked_synonym_servers.add(old_server_id)
        for affected_server_id in sorted(locked_synonym_servers):
            await acquire_synonym_write_lock(conn, affected_server_id)

        current_doc = await conn.fetchrow(
            """
            SELECT id, server_id, title, content, content_hash, last_updated
            FROM law_documents
            WHERE id = $1
              AND server_id IS NOT DISTINCT FROM $2
              AND title IS NOT DISTINCT FROM $3
              AND content IS NOT DISTINCT FROM $4
              AND content_hash IS NOT DISTINCT FROM $5
              AND last_updated IS NOT DISTINCT FROM $6
            FOR UPDATE
            """,
            doc_id,
            doc.get("server_id"),
            doc.get("title"),
            doc.get("content"),
            doc.get("content_hash"),
            doc.get("last_updated"),
        )
        _assert_locked_source_document_matches(
            doc,
            dict(current_doc) if current_doc is not None else None,
        )
        old_article_rows = [dict(r) for r in await conn.fetch(
            """
            SELECT a.id, a.law_document_id, a.server_id, a.source_order,
                   a.article_hash, a.article_ref, a.kind, a.code, a.title,
                   a.doc_title, a.full_text, a.penalty_summary,
                   a.penalty_json, a.enrich_hash, a.title_provenance,
                   a.enrichment_context_hash, a.enrichment_output_hash,
                   EXISTS (
                       SELECT 1
                       FROM law_synonyms s
                       WHERE s.article_id = a.id
                         AND s.source = 'auto'
                         AND s.status = 'approved'
                   ) AS has_approved_auto_synonyms
            FROM law_articles a
            WHERE a.law_document_id = $1
            ORDER BY a.source_order, a.id
            FOR UPDATE
            """,
            doc_id,
        )]
        actual_article_servers = {
            str(row.get("server_id") or "")
            for row in old_article_rows
        }
        if (
            "" in actual_article_servers
            or not actual_article_servers.issubset(locked_synonym_servers)
        ):
            raise SynonymWriteError(
                f"doc={doc_id}: article server ownership changed while "
                "acquiring synonym locks"
            )
        foreign_article_servers = sorted(
            actual_article_servers - {str(doc["server_id"])}
        )
        if foreign_article_servers:
            raise SynonymWriteError(
                f"doc={doc_id}: cross-server catalog migration requires an "
                "explicit multi-server protocol; old article servers="
                f"{foreign_article_servers}"
            )
        # Смена server_id документа не считается той же публичной статьёй: Overlay
        # хранит server рядом с favorite.  Legacy cross-server rows are now
        # rejected above: deleting them under only the new server's catalog
        # lock could race the old server's FRIDA/related writers.
        eligible_old = [r for r in old_article_rows if r.get("server_id") == doc["server_id"]]
        matched_old, ambiguous = _match_existing_articles(rows, eligible_old)
        matched_ids = {old["id"] for old in matched_old if old is not None}
        if matched_ids:
            retained_auto_rows = await conn.fetch(
                """
                SELECT s.id, s.server_id, s.article_id, s.term, s.weight,
                       s.source, s.status, s.suggested_by,
                       a.server_id AS owning_server_id
                FROM law_synonyms s
                JOIN law_articles a ON a.id = s.article_id
                WHERE a.id = ANY($1::int[]) AND s.source = 'auto'
                ORDER BY a.server_id, s.article_id, s.id
                FOR UPDATE OF s
                """,
                sorted(matched_ids),
            )
            retained_auto_by_owner: Dict[
                str, List[Mapping[str, Any]]
            ] = {}
            for auto_row in retained_auto_rows:
                owner_server_id = auto_row.get("owning_server_id")
                if (
                    not isinstance(owner_server_id, str)
                    or not owner_server_id
                ):
                    raise SynonymWriteError(
                        f"doc={doc_id}: retained auto synonym owner cannot "
                        "be validated safely"
                    )
                retained_auto_by_owner.setdefault(
                    owner_server_id,
                    [],
                ).append(dict(auto_row))
            foreign_retained_ids: set[int] = set()
            try:
                for owner_server_id, owner_rows in (
                    retained_auto_by_owner.items()
                ):
                    foreign_retained_ids.update(
                        foreign_auto_ownership_article_ids(
                            server_id=owner_server_id,
                            auto_rows=owner_rows,
                        )
                    )
            except ValueError as exc:
                raise SynonymWriteError(
                    f"doc={doc_id}: retained auto synonym ownership cannot "
                    "be validated safely"
                ) from exc
            if foreign_retained_ids:
                raise SynonymWriteError(
                    "foreign-server auto synonym ownership for retained "
                    f"article ids: {sorted(foreign_retained_ids)}"
                )
        stale_inactive_ids: List[int] = []
        updated = 0
        inserted = 0

        for row, old_article in zip(rows, matched_old):
            source_text_unchanged = bool(
                old_article
                and old_article.get("article_hash") == row["article_hash"]
            )
            # enrich_hash появился позже самого обогащения. NULL на неизменившейся
            # статье означает допустимые legacy-данные: сохраняем их до штатного
            # reenrich. Явно другой hash относится к старому тексту и небезопасен.
            enrichment_compatible = bool(
                source_text_unchanged
                and old_article.get("enrich_hash") in (
                    None,
                    old_article.get("article_hash"),
                )
            )
            tombstone = bool(
                row["kind"] == "article"
                and is_explicitly_empty_or_repealed_article(
                    row["full_text"]
                )
            )
            tombstone_needs_cleanup = bool(
                tombstone
                and old_article
                and (
                    old_article.get("title") is not None
                    or old_article.get("title_provenance") is not None
                    or old_article.get("penalty_summary") is not None
                    or old_article.get("penalty_json") is not None
                    or old_article.get("has_approved_auto_synonyms")
                )
            )
            source_title = row["title"]
            old_title = (
                old_article["title"]
                if enrichment_compatible
                and old_article.get("title")
                and not _is_junk_title(old_article["title"])
                else None
            )
            if tombstone:
                title = None
                title_provenance = None
            elif source_title is not None:
                title = source_title
                title_provenance = TITLE_PROVENANCE_SOURCE
            else:
                title = old_title
                if title is None:
                    title_provenance = None
                else:
                    old_provenance = old_article.get("title_provenance")
                    title_provenance = (
                        old_provenance
                        if old_provenance in _TITLE_PROVENANCE_VALUES
                        else TITLE_PROVENANCE_LEGACY_UNKNOWN
                    )
            penalty_summary = (
                old_article.get("penalty_summary")
                if enrichment_compatible and not tombstone
                else None
            )
            penalty_json = (
                old_article.get("penalty_json")
                if enrichment_compatible and not tombstone
                else None
            )
            enrich_hash = (
                old_article.get("enrich_hash")
                if enrichment_compatible and not tombstone_needs_cleanup
                else None
            )
            # Preserve the hash of the context that produced the current
            # enrichment.  Recomputing it during reconcile would erase the
            # very old→new structural/group drift the autonomous updater must
            # detect.  A changed article body invalidates the stored state.
            enrichment_context_hash = (
                old_article.get("enrichment_context_hash")
                if enrichment_compatible and not tombstone_needs_cleanup
                else None
            )
            output_semantics_unchanged = bool(
                old_article
                and old_article.get("title") == title
                and old_article.get("title_provenance") == title_provenance
                and old_article.get("penalty_summary") == penalty_summary
                and old_article.get("penalty_json") == penalty_json
                and enrichment_compatible
                and not tombstone_needs_cleanup
            )
            enrichment_output_hash = (
                old_article.get("enrichment_output_hash")
                if output_semantics_unchanged
                else None
            )
            article_for_embedding = {
                "title": title,
                "penalty_summary": penalty_summary,
                "full_text": row["full_text"],
            }
            current_input_hash = (
                embedding_input_hash(article_for_embedding)
                if row["kind"] == "article"
                else None
            )
            if old_article is not None:
                source_changed = any((
                    old_article.get("article_hash") != row["article_hash"],
                    old_article.get("article_ref") != row["article_ref"],
                    old_article.get("kind") != row["kind"],
                    old_article.get("code") != row["code"],
                    old_article.get("title") != title,
                    old_article.get("doc_title") != doc.get("title"),
                    old_article.get("penalty_summary") != penalty_summary,
                    old_article.get("penalty_json") != penalty_json,
                    old_article.get("enrich_hash") != enrich_hash,
                ))
                if source_changed:
                    stale_inactive_ids.append(old_article["id"])
                    updated += 1
                if not enrichment_compatible or tombstone_needs_cleanup:
                    # Ручные owner/user варианты относятся к публичной статье и
                    # остаются на стабильном ID. Auto-данные старого текста сразу
                    # выключаются из runtime, но сохраняются как контекст для
                    # crash-safe Gemini-review до CAS replacement.
                    await conn.execute(
                        "UPDATE law_synonyms SET status = 'stale' "
                        "WHERE article_id = $1 AND source = 'auto' "
                        "AND status = 'approved'",
                        old_article["id"],
                    )
                article_id = await conn.fetchval(
                    """
                    UPDATE law_articles
                    SET server_id = $2,
                        article_ref = $3,
                        kind = $4,
                        code = $5,
                        title = $6,
                        doc_title = $7,
                        full_text = $8,
                        article_hash = $9,
                        source_order = $10,
                        penalty_summary = $11,
                        penalty_json = $12::jsonb,
                        enrich_hash = $13,
                        title_provenance = $14,
                        enrichment_context_hash = $15,
                        enrichment_output_hash = $16,
                        embedding = CASE WHEN $17::boolean THEN NULL ELSE embedding END,
                        updated_at = CASE WHEN $17::boolean THEN now() ELSE updated_at END
                    WHERE id = $18 AND law_document_id = $1
                    RETURNING id
                    """,
                    doc_id,
                    doc["server_id"],
                    row["article_ref"],
                    row["kind"],
                    row["code"],
                    title,
                    doc.get("title"),
                    row["full_text"],
                    row["article_hash"],
                    row["source_order"],
                    penalty_summary,
                    penalty_json,
                    enrich_hash,
                    title_provenance,
                    enrichment_context_hash,
                    enrichment_output_hash,
                    source_changed,
                    old_article["id"],
                )
                if article_id != old_article["id"]:
                    raise RuntimeError(
                        f"law article {old_article['id']} disappeared during reconcile"
                    )
            else:
                article_id = await conn.fetchval(
                    """
                    INSERT INTO law_articles
                        (law_document_id, server_id, source_order, article_ref, kind,
                         code, title, doc_title, full_text, article_hash,
                         penalty_summary, penalty_json, enrich_hash,
                         title_provenance, enrichment_context_hash,
                         enrichment_output_hash,
                         embedding_local, embedding_local_space,
                         embedding_local_input_hash, embedding_local_updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            NULL, NULL, NULL, $11, NULL, NULL, NULL,
                            $12, $13, NULL)
                    RETURNING id
                    """,
                    doc_id,
                    doc["server_id"],
                    row["source_order"],
                    row["article_ref"],
                    row["kind"],
                    row["code"],
                    title,
                    doc.get("title"),
                    row["full_text"],
                    row["article_hash"],
                    title_provenance,
                    active_space_id() if row["kind"] == "article" else None,
                    current_input_hash,
                )
                inserted += 1
            if row["kind"] == "article":
                await ensure_article_embedding_target(conn, article_id)

        await _discard_stale_inactive_vectors(
            conn,
            sorted(set(stale_inactive_ids)),
            doc["server_id"],
        )
        deleted_ids = [r["id"] for r in old_article_rows if r["id"] not in matched_ids]
        if deleted_ids:
            # DELETE-trigger migration 020 пометит все затронутые related spaces dirty
            # до каскадного удаления embeddings/jobs/related.
            await conn.execute(
                "DELETE FROM law_articles "
                "WHERE law_document_id = $1 AND id = ANY($2::int[])",
                doc_id,
                deleted_ids,
            )

        if ambiguous:
            logger.warning(
                "[LAW_CATALOG] doc=%s: %d неоднозначных блоков получили новые ID",
                doc_id,
                len(ambiguous),
            )
        logger.info(
            "[LAW_CATALOG] doc=%s stable reconcile: kept=%d updated=%d inserted=%d "
            "deleted=%d ambiguous=%d",
            doc_id,
            len(matched_ids),
            updated,
            inserted,
            len(deleted_ids),
            len(ambiguous),
        )
    return len(rows)


async def preview_catalog_for_document(doc: Dict[str, Any]) -> Dict[str, int]:
    """Строит read-only план stable reconcile для одного документа."""
    content = doc.get("content") or ""
    if content:
        _assert_catalog_mutation_safe(content, doc_id=doc["id"])
    rows = _catalog_rows(content)
    old_rows = [dict(r) for r in await db_manager.fetch(
        """
        SELECT id, law_document_id, server_id, source_order, article_hash,
               article_ref, kind, code, title, doc_title, full_text,
               penalty_summary, penalty_json, enrich_hash,
               title_provenance, enrichment_context_hash,
               enrichment_output_hash
        FROM law_articles
        WHERE law_document_id = $1
        ORDER BY source_order, id
        """,
        doc["id"],
    )]
    eligible_old = [r for r in old_rows if r.get("server_id") == doc["server_id"]]
    matched, ambiguous = _match_existing_articles(rows, eligible_old)
    matched_ids = {old["id"] for old in matched if old is not None}
    return {
        "articles": len(rows),
        "preserved": len(matched_ids),
        "inserted": len(rows) - len(matched_ids),
        "deleted": sum(1 for old in old_rows if old["id"] not in matched_ids),
        "ambiguous": len(ambiguous),
    }


async def embed_articles_for_server(server_id: str, force: bool = False, batch_size: int = 64,
                                    rebuild_related: bool = True) -> int:
    """Совместимая CLI-обёртка над новой DB-очередью FRIDA.

    В обычном auto-sync модель здесь не запускается: бот лишь создаёт jobs, а API
    consumer обрабатывает их. Скрипт backfill вызывает эту функцию в API-контейнере
    и дожидается, пока очередь выбранного сервера опустеет.
    """
    return await process_server_until_idle(
        server_id, force=force, batch_size=batch_size, rebuild_related=rebuild_related,
    )


# --- LLM-обогащение (офлайн): структура наказания + синонимы --------------------------------

# ПОЛ ЗАПИСИ — одно число на все ярусы, см.
# ``services.law_synonym_service.BULK_REPLACEMENT_PHYSICAL``.  Ниже него
# писать нельзя.  Ручки окружения у пола НЕТ намеренно (M-B): он выражает
# решение заказчика, а не настройку развёртывания.
_AUTO_ENRICH_MIN_PHYSICAL = BULK_REPLACEMENT_PHYSICAL
_AUTO_ENRICH_MAX_PHYSICAL = 15
_AUTO_ENRICH_MIN_EFFECTIVE = BULK_REPLACEMENT_EFFECTIVE
# ПЛАНКА КАЧЕСТВА — вторая, отдельная работа прежнего числа 12.  Нехватка до
# неё НЕ запрещает запись: она поднимает пометку на переобогащение.  Одно
# число больше не делает две работы сразу.
_AUTO_ENRICH_QUALITY_TARGET = ENRICHMENT_QUALITY_TARGET
_AUTO_ENRICH_QUALITY_ATTEMPTS = 2
_AUTO_ENRICH_NEIGHBOR_RADIUS = 2
_AUTO_ENRICH_NEIGHBOR_TEXT_LIMIT = 1_200
_AUTO_ENRICH_BATCH_TEXT_BUDGET = 120_000
_AUTO_ENRICH_PROMPT_FEEDBACK_RESERVE = 4_096
_AUTO_ENRICH_REVIEW_CANDIDATE_CHARS_PER_ARTICLE = 5_500
_AUTO_ENRICH_RESPONSE_FIELDS = {
    "id",
    "title",
    "penalty_summary",
    "penalty",
    "synonyms",
}
_AUTO_ENRICH_PENALTY_FIELDS = {
    "fine",
    "prison",
    "arrest",
    "wanted_level",
    "other",
}

_ENRICH_SOURCE_FAITHFULNESS_RULES = """
Правила доказательности каждой выходной формулировки:
- Не усиливай модальность: «может», «вправе» и «по решению» нельзя превращать
  в «обязательно», «автоматически», «всегда» или «точно произойдёт».
- Сохраняй указанные источником действие, орган/лицо, основание, условие,
  исключение, роль и область применения. Не отбрасывай различающий признак,
  если без него запрос начинает утверждать более широкое правило.
- Нейтральный поисковый запрос может не перечислять все детали, пока он ничего
  юридически не утверждает. Но если synonym утверждает квалифицированное
  последствие, набор обязан содержать хотя бы одну формулировку, одновременно
  сохраняющую его критические модальность, орган и условие.
- Возможность наказания по другому закону не является собственной санкцией
  текущей статьи.
- Префикс exact_body_group только разводит одинаковые тексты по доказанной
  структуре. Он не доказывает факты, которых нет в общем operative body.
"""

_ENRICH_PROMPT = """Ты — юридический аналитик правового помощника RP-проекта.
{project_context}

Ниже целевые статьи законов конкретного сервера. У каждой может быть contrast_context —
ближайшие статьи того же документа, enrichment_competitors — похожие нормы из всего
сервера, а exact_body_group — доказанная группа статей с побайтово одинаковым текстом.
Contrast/competitor — только контекст для сравнения: НЕ переноси из чужой статьи
название, санкцию, условия или сленг в целевую.
Для КАЖДОЙ статьи извлеки структуру санкции и формулировки, которыми игрок будет ИСКАТЬ эту статью.

Верни СТРОГО JSON-массив, ровно по одному объекту на КАЖДЫЙ входной id, без пояснений.
Нельзя пропускать id, повторять id или добавлять id, которых нет во входе:
[{{"id": <id статьи>,
   "title": "короткое название статьи (2-8 слов, суть нормы) или null",
   "penalty_summary": "краткая строка санкции для карточки (например: 'лишение свободы 5-10 лет, штраф $40000, розыск 3 зв.') или null",
   "penalty": {{"fine": "строка или null", "prison": "строка или null", "arrest": "строка или null",
               "wanted_level": "строка или null", "other": "строка или null"}} или null,
   "synonyms": ["формулировка", ...]}}]

Правила:
- title: если у статьи в тексте есть название — возьми его; если текст сразу начинается с нормы —
  сформулируй короткое название по сути («Убийство сотрудника», «Езда без мотошлема»). Не номер, не «Статья N».
- Если есть exact_body_group, обработай ВСЮ указанную группу как одно неделимое
  задание. Для участника без source_title каждый title, а для всех участников каждый
  synonym обязан начинаться РОВНО с указанного отдельно префикса:
  title — с required_title_scope_prefix в исходном source-регистре, synonym —
  с required_synonym_scope_prefix, уже приведённого к нижнему регистру хранения.
  Lower-case правило относится ко всему synonym, включая его префикс, но не меняет
  source-регистр title. После этих префиксов title и упорядоченный список synonyms
  должны быть зеркально одинаковыми у всей
  группы; penalty_summary и penalty также должны совпадать. Нельзя назначать общий
  запрос одному первому id, смешивать области или придумывать различия, которых нет
  в одинаковом operative body. Поле shared_operative_body передаётся ровно один раз
  у shared_operative_body_owner_id и является общим исходным текстом для всех member_ids.
  Непустой source_title перепиши в title ровно как дано:
  это заголовок источника, Gemini не имеет права его менять или префиксовать.
- Санкция ТОЛЬКО из текста статьи. Не выдумывай. Нет санкции в тексте → penalty_summary=null, penalty=null.
- synonyms: дай 12-15 РАЗНЫХ, узких и подтверждённых текстом формулировок, которыми игрок
  реально спросит эту норму голосом/текстом. Для процессуальных и организационных
  норм вместо выдуманного сленга давай точные перефразы конкретного действия, обязанности,
  субъекта и условия. Для пустой/отменённой нормы верни title=null, обе санкции null и synonyms=[].
  Вход содержит только source-факты. Не опирайся на прежние generated title или
  автосинонимы: заново докажи каждую выходную формулировку исходным текстом.
  Contrast/competitor — чужие нормы только для сравнения, переносить их нельзя.
  СРАВНИ статью с contrast_context, enrichment_competitors и соседними целевыми статьями. Фраза не должна одинаково
  описывать две нормы. Общий бытовой запрос отдавай только самой подходящей конкретной норме;
  для похожих норм обязательно добавляй различающий признак из текста: действие, субъект,
  потерпевший, количество/порог, место, цель, способ или условие. Не подменяй точный состав
  более общей статьёй и не размножай один сленговый запрос по соседним статьям.
  После нормализации поиска у статьи должны остаться минимум 12 самостоятельных
  canonical-групп: перестановка тех же слов и вопросная обёртка новой группой не считаются.
  ОБЯЗАТЕЛЬНО включи:
  • короткую бытовую суть нарушения («нелегальное оружие», «сбил пешехода», «не приехал на вызов»);
  • перефразы сути ДРУГИМИ корнями-синонимами ключевого термина — это САМОЕ ВАЖНОЕ
    (незаконный↔нелегальный↔запрещённый↔подпольный↔без лицензии; убийство↔убил↔замочил↔лишил жизни;
     кража↔украл↔спёр↔вынес↔хищение; оружие↔ствол↔пушка↔калаш; побои↔избил↔отпинал);
  • игровой сленг и жаргон («дм», «фрикил», «мкрж», «рескрим»);
  • типичный запрос-вопрос без слова «статья» («сколько за убийство копа», «что будет за нелегалку»).
  Вопросные обёртки сами по себе НЕ считаются разными вариантами: «что будет за X» и
  «какая статья за X» имеют одно ядро X. Различай варианты действием, объектом или
  юридически значимым условием.
  Если используешь сленг, добавь и каноническую форму, которая останется после нормализации
  запроса (например «мента избили» + «сотруднику нанесли телесные повреждения»,
  «купил ствол с рук» + «купил оружие с рук»).
  КЛЮЧЕВОЕ слово КАЖДОГО синонима — само ДЕЙСТВИЕ/НАРУШЕНИЕ. НЕ строй синоним вокруг общей роли
  (госник/сотрудник/гражданский/человек) + мелкой детали («борода у госника», «берцы госникам», «тату
  госника») — такие фразы совпадают с ЧУЖИМИ статьями по общему слову и ЛОМАЮТ поиск. Точное название
  слово-в-слово не дублируй (для него отдельный поиск по названию), но перефразируй суть РАЗНЫМИ словами.
  Каждый synonym целиком пиши только в нижнем регистре.

""" + _ENRICH_SOURCE_FAITHFULNESS_RULES + """
{quality_feedback}
ЦЕЛЕВЫЕ СТАТЬИ:
{articles_json}"""

_ENRICH_REVIEW_PROMPT = """Ты — второй, независимый контролёр качества обогащения
юридического поиска RP-проекта.
{project_context}

Ниже даны исходные целевые статьи с contrast_context, enrichment_competitors,
exact_body_group и черновой JSON-ответ другого
прохода Gemini. Лично перепроверь КАЖДЫЙ объект по исходному тексту и верни исправленный
финальный JSON-массив. Никаких пояснений.

Обязательная проверка:
- покрытие id точное: каждый входной id ровно один раз, чужих id нет;
- title и санкция относятся только к тексту самой целевой статьи; ничего не выдумано
  и ничего не перенесено из contrast_context;
- у обычной статьи 12-15 разных, узких, подтверждённых формулировок в нижнем регистре
  и минимум 12 самостоятельных canonical-групп после нормализации;
- точный бытовой/сленговый запрос закреплён за самой конкретной подходящей нормой,
  а похожие соседние нормы разведены действием, субъектом, потерпевшим, количеством,
  порогом, местом, целью, способом или условием;
- одна canonical-суть не повторяется у разных статей; общий запрос не размножен по
  соседним составам;
- exact_body_group покрыта целиком; required_title_scope_prefix и
  required_synonym_scope_prefix соблюдены буквально в своих полях,
  suffix-части title/synonyms зеркальны, санкции одинаковы, source_title не изменён;
- каждая формулировка заново доказана source; чужой contrast/competitor context
  не перенесён;
- для пустой/упразднённой статьи title=null, penalty_summary=null, penalty=null,
  synonyms=[].

""" + _ENRICH_SOURCE_FAITHFULNESS_RULES + """
Формат каждого объекта СТРОГО такой и только с этими полями:
{{"id": <integer>, "title": <string|null>, "penalty_summary": <string|null>,
 "penalty": <object|null>, "synonyms": [<string>, ...]}}

{quality_feedback}
ИСХОДНЫЕ СТАТЬИ:
{articles_json}

ЧЕРНОВОЙ ОТВЕТ:
{candidate_json}"""


def _parse_json_array(raw: str) -> List[Dict[str, Any]]:
    """Достаёт JSON-массив из ответа модели (терпит ```json-заборы и мусор вокруг)."""
    text = (raw or "").strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.MULTILINE).strip()
    start, end = text.find('['), text.rfind(']')
    if start == -1 or end <= start:
        raise ValueError("в ответе модели нет JSON-массива")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("ответ модели не массив")
    return data


def _compact_optional_llm_text(
    value: Any,
    *,
    field: str,
    maximum: int,
) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(
            f"{field} must be non-empty and at most {maximum} characters"
        )
    return normalized


def _exact_group_shared_operative_text(
    group: Any,
    articles_by_id: Mapping[int, Mapping[str, Any]],
) -> str:
    """Return one byte-proven body shared by a complete exact group."""

    bodies: List[bytes] = []
    for member_id in group.article_ids:
        article = articles_by_id.get(int(member_id))
        if article is None:
            raise ValueError(
                f"exact-body group {group.key} lacks article {member_id}"
            )
        body = extract_proven_operative_body(article)
        if body is None:
            raise ValueError(
                f"exact-body group {group.key} lost its proven body"
            )
        bodies.append(body)
    if (
        not bodies
        or any(body != bodies[0] for body in bodies[1:])
        or hashlib.sha256(bodies[0]).hexdigest() != group.body_sha256
        or len(bodies[0]) != group.body_length
    ):
        raise ValueError(
            f"exact-body group {group.key} source bytes changed"
        )
    return bodies[0].decode("utf-8")


def _build_auto_enrichment_payload(
    batch: Sequence[Mapping[str, Any]],
    all_articles: Sequence[Mapping[str, Any]],
    *,
    exact_plan: Optional[ExactDuplicatePlan] = None,
) -> List[Dict[str, Any]]:
    """Attach local, server-wide and exact-structure contrast to each target."""

    # ``all_articles`` is retained as the public parameter name for existing
    # callers, but production passes the complete captured catalog, including
    # parser-owned section/chapter rows.  Prompt and digest consume this one
    # shared source-only projection.
    projections = build_source_only_enrichment_projection(
        all_articles,
        source_title_resolver=source_catalog_title,
    )
    articles_by_id = {
        int(article["id"]): article
        for article in all_articles
        if str(article.get("kind") or "").strip().lower() == "article"
    }
    batch_ids = {int(article["id"]) for article in batch}
    if not batch_ids <= set(projections):
        raise ValueError(
            "enrichment payload targets are absent from the complete "
            f"catalog projection: {sorted(batch_ids - set(projections))}"
        )

    by_document: Dict[int, List[int]] = {}
    for article_id, projection in projections.items():
        source = projection["source"]
        by_document.setdefault(
            int(source["law_document_id"]),
            [],
        ).append(article_id)
    for article_ids in by_document.values():
        article_ids.sort(
            key=lambda article_id: (
                int(projections[article_id]["source"]["source_order"]),
                article_id,
            )
        )

    positions: Dict[int, Tuple[List[int], int]] = {}
    for article_ids in by_document.values():
        for index, article_id in enumerate(article_ids):
            positions[article_id] = (article_ids, index)

    exact_groups_by_id = {}
    if exact_plan is not None:
        for group in exact_plan.groups:
            for member in group.members:
                exact_groups_by_id[member.article_id] = group
    source_only_articles = [
        {
            "id": article_id,
            "law_document_id": projection["source"]["law_document_id"],
            "source_order": projection["source"]["source_order"],
            "kind": projection["source"]["kind"],
            "code": projection["source"]["code"],
            "article_ref": projection["source"]["article_ref"],
            "title": projection["source"]["source_title"],
            "doc_title": projection["source"]["document"],
            "full_text": projection["source"]["text"],
            "article_hash": projection["source"]["article_hash"],
        }
        for article_id, projection in sorted(projections.items())
    ]
    source_only_by_id = {
        int(article["id"]): article for article in source_only_articles
    }
    competitor_matches = select_enrichment_competitors(
        [source_only_by_id[int(article["id"])] for article in batch],
        source_only_articles,
        neighbor_radius=_AUTO_ENRICH_NEIGHBOR_RADIUS,
    )
    shared_body_by_group: Dict[str, str] = {}

    payload: List[Dict[str, Any]] = []
    for article in batch:
        article_id = int(article["id"])
        contrast: List[Dict[str, Any]] = []
        location = positions.get(article_id)
        if location is not None:
            article_ids, index = location
            start = max(0, index - _AUTO_ENRICH_NEIGHBOR_RADIUS)
            end = min(
                len(article_ids),
                index + _AUTO_ENRICH_NEIGHBOR_RADIUS + 1,
            )
            for neighbor_index in range(start, end):
                neighbor_id = article_ids[neighbor_index]
                if neighbor_id == article_id:
                    continue
                neighbor_projection = projections[neighbor_id]
                neighbor = neighbor_projection["source"]
                contrast.append({
                    "relation": (
                        "before" if neighbor_index < index else "after"
                    ),
                    "distance": abs(neighbor_index - index),
                    "id": neighbor_id,
                    "code": neighbor.get("code"),
                    "title": neighbor.get("source_title"),
                    "document": neighbor.get("document"),
                    "structural_ancestry": neighbor_projection[
                        "structural_ancestry"
                    ],
                    "text": str(neighbor.get("text") or "")[
                        :_AUTO_ENRICH_NEIGHBOR_TEXT_LIMIT
                    ],
                })

        exact_group = exact_groups_by_id.get(article_id)
        exact_group_payload = None
        exact_member_ids: set[int] = set()
        target_text = str(article.get("full_text") or "")
        if exact_group is not None:
            exact_member_ids = set(exact_group.article_ids)
            shared_body_owner = exact_group.article_ids[0]
            shared_body = shared_body_by_group.get(exact_group.key)
            if shared_body is None:
                shared_body = _exact_group_shared_operative_text(
                    exact_group,
                    articles_by_id,
                )
                shared_body_by_group[exact_group.key] = shared_body
            # The complete unit is always in one batch.  Send the byte-identical
            # operative body once instead of repeating a potentially huge text
            # for every member; each member still carries its exact source
            # header and structural scope.
            target_text = str(article.get("article_ref") or "")
            exact_group_payload = {
                "group_key": exact_group.key,
                "body_sha256": exact_group.body_sha256,
                "body_length": exact_group.body_length,
                "shared_operative_body_owner_id": shared_body_owner,
                "shared_operative_body": (
                    shared_body
                    if article_id == shared_body_owner
                    else None
                ),
                "scope_level": exact_group.scope_level,
                "member_ids": list(exact_group.article_ids),
                "members": [
                    {
                        "id": member.article_id,
                        "required_title_scope_prefix": (
                            member.title_scope_prefix
                        ),
                        "required_synonym_scope_prefix": (
                            member.synonym_scope_prefix
                        ),
                        "scope_text": member.scope_text,
                        "document": member.path.document_title,
                        "section": (
                            member.path.section.title
                            or member.path.section.article_ref
                            if member.path.section is not None
                            else None
                        ),
                        "chapter": (
                            member.path.chapter.title
                            or member.path.chapter.article_ref
                            if member.path.chapter is not None
                            else None
                        ),
                        "source_title": projections[member.article_id][
                            "source"
                        ]["source_title"],
                        "structural_ancestry": projections[
                            member.article_id
                        ]["structural_ancestry"],
                    }
                    for member in exact_group.members
                ],
            }

        competitors = []
        for match in sorted(
            competitor_matches.get(article_id, ()),
            key=lambda value: int(value.article_id),
        ):
            if match.article_id in exact_member_ids:
                continue
            competitor = articles_by_id.get(match.article_id)
            if competitor is None:
                continue
            competitor_projection = projections[match.article_id]
            competitor_source = competitor_projection["source"]
            competitors.append({
                "id": match.article_id,
                "code": competitor_source.get("code"),
                "title": competitor_source.get("source_title"),
                "document": competitor_source.get("document"),
                "structural_ancestry": competitor_projection[
                    "structural_ancestry"
                ],
                "text": str(competitor_source.get("text") or "")[
                    :_AUTO_ENRICH_NEIGHBOR_TEXT_LIMIT
                ],
            })

        target_projection = projections[article_id]
        target_source = target_projection["source"]
        source_title = target_source.get("source_title")
        payload.append({
            "id": article_id,
            "code": target_source.get("code"),
            "title": source_title,
            "source_title": source_title,
            "document": target_source.get("document"),
            "structural_ancestry": target_projection[
                "structural_ancestry"
            ],
            "text": target_text,
            "contrast_context": contrast,
            "enrichment_competitors": competitors,
            "exact_body_group": exact_group_payload,
        })
    return payload


def _auto_enrichment_prompt_size(
    payload_items: Sequence[Mapping[str, Any]],
    *,
    project_context: str,
) -> int:
    """Return a conservative exact-input size for generation and review."""

    payload_json = json.dumps(
        list(payload_items),
        ensure_ascii=False,
    )
    feedback = "x" * _AUTO_ENRICH_PROMPT_FEEDBACK_RESERVE
    generation_prompt = _ENRICH_PROMPT.format(
        project_context=project_context,
        quality_feedback=feedback,
        articles_json=payload_json,
    )
    candidate_budget = (
        2
        + len(payload_items)
        * _AUTO_ENRICH_REVIEW_CANDIDATE_CHARS_PER_ARTICLE
    )
    review_prompt = _ENRICH_REVIEW_PROMPT.format(
        project_context=project_context,
        quality_feedback=feedback,
        articles_json=payload_json,
        candidate_json="x" * candidate_budget,
    )
    return max(len(generation_prompt), len(review_prompt))


def _assert_auto_enrichment_prompt_budget(
    prompt: str,
    batch: Sequence[Mapping[str, Any]],
    *,
    stage: str,
) -> None:
    if len(prompt) > _AUTO_ENRICH_BATCH_TEXT_BUDGET:
        raise ValueError(
            f"{stage} prompt exceeds the model text budget before network: "
            f"ids={[int(article['id']) for article in batch]}, "
            f"characters={len(prompt)}, "
            f"budget={_AUTO_ENRICH_BATCH_TEXT_BUDGET}"
        )


def _partition_auto_enrichment_batches(
    articles: Sequence[Mapping[str, Any]],
    maximum_articles: int,
    *,
    exact_plan: Optional[ExactDuplicatePlan] = None,
    all_articles: Optional[Sequence[Mapping[str, Any]]] = None,
    project_context: str = "",
) -> List[List[Mapping[str, Any]]]:
    """Bound the real generation/review prompts without splitting a group."""

    maximum_articles = int(maximum_articles)
    if maximum_articles < 1:
        raise ValueError("LAW_ENRICH_BATCH_ARTICLES must be positive")
    articles_by_id = {
        int(article["id"]): article
        for article in articles
    }
    group_by_id = {}
    if exact_plan is not None:
        for group in exact_plan.groups:
            present = set(group.article_ids) & set(articles_by_id)
            if present and present != set(group.article_ids):
                raise ValueError(
                    "exact-body target closure is incomplete before batching: "
                    f"group={group.key}, present={sorted(present)}, "
                    f"required={list(group.article_ids)}"
                )
            for article_id in present:
                group_by_id[article_id] = group

    units: List[List[Mapping[str, Any]]] = []
    consumed: set[int] = set()
    for article in articles:
        article_id = int(article["id"])
        if article_id in consumed:
            continue
        group = group_by_id.get(article_id)
        if group is None:
            unit = [article]
        else:
            unit = [
                articles_by_id[member_id]
                for member_id in group.article_ids
            ]
        consumed.update(int(row["id"]) for row in unit)
        units.append(unit)

    prompt_articles = list(all_articles) if all_articles is not None else list(
        articles
    )
    complete_payload = _build_auto_enrichment_payload(
        articles,
        prompt_articles,
        exact_plan=exact_plan,
    )
    payload_by_id = {
        int(item["id"]): item
        for item in complete_payload
    }

    def prompt_size(rows: Sequence[Mapping[str, Any]]) -> int:
        return _auto_enrichment_prompt_size(
            [payload_by_id[int(row["id"])] for row in rows],
            project_context=project_context,
        )

    batches: List[List[Mapping[str, Any]]] = []
    current: List[Mapping[str, Any]] = []
    for unit in units:
        unit_size = prompt_size(unit)
        if (
            len(unit) > maximum_articles
            and group_by_id.get(int(unit[0]["id"])) is None
        ):
            raise AssertionError("one non-exact unit contains multiple articles")
        if unit_size > _AUTO_ENRICH_BATCH_TEXT_BUDGET:
            raise ValueError(
                "one indivisible enrichment unit exceeds the model text "
                f"budget before any network call: ids="
                f"{[int(article['id']) for article in unit]}, "
                f"characters={unit_size}, "
                f"budget={_AUTO_ENRICH_BATCH_TEXT_BUDGET}"
            )
        proposed = [*current, *unit]
        if current and (
            len(proposed) > maximum_articles
            or prompt_size(proposed) > _AUTO_ENRICH_BATCH_TEXT_BUDGET
        ):
            batches.append(current)
            current = []
        current.extend(unit)
    if current:
        batches.append(current)
    return batches


# --- Строгий валидатор сериализован (M-A) -----------------------------------
#
# У ``services.law_enrichment_validation`` есть МОДУЛЬНОЕ ИЗМЕНЯЕМОЕ состояние
# (memo вхождений, атомы нерешённых клауз и два флага реентрантности), и оно
# чистится на КАЖДОМ публичном входе — в самом файле написано зачем: иначе
# вердикт перестаёт быть функцией входа.
#
# Пока приёмка считалась в корутине, одного входа на процесс хватало.  M3
# отправил её в поток, а сервера синкаются по четыре
# (``knowledge_base_manager.CONCURRENT_UPDATES``), замок каталога ПОсерверный,
# и соседний вызов начал чистить память ПОСРЕДИ чужого.  Замер на настоящих
# статьях, тем же входом, каким ходит продукт: два потока — 9 сбросов памяти
# из 12 случились, пока внутри вызова был ДРУГОЙ поток; один поток — 0 из 9.
# С этим замком — 0 из 12.  Испорченный вердикт вдобавок оседал в кэше ниже.
#
# Замок процессный и берётся вокруг ОДНОГО публичного вызова валидатора, а не
# вокруг всей приёмки: держать его целыми батчами значило бы запирать второй
# сервер на минуты.  Цикл событий остаётся свободным — ровно ради чего делался
# M3, — потому что замка ждёт рабочий поток, а не корутина.
#
# Реентрантный ради вложенных захватов ОДНИМ И ТЕМ ЖЕ потоком: у одного потока
# чередования нет по определению, и запирать его на самого себя незачем.
#
# САМ ВАЛИДАТОР НЕ ТРОНУТ.
_ENRICHMENT_VALIDATOR_LOCK = threading.RLock()


# --- Кэш вердиктов строгого валидатора (M3) ---------------------------------
#
# Разбор радиуса показал, что один и тот же текст прогоняется валидатором
# ТРИЖДЫ: гейт на полном ответе, затем фильтр (много раз), затем тот же гейт
# на уцелевшем.  Замер объяснил, почему это дорого: стоимость вызова почти
# целиком — разбор ИСХОДНОГО ТЕКСТА, и она не зависит от числа синонимов
# (n=0 стоит столько же, сколько n=12).
#
# Кэш НЕ ослабляет контракт доверия ни на байт.  Ключ покрывает КАЖДЫЙ
# аргумент, который видит валидатор, поэтому сохранённый вердикт — это его
# собственный вердикт на тех же входах, а не догадка о нём.  Сам валидатор
# не тронут: он сбрасывает свои памятки на входе, значит его ответ есть
# функция аргументов, и повторный прогон вернул бы ровно то же самое.
#
# Кэшируется только ``ValueError`` — вердикт контракта.  Любое другое
# исключение это авария, и она обязана всплывать каждый раз заново.
#
# Приёмка считается в ПОТОКЕ (``asyncio.to_thread``), и два сервера могут
# считаться одновременно, поэтому у кэша есть замок.  Замок держится только
# на операциях со словарём: сам валидатор считается ВНЕ замка, иначе один
# поток запирал бы другой на десятки секунд.  Гонка без замка была бы не
# «неточной статистикой», а падением: ``move_to_end`` по ключу, который сосед
# уже вытеснил, бросает ``KeyError``.
_AUTO_ENRICH_VERDICT_CACHE: "OrderedDict[str, Optional[str]]" = OrderedDict()
_AUTO_ENRICH_VERDICT_CACHE_LOCK = threading.Lock()
_AUTO_ENRICH_VERDICT_CACHE_LIMIT = 8192
_AUTO_ENRICH_VERDICT_CACHE_STATS = {"hits": 0, "misses": 0}


def _auto_enrichment_verdict_key(*parts: Any) -> str:
    """Byte-exact digest of the complete validator input."""

    return hashlib.sha256(
        json.dumps(
            parts, ensure_ascii=False, sort_keys=True, default=repr,
        ).encode("utf-8")
    ).hexdigest()


def _auto_enrichment_cached_verdict(key: str, run) -> None:
    """Run the unchanged validator once per distinct input, then reuse it."""

    missing = _AUTO_ENRICH_VERDICT_CACHE
    with _AUTO_ENRICH_VERDICT_CACHE_LOCK:
        cached = _AUTO_ENRICH_VERDICT_CACHE.get(key, missing)
        if cached is not missing:
            _AUTO_ENRICH_VERDICT_CACHE.move_to_end(key)
            _AUTO_ENRICH_VERDICT_CACHE_STATS["hits"] += 1
        else:
            _AUTO_ENRICH_VERDICT_CACHE_STATS["misses"] += 1
    if cached is not missing:
        if cached is not None:
            raise ValueError(cached)
        return
    # Валидатор считается ВНЕ замка КЭША: он идёт десятки секунд.  Но он же
    # обязан быть один на процесс, поэтому берётся
    # ``_ENRICHMENT_VALIDATOR_LOCK`` (M-A): без него соседний поток чистит его
    # память посреди этого вызова, а испорченный вердикт тут же кэшируется.
    try:
        with _ENRICHMENT_VALIDATOR_LOCK:
            run()
    except ValueError as exc:
        _auto_enrichment_remember_verdict(key, str(exc))
        raise
    _auto_enrichment_remember_verdict(key, None)


def _auto_enrichment_remember_verdict(key: str, verdict: Optional[str]) -> None:
    with _AUTO_ENRICH_VERDICT_CACHE_LOCK:
        _AUTO_ENRICH_VERDICT_CACHE[key] = verdict
        _AUTO_ENRICH_VERDICT_CACHE.move_to_end(key)
        while (
            len(_AUTO_ENRICH_VERDICT_CACHE)
            > _AUTO_ENRICH_VERDICT_CACHE_LIMIT
        ):
            _AUTO_ENRICH_VERDICT_CACHE.popitem(last=False)


def _cached_validate_enrichment_source_evidence(
    full_text,
    penalty_summary,
    penalty,
    synonyms,
    *,
    trusted_scope_prefix=None,
) -> None:
    """``validate_enrichment_source_evidence`` behind the verdict cache."""

    key = _auto_enrichment_verdict_key(
        "source", full_text, penalty_summary, penalty,
        list(synonyms), trusted_scope_prefix,
    )
    _auto_enrichment_cached_verdict(key, lambda: (
        validate_enrichment_source_evidence(
            full_text,
            penalty_summary,
            penalty,
            synonyms,
            trusted_scope_prefix=trusted_scope_prefix,
        )
    ))


def _cached_validate_enrichment_title_source_evidence(
    full_text,
    reviewed_title,
    *,
    source_title,
    source_context=None,
    trusted_title_prefix=None,
) -> None:
    """``validate_enrichment_title_source_evidence`` behind the same cache."""

    key = _auto_enrichment_verdict_key(
        "title", full_text, reviewed_title, source_title,
        source_context, trusted_title_prefix,
    )
    _auto_enrichment_cached_verdict(key, lambda: (
        validate_enrichment_title_source_evidence(
            full_text,
            reviewed_title,
            source_title=source_title,
            source_context=source_context,
            trusted_title_prefix=trusted_title_prefix,
        )
    ))


# --- Поштучная приёмка синонимов -------------------------------------------
#
# КОНТРАКТ ДОВЕРИЯ НЕ МЕНЯЕТСЯ НИ НА БАЙТ.  Строгий гейт
# ``_validate_auto_enrichment_response`` не тронут и остаётся ПОСЛЕДНИМ словом:
# всё, что доходит до записи, проходит ровно ту же проверку, что и раньше.
# Меняется единственное — мы перестаём выбрасывать годное вместе с негодным.
#
# Почему не «проверь каждый синоним в одиночку».  Три кросс-синонимных правила
# покрытия (formula-owned ground, орган, критический квалификатор) требуют
# покрытия от НАБОРА, поэтому одиночная проба СТРОЖЕ пакетной: честные
# синонимы, которые пакет принимает в компании соседа, поодиночке отвергаются.
# Наивный фильтр по одиночной пробе их бы потерял.  Здесь применяется ЖАДНОЕ
# УДАЛЕНИЕ ПО ИНДЕКСУ ИЗ СООБЩЕНИЯ ОБ ОШИБКЕ: набор проверяется целиком тем же
# неизменённым валидатором, удаляется ровно названный им синоним, и проверка
# повторяется.  Правила покрытия при этом соблюдаются по построению.
_AUTO_ENRICH_SYNONYM_INDEX_RE = re.compile(
    r"(?:\bin\s+synonym\s+(\d+)\b)"
    r"|(?:(?:^|[\s:;,])synonym(?:\s+evidence)?\s+(\d+)\b)"
)


def _failed_synonym_index(message: str) -> Optional[int]:
    """Return the synonym position the strict validator itself named."""

    match = _AUTO_ENRICH_SYNONYM_INDEX_RE.search(str(message or ""))
    if match is None:
        return None
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _runtime_admissible_synonym_positions(
    raw_synonyms: Sequence[Any],
) -> list[int]:
    """Mirror ``validate_runtime_canonical_synonym_claims`` per article.

    Дубликаты нормализованной и канонической формы внутри статьи — тоже
    поштучная беда: удаляется повтор, а не вся статья.  Побеждает первое
    вхождение, ровно как это делает сам общий преflight.
    """

    positions: list[int] = []
    seen_surfaces: set[str] = set()
    seen_canonical: set[str] = set()
    for index, value in enumerate(raw_synonyms):
        if not isinstance(value, str):
            continue
        surface = normalize_synonym_term(value)
        canonical = canonical_synonym_term(value)
        if surface is None or canonical is None:
            continue
        if surface in seen_surfaces or canonical in seen_canonical:
            continue
        seen_surfaces.add(surface)
        seen_canonical.add(canonical)
        positions.append(index)
    return positions


def _auto_enrichment_effective_count(values: Sequence[Any]) -> int:
    """Runtime-reachable count of the rows this article would actually store.

    Писатель кладёт в базу И поверхностную, И каноническую форму
    (``expand_synonym_terms``), поэтому считать достижимость надо по тому же
    набору строк — иначе оценка занижена ровно на всю exact-группу, где
    поверхность несёт префикс области действия.
    """

    stored: set[str] = set()
    for group in expand_synonym_terms(values).values():
        stored.update(group)
    return len(_query_reachable_canonical_terms(stored))


def _filter_auto_enrichment_items(
    items: Any,
    batch: Sequence[Mapping[str, Any]],
    *,
    exact_plan: Optional[ExactDuplicatePlan] = None,
    minimum_physical: Optional[int] = None,
    minimum_effective: Optional[int] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """Drop unsupported synonyms, then unsalvageable articles — never a batch.

    Возвращает пару ``(items, report)``.  ``items`` уже сокращён, но НЕ
    провалидирован: сразу за фильтром обязан идти неизменённый строгий гейт,
    и только его вердикт считается.

    Решение об отбраковке синонима принимается на ГРУППУ байт-идентичных тел и
    применяется ПО ИНДЕКСУ ко всем её членам: иначе последовательности
    суффиксов членов разойдутся и группа честно упадёт на «asymmetric ordered
    synonym suffixes».  Группа отбрасывается целиком или не отбрасывается
    вовсе — частичная группа запрещена и гейтом, и писателем.
    """

    floor_physical = (
        _AUTO_ENRICH_MIN_PHYSICAL
        if minimum_physical is None
        else int(minimum_physical)
    )
    floor_effective = (
        _AUTO_ENRICH_MIN_EFFECTIVE
        if minimum_effective is None
        else int(minimum_effective)
    )
    report: Dict[str, Any] = {
        "articles_kept": 0,
        "articles_dropped": 0,
        "synonyms_dropped": 0,
        "dropped_articles": [],
        "dropped_synonyms": [],
        "applied": False,
    }
    expected: Dict[int, Mapping[str, Any]] = {}
    for article in batch:
        try:
            expected[int(article["id"])] = article
        except (KeyError, TypeError, ValueError):
            return items, report

    # Структурные беды ответа (не список, не объект, чужой/повторный id,
    # неточное покрытие) фильтру не принадлежат: их по-прежнему судит гейт.
    if not isinstance(items, list) or len(items) != len(expected):
        return items, report
    by_id: Dict[int, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            return items, report
        article_id = item.get("id")
        if isinstance(article_id, bool) or not isinstance(article_id, int):
            return items, report
        if article_id not in expected or article_id in by_id:
            return items, report
        by_id[article_id] = item
    if set(by_id) != set(expected):
        return items, report

    trusted_scope_prefixes = {
        member.article_id: member.synonym_scope_prefix
        for group in exact_plan.groups
        for member in group.members
    } if exact_plan is not None else {}
    trusted_title_contexts = {
        member.article_id: member.title_scope_prefix
        for group in exact_plan.groups
        for member in group.members
    } if exact_plan is not None else {}

    groups: list[Tuple[int, ...]] = []
    grouped: set[int] = set()
    if exact_plan is not None:
        for group in exact_plan.groups:
            group_ids = [
                int(article_id)
                for article_id in group.article_ids
                if int(article_id) in by_id
            ]
            if not group_ids:
                continue
            if len(group_ids) != len(group.article_ids):
                # Частичная группа в этом батче — не наш класс беды.
                return items, report
            groups.append(tuple(group_ids))
            grouped.update(group_ids)
    groups.extend(
        (article_id,)
        for article_id in sorted(set(by_id) - grouped)
    )

    def _raw_synonyms(article_id: int) -> Optional[list[Any]]:
        value = by_id[article_id].get("synonyms")
        if isinstance(value, (str, bytes)) or not isinstance(value, list):
            return None
        return list(value)

    def _gate_penalty(article_id: int) -> Any:
        """M-C: penalty ровно в том виде, в каком его увидит ГЕЙТ.

        Гейт не передаёт поле дальше как есть: он требует объект или ``null``,
        отвергает неизвестные поля и сжимает каждое поле до 500 символов.
        Фильтр обязан судить ТУ ЖЕ величину — иначе он «спасает» статью,
        которую гейт всё равно отвергнет, и весь батч гибнет уже ПОСЛЕ
        фильтра, ровно та беда, ради которой фильтр и делался.  На корпусе
        это не выстрелило по удаче (модель не присылала лишних полей), а не
        по свойству конвейера.
        """

        penalty = by_id[article_id].get("penalty")
        if penalty is None:
            return None
        if not isinstance(penalty, dict):
            raise ValueError(
                f"article {article_id} penalty must be an object or null"
            )
        unknown_penalty = set(penalty) - _AUTO_ENRICH_PENALTY_FIELDS
        if unknown_penalty:
            raise ValueError(
                f"article {article_id} penalty has unknown fields: "
                f"{sorted(unknown_penalty)}"
            )
        return {
            key: _compact_optional_llm_text(
                value,
                field=f"article {article_id} penalty.{key}",
                maximum=500,
            )
            for key, value in penalty.items()
        }

    def _probe_penalty(article_id: int) -> Optional[str]:
        """Структура наказания — факт про ВСЮ статью, как и заголовок.

        Вычёркивать по индексу здесь нечего, поэтому статья честно
        пропускается целиком, а батч и прогон продолжаются.
        """

        try:
            _gate_penalty(article_id)
        except Exception as exc:  # noqa: BLE001 — вердикт правила as-is
            return str(exc)
        return None

    def _probe(article_id: int, synonyms: Sequence[Any]) -> Optional[str]:
        try:
            _cached_validate_enrichment_source_evidence(
                str(expected[article_id].get("full_text") or ""),
                _compact_optional_llm_text(
                    by_id[article_id].get("penalty_summary"),
                    field=f"article {article_id} penalty_summary",
                    maximum=1_000,
                ),
                _gate_penalty(article_id),
                list(synonyms),
                trusted_scope_prefix=trusted_scope_prefixes.get(article_id),
            )
        except Exception as exc:  # noqa: BLE001 — вердикт валидатора as-is
            return str(exc)
        return None

    def _probe_title(article_id: int) -> Optional[str]:
        """M2: заголовок судится ровно тем же правилом, что и в гейте.

        Без этой пробы фильтр «спасал» статью, чей заголовок гейт всё равно
        отвергнет, и весь батч честно погибал уже ПОСЛЕ фильтра.

        Замер на настоящем выводе (chicago, 264 статьи; аргументы те же, что
        у гейта — с настоящим ``article_ref``): заголовок отвергнут у 99
        статей (37,5%), из них 84 — правило тождества парсерского названия,
        15 — семантика.  Эти статьи лежат в 31 батче из 33, первый — №1.
        Без пробы гибнут все 248 статей задетых батчей, с пробой — только
        сами 99: проба спасает 149 статей.

        Разбор называл 7,8% (21 из 269) потому, что пробовал заголовок с
        ``source_title=None``; продукт же передаёт настоящий ``article_ref``,
        и парсерских названий тогда 130, а не единицы.

        Заголовок не поштучная беда: у статьи он один, вычёркивать по индексу
        нечего.  Поэтому вердикт здесь всегда про ВСЮ статью, и она честно
        пропускается целиком — но батч и прогон продолжаются.
        """

        full_text = str(expected[article_id].get("full_text") or "")
        source_title_context = "\n".join(
            value
            for value in (
                str(expected[article_id].get("doc_title") or ""),
            )
            if value
        )
        try:
            title = _compact_optional_llm_text(
                by_id[article_id].get("title"),
                field=f"article {article_id} title",
                maximum=300,
            )
            _cached_validate_enrichment_title_source_evidence(
                full_text,
                title,
                source_title=source_catalog_title(
                    expected[article_id].get("article_ref"),
                    full_text,
                ),
                source_context=source_title_context or None,
                trusted_title_prefix=trusted_title_contexts.get(article_id),
            )
        except Exception as exc:  # noqa: BLE001 — вердикт валидатора as-is
            return str(exc)
        return None

    kept_ids: set[int] = set()
    kept_positions: Dict[int, Optional[list[int]]] = {}
    for group_ids in groups:
        # M-C: структура наказания проверяется в гейте у КАЖДОГО элемента и
        # ДО разбора надгробий, поэтому проба стоит здесь же.
        penalty_failure: Optional[Tuple[int, str]] = None
        for article_id in group_ids:
            message = _probe_penalty(article_id)
            if message is not None:
                penalty_failure = (article_id, message)
                break
        if penalty_failure is not None:
            report["dropped_articles"].append({
                "article_ids": list(group_ids),
                "reason": (
                    f"article {penalty_failure[0]} penalty structure "
                    f"rejected: {penalty_failure[1]}"
                )[:400],
            })
            continue

        exempt = {
            article_id: is_explicitly_empty_or_repealed_article(
                str(expected[article_id].get("full_text") or "")
            )
            for article_id in group_ids
        }
        if any(exempt.values()):
            # Надгробия гейт судит отдельным правилом «ничего не должно быть».
            if all(exempt.values()):
                kept_ids.update(group_ids)
                for article_id in group_ids:
                    kept_positions[article_id] = None
            else:
                report["dropped_articles"].append({
                    "article_ids": list(group_ids),
                    "reason": "mixed tombstone/substantive exact-body group",
                })
            continue

        # M2: заголовок проверяется ДО дорогого жадного удаления — если он
        # обречён, спасать синонимы этой статьи бессмысленно, гейт всё равно
        # отвергнет её и утащит за собой весь батч.
        title_failure: Optional[Tuple[int, str]] = None
        for article_id in group_ids:
            message = _probe_title(article_id)
            if message is not None:
                title_failure = (article_id, message)
                break
        if title_failure is not None:
            report["dropped_articles"].append({
                "article_ids": list(group_ids),
                "reason": (
                    f"article {title_failure[0]} title evidence failed: "
                    f"{title_failure[1]}"
                )[:400],
            })
            continue

        raw: Dict[int, list[Any]] = {}
        broken = False
        for article_id in group_ids:
            values = _raw_synonyms(article_id)
            if values is None:
                broken = True
                break
            raw[article_id] = values
        if broken or len({len(values) for values in raw.values()}) != 1:
            report["dropped_articles"].append({
                "article_ids": list(group_ids),
                "reason": "synonyms are not one mirrored list per member",
            })
            continue

        admissible: Optional[set[int]] = None
        for article_id in group_ids:
            positions = set(_runtime_admissible_synonym_positions(
                raw[article_id],
            ))
            admissible = (
                positions if admissible is None else admissible & positions
            )
        keep = sorted(admissible or ())
        # M-C: потолок статьи (``_AUTO_ENRICH_MAX_PHYSICAL``) — правило ГЕЙТА,
        # и без его зеркала статья с 16 годными синонимами доживала до гейта
        # и убивала ВЕСЬ батч.  Лишнее срезается ПО ИНДЕКСУ и одинаково у
        # всех членов группы байт-идентичных тел, иначе разъедутся суффиксы;
        # срез идёт ДО жадного удаления, поэтому последнее слово всё равно
        # остаётся за самим валидатором.
        if len(keep) > _AUTO_ENRICH_MAX_PHYSICAL:
            for index in keep[_AUTO_ENRICH_MAX_PHYSICAL:]:
                for article_id in group_ids:
                    report["dropped_synonyms"].append({
                        "article_id": article_id,
                        "synonym": raw[article_id][index],
                        "reason": (
                            "over the per-article ceiling "
                            f"{_AUTO_ENRICH_MAX_PHYSICAL}"
                        ),
                    })
            keep = keep[:_AUTO_ENRICH_MAX_PHYSICAL]
        failure: Optional[str] = None
        # Жадное удаление: последнее слово всегда за самим валидатором.
        for _ in range(len(raw[group_ids[0]]) + 1):
            if not keep:
                failure = "no synonym survived the unchanged validator"
                break
            offending: Optional[int] = None
            for article_id in group_ids:
                message = _probe(
                    article_id,
                    [raw[article_id][index] for index in keep],
                )
                if message is None:
                    continue
                position = _failed_synonym_index(message)
                if position is None or not 0 <= position < len(keep):
                    failure = message
                    break
                offending = keep[position]
                report["dropped_synonyms"].append({
                    "article_id": article_id,
                    "synonym": raw[article_id][offending],
                    "reason": message[:400],
                })
                break
            if failure is not None:
                break
            if offending is None:
                break
            keep.remove(offending)
        if failure is not None:
            report["dropped_articles"].append({
                "article_ids": list(group_ids),
                "reason": failure[:400],
            })
            continue

        if len(keep) < floor_physical or any(
            _auto_enrichment_effective_count(
                [raw[article_id][index] for index in keep]
            ) < floor_effective
            for article_id in group_ids
        ):
            report["dropped_articles"].append({
                "article_ids": list(group_ids),
                "reason": (
                    f"survivors={len(keep)} below floor "
                    f"physical>={floor_physical}/effective>={floor_effective}"
                ),
            })
            continue
        kept_ids.update(group_ids)
        for article_id in group_ids:
            kept_positions[article_id] = list(keep)

    # Кросс-статейные канонические коллизии внутри уцелевшего набора: владельца
    # выбирать не за что, поэтому спорная форма отбрасывается у ВСЕХ
    # претендентов — fail-closed, и по индексу у всей группы-близнеца.
    group_of = {
        article_id: group_ids
        for group_ids in groups
        for article_id in group_ids
    }
    for _ in range(len(by_id) + 1):
        owners: Dict[str, set[int]] = {}
        for article_id in sorted(kept_ids):
            positions = kept_positions.get(article_id)
            if positions is None:
                continue
            for index in positions:
                canonical = canonical_synonym_term(
                    by_id[article_id]["synonyms"][index]
                )
                if canonical is None:
                    continue
                owners.setdefault(canonical, set()).add(article_id)
        contested = {
            canonical: article_ids
            for canonical, article_ids in owners.items()
            if len(article_ids) > 1
        }
        if not contested:
            break
        for canonical, article_ids in sorted(contested.items()):
            for article_id in sorted(article_ids):
                positions = kept_positions.get(article_id)
                if not positions:
                    continue
                doomed = [
                    index
                    for index in positions
                    if canonical_synonym_term(
                        by_id[article_id]["synonyms"][index]
                    ) == canonical
                ]
                for index in doomed:
                    for sibling in group_of.get(article_id, (article_id,)):
                        sibling_positions = kept_positions.get(sibling)
                        if sibling_positions and index in sibling_positions:
                            report["dropped_synonyms"].append({
                                "article_id": sibling,
                                "synonym": by_id[sibling]["synonyms"][index],
                                "reason": (
                                    "canonical group contested across "
                                    f"articles {sorted(article_ids)}"
                                ),
                            })
                            sibling_positions.remove(index)
    for group_ids in groups:
        if not set(group_ids) <= kept_ids:
            continue
        positions = kept_positions.get(group_ids[0])
        if positions is None:
            continue
        if len(positions) < floor_physical or any(
            _auto_enrichment_effective_count([
                by_id[article_id]["synonyms"][index] for index in positions
            ]) < floor_effective
            for article_id in group_ids
        ):
            report["dropped_articles"].append({
                "article_ids": list(group_ids),
                "reason": (
                    f"survivors={len(positions)} below floor after canonical "
                    "contest resolution"
                ),
            })
            kept_ids.difference_update(group_ids)

    filtered: list[Any] = []
    for item in items:
        article_id = int(item["id"])
        if article_id not in kept_ids:
            report["articles_dropped"] += 1
            continue
        positions = kept_positions.get(article_id)
        if positions is None:
            filtered.append(item)
        else:
            survivors = [item["synonyms"][index] for index in positions]
            report["synonyms_dropped"] += (
                len(item["synonyms"]) - len(survivors)
            )
            filtered.append({**item, "synonyms": survivors})
        report["articles_kept"] += 1
    report["applied"] = True
    return filtered, report


def _validate_auto_enrichment_response(
    items: Any,
    batch: Sequence[Mapping[str, Any]],
    *,
    exact_plan: Optional[ExactDuplicatePlan] = None,
) -> List[Dict[str, Any]]:
    """Validate a complete Gemini batch before the first database write."""

    if not isinstance(items, list):
        raise ValueError("response must be a JSON array")
    expected = {int(article["id"]): article for article in batch}
    normalized_by_id: Dict[int, Dict[str, Any]] = {}
    trusted_scope_prefixes = {
        member.article_id: member.synonym_scope_prefix
        for group in exact_plan.groups
        for member in group.members
    } if exact_plan is not None else {}
    trusted_title_contexts = {
        member.article_id: member.title_scope_prefix
        for group in exact_plan.groups
        for member in group.members
    } if exact_plan is not None else {}

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"item {index} must be an object")
        fields = set(item)
        if fields != _AUTO_ENRICH_RESPONSE_FIELDS:
            raise ValueError(
                f"item {index} fields differ from contract: "
                f"missing={sorted(_AUTO_ENRICH_RESPONSE_FIELDS - fields)}, "
                f"unknown={sorted(fields - _AUTO_ENRICH_RESPONSE_FIELDS)}"
            )
        article_id = item.get("id")
        if isinstance(article_id, bool) or not isinstance(article_id, int):
            raise ValueError(f"item {index} has invalid id")
        if article_id not in expected:
            raise ValueError(f"unexpected article id {article_id}")
        if article_id in normalized_by_id:
            raise ValueError(f"duplicate article id {article_id}")

        title = _compact_optional_llm_text(
            item["title"],
            field=f"article {article_id} title",
            maximum=300,
        )
        summary = _compact_optional_llm_text(
            item["penalty_summary"],
            field=f"article {article_id} penalty_summary",
            maximum=1_000,
        )
        penalty = item["penalty"]
        if penalty is not None:
            if not isinstance(penalty, dict):
                raise ValueError(
                    f"article {article_id} penalty must be an object or null"
                )
            unknown_penalty = set(penalty) - _AUTO_ENRICH_PENALTY_FIELDS
            if unknown_penalty:
                raise ValueError(
                    f"article {article_id} penalty has unknown fields: "
                    f"{sorted(unknown_penalty)}"
                )
            penalty = {
                key: _compact_optional_llm_text(
                    value,
                    field=f"article {article_id} penalty.{key}",
                    maximum=500,
                )
                for key, value in penalty.items()
            }

        full_text = str(expected[article_id].get("full_text") or "")
        exempt = is_explicitly_empty_or_repealed_article(full_text)
        raw_synonyms = item["synonyms"]
        if not isinstance(raw_synonyms, list):
            raise ValueError(
                f"article {article_id} synonyms must be an array"
            )
        try:
            synonyms = validate_runtime_canonical_synonym_claims([{
                "id": article_id,
                "synonyms": raw_synonyms,
            }])[article_id]
        except SynonymWriteError as exc:
            raise ValueError(str(exc)) from exc

        if exempt:
            if title is not None or summary is not None or penalty is not None or synonyms:
                raise ValueError(
                    f"empty/repealed article {article_id} must have null "
                    "title/penalty and no synonyms"
                )
        elif not (
            _AUTO_ENRICH_MIN_PHYSICAL
            <= len(synonyms)
            <= _AUTO_ENRICH_MAX_PHYSICAL
        ):
            raise ValueError(
                f"article {article_id} must have "
                f"{_AUTO_ENRICH_MIN_PHYSICAL}-"
                f"{_AUTO_ENRICH_MAX_PHYSICAL} distinct synonyms, "
                f"got {len(synonyms)}"
            )

        if not exempt:
            try:
                source_title_context = "\n".join(
                    value
                    for value in (
                        str(expected[article_id].get("doc_title") or ""),
                    )
                    if value
                )
                _cached_validate_enrichment_title_source_evidence(
                    full_text,
                    title,
                    source_title=source_catalog_title(
                        expected[article_id].get("article_ref"),
                        full_text,
                    ),
                    source_context=source_title_context or None,
                    trusted_title_prefix=trusted_title_contexts.get(
                        article_id
                    ),
                )
                _cached_validate_enrichment_source_evidence(
                    full_text,
                    summary,
                    penalty,
                    synonyms,
                    trusted_scope_prefix=trusted_scope_prefixes.get(
                        article_id
                    ),
                )
            except ValueError as exc:
                raise ValueError(
                    f"article {article_id} source evidence failed: {exc}"
                ) from exc

        normalized_by_id[article_id] = {
            "id": article_id,
            "title": title,
            "penalty_summary": summary,
            "penalty": penalty,
            "synonyms": synonyms,
        }

    missing = sorted(set(expected) - set(normalized_by_id))
    if missing or len(items) != len(expected):
        raise ValueError(
            "response coverage is not exact: "
            f"missing={missing}, expected={len(expected)}, actual={len(items)}"
        )

    try:
        validate_runtime_canonical_synonym_claims(
            list(normalized_by_id.values())
        )
    except SynonymWriteError as exc:
        raise ValueError(str(exc)) from exc

    if exact_plan is not None:
        expected_ids = set(expected)
        for group in exact_plan.groups:
            group_ids = set(group.article_ids)
            if expected_ids.isdisjoint(group_ids):
                continue
            if not group_ids <= expected_ids:
                raise ValueError(
                    "exact-body group response validation received a partial "
                    f"group: present={sorted(expected_ids & group_ids)}, "
                    f"required={list(group.article_ids)}"
                )
            validate_exact_duplicate_group_response(
                group,
                list(normalized_by_id.values()),
                canonicalize_suffix=canonical_synonym_term,
                source_titles={
                    member.article_id: source_catalog_title(
                        expected[member.article_id].get("article_ref"),
                        str(
                            expected[member.article_id].get("full_text")
                            or ""
                        ),
                    )
                    for member in group.members
                },
            )

    return [
        normalized_by_id[int(article["id"])]
        for article in batch
    ]


def _salvage_auto_enrichment_response(
    raw_items: Any,
    batch: Sequence[Mapping[str, Any]],
    *,
    exact_plan: Optional[ExactDuplicatePlan] = None,
    server_id: str,
    stage: str,
    stats: Optional[MutableMapping[str, Any]] = None,
    allow_article_drop: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Mapping[str, Any]], Dict[str, Any]]:
    """Strict gate first; only then keep whatever the gate itself can accept.

    Один негодный синоним больше не роняет батч, и одна негодная статья больше
    не роняет прогон сервера.  Порядок принципиален: сначала НЕИЗМЕНЁННЫЙ гейт
    на полном ответе (тогда лучший случай не платит ничего), и лишь если он
    отказал — фильтр, а за ним ТОТ ЖЕ гейт на уцелевшем.  Ни один синоним не
    доходит до записи, не пройдя ровно ту же строгую проверку, что и раньше.

    Если годной не осталась НИ ОДНА статья, исключение пробрасывается как
    раньше: молчаливый пустой проход не имеет права выглядеть успехом.
    """

    try:
        return (
            _validate_auto_enrichment_response(
                raw_items,
                batch,
                exact_plan=exact_plan,
            ),
            list(batch),
            {"applied": False},
        )
    except ValueError as strict_error:
        first_error = strict_error

    filtered, report = _filter_auto_enrichment_items(
        raw_items,
        batch,
        exact_plan=exact_plan,
    )
    if not report["applied"]:
        raise first_error
    kept_ids = {int(item["id"]) for item in filtered}
    kept_batch = [
        article
        for article in batch
        if int(article["id"]) in kept_ids
    ]
    if not kept_batch:
        raise first_error
    if not allow_article_drop and len(kept_batch) != len(batch):
        # Офлайн-артефакт обязан покрывать батч ТОЧНО (его читают писатель,
        # план применения и повторная валидация), поэтому там разрешена только
        # поштучная приёмка синонимов, а выпадение статьи остаётся отказом.
        raise first_error
    items = _validate_auto_enrichment_response(
        filtered,
        kept_batch,
        exact_plan=exact_plan,
    )
    if stats is not None:
        stats["articles_dropped"] = (
            int(stats.get("articles_dropped", 0)) + report["articles_dropped"]
        )
        stats["synonyms_dropped"] = (
            int(stats.get("synonyms_dropped", 0)) + report["synonyms_dropped"]
        )
    logger.warning(
        "[LAW_CATALOG] server=%s stage=%s: поштучная приёмка отбросила "
        "синонимов=%d, статей=%d, оставила статей=%d; причина первого отказа "
        "пакета: %s",
        server_id,
        stage,
        report["synonyms_dropped"],
        report["articles_dropped"],
        report["articles_kept"],
        first_error,
    )
    for dropped in report["dropped_synonyms"]:
        logger.info(
            "[LAW_CATALOG] server=%s stage=%s article=%s: синоним отброшен "
            "%r — %s",
            server_id,
            stage,
            dropped["article_id"],
            dropped["synonym"],
            dropped["reason"],
        )
    for dropped in report["dropped_articles"]:
        logger.warning(
            "[LAW_CATALOG] server=%s stage=%s: статьи %s пропущены целиком — "
            "%s",
            server_id,
            stage,
            dropped["article_ids"],
            dropped["reason"],
        )
    return items, kept_batch, report


async def _write_auto_enrichment_batch(
    server_id: str,
    batch: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    signature_registry: ExactSignatureRegistry,
) -> Dict[str, int]:
    """Commit one validated model batch all-or-none under the synonym lock."""

    articles_by_id = {int(article["id"]): article for article in batch}
    working_registry = signature_registry.clone()
    local_stats = {"articles": 0, "synonyms": 0}
    async with db_manager.transaction() as conn:
        await acquire_synonym_write_lock(conn, server_id)
        for item in items:
            article_id = int(item["id"])
            article = articles_by_id[article_id]
            written = await _write_enrichment_in_transaction(
                conn,
                server_id,
                article_id,
                item.get("penalty_summary") or None,
                item.get("penalty"),
                item.get("synonyms") or (),
                local_stats,
                expected_article_hash=str(article["article_hash"]),
                title=item.get("title"),
                signature_registry=working_registry,
                synonym_lock_already_held=True,
                minimum_synonym_physical=_AUTO_ENRICH_MIN_PHYSICAL,
                minimum_synonym_effective=_AUTO_ENRICH_MIN_EFFECTIVE,
            )
            if not written:
                raise SynonymWriteError(
                    f"article {article_id} changed while its Gemini batch "
                    "was being committed"
                )
            local_stats["articles"] += 1
        if local_stats.get("synonym_collisions_rejected", 0):
            raise SynonymWriteError(
                "atomic Gemini batch rejected one or more PostgreSQL exact-leg "
                "signature collisions"
            )
    signature_registry.replace_with(working_registry)
    return local_stats


def _desired_auto_enrichment_title_state(
    article: Mapping[str, Any],
    reviewed_title: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve title and persisted ownership without guessing legacy state."""

    derived_kind, derived_code, source_title = derive_catalog_fields(
        article.get("article_ref"),
        str(article.get("full_text") or ""),
    )
    if (
        derived_kind != str(article.get("kind") or "")
        or derived_code != article.get("code")
    ):
        raise SynonymWriteError(
            f"article {article.get('id')} parser fields differ from the "
            "captured catalog; run normal catalog reconcile first"
        )

    provenance = article.get("title_provenance")
    if provenance is not None and provenance not in _TITLE_PROVENANCE_VALUES:
        raise SynonymWriteError(
            f"article {article.get('id')} has invalid title provenance "
            f"{provenance!r}"
        )

    if is_explicitly_empty_or_repealed_article(article.get("full_text")):
        if article.get("title") is not None or provenance is not None:
            raise SynonymWriteError(
                f"article {article.get('id')} tombstone title was not cleared "
                "by normal catalog reconcile"
            )
        if reviewed_title is not None:
            raise SynonymWriteError(
                f"article {article.get('id')} tombstone cannot receive a title"
            )
        return None, None

    if source_title is not None:
        if article.get("title") != source_title:
            raise SynonymWriteError(
                f"article {article.get('id')} source-owned title differs from "
                "the captured catalog; run normal catalog reconcile first"
            )
        if reviewed_title != source_title:
            raise SynonymWriteError(
                f"article {article.get('id')} Gemini attempted to change a "
                "source-owned title"
            )
        return source_title, TITLE_PROVENANCE_SOURCE

    existing_title = article.get("title")
    if existing_title is not None and provenance != TITLE_PROVENANCE_GENERATED:
        raise SynonymWriteError(
            f"article {article.get('id')} has a non-source title with "
            "unattested ownership; run the versioned provenance attestation "
            "before Gemini update"
        )
    if existing_title is None and provenance is not None:
        raise SynonymWriteError(
            f"article {article.get('id')} has title provenance without a title"
        )
    return (
        reviewed_title,
        TITLE_PROVENANCE_GENERATED if reviewed_title is not None else None,
    )


def _desired_auto_enrichment_title(
    article: Mapping[str, Any],
    reviewed_title: Optional[str],
) -> Optional[str]:
    """Backward-compatible title-only view of the strict ownership result."""

    return _desired_auto_enrichment_title_state(article, reviewed_title)[0]


def _strict_auto_enrichment_article_id(
    value: Any,
    *,
    field: str,
) -> int:
    """Reject Python-equal aliases instead of coercing writer identities."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise SynonymWriteError(
            f"server-wide enrichment {field} must use integer article ids"
        )
    return value


async def _write_auto_enrichment_server_in_transaction(
    conn,
    server_id: str,
    articles: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    generation_state: Mapping[str, Any],
    *,
    synonym_lock_already_held: bool = False,
) -> Dict[str, int]:
    """Shared complete-replacement writer inside the caller's transaction."""

    if not isinstance(generation_state, Mapping):
        raise SynonymWriteError(
            "server-wide enrichment lacks its generation snapshot"
        )
    if (
        isinstance(articles, (str, bytes))
        or not isinstance(articles, Sequence)
        or isinstance(items, (str, bytes))
        or not isinstance(items, Sequence)
    ):
        raise SynonymWriteError(
            "server-wide enrichment write inputs must be sequences"
        )
    try:
        frozen_generation_state = dict(generation_state)
        frozen_articles = [
            deepcopy(dict(article))
            for article in articles
            if isinstance(article, Mapping)
        ]
        frozen_items = [
            deepcopy(dict(item))
            for item in items
            if isinstance(item, Mapping)
        ]
    except (TypeError, ValueError) as exc:
        raise SynonymWriteError(
            "server-wide enrichment inputs cannot be materialized safely"
        ) from exc
    if (
        len(frozen_articles) != len(articles)
        or len(frozen_items) != len(items)
    ):
        raise SynonymWriteError(
            "server-wide enrichment write inputs must contain mappings"
        )
    passed_articles_by_id = {
        _strict_auto_enrichment_article_id(
            article.get("id"),
            field="articles",
        ): article
        for article in frozen_articles
    }
    items_by_id = {
        _strict_auto_enrichment_article_id(
            item.get("id"),
            field="items",
        ): item
        for item in frozen_items
    }
    if (
        len(passed_articles_by_id) != len(frozen_articles)
        or len(items_by_id) != len(frozen_items)
        or set(passed_articles_by_id) != set(items_by_id)
    ):
        raise SynonymWriteError(
            "server-wide enrichment write coverage is not exact"
        )
    raw_fingerprints = frozen_generation_state.get("auto_fingerprints")
    raw_snapshot = frozen_generation_state.get("snapshot")
    raw_context_hashes = frozen_generation_state.get("context_hashes")
    if (
        not isinstance(raw_fingerprints, Mapping)
        or not isinstance(raw_snapshot, Mapping)
        or not isinstance(raw_context_hashes, Mapping)
    ):
        raise SynonymWriteError(
            "server-wide enrichment lacks its generation snapshot"
        )
    try:
        fingerprints = deepcopy(dict(raw_fingerprints))
        snapshot = deepcopy(dict(raw_snapshot))
        supplied_context_hashes = deepcopy(dict(raw_context_hashes))
    except (TypeError, ValueError) as exc:
        raise SynonymWriteError(
            "server-wide enrichment state cannot be materialized safely"
        ) from exc
    for field, values in (
        ("auto_fingerprints", fingerprints),
        ("context_hashes", supplied_context_hashes),
    ):
        if any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in values
        ):
            raise SynonymWriteError(
                f"server-wide enrichment {field} must use integer article ids"
            )
    raw_catalog_rows = frozen_generation_state.get("catalog_rows")
    if isinstance(raw_catalog_rows, (str, bytes)) or not isinstance(
        raw_catalog_rows,
        Sequence,
    ):
        raise SynonymWriteError(
            "server-wide enrichment lacks its exact catalog snapshot"
        )
    catalog_rows = validate_auto_replacement_catalog_binding(
        server_id,
        snapshot,
        raw_catalog_rows,
    )
    try:
        exact_plan = build_exact_duplicate_plan(
            catalog_rows,
            is_substantive_article=lambda row: not (
                is_explicitly_empty_or_repealed_article(
                    row.get("full_text")
                )
            ),
            canonicalize_scope=canonical_synonym_term,
        )
    except ValueError as exc:
        raise SynonymWriteError(
            "server-wide enrichment exact catalog snapshot is invalid: "
            f"{exc}"
        ) from exc
    trusted_scope_prefixes = {
        member.article_id: member.synonym_scope_prefix
        for group in exact_plan.groups
        for member in group.members
    }
    trusted_title_contexts = {
        member.article_id: member.title_scope_prefix
        for group in exact_plan.groups
        for member in group.members
    }
    catalog_articles_by_id = {
        int(row["id"]): row
        for row in catalog_rows
        if str(row.get("kind") or "") == "article"
    }
    catalog_article_count = sum(
        1
        for row in catalog_rows
        if str(row.get("kind") or "") == "article"
    )
    if len(catalog_articles_by_id) != catalog_article_count:
        raise SynonymWriteError(
            "server-wide enrichment exact catalog contains duplicate "
            "article ids"
        )
    missing_catalog_rows = sorted(
        set(passed_articles_by_id) - set(catalog_articles_by_id)
    )
    if missing_catalog_rows:
        raise SynonymWriteError(
            "server-wide enrichment exact catalog snapshot lacks article ids: "
            f"{missing_catalog_rows}"
        )
    articles_by_id = {
        article_id: catalog_articles_by_id[article_id]
        for article_id in passed_articles_by_id
    }
    try:
        context_hashes = build_auto_replacement_context_hashes(
            server_id,
            catalog_rows,
            exact_duplicate_plan=exact_plan,
        )
    except ValueError as exc:
        raise SynonymWriteError(
            "server-wide enrichment context cannot be derived from its "
            f"bound catalog: {exc}"
        ) from exc
    if supplied_context_hashes != context_hashes:
        raise SynonymWriteError(
            "server-wide enrichment context hashes differ from its bound "
            "catalog"
        )
    missing_fingerprints = sorted(
        set(articles_by_id) - set(fingerprints)
    )
    if missing_fingerprints or any(
        not isinstance(fingerprints[article_id], str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprints[article_id]) is None
        for article_id in articles_by_id
    ):
        raise SynonymWriteError(
            "server-wide enrichment lacks valid auto fingerprints for ids: "
            f"{missing_fingerprints}"
        )

    write_article_ids = set(articles_by_id)
    intersecting_exact_groups = [
        group
        for group in exact_plan.groups
        if not write_article_ids.isdisjoint(group.article_ids)
    ]
    for group in intersecting_exact_groups:
        missing_group_ids = sorted(
            set(group.article_ids) - write_article_ids
        )
        if missing_group_ids:
            raise SynonymWriteError(
                "server-wide enrichment write intersects an incomplete "
                "exact-body group: "
                f"present={sorted(write_article_ids & set(group.article_ids))}, "
                f"missing={missing_group_ids}"
            )
    for group in intersecting_exact_groups:
        try:
            validate_exact_duplicate_group_response(
                group,
                [
                    items_by_id[article_id]
                    for article_id in group.article_ids
                ],
                canonicalize_suffix=canonical_synonym_term,
                source_titles={
                    member.article_id: source_catalog_title(
                        articles_by_id[member.article_id].get("article_ref"),
                        str(
                            articles_by_id[member.article_id].get(
                                "full_text"
                            )
                            or ""
                        ),
                    )
                    for member in group.members
                },
            )
        except ValueError as exc:
            raise SynonymWriteError(
                "server-wide enrichment exact-body response is invalid: "
                f"{exc}"
            ) from exc

    for article_id in sorted(articles_by_id):
        article = articles_by_id[article_id]
        item = items_by_id[article_id]
        try:
            source_title_context = "\n".join(
                value
                for value in (
                    str(article.get("doc_title") or ""),
                )
                if value
            )
            full_text = str(article.get("full_text") or "")
            # M-A: это тоже публичные входы валидатора, и они идут в цикле
            # событий, пока приёмка соседнего сервера считается в потоке.
            # Без общего замка сосед чистил бы память валидатора посреди
            # ЭТОЙ проверки — последней перед записью.  Захват один на
            # статью, поэтому ожидание ограничено одним вызовом валидатора,
            # а не целым батчем.
            with _ENRICHMENT_VALIDATOR_LOCK:
                validate_enrichment_title_source_evidence(
                    full_text,
                    item.get("title"),
                    source_title=source_catalog_title(
                        article.get("article_ref"),
                        full_text,
                    ),
                    source_context=source_title_context or None,
                    trusted_title_prefix=trusted_title_contexts.get(
                        article_id
                    ),
                )
                validate_enrichment_source_evidence(
                    full_text,
                    item.get("penalty_summary"),
                    item.get("penalty"),
                    item.get("synonyms") or (),
                    trusted_scope_prefix=trusted_scope_prefixes.get(
                        article_id
                    ),
                )
        except ValueError as exc:
            raise SynonymWriteError(
                f"article {article_id} enrichment is not supported by the "
                f"current source: {exc}"
            ) from exc

    replacement_items = [
        {
            "id": article_id,
            "article_hash": str(articles_by_id[article_id]["article_hash"]),
            "expected_enrich_hash": articles_by_id[article_id].get(
                "enrich_hash"
            ),
            "expected_auto_synonym_fingerprint": fingerprints[article_id],
            "synonyms": list(items_by_id[article_id].get("synonyms") or ()),
        }
        for article_id in sorted(articles_by_id)
    ]
    desired_title_states = {
        article_id: _desired_auto_enrichment_title_state(
            articles_by_id[article_id],
            items_by_id[article_id].get("title"),
        )
        for article_id in articles_by_id
    }

    return await _persist_auto_enrichment_server_in_savepoint(
        conn,
        server_id,
        articles_by_id,
        items_by_id,
        replacement_items,
        desired_title_states,
        context_hashes,
        snapshot,
        synonym_lock_already_held=synonym_lock_already_held,
    )


async def _persist_auto_enrichment_server_in_savepoint(
    conn,
    server_id: str,
    articles_by_id: Mapping[int, Mapping[str, Any]],
    items_by_id: Mapping[int, Mapping[str, Any]],
    replacement_items: Sequence[Mapping[str, Any]],
    desired_title_states: Mapping[
        int,
        Tuple[Optional[str], Optional[str]],
    ],
    context_hashes: Mapping[int, str],
    snapshot: Mapping[str, Any],
    *,
    synonym_lock_already_held: bool,
) -> Dict[str, int]:
    """Keep synonyms, metadata and embedding jobs in one writer savepoint."""

    nested_transaction = getattr(conn, "transaction", None)
    if not callable(nested_transaction):
        raise SynonymWriteError(
            "server-wide enrichment writer requires savepoint support"
        )
    async with nested_transaction():
        embedding_jobs = 0
        if not synonym_lock_already_held:
            await acquire_synonym_write_lock(conn, server_id)
        outcome = await replace_auto_synonyms_bulk_in_transaction(
            conn,
            server_id,
            replacement_items,
            generation_snapshot=snapshot,
            lock_already_held=True,
            minimum_physical=_AUTO_ENRICH_MIN_PHYSICAL,
            minimum_effective=_AUTO_ENRICH_MIN_EFFECTIVE,
        )
        final_auto_by_article: Dict[int, list[Mapping[str, Any]]] = {}
        for row in await conn.fetch(
            """
            SELECT server_id, article_id, term, weight, source, status,
                   suggested_by
            FROM law_synonyms
            WHERE article_id = ANY($1::int[]) AND source = 'auto'
            ORDER BY article_id, term, status, weight, suggested_by
            """,
            sorted(articles_by_id),
        ):
            final_auto_by_article.setdefault(
                int(row["article_id"]),
                [],
            ).append(dict(row))
        desired_output_hashes = {
            article_id: build_enrichment_output_hash(
                server_id=server_id,
                article_id=article_id,
                title=desired_title_states[article_id][0],
                title_provenance=desired_title_states[article_id][1],
                penalty_summary=items_by_id[article_id].get(
                    "penalty_summary"
                ),
                penalty_json=items_by_id[article_id].get("penalty"),
                auto_rows=final_auto_by_article.get(article_id, ()),
            )
            for article_id in articles_by_id
        }

        for article_id in sorted(articles_by_id):
            article = articles_by_id[article_id]
            item = items_by_id[article_id]
            updated_id = await conn.fetchval(
                """
                UPDATE law_articles
                SET title = $2,
                    penalty_summary = $3,
                    penalty_json = $4::jsonb,
                    enrich_hash = $5,
                    title_provenance = $6,
                    enrichment_context_hash = $7,
                    enrichment_output_hash = $8,
                    updated_at = now()
                WHERE id = $1
                  AND server_id = $9
                  AND article_hash = $5
                  AND article_ref IS NOT DISTINCT FROM $10
                  AND kind = $11
                  AND code IS NOT DISTINCT FROM $12
                  AND title IS NOT DISTINCT FROM $13
                  AND full_text = $14
                  AND enrich_hash IS NOT DISTINCT FROM $15
                  AND title_provenance IS NOT DISTINCT FROM $16
                  AND enrichment_context_hash IS NOT DISTINCT FROM $17
                  AND enrichment_output_hash IS NOT DISTINCT FROM $18
                  AND penalty_summary IS NOT DISTINCT FROM $19
                  AND penalty_json IS NOT DISTINCT FROM $20::jsonb
                RETURNING id
                """,
                article_id,
                desired_title_states[article_id][0],
                item.get("penalty_summary"),
                (
                    json.dumps(item.get("penalty"), ensure_ascii=False)
                    if item.get("penalty") is not None
                    else None
                ),
                str(article["article_hash"]),
                desired_title_states[article_id][1],
                context_hashes[article_id],
                desired_output_hashes[article_id],
                server_id,
                article.get("article_ref"),
                str(article.get("kind") or ""),
                article.get("code"),
                article.get("title"),
                str(article.get("full_text") or ""),
                article.get("enrich_hash"),
                article.get("title_provenance"),
                article.get("enrichment_context_hash"),
                article.get("enrichment_output_hash"),
                article.get("penalty_summary"),
                (
                    json.dumps(
                        normalize_penalty_json(
                            article.get("penalty_json")
                        ),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if article.get("penalty_json") is not None
                    else None
                ),
            )
            if updated_id != article_id:
                raise SynonymWriteError(
                    f"article {article_id} metadata/title CAS changed inside "
                    "the server-wide enrichment transaction"
                )
            if await ensure_article_embedding_target(conn, article_id):
                embedding_jobs += 1

        return {
            "articles": len(articles_by_id),
            "synonyms": outcome.result.inserted,
            "embedding_jobs": embedding_jobs,
            "synonym_collisions_rejected": (
                outcome.result.rejected_collisions
            ),
        }


async def _write_auto_enrichment_server(
    server_id: str,
    articles: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    generation_state: Mapping[str, Any],
) -> Dict[str, int]:
    """Commit one fully reviewed server plan or roll back every change."""

    async with db_manager.transaction() as conn:
        return await _write_auto_enrichment_server_in_transaction(
            conn,
            server_id,
            articles,
            items,
            generation_state,
        )


async def begin_enrichment_run(
    server_id: str,
    *,
    invalidation: str = "none",
    source_documents: Optional[Sequence[Mapping[str, Any]]] = None,
) -> EnrichmentPendingClaim:
    """Persist retry ownership before catalog/enrichment mutation or network.

    Caller must hold document→catalog locks.  The transaction claims only
    markers that were false, preserving pre-existing pending work.  Optional
    ``all``/``titleless`` invalidation sets ``enrich_hash=NULL`` atomically
    with the marker so a crash cannot lose a forced rerun.
    """

    if invalidation not in {"none", "all", "titleless"}:
        raise ValueError(
            "invalidation must be one of: none, all, titleless"
        )
    if not isinstance(server_id, str) or not server_id.strip():
        raise ValueError("server_id must be non-empty")

    expected_by_id = None
    if source_documents is not None:
        expected_by_id = {
            int(row["id"]): dict(row)
            for row in source_documents
        }
        if len(expected_by_id) != len(source_documents):
            raise CatalogSourceSnapshotChangedError(
                f"{server_id}: duplicate source document ids"
            )

    async with db_manager.transaction() as conn:
        await acquire_synonym_write_lock(conn, server_id)
        current_rows = await conn.fetch(
            """
            SELECT id, server_id, title, content, content_hash, last_updated,
                   derived_sync_pending
            FROM law_documents
            WHERE server_id = $1
            ORDER BY id
            FOR UPDATE
            """,
            server_id,
        )
        current_by_id = {
            int(row["id"]): dict(row)
            for row in current_rows
        }
        if expected_by_id is not None:
            if set(current_by_id) != set(expected_by_id):
                raise CatalogSourceSnapshotChangedError(
                    f"{server_id}: source document set changed before "
                    "enrichment run"
                )
            for document_id in sorted(expected_by_id):
                _assert_locked_source_document_matches(
                    expected_by_id[document_id],
                    current_by_id[document_id],
                )

        document_ids = sorted(current_by_id)
        if document_ids:
            pending_rows = await conn.fetch(
                """
                UPDATE law_documents
                SET derived_sync_pending = TRUE
                WHERE server_id = $1 AND id = ANY($2::int[])
                RETURNING id
                """,
                server_id,
                document_ids,
            )
            if sorted(int(row["id"]) for row in pending_rows) != document_ids:
                raise RuntimeError(
                    f"{server_id}: could not claim every source document "
                    "for enrichment"
                )

        if invalidation != "none":
            titleless_clause = (
                "AND title IS NULL"
                if invalidation == "titleless"
                else ""
            )
            await conn.execute(
                f"""
                UPDATE law_articles
                SET enrich_hash = NULL
                WHERE server_id = $1 AND kind = 'article'
                  {titleless_clause}
                """,
                server_id,
            )

        claimed = tuple(
            (
                document_id,
                current_by_id[document_id].get("title"),
                current_by_id[document_id].get("content"),
                current_by_id[document_id].get("content_hash"),
                current_by_id[document_id].get("last_updated"),
            )
            for document_id in document_ids
            if current_by_id[document_id].get(
                "derived_sync_pending"
            ) is not True
        )
        return EnrichmentPendingClaim(
            server_id=server_id,
            documents=claimed,
        )


async def enrich_articles_for_server(
    server_id: str,
    only_stale: bool = True,
) -> Dict[str, Any]:
    """Generate, review, preview and atomically apply one server update.

    No model batch is written independently.  The captured catalog, structural
    ancestry, target closure and every auto synonym row are rechecked after all
    remote calls; then synonyms, metadata and local-FRIDA scheduling commit in
    one transaction.
    """
    from google.genai import types
    from rate_limiter import execute_vertex_request, RequestPriority, get_current_client

    stats: Dict[str, Any] = {
        "articles": 0,
        "synonyms": 0,
        "batches_failed": 0,
        # Диагностика поштучной приёмки: сколько годного мы сохранили, отбросив
        # негодное поштучно вместо гибели целого батча.
        "articles_dropped": 0,
        "synonyms_dropped": 0,
        # Расщепление ролей порога: записано, но беднее планки качества.
        "articles_below_quality_target": 0,
        "reenrichment_article_ids": [],
    }
    generation_state = await capture_auto_replacement_generation_state(
        server_id,
        only_stale=only_stale,
    )
    catalog_rows = [
        dict(row)
        for row in generation_state["catalog_rows"]
    ]
    exact_plan = build_exact_duplicate_plan(
        catalog_rows,
        is_substantive_article=lambda row: not (
            is_explicitly_empty_or_repealed_article(row.get("full_text"))
        ),
        canonicalize_scope=canonical_synonym_term,
    )

    current_auto: Dict[int, list[str]] = {}
    for row in generation_state["auto_rows"]:
        if str(row.get("status")) not in {"approved", "stale"}:
            continue
        current_auto.setdefault(int(row["article_id"]), []).append(
            str(row["term"])
        )
    all_articles = []
    for captured in generation_state["articles"]:
        article = dict(captured)
        article["current_auto_synonyms"] = current_auto.get(
            int(article["id"]),
            [],
        )
        all_articles.append(article)

    required_target_ids = set(
        generation_state["snapshot"]["required_target_ids"]
    )
    articles = [
        article
        for article in all_articles
        if int(article["id"]) in required_target_ids
    ]
    if {int(article["id"]) for article in articles} != required_target_ids:
        raise SynonymWriteError(
            "captured required target ids are not complete article rows"
        )
    if not articles:
        return stats

    # Fail before an expensive model call if legacy mixed title provenance
    # cannot be proven from the same parser used by catalog construction.
    for article in articles:
        _kind, _code, source_title = derive_catalog_fields(
            article.get("article_ref"),
            str(article.get("full_text") or ""),
        )
        probe_title = (
            source_title
            if source_title is not None
            else article.get("title")
        )
        _desired_auto_enrichment_title(article, probe_title)

    model = config.LAW_CATALOG_ENRICH_MODEL
    batch_size = config.LAW_ENRICH_BATCH_ARTICLES
    project_context = normalize_enrichment_project_context(
        project_context_for_server(server_id)
    )
    batches = _partition_auto_enrichment_batches(
        articles,
        batch_size,
        exact_plan=exact_plan,
        all_articles=catalog_rows,
        project_context=project_context,
    )
    logger.info(
        "[LAW_CATALOG] server=%s: staged atomic enrichment targets=%d "
        "exact_groups=%d model=%s batches=%d",
        server_id,
        len(articles),
        sum(
            1
            for group in exact_plan.groups
            if not required_target_ids.isdisjoint(group.article_ids)
        ),
        model,
        len(batches),
    )

    reviewed_server_items: list[Dict[str, Any]] = []
    accepted_article_ids: set[int] = set()
    batch_offset = 0

    def _enrichment_payload(articles_subset):
        return json.dumps(
            _build_auto_enrichment_payload(
                articles_subset,
                catalog_rows,
                exact_plan=exact_plan,
            ),
            ensure_ascii=False,
        )

    for batch_index, batch in enumerate(batches):
        i = batch_offset
        batch_offset += len(batch)
        # ``active_batch`` сжимается, если поштучная приёмка пропустила статью:
        # дальнейшие промпты и гейты обязаны видеть ровно уцелевший набор.
        active_batch = list(batch)
        payload = _enrichment_payload(active_batch)

        try:
            quality_feedback = ""
            items = None
            for quality_attempt in range(_AUTO_ENRICH_QUALITY_ATTEMPTS):
                prompt = _ENRICH_PROMPT.format(
                    project_context=project_context,
                    quality_feedback=quality_feedback,
                    articles_json=payload,
                )
                _assert_auto_enrichment_prompt_budget(
                    prompt,
                    batch,
                    stage="generation",
                )

                async def make_request(request_prompt=prompt):
                    client = get_current_client()
                    if not client:
                        raise RuntimeError("genai_client не инициализирован")
                    return await client.aio.models.generate_content(
                        model=model,
                        contents=request_prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.2,
                            response_mime_type="application/json",
                        ),
                    )

                response = await execute_vertex_request(
                    func=make_request, model_name=model,
                    priority=RequestPriority.LOW, max_retries=3,
                )
                raw_items = _parse_json_array(response.text or "")
                if quality_attempt + 1 >= _AUTO_ENRICH_QUALITY_ATTEMPTS:
                    # Попытки исчерпаны: вместо смерти батча принимаем
                    # поштучно.  Последнее слово всё равно за тем же гейтом.
                    #
                    # M3: приёмка это чистый CPU на десятки секунд, и в
                    # корутине она вставала колом ПОПЕРЁК цикла событий —
                    # бот переставал отвечать всем.  Работа уходит в поток;
                    # логика внутри не меняется ни на строку.
                    items, active_batch, _generation_report = (
                        await asyncio.to_thread(
                            _salvage_auto_enrichment_response,
                            raw_items,
                            active_batch,
                            exact_plan=exact_plan,
                            server_id=server_id,
                            stage="generation",
                            stats=stats,
                        )
                    )
                    payload = _enrichment_payload(active_batch)
                    break
                try:
                    # Строгий гейт тоже чистый CPU на исходных текстах и тоже
                    # вставал поперёк цикла событий.  Функция не меняется —
                    # меняется только то, в каком потоке она считается.
                    items = await asyncio.to_thread(
                        _validate_auto_enrichment_response,
                        raw_items,
                        active_batch,
                        exact_plan=exact_plan,
                    )
                    break
                except ValueError as exc:
                    logger.warning(
                        "[LAW_CATALOG] server=%s: ответ Gemini для батча %d-%d "
                        "не прошёл полный quality gate, повторяем: %s",
                        server_id,
                        i,
                        i + len(batch) - 1,
                        exc,
                    )
                    quality_feedback = (
                        "ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН ДО ЗАПИСИ. "
                        f"Исправь весь пакет: {exc}\n"
                    )
            if items is None:
                raise ValueError("Gemini returned no validated enrichment items")

            candidate_json = json.dumps(items, ensure_ascii=False)
            review_feedback = ""
            reviewed_items = None
            for review_attempt in range(_AUTO_ENRICH_QUALITY_ATTEMPTS):
                review_prompt = _ENRICH_REVIEW_PROMPT.format(
                    project_context=project_context,
                    quality_feedback=review_feedback,
                    articles_json=payload,
                    candidate_json=candidate_json,
                )
                _assert_auto_enrichment_prompt_budget(
                    review_prompt,
                    batch,
                    stage="review",
                )

                async def make_review_request(request_prompt=review_prompt):
                    client = get_current_client()
                    if not client:
                        raise RuntimeError("genai_client не инициализирован")
                    return await client.aio.models.generate_content(
                        model=model,
                        contents=request_prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.1,
                            response_mime_type="application/json",
                        ),
                    )

                review_response = await execute_vertex_request(
                    func=make_review_request,
                    model_name=model,
                    priority=RequestPriority.LOW,
                    max_retries=3,
                )
                raw_reviewed = _parse_json_array(review_response.text or "")
                if review_attempt + 1 >= _AUTO_ENRICH_QUALITY_ATTEMPTS:
                    reviewed_items, active_batch, _review_report = (
                        await asyncio.to_thread(
                            _salvage_auto_enrichment_response,
                            raw_reviewed,
                            active_batch,
                            exact_plan=exact_plan,
                            server_id=server_id,
                            stage="review",
                            stats=stats,
                        )
                    )
                    break
                try:
                    reviewed_items = await asyncio.to_thread(
                        _validate_auto_enrichment_response,
                        raw_reviewed,
                        active_batch,
                        exact_plan=exact_plan,
                    )
                    break
                except ValueError as exc:
                    logger.warning(
                        "[LAW_CATALOG] server=%s: контрольный ответ Gemini для "
                        "батча %d-%d не прошёл quality gate, повторяем: %s",
                        server_id,
                        i,
                        i + len(batch) - 1,
                        exc,
                    )
                    review_feedback = (
                        "ПРЕДЫДУЩАЯ ПРОВЕРКА ОТКЛОНЕНА ДО ЗАПИСИ. "
                        f"Верни исправленный полный пакет: {exc}\n"
                    )
            if reviewed_items is None:
                raise ValueError("Gemini review returned no validated items")
            items = reviewed_items
        except Exception:
            # M1.  Здесь стоял ``return stats``, и это выходило из ВСЕЙ
            # функции, а не из цикла: первый же неудачный батч обрывал
            # прогон, а запись стоит ПОСЛЕ цикла — значит сервер оставался
            # без единой строки.  Радиус одного негодного синонима был не
            # батч, а весь сервер.
            #
            # Заказчик решение отменил: ЧАСТИЧНАЯ ЗАПИСЬ РАЗРЕШЕНА, потому
            # что она возобновляема ПО ПОСТРОЕНИЮ — выборка идёт с
            # ``only_stale=True``, свежесть считается по хешам статьи,
            # контекста и вывода, поэтому записанная статья перестаёт быть
            # устаревшей, а пропущенная остаётся ею и придёт следующим
            # прогоном.
            #
            # Маркер повтора при этом ОБЯЗАТЕЛЕН и сохранён: ``batches_failed``
            # растёт, а ``require_successful_enrichment_completion`` по нему
            # ставит ``derived_sync_pending`` и всё равно падает — сервер
            # честно считается незавершённым, пока остаток не докатится.
            stats["batches_failed"] += 1
            logger.exception(
                "[LAW_CATALOG] server=%s: staged batch %d-%d failed; "
                "его статьи пропущены и останутся устаревшими до "
                "следующего прогона, остальные батчи продолжаются",
                server_id,
                i,
                i + len(batch) - 1,
            )
            continue

        reviewed_server_items.extend(reviewed_items)
        accepted_article_ids.update(
            int(article["id"]) for article in active_batch
        )
        if batch_index + 1 < len(batches):
            await asyncio.sleep(
                max(
                    0.0,
                    float(config.LAW_ENRICH_INTER_BATCH_DELAY_SECONDS),
                )
            )

    if not accepted_article_ids:
        # Считать провал ВТОРОЙ раз нельзя: цикл уже посчитал каждый упавший
        # батч поимённо.  Лишний инкремент не менял поведения (маркер повтора
        # смотрит на истинность), но врал в отчёте — один провал выглядел
        # двумя.  Инкремент остаётся только для случая, когда цикл не посчитал
        # ничего, а принять всё равно оказалось нечего.
        if not stats["batches_failed"]:
            stats["batches_failed"] += 1
        logger.error(
            "[LAW_CATALOG] server=%s: ни одна статья не пережила поштучную "
            "приёмку; сервер остаётся незаписанным",
            server_id,
        )
        return stats
    if accepted_article_ids != {int(article["id"]) for article in articles}:
        # Поштучная приёмка пропустила часть статей.  Писатель требует, чтобы
        # staged-набор ТОЧНО совпадал с ``required_target_ids`` снимка,
        # поэтому уменьшенный набор обязан получить собственный снимок: иначе
        # запись будет честно отвергнута как рассинхронизированная.  Пропущенные
        # статьи остаются неполными и будут переспрошены следующим прогоном.
        skipped = sorted(
            {int(article["id"]) for article in articles} - accepted_article_ids
        )
        logger.warning(
            "[LAW_CATALOG] server=%s: пишем %d статей из %d, пропущены %s",
            server_id,
            len(accepted_article_ids),
            len(articles),
            skipped,
        )
        try:
            generation_state = (
                await capture_auto_replacement_generation_state_for_targets(
                    server_id,
                    sorted(accepted_article_ids),
                )
            )
        except Exception:
            stats["batches_failed"] += 1
            logger.exception(
                "[LAW_CATALOG] server=%s: не удалось пересобрать снимок под "
                "уцелевший набор статей; сервер остаётся незаписанным",
                server_id,
            )
            return stats
        catalog_rows = [
            dict(row) for row in generation_state["catalog_rows"]
        ]
        current_by_id = {
            int(captured["id"]): captured
            for captured in generation_state["articles"]
        }
        missing_after_recapture = sorted(
            accepted_article_ids - set(current_by_id)
        )
        if missing_after_recapture:
            stats["batches_failed"] += 1
            logger.error(
                "[LAW_CATALOG] server=%s: пересобранный снимок не содержит "
                "статей %s; сервер остаётся незаписанным",
                server_id,
                missing_after_recapture,
            )
            return stats
        articles = [
            dict(
                current_by_id[article_id],
                current_auto_synonyms=current_auto.get(article_id, []),
            )
            for article_id in sorted(accepted_article_ids)
        ]
        exact_plan = build_exact_duplicate_plan(
            catalog_rows,
            is_substantive_article=lambda row: not (
                is_explicitly_empty_or_repealed_article(row.get("full_text"))
            ),
            canonicalize_scope=canonical_synonym_term,
        )

    try:
        # Repeat validation over the combined output.  This is the gate that
        # detects inter-batch canonical ownership collisions before any DB
        # mutation.
        reviewed_server_items = await asyncio.to_thread(
            _validate_auto_enrichment_response,
            reviewed_server_items,
            articles,
            exact_plan=exact_plan,
        )
        article_by_id = {
            int(article["id"]): article for article in articles
        }
        fingerprints = generation_state["auto_fingerprints"]
        replacement_items = [
            {
                "id": int(item["id"]),
                "article_hash": str(
                    article_by_id[int(item["id"])]["article_hash"]
                ),
                "expected_enrich_hash": article_by_id[
                    int(item["id"])
                ].get("enrich_hash"),
                "expected_auto_synonym_fingerprint": fingerprints[
                    int(item["id"])
                ],
                "synonyms": list(item.get("synonyms") or ()),
            }
            for item in reviewed_server_items
        ]
        preview = await preview_auto_synonym_replacements(
            server_id,
            replacement_items,
            generation_snapshot=generation_state["snapshot"],
        )
        if preview.rejected_collisions:
            raise SynonymWriteError(
                "server-wide read-only preview found synonym collisions"
            )
        preview_errors = []
        # РАСЩЕПЛЕНИЕ РОЛЕЙ.  Пол записи и планка качества — две разные
        # работы, и с этого места они разведены: ниже пола писать нельзя,
        # а нехватка до планки только помечает статью на переобогащение.
        below_quality_target: list[Tuple[int, int, int]] = []
        for article_id, article in article_by_id.items():
            tombstone = is_explicitly_empty_or_repealed_article(
                article.get("full_text")
            )
            physical = int(preview.physical_after.get(article_id, 0))
            effective = int(preview.effective_after.get(article_id, 0))
            if tombstone:
                if physical or effective:
                    preview_errors.append(
                        f"{article_id}: tombstone={physical}/{effective}"
                    )
            elif (
                physical < _AUTO_ENRICH_MIN_PHYSICAL
                or effective < _AUTO_ENRICH_MIN_EFFECTIVE
            ):
                preview_errors.append(
                    f"{article_id}: physical/effective="
                    f"{physical}/{effective}"
                )
            elif (
                physical < _AUTO_ENRICH_QUALITY_TARGET
                or effective < _AUTO_ENRICH_QUALITY_TARGET
            ):
                below_quality_target.append(
                    (article_id, physical, effective)
                )
        if preview_errors:
            raise SynonymWriteError(
                "server-wide read-only quality preview failed: "
                + ", ".join(preview_errors)
            )

        write_stats = await _write_auto_enrichment_server(
            server_id,
            articles,
            reviewed_server_items,
            generation_state,
        )
    except Exception:
        stats["batches_failed"] += 1
        logger.exception(
            "[LAW_CATALOG] server=%s: combined preview/write failed; "
            "the server transaction was rolled back",
            server_id,
        )
        return stats

    # Пометка на переобогащение выставляется ТОЛЬКО после успешной записи:
    # это признак записанной, но бедной статьи, а не причина её не писать.
    # Статья находима уже сейчас; кампания переобогащения поднимет её до
    # планки отдельным прогоном по этому списку.
    stats["articles_below_quality_target"] = len(below_quality_target)
    stats["reenrichment_article_ids"] = [
        article_id for article_id, _physical, _effective in
        sorted(below_quality_target)
    ]
    if below_quality_target:
        logger.warning(
            "[LAW_CATALOG] server=%s: REENRICH_PENDING статей=%d "
            "(записаны, но ниже планки качества %d): %s",
            server_id,
            len(below_quality_target),
            _AUTO_ENRICH_QUALITY_TARGET,
            ", ".join(
                f"{article_id}:{physical}/{effective}"
                for article_id, physical, effective in
                sorted(below_quality_target)
            ),
        )
    stats["articles"] = write_stats["articles"]
    stats["synonyms"] = write_stats["synonyms"]
    stats["synonym_collisions_rejected"] = write_stats.get(
        "synonym_collisions_rejected",
        0,
    )
    logger.info(
        "[LAW_CATALOG] server=%s: atomic enrichment committed articles=%d "
        "synonyms=%d embedding_jobs=%d",
        server_id,
        stats["articles"],
        stats["synonyms"],
        write_stats.get("embedding_jobs", 0),
    )
    return stats


async def _mark_enrichment_pending_in_transaction(
    conn,
    server_id: str,
    document_ids: Optional[Sequence[int]] = None,
) -> Tuple[int, ...]:
    """Retain retry work on ``conn``; ``None`` means every server document."""

    if document_ids is None:
        rows = await conn.fetch(
            """
            UPDATE law_documents
            SET derived_sync_pending = TRUE
            WHERE server_id = $1
            RETURNING id
            """,
            server_id,
        )
        return tuple(sorted(int(row["id"]) for row in rows))

    normalized = sorted({int(value) for value in document_ids})
    if not normalized:
        return ()
    rows = await conn.fetch(
        """
        UPDATE law_documents
        SET derived_sync_pending = TRUE
        WHERE server_id = $1 AND id = ANY($2::int[])
        RETURNING id
        """,
        server_id,
        normalized,
    )
    updated = sorted(int(row["id"]) for row in rows)
    if updated != normalized:
        raise RuntimeError(
            f"{server_id}: could not retain every incomplete "
            "document pending"
        )
    return tuple(updated)


async def _mark_enrichment_pending(
    server_id: str,
    document_ids: Optional[Sequence[int]] = None,
) -> Tuple[int, ...]:
    """Durably retain retry work; ``None`` means every server document."""

    async with db_manager.transaction() as conn:
        return await _mark_enrichment_pending_in_transaction(
            conn,
            server_id,
            document_ids,
        )


async def _clear_enrichment_pending_claim_in_transaction(
    conn,
    claim: EnrichmentPendingClaim,
) -> None:
    """Clear this run's markers on ``conn`` and only on the same source."""

    if not claim.documents:
        return
    for (
        document_id,
        title,
        content,
        content_hash,
        last_updated,
    ) in claim.documents:
        cleared_id = await conn.fetchval(
            """
            UPDATE law_documents
            SET derived_sync_pending = FALSE
            WHERE id = $1 AND server_id = $2
              AND title IS NOT DISTINCT FROM $3
              AND content IS NOT DISTINCT FROM $4
              AND content_hash IS NOT DISTINCT FROM $5
              AND last_updated IS NOT DISTINCT FROM $6
              AND derived_sync_pending IS TRUE
            RETURNING id
            """,
            document_id,
            claim.server_id,
            title,
            content,
            content_hash,
            last_updated,
        )
        if cleared_id != document_id:
            raise CatalogSourceSnapshotChangedError(
                f"{claim.server_id}: source/pending claim changed for "
                f"document {document_id}"
            )


async def _clear_enrichment_pending_claim(
    claim: EnrichmentPendingClaim,
) -> None:
    """Clear only markers this run created and only on the same source."""

    async with db_manager.transaction() as conn:
        await _clear_enrichment_pending_claim_in_transaction(conn, claim)


async def require_successful_enrichment_completion(
    server_id: str,
    stats: Mapping[str, Any],
    *,
    pending_claim: Optional[EnrichmentPendingClaim] = None,
):
    """Fail closed, durably scheduling retry, unless the server is complete."""

    invalid_stats = not isinstance(stats, Mapping)
    batches_failed = stats.get("batches_failed") if not invalid_stats else None
    invalid_batches = (
        isinstance(batches_failed, bool)
        or not isinstance(batches_failed, int)
        or batches_failed < 0
    )
    if invalid_stats or invalid_batches or batches_failed:
        await _mark_enrichment_pending(server_id)
        if invalid_stats:
            reason = "no structured statistics"
        elif invalid_batches:
            reason = "batches_failed is missing or invalid"
        else:
            reason = f"failed batches={batches_failed}"
        raise SynonymWriteError(f"{server_id}: enrichment {reason}")
    if pending_claim is not None and pending_claim.server_id != server_id:
        await _mark_enrichment_pending(server_id)
        raise SynonymWriteError(
            "enrichment pending claim belongs to another server"
        )

    from services.law_enrichment_completion import (
        evaluate_law_enrichment_completion,
    )

    failure_message = None
    try:
        async with db_manager.transaction() as conn:
            # The external document→catalog locks freeze source/catalog rows.
            # This xact-lock freezes every supported enrichment/synonym writer
            # through the completion snapshot and owned-marker clear.
            await acquire_synonym_write_lock(conn, server_id)
            completion = await evaluate_law_enrichment_completion(
                conn,
                server_id,
            )
            if completion.articles <= 0:
                await _mark_enrichment_pending_in_transaction(
                    conn,
                    server_id,
                )
                failure_message = (
                    f"{server_id}: enrichment cannot complete an empty "
                    "article corpus"
                )
            elif not completion.complete:
                incomplete_document_ids = tuple(
                    completion.incomplete_document_ids
                )
                await _mark_enrichment_pending_in_transaction(
                    conn,
                    server_id,
                    incomplete_document_ids or None,
                )
                failure_message = (
                    f"{server_id}: enrichment finished with incomplete "
                    "article ids "
                    f"{list(completion.incomplete_article_ids)}"
                )
            elif pending_claim is not None:
                await _clear_enrichment_pending_claim_in_transaction(
                    conn,
                    pending_claim,
                )
    except Exception as exc:
        await _mark_enrichment_pending(server_id)
        raise SynonymWriteError(
            f"{server_id}: authoritative enrichment completion/commit failed"
        ) from exc
    if failure_message is not None:
        raise SynonymWriteError(failure_message)
    return completion


async def run_verified_enrichment_for_server(
    server_id: str,
    *,
    only_stale: bool,
    pending_claim: Optional[EnrichmentPendingClaim] = None,
) -> Dict[str, Any]:
    """Run autonomous enrichment and enforce durable full-server success."""

    try:
        stats = await enrich_articles_for_server(
            server_id,
            only_stale=only_stale,
        )
    except Exception:
        await _mark_enrichment_pending(server_id)
        raise
    await require_successful_enrichment_completion(
        server_id,
        stats,
        pending_claim=pending_claim,
    )
    return stats


async def _write_enrichment(
    server_id: str,
    article_id: int,
    summary,
    penalty,
    syns,
    stats,
    *,
    expected_article_hash: str,
    title: Optional[str] = None,
    signature_registry: Optional[ExactSignatureRegistry] = None,
    minimum_synonym_physical: int = MIN_REPLACEMENT_PHYSICAL,
    minimum_synonym_effective: int = MIN_REPLACEMENT_EFFECTIVE,
) -> bool:
    """Запись результата обогащения одной статьи (транзакция: penalty + title-фолбэк + автосинонимы).

    title пишется ТОЛЬКО если у статьи его ещё нет (заголовок/эвристика приоритетнее LLM).
    """
    async with db_manager.transaction() as conn:
        return await _write_enrichment_in_transaction(
            conn,
            server_id,
            article_id,
            summary,
            penalty,
            syns,
            stats,
            expected_article_hash=expected_article_hash,
            title=title,
            signature_registry=signature_registry,
            synonym_lock_already_held=False,
            minimum_synonym_physical=minimum_synonym_physical,
            minimum_synonym_effective=minimum_synonym_effective,
        )


async def _write_enrichment_in_transaction(
    conn,
    server_id: str,
    article_id: int,
    summary,
    penalty,
    syns,
    stats,
    *,
    expected_article_hash: str,
    title: Optional[str] = None,
    signature_registry: Optional[ExactSignatureRegistry] = None,
    synonym_lock_already_held: bool = False,
    minimum_synonym_physical: int = MIN_REPLACEMENT_PHYSICAL,
    minimum_synonym_effective: int = MIN_REPLACEMENT_EFFECTIVE,
    replace_title_state: bool = False,
    desired_title_provenance: Optional[str] = None,
    enrichment_context_hash: Optional[str] = None,
) -> bool:
    """Transaction-owned variant for all-or-none reviewed artifacts."""

    if replace_title_state:
        if (
            desired_title_provenance is not None
            and desired_title_provenance not in _TITLE_PROVENANCE_VALUES
        ):
            raise SynonymWriteError("invalid reviewed title provenance")
        if (
            not isinstance(enrichment_context_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", enrichment_context_hash) is None
        ):
            raise SynonymWriteError(
                "reviewed enrichment requires a valid context hash"
            )
    if not synonym_lock_already_held:
        await acquire_synonym_write_lock(conn, server_id)
    article_before = await conn.fetchrow(
        """
        SELECT id, article_hash, enrich_hash, title, title_provenance,
               enrichment_context_hash
        FROM law_articles
        WHERE id = $1 AND server_id = $2 AND kind = 'article'
          AND article_hash = $3
        FOR UPDATE
        """,
        article_id,
        server_id,
        expected_article_hash,
    )
    if article_before is None:
        return False
    prior_enrichment_current = (
        str(article_before["enrich_hash"] or "")
        == str(article_before["article_hash"])
    )
    updated_id = await conn.fetchval(
        """
        UPDATE law_articles
        SET penalty_summary = $2, penalty_json = $3::jsonb,
            title = CASE
                WHEN $7::boolean THEN $4
                ELSE COALESCE(title, $4)
            END,
            title_provenance = CASE
                WHEN $7::boolean THEN $8
                ELSE title_provenance
            END,
            enrichment_context_hash = CASE
                WHEN $7::boolean THEN $9
                ELSE enrichment_context_hash
            END,
            enrich_hash = $6,
            enrichment_output_hash = NULL,
            updated_at = now()
        WHERE id = $1 AND server_id = $5 AND article_hash = $6
        RETURNING id
        """,
        article_id,
        summary,
        json.dumps(penalty, ensure_ascii=False) if penalty else None,
        title,
        server_id,
        expected_article_hash,
        replace_title_state,
        desired_title_provenance,
        enrichment_context_hash,
    )
    if updated_id != article_id:
        return False
    await ensure_article_embedding_target(conn, article_id)
    synonym_result = await replace_auto_synonyms(
        conn,
        server_id,
        article_id,
        expected_article_hash,
        syns,
        registry=signature_registry,
        lock_already_held=True,
        prior_enrichment_current=prior_enrichment_current,
        minimum_physical=minimum_synonym_physical,
        minimum_effective=minimum_synonym_effective,
    )
    stats["synonyms"] += synonym_result.inserted
    stats["synonym_collisions_rejected"] = (
        stats.get("synonym_collisions_rejected", 0)
        + synonym_result.rejected_collisions
    )
    return True


# --- Матрица «ещё релевантное» ---------------------------------------------------------------

async def rebuild_related_for_server(server_id: str, space_id: Optional[str] = None) -> int:
    """Пересчёт related для одного явно заданного FRIDA-пространства.

    Gemini/cloud-centroid здесь больше не участвует: разные пространства нельзя
    смешивать, а Overlay использует только active local space.
    """
    top_n = config.LAW_RELATED_TOP_N
    min_score = config.LAW_RELATED_MIN_SCORE
    vector_space = space_id or active_space_id()
    timeout_ms = max(1, int(float(config.LAW_RELATED_REBUILD_TIMEOUT) * 1000))
    async with db_manager.transaction() as conn:
        await conn.execute(
            "SELECT set_config('statement_timeout', $1, true)", f"{timeout_ms}ms"
        )
        await conn.execute(
            "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"
        )
        await conn.execute(
            "DELETE FROM law_article_related USING law_articles a "
            "WHERE law_article_related.article_id = a.id AND a.server_id = $1 "
            "AND law_article_related.vector_space = $2",
            server_id, vector_space,
        )
        status = await conn.execute(
            """
            INSERT INTO law_article_related (article_id, related_id, vector_space, score)
            SELECT a.id, r.id, $2, r.score
            FROM law_articles a
            JOIN law_article_embeddings ae
              ON ae.article_id = a.id AND ae.space_id = $2
            CROSS JOIN
            LATERAL (
                SELECT b.id, (1 - (be.embedding <=> ae.embedding))::real AS score
                FROM law_article_embeddings be
                JOIN law_articles b ON b.id = be.article_id
                WHERE be.space_id = $2
                  AND b.server_id = a.server_id AND b.id <> a.id
                  AND b.kind = 'article'
                ORDER BY be.embedding <=> ae.embedding
                LIMIT $3
            ) r
            WHERE a.server_id = $1 AND a.kind = 'article' AND r.score >= $4
            ON CONFLICT (article_id, vector_space, related_id) DO NOTHING
            """,
            server_id, vector_space, top_n, min_score,
        )
    try:
        inserted = int(str(status).split()[-1])
    except (ValueError, IndexError):
        inserted = 0
    logger.info(
        "[LAW_CATALOG] server=%s space=%s: матрица related пересчитана (%d связей)",
        server_id, vector_space, inserted,
    )
    return inserted


async def _rebuild_all_related_spaces(server_id: str) -> Dict[str, int]:
    """Восстанавливает графы всех сохранённых пространств после смены article id.

    Catalog sync использует DELETE+INSERT, поэтому CASCADE удаляет часть related не
    только active space. Пересчёт всех имеющихся пространств сохраняет реальный откат.
    """
    rows = await db_manager.fetch(
        """
        SELECT DISTINCT e.space_id
        FROM law_article_embeddings e
        WHERE e.server_id = $1
          AND NOT EXISTS (
              SELECT 1
              FROM law_embedding_jobs j
              WHERE j.server_id = e.server_id AND j.space_id = e.space_id
                AND (
                    j.status <> 'failed'
                    OR j.attempts < $2
                )
          )
        ORDER BY e.space_id
        """,
        server_id,
        int(getattr(config, "LAW_EMBED_JOB_MAX_ATTEMPTS", 5)),
    )
    result: Dict[str, int] = {}
    for row in rows:
        result[row["space_id"]] = await rebuild_related_for_server(
            server_id, space_id=row["space_id"],
        )
    return result


async def _queued_related_spaces(server_id: str) -> List[str]:
    """Пространства, граф которых надёжно поставлен в очередь API-worker-а."""
    rows = await db_manager.fetch(
        "SELECT space_id FROM law_related_rebuild_jobs "
        "WHERE server_id = $1 ORDER BY space_id",
        server_id,
    )
    return [row["space_id"] for row in rows]


# --- Оркестраторы -----------------------------------------------------------------------------

async def build_catalog_for_server(
    server_id: str,
    enrich: bool = True,
    retitle: bool = False,
    reenrich: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Self-owned full build over one immutable source generation.

    Lock order matches the normal forum updater: source documents first,
    catalog second.  Both remain held through source fetch, every reconcile,
    optional enrichment and the final report, so a successful report cannot
    combine catalog rows from different source generations.
    """

    async with law_document_server_lock(server_id, manager=db_manager):
        async with catalog_server_lock(server_id):
            return await _build_catalog_for_server(
                server_id,
                enrich=enrich,
                retitle=retitle,
                reenrich=reenrich,
                dry_run=dry_run,
            )


async def _build_catalog_for_server(
    server_id: str,
    enrich: bool = True,
    retitle: bool = False,
    reenrich: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Caller-locked full build: статьи → FRIDA jobs → enrichment → related.

    retitle=True: ПОСЛЕ пересборки (когда эвристика уже раздала названия со вторых строк)
    сбрасывает enrich_hash статей, оставшихся без title, — обогащение перегенерит их
    с новым промптом (разовый догон названий).
    reenrich=True: ДО пересборки сбрасывает enrich_hash ВСЕХ статей сервера — полная
    перегенерация penalty+синонимов новым промптом. Старые данные остаются доступными,
    пока успешный ответ не заменит их атомарно; неудачный LLM-батч поиск не обедняет.

    The caller must hold ``law_document_server_lock`` and then
    ``catalog_server_lock`` for the entire call.  This private entry exists for
    the all-server backfill, which already owns both locks.
    """
    if (retitle or reenrich) and not enrich:
        raise ValueError(
            "retitle/reenrich require enrich=True; refusing to invalidate "
            "enrichment without generation and completion"
        )
    docs = await db_manager.fetch(
        "SELECT id, server_id, title, content, content_hash, last_updated "
        "FROM law_documents WHERE server_id = $1",
        server_id,
    )
    # Validate the complete source set before dry-run planning, reenrich
    # invalidation, or the first per-document transaction. One ambiguous
    # parser result must not leave a half-reconciled server.
    for row in docs:
        source_doc = dict(row)
        content = source_doc.get("content")
        if content:
            _assert_catalog_mutation_safe(
                str(content),
                doc_id=source_doc.get("id"),
            )
    if dry_run:
        if retitle or reenrich:
            raise ValueError("dry-run нельзя совмещать с retitle/reenrich")
        plan = {
            "articles": 0,
            "preserved": 0,
            "inserted": 0,
            "deleted": 0,
            "ambiguous": 0,
        }
        for row in docs:
            doc_plan = await preview_catalog_for_document(dict(row))
            for key in plan:
                plan[key] += doc_plan[key]
        related_rebuild_queued = await _queued_related_spaces(server_id)
        queued = await db_manager.fetchval(
            "SELECT count(*) FROM law_embedding_jobs "
            "WHERE server_id = $1 AND space_id = $2",
            server_id,
            active_space_id(),
        )
        summary = {
            "documents": len(docs),
            **plan,
            "dry_run": True,
            "related_rebuild_queued": related_rebuild_queued,
            "embedding_jobs": int(queued or 0),
            "enrich": None,
        }
        logger.info("[LAW_CATALOG] server=%s: dry-run %s", server_id, summary)
        return summary

    pending_claim: Optional[EnrichmentPendingClaim] = None
    if enrich:
        invalidation = (
            "all"
            if reenrich
            else "titleless"
            if retitle
            else "none"
        )
        pending_claim = await begin_enrichment_run(
            server_id,
            invalidation=invalidation,
            source_documents=[dict(row) for row in docs],
        )
        logger.info(
            "[LAW_CATALOG] server=%s: durable enrichment run started "
            "(invalidation=%s, newly_pending=%d)",
            server_id,
            invalidation,
            len(pending_claim.documents),
        )
    articles = 0
    failed_documents: List[int] = []
    for row in docs:
        doc = dict(row)
        try:
            articles += await _build_catalog_for_document_locked(doc)
        except Exception:
            logger.exception("[LAW_CATALOG] ошибка каталогизации документа %s", doc.get("id"))
            failed_documents.append(int(doc["id"]))
    if failed_documents:
        raise RuntimeError(
            f"server {server_id}: catalog reconcile failed for documents {failed_documents}"
        )
    enrich_stats = None
    if enrich:
        enrich_stats = await run_verified_enrichment_for_server(
            server_id,
            only_stale=True,
            pending_claim=pending_claim,
        )
    # Не считаем граф здесь второй раз: точечные invalidation/vector writes уже поставили
    # durable marker, а API-worker дождётся полного дренирования FRIDA jobs.
    related_rebuild_queued = await _queued_related_spaces(server_id)
    queued = await db_manager.fetchval(
        "SELECT count(*) FROM law_embedding_jobs WHERE server_id = $1 AND space_id = $2",
        server_id, active_space_id(),
    )
    summary = {"documents": len(docs), "articles": articles,
               "related": 0, "related_spaces": {},
               "related_rebuild_queued": related_rebuild_queued,
               "embedding_jobs": int(queued or 0), "enrich": enrich_stats,
               "failed_documents": 0}
    logger.info("[LAW_CATALOG] server=%s: сборка завершена %s", server_id, summary)
    return summary


async def sync_catalog_for_documents(
    server_id: str,
    doc_ids: List[int],
    *,
    document_lock_already_held: bool = False,
) -> Optional[Dict[str, Any]]:
    """Synchronize changed docs under an explicit source-lock contract.

    Standalone callers get the global ``document -> catalog`` locks.  The
    knowledge-base updater already owns the session document lock and must set
    ``document_lock_already_held=True`` so it does not deadlock by trying to
    re-acquire that lock on another pooled connection.
    """
    if not isinstance(document_lock_already_held, bool):
        raise TypeError("document_lock_already_held must be bool")
    if not doc_ids:
        return None
    if document_lock_already_held:
        async with catalog_server_lock(server_id):
            return await _sync_catalog_for_documents(server_id, doc_ids)
    async with law_document_server_lock(server_id, manager=db_manager):
        async with catalog_server_lock(server_id):
            return await _sync_catalog_for_documents(server_id, doc_ids)


async def _sync_catalog_for_documents(server_id: str, doc_ids: List[int]) -> Optional[Dict[str, Any]]:
    """Caller-locked incremental build for changed knowledge-base documents.

    Гейт: работает только если каталог сервера уже построен (есть строки law_articles) —
    первичная сборка сервера делается backfill-скриптом. Поисковые embeddings law_chunks
    здесь не используются: для Overlay существует отдельное пространство FRIDA.
    """
    if not doc_ids:
        return None
    requested_doc_ids = list(dict.fromkeys(int(doc_id) for doc_id in doc_ids))
    if not getattr(config, "LAW_CATALOG_AUTO_SYNC", True):
        logger.info("[LAW_CATALOG] авто-синк выключен (LAW_CATALOG_AUTO_SYNC) — пропуск")
        return None
    has_catalog = await db_manager.fetchval(
        "SELECT EXISTS (SELECT 1 FROM law_articles WHERE server_id = $1)", server_id
    )
    if not has_catalog:
        logger.info("[LAW_CATALOG] server=%s без каталога — авто-синк пропущен (%d док.)",
                    server_id, len(doc_ids))
        return None

    docs = await db_manager.fetch(
        "SELECT id, server_id, title, content, content_hash, last_updated "
        "FROM law_documents "
        "WHERE server_id = $1 AND id = ANY($2)",
        server_id, requested_doc_ids,
    )
    # Parser ambiguity is different from an ordinary per-document runtime
    # failure: it proves that the requested source batch cannot be reconciled
    # safely. Validate the complete batch before the first catalog transaction
    # or server-wide enrichment. Otherwise one ambiguous document could retain
    # its old catalog yet still trigger unrelated synonym/FRIDA mutations.
    parse_ambiguities: List[tuple[int, str]] = []
    for row in docs:
        source_doc = dict(row)
        content = source_doc.get("content")
        if not content:
            continue
        try:
            _assert_catalog_mutation_safe(
                str(content),
                doc_id=source_doc.get("id"),
            )
        except CatalogParseAmbiguityError as exc:
            parse_ambiguities.append((int(source_doc["id"]), str(exc)))
    if parse_ambiguities:
        for doc_id, reason in parse_ambiguities:
            logger.error(
                "[LAW_CATALOG] авто-синк: неоднозначный parser doc=%s: %s",
                doc_id,
                reason,
            )
        related_rebuild_queued = await _queued_related_spaces(server_id)
        queued = await db_manager.fetchval(
            "SELECT count(*) FROM law_embedding_jobs "
            "WHERE server_id = $1 AND space_id = $2",
            server_id,
            active_space_id(),
        )
        summary = {
            "documents": len(docs),
            "articles": 0,
            "related": 0,
            "related_spaces": {},
            "related_rebuild_queued": related_rebuild_queued,
            "embedding_jobs": int(queued or 0),
            "enrich": None,
            "succeeded_document_ids": [],
            # Retain the durable marker for the whole requested batch. This is
            # all-or-nothing only for proven parse ambiguity; ordinary runtime
            # failures keep the established per-document behavior below.
            "failed_document_ids": requested_doc_ids,
            "failed_documents": len(requested_doc_ids),
        }
        logger.info(
            "[LAW_CATALOG] server=%s: parser ambiguity retained pending %s",
            server_id,
            summary,
        )
        return summary

    auto_enrich = bool(getattr(config, "LAW_CATALOG_AUTO_ENRICH", True))
    articles = 0
    succeeded_document_ids: List[int] = []
    failed_document_ids: List[int] = []
    found_document_ids = {int(dict(row)["id"]) for row in docs}
    # A requested document that disappeared or moved between the caller snapshot
    # and this read is deliberately not reported as successful.
    failed_document_ids.extend(
        doc_id for doc_id in requested_doc_ids
        if doc_id not in found_document_ids
    )
    for row in docs:
        doc = dict(row)
        try:
            articles += await _build_catalog_for_document_locked(doc)
        except Exception:
            logger.exception("[LAW_CATALOG] авто-синк: ошибка каталогизации документа %s", doc.get("id"))
            failed_document_ids.append(int(doc["id"]))
        else:
            succeeded_document_ids.append(int(doc["id"]))
    enrich_stats = None
    enrichment_raised = False
    if auto_enrich:
        try:
            enrich_stats = await enrich_articles_for_server(server_id, only_stale=True)
        except Exception:
            enrichment_raised = True
            logger.exception("[LAW_CATALOG] авто-синк: обогащение упало (каталог собран без него)")
    # Completion is unconditional even when the Gemini kill-switch is off.
    # Reconcile may have invalidated body/context/output state; clearing the
    # durable source marker in that state would lose the automatic retry.
    # ``enrich_articles_for_server`` also intentionally continues after a bad
    # model batch, so its aggregate success cannot replace this full probe.
    try:
        from services.law_enrichment_completion import (
            evaluate_law_enrichment_completion,
        )

        completion = await evaluate_law_enrichment_completion(
            db_manager,
            server_id,
        )
        # A changed/deleted article can alter exact-body topology or
        # competitors for an unchanged article in another document.  A
        # document-scoped probe is therefore not a completion proof: a
        # now-structural-only changed doc would be vacuously complete while
        # leaving cross-document context stale with no retry anchor.
        incomplete_enrichment_ids = (
            set()
            if completion.complete
            else set(succeeded_document_ids)
        )
    except Exception:
        logger.exception(
            "[LAW_CATALOG] авто-синк: не удалось доказать полноту "
            "обогащения; pending сохранён"
        )
        incomplete_enrichment_ids = set(succeeded_document_ids)
    if (
        enrichment_raised
        or int((enrich_stats or {}).get("batches_failed") or 0) > 0
    ):
        # The exception may have happened before/inside a batch whose rows
        # are not safely attributable from aggregate stats.  A returned
        # batches_failed count has the same meaning: fail closed for every
        # otherwise successful requested document so the durable marker
        # triggers a complete server retry.
        incomplete_enrichment_ids.update(succeeded_document_ids)
    if incomplete_enrichment_ids:
        failed_seen = set(failed_document_ids)
        for doc_id in requested_doc_ids:
            if (
                doc_id in incomplete_enrichment_ids
                and doc_id not in failed_seen
            ):
                failed_document_ids.append(doc_id)
                failed_seen.add(doc_id)
        succeeded_document_ids = [
            doc_id
            for doc_id in succeeded_document_ids
            if doc_id not in incomplete_enrichment_ids
        ]
    related_rebuild_queued = await _queued_related_spaces(server_id)
    queued = await db_manager.fetchval(
        "SELECT count(*) FROM law_embedding_jobs WHERE server_id = $1 AND space_id = $2",
        server_id, active_space_id(),
    )
    summary = {"documents": len(docs), "articles": articles,
               "related": 0, "related_spaces": {},
               "related_rebuild_queued": related_rebuild_queued,
               "embedding_jobs": int(queued or 0), "enrich": enrich_stats,
               "succeeded_document_ids": succeeded_document_ids,
               "failed_document_ids": failed_document_ids,
               "failed_documents": len(failed_document_ids)}
    logger.info("[LAW_CATALOG] server=%s: авто-синк завершён %s", server_id, summary)
    return summary


# END OF FILE services/law_catalog_service.py
