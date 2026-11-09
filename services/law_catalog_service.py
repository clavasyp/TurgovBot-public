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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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
from services.law_title_utils import is_rating_metadata
from services.law_structural_code import normalize_roman_structural_token
from services.law_synonym_service import (
    ExactSignatureRegistry,
    MIN_REPLACEMENT_EFFECTIVE,
    MIN_REPLACEMENT_PHYSICAL,
    SynonymWriteError,
    acquire_synonym_write_lock,
    expand_synonym_terms,
    is_explicitly_empty_or_repealed_article,
    load_exact_signature_registry,
    normalize_synonym_term,
    project_context_for_server,
    replace_auto_synonyms,
)

logger = logging.getLogger(__name__)


class CatalogParseAmbiguityError(RuntimeError):
    """Raised before catalog mutation when the shared parser is ambiguous."""


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


async def _delete_document_articles(doc_id: int, server_id: str) -> None:
    """Delete a document catalog and durably invalidate affected related graphs."""
    async with db_manager.transaction() as conn:
        await acquire_synonym_write_lock(conn, server_id)
        spaces = await conn.fetch(
            """
            SELECT DISTINCT e.space_id
            FROM law_article_embeddings e
            JOIN law_articles a ON a.id = e.article_id
            WHERE a.law_document_id = $1
            """,
            doc_id,
        )
        await conn.execute("DELETE FROM law_articles WHERE law_document_id = $1", doc_id)
        for row in spaces:
            await mark_related_rebuild(conn, server_id, row["space_id"])

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


def _article_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


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


