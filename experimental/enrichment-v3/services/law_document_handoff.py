"""Conservative identity handoff for replaced forum threads.

It implements the database operation used by ``knowledge_base_manager`` after
the caller has already identified:

* forum thread ids which disappeared from a server section; and
* current/live thread ids discovered in that server section.

Documents are paired only when the complete stored ``content`` is equal and
its locally computed SHA-256 is equal, the normalised document title is equal,
and both documents contain enough real text to be useful identity evidence.
The legacy ``content_hash`` column is not trusted because existing rows
commonly contain MD5 values.

The public Overlay identity is ``law_articles.id``.  A successful handoff
moves the old document's articles and RAG chunks to the newly parsed document,
then removes the old document.  Article primary keys and every table referring
to those article ids therefore remain unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

from database.manager import db_manager
from services.law_structural_code import normalize_roman_structural_token


logger = logging.getLogger(__name__)

# These documents are complete laws/codes, not individual article snippets.
# A deliberately conservative floor prevents an empty parser shell, an error
# page fragment, or a repeated marker from becoming identity evidence.
_MIN_CONTENT_CHARS = 256
_MIN_CONTENT_ALNUM_CHARS = 180
_MIN_CONTENT_WORDS = 30
_MIN_CONTENT_UNIQUE_WORDS = 10
_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)

# --- Structural (non byte-identical) identity -------------------------------------------------
# Byte equality only covers a re-post of an unchanged law.  Servers regularly publish an
# EDITED law as a brand new thread: the old thread disappears, content differs, and the
# strict path can prove nothing.  Deleting there costs the whole accumulated identity of
# that law — public article ids (Overlay favourites point at them), auto synonyms, penalty
# data — and the replacement starts from scratch.  Memphis lost 20 of 32 laws this way.
#
# The structural path pairs such documents when the normalised title is an exact, unique
# anchor on both sides AND the article skeleton still matches.  It never guesses: a title
# shared by two candidates, a thin skeleton, or a weak overlap is refused, and every
# refusal keeps the existing conservative delete behaviour.
_STRUCTURAL_HANDOFF_ENABLED = (
    os.environ.get("LAW_DOCUMENT_HANDOFF_STRUCTURAL", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)
# Both sides must expose at least this many article codes; below that a coincidental
# overlap ("Статья 1", "Статья 2") would be indistinguishable from real identity.
_MIN_STRUCTURAL_CODES = 5
# Share of the OLD skeleton that must survive in the replacement.
_MIN_STRUCTURAL_RETENTION = 0.7
# Jaccard over both skeletons; blocks pairing a law with a much larger unrelated code.
_MIN_STRUCTURAL_JACCARD = 0.6

# «Статья 12.1», «Ст. 5», «Статья 10-1» → номер как структурный признак. Дефис приводим к
# точке так же, как normalize_header в каталоге, чтобы «10-1» и «10.1» не считались разными.
_STRUCTURAL_CODE_RE = re.compile(
    r"^\s*(?:статья|ст\.?)\s*[№N]?\s*([\dIVXLCМ]+(?:[.\-–]\d+)*)",
    re.IGNORECASE,
)

# Two narrowly stronger identity proofs cover sources which intentionally do
# not expose ordinary ``Статья N`` rows:
#
# * section-primary laws (for example a constitution) keep an exact title and
#   are compared by typed ``Глава``/``Раздел`` markers; and
# * some quoted laws are re-posted with only a neutral jurisdiction phrase in
#   the title while their parser dialect changes from legacy bare ``N.M`` to
#   proven true-N.M.  There the exact quoted legal subject plus a long,
#   ordered bare-article skeleton is the anchor.
#
# These paths are intentionally stricter than the legacy article path.  A
# type mismatch, reordered shared markers, a short skeleton, a generic quote,
# or a non-neutral title change refuses the handoff.
_TYPED_STRUCTURAL_REF_RE = re.compile(
    r"^\s*(?P<label>статья\.?|ст\.?|глава|раздел)\s*[№N]?\s*"
    r"(?P<code>"
    r"\d+(?:[.\-–]\d+)*"
    r"|[IVXLCMІХСМ\u2160-\u217F]+"
    r")"
    r"(?![0-9A-Za-zА-Яа-яЁё])",
    re.IGNORECASE,
)
# This matcher receives only refs already accepted by ``split_into_blocks``.
# Keep an accepted legacy ``N.M`` even when the source line continues with a
# citation or money suffix; a real ``N.M.K`` remains a different structure.
_BARE_ARTICLE_REF_RE = re.compile(
    r"^\s*(?P<code>\d+\.\d+)(?!\d|\.\d)"
)
_TYPED_KIND_BY_LABEL = {
    "статья": "article",
    "ст": "article",
    "глава": "chapter",
    "раздел": "section",
}

_MIN_SECTION_PRIMARY_CODES = 24
_MIN_SECTION_PRIMARY_CHAPTERS = 3
_MIN_SECTION_PRIMARY_SHARED = 24
_MIN_SECTION_PRIMARY_RETENTION = 0.75
_MIN_SECTION_PRIMARY_JACCARD = 0.65
_MIN_SECTION_CHAPTER_RETENTION = 0.70
_MIN_SECTION_CHAPTER_JACCARD = 0.60

_MIN_QUOTED_BARE_ARTICLE_CODES = 12
_MIN_QUOTED_BARE_SHARED = 12
_MIN_QUOTED_BARE_RETENTION = 0.80
_MIN_QUOTED_BARE_JACCARD = 0.40
_MAX_QUOTED_BARE_GROWTH = 2.0

_QUOTED_TITLE_SPAN_RE = re.compile(
    r'"(?P<straight>[^"\r\n]+)"|«(?P<guillemet>[^«»\r\n]+)»'
)
_ANY_QUOTE_CHAR_RE = re.compile(r'["«»„“”]')
_NEUTRAL_LAW_PREFIX_RE = re.compile(
    r"^(?P<base>закон)"
    r"(?:\s+(?P<jurisdiction>(?:штата|республики|округа|города|области|края)"
    r"\s+[0-9a-zа-яё][0-9a-zа-яё.\-]*))?$",
    re.IGNORECASE,
)
_JURISDICTION_NAME_RE = re.compile(
    r"(?:[A-ZА-ЯЁ]{2,8}|[A-ZА-ЯЁ][a-zа-яё]+"
    r"(?:-[A-ZА-ЯЁ][a-zа-яё]+)*)$"
)
_QUOTED_SUBJECT_STOPWORDS = frozenset(
    {"о", "об", "и", "в", "во", "на", "по", "для", "от", "до", "с", "со"}
)
_GENERIC_QUOTED_SUBJECT_PREFIXES = (
    "о внесении изменений",
    "об изменении",
    "о поправках",
)


@dataclass(frozen=True)
class _TypedStructuralSkeleton:
    """Ordered parser markers used only by the narrow structural proofs."""

    # ``is_bare`` is meaningful only for ``article`` markers.
    markers: Tuple[Tuple[str, str, bool], ...]
    parser_safe: bool = True
    fallback_mode: Optional[str] = None
    article_dialect_status: str = "not_applicable"
    article_dialect_mode: Optional[str] = None
    structured_article_count: int = 0
    legacy_bare_candidate_count: int = 0
    mixed_special_candidate_count: int = 0
    accepted_mixed_special_count: int = 0
    recovered_parent_count: int = 0
    judicial_candidate_count: int = 0
    accepted_judicial_count: int = 0
    untyped_ref_count: int = 0


@dataclass(frozen=True)
class _QuotedLegalTitle:
    base: str
    subject: str
    jurisdiction: Optional[str]

    @property
    def identity(self) -> Tuple[str, str]:
        return self.base, self.subject


def _normalise_structural_code(kind: str, code: str) -> str:
    normalised = code.strip().strip(".").replace("-", ".").replace("–", ".")
    if kind in {"chapter", "section"}:
        roman = normalize_roman_structural_token(normalised)
        if roman is not None:
            return roman
    return normalised


def _typed_structural_skeleton(content: Any) -> _TypedStructuralSkeleton:
    """Extract typed markers from the shared parser's actual block plan.

    This does not rescan arbitrary body lines.  Only refs already accepted by
    ``split_into_blocks`` can become evidence, then a small local normalizer
    assigns ``article``/``chapter``/``section`` and canonicalises its code.
    Keeping the extractor here avoids importing the catalog/embedding layer
    into the source-sync transaction.
    """

    text = content if isinstance(content, str) else ""
    if not text:
        return _TypedStructuralSkeleton(markers=())
    try:
        from services.indexing_service import _header_match_plan, split_into_blocks
    except Exception:  # pragma: no cover - import guard, structural path stays off
        logger.warning("[HANDOFF] split_into_blocks unavailable; typed pairing disabled")
        return _TypedStructuralSkeleton(markers=(), parser_safe=False)

    plan = _header_match_plan(text)
    if (
        plan.article_dialect_status == "ambiguous"
        or bool(plan.recovery_unresolved_bare_candidates)
    ):
        logger.info(
            "[HANDOFF] typed structural proof refused unsafe parser plan "
            "(dialect=%s unresolved_parent_candidates=%d)",
            plan.article_dialect_status,
            len(plan.recovery_unresolved_bare_candidates),
        )
        return _TypedStructuralSkeleton(markers=(), parser_safe=False)

    markers: list[tuple[str, str, bool]] = []
    untyped_ref_count = 0
    for ref, _body in split_into_blocks(text):
        raw_ref = str(ref or "")
        if not raw_ref:
            continue
        match = _TYPED_STRUCTURAL_REF_RE.match(raw_ref)
        if match is not None:
            label = match.group("label").casefold().rstrip(".")
            kind = _TYPED_KIND_BY_LABEL.get(label)
            if kind is None:
                continue
            code = _normalise_structural_code(kind, match.group("code"))
            if code:
                markers.append((kind, code, False))
            continue

        bare = _BARE_ARTICLE_REF_RE.match(raw_ref)
        if bare is not None:
            markers.append(
                (
                    "article",
                    _normalise_structural_code("article", bare.group("code")),
                    True,
                )
            )
            continue
        untyped_ref_count += 1
    return _TypedStructuralSkeleton(
        markers=tuple(markers),
        parser_safe=True,
        fallback_mode=plan.fallback_mode,
        article_dialect_status=plan.article_dialect_status,
        article_dialect_mode=plan.article_dialect_mode,
        structured_article_count=plan.structured_article_count,
        legacy_bare_candidate_count=len(plan.legacy_bare_candidates),
        mixed_special_candidate_count=len(plan.mixed_special_candidates),
        accepted_mixed_special_count=len(
            plan.accepted_mixed_special_candidates
        ),
        recovered_parent_count=len(plan.recovered_parent_articles),
        judicial_candidate_count=len(plan.judicial_material_candidates),
        accepted_judicial_count=len(plan.accepted_judicial_materials),
        untyped_ref_count=untyped_ref_count,
    )


def _ordered_unique_typed_codes(
    skeleton: _TypedStructuralSkeleton,
    *,
    kinds: frozenset[str],
) -> Tuple[Tuple[str, str], ...]:
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    chapter_scope = ""
    for kind, code, is_bare in skeleton.markers:
        if kind == "chapter":
            chapter_scope = code
        # A bare N.M can repeat under a different chapter in real sources.
        # Preserve its parser-approved parent path instead of comparing a flat
        # number set.  Keyword articles keep the established global code.
        scoped_code = (
            f"{chapter_scope}\x1f{code}"
            if (
                (kind == "article" and is_bare)
                or kind == "section"
            )
            and chapter_scope
            else code
        )
        key = (kind, scoped_code)
        if kind not in kinds or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return tuple(ordered)


def _typed_structural_similarity(
    old_skeleton: _TypedStructuralSkeleton,
    new_skeleton: _TypedStructuralSkeleton,
    *,
    kinds: frozenset[str],
    minimum_codes: int,
    minimum_shared: int,
    minimum_retention: float,
    minimum_jaccard: float,
) -> tuple[bool, float, float, int, bool]:
    """Compare typed sets and require shared markers to keep their order."""

    if not old_skeleton.parser_safe or not new_skeleton.parser_safe:
        return False, 0.0, 0.0, 0, False

    old_order = _ordered_unique_typed_codes(old_skeleton, kinds=kinds)
    new_order = _ordered_unique_typed_codes(new_skeleton, kinds=kinds)
    if len(old_order) < minimum_codes or len(new_order) < minimum_codes:
        return False, 0.0, 0.0, 0, False

    old_codes = frozenset(old_order)
    new_codes = frozenset(new_order)
    shared = old_codes & new_codes
    union = old_codes | new_codes
    retention = len(shared) / len(old_codes)
    jaccard = len(shared) / len(union) if union else 0.0
    old_shared_order = tuple(key for key in old_order if key in shared)
    new_shared_order = tuple(key for key in new_order if key in shared)
    order_preserved = old_shared_order == new_shared_order
    strong = (
        len(shared) >= minimum_shared
        and retention >= minimum_retention
        and jaccard >= minimum_jaccard
        and order_preserved
    )
    return strong, retention, jaccard, len(shared), order_preserved


def _section_primary_similarity(
    old_skeleton: _TypedStructuralSkeleton,
    new_skeleton: _TypedStructuralSkeleton,
) -> tuple[bool, float, float, int, bool]:
    """Strong exact-title proof for laws made of chapters and sections."""

    if not old_skeleton.parser_safe or not new_skeleton.parser_safe:
        return False, 0.0, 0.0, 0, False

    for skeleton in (old_skeleton, new_skeleton):
        marker_kinds = {kind for kind, _code, _bare in skeleton.markers}
        if (
            marker_kinds - {"chapter", "section"}
            or "chapter" not in marker_kinds
            or "section" not in marker_kinds
            or skeleton.fallback_mode is not None
            or skeleton.article_dialect_status != "not_applicable"
            or skeleton.article_dialect_mode is not None
            or skeleton.structured_article_count != 0
            or skeleton.legacy_bare_candidate_count != 0
            or skeleton.mixed_special_candidate_count != 0
            or skeleton.accepted_mixed_special_count != 0
            or skeleton.recovered_parent_count != 0
            or skeleton.judicial_candidate_count != 0
            or skeleton.accepted_judicial_count != 0
            or skeleton.untyped_ref_count != 0
        ):
            return False, 0.0, 0.0, 0, False

    chapter_result = _typed_structural_similarity(
        old_skeleton,
        new_skeleton,
        kinds=frozenset({"chapter"}),
        minimum_codes=_MIN_SECTION_PRIMARY_CHAPTERS,
        minimum_shared=_MIN_SECTION_PRIMARY_CHAPTERS,
        minimum_retention=_MIN_SECTION_CHAPTER_RETENTION,
        minimum_jaccard=_MIN_SECTION_CHAPTER_JACCARD,
    )
    if not chapter_result[0]:
        return False, 0.0, 0.0, 0, chapter_result[4]

    return _typed_structural_similarity(
        old_skeleton,
        new_skeleton,
        kinds=frozenset({"section"}),
        minimum_codes=_MIN_SECTION_PRIMARY_CODES,
        minimum_shared=_MIN_SECTION_PRIMARY_SHARED,
        minimum_retention=_MIN_SECTION_PRIMARY_RETENTION,
        minimum_jaccard=_MIN_SECTION_PRIMARY_JACCARD,
    )


def _quoted_bare_article_similarity(
    old_skeleton: _TypedStructuralSkeleton,
    new_skeleton: _TypedStructuralSkeleton,
) -> tuple[bool, float, float, int, bool]:
    """Strong proof for a parser-safe bare N.M law replacement."""

    if not old_skeleton.parser_safe or not new_skeleton.parser_safe:
        return False, 0.0, 0.0, 0, False

    if (
        old_skeleton.fallback_mode != "bare"
        or old_skeleton.article_dialect_status != "not_applicable"
        or old_skeleton.article_dialect_mode is not None
        or new_skeleton.fallback_mode != "article_dialect_true_nm"
        or new_skeleton.article_dialect_status != "resolved"
        or new_skeleton.article_dialect_mode != "true_nm"
        or old_skeleton.structured_article_count != 0
        or new_skeleton.structured_article_count != 0
        or old_skeleton.recovered_parent_count != 0
        or new_skeleton.recovered_parent_count != 0
        or old_skeleton.mixed_special_candidate_count != 0
        or new_skeleton.mixed_special_candidate_count != 0
        or old_skeleton.accepted_mixed_special_count != 0
        or new_skeleton.accepted_mixed_special_count != 0
        or old_skeleton.judicial_candidate_count != 0
        or new_skeleton.judicial_candidate_count != 0
        or old_skeleton.accepted_judicial_count != 0
        or new_skeleton.accepted_judicial_count != 0
        or old_skeleton.untyped_ref_count != 0
        or new_skeleton.untyped_ref_count != 0
    ):
        return False, 0.0, 0.0, 0, False

    for skeleton in (old_skeleton, new_skeleton):
        article_markers = [
            marker for marker in skeleton.markers if marker[0] == "article"
        ]
        marker_kinds = {kind for kind, _code, _bare in skeleton.markers}
        chapter_count = len(
            _ordered_unique_typed_codes(
                skeleton,
                kinds=frozenset({"chapter"}),
            )
        )
        # Keyword articles and mixed article styles are outside this narrow
        # proof.  Both legacy-bare and true-N.M expose bare refs here.
        if (
            marker_kinds - {"chapter", "article"}
            or chapter_count < 3
            or len(article_markers) < _MIN_QUOTED_BARE_ARTICLE_CODES
            or any(not is_bare for _kind, _code, is_bare in article_markers)
        ):
            return False, 0.0, 0.0, 0, False

    result = _typed_structural_similarity(
        old_skeleton,
        new_skeleton,
        kinds=frozenset({"article"}),
        minimum_codes=_MIN_QUOTED_BARE_ARTICLE_CODES,
        minimum_shared=_MIN_QUOTED_BARE_SHARED,
        minimum_retention=_MIN_QUOTED_BARE_RETENTION,
        minimum_jaccard=_MIN_QUOTED_BARE_JACCARD,
    )
    strong, retention, jaccard, shared, order_preserved = result
    old_codes = _ordered_unique_typed_codes(
        old_skeleton,
        kinds=frozenset({"article"}),
    )
    new_codes = _ordered_unique_typed_codes(
        new_skeleton,
        kinds=frozenset({"article"}),
    )
    if (
        not old_codes
        or len(new_codes) > len(old_codes) * _MAX_QUOTED_BARE_GROWTH
    ):
        strong = False
    return strong, retention, jaccard, shared, order_preserved


def _quoted_legal_title_anchor(title: Any) -> Optional[_QuotedLegalTitle]:
    """Return a strict jurisdiction-neutral anchor for one quoted law title."""

    if not isinstance(title, str):
        return None
    normalised = unicodedata.normalize("NFKC", title)
    matches = list(_QUOTED_TITLE_SPAN_RE.finditer(normalised))
    if len(matches) != 1:
        return None

    match = matches[0]
    subject = match.group("straight") or match.group("guillemet") or ""
    subject = _normalise_title(subject)
    prefix = " ".join(normalised[: match.start()].split())
    suffix = " ".join(normalised[match.end() :].split())
    if (
        suffix
        or _ANY_QUOTE_CHAR_RE.search(prefix)
        or _ANY_QUOTE_CHAR_RE.search(subject)
    ):
        return None

    prefix_match = _NEUTRAL_LAW_PREFIX_RE.fullmatch(prefix)
    if prefix_match is None:
        return None

    words = _WORD_RE.findall(subject)
    significant = {
        word for word in words if word not in _QUOTED_SUBJECT_STOPWORDS
    }
    alnum_chars = sum(character.isalnum() for character in subject)
    if (
        not (subject.startswith("о ") or subject.startswith("об "))
        or subject.startswith(_GENERIC_QUOTED_SUBJECT_PREFIXES)
        or len(words) < 5
        or len(significant) < 4
        or alnum_chars < 24
    ):
        return None

    jurisdiction = prefix_match.group("jurisdiction")
    if jurisdiction is not None:
        jurisdiction_words = jurisdiction.split()
        if (
            len(jurisdiction_words) != 2
            or _JURISDICTION_NAME_RE.fullmatch(jurisdiction_words[1]) is None
        ):
            return None
    return _QuotedLegalTitle(
        base=prefix_match.group("base").casefold(),
        subject=subject,
        jurisdiction=(
            _normalise_title(jurisdiction)
            if jurisdiction is not None
            else None
        ),
    )


def _quoted_title_change_is_neutral(
    old_anchor: _QuotedLegalTitle,
    new_anchor: _QuotedLegalTitle,
) -> bool:
    """Allow only bare ``Закон`` -> one jurisdiction-qualified ``Закон``."""

    if old_anchor.identity != new_anchor.identity:
        return False
    return (
        old_anchor.jurisdiction is None
        and new_anchor.jurisdiction is not None
    )


def _structural_codes(content: Any) -> frozenset[str]:
    """Article-number skeleton of a document.

    Deliberately independent of the catalog module: this must keep working while the
    catalog/enrichment layer evolves, and it needs no title/penalty semantics — only the
    set of article numbers a law declares.
    """

    text = content if isinstance(content, str) else ""
    if not text:
        return frozenset()
    try:
        from services.indexing_service import split_into_blocks
    except Exception:  # pragma: no cover - import guard, structural path just stays off
        logger.warning("[HANDOFF] split_into_blocks unavailable; structural pairing disabled")
        return frozenset()

    codes: set[str] = set()
    for ref, _body in split_into_blocks(text):
        match = _STRUCTURAL_CODE_RE.match(str(ref or ""))
        if match:
            codes.add(match.group(1).strip(".").replace("-", ".").replace("–", "."))
    return frozenset(codes)


def _structural_similarity(
    old_codes: frozenset[str],
    new_codes: frozenset[str],
) -> tuple[bool, float, float]:
    """(is_strong_enough, retention, jaccard) for two article skeletons."""

    if len(old_codes) < _MIN_STRUCTURAL_CODES or len(new_codes) < _MIN_STRUCTURAL_CODES:
        return False, 0.0, 0.0
    shared = old_codes & new_codes
    union = old_codes | new_codes
    retention = len(shared) / len(old_codes)
    jaccard = len(shared) / len(union) if union else 0.0
    strong = (
        retention >= _MIN_STRUCTURAL_RETENTION
        and jaccard >= _MIN_STRUCTURAL_JACCARD
    )
    return strong, retention, jaccard


@dataclass(frozen=True)
class ExactThreadHandoff:
    """One completed old-thread -> new-thread identity handoff."""

    server_id: str
    old_document_id: int
    target_document_id: int
    old_thread_id: int
    target_thread_id: int
    content_sha256: str
    article_count: int
    chunk_count: int


@dataclass(frozen=True)
class ExactThreadHandoffWarning:
    """A structured, non-mutating reason why candidates were not handed off."""

    code: str
    message: str
    content_sha256: Optional[str] = None
    old_thread_ids: Tuple[int, ...] = ()
    current_thread_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class ExactThreadHandoffResult:
    """Outcome for one server sync."""

    server_id: str
    missing_thread_ids: Tuple[int, ...]
    current_thread_ids: Tuple[int, ...]
    handoffs: Tuple[ExactThreadHandoff, ...]
    warnings: Tuple[ExactThreadHandoffWarning, ...]

    @property
    def mutated(self) -> bool:
        return bool(self.handoffs)


class ExactThreadHandoffInvariantError(RuntimeError):
    """Raised when locked database state changes contrary to our assumptions."""


@dataclass(frozen=True)
class RemovedLawDocument:
    """Metadata retained after one missing source document is removed."""

    document_id: int
    thread_id: int
    title: str
    gcs_uri: Optional[str]
    handoff_target_thread_id: Optional[int] = None


@dataclass(frozen=True)
class MissingThreadReconciliationResult:
    """Atomic DB outcome for all missing threads in one server sync."""

    server_id: str
    missing_thread_ids: Tuple[int, ...]
    handoff_result: ExactThreadHandoffResult
    removed_documents: Tuple[RemovedLawDocument, ...]
    ordinary_deleted_thread_ids: Tuple[int, ...]

    @property
    def handoffs(self) -> Tuple[ExactThreadHandoff, ...]:
        return self.handoff_result.handoffs

    @property
    def target_thread_ids(self) -> Tuple[int, ...]:
        return tuple(
            sorted({handoff.target_thread_id for handoff in self.handoffs})
        )

    @property
    def gcs_uris(self) -> Tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.gcs_uri
                for item in self.removed_documents
                if item.gcs_uri
            )
        )


def _normalise_thread_ids(values: Iterable[int], *, label: str) -> Tuple[int, ...]:
    ids = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must contain only positive integer thread ids")
        ids.append(value)
    return tuple(sorted(set(ids)))


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _normalise_title(title: Any) -> str:
    if not isinstance(title, str):
        return ""
    normalised = unicodedata.normalize("NFKC", title)
    return " ".join(normalised.split()).casefold()


def _content_quality(content: Any) -> tuple[bool, str]:
    if not isinstance(content, str) or not content.strip():
        return False, "no_content"

    normalised = unicodedata.normalize("NFKC", content)
    compact = " ".join(normalised.split())
    words = _WORD_RE.findall(compact.casefold())
    alnum_chars = sum(character.isalnum() for character in compact)
    if (
        len(compact) < _MIN_CONTENT_CHARS
        or alnum_chars < _MIN_CONTENT_ALNUM_CHARS
        or len(words) < _MIN_CONTENT_WORDS
        or len(set(words)) < _MIN_CONTENT_UNIQUE_WORDS
    ):
        return False, "low_information_content"
    return True, ""


def _require_active_transaction(conn: Any) -> None:
    checker = getattr(conn, "is_in_transaction", None)
    if not callable(checker) or checker() is not True:
        raise ExactThreadHandoffInvariantError(
            "Exact thread handoff requires an already active database transaction"
        )


def _command_count(status: str, command: str) -> int:
    """Read asyncpg's ``UPDATE N`` command status defensively."""

    parts = str(status).strip().split()
    if len(parts) != 2 or parts[0].upper() != command:
        raise ExactThreadHandoffInvariantError(
            f"Unexpected database command status for {command}: {status!r}"
        )
    try:
        return int(parts[1])
    except ValueError as exc:
        raise ExactThreadHandoffInvariantError(
            f"Unexpected database command count for {command}: {status!r}"
        ) from exc


