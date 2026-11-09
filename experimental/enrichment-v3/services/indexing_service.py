# START OF FILE services/indexing_service.py

"""
Индексация законки в семантический индекс (law_chunks) для RAG голосового ассистента.

Пайплайн: law_documents.content → структурный чанкинг (по статьям/главам, с дроблением
крупных блоков по размеру) → эмбеддинги gemini-embedding-001 → апсерт в law_chunks (pgvector).

Переиндексацию можно дёргать вручную или из существующего хука обновления базы знаний.
"""

from __future__ import annotations

import logging
import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import config
from database.manager import db_manager
from services.embeddings import embed_texts, to_pgvector, TASK_DOCUMENT
from services.law_article_dialect import classify_article_dialect
from services.law_structural_code import (
    canonical_roman_value,
    normalize_roman_structural_token,
)

logger = logging.getLogger(__name__)

# Заголовки структурных единиц закона: «Статья 12», «Глава III», «Раздел 2», «§ 5», «Пункт 4».
_HEADER_RE = re.compile(
    r'(?im)^[ \t]*((?:'
    r'(?:статья\.?|ст\.|пункт|п\.|§)\s*[№N]?\s*'
    r'(?:\d[\d.]*|[IVXLCMМ]+(?![A-Za-zА-Яа-яЁё]))'
    r'|(?:глава|раздел)\s*[№N]?\s*'
    r'(?:\d[\d.]*|[IVXLCMІХСМ\u2160-\u217F]+'
    r'(?![A-Za-zА-Яа-яЁё]))'
    r')[^\n]*)$'
)
# Голый номер статьи БЕЗ слова «Статья»: строка начинается с «N.M» (напр. УК phoenix: «1.1», «3.7»).
# `(?!\.\d)` отсекает трёхуровневые «N.M.K» — это ЧАСТИ статьи, отдельными статьями не режем.
# `(?![.)])` не даёт спутать со списком «1.» / «1)». Применяется ТОЛЬКО к сводам без «Статья»
# (см. split_into_blocks) → документы со «Статья» не затрагиваются (0 регрессий на др. серверах).
_BARE_HEADER_RE = re.compile(r'(?im)^[ \t]*(\d+\.\d+(?!\.\d)\.?[ \t]*(?![.)\d])[^\n]*)$')
_STATYA_KW_RE = re.compile(r'(?i)^\s*(?:статья|ст\.)')
_ABBREVIATED_ARTICLE_PREFIX_RE = re.compile(
    r'(?i)^\s*ст\.\s*[№N]?\s*\d+(?:[.-]\d+)*\.?'
)
_CITATION_LIST_CONTINUATION_RE = re.compile(
    r'(?i)^\s*[,;]\s*(?:ст\.?\s*)?\d+(?:[.-]\d+)+'
)
_CROSS_CODE_CITATION_RE = re.compile(
    r'(?i)^\s*ст\.\s*[№N]?\s*\d+(?:[.-]\d+)*\.?\s+'
    r'(?:УК|УПК|УАК|АК|ПК|ДК|КоАП)(?:\s+(?:SA|СА|РФ))?\.?\s*$'
)
_STRUCTURAL_SELF_REFERENCE_RE = re.compile(
    r'(?i)^\s*(?:статья\.?|ст\.|пункт|п\.)\s*[№N]?\s*'
    r'\d+(?:[.\-–]\d+)*\.?\s+'
    r'настоящ(?:его|ей)\s+'
    r'(?:закона|кодекса|статьи|главы|раздела)\b'
)
_REVISION_DATE_MARKER_RE = re.compile(
    r'(?i)^[ \t]*редакци(?:я|и)[ \t]+от[ \t]+'
    r'\d{2}\.\d{2}\.\d{4}[ \t]*$'
)
_REVISION_CHANGELOG_ARTICLE_RE = re.compile(
    r'(?i)^[ \t]*статья[ \t]+(?:№|N)?[ \t]*'
    r'\d+(?:[.\-–]\d+)*[ \t]+'
    r'главы[ \t]+(?:№|N)?[ \t]*'
    r'(?:\d+(?:\.\d+)*|[IVXLCMІХСМ\u2160-\u217F]+)'
    r'[ \t]*(?::|[—–-])[ \t]*'
    r'(?:'
    r'уточн(?:ен|ён)(?:а|о|ы)?'
    r'|измен(?:ен|ён)(?:а|о|ы)?'
    r'|добавлен(?:а|о|ы)?'
    r'|удал(?:ен|ён)(?:а|о|ы)?'
    r'|убран(?:а|о|ы)?'
    r'|исправлен(?:а|о|ы)?'
    r'|перенес(?:ен|ён)(?:а|о|ы)?'
    r'|дополнен(?:а|о|ы)?'
    r'|скорректирован(?:а|о|ы)?'
    r'|замен(?:ен|ён)(?:а|о|ы)?'
    r'|актуализирован(?:а|о|ы)?'
    r'|внес(?:ен|ён)(?:а|о|ы)?'
    r')\b[^\r\n]*$'
)
_STRUCTURAL_REFERENCE_LABEL_RE = re.compile(
    r'(?i)^\s*(?:'
    r'исключение'
    r'|примечание(?:\s*(?:№|no\.?)?\s*\d+)?'
    r')\s*:?\s*$'
)
_STANDALONE_ARTICLE_RE = re.compile(
    r'(?i)^\s*статья\s+[№N]?\s*(\d+(?:[.-]\d+)*)\.?\s*$'
)
_STANDALONE_DOTTED_CLAUSE_RE = re.compile(
    r'^\s*(\d+)\.(\d+)\.?\s*$'
)
_BARE_DIRECT_CITATION_CONTINUATION_RE = re.compile(
    r'(?i)^\s*(?:[,;)]|(?:и|или)\s+п\.)'
)
_EXACT_BARE_HEADER_RE = re.compile(r'^\s*\d+\.\d+\.?\s*$')
_BARE_GENITIVE_CONTINUATION_RE = re.compile(
    r'(?i)^\s*настоящего\s+(?:кодекса|закона)\b'
)
_CITATION_CONNECTOR_LINE_RE = re.compile(
    r'(?i)^\s*(?:и|или)\s+п\.\s*$'
)
_BARE_REFERENCE_INTRO_RE = re.compile(
    r'(?i)(?:'
    r'\b(?:по|из|согласно)\s+(?:статье|статьи|ст\.)'
    r'|\bв\s+соответствии\s+со\s+статьями'
    r'|\b(?:предусмотрен\w*|указан\w*|перечислен\w*)'
    r'[^\r\n]{0,64}\b(?:(?:стать(?:е|и|ях|ями)|статей)|'
    r'ст\.(?:\s*ст\.)?)'
    r'|\b(?:стать(?:и|ях|ями)|статей)\s*,\s*как\s+ст\.'
    r'|\b(?:пункт(?:а|ов|е|у|ы|ами|ах)|п\.)[^\r\n]{0,50},\s*'
    r'(?:стать(?:и|е)|ст\.)'
    r')\s*[,;:]?\s*$'
)
_BARE_REFERENCE_CONNECTOR_LINE_RE = re.compile(
    r'(?i)^\s*[,;]?\s*статьями\s*:?\s*$'
)
_BARE_REFERENCE_CONNECTOR_OWNER_RE = re.compile(
    r'(?i)\b(?:предусмотрен\w*|указан\w*|перечислен\w*)\s*$'
)
_BARE_CROSS_CODE_REFERENCE_RE = re.compile(
    r'(?i)^\s*\d+\.\d+\.?\s*'
    r'(?:УК|УПК|УАК|АК|ПК|ДК|КоАП)'
    r'(?:\s+(?:SA|СА|РФ))?\.?\s*$'
)
_FULL_ARTICLE_REFERENCE_LIST_ITEM_RE = re.compile(
    r'(?i)^\s*статья\s+[№N]?\s*\d+(?:[.\-–]\d+)*\.?\s*'
    r'[-—–]\s*\S.*[;,]\s*$'
)
_PARENT_PROVISION_CONTINUATION_RE = re.compile(
    r'(?i)^\s*(?:ч(?:асть)?\.?|п(?:ункт)?\.?)\s*\d+(?:\.\d+)*\.?\s*$'
)
_SPACED_ARTICLE_SUFFIX_RE = re.compile(
    r'(?i)^\s*(?:статья|ст\.)\s*[№N]?\s*'
    r'(?P<base>\d+(?:[.\-–]\d+)*)\.\s+(?P<suffix>\d+)\.\s*$'
)
_WRAPPED_ARTICLE_BASE_RE = re.compile(
    r'(?i)^\s*(?:статья|ст\.)\s*[№N]?\s*'
    r'(?P<base>\d+(?:[.\-–]\d+)*)\s*$'
)
_WRAPPED_ARTICLE_SUFFIX_AFTER_RE = re.compile(
    r'\n[ \t]*\.(?P<suffix>\d+(?:\.\d+)*)\.?[ \t]*(?=\r?\n|$)'
)
_SPLIT_ARTICLE_KEYWORD_RE = re.compile(
    r'(?im)^[ \t]*(?P<left>[А-Яа-яЁё]{1,5})[ \t]*\r?\n'
    r'[ \t]*(?P<right>[А-Яа-яЁё]{1,5})'
    r'(?P<tail>[ \t]+[№N]?\s*\d[\d.\-–]*[^\n]*)$'
)
_SPLIT_ARTICLE_TRAILING_DOT_RE = re.compile(
    r'\r?\n[ \t]*\.[ \t]*(?=\r?\n|$)'
)
_SPLIT_CHAPTER_KEYWORD_RE = re.compile(
    r'(?im)^[ \t]*(?P<left>[А-Яа-яЁё]{1,4})[ \t]*\r?\n'
    r'[ \t]*(?P<right>[А-Яа-яЁё]{1,4})'
    r'(?P<tail>[ \t]+[№N]?\s*(?:'
    r'\d+(?:\.\d+)*'
    r'|[IVXLCMІХСМ\u2160-\u217F]+'
    r')(?![A-Za-zА-Яа-яЁё])[^\n]*)$'
)
_SPLIT_CHAPTER_TRAILING_DOT_RE = re.compile(
    r'\r?\n[ \t]*\.[ \t]*(?=\r?\n|$)'
)
_WRAPPED_CHAPTER_CODE_RE = re.compile(
    r'^[ \t]*глава[ \t]*\r?\n'
    r'[ \t]*(?P<code>[IVXLCMІХСМ\u2160-\u217F]+)[ \t]*\r?$',
    re.IGNORECASE | re.MULTILINE,
)
_CHAPTER_REF_RE = re.compile(
    r'(?i)^\s*глава\s*[№N]?\s*'
    r'(?P<code>\d+(?:\.\d+)*|[IVXLCMІХСМ\u2160-\u217F]+)'
    r'(?![A-Za-zА-Яа-яЁё])(?P<tail>[^\n]*)$'
)
_ISOLATED_ROMAN_FRAGMENT_RE = re.compile(
    r'^(?P<code>[IVXLCMІХСМ\u2160-\u217F])(?P<dot>\.)?$',
    re.IGNORECASE,
)
_ROMAN_CHAPTER_TITLE_LINE_RE = re.compile(r'^\.\s+(?P<title>\S.*)$')
_JUDICIAL_DIRECT_HEADING_RE = re.compile(
    r'(?i)^(?:'
    r'прецедент\s+(?:окружного|верховного)\s+суда\b.{0,180}\s№\s*\S+'
    r'(?:\s+от\s+\S.*)?'
    r'|толкование(?:\s+тк)?\s+по\s+обращению\s+'
    r'(?:в\s+вс|о\s+толковании)\s+№\s*\S+'
    r')\s*$'
)
_JUDICIAL_COURT_BANNER_RE = re.compile(
    r'(?i)^(?:верховный|окружной)\s+суд\s+штата\s+'
    r'[A-Za-zА-Яа-яЁё]+(?:[-\s][A-Za-zА-Яа-яЁё]+)*\s*$'
)
_JUDICIAL_DECISION_BANNER_RE = re.compile(
    r'(?i)^постановление(?:\s+(?:верховного|окружного)\s+суда'
    r'(?:\s+штата\s+[A-Za-zА-Яа-яЁё]+(?:[-\s][A-Za-zА-Яа-яЁё]+)*)?)?\s*$'
)
_JUDICIAL_DATE_RE = re.compile(
    r'(?i)^от\s+\d{1,2}(?:[./-]\d{1,2}[./-]\d{2,4}'
    r'|\s+[а-яё]+\s+\d{4}\s+года?)\s*$'
)
_JUDICIAL_SUBJECT_RE = re.compile(
    r'(?i)^(?:по\s+делу\s+)?о\s+толкован'
)
_ZERO_WIDTH_RE = re.compile(r'[\u200b-\u200d\u2060\ufeff]')
_MIXED_BARE_MARKER_RE = re.compile(
    r'(?im)^[ \t]*\u043e\u0441\u043e\u0431\u0435\u043d\u043d\u0430\u044f'
    r'[ \t]+\u0447\u0430\u0441\u0442\u044c[ \t]*$'
)
_MIXED_CHAPTER_HEADER_RE = re.compile(
    r'(?i)^\s*\u0433\u043b\u0430\u0432\u0430\s*[\u2116N]?\s*'
    r'(\d+)(?!\d)(?!\.\d)'
)
_MIXED_SPECIAL_CODE_RE = re.compile(
    r'(?m)^[ \t]*(?P<code>\d+\.\d+(?:\.\d+)?)\.?[ \t]*(?=\r?$)'
)
_MIXED_SPECIAL_WRAPPED_CODE_RE = re.compile(
    r'(?m)^[ \t]*(?P<prefix>\d+\.\d+)\.?[ \t]*\r?\n'
    r'[ \t]*\.(?P<suffix>\d+)\.?[ \t]*(?=\r?$)'
)
_MIXED_JURISDICTION_AFTER_RE = re.compile(
    r'[\s\u200b-\u200d\u2060\ufeff]*'
    r'\([\s\u200b-\u200d\u2060\ufeff]*'
    r'(?P<scope>F(?:[\s\u200b-\u200d\u2060\ufeff]*/'
    r'[\s\u200b-\u200d\u2060\ufeff]*R)?|R)'
    r'[\s\u200b-\u200d\u2060\ufeff]*\)'
    r'(?=[ \t\u200b-\u200d\u2060\ufeff]*(?:\r?\n|$))',
    re.IGNORECASE,
)
_MIXED_INLINE_CLASSIFICATION_RE = re.compile(
    r'^\(\s*[AFRАФР](?:\s*[,./]\s*(?:CR|[AFRАФР]))*\s*\)\s+\S',
    re.IGNORECASE,
)
_STRUCTURED_ARTICLE_CODE_RE = re.compile(
    r'(?is)^\s*(?:статья\.?|ст\.)\s*[№N]?\s*'
    r'(?P<code>\d+(?:[.\-–]\d+)*)'
)
_SHALLOW_BARE_PARENT_RE = re.compile(
    r'(?m)^[ \t]*(?P<code>\d{1,3})\.[ \t]+'
    r'(?P<title>\S[^\r\n]*)\r?$'
)
_DOTTED_PROVISION_LINE_RE = re.compile(
    r'(?m)^[ \t]*(?P<code>\d{1,3}(?:\.\d{1,3}){1,3})'
    r'\.?[ \t]+(?P<body>\S[^\r\n]*)\r?$'
)
_BARE_MAJOR_PREFIX_RE = re.compile(r'^\s*(?P<major>\d+)\.')
_RECOVERY_NON_PROVISION_PREFIX_RE = re.compile(
    r'(?i)^(?:'
    r'см\.?\b|смотри\b|комментар(?:ий|ия|ии|ию|ием|ие)\b|'
    r'пункт(?:ы|а|у|ом|е)?\b|подпункт(?:ы|а|у|ом|е)?\b|'
    r'элемент(?:ы|а|у|ом|е)?\b|позици(?:я|и|ю|ей)\b|'
    r'рис(?:унок|\.)\b|изображени(?:е|я|ю|ем)\b|'
    r'таблиц(?:а|ы|у|ей)\b|приложени(?:е|я|ю|ем)\b|'
    r'ссылк(?:а|и|у|ой)\b|'
    r'(?:статья\.?|ст\.)\s*[№N]?\s*\d|'
    r'(?:перв(?:ый|ая|ое)|втор(?:ой|ая|ое)|трет(?:ий|ья|ье)|'
    r'\d+[- ]?(?:й|я|е))\s+(?:пункт|элемент|позиция)\b'
    r')'
)
_PARA_SPLIT_RE = re.compile(r'\n\s*\n')


