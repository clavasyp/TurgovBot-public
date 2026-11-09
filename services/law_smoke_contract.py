"""Pure, versioned contract for the Overlay law-search acceptance smoke.

The HTTP producer and the Phase A checkpoint consumer intentionally share this
module.  A JSON file may carry the producer's ``status``/``point`` fields for
operator readability, but acceptance always recomputes them from the immutable
case contract and the recorded top-card evidence.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence


LAW_SMOKE_SCHEMA_VERSION = 2
LAW_SMOKE_CASES = 10
LAW_SMOKE_MIN_SCORE = 8
LAW_SMOKE_MAX_AGE_SECONDS = 30 * 60
LAW_SMOKE_CLOCK_SKEW_SECONDS = 60


@dataclass(frozen=True)
class SemanticRule:
    """Conservative OR-of-ANDs predicate over visible top-card text."""

    name: str
    query: str
    alternatives: tuple[tuple[tuple[str, ...], ...], ...]


_AUTHORITY_REPRESENTATIVE_FORMS = (
    "представитель власти",
    "представителя власти",
    "представителю власти",
    "представителем власти",
    "представителе власти",
    "представители власти",
    "представителей власти",
    "представителям власти",
    "представителями власти",
    "представителях власти",
)

_PUBLIC_ORDER_FORMS = (
    "общественный порядок",
    "общественного порядка",
    "общественному порядку",
    "общественным порядком",
    "общественном порядке",
)


SLANG_RULES: tuple[SemanticRule, ...] = (
    SemanticRule(
        "homicide",
        "замочил копа",
        (
            (
                ("убийств", "причинен смерт", "лишен жизн"),
                (
                    "сотрудник",
                    "полицейск",
                    "правоохран",
                    "коп",
                    *_AUTHORITY_REPRESENTATIVE_FORMS,
                ),
            ),
            (
                ("посягательств",),
                ("жизн",),
                (
                    "сотрудник",
                    "полицейск",
                    "правоохран",
                    "коп",
                    *_AUTHORITY_REPRESENTATIVE_FORMS,
                ),
            ),
        ),
    ),
    SemanticRule(
        "residential_theft",
        "вынес хату",
        (
            (("краж",),),
            (("тайн",), ("хищен",)),
        ),
    ),
    SemanticRule(
        "illicit_weapon_purchase",
        "купил ствол с рук",
        (
            (
                ("оруж", "огнестрел"),
                (
                    "приобретен",
                    "покупк",
                    "купл",
                    "оборот",
                    "сбыт",
                    "передач",
                    "сделк",
                ),
                (
                    "незакон",
                    "нелегал",
                    "без лиценз",
                    "без разрешен",
                    "нарушен порядк",
                ),
            ),
        ),
    ),
    SemanticRule(
        "drug_possession",
        "поймали с закладкой",
        (
            (
                ("наркот", "запрещенн веществ", "психотроп"),
                (
                    "хранен",
                    "приобретен",
                    "ношен",
                    "владен",
                    "при себе",
                    "заклад",
                    "незаконн оборот",
                ),
            ),
        ),
    ),
    SemanticRule(
        "assault_on_employee",
        "мента избили",
        (
            (
                ("телесн", "поврежден", "избиен", "насили", "нападен"),
                (
                    "сотрудник",
                    "правоохран",
                    "полицейск",
                    "государственн",
                    *_AUTHORITY_REPRESENTATIVE_FORMS,
                ),
            ),
        ),
    ),
    SemanticRule(
        "obscene_language",
        "ругается матом",
        (
            (("нецензур",), ("лексик",)),
            (("нецензур", "матер"), ("бран", "ругат", "выражен")),
            (("мелк",), ("хулиган",), _PUBLIC_ORDER_FORMS),
            (("оскорблен",), ("неприличн", "нецензур")),
        ),
    ),
)

LAW_SMOKE_CASE_NAMES = (
    *(rule.name for rule in SLANG_RULES),
    "dynamic_code_doc_1",
    "dynamic_code_doc_2",
    "dynamic_code_doc_3",
    "dynamic_voice_code",
)
LAW_SMOKE_CASE_KINDS = (
    *(("slang",) * len(SLANG_RULES)),
    "exact_code_doc",
    "exact_code_doc",
    "exact_code_doc",
    "voice_code",
)

assert len(LAW_SMOKE_CASE_NAMES) == LAW_SMOKE_CASES
assert len(LAW_SMOKE_CASE_KINDS) == LAW_SMOKE_CASES

_RULE_BY_NAME = {rule.name: rule for rule in SLANG_RULES}
_DYNAMIC_EXACT_NAMES = frozenset(LAW_SMOKE_CASE_NAMES[6:9])


def normalize_smoke_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.lower().replace("ё", "е").split())


def semantic_card_text(card: Mapping[str, Any]) -> str:
    """Build semantic evidence without trusting a generic document title."""

    return normalize_smoke_text(
        " ".join(
            str(card.get(field) or "")
            for field in (
                "title",
                "article_ref",
                "penalty_summary",
                "snippet",
            )
        )
    )


def validate_semantic_card(
    rule: SemanticRule,
    card: Mapping[str, Any],
) -> tuple[str, str]:
    """Return pass or fail-closed manual_review for an editorially variable card."""

    text = semantic_card_text(card)
    if not text:
        return "manual_review", "top card has no semantic text"
    for alternative in rule.alternatives:
        if all(
            any(normalize_smoke_text(stem) in text for stem in group)
            for group in alternative
        ):
            return "pass", "top card satisfies the conservative semantic predicate"
    return (
        "manual_review",
        "top card wording does not prove the expected concept across editions",
    )


def top_card_evidence(top: Any) -> Optional[dict[str, Any]]:
    """Copy only the compact API fields needed for independent re-evaluation."""

    if not isinstance(top, Mapping):
        return None
    return {
        "id": top.get("id"),
        "code": top.get("code"),
        "kind": top.get("kind"),
        "title": top.get("title"),
        "doc_title": top.get("doc_title"),
        "article_ref": top.get("article_ref"),
        "penalty_summary": top.get("penalty_summary"),
        "snippet": str(top.get("snippet") or "")[:500],
        "rank_score": top.get("score"),
        "sources": list(top.get("sources") or ()),
    }


_UNITS = (
    "ноль",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
)
_TENS = {
    20: "двадцать",
    30: "тридцать",
    40: "сорок",
    50: "пятьдесят",
    60: "шестьдесят",
    70: "семьдесят",
    80: "восемьдесят",
    90: "девяносто",
}


def _number_to_words(value: int) -> Optional[str]:
    if not 0 <= value <= 99:
        return None
    if value < 20:
        return _UNITS[value]
    tens, units = divmod(value, 10)
    return (
        _TENS[tens * 10]
        if units == 0
        else f"{_TENS[tens * 10]} {_UNITS[units]}"
    )


def code_to_voice_query(code: str) -> Optional[str]:
    parts = str(code or "").split(".")
    if (
        not parts
        or any(not part.isdigit() for part in parts)
        or any(len(part) > 1 and part.startswith("0") for part in parts)
    ):
        return None
    words = [_number_to_words(int(part)) for part in parts]
    if any(word is None for word in words):
        return None
    return "статья " + " точка ".join(str(word) for word in words)


def _collapsed(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def canonical_dynamic_query(kind: str, expected: Mapping[str, Any]) -> str:
    code = _collapsed(expected.get("code"), limit=32)
    if kind == "exact_code_doc":
        doc_title = _collapsed(expected.get("doc_title"), limit=120)
        return f"статья {code} {doc_title}"
    if kind == "voice_code":
        query = code_to_voice_query(code)
        if query is None:
            raise ValueError("voice_code expected.code is not representable")
        return query
    raise ValueError(f"unsupported dynamic smoke kind: {kind!r}")


def validate_case_contract(case: Mapping[str, Any], index: int) -> None:
    """Validate immutable name/kind/query/expected fields for one case slot."""

    if not 0 <= index < LAW_SMOKE_CASES:
        raise ValueError(f"invalid smoke case index: {index}")
    name = case.get("name")
    kind = case.get("kind")
    query = case.get("query")
    expected_name = LAW_SMOKE_CASE_NAMES[index]
    expected_kind = LAW_SMOKE_CASE_KINDS[index]
    if name != expected_name:
        raise ValueError(
            f"case[{index}] name={name!r}, expected={expected_name!r}"
        )
    if kind != expected_kind:
        raise ValueError(
            f"case[{index}] kind={kind!r}, expected={expected_kind!r}"
        )

    expected = case.get("expected")
    if not isinstance(expected, Mapping):
        raise ValueError(f"case[{index}] expected must be an object")
    if kind == "slang":
        rule = _RULE_BY_NAME[expected_name]
        if query != rule.query:
            raise ValueError(
                f"case[{index}] fixed query={query!r}, expected={rule.query!r}"
            )
        if dict(expected) != {"semantic_validator": rule.name}:
            raise ValueError(f"case[{index}] semantic validator is not canonical")
        return

    if set(expected) != {"id", "code", "title", "doc_title"}:
        raise ValueError(f"case[{index}] dynamic expected fields are not canonical")
    article_id = expected.get("id")
    if type(article_id) is not int or article_id <= 0:
        raise ValueError(f"case[{index}] expected.id must be a positive integer")
    code = _collapsed(expected.get("code"), limit=32)
    if not code or code != expected.get("code"):
        raise ValueError(f"case[{index}] expected.code is not canonical")
    if kind == "exact_code_doc":
        doc_title = _collapsed(expected.get("doc_title"), limit=120)
        if (
            not 3 <= len(doc_title) <= 120
            or not any(char.isalpha() for char in doc_title)
            or doc_title != expected.get("doc_title")
        ):
            raise ValueError(f"case[{index}] expected.doc_title is not canonical")
    canonical_query = canonical_dynamic_query(str(kind), expected)
    if query != canonical_query:
        raise ValueError(
            f"case[{index}] query={query!r}, expected={canonical_query!r}"
        )


def recompute_case_result(
    case: Mapping[str, Any],
    index: int,
) -> dict[str, Any]:
    """Recompute technical and semantic/dynamic acceptance from evidence only."""

    validate_case_contract(case, index)
    top = case.get("top")
    technical_full = (
        type(case.get("http_status")) is int
        and case.get("http_status") == 200
        and case.get("search_mode") == "full"
        and case.get("degraded") is False
        and case.get("warnings") == []
        and isinstance(top, Mapping)
    )
    if not technical_full:
        return {
            "technical_full": False,
            "status": "technical_fail",
            "point": 0,
            "reason": "HTTP response is not 200/full/non-degraded/warning-free",
        }

    kind = str(case["kind"])
    if kind == "slang":
        status, reason = validate_semantic_card(
            _RULE_BY_NAME[str(case["name"])],
            top,
        )
    else:
        expected_id = case["expected"]["id"]
        actual_id = top.get("id")
        if type(actual_id) is int and actual_id == expected_id:
            status = "pass"
            reason = "dynamic expected article is top-1"
        else:
            status = "fail"
            reason = (
                "dynamic top-1 mismatch: "
                f"expected={expected_id}, actual={actual_id}"
            )
    return {
        "technical_full": True,
        "status": status,
        "point": 1 if status == "pass" else 0,
        "reason": reason,
    }


def recompute_cases(
    cases: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, bool, list[str]]:
    """Return normalized cases and aggregate counters without trusting claims."""

    if len(cases) != LAW_SMOKE_CASES:
        raise ValueError(
            f"smoke evidence must contain exactly {LAW_SMOKE_CASES} cases"
        )
    normalized: list[dict[str, Any]] = []
    score = 0
    all_full = True
    manual_review: list[str] = []
    for index, raw in enumerate(cases):
        if not isinstance(raw, Mapping):
            raise ValueError(f"case[{index}] is not an object")
        computed = recompute_case_result(raw, index)
        item = dict(raw)
        item.update(computed)
        normalized.append(item)
        score += int(computed["point"])
        all_full = all_full and computed["technical_full"] is True
        if computed["status"] == "manual_review":
            manual_review.append(str(raw["name"]))
    return normalized, score, all_full, manual_review