def _warning(
    warnings: list[ExactThreadHandoffWarning],
    *,
    code: str,
    message: str,
    content_sha256: Optional[str] = None,
    old_thread_ids: Iterable[int] = (),
    current_thread_ids: Iterable[int] = (),
) -> None:
    item = ExactThreadHandoffWarning(
        code=code,
        message=message,
        content_sha256=content_sha256,
        old_thread_ids=tuple(sorted(old_thread_ids)),
        current_thread_ids=tuple(sorted(current_thread_ids)),
    )
    warnings.append(item)
    logger.warning(
        "Exact forum-thread handoff skipped: server-independent code=%s "
        "old_threads=%s current_threads=%s sha256=%s",
        item.code,
        item.old_thread_ids,
        item.current_thread_ids,
        item.content_sha256,
    )


def _structural_candidate_pairs(
    *,
    old_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
    paired_old_ids: set[int],
    paired_current_ids: set[int],
    digests: dict[int, str],
) -> tuple[
    list[tuple[str, dict[str, Any], dict[str, Any]]],
    list[ExactThreadHandoffWarning],
]:
    """Pair edited re-posts of the same law that byte equality cannot prove.

    Only documents left over from the exact pass take part.  The legacy proof
    remains exact unique title + keyword-article skeleton.  Two narrow extensions
    additionally cover exact-title section-primary laws and jurisdiction-only
    title changes around an identical quoted subject with bare-N.M articles.
    Anything weaker keeps the previous behaviour (old document deleted,
    identity lost).
    """

    warnings: list[ExactThreadHandoffWarning] = []
    pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    leftovers = [row for row in old_rows if int(row["id"]) not in paired_old_ids]
    if not leftovers:
        return pairs, warnings

    def _refuse(
        row: dict[str, Any],
        code: str,
        message: str,
        *,
        old_thread_ids: Optional[Iterable[int]] = None,
        current_thread_ids: Iterable[int] = (),
    ) -> None:
        _warning(
            warnings,
            code=code,
            message=message,
            content_sha256=digests.get(int(row["id"])),
            old_thread_ids=(
                old_thread_ids
                if old_thread_ids is not None
                else (int(row["source_thread_id"]),)
            ),
            current_thread_ids=current_thread_ids,
        )

    if not _STRUCTURAL_HANDOFF_ENABLED:
        for row in leftovers:
            _refuse(
                row,
                "old_document_has_no_exact_current_match",
                "No current document has exactly the same normalised title and full content.",
            )
        return pairs, warnings

    # Content quality remains mandatory for structural proofs.  The outer exact
    # pass already screened old rows; explicitly screen live rows as well so a
    # long title and a coincidental marker list cannot rescue an error shell.
    eligible_current_rows = [
        row
        for row in current_rows
        if _content_quality(row.get("content"))[0]
        and bool(_normalise_title(row.get("title")))
    ]

    # Exact title is the primary anchor, so it must identify exactly one
    # document per side.
    current_by_title: dict[str, list[dict[str, Any]]] = {}
    for row in eligible_current_rows:
        if int(row["id"]) in paired_current_ids:
            continue
        current_by_title.setdefault(_normalise_title(row.get("title")), []).append(row)

    old_by_title: dict[str, list[dict[str, Any]]] = {}
    for row in leftovers:
        old_by_title.setdefault(_normalise_title(row.get("title")), []).append(row)

    # The secondary title anchor is intentionally much narrower than fuzzy
    # similarity: exactly one specific quoted legal subject and only a
    # jurisdiction phrase may differ around it.
    current_by_quoted_anchor: dict[
        tuple[str, str], list[dict[str, Any]]
    ] = {}
    for row in eligible_current_rows:
        if int(row["id"]) in paired_current_ids:
            continue
        anchor = _quoted_legal_title_anchor(row.get("title"))
        if anchor is not None:
            current_by_quoted_anchor.setdefault(anchor.identity, []).append(row)

    old_by_quoted_anchor: dict[
        tuple[str, str], list[dict[str, Any]]
    ] = {}
    for row in leftovers:
        anchor = _quoted_legal_title_anchor(row.get("title"))
        if anchor is not None:
            old_by_quoted_anchor.setdefault(anchor.identity, []).append(row)

    skeletons: dict[int, _TypedStructuralSkeleton] = {}

    def _skeleton(row: dict[str, Any]) -> _TypedStructuralSkeleton:
        document_id = int(row["id"])
        if document_id not in skeletons:
            skeletons[document_id] = _typed_structural_skeleton(row.get("content"))
        return skeletons[document_id]

    claimed_current_ids: set[int] = set()
    for row in leftovers:
        title = _normalise_title(row.get("title"))
        candidates = current_by_title.get(title, [])
        target: Optional[dict[str, Any]] = None
        match_mode = ""
        retention = 0.0
        jaccard = 0.0
        shared_count = 0
        order_preserved = False
        old_size = 0
        new_size = 0
        parser_unsafe_target: Optional[dict[str, Any]] = None
        ambiguous_old_thread_ids: Tuple[int, ...] = ()
        ambiguous_current_thread_ids: Tuple[int, ...] = ()

        exact_title_old = old_by_title.get(title, [])
        if candidates and (
            len(exact_title_old) != 1 or len(candidates) != 1
        ):
            ambiguous_old_thread_ids = tuple(
                int(item["source_thread_id"])
                for item in exact_title_old
            )
            ambiguous_current_thread_ids = tuple(
                int(item["source_thread_id"])
                for item in candidates
            )
        elif len(exact_title_old) == 1 and len(candidates) == 1:
            exact_title_target = candidates[0]
            old_skeleton = _skeleton(row)
            new_skeleton = _skeleton(exact_title_target)
            if old_skeleton.parser_safe and new_skeleton.parser_safe:
                old_codes = _structural_codes(row.get("content"))
                new_codes = _structural_codes(exact_title_target.get("content"))
                strong, retention, jaccard = _structural_similarity(
                    old_codes,
                    new_codes,
                )
            else:
                old_codes = frozenset()
                new_codes = frozenset()
                strong = False
                parser_unsafe_target = exact_title_target
            if strong:
                target = exact_title_target
                match_mode = "exact-title-articles-v1"
                shared_count = len(old_codes & new_codes)
                old_size = len(old_codes)
                new_size = len(new_codes)
                order_preserved = True  # legacy set proof is intentionally unchanged
            else:
                (
                    strong,
                    retention,
                    jaccard,
                    shared_count,
                    order_preserved,
                ) = _section_primary_similarity(old_skeleton, new_skeleton)
                if strong:
                    target = exact_title_target
                    match_mode = "exact-title-section-primary-v1"
                    section_kinds = frozenset({"chapter", "section"})
                    old_size = len(
                        _ordered_unique_typed_codes(
                            old_skeleton,
                            kinds=section_kinds,
                        )
                    )
                    new_size = len(
                        _ordered_unique_typed_codes(
                            new_skeleton,
                            kinds=section_kinds,
                        )
                    )

        # Exact-title paths were either unavailable or structurally too weak.
        # A jurisdiction-neutral quoted title can select a target only when
        # the quoted subject itself is unique on both sides.
        if target is None and not candidates:
            quoted_anchor = _quoted_legal_title_anchor(row.get("title"))
            quoted_candidates = (
                current_by_quoted_anchor.get(quoted_anchor.identity, [])
                if quoted_anchor is not None
                else []
            )
            quoted_target_anchor = (
                _quoted_legal_title_anchor(quoted_candidates[0].get("title"))
                if len(quoted_candidates) == 1
                else None
            )
            quoted_old = (
                old_by_quoted_anchor.get(quoted_anchor.identity, [])
                if quoted_anchor is not None
                else []
            )
            if quoted_candidates and (
                len(quoted_old) != 1 or len(quoted_candidates) != 1
            ):
                ambiguous_old_thread_ids = tuple(
                    int(item["source_thread_id"])
                    for item in quoted_old
                )
                ambiguous_current_thread_ids = tuple(
                    int(item["source_thread_id"])
                    for item in quoted_candidates
                )
            if (
                quoted_anchor is not None
                and quoted_target_anchor is not None
                and _quoted_title_change_is_neutral(
                    quoted_anchor,
                    quoted_target_anchor,
                )
                and len(quoted_old) == 1
                and len(quoted_candidates) == 1
            ):
                quoted_target = quoted_candidates[0]
                old_skeleton = _skeleton(row)
                new_skeleton = _skeleton(quoted_target)
                if (
                    not old_skeleton.parser_safe
                    or not new_skeleton.parser_safe
                ):
                    parser_unsafe_target = quoted_target
                (
                    strong,
                    retention,
                    jaccard,
                    shared_count,
                    order_preserved,
                ) = _quoted_bare_article_similarity(
                    old_skeleton,
                    new_skeleton,
                )
                if strong:
                    target = quoted_target
                    match_mode = "quoted-subject-bare-articles-v1"
                    article_kinds = frozenset({"article"})
                    old_size = len(
                        _ordered_unique_typed_codes(
                            old_skeleton,
                            kinds=article_kinds,
                        )
                    )
                    new_size = len(
                        _ordered_unique_typed_codes(
                            new_skeleton,
                            kinds=article_kinds,
                        )
                    )

        if target is None:
            if ambiguous_old_thread_ids or ambiguous_current_thread_ids:
                _refuse(
                    row,
                    "ambiguous_structural_match",
                    (
                        "A structural title anchor resolves to more than one "
                        "old or current document; destructive cleanup is "
                        "blocked because identity cannot be guessed."
                    ),
                    old_thread_ids=(
                        ambiguous_old_thread_ids
                        or (int(row["source_thread_id"]),)
                    ),
                    current_thread_ids=ambiguous_current_thread_ids,
                )
            elif parser_unsafe_target is not None:
                _refuse(
                    row,
                    "structural_candidate_parser_unsafe",
                    (
                        "A unique title-anchored replacement candidate has an "
                        "ambiguous or partial parser plan; destructive cleanup "
                        "is blocked until the source can be classified safely."
                    ),
                    current_thread_ids=(
                        int(parser_unsafe_target["source_thread_id"]),
                    ),
                )
            else:
                _refuse(
                    row,
                    "old_document_has_no_exact_current_match",
                    "No current document has exactly the same normalised title and full content.",
                )
            logger.info(
                "[HANDOFF] structural pairing refused for thread=%s title=%r "
                "(last profile old=%d new=%d shared=%d retention=%.2f "
                "jaccard=%.2f ordered=%s)",
                row.get("source_thread_id"),
                title,
                old_size,
                new_size,
                shared_count,
                retention,
                jaccard,
                order_preserved,
            )
            continue

        target_id = int(target["id"])
        if target_id in claimed_current_ids:
            _refuse(
                row,
                "ambiguous_structural_match",
                "Two missing documents claim the same replacement; identity is not guessed.",
            )
            continue

        claimed_current_ids.add(target_id)
        pairs.append((digests.get(int(row["id"]), ""), row, target))
        logger.warning(
            "[HANDOFF] structural identity kept: thread %s -> %s title=%r "
            "mode=%s (shared=%d old=%d new=%d retention=%.2f "
            "jaccard=%.2f ordered=%s)",
            row.get("source_thread_id"),
            target.get("source_thread_id"),
            title,
            match_mode,
            shared_count,
            old_size,
            new_size,
            retention,
            jaccard,
            order_preserved,
        )

    return pairs, warnings