@dataclass(frozen=True)
class MixedSpecialBoundary:
    """Synthetic article boundary for a high-confidence mixed special part."""

    raw_match: re.Match
    code: str
    jurisdiction: str
    marker_end: int
    chapter_start: int
    chapter_major: int

    def start(self) -> int:
        return self.raw_match.start()

    def end(self) -> int:
        return self.raw_match.end()

    def group(self, index: int = 0) -> str:
        if index == 0:
            return self.raw_match.group(0)
        if index == 1:
            # Reuse the existing catalog normalizer without widening the global
            # bare-number grammar (where N.M.K intentionally means a clause).
            return f"Статья {self.code}"
        raise IndexError("no such group")


@dataclass(frozen=True)
class RecoveredParentArticleBoundary:
    """One omitted parent article proven by the surrounding source dialect."""

    raw_match: re.Match
    canonical_ref: str

    def start(self) -> int:
        return self.raw_match.start()

    def end(self) -> int:
        return self.raw_match.end()

    def group(self, index: int = 0) -> str:
        if index == 0:
            return self.raw_match.group(0)
        if index == 1:
            return self.canonical_ref
        raise IndexError("no such group")


@dataclass(frozen=True)
class CanonicalArticleBoundary:
    """A malformed source header exposed through one canonical article ref."""

    raw_match: re.Match
    canonical_ref: str
    source_end: int

    def start(self) -> int:
        return self.raw_match.start()

    def end(self) -> int:
        return self.source_end

    def group(self, index: int = 0) -> str:
        if index == 0:
            return self.raw_match.group(0)
        if index == 1:
            return self.canonical_ref
        raise IndexError("no such group")


@dataclass(frozen=True)
class AdjacentDuplicateArticleBoundary:
    """One article emitted twice on adjacent forum lines."""

    first_match: Any
    second_match: Any
    canonical_ref: str
    body_start: int
    prefix_end: Optional[int] = None

    def start(self) -> int:
        return self.first_match.start()

    def end(self) -> int:
        return self.second_match.end()

    def group(self, index: int = 0) -> str:
        if index == 0:
            return self.canonical_ref
        if index == 1:
            return self.canonical_ref
        raise IndexError("no such group")


@dataclass(frozen=True)
class SplitArticleKeywordBoundary:
    """A real article whose ``Статья`` keyword was split across two lines."""

    raw_match: re.Match
    canonical_ref: str
    source_end: int

    def start(self) -> int:
        return self.raw_match.start()

    def end(self) -> int:
        return self.source_end

    def group(self, index: int = 0) -> str:
        if index == 0:
            return self.raw_match.group(0)
        if index == 1:
            return self.canonical_ref
        raise IndexError("no such group")


@dataclass(frozen=True)
class SplitChapterKeywordBoundary:
    """A structural chapter whose ``Глава`` keyword spans two source lines."""

    raw_match: re.Match
    canonical_ref: str
    source_end: int

    def start(self) -> int:
        return self.raw_match.start()

    def end(self) -> int:
        return self.source_end

    def group(self, index: int = 0) -> str:
        if index == 0:
            return self.raw_match.group(0)
        if index == 1:
            return self.canonical_ref
        raise IndexError("no such group")


@dataclass(frozen=True)
class WrappedChapterCodeBoundary:
    """A chapter whose Roman code and trailing dot occupy separate lines."""

    raw_match: re.Match
    canonical_ref: str
    source_end: int

    def start(self) -> int:
        return self.raw_match.start()

    def end(self) -> int:
        return self.source_end

    def group(self, index: int = 0) -> str:
        if index == 0:
            return self.raw_match.group(0)
        if index == 1:
            return self.canonical_ref
        raise IndexError("no such group")


@dataclass(frozen=True)
class SplitRomanChapterBoundary:
    """A chapter code reconstructed from narrowly proven wrapped Roman glyphs."""

    original_boundary: Any
    canonical_ref: str
    source_end: int

    def start(self) -> int:
        return self.original_boundary.start()

    def end(self) -> int:
        return self.source_end

    def group(self, index: int = 0) -> str:
        if index == 0:
            return self.original_boundary.group(0)
        if index == 1:
            return self.canonical_ref
        raise IndexError("no such group")


@dataclass(frozen=True)
class JudicialMaterialBoundary:
    """Synthetic top-level boundary in a qualified court-material collection."""

    source_start: int
    source_end: int
    ref: str

    def start(self) -> int:
        return self.source_start

    def end(self) -> int:
        return self.source_end

    def group(self, index: int = 0) -> str:
        if index in (0, 1):
            return self.ref
        raise IndexError("no such group")


@dataclass(frozen=True)
class MixedSpecialChapterGroup:
    """Marker-qualified candidates belonging to one structural chapter."""

    chapter_start: int
    chapter_major: int
    candidates: tuple[MixedSpecialBoundary, ...]

    @property
    def accepted(self) -> bool:
        return len(self.candidates) >= 3


@dataclass(frozen=True)
class HeaderMatchPlan:
    """One shared structural/fallback decision for parser and preflight."""

    matches: tuple[Any, ...]
    structural: tuple[Any, ...]
    legacy_bare_candidates: tuple[re.Match, ...]
    mixed_special_candidates: tuple[MixedSpecialBoundary, ...]
    mixed_special_groups: tuple[MixedSpecialChapterGroup, ...]
    accepted_mixed_special_candidates: tuple[MixedSpecialBoundary, ...]
    recovered_parent_articles: tuple[RecoveredParentArticleBoundary, ...]
    recovery_unresolved_bare_candidates: tuple[re.Match, ...]
    structured_article_count: int
    fallback_mode: Optional[str]
    article_dialect_status: str
    article_dialect_mode: Optional[str]
    article_dialect_reasons: tuple[str, ...]
    judicial_material_candidates: tuple[JudicialMaterialBoundary, ...]
    accepted_judicial_materials: tuple[JudicialMaterialBoundary, ...]
    judicial_rejection_reasons: tuple[str, ...]

    @property
    def bare_candidates(self) -> tuple[re.Match, ...]:
        """Compatibility alias for the established pure-bare diagnostic."""

        return self.legacy_bare_candidates

    @property
    def mixed_bare_candidates(self) -> tuple[MixedSpecialBoundary, ...]:
        """Compatibility alias; mixed candidates are a distinct parser path."""

        return self.mixed_special_candidates


def _next_nonempty_line(text: str, position: int) -> str:
    cursor = text.find("\n", position)
    while cursor >= 0:
        start = cursor + 1
        end = text.find("\n", start)
        if end < 0:
            end = len(text)
        line = text[start:end].strip()
        if line:
            return line
        if end >= len(text):
            break
        cursor = end
    return ""