def _catalog_rows(content: str) -> List[Dict[str, Any]]:
    """Парсит документ в упорядоченные строки каталога без обращения к БД."""
    rows: List[Dict[str, Any]] = []
    for source_order, (ref, body) in enumerate(split_into_blocks(content)):
        kind, code, title = normalize_header(ref)
        # Однострочный заголовок иногда заканчивается служебными звёздами/юрисдикцией.
        # Это не название: берём следующую содержательную строку тела.
        if (title is None or _is_junk_title(title)) and ref is not None:
            title = _title_from_body(body)
        rows.append({
            "article_ref": ref,
            "kind": kind,
            "code": code,
            "title": title,
            "full_text": body,
            "article_hash": _article_hash(body),
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
    return str(item.get("kind") or "other"), _article_hash(normalized)


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
    """Синхронизирует строки документа, сохраняя ID уверенно узнанных статей.

    doc: dict с id, server_id, title, content (как в indexing_service.index_document).
    Возвращает текущее число строк документа.
    """
    content = doc.get("content")
    doc_id = doc["id"]
    if not content:
        await _delete_document_articles(doc_id, doc["server_id"])
        return 0

    _assert_catalog_mutation_safe(content, doc_id=doc_id)
    rows = _catalog_rows(content)
    if not rows:
        await _delete_document_articles(doc_id, doc["server_id"])
        return 0

    async with db_manager.transaction() as conn:
        await acquire_synonym_write_lock(conn, doc["server_id"])
        old_article_rows = [dict(r) for r in await conn.fetch(
            """
            SELECT id, law_document_id, server_id, source_order, article_hash,
                   article_ref, kind, code, title, doc_title, full_text,
                   penalty_summary, penalty_json, enrich_hash
            FROM law_articles
            WHERE law_document_id = $1
            ORDER BY source_order, id
            FOR UPDATE
            """,
            doc_id,
        )]
        # Смена server_id документа не считается той же публичной статьёй: Overlay
        # хранит server рядом с favorite, поэтому старый ID безопаснее честно удалить.
        eligible_old = [r for r in old_article_rows if r.get("server_id") == doc["server_id"]]
        matched_old, ambiguous = _match_existing_articles(rows, eligible_old)
        matched_ids = {old["id"] for old in matched_old if old is not None}
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
            old_title = (
                old_article["title"]
                if enrichment_compatible
                and old_article.get("title")
                and not _is_junk_title(old_article["title"])
                else None
            )
            title = row["title"] or old_title
            penalty_summary = (
                old_article.get("penalty_summary") if enrichment_compatible else None
            )
            penalty_json = old_article.get("penalty_json") if enrichment_compatible else None
            enrich_hash = old_article.get("enrich_hash") if enrichment_compatible else None
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
                if not enrichment_compatible:
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
                        embedding = CASE WHEN $14::boolean THEN NULL ELSE embedding END,
                        updated_at = CASE WHEN $14::boolean THEN now() ELSE updated_at END
                    WHERE id = $15 AND law_document_id = $1
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
                         embedding_local, embedding_local_space,
                         embedding_local_input_hash, embedding_local_updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            NULL, NULL, NULL, NULL, $11, $12, NULL)
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
               penalty_summary, penalty_json, enrich_hash
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

_AUTO_ENRICH_MIN_PHYSICAL = 12
_AUTO_ENRICH_MAX_PHYSICAL = 15
_AUTO_ENRICH_MIN_EFFECTIVE = 12
_AUTO_ENRICH_QUALITY_ATTEMPTS = 2
_AUTO_ENRICH_NEIGHBOR_RADIUS = 2
_AUTO_ENRICH_NEIGHBOR_TEXT_LIMIT = 1_200
_AUTO_ENRICH_BATCH_TEXT_BUDGET = 120_000
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

_ENRICH_PROMPT = """Ты — юридический аналитик правового помощника RP-проекта.
{project_context}

Ниже целевые статьи законов конкретного сервера. У каждой может быть contrast_context —
ближайшие статьи того же документа. Это только контекст для сравнения: НЕ переноси из
соседней статьи название, санкцию, условия или сленг в целевую.
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
- Санкция ТОЛЬКО из текста статьи. Не выдумывай. Нет санкции в тексте → penalty_summary=null, penalty=null.
- synonyms: дай 12-15 РАЗНЫХ, узких и подтверждённых текстом формулировок, которыми игрок
  реально спросит эту норму голосом/текстом. Для процессуальных и организационных
  норм вместо выдуманного сленга давай точные перефразы конкретного действия, обязанности,
  субъекта и условия. Для пустой/отменённой нормы верни title=null, обе санкции null и synonyms=[].
  Поле current_auto_synonyms целевой статьи содержит её прежние автосинонимы: сохрани те,
  которые всё ещё точно соответствуют новому тексту, исправь устаревшие и не копируй
  их вслепую. Списки у contrast_context нужны только для предотвращения коллизий,
  переносить их в целевую статью нельзя.
  СРАВНИ статью с contrast_context и соседними целевыми статьями. Фраза не должна одинаково
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
  Только нижний регистр.

{quality_feedback}
ЦЕЛЕВЫЕ СТАТЬИ:
{articles_json}"""

_ENRICH_REVIEW_PROMPT = """Ты — второй, независимый контролёр качества обогащения
юридического поиска RP-проекта.
{project_context}

Ниже даны исходные целевые статьи с contrast_context и черновой JSON-ответ другого
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
- всё ещё корректные current_auto_synonyms самой целевой статьи сохранены,
  устаревшие удалены, а синонимы contrast_context не перенесены;
- для пустой/упразднённой статьи title=null, penalty_summary=null, penalty=null,
  synonyms=[].

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


def _build_auto_enrichment_payload(
    batch: Sequence[Mapping[str, Any]],
    all_articles: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach narrow same-document contrast context to every LLM target."""

    def current_terms(article: Mapping[str, Any]) -> List[str]:
        return sorted({
            normalized
            for value in (article.get("current_auto_synonyms") or ())
            if (normalized := normalize_synonym_term(value)) is not None
        })

    by_document: Dict[Any, List[Mapping[str, Any]]] = {}
    for article in all_articles:
        document_id = article.get("law_document_id")
        if document_id is None:
            continue
        by_document.setdefault(document_id, []).append(article)
    for rows in by_document.values():
        rows.sort(
            key=lambda row: (
                int(row.get("source_order") or 0),
                int(row["id"]),
            )
        )

    positions: Dict[int, Tuple[List[Mapping[str, Any]], int]] = {}
    for rows in by_document.values():
        for index, article in enumerate(rows):
            positions[int(article["id"])] = (rows, index)

    payload: List[Dict[str, Any]] = []
    for article in batch:
        article_id = int(article["id"])
        contrast: List[Dict[str, Any]] = []
        location = positions.get(article_id)
        if location is not None:
            rows, index = location
            start = max(0, index - _AUTO_ENRICH_NEIGHBOR_RADIUS)
            end = min(len(rows), index + _AUTO_ENRICH_NEIGHBOR_RADIUS + 1)
            for neighbor_index in range(start, end):
                neighbor = rows[neighbor_index]
                if int(neighbor["id"]) == article_id:
                    continue
                contrast.append({
                    "relation": (
                        "before" if neighbor_index < index else "after"
                    ),
                    "id": int(neighbor["id"]),
                    "code": neighbor.get("code"),
                    "title": neighbor.get("title"),
                    "text": str(neighbor.get("full_text") or "")[
                        :_AUTO_ENRICH_NEIGHBOR_TEXT_LIMIT
                    ],
                    "current_auto_synonyms": current_terms(neighbor),
                })

        payload.append({
            "id": article_id,
            "code": article.get("code"),
            "title": article.get("title"),
            "document": article.get("doc_title"),
            "text": str(article.get("full_text") or ""),
            "current_auto_synonyms": current_terms(article),
            "contrast_context": contrast,
        })
    return payload


def _partition_auto_enrichment_batches(
    articles: Sequence[Mapping[str, Any]],
    maximum_articles: int,
) -> List[List[Mapping[str, Any]]]:
    """Keep every target complete while bounding normal multi-article prompts."""

    maximum_articles = int(maximum_articles)
    if maximum_articles < 1:
        raise ValueError("LAW_ENRICH_BATCH_ARTICLES must be positive")
    batches: List[List[Mapping[str, Any]]] = []
    current: List[Mapping[str, Any]] = []
    current_chars = 0
    for article in articles:
        article_chars = len(str(article.get("full_text") or ""))
        if current and (
            len(current) >= maximum_articles
            or current_chars + article_chars > _AUTO_ENRICH_BATCH_TEXT_BUDGET
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(article)
        current_chars += article_chars
    if current:
        batches.append(current)
    return batches


def _validate_auto_enrichment_response(
    items: Any,
    batch: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Validate a complete Gemini batch before the first database write."""

    if not isinstance(items, list):
        raise ValueError("response must be a JSON array")
    expected = {int(article["id"]): article for article in batch}
    normalized_by_id: Dict[int, Dict[str, Any]] = {}
    canonical_groups_by_id: Dict[int, set[str]] = {}

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

        raw_synonyms = item["synonyms"]
        if not isinstance(raw_synonyms, list):
            raise ValueError(
                f"article {article_id} synonyms must be an array"
            )
        synonyms: List[str] = []
        for value in raw_synonyms:
            if not isinstance(value, str):
                raise ValueError(
                    f"article {article_id} has a non-string synonym"
                )
            normalized = normalize_synonym_term(value)
            if normalized is None:
                raise ValueError(
                    f"article {article_id} has a synonym outside shared bounds"
                )
            synonyms.append(normalized)
        if len(synonyms) != len(set(synonyms)):
            raise ValueError(
                f"article {article_id} has duplicate normalized synonyms"
            )
        canonical_groups = set(expand_synonym_terms(synonyms))
        if len(canonical_groups) != len(synonyms):
            raise ValueError(
                f"article {article_id} has duplicate runtime-canonical "
                "synonym groups"
            )

        exempt = is_explicitly_empty_or_repealed_article(
            expected[article_id].get("full_text")
        )
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

        normalized_by_id[article_id] = {
            "id": article_id,
            "title": title,
            "penalty_summary": summary,
            "penalty": penalty,
            "synonyms": synonyms,
        }
        canonical_groups_by_id[article_id] = canonical_groups

    missing = sorted(set(expected) - set(normalized_by_id))
    if missing or len(items) != len(expected):
        raise ValueError(
            "response coverage is not exact: "
            f"missing={missing}, expected={len(expected)}, actual={len(items)}"
        )

    canonical_claims: Dict[str, set[int]] = {}
    for article_id, canonical_groups in canonical_groups_by_id.items():
        for canonical in canonical_groups:
            canonical_claims.setdefault(canonical, set()).add(article_id)
    collisions = {
        canonical: sorted(article_ids)
        for canonical, article_ids in canonical_claims.items()
        if len(article_ids) > 1
    }
    if collisions:
        preview = list(sorted(collisions.items()))[:5]
        raise ValueError(
            "response reuses canonical synonym groups across articles: "
            f"{preview}"
        )

    return [
        normalized_by_id[int(article["id"])]
        for article in batch
    ]


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


async def enrich_articles_for_server(
    server_id: str,
    only_stale: bool = True,
) -> Dict[str, int]:
    """LLM-обогащение статей сервера: penalty_* + автосинонимы. Только kind='article'.

    only_stale=True → только статьи, у которых enrich_hash не совпадает с article_hash
    (новые/изменённые). Ошибки батча не прерывают остальные.
    """
    from google.genai import types
    from rate_limiter import execute_vertex_request, RequestPriority, get_current_client

    stats = {"articles": 0, "synonyms": 0, "batches_failed": 0}
    all_articles = [
        dict(row)
        for row in await db_manager.fetch(
            """
        SELECT a.id, a.law_document_id, a.source_order, a.code, a.title,
               a.doc_title, a.full_text, a.article_hash, a.enrich_hash,
               COALESCE(
                 ARRAY(
                   SELECT s.term
                   FROM law_synonyms s
                   WHERE s.article_id = a.id
                     AND s.source = 'auto'
                     AND s.status IN ('approved', 'stale')
                   ORDER BY s.term
                 ),
                 ARRAY[]::text[]
               ) AS current_auto_synonyms
        FROM law_articles a
        WHERE a.server_id = $1 AND a.kind = 'article'
        ORDER BY a.id
        """,
            server_id,
        )
    ]
    articles = [
        article
        for article in all_articles
        if (
            not only_stale
            or str(article.get("enrich_hash") or "")
            != str(article["article_hash"])
        )
    ]
    if not articles:
        return stats

    # One O(N) snapshot per server is enough for coverage metrics.  Collision
    # safety itself is re-checked against CURRENT DB rows under the per-server
    # advisory lock for every write transaction.
    signature_registry = await load_exact_signature_registry(db_manager, server_id)
    model = config.LAW_CATALOG_ENRICH_MODEL
    batch_size = config.LAW_ENRICH_BATCH_ARTICLES
    logger.info("[LAW_CATALOG] server=%s: обогащение %d статей (%s, батч %d)",
                server_id, len(articles), model, batch_size)

    batch_offset = 0
    for batch in _partition_auto_enrichment_batches(articles, batch_size):
        i = batch_offset
        batch_offset += len(batch)
        payload = json.dumps(
            _build_auto_enrichment_payload(batch, all_articles),
            ensure_ascii=False,
        )

        try:
            quality_feedback = ""
            items = None
            for quality_attempt in range(_AUTO_ENRICH_QUALITY_ATTEMPTS):
                prompt = _ENRICH_PROMPT.format(
                    project_context=project_context_for_server(server_id),
                    quality_feedback=quality_feedback,
                    articles_json=payload,
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
                try:
                    items = _validate_auto_enrichment_response(
                        _parse_json_array(response.text or ""),
                        batch,
                    )
                    break
                except ValueError as exc:
                    if quality_attempt + 1 >= _AUTO_ENRICH_QUALITY_ATTEMPTS:
                        raise
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
                    project_context=project_context_for_server(server_id),
                    quality_feedback=review_feedback,
                    articles_json=payload,
                    candidate_json=candidate_json,
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
                try:
                    reviewed_items = _validate_auto_enrichment_response(
                        _parse_json_array(review_response.text or ""),
                        batch,
                    )
                    break
                except ValueError as exc:
                    if review_attempt + 1 >= _AUTO_ENRICH_QUALITY_ATTEMPTS:
                        raise
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
            stats["batches_failed"] += 1
            logger.exception("[LAW_CATALOG] server=%s: батч обогащения %d-%d упал",
                             server_id, i, i + len(batch) - 1)
            continue

        try:
            batch_stats = await _write_auto_enrichment_batch(
                server_id,
                batch,
                items,
                signature_registry,
            )
        except SynonymWriteError:
            stats["batches_failed"] += 1
            logger.exception(
                "[LAW_CATALOG] server=%s: validated Gemini batch %d-%d "
                "failed the atomic DB quality/CAS gate; no article in the "
                "batch was committed",
                server_id,
                i,
                i + len(batch) - 1,
            )
            continue
        stats["articles"] += batch_stats["articles"]
        stats["synonyms"] += batch_stats["synonyms"]
        stats["synonym_collisions_rejected"] = (
            stats.get("synonym_collisions_rejected", 0)
            + batch_stats.get("synonym_collisions_rejected", 0)
        )
        await asyncio.sleep(
            max(0.0, float(config.LAW_ENRICH_INTER_BATCH_DELAY_SECONDS))
        )

    logger.info("[LAW_CATALOG] server=%s: обогащено статей=%d, синонимов=%d, упавших батчей=%d",
                server_id, stats["articles"], stats["synonyms"], stats["batches_failed"])
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
) -> bool:
    """Transaction-owned variant for all-or-none reviewed artifacts."""

    if not synonym_lock_already_held:
        await acquire_synonym_write_lock(conn, server_id)
    article_before = await conn.fetchrow(
        """
        SELECT id, article_hash, enrich_hash
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
            title = COALESCE(title, $4),
            enrich_hash = $6, updated_at = now()
        WHERE id = $1 AND server_id = $5 AND article_hash = $6
        RETURNING id
        """,
        article_id,
        summary,
        json.dumps(penalty, ensure_ascii=False) if penalty else None,
        title,
        server_id,
        expected_article_hash,
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
    """Сериализует полную пересборку одного сервера между всеми процессами."""
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
    """Полная (пере)сборка каталога: статьи → FRIDA jobs → [обогащение] → related job.

    retitle=True: ПОСЛЕ пересборки (когда эвристика уже раздала названия со вторых строк)
    сбрасывает enrich_hash статей, оставшихся без title, — обогащение перегенерит их
    с новым промптом (разовый догон названий).
    reenrich=True: ДО пересборки сбрасывает enrich_hash ВСЕХ статей сервера — полная
    перегенерация penalty+синонимов новым промптом. Старые данные остаются доступными,
    пока успешный ответ не заменит их атомарно; неудачный LLM-батч поиск не обедняет.
    """
    docs = await db_manager.fetch(
        "SELECT id, server_id, title, content FROM law_documents WHERE server_id = $1",
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

    if reenrich:
        n = await db_manager.execute(
            "UPDATE law_articles SET enrich_hash = NULL WHERE server_id = $1", server_id
        )
        logger.info("[LAW_CATALOG] server=%s: reenrich — сброшен enrich_hash у всех: %s", server_id, n)
    articles = 0
    failed_documents: List[int] = []
    for row in docs:
        doc = dict(row)
        try:
            articles += await build_catalog_for_document(doc)
        except Exception:
            logger.exception("[LAW_CATALOG] ошибка каталогизации документа %s", doc.get("id"))
            failed_documents.append(int(doc["id"]))
    if failed_documents:
        raise RuntimeError(
            f"server {server_id}: catalog reconcile failed for documents {failed_documents}"
        )
    enrich_stats = None
    if enrich:
        if retitle:
            n = await db_manager.execute(
                "UPDATE law_articles SET enrich_hash = NULL "
                "WHERE server_id = $1 AND kind = 'article' AND title IS NULL "
                "AND enrich_hash IS NOT NULL",
                server_id,
            )
            logger.info("[LAW_CATALOG] server=%s: retitle — сброшено enrich_hash: %s", server_id, n)
        enrich_stats = await enrich_articles_for_server(server_id, only_stale=True)
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


async def sync_catalog_for_documents(server_id: str, doc_ids: List[int]) -> Optional[Dict[str, Any]]:
    """Сериализует инкрементальный sync одного сервера между ботом/backfill."""
    if not doc_ids:
        return None
    async with catalog_server_lock(server_id):
        return await _sync_catalog_for_documents(server_id, doc_ids)


async def _sync_catalog_for_documents(server_id: str, doc_ids: List[int]) -> Optional[Dict[str, Any]]:
    """Инкрементальная досборка каталога для изменённых документов (из цикла обновления БЗ).

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
        "SELECT id, server_id, title, content FROM law_documents "
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
            articles += await build_catalog_for_document(doc)
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
        # ``enrich_articles_for_server`` intentionally continues after a bad
        # model batch or a per-article synonym quality failure.  Its aggregate
        # stats therefore cannot prove which source documents are fully
        # current.  Re-read the requested documents under the still-held
        # catalog lock and classify by the authoritative row hashes.
        try:
            incomplete_rows = await db_manager.fetch(
                """
                SELECT DISTINCT a.law_document_id AS id
                FROM law_articles a
                WHERE a.server_id = $1
                  AND a.law_document_id = ANY($2::int[])
                  AND a.kind = 'article'
                  AND a.enrich_hash IS DISTINCT FROM a.article_hash
                ORDER BY a.law_document_id
                """,
                server_id,
                requested_doc_ids,
            )
            incomplete_enrichment_ids = {
                int(row["id"]) for row in incomplete_rows
            }
        except Exception:
            logger.exception(
                "[LAW_CATALOG] авто-синк: не удалось доказать полноту "
                "обогащения; pending сохранён"
            )
            incomplete_enrichment_ids = set(succeeded_document_ids)
        if enrichment_raised:
            # The exception may have happened before/inside a batch whose rows
            # are not safely attributable from aggregate stats.  Fail closed
            # for every otherwise successful requested document.
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