async def _handoff_with_connection(
    conn: Any,
    *,
    server_id: str,
    missing_thread_ids: Tuple[int, ...],
    current_thread_ids: Tuple[int, ...],
) -> ExactThreadHandoffResult:
    _require_active_transaction(conn)

    warnings: list[ExactThreadHandoffWarning] = []
    handoffs: list[ExactThreadHandoff] = []

    overlap = set(missing_thread_ids) & set(current_thread_ids)
    if overlap:
        _warning(
            warnings,
            code="overlapping_thread_ids",
            message=(
                "A thread id cannot be both missing and currently live in the "
                "same sync; overlapping ids were ignored."
            ),
            old_thread_ids=overlap,
            current_thread_ids=overlap,
        )

    old_ids = tuple(thread_id for thread_id in missing_thread_ids if thread_id not in overlap)
    current_ids = tuple(
        thread_id for thread_id in current_thread_ids if thread_id not in overlap
    )
    candidate_ids = tuple(sorted(set(old_ids) | set(current_ids)))

    if not candidate_ids:
        if not warnings:
            _warning(
                warnings,
                code="no_candidates",
                message="No missing/current thread ids were supplied.",
            )
        return ExactThreadHandoffResult(
            server_id=server_id,
            missing_thread_ids=missing_thread_ids,
            current_thread_ids=current_thread_ids,
            handoffs=(),
            warnings=tuple(warnings),
        )

    # Lock candidate parents in deterministic order.  Besides serialising two
    # handoff attempts, FOR UPDATE prevents a concurrent FK insert from adding
    # a new article/chunk after the "current target has no dependents" check.
    document_rows = [
        dict(row)
        for row in await conn.fetch(
            """
            SELECT id, server_id, source_thread_id, title, content
            FROM law_documents
            WHERE server_id = $1
              AND source_thread_id = ANY($2::int[])
            ORDER BY source_thread_id, id
            FOR UPDATE
            """,
            server_id,
            list(candidate_ids),
        )
    ]

    rows_by_thread: dict[int, list[dict[str, Any]]] = {}
    for row in document_rows:
        rows_by_thread.setdefault(int(row["source_thread_id"]), []).append(row)

    old_rows: list[dict[str, Any]] = []
    current_rows: list[dict[str, Any]] = []
    for thread_id in old_ids:
        rows = rows_by_thread.get(thread_id, [])
        if len(rows) != 1:
            _warning(
                warnings,
                code="old_document_not_found" if not rows else "duplicate_old_document",
                message=(
                    "The missing thread did not resolve to exactly one document "
                    "on the requested server."
                ),
                old_thread_ids=(thread_id,),
            )
            continue
        old_rows.append(rows[0])

    for thread_id in current_ids:
        rows = rows_by_thread.get(thread_id, [])
        if len(rows) != 1:
            _warning(
                warnings,
                code=(
                    "current_document_not_found"
                    if not rows
                    else "duplicate_current_document"
                ),
                message=(
                    "The current/live thread did not resolve to exactly one "
                    "document on the requested server."
                ),
                current_thread_ids=(thread_id,),
            )
            continue
        current_rows.append(rows[0])

    # The full content is part of the grouping key as a deliberate second
    # equality check, and the normalised source title is an independent exact
    # anchor.  Even a theoretical SHA-256 collision cannot hand off an
    # unrelated document.
    old_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    current_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    for side, rows, groups in (
        ("old", old_rows, old_groups),
        ("current", current_rows, current_groups),
    ):
        for row in rows:
            content = row.get("content")
            thread_id = int(row["source_thread_id"])
            content_is_safe, content_reason = _content_quality(content)
            if not content_is_safe:
                _warning(
                    warnings,
                    code=f"{side}_document_{content_reason}",
                    message=(
                        "Empty, short, or low-information content is never safe "
                        "evidence of document identity."
                    ),
                    old_thread_ids=(thread_id,) if side == "old" else (),
                    current_thread_ids=(thread_id,) if side == "current" else (),
                )
                continue
            normalised_title = _normalise_title(row.get("title"))
            if not normalised_title:
                _warning(
                    warnings,
                    code=f"{side}_document_has_no_title",
                    message=(
                        "A non-empty normalised document title is required as an "
                        "independent identity anchor."
                    ),
                    old_thread_ids=(thread_id,) if side == "old" else (),
                    current_thread_ids=(thread_id,) if side == "current" else (),
                )
                continue
            key = (_sha256_text(content), content, normalised_title)
            groups.setdefault(key, []).append(row)

    candidate_pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    paired_old_ids: set[int] = set()
    paired_current_ids: set[int] = set()
    unmatched_old: list[tuple[str, dict[str, Any]]] = []
    for key in sorted(
        set(old_groups) | set(current_groups),
        key=lambda item: (item[0], item[2]),
    ):
        digest, _content, _normalised_title = key
        matching_old = old_groups.get(key, [])
        matching_current = current_groups.get(key, [])
        old_threads = tuple(int(row["source_thread_id"]) for row in matching_old)
        current_threads = tuple(
            int(row["source_thread_id"]) for row in matching_current
        )

        if len(matching_old) == 1 and len(matching_current) == 1:
            candidate_pairs.append((digest, matching_old[0], matching_current[0]))
            paired_old_ids.add(int(matching_old[0]["id"]))
            paired_current_ids.add(int(matching_current[0]["id"]))
        elif matching_old and matching_current:
            _warning(
                warnings,
                code="ambiguous_exact_match",
                message=(
                    "Exact title/content exists in more than one old or current "
                    "candidate; identity cannot be chosen safely."
                ),
                content_sha256=digest,
                old_thread_ids=old_threads,
                current_thread_ids=current_threads,
            )
        elif matching_old:
            # Byte equality failed.  Before accepting the loss, try the structural
            # path below; only if that also refuses does the old warning stand.
            for row in matching_old:
                unmatched_old.append((digest, row))
        # A live/current document without a matching missing document is the
        # normal case when callers pass the full discovery set.  It is not a
        # handoff warning and makes retries safe after the target row already
        # exists.

    structural_pairs, structural_warnings = _structural_candidate_pairs(
        old_rows=[row for _digest, row in unmatched_old],
        current_rows=current_rows,
        paired_old_ids=paired_old_ids,
        paired_current_ids=paired_current_ids,
        digests={int(row["id"]): digest for digest, row in unmatched_old},
    )
    candidate_pairs.extend(structural_pairs)
    warnings.extend(structural_warnings)

    if not candidate_pairs:
        return ExactThreadHandoffResult(
            server_id=server_id,
            missing_thread_ids=missing_thread_ids,
            current_thread_ids=current_thread_ids,
            handoffs=(),
            warnings=tuple(warnings),
        )

    # Lock all known dependents before counting/moving them.  Parent row locks
    # prevent new FK inserts; child locks prevent concurrent reindex/delete
    # from changing the rows between our check and the handoff.
    document_ids = sorted(
        {
            int(row["id"])
            for _digest, old_row, new_row in candidate_pairs
            for row in (old_row, new_row)
        }
    )
    article_rows = await conn.fetch(
        """
        SELECT id, law_document_id, server_id
        FROM law_articles
        WHERE law_document_id = ANY($1::int[])
        ORDER BY law_document_id, id
        FOR UPDATE
        """,
        document_ids,
    )
    chunk_rows = await conn.fetch(
        """
        SELECT id, law_document_id, server_id
        FROM law_chunks
        WHERE law_document_id = ANY($1::int[])
        ORDER BY law_document_id, id
        FOR UPDATE
        """,
        document_ids,
    )

    article_counts: dict[int, int] = {}
    chunk_counts: dict[int, int] = {}
    child_server_mismatches: dict[int, list[int]] = {}
    for row in article_rows:
        doc_id = int(row["law_document_id"])
        article_counts[doc_id] = article_counts.get(doc_id, 0) + 1
        if row.get("server_id") != server_id:
            child_server_mismatches.setdefault(doc_id, []).append(int(row["id"]))
    for row in chunk_rows:
        doc_id = int(row["law_document_id"])
        chunk_counts[doc_id] = chunk_counts.get(doc_id, 0) + 1
        if row.get("server_id") != server_id:
            child_server_mismatches.setdefault(doc_id, []).append(int(row["id"]))

    safe_pairs: list[
        tuple[str, dict[str, Any], dict[str, Any], int, int]
    ] = []
    for digest, old_row, new_row in candidate_pairs:
        old_document_id = int(old_row["id"])
        new_document_id = int(new_row["id"])
        if new_document_id <= old_document_id:
            _warning(
                warnings,
                code="target_document_not_newer",
                message=(
                    "The current target row must have a greater document id than "
                    "the missing source row; reverse/uncertain handoffs are refused."
                ),
                content_sha256=digest,
                old_thread_ids=(int(old_row["source_thread_id"]),),
                current_thread_ids=(int(new_row["source_thread_id"]),),
            )
            continue
        if (
            old_document_id in child_server_mismatches
            or new_document_id in child_server_mismatches
        ):
            _warning(
                warnings,
                code="child_server_mismatch",
                message=(
                    "An article or chunk belongs to a different server than its "
                    "candidate document; no dependent rows were moved."
                ),
                content_sha256=digest,
                old_thread_ids=(int(old_row["source_thread_id"]),),
                current_thread_ids=(int(new_row["source_thread_id"]),),
            )
            continue
        new_article_count = article_counts.get(new_document_id, 0)
        new_chunk_count = chunk_counts.get(new_document_id, 0)
        if new_article_count or new_chunk_count:
            _warning(
                warnings,
                code="current_document_has_dependents",
                message=(
                    "The current target document already has articles or chunks; "
                    "merging two indexed identities is intentionally not attempted."
                ),
                content_sha256=digest,
                old_thread_ids=(int(old_row["source_thread_id"]),),
                current_thread_ids=(int(new_row["source_thread_id"]),),
            )
            continue
        safe_pairs.append(
            (
                digest,
                old_row,
                new_row,
                article_counts.get(old_document_id, 0),
                chunk_counts.get(old_document_id, 0),
            )
        )

    for digest, old_row, new_row, article_count, chunk_count in safe_pairs:
        old_document_id = int(old_row["id"])
        new_document_id = int(new_row["id"])

        marked_target_id = await conn.fetchval(
            """
            UPDATE law_documents
            SET derived_sync_pending = TRUE
            WHERE id = $1
              AND server_id = $2
              AND source_thread_id = $3
            RETURNING id
            """,
            new_document_id,
            server_id,
            int(new_row["source_thread_id"]),
        )
        if marked_target_id != new_document_id:
            raise ExactThreadHandoffInvariantError(
                "Locked target law_document disappeared before pending marker"
            )

        article_status = await conn.execute(
            """
            UPDATE law_articles
            SET law_document_id = $1
            WHERE law_document_id = $2
            """,
            new_document_id,
            old_document_id,
        )
        moved_articles = _command_count(article_status, "UPDATE")
        if moved_articles != article_count:
            raise ExactThreadHandoffInvariantError(
                "Locked law_articles count changed during exact thread handoff"
            )

        chunk_status = await conn.execute(
            """
            UPDATE law_chunks
            SET law_document_id = $1
            WHERE law_document_id = $2
            """,
            new_document_id,
            old_document_id,
        )
        moved_chunks = _command_count(chunk_status, "UPDATE")
        if moved_chunks != chunk_count:
            raise ExactThreadHandoffInvariantError(
                "Locked law_chunks count changed during exact thread handoff"
            )

        deleted_id = await conn.fetchval(
            """
            DELETE FROM law_documents
            WHERE id = $1
              AND server_id = $2
              AND source_thread_id = $3
            RETURNING id
            """,
            old_document_id,
            server_id,
            int(old_row["source_thread_id"]),
        )
        if deleted_id != old_document_id:
            raise ExactThreadHandoffInvariantError(
                "Locked old law_document disappeared during exact thread handoff"
            )

        handoffs.append(
            ExactThreadHandoff(
                server_id=server_id,
                old_document_id=old_document_id,
                target_document_id=new_document_id,
                old_thread_id=int(old_row["source_thread_id"]),
                target_thread_id=int(new_row["source_thread_id"]),
                content_sha256=digest,
                article_count=article_count,
                chunk_count=chunk_count,
            )
        )

    return ExactThreadHandoffResult(
        server_id=server_id,
        missing_thread_ids=missing_thread_ids,
        current_thread_ids=current_thread_ids,
        handoffs=tuple(handoffs),
        warnings=tuple(warnings),
    )