def _previous_nonempty_lines(
    text: str,
    position: int,
    *,
    limit: int,
) -> List[str]:
    lines: List[str] = []
    cursor = position
    while cursor > 0 and len(lines) < limit:
        line_end = text.rfind("\n", 0, cursor)
        if line_end < 0:
            line = text[:cursor].strip()
            if line:
                lines.append(line)
            break
        line_start = text.rfind("\n", 0, line_end) + 1
        line = text[line_start:line_end].strip()
        if line:
            lines.append(line)
        cursor = line_start
    return lines


def _clean_source_line(value: str) -> str:
    """Remove invisible forum separators without changing source offsets."""

    return _ZERO_WIDTH_RE.sub('', value or '').strip()


def _previous_physical_source_line(
    text: str,
    position: int,
) -> Optional[tuple[str, int]]:
    """Return the immediately preceding physical line and its source start."""

    if position <= 0 or text[position - 1] != '\n':
        return None
    line_end = position - 1
    if line_end > 0 and text[line_end - 1] == '\r':
        line_end -= 1
    line_start = text.rfind('\n', 0, line_end) + 1
    return text[line_start:line_end], line_start


def _is_revision_changelog_article_boundary(
    text: str,
    match: re.Match,
) -> bool:
    """Reject only an article-shaped edit note next to an exact revision date."""

    ref = _clean_source_line(match.group(1))
    if _REVISION_CHANGELOG_ARTICLE_RE.fullmatch(ref) is None:
        return False

    previous = _previous_physical_source_line(text, match.start())
    if previous is None:
        return False
    previous_line, previous_start = previous
    if not _clean_source_line(previous_line):
        previous = _previous_physical_source_line(text, previous_start)
        if previous is None:
            return False
        previous_line, _previous_start = previous
        if not _clean_source_line(previous_line):
            return False

    return (
        _REVISION_DATE_MARKER_RE.fullmatch(
            _clean_source_line(previous_line)
        )
        is not None
    )


def _source_line_records(text: str) -> List[tuple[int, int, str]]:
    """Return ``(start, visible_end, cleaned_line)`` with original offsets."""

    records: List[tuple[int, int, str]] = []
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        source_line = raw_line.rstrip('\r\n')
        records.append(
            (
                offset,
                offset + len(source_line),
                _clean_source_line(source_line),
            )
        )
        offset += len(raw_line)
    if offset < len(text):
        source_line = text[offset:]
        records.append(
            (
                offset,
                len(text),
                _clean_source_line(source_line),
            )
        )
    return records


def _article_reference_list_starts(text: str) -> set[int]:
    """Find only explicit multi-line lists of references to another law.

    A dash or semicolon by itself is not enough: the list must contain at least
    two consecutive ``Статья N — ...;`` items, follow an introducing sentence
    about the *following articles* of a law/code, and be followed by the next
    numbered part of the parent article.
    """

    records = _source_line_records(text)
    starts: set[int] = set()
    index = 0
    while index < len(records):
        if _FULL_ARTICLE_REFERENCE_LIST_ITEM_RE.fullmatch(records[index][2]) is None:
            index += 1
            continue

        item_indexes: List[int] = []
        cursor = index
        while cursor < len(records):
            cleaned = records[cursor][2]
            if _FULL_ARTICLE_REFERENCE_LIST_ITEM_RE.fullmatch(cleaned):
                item_indexes.append(cursor)
                cursor += 1
                continue
            if not cleaned:
                cursor += 1
                continue
            break

        previous = index - 1
        while previous >= 0 and not records[previous][2]:
            previous -= 1
        following = cursor
        while following < len(records) and not records[following][2]:
            following += 1

        intro = records[previous][2] if previous >= 0 else ''
        intro_folded = intro.casefold()
        qualified_intro = (
            intro.endswith(':')
            and 'следующ' in intro_folded
            and 'стат' in intro_folded
            and ('кодекс' in intro_folded or 'закон' in intro_folded)
        )
        qualified_continuation = (
            following < len(records)
            and _PARENT_PROVISION_CONTINUATION_RE.fullmatch(
                records[following][2]
            )
            is not None
        )
        if (
            len(item_indexes) >= 2
            and qualified_intro
            and qualified_continuation
        ):
            starts.update(records[item][0] for item in item_indexes)
        index = max(cursor, index + 1)
    return starts


def _canonical_article_boundary(
    text: str,
    match: re.Match,
) -> Any:
    """Canonicalize two narrowly proven broken numeric header layouts."""

    ref = _clean_source_line(match.group(1))
    spaced = _SPACED_ARTICLE_SUFFIX_RE.fullmatch(ref)
    if spaced is not None:
        code = f"{spaced.group('base')}.{spaced.group('suffix')}"
        code = code.replace('-', '.').replace('–', '.')
        return CanonicalArticleBoundary(
            raw_match=match,
            canonical_ref=f"Статья {code}",
            source_end=match.end(),
        )

    base = _WRAPPED_ARTICLE_BASE_RE.fullmatch(ref)
    if base is None:
        return match
    suffix = _WRAPPED_ARTICLE_SUFFIX_AFTER_RE.match(text, match.end())
    if suffix is None:
        return match
    code = f"{base.group('base')}.{suffix.group('suffix')}"
    code = code.replace('-', '.').replace('–', '.')
    return CanonicalArticleBoundary(
        raw_match=match,
        canonical_ref=f"Статья {code}",
        source_end=suffix.end(),
    )


def _visible_source_length(value: str) -> int:
    return len(re.sub(r'\s+', '', _ZERO_WIDTH_RE.sub('', value or '')))


