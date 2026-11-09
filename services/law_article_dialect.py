"""Fail-closed recovery of article boundaries in bare numeric law sources.

The ordinary law parser intentionally treats ``N.M`` conservatively.  Some
source documents, however, use one of three complete and mechanically
provable numbering dialects:

* every article number is ``chapter.article`` and agrees with its chapter;
* article numbers form one global ``1..N`` path across all chapters;
* article numbers restart inside each chapter and form an outer path there.

This module classifies only those complete layouts.  It never receives a
server, document, or article id, and it never mutates source text.  Ambiguous
documents are reported to the caller so preflight can stop instead of silently
falling back to the broad legacy ``N.M`` interpretation.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional, Sequence

from services.law_structural_code import canonical_roman_value


DialectStatus = Literal["not_applicable", "resolved", "ambiguous"]
DialectMode = Literal["true_nm", "global", "chapter_local"]

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\u2060\ufeff]")
_ONE_LEVEL_RE = re.compile(
    r"^[ \t]*(?P<code>\d{1,3})\."
    r"(?!\d)(?:[ \t]+(?P<title>\S[^\r\n]*))?[ \t]*$"
)
_SPLIT_ONE_CODE_RE = re.compile(r"^[ \t]*(?P<code>\d{1,3})[ \t]*$")
_SPLIT_ONE_CONTINUATION_RE = re.compile(
    r"^[ \t]*\.[ \t]+[A-ZА-ЯЁ«\"]",
)
_DOTTED_RE = re.compile(
    r"^[ \t]*(?P<code>\d{1,3}\.\d{1,3})\.?"
    r"(?![.\d])(?:[ \t]+(?P<title>\S[^\r\n]*))?[ \t]*$"
)
_THREE_LEVEL_RE = re.compile(
    r"^[ \t]*(?P<code>\d{1,3}\.\d{1,3}\.\d{1,3})\.?"
    r"(?!\d)(?:[ \t]+\S[^\r\n]*)?[ \t]*$"
)
_CHAPTER_RE = re.compile(
    r"(?i)^\s*глава\s*(?:№|N)?\s*"
    r"(?P<code>\d{1,3}|[IVXLCMІХСМ\u2160-\u217f]+)"
    r"(?P<tail>[^\r\n]*)$"
)
_EXPLICIT_ARTICLE_RE = re.compile(r"(?i)^\s*(?:статья|ст\.)\b")
_PROSE_CHAPTER_TAIL_RE = re.compile(
    r"(?i)^\s*настоящ(?:его|ей)\s+"
    r"(?:кодекса|закона|главы|статьи|раздела)\b"
)
_SELF_REFERENCE_CLAUSE_RE = re.compile(
    r"(?i)^\s*(?:пункт|п\.)\s*[№N]?\s*\d+(?:[.\-–]\d+)*\.?\s+"
    r"(?:данной|настоящей)\s+статьи\b"
)
_PENALTY_PROOF_RE = re.compile(
    r"(?im)^[ \t]*наказание[ \t]*:",
)
_PENALTY_OWNERSHIP_PREFIX_RE = re.compile(
    r"(?i)^[ \t]*наказание[ \t]+за[ \t]+нарушение[ \t]+"
    r"процедур,[ \t]+описанных[ \t]+в[ \t]+статьях\b"
)
_PENALTY_OWNERSHIP_LIST_RE = re.compile(
    r"(?i)^[ \t]*[;:][ \t]*"
    r"(?P<codes>\d{1,3}(?:\.\d{1,3})?"
    r"(?:[ \t]*,[ \t]*\d{1,3}(?:\.\d{1,3})?)+)"
    r"[ \t]*[-–—][ \t]*штраф[ \t]+"
    r"(?:до[ \t]+)?\$[ \t]*\d{1,9}(?:[.,]\d{1,2})?"
    r"[ \t]*[.!]?[ \t]*$"
)
_PUNCTUATION_ONLY_RE = re.compile(r"^[\s.,:;!?(){}\[\]«»\"'—–-]*$")


@dataclass(frozen=True)
class DialectArticleBoundary:
    """Synthetic article boundary that preserves original source offsets."""

    source_start: int
    source_end: int
    canonical_ref: str
    code: str

    def start(self) -> int:
        return self.source_start

    def end(self) -> int:
        return self.source_end

    def group(self, index: int = 0) -> str:
        if index in (0, 1):
            return self.canonical_ref
        raise IndexError("no such group")


@dataclass(frozen=True)
class DialectClassification:
    """One complete dialect decision shared by parser and preflight."""

    status: DialectStatus
    mode: Optional[DialectMode]
    article_boundaries: tuple[Any, ...]
    rejected_structural_starts: frozenset[int]
    reasons: tuple[str, ...]
    observed_bare_count: int

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"


@dataclass(frozen=True)
class _Line:
    index: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Candidate:
    kind: Literal["one", "dotted", "three"]
    code: tuple[int, ...]
    line_index: int
    start: int
    end: int
    title: Optional[str]
    inline_title: bool
    separator_after_line: bool
    separator_after_title: bool
    substantive: bool
    original_boundary: Optional[Any] = None

    @property
    def major(self) -> int:
        return self.code[0]

    @property
    def code_text(self) -> str:
        return ".".join(str(part) for part in self.code)


@dataclass(frozen=True)
class _Chapter:
    start: int
    end: int
    number: int
    boundary: Any


def _clean_line(value: str) -> str:
    return _ZERO_WIDTH_RE.sub("", value or "").strip()


def _source_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    offset = 0
    for index, raw_line in enumerate(text.splitlines(keepends=True)):
        source = raw_line.rstrip("\r\n")
        lines.append(
            _Line(
                index=index,
                start=offset,
                end=offset + len(source),
                text=source,
            )
        )
        offset += len(raw_line)
    if not lines and text:
        lines.append(_Line(index=0, start=0, end=len(text), text=text))
    elif offset < len(text):
        lines.append(
            _Line(
                index=len(lines),
                start=offset,
                end=len(text),
                text=text[offset:],
            )
        )
    return lines


def _is_separator(line: Optional[_Line]) -> bool:
    if line is None:
        return False
    return not _clean_line(line.text)


def _next_visible_line(
    lines: Sequence[_Line],
    index: int,
) -> Optional[_Line]:
    for line in lines[index + 1 :]:
        if _clean_line(line.text):
            return line
    return None


def _looks_like_outline(value: str) -> bool:
    cleaned = _clean_line(value)
    return bool(
        _ONE_LEVEL_RE.fullmatch(cleaned)
        or _DOTTED_RE.fullmatch(cleaned)
        or _THREE_LEVEL_RE.fullmatch(cleaned)
        or _CHAPTER_RE.fullmatch(cleaned)
    )


def _candidate_has_payload(
    lines: Sequence[_Line],
    line_index: int,
    inline_title: bool,
) -> bool:
    if inline_title:
        return True
    following = _next_visible_line(lines, line_index)
    if following is None:
        return False
    value = _clean_line(following.text)
    return bool(
        value
        and not _looks_like_outline(value)
        and _PUNCTUATION_ONLY_RE.fullmatch(value) is None
    )


def _short_following_title(
    lines: Sequence[_Line],
    line_index: int,
) -> Optional[str]:
    following = _next_visible_line(lines, line_index)
    if following is None:
        return None
    value = re.sub(r"\s+", " ", _clean_line(following.text)).strip()
    if (
        not value
        or value.startswith(".")
        or len(value) > 90
        or _looks_like_outline(value)
        or _PUNCTUATION_ONLY_RE.fullmatch(value)
    ):
        return None
    return value.rstrip(".").strip() or None


def _build_candidates(
    text: str,
    bare_candidates: Sequence[Any],
) -> tuple[list[_Line], list[_Candidate], list[_Candidate], list[_Candidate]]:
    lines = _source_lines(text)
    bare_by_start = {item.start(): item for item in bare_candidates}
    one: list[_Candidate] = []
    dotted: list[_Candidate] = []
    three: list[_Candidate] = []

    for line in lines:
        cleaned = _clean_line(line.text)
        matched_kind: Optional[Literal["one", "dotted", "three"]] = None
        match = _ONE_LEVEL_RE.fullmatch(cleaned)
        split_one = False
        if match is not None:
            matched_kind = "one"
        else:
            match = _DOTTED_RE.fullmatch(cleaned)
            if match is not None:
                matched_kind = "dotted"
            else:
                match = _THREE_LEVEL_RE.fullmatch(cleaned)
                if match is not None:
                    matched_kind = "three"
                else:
                    match = _SPLIT_ONE_CODE_RE.fullmatch(cleaned)
                    following = (
                        lines[line.index + 1]
                        if line.index + 1 < len(lines)
                        else None
                    )
                    if (
                        match is not None
                        and following is not None
                        and _SPLIT_ONE_CONTINUATION_RE.match(following.text)
                    ):
                        matched_kind = "one"
                        split_one = True
        if match is None or matched_kind is None:
            continue

        # Dotted promotion is allowed only after the established bare-number
        # citation guards have accepted this exact source offset.
        original = None
        if matched_kind == "dotted":
            original = bare_by_start.get(line.start)
            if original is None:
                continue

        code = tuple(int(part) for part in match.group("code").split("."))
        title = match.groupdict().get("title")
        title = (
            re.sub(r"\s+", " ", title).strip().rstrip(".").strip()
            if title
            else None
        )
        following = lines[line.index + 1] if line.index + 1 < len(lines) else None
        after_following = (
            lines[line.index + 2] if line.index + 2 < len(lines) else None
        )
        candidate = _Candidate(
            kind=matched_kind,
            code=code,
            line_index=line.index,
            start=line.start,
            end=line.end,
            title=title,
            inline_title=bool(title) and not split_one,
            separator_after_line=_is_separator(following),
            separator_after_title=(
                not title
                and following is not None
                and bool(_clean_line(following.text))
                and _is_separator(after_following)
            ),
            substantive=_candidate_has_payload(
                lines,
                line.index,
                inline_title=bool(title),
            ),
            original_boundary=original,
        )
        if matched_kind == "one":
            one.append(candidate)
        elif matched_kind == "dotted":
            dotted.append(candidate)
        else:
            three.append(candidate)

    return lines, one, dotted, three


def _chapter_number(value: str) -> Optional[int]:
    match = _CHAPTER_RE.fullmatch(value)
    if match is None:
        return None
    tail = match.group("tail") or ""
    if _PROSE_CHAPTER_TAIL_RE.match(tail):
        return None
    token = match.group("code")
    if token.isdigit():
        number = int(token)
        return number if number > 0 else None
    return canonical_roman_value(token)


def _chapters(
    text: str,
    structural: Sequence[Any],
) -> tuple[list[_Chapter], frozenset[int], bool]:
    accepted: list[tuple[int, int, Any]] = []
    rejected: set[int] = set()
    has_explicit_articles = False

    for boundary in structural:
        ref = re.sub(r"\s+", " ", str(boundary.group(1) or "")).strip()
        if _EXPLICIT_ARTICLE_RE.match(ref):
            has_explicit_articles = True
        if _SELF_REFERENCE_CLAUSE_RE.match(ref):
            rejected.add(boundary.start())
            continue
        chapter_match = _CHAPTER_RE.fullmatch(ref)
        if chapter_match is None:
            continue
        number = _chapter_number(ref)
        if number is None:
            if _PROSE_CHAPTER_TAIL_RE.match(chapter_match.group("tail") or ""):
                rejected.add(boundary.start())
            continue
        accepted.append((boundary.start(), number, boundary))

    accepted.sort(key=lambda item: item[0])
    chapters = [
        _Chapter(
            start=start,
            end=(
                accepted[index + 1][0]
                if index + 1 < len(accepted)
                else len(text)
            ),
            number=number,
            boundary=boundary,
        )
        for index, (start, number, boundary) in enumerate(accepted)
    ]
    return chapters, frozenset(rejected), has_explicit_articles


def _chapter_for(
    chapters: Sequence[_Chapter],
    position: int,
) -> Optional[_Chapter]:
    if not chapters:
        return None
    starts = [chapter.start for chapter in chapters]
    index = bisect.bisect_right(starts, position) - 1
    if index < 0:
        return None
    chapter = chapters[index]
    return chapter if position < chapter.end else None


def _boundary_for_one(
    candidate: _Candidate,
    lines: Sequence[_Line],
) -> DialectArticleBoundary:
    title = candidate.title or _short_following_title(lines, candidate.line_index)
    ref = f"Статья {candidate.major}"
    if title:
        ref = f"{ref}. {title}"
    return DialectArticleBoundary(
        source_start=candidate.start,
        source_end=candidate.end,
        canonical_ref=ref,
        code=str(candidate.major),
    )


def _true_nm_classification(
    chapters: Sequence[_Chapter],
    bare: Sequence[Any],
    dotted: Sequence[_Candidate],
) -> Optional[tuple[Any, ...]]:
    if len(chapters) < 3 or len(bare) < 3 or len(dotted) != len(bare):
        return None
    aligned_chapters: set[int] = set()
    for candidate in dotted:
        chapter = _chapter_for(chapters, candidate.start)
        if chapter is None or candidate.major != chapter.number:
            return None
        aligned_chapters.add(chapter.start)
    if len(aligned_chapters) < 3:
        return None
    return tuple(sorted(bare, key=lambda item: item.start()))


def _global_outer_path(
    one: Sequence[_Candidate],
) -> Optional[tuple[list[_Candidate], list[_Candidate]]]:
    substantive = [candidate for candidate in one if candidate.substantive]
    if not substantive or substantive[0].major != 1:
        return None

    selected: list[_Candidate] = []
    expected = 1
    tail_start = len(substantive)
    for index, candidate in enumerate(substantive):
        if candidate.major == expected:
            selected.append(candidate)
            expected += 1
            continue
        tail_start = index
        break

    if len(selected) < 10:
        return None

    # A later substantive reset is a nested list, not a harmless image caption.
    # It makes the global path ambiguous instead of silently truncating it.
    tail_candidates = substantive[tail_start:]
    if tail_candidates:
        return None

    selected_starts = {candidate.start for candidate in selected}
    non_substantive_tail = [
        candidate
        for candidate in one
        if candidate.start > selected[-1].start
        and candidate.start not in selected_starts
    ]
    if any(candidate.substantive for candidate in non_substantive_tail):
        return None
    return selected, non_substantive_tail


def _candidate_block(
    text: str,
    candidate: _Candidate,
    outline: Sequence[_Candidate],
) -> str:
    positions = [item.start for item in outline]
    index = bisect.bisect_right(positions, candidate.start)
    end = positions[index] if index < len(positions) else len(text)
    return text[candidate.end:end]


def _explicit_article_reference(text: str, code: str) -> bool:
    """Return a local penalty-ownership proof, never a loose citation.

    The caller supplies only the owning outer-article family.  Within that
    source scope, the code must be enumerated after a plural article marker in
    a line that explicitly assigns punishment for violating those articles.
    Ordinary references such as ``согласно статье 3.1 другого кодекса`` are
    evidence about the cited law, not about this document's numbering dialect.
    """

    parent_major = code.split(".", maxsplit=1)[0]
    for line in text.splitlines():
        prefix = _PENALTY_OWNERSHIP_PREFIX_RE.match(line)
        if prefix is None:
            continue
        ownership = _PENALTY_OWNERSHIP_LIST_RE.match(
            line[prefix.end() :],
        )
        if ownership is None:
            continue
        listed = [
            ".".join(str(int(part)) for part in token.split("."))
            for token in re.findall(
                r"\d{1,3}(?:\.\d{1,3})?",
                ownership.group("codes"),
            )
        ]
        if (
            listed
            and listed[0] == parent_major
            and parent_major in listed
            and code in listed
            and len(listed) == len(set(listed))
            and all(
                token == parent_major
                or token.startswith(f"{parent_major}.")
                for token in listed
            )
        ):
            return True
    return False


def _outer_family_block(
    text: str,
    candidate: _Candidate,
    selected_outer: Sequence[_Candidate],
) -> str:
    owner_index = next(
        (
            index
            for index, outer in enumerate(selected_outer)
            if outer.major == candidate.major
        ),
        -1,
    )
    if owner_index < 0:
        return ""
    start = selected_outer[owner_index].start
    end = (
        selected_outer[owner_index + 1].start
        if owner_index + 1 < len(selected_outer)
        else len(text)
    )
    return text[start:end]


def _global_dotted_primaries(
    text: str,
    one: Sequence[_Candidate],
    dotted: Sequence[_Candidate],
    selected_outer: Sequence[_Candidate],
) -> list[_Candidate]:
    if not selected_outer:
        return []
    max_major = selected_outer[-1].major
    outline = sorted([*one, *dotted], key=lambda item: item.start)
    accepted: list[_Candidate] = []
    for candidate in dotted:
        if not candidate.substantive or candidate.major > max_major:
            continue
        block = _candidate_block(text, candidate, outline)
        family_block = _outer_family_block(
            text,
            candidate,
            selected_outer,
        )
        if _PENALTY_PROOF_RE.search(block) or _explicit_article_reference(
            family_block,
            candidate.code_text,
        ):
            accepted.append(candidate)
    return accepted


def _global_orphan_run(
    dotted: Sequence[_Candidate],
    selected_outer: Sequence[_Candidate],
    one: Sequence[_Candidate],
) -> list[_Candidate]:
    if not selected_outer:
        return []
    next_major = selected_outer[-1].major + 1
    if any(candidate.major == next_major for candidate in one):
        return []
    run = [
        candidate
        for candidate in dotted
        if candidate.major == next_major
        and candidate.start > selected_outer[-1].start
    ]
    run.sort(key=lambda item: item.start)
    if len(run) < 3 or any(not candidate.substantive for candidate in run):
        return []
    minors = [candidate.code[1] for candidate in run]
    if minors != list(range(1, minors[-1] + 1)):
        return []

    # The orphan path must be one contiguous same-level source run.  Ignoring
    # an interleaved 12.1 while collecting 11.1, 11.2, 11.3 would invent a
    # coherent article family that the source itself does not contain.
    same_level_between = sorted(
        [
            candidate
            for candidate in dotted
            if run[0].start <= candidate.start <= run[-1].start
        ],
        key=lambda item: item.start,
    )
    if [candidate.start for candidate in same_level_between] != [
        candidate.start for candidate in run
    ]:
        return []
    if any(
        candidate.start > selected_outer[-1].start
        and candidate.major > next_major
        for candidate in dotted
    ):
        return []
    if any(candidate.start > run[-1].start for candidate in dotted):
        return []
    if any(
        run[0].start < candidate.start < run[-1].start
        for candidate in one
    ):
        return []
    return run


def _has_matching_three_level_child(
    candidate: _Candidate,
    dotted: Sequence[_Candidate],
    one: Sequence[_Candidate],
    three: Sequence[_Candidate],
    chapter_end: int,
) -> bool:
    later_boundaries = [
        item.start
        for item in [*one, *dotted]
        if candidate.start < item.start < chapter_end
    ]
    end = min(later_boundaries) if later_boundaries else chapter_end
    return any(
        child.start > candidate.start
        and child.start < end
        and child.code[:2] == candidate.code
        for child in three
    )


def _chapter_primary_dotted(
    chapter: _Chapter,
    one: Sequence[_Candidate],
    dotted: Sequence[_Candidate],
    three: Sequence[_Candidate],
) -> list[_Candidate]:
    accepted: list[_Candidate] = []
    for candidate in dotted:
        if not (chapter.start < candidate.start < chapter.end):
            continue
        if candidate.inline_title and candidate.separator_after_line:
            accepted.append(candidate)
            continue
        if (
            not candidate.inline_title
            and candidate.separator_after_title
            and _has_matching_three_level_child(
                candidate,
                dotted,
                one,
                three,
                chapter.end,
            )
        ):
            accepted.append(candidate)
    return accepted


def _strong_one(candidate: _Candidate) -> bool:
    return (
        candidate.inline_title
        and candidate.separator_after_line
    ) or (
        not candidate.inline_title
        and candidate.separator_after_title
    )


def _hierarchical_outer_starts(
    candidates: Sequence[_Candidate],
    lines: Sequence[_Line],
) -> frozenset[int]:
    """Find a consecutive outer spine whose headings own nested ``1..`` lists.

    A repeated one-level list is otherwise indistinguishable from an article
    list after forum formatting has been flattened.  The outer headings are
    independently evidenced when a short heading is immediately followed by
    a reset to ``1``.  We accept this proof only for a unique consecutive run
    of at least two different majors.
    """

    ordered = sorted(candidates, key=lambda item: item.start)
    evidenced: list[_Candidate] = []
    for index, candidate in enumerate(ordered[:-1]):
        if candidate.major <= 1:
            continue
        title = candidate.title or _short_following_title(
            lines,
            candidate.line_index,
        )
        if not title:
            continue
        if ordered[index + 1].major == 1:
            evidenced.append(candidate)

    by_major: dict[int, list[_Candidate]] = {}
    for candidate in evidenced:
        by_major.setdefault(candidate.major, []).append(candidate)
    unique = {
        major: items[0]
        for major, items in by_major.items()
        if len(items) == 1
    }
    if len(unique) < 2:
        return frozenset()

    runs: list[list[int]] = []
    for major in sorted(unique):
        if not runs or major != runs[-1][-1] + 1:
            runs.append([major])
        else:
            runs[-1].append(major)
    proven = [run for run in runs if len(run) >= 2]
    if len(proven) != 1:
        return frozenset()
    return frozenset(unique[major].start for major in proven[0])


def _unique_visible_run(
    candidates: Sequence[_Candidate],
) -> Optional[list[_Candidate]]:
    visible = [candidate for candidate in candidates if candidate.substantive]
    if len(visible) < 2:
        return None
    majors = [candidate.major for candidate in visible]
    if len(set(majors)) != len(majors):
        return None
    if majors[0] not in (1, 2):
        return None
    if majors != list(range(majors[0], majors[-1] + 1)):
        return None
    return visible


def _chapter_outer_path(
    chapter: _Chapter,
    one: Sequence[_Candidate],
    primary_dotted: Sequence[_Candidate],
    lines: Sequence[_Line],
) -> tuple[Optional[list[_Candidate]], Optional[str]]:
    chapter_one = [
        candidate
        for candidate in one
        if chapter.start < candidate.start < chapter.end
        and candidate.substantive
    ]
    strong_by_major: dict[int, list[_Candidate]] = {}
    weak_by_major: dict[int, list[_Candidate]] = {}
    hierarchical_starts = _hierarchical_outer_starts(chapter_one, lines)
    for candidate in chapter_one:
        target = (
            strong_by_major
            if _strong_one(candidate) or candidate.start in hierarchical_starts
            else weak_by_major
        )
        target.setdefault(candidate.major, []).append(candidate)

    dotted_by_major: dict[int, list[_Candidate]] = {}
    for candidate in primary_dotted:
        dotted_by_major.setdefault(candidate.major, []).append(candidate)

    anchor_majors = sorted(set(strong_by_major) | set(dotted_by_major))
    if not anchor_majors:
        run = _unique_visible_run(chapter_one)
        if run is not None:
            return run, None
        if chapter_one:
            return None, "one-level headings do not prove a unique outer path"
        return [], None

    if any(len(items) != 1 for items in strong_by_major.values()):
        return None, "multiple strong one-level headings share one code"

    first_major = anchor_majors[0]
    if first_major > 2:
        return None, "chapter outer path starts after major 2"
    forced_weak: dict[int, _Candidate] = {}
    while first_major > 1:
        first_anchor_position = min(
            [
                *(
                    item.start
                    for item in strong_by_major.get(first_major, [])
                ),
                *(
                    item.start
                    for item in dotted_by_major.get(first_major, [])
                ),
            ]
        )
        previous = [
            item
            for item in weak_by_major.get(first_major - 1, [])
            if item.start < first_anchor_position
        ]
        if len(previous) != 1:
            break
        forced_weak[first_major - 1] = previous[0]
        first_major -= 1
    last_major = anchor_majors[-1]
    last_anchor_position = max(
        [
            *(item.start for item in strong_by_major.get(last_major, [])),
            *(item.start for item in dotted_by_major.get(last_major, [])),
        ]
    )
    while True:
        next_major = last_major + 1
        candidates = [
            *strong_by_major.get(next_major, []),
            *weak_by_major.get(next_major, []),
        ]
        following = [
            candidate
            for candidate in candidates
            if candidate.start > last_anchor_position
        ]
        if len(following) != 1:
            break
        if any(
            candidate.start > last_anchor_position
            and candidate.start < following[0].start
            and candidate.major <= last_major
            for candidate in chapter_one
        ):
            break
        forced_weak[next_major] = following[0]
        last_major = next_major
        last_anchor_position = following[0].start

    selected: list[_Candidate] = []
    for major in range(first_major, last_major + 1):
        strong = strong_by_major.get(major, [])
        dotted_items = sorted(
            dotted_by_major.get(major, []),
            key=lambda item: item.code,
        )
        one_item: Optional[_Candidate] = strong[0] if strong else None
        if one_item is None and dotted_items:
            preceding_weak = [
                item
                for item in weak_by_major.get(major, [])
                if item.start < dotted_items[0].start
            ]
            if len(preceding_weak) == 1:
                one_item = preceding_weak[0]
            elif len(preceding_weak) > 1:
                return None, (
                    f"major {major} has multiple possible parents before "
                    "a dotted outer heading"
                )
        if one_item is None and not dotted_items:
            weak = (
                [forced_weak[major]]
                if major in forced_weak
                else weak_by_major.get(major, [])
            )
            if len(weak) != 1:
                return None, f"major {major} has no unique outer heading"
            one_item = weak[0]
        if one_item is not None:
            selected.append(one_item)
        selected.extend(dotted_items)

    # Numeric order must agree with source order.  A nested reset that happens
    # to reuse the same numbers is therefore rejected rather than guessed.
    if [item.start for item in selected] != sorted(item.start for item in selected):
        return None, "outer headings do not follow source order"
    return selected, None


def _chapter_local_boundaries(
    chapters: Sequence[_Chapter],
    one: Sequence[_Candidate],
    dotted: Sequence[_Candidate],
    three: Sequence[_Candidate],
    lines: Sequence[_Line],
) -> tuple[Optional[list[_Candidate]], tuple[str, ...]]:
    selected: list[_Candidate] = []
    reasons: list[str] = []
    populated_chapters = 0

    for chapter in chapters:
        primary_dotted = _chapter_primary_dotted(
            chapter,
            one,
            dotted,
            three,
        )
        path, reason = _chapter_outer_path(
            chapter,
            one,
            primary_dotted,
            lines,
        )
        if path is None:
            reasons.append(
                f"chapter@{chapter.start}: {reason or 'ambiguous outer path'}"
            )
            continue
        if path:
            populated_chapters += 1
            selected.extend(path)

    if reasons:
        return None, tuple(reasons)
    if populated_chapters < 2 or len(selected) < 10:
        return None, ("too few proven chapter-local article paths",)
    selected.sort(key=lambda item: item.start)
    return selected, ()


def _materialize_boundaries(
    candidates: Iterable[_Candidate],
    lines: Sequence[_Line],
) -> tuple[Any, ...]:
    boundaries: list[Any] = []
    seen_starts: set[int] = set()
    for candidate in sorted(candidates, key=lambda item: item.start):
        if candidate.start in seen_starts:
            continue
        seen_starts.add(candidate.start)
        if candidate.kind == "dotted":
            if candidate.original_boundary is None:
                raise AssertionError("dotted dialect boundary lacks citation guard")
            boundaries.append(candidate.original_boundary)
        elif candidate.kind == "one":
            boundaries.append(_boundary_for_one(candidate, lines))
    return tuple(boundaries)


def classify_article_dialect(
    text: str,
    *,
    structural: Sequence[Any],
    bare_candidates: Sequence[Any],
) -> DialectClassification:
    """Classify one complete numeric article dialect without any ID knowledge."""

    source = text or ""
    lines, one, dotted, three = _build_candidates(source, bare_candidates)
    chapters, rejected_structural, has_explicit_articles = _chapters(
        source,
        structural,
    )
    observed = len(bare_candidates)

    if has_explicit_articles or len(chapters) < 3 or observed < 3:
        return DialectClassification(
            status="not_applicable",
            mode=None,
            article_boundaries=tuple(),
            rejected_structural_starts=frozenset(),
            reasons=tuple(),
            observed_bare_count=observed,
        )

    true_nm = _true_nm_classification(chapters, bare_candidates, dotted)
    if true_nm is not None:
        return DialectClassification(
            status="resolved",
            mode="true_nm",
            article_boundaries=true_nm,
            rejected_structural_starts=frozenset(),
            reasons=tuple(),
            observed_bare_count=observed,
        )

    mismatched = sum(
        1
        for candidate in dotted
        if (
            (chapter := _chapter_for(chapters, candidate.start)) is None
            or candidate.major != chapter.number
        )
    )
    substantive_one = [candidate for candidate in one if candidate.substantive]
    ambiguity_triggered = (
        len(substantive_one) >= 3
        and mismatched >= 3
        and mismatched * 5 >= max(1, len(dotted)) * 3
    )
    if not ambiguity_triggered:
        return DialectClassification(
            status="not_applicable",
            mode=None,
            article_boundaries=tuple(),
            rejected_structural_starts=frozenset(),
            reasons=tuple(),
            observed_bare_count=observed,
        )

    global_path = _global_outer_path(one)
    if global_path is not None:
        selected_outer, _caption_tail = global_path
        selected_dotted = _global_dotted_primaries(
            source,
            one,
            dotted,
            selected_outer,
        )
        orphan = _global_orphan_run(dotted, selected_outer, one)
        boundaries = _materialize_boundaries(
            [*selected_outer, *selected_dotted, *orphan],
            lines,
        )
        return DialectClassification(
            status="resolved",
            mode="global",
            article_boundaries=boundaries,
            rejected_structural_starts=rejected_structural,
            reasons=tuple(),
            observed_bare_count=observed,
        )

    chapter_local, reasons = _chapter_local_boundaries(
        chapters,
        one,
        dotted,
        three,
        lines,
    )
    if chapter_local is not None:
        return DialectClassification(
            status="resolved",
            mode="chapter_local",
            article_boundaries=_materialize_boundaries(chapter_local, lines),
            rejected_structural_starts=rejected_structural,
            reasons=tuple(),
            observed_bare_count=observed,
        )

    return DialectClassification(
        status="ambiguous",
        mode=None,
        article_boundaries=tuple(),
        rejected_structural_starts=frozenset(),
        reasons=reasons or ("numeric article dialect is not fully proven",),
        observed_bare_count=observed,
    )