async def handoff_exact_thread_replacements(
    server_id: str,
    missing_thread_ids: Iterable[int],
    current_thread_ids: Iterable[int],
    *,
    conn: Any = None,
) -> ExactThreadHandoffResult:
    """Hand off exact, unique forum-thread replacements for one server.

    ``current_thread_ids`` may be the complete set of live/discovered forum
    threads, not only rows inserted during this process.  This makes retries
    safe: an already-created target remains eligible while unmatched current
    documents are ignored.

    When ``conn`` is supplied, ``conn.is_in_transaction()`` must already be
    true; no commit/rollback is performed here.  Without ``conn`` the complete
    operation runs in one ``db_manager.transaction()`` context.

    Ambiguous, non-exact, missing, low-information, reverse-id, and
    already-indexed target documents are reported and not mutated.  Other
    independent exact 1:1 pairs in the same call may still be handed off.
    """

    clean_server_id = str(server_id or "").strip()
    if not clean_server_id:
        raise ValueError("server_id must not be empty")

    old_ids = _normalise_thread_ids(
        missing_thread_ids,
        label="missing_thread_ids",
    )
    live_ids = _normalise_thread_ids(
        current_thread_ids,
        label="current_thread_ids",
    )

    if conn is not None:
        return await _handoff_with_connection(
            conn,
            server_id=clean_server_id,
            missing_thread_ids=old_ids,
            current_thread_ids=live_ids,
        )

    async with db_manager.transaction() as owned_conn:
        return await _handoff_with_connection(
            owned_conn,
            server_id=clean_server_id,
            missing_thread_ids=old_ids,
            current_thread_ids=live_ids,
        )