def _judicial_material_plan(
    text: str,
) -> tuple[
    List[JudicialMaterialBoundary],
    List[JudicialMaterialBoundary],
    List[str],
]:
    """Recognize a collection of court precedents/interpretations fail-closed."""

    records = _source_line_records(text)
    nonempty = [index for index, record in enumerate(records) if record[2]]
    candidates: List[JudicialMaterialBoundary] = []

    for index, (start, end, cleaned) in enumerate(records):
        if not cleaned:
            continue
        if _JUDICIAL_DIRECT_HEADING_RE.fullmatch(cleaned):
            candidates.append(
                JudicialMaterialBoundary(
                    source_start=start,
                    source_end=end,
                    ref=cleaned,
                )
            )
            continue

        is_court_banner = _JUDICIAL_COURT_BANNER_RE.fullmatch(cleaned) is not None
        is_court_decision = (
            _JUDICIAL_DECISION_BANNER_RE.fullmatch(cleaned) is not None
            and 'суд' in cleaned.casefold()
        )
        if not (is_court_banner or is_court_decision):
            continue

        window = [
            item
            for item in nonempty
            if item >= index
        ][:7]
        decision_index = next(
            (
                item
                for item in window
                if _JUDICIAL_DECISION_BANNER_RE.fullmatch(records[item][2])
            ),
            None,
        )
        date_index = next(
            (
                item
                for item in window
                if _JUDICIAL_DATE_RE.fullmatch(records[item][2])
            ),
            None,
        )
        subject_index = next(
            (
                item
                for item in window
                if _JUDICIAL_SUBJECT_RE.match(records[item][2])
            ),
            None,
        )
        if (
            decision_index is None
            or date_index is None
            or subject_index is None
            or (is_court_banner and decision_index <= index)
        ):
            continue

        date_text = records[date_index][2]
        subject = records[subject_index][2]
        candidates.append(
            JudicialMaterialBoundary(
                source_start=start,
                source_end=end,
                ref=f"Толкование {date_text} — {subject}",
            )
        )

    candidates = sorted(
        {candidate.start(): candidate for candidate in candidates}.values(),
        key=lambda candidate: candidate.start(),
    )
    if not candidates:
        return [], [], []

    reasons: List[str] = []
    if len(candidates) < 3:
        reasons.append(
            "judicial collection has "
            f"{len(candidates)} strong material headers; at least 3 are required"
        )

    first_start = candidates[0].start()
    close_limit = max(2_000, len(text) // 20)
    if first_start > close_limit:
        reasons.append(
            "first judicial material starts too far from document beginning "
            f"({first_start}>{close_limit})"
        )

    if any(match.start() < first_start for match in _HEADER_RE.finditer(text)):
        reasons.append(
            "legislative structural header appears before judicial materials"
        )

    for position, candidate in enumerate(candidates):
        material_end = (
            candidates[position + 1].start()
            if position + 1 < len(candidates)
            else len(text)
        )
        visible = _visible_source_length(text[candidate.start():material_end])
        if visible < 256:
            reasons.append(
                "judicial material is too short to distinguish from a contents "
                f"entry (start={candidate.start()}, visible={visible})"
            )

    final_refs = [
        re.sub(r'\s+', ' ', candidate.ref).strip()[:120].casefold()
        for candidate in candidates
    ]
    if len(final_refs) != len(set(final_refs)):
        reasons.append("judicial material refs are not unique after normalization")

    return candidates, ([] if reasons else candidates), reasons


def _is_sandwiched_article_citation(
    text: str,
    match: re.Match,
) -> bool:
    """Detect a titleless cross-reference inside one continuing clause sequence."""

    ref = match.group(1)
    # Some real sources wrap a header as ``Статья\n27.1.``.  This guard keeps
    # those legacy two-line headers outside the contextual citation heuristic.
    if "\n" in ref or "\r" in ref:
        return False
    header = _STANDALONE_ARTICLE_RE.fullmatch(ref)
    if header is None:
        return False
    following = _STANDALONE_DOTTED_CLAUSE_RE.fullmatch(
        _next_nonempty_line(text, match.end())
    )
    if following is None:
        return False
    clause_major = int(following.group(1))
    header_major = int(re.split(r"[.-]", header.group(1), maxsplit=1)[0])
    if header_major == clause_major:
        return False
    for previous in _previous_nonempty_lines(text, match.start(), limit=3):
        previous_clause = _STANDALONE_DOTTED_CLAUSE_RE.fullmatch(previous)
        if previous_clause is not None:
            return (
                int(previous_clause.group(1)) == clause_major
                and int(following.group(2)) == int(previous_clause.group(2)) + 1
            )
    return False


def _is_labeled_structural_self_reference(
    text: str,
    match: re.Match,
) -> bool:
    """Reject only a grammatically explicit reference inside a service note."""

    ref = match.group(1)
    if '\n' in ref or '\r' in ref:
        return False
    if _STRUCTURAL_SELF_REFERENCE_RE.match(ref) is None:
        return False
    previous = _previous_nonempty_lines(text, match.start(), limit=1)
    return bool(
        previous
        and _STRUCTURAL_REFERENCE_LABEL_RE.fullmatch(previous[0])
    )


def _split_article_keyword_boundaries(
    text: str,
) -> List[SplitArticleKeywordBoundary]:
    """Return only proven two-line splits of the ``Статья`` keyword."""

    boundaries: List[SplitArticleKeywordBoundary] = []
    for match in _SPLIT_ARTICLE_KEYWORD_RE.finditer(text):
        if (
            f"{match.group('left')}{match.group('right')}".casefold()
            != "статья"
        ):
            continue
        canonical_ref = f"Статья{match.group('tail')}".strip()
        source_end = match.end()
        trailing_dot = _SPLIT_ARTICLE_TRAILING_DOT_RE.match(
            text,
            source_end,
        )
        if trailing_dot is not None:
            source_end = trailing_dot.end()
            if not canonical_ref.endswith("."):
                canonical_ref += "."
        boundaries.append(
            SplitArticleKeywordBoundary(
                raw_match=match,
                canonical_ref=canonical_ref,
                source_end=source_end,
            )
        )
    return boundaries


def canonicalize_split_article_keyword_layout(value: Any) -> str:
    """Repair only the exact split-keyword layouts accepted by the parser."""

    text = str(value or "")
    boundaries = _split_article_keyword_boundaries(text)
    if not boundaries:
        return text

    parts: List[str] = []
    cursor = 0
    for boundary in boundaries:
        parts.append(text[cursor:boundary.start()])
        parts.append(boundary.canonical_ref)
        cursor = boundary.end()
    parts.append(text[cursor:])
    return "".join(parts)


def _chapter_ref_parts(
    value: Any,
) -> Optional[tuple[str, bool, Optional[str]]]:
    """Return ``(code, titleless, title)`` for one strict chapter ref."""

    ref = re.sub(r"\s+", " ", str(value or "")).strip()
    match = _CHAPTER_REF_RE.fullmatch(ref)
    if match is None:
        return None

    raw_code = match.group("code")
    if raw_code.isdigit() or re.fullmatch(r"\d+(?:\.\d+)*", raw_code):
        code = raw_code
    else:
        code = normalize_roman_structural_token(raw_code)
        if code is None:
            return None

    tail = (match.group("tail") or "").strip()
    if tail in {"", "."}:
        return code, True, None
    title = re.sub(r"^[.\-—–:]+\s*", "", tail).strip()
    if (
        len(title) < 2
        or re.search(r"[0-9A-Za-zА-Яа-яЁё]", title) is None
    ):
        return None
    return code, False, title


def _split_chapter_keyword_boundaries(
    text: str,
) -> List[SplitChapterKeywordBoundary]:
    """Return exact two-line splits whose fragments concatenate to ``Глава``."""

    boundaries: List[SplitChapterKeywordBoundary] = []
    for match in _SPLIT_CHAPTER_KEYWORD_RE.finditer(text):
        if (
            f"{match.group('left')}{match.group('right')}".casefold()
            != "глава"
        ):
            continue
        canonical_ref = f"Глава{match.group('tail')}".strip()
        chapter_parts = _chapter_ref_parts(canonical_ref)
        if chapter_parts is None:
            continue

        source_end = match.end()
        _code, titleless, _title = chapter_parts
        if titleless:
            trailing_dot = _SPLIT_CHAPTER_TRAILING_DOT_RE.match(
                text,
                source_end,
            )
            if trailing_dot is not None:
                source_end = trailing_dot.end()
                if not canonical_ref.endswith("."):
                    canonical_ref += "."
        boundaries.append(
            SplitChapterKeywordBoundary(
                raw_match=match,
                canonical_ref=canonical_ref,
                source_end=source_end,
            )
        )
    return boundaries


def _wrapped_chapter_code_boundary(
    text: str,
    raw_match: re.Match,
) -> Optional[WrappedChapterCodeBoundary]:
    """Canonicalize one exact ``Глава`` / Roman code / ``.`` layout."""

    wrapped = _WRAPPED_CHAPTER_CODE_RE.fullmatch(raw_match.group(1))
    if wrapped is None:
        return None
    code = normalize_roman_structural_token(wrapped.group("code"))
    if code is None or canonical_roman_value(code) is None:
        return None

    trailing_dot = _SPLIT_CHAPTER_TRAILING_DOT_RE.match(
        text,
        raw_match.end(),
    )
    if trailing_dot is None:
        return None
    return WrappedChapterCodeBoundary(
        raw_match=raw_match,
        canonical_ref=f"Глава {code}.",
        source_end=trailing_dot.end(),
    )


def _wrapped_chapter_code_boundaries(
    text: str,
) -> List[WrappedChapterCodeBoundary]:
    """Return only fully proven three-line wrapped chapter headers."""

    boundaries: List[WrappedChapterCodeBoundary] = []
    for match in _HEADER_RE.finditer(text):
        boundary = _wrapped_chapter_code_boundary(text, match)
        if boundary is not None:
            boundaries.append(boundary)
    return boundaries


def canonicalize_split_structural_keyword_layout(value: Any) -> str:
    """Canonicalize only exact, proven wrapped structural layouts."""

    text = canonicalize_split_article_keyword_layout(value)
    boundaries: List[Any] = [
        *_split_chapter_keyword_boundaries(text),
        *_wrapped_chapter_code_boundaries(text),
    ]
    if not boundaries:
        return text

    parts: List[str] = []
    cursor = 0
    for boundary in sorted(boundaries, key=lambda item: item.start()):
        if boundary.start() < cursor:
            continue
        parts.append(text[cursor:boundary.start()])
        parts.append(boundary.canonical_ref)
        cursor = boundary.end()
    parts.append(text[cursor:])
    return "".join(parts)


@dataclass(frozen=True)
class _RomanChapterContinuation:
    fragments: tuple[str, ...]
    source_end: int
    title: Optional[str]
    terminal_fragment_dot: bool


def _roman_chapter_continuation(
    records: List[tuple[int, int, str]],
    line_index: int,
) -> tuple[Optional[_RomanChapterContinuation], bool]:
    """Inspect the immediately following physical lines without skipping blanks."""

    cursor = line_index + 1
    if cursor >= len(records):
        return None, False
    first = _ISOLATED_ROMAN_FRAGMENT_RE.fullmatch(records[cursor][2])
    if first is None:
        return None, False

    fragments: List[str] = []
    for _count in range(3):
        if cursor >= len(records):
            break
        fragment = _ISOLATED_ROMAN_FRAGMENT_RE.fullmatch(records[cursor][2])
        if fragment is None:
            break
        normalized = normalize_roman_structural_token(fragment.group("code"))
        if normalized is None:
            return None, True
        fragments.append(normalized)
        source_end = records[cursor][1]
        cursor += 1

        if fragment.group("dot"):
            return (
                _RomanChapterContinuation(
                    fragments=tuple(fragments),
                    source_end=source_end,
                    title=None,
                    terminal_fragment_dot=True,
                ),
                True,
            )

        if cursor < len(records):
            title_match = _ROMAN_CHAPTER_TITLE_LINE_RE.fullmatch(
                records[cursor][2]
            )
            if title_match is not None:
                title = title_match.group("title").strip()
                if (
                    len(title) < 2
                    or re.search(r"[0-9A-Za-zА-Яа-яЁё]", title) is None
                ):
                    return None, True
                return (
                    _RomanChapterContinuation(
                        fragments=tuple(fragments),
                        source_end=records[cursor][1],
                        title=title,
                        terminal_fragment_dot=False,
                    ),
                    True,
                )

    return None, True


def _chapter_sequence_value(boundary: Any) -> Optional[int]:
    parts = _chapter_ref_parts(boundary.group(1))
    if parts is None:
        return None
    code = parts[0]
    if code.isdigit():
        return int(code)
    return canonical_roman_value(code)


def _repair_split_roman_chapter_layouts(
    text: str,
    matches: List[Any],
) -> List[Any]:
    """Repair wrapped Roman suffixes only with independently strong proof."""

    records = _source_line_records(text)
    line_by_end: Dict[int, int] = {}
    for index, (_start, visible_end, _line) in enumerate(records):
        line_by_end[visible_end] = index
        # ``[^\n]*`` in the structural-header regexes consumes the ``\r`` of
        # a CRLF separator, while ``_source_line_records`` deliberately keeps
        # ``visible_end`` before both newline bytes. Accept that equivalent
        # physical-line endpoint without normalizing or rewriting source text.
        if text[visible_end : visible_end + 2] == "\r\n":
            line_by_end[visible_end + 1] = index
    chapters = [
        boundary
        for boundary in sorted(matches, key=lambda item: item.start())
        if _chapter_ref_parts(boundary.group(1)) is not None
    ]
    chapter_position = {
        boundary.start(): index for index, boundary in enumerate(chapters)
    }

    repaired: List[Any] = []
    for boundary in matches:
        parts = _chapter_ref_parts(boundary.group(1))
        if parts is None or not parts[1]:
            repaired.append(boundary)
            continue
        base_code = parts[0]
        base_value = canonical_roman_value(base_code)
        line_index = line_by_end.get(boundary.end())
        if base_value is None or line_index is None:
            repaired.append(boundary)
            continue

        continuation, suffix_like = _roman_chapter_continuation(
            records,
            line_index,
        )
        if continuation is None:
            # A lexically proven split keyword must not expose an incomplete
            # chapter code when the following line looks like a wrapped Roman
            # suffix.  Ordinary already-recognized chapter headers retain their
            # established fail-closed behavior.
            if not (
                suffix_like
                and isinstance(boundary, SplitChapterKeywordBoundary)
            ):
                repaired.append(boundary)
            continue

        combined_code = (
            normalize_roman_structural_token(base_code) or ""
        ) + "".join(continuation.fragments)
        combined_value = canonical_roman_value(combined_code)
        position = chapter_position.get(boundary.start())
        previous = (
            chapters[position - 1]
            if position is not None and position > 0
            else None
        )
        following = (
            chapters[position + 1]
            if position is not None and position + 1 < len(chapters)
            else None
        )
        previous_value = (
            _chapter_sequence_value(previous) if previous is not None else None
        )
        following_value = (
            _chapter_sequence_value(following) if following is not None else None
        )

        two_sided_proof = bool(
            continuation.title
            and combined_value is not None
            and previous_value == combined_value - 1
            and following_value == combined_value + 1
        )
        terminal_split_keyword_proof = bool(
            continuation.terminal_fragment_dot
            and len(continuation.fragments) == 1
            and isinstance(boundary, SplitChapterKeywordBoundary)
            and combined_value is not None
            and previous_value == combined_value - 1
            and following is None
        )
        if not (two_sided_proof or terminal_split_keyword_proof):
            if not isinstance(boundary, SplitChapterKeywordBoundary):
                repaired.append(boundary)
            continue

        canonical_ref = f"Глава {combined_code}"
        if continuation.title:
            canonical_ref += f". {continuation.title}"
        elif continuation.terminal_fragment_dot:
            canonical_ref += "."
        repaired.append(
            SplitRomanChapterBoundary(
                original_boundary=boundary,
                canonical_ref=canonical_ref,
                source_end=continuation.source_end,
            )
        )
    return sorted(repaired, key=lambda item: item.start())


def _article_boundary_code(boundary: Any) -> Optional[str]:
    """Return a normalized numeric code only for an explicit article header."""

    ref = re.sub(r'\s+', ' ', str(boundary.group(1) or '')).strip()
    match = _STRUCTURED_ARTICLE_CODE_RE.match(ref)
    if match is None:
        return None
    return match.group('code').rstrip('.').replace('-', '.').replace('–', '.')


def _article_boundary_ref(boundary: Any) -> str:
    return re.sub(r'\s+', ' ', str(boundary.group(1) or '')).strip()


def _is_titleless_article_boundary(boundary: Any) -> bool:
    ref = _article_boundary_ref(boundary)
    return (
        '\n' not in str(boundary.group(1) or '')
        and '\r' not in str(boundary.group(1) or '')
        and _STANDALONE_ARTICLE_RE.fullmatch(ref) is not None
    )


def _collapse_adjacent_duplicate_article_headers(
    text: str,
    matches: List[Any],
) -> List[Any]:
    """Collapse only immediately adjacent explicit headers with one article code.

    Forum formatting sometimes emits a code-only header and repeats the same
    header on the next physical line with its title or even the beginning of
    the disposition.  Leaving both matches creates an empty duplicate article;
    dropping either raw line would instead move formatting into the previous
    provision or lose inline source text.  One canonical boundary therefore
    starts at the first match, spans every adjacent duplicate for overlap
    checks, exposes the richest ref, while ``split_into_blocks`` continues to
    retain the complete original source bytes in the resulting article body.
    """

    collapsed: List[Any] = []
    index = 0
    while index < len(matches):
        first = matches[index]
        if index + 1 >= len(matches):
            collapsed.append(first)
            break

        second = matches[index + 1]
        same_code = (
            _article_boundary_code(first) is not None
            and _article_boundary_code(first) == _article_boundary_code(second)
        )
        adjacent = _visible_source_length(
            text[first.end():second.start()]
        ) == 0
        first_titleless = _is_titleless_article_boundary(first)
        second_titleless = _is_titleless_article_boundary(second)

        # At least one line must be an exact code-only duplicate.  Two rich
        # same-code headings can represent genuinely different repeated
        # provisions and remain separate.
        if same_code and adjacent and (first_titleless or second_titleless):
            if first_titleless:
                # The second existing row already owns the real body.  Starting
                # there keeps its full text/hash byte-identical and prevents the
                # empty first line from leaking into the previous provision.
                canonical_ref = _article_boundary_ref(second)
                body_start = second.start()
                prefix_end = None
            else:
                # The first line carries the title/disposition, while the
                # second is formatting-only.  Keep the complete first source
                # line and resume the body after the duplicate.
                canonical_ref = _article_boundary_ref(first)
                body_start = first.start()
                prefix_end = first.end()
            collapsed.append(
                AdjacentDuplicateArticleBoundary(
                    first_match=first,
                    second_match=second,
                    canonical_ref=canonical_ref,
                    body_start=body_start,
                    prefix_end=prefix_end,
                )
            )
            index += 2
            continue

        collapsed.append(first)
        index += 1
    return collapsed


def _structural_header_matches(text: str) -> List[Any]:
    """Return real structural headers, excluding unambiguous citations.

    A source line may start with a list such as
    ``ст. 6.2, ст. 6.3, 6.5, ст. 10.6 УК``.  The broad legacy regex sees its
    first token as a header and creates a junk article.  Standalone references
    to another code and a titleless reference sandwiched between two clauses of
    the same article are equally non-structural.  Genuine abbreviated headers
    such as ``Ст. 7 - Кража`` remain valid.
    """

    reference_list_starts = _article_reference_list_starts(text)
    matches: List[Any] = []
    for match in _HEADER_RE.finditer(text):
        if match.start() in reference_list_starts:
            continue
        ref = match.group(1)
        # After an explicit ``Примечание``/``Исключение`` label, a grammatical
        # continuation such as
        # ``статья 25 настоящего закона`` or
        # ``п.4 настоящей статьи реализуется ...`` is a cross-reference
        # sentence, not a title.  A real heading with a title delimiter and
        # wording such as ``Статья 2. Действие настоящего закона`` does not
        # match this deliberately narrow shape.
        if _is_labeled_structural_self_reference(text, match):
            continue
        # Forum changelog posts sometimes contain an article-shaped edit note
        # immediately after ``Редакция от DD.MM.YYYY``.  Suppress only the
        # structural boundary; the raw source line stays in the owning block.
        if _is_revision_changelog_article_boundary(text, match):
            continue
        if _CROSS_CODE_CITATION_RE.fullmatch(ref):
            continue
        prefix = _ABBREVIATED_ARTICLE_PREFIX_RE.match(ref)
        if (
            prefix is not None
            and _CITATION_LIST_CONTINUATION_RE.match(ref[prefix.end():])
        ):
            continue
        if _is_sandwiched_article_citation(text, match):
            continue
        boundary = _canonical_article_boundary(text, match)
        wrapped_chapter = _wrapped_chapter_code_boundary(text, match)
        matches.append(wrapped_chapter or boundary)

    # Forum layout occasionally wraps the keyword itself (``С`` / ``татья``,
    # ``Ст`` / ``атья`` and so on).  Scan only two complete word fragments
    # followed by a numeric article tail, then require their exact
    # concatenation to be ``статья``.  This is intentionally much narrower
    # than allowing arbitrary whitespace inside every structural keyword.
    matches.extend(_split_article_keyword_boundaries(text))
    matches.extend(_split_chapter_keyword_boundaries(text))
    matches = _repair_split_roman_chapter_layouts(
        text,
        sorted(matches, key=lambda match: match.start()),
    )
    return _collapse_adjacent_duplicate_article_headers(
        text,
        matches,
    )


def _bare_header_matches(text: str) -> List[re.Match]:
    """Return bare N.M headers, excluding wrapped citation continuations."""

    matches: List[re.Match] = []
    for match in _BARE_HEADER_RE.finditer(text):
        if _is_bare_article_reference_continuation(text, match):
            continue
        if _EXACT_BARE_HEADER_RE.fullmatch(match.group(1)) is None:
            matches.append(match)
            continue
        following = _next_nonempty_line(text, match.end())
        if _BARE_DIRECT_CITATION_CONTINUATION_RE.match(following):
            continue
        if (
            _BARE_GENITIVE_CONTINUATION_RE.match(following)
            and any(
                _CITATION_CONNECTOR_LINE_RE.fullmatch(previous)
                for previous in _previous_nonempty_lines(
                    text,
                    match.start(),
                    limit=1,
                )
            )
        ):
            continue
        matches.append(match)
    return matches


def _is_bare_article_reference_continuation(
    text: str,
    match: re.Match,
) -> bool:
    """Reject a bare number that grammatically completes a source citation.

    Forum formatting can wrap ``по статье 13.2`` or a list introduced by
    ``предусмотрено статьями`` onto separate physical lines.  The number still
    belongs to the surrounding sentence; treating it as a new article both
    truncates the owner and creates a false catalog row.  This proof is based
    only on the immediately preceding source grammar, never document or server
    identity.
    """

    previous = _previous_nonempty_lines(text, match.start(), limit=3)
    if not previous:
        return False
    if _BARE_REFERENCE_INTRO_RE.search(previous[0]):
        return True
    if (
        _BARE_REFERENCE_CONNECTOR_LINE_RE.fullmatch(previous[0])
        and len(previous) >= 2
        and any(
            _BARE_REFERENCE_CONNECTOR_OWNER_RE.search(line)
            for line in previous[1:]
        )
    ):
        return True
    return bool(
        _STRUCTURAL_REFERENCE_LABEL_RE.fullmatch(previous[0])
        and _BARE_CROSS_CODE_REFERENCE_RE.fullmatch(match.group(1))
    )


def _mixed_special_header_groups(
    text: str,
    *,
    structural: List[Any],
    bare: List[re.Match],
) -> List[MixedSpecialChapterGroup]:
    """Find high-confidence mixed-format article groups, chapter by chapter.

    This scanner is deliberately separate from the established pure-bare
    fallback.  It only considers exact ``N.M``/``N.M.K`` number lines (including
    ``N.M`` + wrapped ``.K``), requires an immediately following ``(F)``,
    ``(R)`` or ``(F/R)`` marker, and binds the result to the nearest preceding
    structural ``Глава N`` with the same major number.  Two-level candidates
    must additionally pass the existing bare citation guards.
    """

    marker_ends = [
        match.end() for match in _MIXED_BARE_MARKER_RE.finditer(text)
    ]
    if not marker_ends:
        return []

    legacy_bare_starts = {match.start() for match in bare}
    wrapped_by_start = {
        match.start(): match
        for match in _MIXED_SPECIAL_WRAPPED_CODE_RE.finditer(text)
    }
    raw_candidates: List[tuple[re.Match, str]] = [
        (
            match,
            f"{match.group('prefix')}.{match.group('suffix')}",
        )
        for match in wrapped_by_start.values()
    ]
    raw_candidates.extend(
        (match, match.group("code"))
        for match in _MIXED_SPECIAL_CODE_RE.finditer(text)
        if match.start() not in wrapped_by_start
    )
    raw_candidates.sort(key=lambda item: item[0].start())

    grouped: dict[int, list[MixedSpecialBoundary]] = {}
    chapter_majors: dict[int, int] = {}
    for raw_match, code in raw_candidates:
        # The existing citation-continuation guards remain authoritative for
        # every ordinary N.M candidate.  N.M.K is only enabled in this scanner.
        if code.count(".") == 1 and raw_match.start() not in legacy_bare_starts:
            continue

        preceding = next(
            (
                header
                for header in reversed(structural)
                if header.start() < raw_match.start()
            ),
            None,
        )
        if preceding is None:
            continue
        chapter = _MIXED_CHAPTER_HEADER_RE.match(preceding.group(1))
        if chapter is None:
            continue
        chapter_major = int(chapter.group(1))
        code_major = int(code.split(".", maxsplit=1)[0])
        if chapter_major != code_major:
            continue
        if not any(end <= preceding.start() for end in marker_ends):
            continue

        jurisdiction_match = _MIXED_JURISDICTION_AFTER_RE.match(
            text,
            raw_match.end(),
        )
        if jurisdiction_match is None:
            continue
        jurisdiction = re.sub(
            r'[\s\u200b-\u200d\u2060\ufeff]+',
            '',
            jurisdiction_match.group("scope"),
        ).upper()
        boundary = MixedSpecialBoundary(
            raw_match=raw_match,
            code=code,
            jurisdiction=jurisdiction,
            marker_end=jurisdiction_match.end(),
            chapter_start=preceding.start(),
            chapter_major=chapter_major,
        )
        grouped.setdefault(preceding.start(), []).append(boundary)
        chapter_majors[preceding.start()] = chapter_major

    return [
        MixedSpecialChapterGroup(
            chapter_start=chapter_start,
            chapter_major=chapter_majors[chapter_start],
            candidates=tuple(candidates),
        )
        for chapter_start, candidates in sorted(grouped.items())
    ]


def _mixed_classified_chapter_tail_groups(
    text: str,
    *,
    structural: List[Any],
) -> List[MixedSpecialChapterGroup]:
    """Recognize one audited mid-document header-format switch.

    Some criminal codes use explicit ``Статья N.M`` headers in their general
    part and then switch to exact standalone ``N.M`` lines in a long tail of
    numeric chapters.  Those sources do not necessarily contain the
    ``Особенная часть``/standalone-jurisdiction layout handled above.  A broad
    mixed bare fallback is unsafe: monetary lines such as ``75.000$`` and
    ordinary numbered clauses are common in the same documents.

    This second path therefore requires all independently useful signals from
    the audited format:

    * at least two earlier numeric chapters contain explicit dotted article
      headers;
    * every candidate is an exact standalone two- or three-level number;
    * its major number equals the surrounding numeric chapter;
    * the next non-empty line begins with an inline legal classification such
      as ``(A, CR)``, ``(F. CR)`` or ``(R)``;
    * the bounded article block contains the corpus-specific sanction fields
      ``Выход под залог`` and ``Наказание`` plus a wanted-level marker;
    * each chapter is a complete, strictly ordered ``N.1..N.K`` sequence with
      at least three base articles; optional ``N.M.1..N.M.K`` children must
      immediately follow their base and also be complete;
    * every numeric chapter after the last explicitly numbered article chapter
      uses that format, with at least three consecutive chapters in the tail.

    The last two requirements deliberately reject partial recognition.  A
    future source with gaps remains a preflight warning for manual audit
    instead of being silently cut into an incomplete catalog.
    """

    chapters: List[tuple[Any, int]] = []
    for header in structural:
        chapter = _MIXED_CHAPTER_HEADER_RE.match(header.group(1))
        if chapter is not None:
            chapters.append((header, int(chapter.group(1))))
    if len(chapters) < 5:
        # Two established structured chapters plus a three-chapter bare tail
        # is the minimum format-switch proof.
        return []

    structured_chapter_indexes: List[int] = []
    candidates_by_chapter: dict[
        int,
        list[tuple[re.Match, str]],
    ] = {}
    for index, (chapter_header, chapter_major) in enumerate(chapters):
        chapter_end = (
            chapters[index + 1][0].start()
            if index + 1 < len(chapters)
            else len(text)
        )
        has_dotted_structured_article = False
        for header in structural:
            if not (chapter_header.start() < header.start() < chapter_end):
                continue
            article = _STRUCTURED_ARTICLE_CODE_RE.match(header.group(1))
            if article is None:
                continue
            code = (
                article.group("code")
                .replace("-", ".")
                .replace("–", ".")
            )
            parts = code.split(".")
            if int(parts[0]) == chapter_major and len(parts) >= 2:
                has_dotted_structured_article = True
                break
        if has_dotted_structured_article:
            structured_chapter_indexes.append(index)

        raw_matches = [
            match
            for match in _MIXED_SPECIAL_CODE_RE.finditer(
                text,
                chapter_header.start(),
                chapter_end,
            )
        ]
        chapter_candidates: list[tuple[re.Match, str]] = []
        for candidate_index, match in enumerate(raw_matches):
            code = match.group("code")
            if int(code.split(".", maxsplit=1)[0]) != chapter_major:
                continue
            if _MIXED_INLINE_CLASSIFICATION_RE.match(
                _next_nonempty_line(text, match.end())
            ) is None:
                continue
            candidate_end = (
                raw_matches[candidate_index + 1].start()
                if candidate_index + 1 < len(raw_matches)
                else chapter_end
            )
            provision = text[match.end():candidate_end].casefold()
            if not (
                "выход под залог" in provision
                and "наказание" in provision
                and "*" in provision
            ):
                continue
            chapter_candidates.append((match, code))
        if chapter_candidates:
            candidates_by_chapter[index] = chapter_candidates

    if len(structured_chapter_indexes) < 2:
        return []
    last_structured_chapter = max(structured_chapter_indexes)

    qualified_indexes: List[int] = []
    for index in sorted(candidates_by_chapter):
        if index <= last_structured_chapter:
            continue
        items = candidates_by_chapter[index]
        chapter_major = chapters[index][1]
        parsed_codes = [
            tuple(int(part) for part in code.split("."))
            for _match, code in items
        ]
        base_minors = [
            code[1]
            for code in parsed_codes
            if len(code) == 2
        ]
        complete = len(base_minors) >= 3
        if complete:
            complete = base_minors == list(range(1, base_minors[-1] + 1))
        expected_codes: List[tuple[int, ...]] = []
        if complete:
            for minor in base_minors:
                base = (chapter_major, minor)
                expected_codes.append(base)
                child_suffixes = [
                    code[2]
                    for code in parsed_codes
                    if len(code) == 3 and code[:2] == base
                ]
                if child_suffixes:
                    if child_suffixes != list(
                        range(1, child_suffixes[-1] + 1)
                    ):
                        complete = False
                        break
                    expected_codes.extend(
                        (chapter_major, minor, suffix)
                        for suffix in child_suffixes
                    )
        if complete and parsed_codes == expected_codes:
            qualified_indexes.append(index)

    tail_indexes = list(range(last_structured_chapter + 1, len(chapters)))
    if (
        len(tail_indexes) < 3
        or qualified_indexes != tail_indexes
        or any(
            chapters[current][1] != chapters[previous][1] + 1
            for previous, current in zip(tail_indexes, tail_indexes[1:])
        )
    ):
        # Never accept disjoint valid islands around one malformed/missing
        # chapter.  The dialect is proven only when it covers the complete
        # remaining numeric-chapter tail.
        return []
    accepted_indexes = set(tail_indexes)

    groups: List[MixedSpecialChapterGroup] = []
    for index in sorted(accepted_indexes):
        chapter_header, chapter_major = chapters[index]
        boundaries = tuple(
            MixedSpecialBoundary(
                raw_match=match,
                code=code,
                jurisdiction="",
                marker_end=match.end(),
                chapter_start=chapter_header.start(),
                chapter_major=chapter_major,
            )
            for match, code in candidates_by_chapter[index]
        )
        groups.append(
            MixedSpecialChapterGroup(
                chapter_start=chapter_header.start(),
                chapter_major=chapter_major,
                candidates=boundaries,
            )
        )
    return groups


def _chapter_boundary_value(boundary: Any) -> Optional[int]:
    """Return a strict numeric value for one already-proven chapter boundary."""

    ref = re.sub(r'\s+', ' ', str(boundary.group(1) or '')).strip()
    match = _CHAPTER_REF_RE.match(ref)
    if match is None:
        return None
    code = match.group('code')
    if code.isdigit():
        return int(code)
    return canonical_roman_value(code)


def _recovery_payload_is_provision(
    value: Any,
    *,
    has_direct_child: bool,
) -> bool:
    """Require content proof in addition to a numerically plausible gap."""

    payload = re.sub(r'\s+', ' ', str(value or '')).strip()
    if (
        len(payload) < 8
        or _RECOVERY_NON_PROVISION_PREFIX_RE.match(payload)
    ):
        return False
    # A real direct child is structural proof.  Without one, accept only a
    # full disposition-sized line; short captions and list labels stay
    # fail-closed even if their number happens to fill an article gap.
    return has_direct_child or len(payload) >= 120


def _recovered_parent_article_boundaries(
    text: str,
    *,
    structural: List[Any],
) -> List[RecoveredParentArticleBoundary]:
    """Recover only complete, source-proven runs of omitted parent articles.

    A small family of forum documents mixes ordinary ``Статья N`` headings
    with two other layouts:

    * a chapter starts directly with ``N.1 ...`` because the ``Статья N`` line
      was omitted;
    * a parent is printed as ``N. Название`` while its children remain
      ``N.1 ...``.

    Treating every such number as an article would split ordinary lists and
    citations.  This scanner therefore accepts only a globally ordered source
    dialect with at least ten explicit single-level article anchors.  A
    recovered item must either fill an entire numeric gap between anchors and
    be the first numeric provision in its chapter, or belong to one complete
    multi-chapter format-switch tail.  The tail requires at least three
    sequential chapters, two consecutive shallow parents per chapter and a
    matching ``N.1`` child in every chapter.  Any missing number, stray dotted
    group, explicit article in the tail or malformed chapter rejects the whole
    corresponding recovery path.
    """

    article_boundaries = [
        boundary
        for boundary in structural
        if _STATYA_KW_RE.match(_article_boundary_ref(boundary))
    ]
    explicit: List[tuple[int, int, Any]] = []
    for boundary in article_boundaries:
        code = _article_boundary_code(boundary)
        # This dialect is deliberately disjoint from the mixed-dotted article
        # recovery.  One dotted/non-numeric explicit article makes the source
        # ambiguous for this scanner.
        if code is None or not code.isdigit():
            return []
        explicit.append((boundary.start(), int(code), boundary))
    explicit.sort(key=lambda item: item[0])
    if len(explicit) < 10 or any(
        right[1] <= left[1]
        for left, right in zip(explicit, explicit[1:])
    ):
        return []

    chapters = [
        boundary
        for boundary in structural
        if _CHAPTER_REF_RE.match(_article_boundary_ref(boundary))
    ]
    chapters.sort(key=lambda item: item.start())
    if not chapters:
        return []
    chapter_starts = [boundary.start() for boundary in chapters]
    chapter_ends = chapter_starts[1:] + [len(text)]

    def chapter_index(position: int) -> int:
        index = bisect_right(chapter_starts, position) - 1
        if index < 0 or position >= chapter_ends[index]:
            return -1
        return index

    def overlaps_structural(match: re.Match) -> bool:
        return any(
            boundary.start() <= match.start() < boundary.end()
            for boundary in structural
        )

    parent_matches = [
        match
        for match in _SHALLOW_BARE_PARENT_RE.finditer(text)
        if not overlaps_structural(match) and chapter_index(match.start()) >= 0
    ]
    dotted_matches = [
        match
        for match in _DOTTED_PROVISION_LINE_RE.finditer(text)
        if not overlaps_structural(match) and chapter_index(match.start()) >= 0
    ]

    # Event tuple:
    # (position, kind, major, numeric parts, source match/boundary).
    events_by_chapter: List[
        List[tuple[int, str, int, tuple[int, ...], Any]]
    ] = [[] for _chapter in chapters]
    for position, major, boundary in explicit:
        index = chapter_index(position)
        if index >= 0:
            events_by_chapter[index].append(
                (position, 'explicit', major, (major,), boundary)
            )
    for match in parent_matches:
        major = int(match.group('code'))
        index = chapter_index(match.start())
        events_by_chapter[index].append(
            (match.start(), 'parent', major, (major,), match)
        )
    for match in dotted_matches:
        parts = tuple(int(part) for part in match.group('code').split('.'))
        index = chapter_index(match.start())
        events_by_chapter[index].append(
            (match.start(), 'dotted', parts[0], parts, match)
        )
    for events in events_by_chapter:
        events.sort(key=lambda item: item[0])

    first_candidates: List[
        tuple[int, int, int, RecoveredParentArticleBoundary]
    ] = []
    for index, events in enumerate(events_by_chapter):
        if not events:
            continue
        position, kind, major, parts, source = events[0]
        dotted_parts = {
            event[3]
            for event in events
            if event[1] == 'dotted' and event[2] == major
        }
        if kind == 'parent':
            title = re.sub(r'\s+', ' ', source.group('title')).strip()
            if not _recovery_payload_is_provision(
                title,
                has_direct_child=(major, 1) in dotted_parts,
            ):
                continue
            first_candidates.append(
                (
                    position,
                    major,
                    index,
                    RecoveredParentArticleBoundary(
                        raw_match=source,
                        canonical_ref=f"Статья {major}. {title}",
                    ),
                )
            )
        elif kind == 'dotted' and parts == (major, 1):
            if not _recovery_payload_is_provision(
                source.group('body'),
                has_direct_child=(major, 2) in dotted_parts,
            ):
                continue
            first_candidates.append(
                (
                    position,
                    major,
                    index,
                    RecoveredParentArticleBoundary(
                        raw_match=source,
                        canonical_ref=f"Статья {major}",
                    ),
                )
            )

    bounded_recovered: List[
        tuple[int, RecoveredParentArticleBoundary]
    ] = []
    # The synthetic zero anchor supports an omitted first Article 1, but only
    # when the real first explicit article and every intervening parent prove
    # one complete 1..N-1 run.
    anchors = [(-1, 0, None), *explicit]
    for left, right in zip(anchors, explicit):
        if right[1] <= left[1] + 1:
            continue
        candidates = [
            candidate
            for candidate in first_candidates
            if left[0] < candidate[0] < right[0]
        ]
        expected = list(range(left[1] + 1, right[1]))
        if [candidate[1] for candidate in candidates] == expected:
            bounded_recovered.extend(
                (candidate[2], candidate[3])
                for candidate in candidates
            )

    recovered: List[RecoveredParentArticleBoundary] = []
    if (
        len(bounded_recovered) >= 2
        and len({index for index, _boundary in bounded_recovered}) >= 2
    ):
        recovered.extend(
            boundary for _index, boundary in bounded_recovered
        )

    # A tail is a separate all-or-nothing proof.  It starts only in the chapter
    # after the final explicit article and must cover every remaining chapter.
    last_explicit_chapter = chapter_index(explicit[-1][0])
    tail_indexes = list(range(last_explicit_chapter + 1, len(chapters)))
    tail_recovered: List[RecoveredParentArticleBoundary] = []
    tail_valid = len(tail_indexes) >= 3
    if tail_valid:
        sequence_indexes = [last_explicit_chapter, *tail_indexes]
        chapter_values = [
            _chapter_boundary_value(chapters[index])
            for index in sequence_indexes
        ]
        tail_valid = all(value is not None for value in chapter_values) and all(
            right == left + 1
            for left, right in zip(chapter_values, chapter_values[1:])
        )

    previous_tail_major = explicit[-1][1]
    if tail_valid:
        for index in tail_indexes:
            events = events_by_chapter[index]
            parents = [event for event in events if event[1] == 'parent']
            dotted = [event for event in events if event[1] == 'dotted']
            parent_majors = [event[2] for event in parents]
            parent_set = set(parent_majors)
            direct_child_majors = {
                event[2]
                for event in dotted
                if event[3] == (event[2], 1)
            }
            if not (
                events
                and events[0][1] == 'parent'
                and not any(event[1] == 'explicit' for event in events)
                and len(parents) >= 2
                and parent_majors
                == list(range(parent_majors[0], parent_majors[-1] + 1))
                and parent_majors[0] >= previous_tail_major
                and direct_child_majors
                and direct_child_majors.issubset(parent_set)
                and all(event[2] in parent_set for event in dotted)
                and all(
                    _recovery_payload_is_provision(
                        event[4].group('title'),
                        has_direct_child=event[2] in direct_child_majors,
                    )
                    for event in parents
                )
            ):
                tail_valid = False
                break
            for event in parents:
                source = event[4]
                title = re.sub(r'\s+', ' ', source.group('title')).strip()
                tail_recovered.append(
                    RecoveredParentArticleBoundary(
                        raw_match=source,
                        canonical_ref=f"Статья {event[2]}. {title}",
                    )
                )
            previous_tail_major = parent_majors[-1]

    if tail_valid and len(tail_recovered) >= 6:
        recovered.extend(tail_recovered)

    unique = {
        boundary.start(): boundary
        for boundary in recovered
    }
    return [unique[position] for position in sorted(unique)]


def _unresolved_recovery_bare_candidates(
    *,
    structural: List[Any],
    bare: List[re.Match],
) -> List[re.Match]:
    """Return bare N.M lines not owned by the nearest article in its scope."""

    scope_boundaries = sorted(
        [
            boundary
            for boundary in structural
            if (
                _article_boundary_code(boundary) is not None
                or _CHAPTER_REF_RE.match(_article_boundary_ref(boundary))
                is not None
                or _article_boundary_ref(boundary).casefold().startswith(
                    'раздел'
                )
            )
        ],
        key=lambda item: item.start(),
    )
    scope_starts = [boundary.start() for boundary in scope_boundaries]
    unresolved: List[re.Match] = []
    for candidate in bare:
        major_match = _BARE_MAJOR_PREFIX_RE.match(candidate.group(1))
        owner_index = bisect_right(scope_starts, candidate.start() - 1) - 1
        owner = (
            scope_boundaries[owner_index]
            if owner_index >= 0
            else None
        )
        owner_code = (
            _article_boundary_code(owner)
            if owner is not None
            else None
        )
        owner_major = (
            owner_code.split('.', maxsplit=1)[0]
            if owner_code is not None
            else None
        )
        if (
            major_match is None
            or owner_major != major_match.group('major')
        ):
            unresolved.append(candidate)
    return unresolved


def _header_match_plan(text: str) -> HeaderMatchPlan:
    """Apply the exact shared pure-bare and mixed-bare fallback policy."""

    (
        judicial_candidates,
        accepted_judicial,
        judicial_rejection_reasons,
    ) = _judicial_material_plan(text)
    if accepted_judicial:
        return HeaderMatchPlan(
            matches=tuple(accepted_judicial),
            structural=tuple(),
            legacy_bare_candidates=tuple(),
            mixed_special_candidates=tuple(),
            mixed_special_groups=tuple(),
            accepted_mixed_special_candidates=tuple(),
            recovered_parent_articles=tuple(),
            recovery_unresolved_bare_candidates=tuple(),
            structured_article_count=0,
            fallback_mode="judicial_materials",
            article_dialect_status="not_applicable",
            article_dialect_mode=None,
            article_dialect_reasons=tuple(),
            judicial_material_candidates=tuple(judicial_candidates),
            accepted_judicial_materials=tuple(accepted_judicial),
            judicial_rejection_reasons=tuple(),
        )

    # Preserve the established pure-bare behavior around older citation
    # filters.  If a labeled self-reference or revision-log note was the
    # document's sole apparent ``Статья``, stay fail-closed instead of suddenly
    # opening the broad legacy bare parser.
    rejected_explicit_article_nonheader = any(
        _STATYA_KW_RE.match(match.group(1))
        and (
            _is_labeled_structural_self_reference(text, match)
            or _is_revision_changelog_article_boundary(text, match)
        )
        for match in _HEADER_RE.finditer(text)
    )
    base_structural = _structural_header_matches(text)
    recovered_parent_articles = _recovered_parent_article_boundaries(
        text,
        structural=base_structural,
    )
    structural = list(base_structural)
    if recovered_parent_articles:
        structural = sorted(
            [*structural, *recovered_parent_articles],
            key=lambda match: match.start(),
        )
    bare = [
        match
        for match in _bare_header_matches(text)
        if not any(
            header.start() <= match.start() < header.end()
            for header in structural
        )
    ]
    recovery_unresolved_bare = (
        _unresolved_recovery_bare_candidates(
            structural=structural,
            bare=bare,
        )
        if recovered_parent_articles
        else []
    )
    if recovery_unresolved_bare:
        # Partial recovery is unsafe for the autonomous catalog-sync path,
        # which does not run the Phase A CLI first. Reject the whole synthetic
        # set and retain the ambiguity signal for fail-closed preflight.
        recovered_parent_articles = []
        structural = list(base_structural)
        bare = [
            match
            for match in _bare_header_matches(text)
            if not any(
                header.start() <= match.start() < header.end()
                for header in structural
            )
        ]
    accepted: List[Any] = []
    mixed_groups: List[MixedSpecialChapterGroup] = []
    mixed: List[MixedSpecialBoundary] = []
    accepted_mixed: List[MixedSpecialBoundary] = []
    fallback_mode: Optional[str] = None
    article_dialect_status = "not_applicable"
    article_dialect_mode: Optional[str] = None
    article_dialect_reasons: tuple[str, ...] = tuple()

    # Explicit-parent recovery has the stronger source proof and therefore
    # owns the document when it succeeds (or blocks it when only a partial
    # recovery is possible).  The numeric dialect classifier runs only after
    # that decision and before either broad legacy fallback.
    if not recovery_unresolved_bare and not recovered_parent_articles:
        dialect = classify_article_dialect(
            text,
            structural=structural,
            bare_candidates=bare,
        )
        article_dialect_status = dialect.status
        article_dialect_mode = dialect.mode
        article_dialect_reasons = dialect.reasons
        if dialect.resolved:
            rejected_starts = dialect.rejected_structural_starts
            if rejected_starts:
                structural = [
                    match
                    for match in structural
                    if match.start() not in rejected_starts
                ]
            accepted = list(dialect.article_boundaries)

    structured_article_count = sum(
        1 for match in structural if _STATYA_KW_RE.match(match.group(1))
    )

    # Preserve the established fallback in meaning: documents without a
    # ``Статья`` header accept every guarded bare candidate at >=3.
    if recovery_unresolved_bare:
        # Do not combine an ambiguous parent-recovery attempt with another
        # fallback grammar in the same automatic parse.
        fallback_mode = None
    elif recovered_parent_articles:
        # This recovery is already one complete document-level proof.  Never
        # combine it with either dotted mixed fallback in the same source.
        fallback_mode = "mixed_parent_recovery"
    elif article_dialect_status == "resolved":
        fallback_mode = f"article_dialect_{article_dialect_mode}"
    elif article_dialect_status == "ambiguous":
        # Keep the parser output non-destructive while exposing an explicit
        # signal to catalog/preflight mutation gates.
        accepted = []
        fallback_mode = "article_dialect_ambiguous"
    elif structured_article_count == 0:
        if not rejected_explicit_article_nonheader and len(bare) >= 3:
            accepted = bare
            fallback_mode = "bare"
    else:
        mixed_groups = _mixed_special_header_groups(
            text,
            structural=structural,
            bare=bare,
        )
        mixed_mode = "mixed_special"
        if not mixed_groups:
            mixed_groups = _mixed_classified_chapter_tail_groups(
                text,
                structural=structural,
            )
            mixed_mode = "mixed_classified_chapter_tail"
        mixed = [
            candidate
            for group in mixed_groups
            for candidate in group.candidates
        ]
        accepted_mixed = [
            candidate
            for group in mixed_groups
            if group.accepted
            for candidate in group.candidates
        ]
        if accepted_mixed:
            accepted = accepted_mixed
            fallback_mode = mixed_mode

    seen = {match.start() for match in structural}
    matches = sorted(
        structural
        + [candidate for candidate in accepted if candidate.start() not in seen],
        key=lambda match: match.start(),
    )
    return HeaderMatchPlan(
        matches=tuple(matches),
        structural=tuple(structural),
        legacy_bare_candidates=tuple(bare),
        mixed_special_candidates=tuple(mixed),
        mixed_special_groups=tuple(mixed_groups),
        accepted_mixed_special_candidates=tuple(accepted_mixed),
        recovered_parent_articles=tuple(recovered_parent_articles),
        recovery_unresolved_bare_candidates=tuple(recovery_unresolved_bare),
        structured_article_count=structured_article_count,
        fallback_mode=fallback_mode,
        article_dialect_status=article_dialect_status,
        article_dialect_mode=article_dialect_mode,
        article_dialect_reasons=article_dialect_reasons,
        judicial_material_candidates=tuple(judicial_candidates),
        accepted_judicial_materials=tuple(),
        judicial_rejection_reasons=tuple(judicial_rejection_reasons),
    )


def _split_by_size(text: str, max_chars: int, overlap: int) -> List[str]:
    """Дробит крупный блок на куски ≤ max_chars по абзацам; очень длинные абзацы режет жёстко."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paras = [p.strip() for p in _PARA_SPLIT_RE.split(text) if p.strip()]
    pieces: List[str] = []
    cur = ""
    for p in paras:
        if len(p) > max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            step = max(1, max_chars - overlap)
            for i in range(0, len(p), step):
                pieces.append(p[i:i + max_chars])
            continue
        if not cur:
            cur = p
        elif len(cur) + len(p) + 1 <= max_chars:
            cur = cur + "\n" + p
        else:
            pieces.append(cur)
            cur = p
    if cur:
        pieces.append(cur)
    return pieces


def split_into_blocks(content: str) -> List[tuple[Optional[str], str]]:
    """Режет текст закона на структурные блоки [(article_ref | None, body), ...].

    article_ref — сырая строка заголовка («Статья 12.1 Халатность...», ≤120 симв.),
    None — преамбула до первого заголовка или документ без распознанных заголовков.
    Единственный источник сегментации: и чанкер (law_chunks), и каталог статей
    (law_articles) обязаны резать ОДИНАКОВО — по этому ref они связываются.
    """
    text = (content or "").strip()
    if not text:
        return []

    matches = list(_header_match_plan(text).matches)
    # Pure-bare fallback remains unchanged. Mixed documents get bare boundaries
    # only through one of the two strict, independently audited chapter plans.
    blocks: List[tuple[Optional[str], str]] = []

    if matches:
        if matches[0].start() > 0:
            preamble = text[:matches[0].start()].strip()
            if preamble:
                blocks.append((None, preamble))
        for i, mt in enumerate(matches):
            start = mt.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            ref = re.sub(r'\s+', ' ', mt.group(1)).strip()[:120]
            if isinstance(mt, MixedSpecialBoundary):
                # Only the formatting-only number/marker prefix is canonicalized.
                # The complete law text after the closing marker is copied
                # byte-for-byte (apart from outer whitespace stripped for every
                # block), so exporter/chunker/catalog see the same semantic body.
                remainder = text[mt.marker_end:end].strip()
                body = "\n".join(
                    part
                    for part in (
                        ref,
                        (
                            f"({mt.jurisdiction})"
                            if mt.jurisdiction
                            else None
                        ),
                        remainder,
                    )
                    if part
                )
            elif isinstance(mt, SplitArticleKeywordBoundary):
                # The split keyword is formatting damage, not semantic text.
                # Expose one canonical header to the shared catalog/chunker and
                # preserve the complete provision after the broken source line.
                remainder = text[mt.source_end:end].strip()
                body = "\n".join(part for part in (ref, remainder) if part)
            elif isinstance(mt, AdjacentDuplicateArticleBoundary):
                if mt.prefix_end is None:
                    # The real second boundary and its complete inline text are
                    # retained byte-for-byte; only the empty first duplicate is
                    # excluded.
                    body = text[mt.body_start:end].strip()
                else:
                    # The first boundary carries semantic inline text and the
                    # second is code-only formatting.  Drop just that duplicate.
                    prefix = text[mt.body_start:mt.prefix_end].strip()
                    remainder = text[mt.end():end].strip()
                    body = "\n".join(
                        part for part in (prefix, remainder) if part
                    )
            else:
                body = text[start:end].strip()
            if body:
                blocks.append((ref, body))
    else:
        blocks.append((None, text))
    return blocks


def chunk_law_text(content: str, title: str) -> List[Dict[str, Any]]:
    """Разбивает текст закона на чанки. Возвращает список {chunk_index, article_ref, content}."""
    blocks = split_into_blocks(content)
    if not blocks:
        return []

    chunks: List[Dict[str, Any]] = []
    for ref, body in blocks:
        for piece in _split_by_size(body, config.RAG_CHUNK_MAX_CHARS, config.RAG_CHUNK_OVERLAP_CHARS):
            chunks.append({"article_ref": ref, "content": piece})

    for idx, ch in enumerate(chunks):
        ch["chunk_index"] = idx
    return chunks


async def index_document(doc: Dict[str, Any]) -> int:
    """Индексирует один документ law_documents (dict с id, server_id, title, content, content_hash).
    Возвращает число записанных чанков."""
    content = doc.get("content")
    if not content:
        return 0
    chunks = chunk_law_text(content, doc.get("title") or "")
    if not chunks:
        return 0

    title = doc.get("title") or ""
    embeddings = await embed_texts(
        [c["content"] for c in chunks],
        task_type=TASK_DOCUMENT,
        titles=[title] * len(chunks),
    )
    if len(embeddings) != len(chunks):
        logger.warning(
            "[INDEXING] doc=%s: эмбеддингов %d != чанков %d — пропуск",
            doc.get("id"), len(embeddings), len(chunks),
        )
        return 0

    # Переиндексация: удаляем старые чанки документа и пишем заново — атомарно,
    # чтобы ретривал не видел документ «полупустым» посреди переиндексации.
    # Блокируем parent ДО DELETE/INSERT: exact cross-thread handoff использует тот
    # же row lock. Если он перенёс старые chunks в новый документ первым, этот
    # writer после ожидания штатно заменит их, а не добавит второй набор поверх.
    # Тип каста (vector/halfvec) — из config (halfvec для 3072d модели -2).
    pg_type = getattr(config, "EMBEDDING_PG_TYPE", "vector")
    async with db_manager.transaction() as conn:
        parent_id = await conn.fetchval(
            """
            SELECT id
            FROM law_documents
            WHERE id = $1 AND server_id = $2
            FOR UPDATE
            """,
            doc["id"],
            doc["server_id"],
        )
        if parent_id != doc["id"]:
            logger.warning(
                "[INDEXING] doc=%s исчез/сменил server до записи chunks — пропуск",
                doc.get("id"),
            )
            return 0
        await conn.execute("DELETE FROM law_chunks WHERE law_document_id = $1", doc["id"])
        for ch, emb in zip(chunks, embeddings):
            await conn.execute(
                f"""
                INSERT INTO law_chunks
                    (law_document_id, server_id, chunk_index, title, article_ref,
                     content, content_hash, token_estimate, embedding)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::{pg_type})
                """,
                doc["id"], doc["server_id"], ch["chunk_index"], doc.get("title"),
                ch.get("article_ref"), ch["content"], doc.get("content_hash"),
                len(ch["content"]) // 4, to_pgvector(emb),
            )
    return len(chunks)


async def reindex_changed_documents(
    server_id: str,
    thread_ids,
    *,
    document_lock_already_held: bool = False,
) -> Dict[str, int]:
    """Доводит RAG и Overlay-каталог изменённых документов до сохранённой версии.

    ``law_documents.derived_sync_pending`` является durable-маркером: он снимается
    только после успеха всех уже существующих для сервера derived-путей. Отсутствие
    baseline означает, что соответствующий путь серверу пока не нужен; выключенный
    kill-switch при существующем baseline, ошибка построения и CAS-конфликт маркер
    сохраняют, чтобы следующий цикл мог безопасно повторить работу.

    Старые поля статистики сохранены. ``documents``/``chunks`` по-прежнему описывают
    только RAG, ``skipped`` — документы, для которых RAG не запускался. Дополнительно
    возвращаются ``pending_cleared`` и ``failed``.

    ``document_lock_already_held=True`` разрешён только normal knowledge-base
    updater-у, который держит shared session document lock на всём вызове.
    Standalone callers оставляют False: public catalog sync сам возьмёт
    document→catalog locks.
    """
    if not isinstance(document_lock_already_held, bool):
        raise TypeError("document_lock_already_held must be bool")
    stats = {
        "documents": 0,
        "chunks": 0,
        "skipped": 0,
        "pending_cleared": 0,
        "failed": 0,
    }
    ids = list(dict.fromkeys(int(t) for t in thread_ids if t is not None))
    if not ids:
        return stats

    rows = await db_manager.fetch(
        """
        SELECT id, server_id, source_thread_id, title, content, content_hash,
               last_updated, derived_sync_pending
        FROM law_documents
        WHERE server_id = $1 AND source_thread_id = ANY($2::int[])
        """,
        server_id, ids,
    )
    docs = []
    for row in rows:
        doc = dict(row)
        if doc.get("derived_sync_pending") is True:
            docs.append(doc)
    if not docs:
        return stats

    doc_ids = {int(doc["id"]) for doc in docs}
    failed_ids: set[int] = set()

    # RAG и Overlay-каталог — независимые derived-пути. Наличие одного baseline
    # никак не должно блокировать обновление второго.
    has_rag_baseline = bool(await db_manager.fetchval(
        "SELECT EXISTS (SELECT 1 FROM law_chunks WHERE server_id = $1)",
        server_id,
    ))
    has_catalog_baseline = bool(await db_manager.fetchval(
        "SELECT EXISTS (SELECT 1 FROM law_articles WHERE server_id = $1)",
        server_id,
    ))

    if has_rag_baseline:
        if not getattr(config, "LAW_INDEX_AUTO_REINDEX", True):
            logger.info(
                "[INDEXING] auto-reindex disabled for server=%s; %d pending docs retained",
                server_id,
                len(docs),
            )
            stats["skipped"] += len(docs)
            failed_ids.update(doc_ids)
        else:
            for doc in docs:
                try:
                    chunks = await index_document(doc)
                except Exception:
                    logger.exception(
                        "[INDEXING] auto-reindex failed for document %s",
                        doc.get("id"),
                    )
                    failed_ids.add(int(doc["id"]))
                    continue
                if chunks:
                    stats["documents"] += 1
                    stats["chunks"] += chunks
                else:
                    logger.warning(
                        "[INDEXING] auto-reindex produced no chunks for document %s; "
                        "pending marker retained",
                        doc.get("id"),
                    )
                    failed_ids.add(int(doc["id"]))
    else:
        # Сохраняем прежний смысл skipped: RAG не запускался. Это не ошибка для
        # durable-маркера, потому что сервер ещё не имеет RAG baseline.
        stats["skipped"] += len(docs)

    if has_catalog_baseline and not getattr(config, "LAW_CATALOG_AUTO_SYNC", True):
        logger.info(
            "[LAW_CATALOG] auto-sync disabled for server=%s; %d pending docs retained",
            server_id,
            len(docs),
        )
        failed_ids.update(doc_ids)
    elif has_catalog_baseline:
        from services.law_catalog_service import sync_catalog_for_documents

        # Ошибка вне per-document reconcile считается неожиданной. До этой точки
        # pending ещё не очищался, поэтому исключение безопасно отдаётся вызывающему.
        if document_lock_already_held:
            catalog_summary = await sync_catalog_for_documents(
                server_id,
                sorted(doc_ids),
                document_lock_already_held=True,
            )
        else:
            # Keep two-argument compatibility for injected/test callables;
            # the real public entry self-acquires document -> catalog locks.
            catalog_summary = await sync_catalog_for_documents(
                server_id,
                sorted(doc_ids),
            )
        if not isinstance(catalog_summary, dict):
            logger.error(
                "[LAW_CATALOG] auto-sync returned no durable result for server=%s",
                server_id,
            )
            failed_ids.update(doc_ids)
        else:
            catalog_succeeded = doc_ids & {
                int(doc_id)
                for doc_id in catalog_summary.get("succeeded_document_ids", [])
            }
            catalog_failed = doc_ids & {
                int(doc_id)
                for doc_id in catalog_summary.get("failed_document_ids", [])
            }
            # Fail closed if an older/incomplete summary does not classify a doc.
            failed_ids.update(catalog_failed)
            failed_ids.update(doc_ids - catalog_succeeded)

    ready_docs = [
        doc for doc in docs
        if int(doc["id"]) not in failed_ids
    ]
    if ready_docs:
        # Снимаем marker только CAS-обновлением по версии исходного документа.
        # Одна транзакция не допускает частичной очистки batch при ошибке БД.
        cleared_ids: set[int] = set()
        async with db_manager.transaction() as conn:
            for doc in ready_docs:
                cleared_id = await conn.fetchval(
                    """
                    UPDATE law_documents
                    SET derived_sync_pending = FALSE
                    WHERE id = $1
                      AND server_id = $2
                      AND derived_sync_pending IS TRUE
                      AND last_updated IS NOT DISTINCT FROM $3
                    RETURNING id
                    """,
                    doc["id"],
                    server_id,
                    doc.get("last_updated"),
                )
                if cleared_id is not None:
                    cleared_ids.add(int(cleared_id))
        stats["pending_cleared"] = len(cleared_ids)
        failed_ids.update(
            int(doc["id"])
            for doc in ready_docs
            if int(doc["id"]) not in cleared_ids
        )

    stats["failed"] = len(failed_ids)
    logger.info(
        "[INDEXING] derived sync server=%s: rag_documents=%d chunks=%d "
        "pending_cleared=%d failed=%d",
        server_id,
        stats["documents"],
        stats["chunks"],
        stats["pending_cleared"],
        stats["failed"],
    )

    return stats


async def index_server_laws(server_id: str) -> Dict[str, int]:
    """Полная (пере)индексация всех документов сервера. Возвращает сводку."""
    docs = await db_manager.fetch(
        "SELECT id, server_id, title, content, content_hash FROM law_documents WHERE server_id = $1",
        server_id,
    )
    indexed_docs = 0
    total_chunks = 0
    for row in docs:
        doc = dict(row)
        try:
            n = await index_document(doc)
        except Exception:
            logger.exception("[INDEXING] ошибка индексации документа %s", doc.get("id"))
            continue
        if n:
            indexed_docs += 1
            total_chunks += n

    logger.info(
        "[INDEXING] server=%s: проиндексировано документов=%d, чанков=%d",
        server_id, indexed_docs, total_chunks,
    )
    return {"documents": indexed_docs, "chunks": total_chunks}


# END OF FILE services/indexing_service.py