_ALLOWED_CLEANUP_WARNING_CODES = {
    # This is the normal, deliberately conservative case: the old forum topic
    # truly disappeared and no byte-identical replacement can be proven.
    "old_document_has_no_exact_current_match",
}
_ALWAYS_BLOCKING_CLEANUP_WARNING_CODES = {
    "overlapping_thread_ids",
    "no_candidates",
    "old_document_not_found",
    "duplicate_old_document",
    "current_document_not_found",
    "duplicate_current_document",
    "current_document_no_content",
    "current_document_low_information_content",
    "current_document_has_no_title",
}


def _blocking_cleanup_warnings(
    warnings: Iterable[ExactThreadHandoffWarning],
) -> Tuple[ExactThreadHandoffWarning, ...]:
    """Return warnings which make a following bulk DELETE unsafe."""

    return tuple(
        warning
        for warning in warnings
        if warning.code not in _ALLOWED_CLEANUP_WARNING_CODES
        and (
            bool(warning.old_thread_ids)
            or warning.code in _ALWAYS_BLOCKING_CLEANUP_WARNING_CODES
        )
    )


async def reconcile_missing_thread_documents(
    conn: Any,
    *,
    server_id: str,
    missing_thread_ids: Iterable[int],
    current_thread_ids: Iterable[int],
) -> MissingThreadReconciliationResult:
    """Atomically hand off exact replacements and delete genuine removals.

    The caller owns both the surrounding per-server advisory lock and the
    active transaction.  Keeping the warning check and the ordinary DELETE in
    that same transaction is essential: if one candidate is unsafe, even
    earlier safe handoffs in this call must roll back.

    ``current_thread_ids`` must be the caller's complete live set whose parse,
    database persistence, and canonical GCS upload all succeeded in this sync.
    A missing or only partially persisted current topic is not safe identity
    evidence and must be rejected before calling this helper.

    GCS is intentionally outside this helper.  ``gcs_uris`` contains only old
    source objects and may be deleted by the caller *after* the transaction
    commits successfully.
    """

    _require_active_transaction(conn)
    clean_server_id = str(server_id or "").strip()
    if not clean_server_id:
        raise ValueError("server_id must not be empty")

    old_ids = _normalise_thread_ids(
        missing_thread_ids,
        label="missing_thread_ids",
    )
    live_ids = _normalise_thread_ids(
        current_thread_ids,
        label="current_thread_ids",
    )
    if not old_ids:
        empty_handoff = ExactThreadHandoffResult(
            server_id=clean_server_id,
            missing_thread_ids=(),
            current_thread_ids=live_ids,
            handoffs=(),
            warnings=(),
        )
        return MissingThreadReconciliationResult(
            server_id=clean_server_id,
            missing_thread_ids=(),
            handoff_result=empty_handoff,
            removed_documents=(),
            ordinary_deleted_thread_ids=(),
        )

    # Capture cleanup metadata while locking every row which the following
    # handoff/delete may remove.  It remains available after the old parent
    # rows have gone and lets the caller defer GCS deletion until after commit.
    old_rows = [
        dict(row)
        for row in await conn.fetch(
            """
            SELECT id, source_thread_id, title, gemini_file_uri
            FROM law_documents
            WHERE server_id = $1
              AND source_thread_id = ANY($2::int[])
            ORDER BY source_thread_id, id
            FOR UPDATE
            """,
            clean_server_id,
            list(old_ids),
        )
    ]
    old_rows_by_thread: dict[int, list[dict[str, Any]]] = {}
    for row in old_rows:
        old_rows_by_thread.setdefault(int(row["source_thread_id"]), []).append(row)
    unresolved_old_ids = tuple(
        thread_id
        for thread_id in old_ids
        if len(old_rows_by_thread.get(thread_id, ())) != 1
    )
    if unresolved_old_ids:
        raise ExactThreadHandoffInvariantError(
            "Missing thread ids did not resolve to exactly one locked "
            f"law_document each: {unresolved_old_ids!r}"
        )

    handoff_result = await _handoff_with_connection(
        conn,
        server_id=clean_server_id,
        missing_thread_ids=old_ids,
        current_thread_ids=live_ids,
    )
    blockers = _blocking_cleanup_warnings(handoff_result.warnings)
    if blockers:
        blocker_codes = tuple(sorted({warning.code for warning in blockers}))
        raise ExactThreadHandoffInvariantError(
            "Exact thread reconciliation refused destructive cleanup after "
            f"blocking warnings: {blocker_codes!r}"
        )

    handoffs_by_old_thread = {
        handoff.old_thread_id: handoff
        for handoff in handoff_result.handoffs
    }
    ordinary_ids = tuple(
        thread_id
        for thread_id in old_ids
        if thread_id not in handoffs_by_old_thread
    )
    if ordinary_ids:
        deleted_rows = await conn.fetch(
            """
            DELETE FROM law_documents
            WHERE server_id = $1
              AND source_thread_id = ANY($2::int[])
            RETURNING id, source_thread_id
            """,
            clean_server_id,
            list(ordinary_ids),
        )
        deleted_ids = tuple(
            sorted(int(row["source_thread_id"]) for row in deleted_rows)
        )
        if deleted_ids != ordinary_ids:
            raise ExactThreadHandoffInvariantError(
                "Locked law_documents changed during ordinary missing-thread "
                f"cleanup: expected={ordinary_ids!r}, deleted={deleted_ids!r}"
            )

    removed_documents = []
    for thread_id in old_ids:
        row = old_rows_by_thread[thread_id][0]
        handoff = handoffs_by_old_thread.get(thread_id)
        removed_documents.append(
            RemovedLawDocument(
                document_id=int(row["id"]),
                thread_id=thread_id,
                title=str(row.get("title") or ""),
                gcs_uri=(
                    str(row["gemini_file_uri"])
                    if row.get("gemini_file_uri")
                    else None
                ),
                handoff_target_thread_id=(
                    handoff.target_thread_id if handoff else None
                ),
            )
        )

    return MissingThreadReconciliationResult(
        server_id=clean_server_id,
        missing_thread_ids=old_ids,
        handoff_result=handoff_result,
        removed_documents=tuple(removed_documents),
        ordinary_deleted_thread_ids=ordinary_ids,
    )


__all__ = [
    "ExactThreadHandoff",
    "ExactThreadHandoffInvariantError",
    "ExactThreadHandoffResult",
    "ExactThreadHandoffWarning",
    "MissingThreadReconciliationResult",
    "RemovedLawDocument",
    "handoff_exact_thread_replacements",
    "reconcile_missing_thread_documents",
]
