"""Pure deterministic evidence helpers for post-RRF law-search ranking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


_NUMBER = r"(?:\d{1,3}(?:[ .,]\d{3})+|\d+)(?:[,.]\d+)?"
_TOKEN_RE = re.compile(
    rf"{_NUMBER}|[$%]|[a-zа-яё]+",
    re.IGNORECASE,
)
_SANCTION_MARKER_RE = re.compile(
    r"(?:^|[\n.;:—–]|\s-\s)\s*(?:"
    r"наказание|карается|приоритет\s+розыска|"
    r"штрафная\s+санкция|мера\s+наказания|"
    r"штраф\b|влеч[её]т\b"
    r")\b",
    re.IGNORECASE,
)
_SANCTION_CLAUSE_RE = re.compile(
    r"\b(?:"
    r"наказыва(?:ется|ются)|карается|"
    r"штрафу(?:ется|ются)|"
    r"подлеж(?:ит|ат)\s+наказани(?:ю|ям)|"
    r"наказуем(?:о|а|ы|ое|ая|ые)\s+"
    r"(?:административн(?:ым|ой|ыми)\s+|уголовн(?:ым|ой|ыми)\s+)?"
    r"(?:штрафом|арестом|лишением|наказанием)|"
    r"(?:влеч[её]т|предусматривает)\s+"
    r"(?:за\s+собой\s+)?(?:наложение\s+)?"
    r"(?:(?:административн|уголовн|денежн)\w*\s+)?"
    r"(?:штраф(?:а|у|ом)?|арест|лишение|наказание|санкци[юя])|"
    r"назнача(?:ется|ются)\s+"
    r"(?:(?:административн|уголовн|денежн)\w*\s+)?"
    r"(?:штраф|арест|лишение|наказание|санкци[юя])|"
    r"подверга(?:ется|ются)\s+"
    r"(?:(?:административн|уголовн|денежн)\w*\s+)?"
    r"(?:штрафу|аресту|наказанию)|"
    r"(?:налага|накладыва)(?:ется|ются)\s+"
    r"(?:(?:административн|уголовн|денежн)\w*\s+)?"
    r"штраф\w*(?:\s+санкци\w*)?|"
    r"с\s+назначением\s+"
    r"(?:(?:административн|уголовн|денежн|судебн)\w*\s+)?штрафа|"
    r"основани(?:ем|я)\s+для\s+взыскани\w*"
    r"(?=[^.;\n]{0,120}\bштраф\w*)|"
    r"может\s+привести\s+к\s+назначени\w*"
    r"(?=[^.;\n]{0,120}\bштраф\w*)|"
    r"может\s+применить\s+санкци\w*"
    r"(?=[^.;\n]{0,160}\b(?:наложени\w*\s+)?штраф\w*)|"
    r"наложени\w+\s+штрафн\w+\s+санкци\w*|"
    r"может\s+быть\s+(?:назначен|наложен)(?:а|о|ы)?"
    r"(?=[^.;\n]{0,120}\bштраф\w*)|"
    r"вправе\s+назначить"
    r"(?=[^.;\n]{0,120}\bштраф\w*)|"
    r"предусмотрен(?:а|о|ы)?\s+в\s+виде\s+"
    r"(?:(?:административн|уголовн|денежн)\w*\s+)?штрафа?|"
    r"может\s+быть\s+применен(?:о|а|ы)?"
    r"(?=[^.;\n]{0,120}\b(?:взыскани\w*|штраф\w*))"
    r")\b",
    re.IGNORECASE,
)
_UNIT_PATTERN = (
    r"(?:"
    r"(?:г|кг|мг)(?![a-zа-яё])|"
    r"грам(?:мами|мам|мах|мов|ма|м)?|"
    r"килограмм(?:ами|ам|ах|ов|ом|а|у|ы)?|"
    r"миллиграм(?:мами|мам|мах|мов|ма|м)?|"
    r"доллар(?:ами|ах|ов|ом|а|у)?|\$|"
    r"процент(?:ами|ах|ов|ом|а|у)?|%|"
    r"час(?:ами|ах|ов|ом|а|у)?|"
    r"сут(?:ками|кам|ках|ки|ок)?|"
    r"д(?:ень|ня|ню|нем|ни|ней|ням|нями|нях)|"
    r"голос(?:ами|ах|ов|ом|а|у)?"
    r")(?![a-zа-яё])"
)
_UNIT_RE = re.compile(rf"^(?:{_UNIT_PATTERN})$", re.IGNORECASE)
_DAY_FORMS = frozenset(
    {
        "день",
        "дня",
        "дню",
        "днем",
        "дни",
        "дней",
        "дням",
        "днями",
        "днях",
    }
)
_SCALE_VALUES = {
    "сотня": 100.0,
    "сотни": 100.0,
    "сотен": 100.0,
    "тысяч": 1_000.0,
    "тысяча": 1_000.0,
    "тысячи": 1_000.0,
    "тыс": 1_000.0,
    "к": 1_000.0,
    "k": 1_000.0,
    "миллион": 1_000_000.0,
    "миллиона": 1_000_000.0,
    "миллионов": 1_000_000.0,
    "млн": 1_000_000.0,
    "миллиард": 1_000_000_000.0,
    "миллиарда": 1_000_000_000.0,
    "миллиардов": 1_000_000_000.0,
    "млрд": 1_000_000_000.0,
    "триллион": 1_000_000_000_000.0,
    "триллиона": 1_000_000_000_000.0,
    "триллионов": 1_000_000_000_000.0,
}
_SCALE_PATTERN = "(?:" + "|".join(
    re.escape(value)
    for value in sorted(_SCALE_VALUES, key=len, reverse=True)
) + r")(?![a-zа-яё])"
_UNCERTAIN_NUMBER_CUES = frozenset(
    {
        "до",
        "от",
        "свыше",
        "более",
        "менее",
        "выше",
        "ниже",
        "больше",
        "меньше",
        "минимум",
        "максимум",
        "около",
        "примерно",
        "приблизительно",
        "ориентировочно",
        "порядка",
        "почти",
        "между",
    }
)
_NUMERIC_RANGE_RE = re.compile(
    rf"{_NUMBER}\s*(?:[-–—/]|\.{{2,}})\s*{_NUMBER}",
    re.IGNORECASE,
)
_APPROXIMATE_NUMBER_RE = re.compile(
    rf"(?:[~≈±]\s*{_NUMBER}|{_NUMBER}\s*±\s*{_NUMBER}|{_NUMBER}\s*\+)",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    rf"\bот\s+(?P<lower_prefix>[$%])?\s*"
    rf"(?P<lower>{_NUMBER})"
    rf"(?:\s*(?P<lower_scale>{_SCALE_PATTERN}))?"
    rf"(?:\s*(?P<lower_suffix>{_UNIT_PATTERN}))?\s*"
    rf"(?:до|по)\s+(?P<upper_prefix>[$%])?\s*"
    rf"(?P<upper>{_NUMBER})"
    rf"(?:\s*(?P<upper_scale>{_SCALE_PATTERN}))?"
    rf"(?:\s*(?P<upper_suffix>{_UNIT_PATTERN}))?"
    rf"(?P<inclusive>\s+включительно)?",
    re.IGNORECASE,
)
_BROAD_RANGE_RE = re.compile(
    rf"\bот\s+[$%]?\s*{_NUMBER}"
    rf"(?:\s+[a-zа-яё]+){{0,2}}\s+"
    rf"(?:до|по)\s+[$%]?\s*{_NUMBER}"
    rf"(?:\s+[a-zа-яё]+){{0,2}}",
    re.IGNORECASE,
)
_BOUND_OPERATOR_PATTERN = (
    r"(?:"
    r"не\s+(?:более|менее|выше|ниже|свыше)|"
    r"менее|до|свыше|более|выше|ниже|от|ровно"
    r")"
)
_UNSUPPORTED_NEGATED_BOUND_RE = re.compile(
    rf"(?<![\w])не\s+(?:ровно|от|до)\s+"
    rf"[$%]?\s*{_NUMBER}"
    rf"(?:\s*{_SCALE_PATTERN})?"
    rf"(?:\s*{_UNIT_PATTERN})",
    re.IGNORECASE,
)
_UNSUPPORTED_NEGATED_RANGE_RE = re.compile(
    rf"\bне\s+(?:в\s+(?:диапазоне|интервале|пределах)\s+)?"
    rf"от\s+[^.;!?\n]{{0,80}}\b(?:до|по)\s+"
    rf"[^.;!?\n]{{0,50}}(?:[$%]|{_UNIT_PATTERN})",
    re.IGNORECASE,
)
_DISTRIBUTED_NEGATED_BOUND_RE = re.compile(
    rf"\bне\s+(?:"
    rf"может|могут|мог|могла|могло|могли|"
    rf"должен|должна|должно|должны"
    rf")\b"
    rf"[^.;!?\n]{{0,80}}\b"
    rf"(?:более|менее|выше|ниже|свыше|до|от|ровно)\s+"
    rf"[$%]?\s*{_NUMBER}"
    rf"(?:\s*{_SCALE_PATTERN})?"
    rf"(?:\s*{_UNIT_PATTERN})",
    re.IGNORECASE,
)
_BOUND_RE = re.compile(
    rf"(?<![\w])(?P<operator>{_BOUND_OPERATOR_PATTERN})"
    rf"\s+(?P<prefix>[$%])?\s*(?P<value>{_NUMBER})"
    rf"(?:\s*(?P<scale>{_SCALE_PATTERN}))?"
    rf"(?:\s*(?P<suffix>{_UNIT_PATTERN}))?",
    re.IGNORECASE,
)
_COORDINATED_BOUND_RE = re.compile(
    rf"(?<![\w])(?P<left_operator>{_BOUND_OPERATOR_PATTERN})"
    rf"\s+(?P<left_prefix>[$%])?\s*(?P<left_value>{_NUMBER})"
    rf"(?:\s*(?P<left_scale>{_SCALE_PATTERN}))?"
    rf"(?:\s*(?P<left_suffix>{_UNIT_PATTERN}))?"
    rf"\s*[,;:—–-]?\s*"
    rf"(?P<connector>"
    rf"но|и(?:\s+одновременно)?|или|либо|а\s+также|при\s+этом|/"
    rf")\s+"
    rf"(?P<right_operator>{_BOUND_OPERATOR_PATTERN})"
    rf"\s+(?P<right_prefix>[$%])?\s*(?P<right_value>{_NUMBER})"
    rf"(?:\s*(?P<right_scale>{_SCALE_PATTERN}))?"
    rf"(?:\s*(?P<right_suffix>{_UNIT_PATTERN}))?",
    re.IGNORECASE,
)
_AND_ABOVE_RE = re.compile(
    rf"(?P<prefix>[$%])?\s*(?P<value>{_NUMBER})"
    rf"(?:\s*(?P<scale>{_SCALE_PATTERN}))?"
    rf"(?:\s*(?P<suffix>{_UNIT_PATTERN}))?"
    rf"\s+(?P<inclusive>и\s+(?:более|выше))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QueryMeasure:
    value: float
    unit: str


@dataclass(frozen=True)
class ApplicabilityInterval:
    unit: str
    lower: float | None = None
    upper: float | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    def contains(self, value: float) -> bool:
        if self.lower is not None:
            if value < self.lower:
                return False
            if value == self.lower and not self.lower_inclusive:
                return False
        if self.upper is not None:
            if value > self.upper:
                return False
            if value == self.upper and not self.upper_inclusive:
                return False
        return True


def _number_value(raw: str) -> float:
    value = raw.strip().replace("\xa0", " ")
    if re.fullmatch(r"0\.\d+", value):
        # A leading-zero decimal is never a forum thousands group:
        # ``0.005 грамма`` means five milligrams, not five grams.
        pass
    elif re.fullmatch(r"\d{1,3}(?:[ .]\d{3})+(?:,\d+)?", value):
        value = value.replace(" ", "").replace(".", "")
        value = value.replace(",", ".")
    else:
        value = value.replace(" ", "").replace(",", ".")
    return float(value)


def _safe_number_value(raw: str) -> float | None:
    try:
        return _number_value(raw)
    except (TypeError, ValueError):
        return None


def _unit(raw: str) -> str | None:
    token = raw.lower().replace("ё", "е")
    if _UNIT_RE.fullmatch(token) is None:
        return None
    if token == "$" or token.startswith("доллар"):
        return "money_usd"
    if token == "%" or token.startswith("процент"):
        return "percent"
    if (
        token.startswith("грам")
        or token.startswith("килограмм")
        or token.startswith("миллиграм")
        or token in {"г", "кг", "мг"}
    ):
        return "mass_g"
    if token == "г":
        return "mass_g"
    if token.startswith("час"):
        return "duration_hours"
    if token.startswith("сут") or token in _DAY_FORMS:
        return "duration_days"
    if token.startswith("голос"):
        return "votes"
    return None


def _unit_factor(raw: str | None) -> float:
    token = (raw or "").lower().replace("ё", "е")
    if token.startswith("килограмм") or token == "кг":
        return 1_000.0
    if token.startswith("миллиграм") or token == "мг":
        return 0.001
    return 1.0


_OTHER_MEASURE_UNIT_STEMS = (
    "рубл",
    "минут",
    "секунд",
    "недел",
    "месяц",
    "звезд",
    "уров",
    "балл",
    "метр",
    "километр",
    "миль",
    "штук",
    "единиц",
    "патрон",
    "растен",
    "семен",
    "человек",
)
_OTHER_MEASURE_UNIT_FORMS = frozenset(
    {
        "год", "года", "году", "годом", "годы", "годов",
        "лет", "км",
    }
)


def _is_any_measure_unit(token: str) -> bool:
    normalized = token.lower().replace("ё", "е")
    return (
        _unit(normalized) is not None
        or normalized in _OTHER_MEASURE_UNIT_FORMS
        or normalized.startswith(_OTHER_MEASURE_UNIT_STEMS)
    )


def _has_conflicting_trailing_unit(
    text: str,
    end: int,
    *,
    unit: str,
) -> bool:
    match = re.match(r"\s*([a-zа-яё]+|[$%])", text[end:], re.IGNORECASE)
    if match is None or not _is_any_measure_unit(match.group(1)):
        return False
    trailing_unit = _unit(match.group(1))
    return not (unit == "percent" and trailing_unit == "votes")


def _typed_number_value(
    raw: str,
    *,
    unit: str,
    scale: str | None = None,
    raw_unit: str | None = None,
) -> float | None:
    """Parse one value only after its physical type has been proven."""

    value_text = raw.strip().replace("\xa0", " ")
    try:
        if (
            unit == "money_usd"
            and re.fullmatch(r"\d{1,3}(?:,\d{3})+", value_text)
        ):
            value = float(value_text.replace(",", ""))
        else:
            value = _number_value(value_text)
    except (TypeError, ValueError):
        return None
    if scale:
        multiplier = _SCALE_VALUES.get(scale.lower().replace("ё", "е"))
        if multiplier is None:
            return None
        value *= multiplier
    value *= _unit_factor(raw_unit)
    return value


def _consistent_unit(*raw_units: str | None) -> str | None:
    """Return one proven unit, refusing absent or contradictory evidence."""

    normalized_raw = {
        raw_unit.lower().replace("ё", "е")
        for raw_unit in raw_units
        if raw_unit
    }
    units = {
        unit
        for raw_unit in raw_units
        if raw_unit and (unit := _unit(raw_unit)) is not None
    }
    if "%" in normalized_raw and units == {"percent", "votes"}:
        # Natural denominator syntax: «25% голосов».  Other mixed units
        # remain contradictory and fail closed.
        return "percent"
    return next(iter(units)) if len(units) == 1 else None


def _query_number_value(
    tokens: Sequence[str],
    index: int,
) -> float | None:
    """Interpret comma grouping only when the adjacent unit proves money."""

    prefix = (
        tokens[index - 1]
        if index > 0 and tokens[index - 1] in {"$", "%"}
        else None
    )
    cursor = index + 1
    scale = (
        tokens[cursor]
        if cursor < len(tokens) and tokens[cursor] in _SCALE_VALUES
        else None
    )
    if scale:
        cursor += 1
    suffix_token = (
        tokens[cursor]
        if cursor < len(tokens) and _is_any_measure_unit(tokens[cursor])
        else None
    )
    suffix = (
        suffix_token
        if suffix_token and _unit(suffix_token) is not None
        else None
    )
    if suffix_token and suffix is None:
        return _safe_number_value(tokens[index])
    unit = _consistent_unit(prefix, suffix)
    if unit is None:
        return _safe_number_value(tokens[index])
    token = tokens[index]
    if (
        unit == "money_usd"
        and re.fullmatch(r"\d{1,3}(?:,\d{3})+", token)
    ):
        return float(token.replace(",", ""))
    # Semantic numbers store the number as spoken/written, before a scale or
    # physical-unit conversion.  Compare that raw value here; kilograms and
    # milligrams are converted only when the final QueryMeasure is built.
    return _safe_number_value(token)


def _is_allowed_query_number(
    token: str,
    *,
    parsed: float | None = None,
    allowed_raw: set[str],
    allowed: set[float],
) -> bool:
    value = parsed if parsed is not None else _safe_number_value(token)
    if value is None:
        return False
    grouped_parts = re.findall(r"\d+", token)
    grouped_semantic = (
        len(grouped_parts) > 1
        and all(part in allowed_raw for part in grouped_parts)
    )
    return value in allowed or grouped_semantic


def _has_compound_scaled_measure(
    tokens: Sequence[str],
    allowed_indexes: set[int],
) -> bool:
    """Reject partial readings such as ``1 million 500 thousand dollars``."""

    for index in range(len(tokens) - 3):
        if _safe_number_value(tokens[index]) is None:
            continue
        if tokens[index + 1] not in _SCALE_VALUES:
            continue
        cursor = index + 2
        if cursor < len(tokens) and tokens[cursor] in {"и", "плюс"}:
            cursor += 1
        number_indexes: list[int] = [index]
        while cursor < len(tokens) and _safe_number_value(tokens[cursor]) is not None:
            number_indexes.append(cursor)
            cursor += 1
            if cursor < len(tokens) and tokens[cursor] in _SCALE_VALUES:
                cursor += 1
        if (
            len(number_indexes) > 1
            and cursor < len(tokens)
            and _unit(tokens[cursor]) is not None
            and any(number_index in allowed_indexes for number_index in number_indexes)
        ):
            return True
    return False


def _has_uncertain_measure_syntax(
    numeric_query: str,
    tokens: Sequence[str],
    allowed_indexes: set[int],
) -> bool:
    """Return true when a point value cannot be recovered without guessing."""

    if not allowed_indexes:
        return False
    if _NUMERIC_RANGE_RE.search(numeric_query) or _APPROXIMATE_NUMBER_RE.search(
        numeric_query
    ):
        return True

    for index in allowed_indexes:
        # Spoken-number shadows can leave the cue two or three tokens before
        # the numeric token (``около десяти 10 грамм``), so inspect the short
        # local prefix rather than only the immediately preceding token.
        prefix = tokens[max(0, index - 3):index]
        prefix_text = " ".join(prefix)
        if (
            re.search(
                r"\bпо\s+(?:крайней|меньшей)\s+мере\b",
                prefix_text,
            )
            or any(token.startswith("предел") for token in prefix)
            or "без малого" in prefix_text
            or "в районе" in prefix_text
            or "едва" in prefix
            or any(
                left == "не" and right.startswith("достиг")
                for left, right in zip(prefix, prefix[1:])
            )
            or prefix[-1:] == ["не"]
            or prefix[-2:] in (
                ["не", "ровно"],
                ["не", "равно"],
                ["точно", "не"],
            )
            or any(
                (left, right)
                in {
                    ("не", "ровно"),
                    ("не", "равно"),
                    ("точно", "не"),
                }
                for left, right in zip(prefix, prefix[1:])
            )
        ):
            return True
        if any(
            token in _UNCERTAIN_NUMBER_CUES
            or token.startswith(
                (
                    "примерн",
                    "приблизительн",
                    "ориентировочн",
                    "превыш",
                    "максимальн",
                    "минимальн",
                )
            )
            for token in prefix
        ):
            return True
        suffix = tokens[index + 1:min(len(tokens), index + 5)]
        if any(
            left == "не" and right in {"точно", "ровно", "равно"}
            for left, right in zip(suffix, suffix[1:])
        ):
            return True
        if any(
            token in {
                "около",
                "примерно",
                "приблизительно",
                "ориентировочно",
                "максимум",
                "минимум",
                "порядка",
            }
            for token in suffix
        ):
            return True
        if any(
            left == "плюс" and right == "минус"
            for left, right in zip(suffix, suffix[1:])
        ):
            return True
        if any(
            left == "с" and right.startswith("половин")
            for left, right in zip(suffix, suffix[1:])
        ):
            return True
        if any(
            left in {"и", "или", "либо"}
            and right in {"более", "выше", "меньше", "ниже"}
            for left, right in zip(suffix, suffix[1:])
        ):
            return True
        if any(
            left == "не" and right in {"более", "менее"}
            for left, right in zip(suffix, suffix[1:])
        ):
            return True
        if any(
            left == "с"
            and right.startswith(("лишн", "небольш"))
            for left, right in zip(suffix, suffix[1:])
        ):
            return True

    # A shared unit after alternative/listed values is not enough evidence to
    # decide whether either number is the actual fact of the query.
    ordered_indexes = sorted(allowed_indexes)
    for left, right in zip(ordered_indexes, ordered_indexes[1:]):
        between = tokens[left + 1:right]
        if any(token in {"или", "либо"} for token in between):
            return True
        if not between or all(
            token in {"и", "до", "по", "или", "либо"}
            for token in between
        ):
            unit_index = right + 1
            if (
                unit_index < len(tokens)
                and tokens[unit_index] in _SCALE_VALUES
            ):
                unit_index += 1
            if unit_index < len(tokens) and _unit(tokens[unit_index]) is not None:
                return True
    return False


def extract_query_measures(
    numeric_query: str,
    semantic_numbers: Iterable[str],
) -> list[QueryMeasure]:
    """Extract only typed values already classified as semantic by the query parser."""

    allowed_raw = {str(value) for value in semantic_numbers}
    allowed = {
        parsed
        for value in allowed_raw
        if (parsed := _safe_number_value(value)) is not None
    }
    if not allowed_raw:
        return []
    normalized_query = (numeric_query or "").lower().replace("ё", "е")
    tokens = _TOKEN_RE.findall(normalized_query)
    allowed_indexes = {
        index
        for index, token in enumerate(tokens)
        if re.fullmatch(_NUMBER, token)
        and _is_allowed_query_number(
            token,
            parsed=_query_number_value(tokens, index),
            allowed_raw=allowed_raw,
            allowed=allowed,
        )
    }
    if _has_compound_scaled_measure(tokens, allowed_indexes):
        return []
    if _has_uncertain_measure_syntax(
        normalized_query,
        tokens,
        allowed_indexes,
    ):
        return []

    found: list[QueryMeasure] = []
    typed_indexes: set[int] = set()
    for index, token in enumerate(tokens):
        if index not in allowed_indexes:
            continue
        prefix = (
            tokens[index - 1]
            if index > 0 and tokens[index - 1] in {"$", "%"}
            else None
        )
        next_index = index + 1
        scale_token: str | None = None
        if (
            next_index < len(tokens)
            and tokens[next_index] in _SCALE_VALUES
        ):
            scale_token = tokens[next_index]
            next_index += 1
        suffix_token = (
            tokens[next_index]
            if next_index < len(tokens)
            and _is_any_measure_unit(tokens[next_index])
            else None
        )
        suffix = (
            suffix_token
            if suffix_token and _unit(suffix_token) is not None
            else None
        )
        if suffix_token and suffix is None:
            continue
        unit = _consistent_unit(prefix, suffix)
        if unit is None:
            continue
        value = _typed_number_value(
            token,
            unit=unit,
            scale=scale_token,
            raw_unit=suffix or prefix,
        )
        if value is not None:
            typed_indexes.add(index)
            found.append(QueryMeasure(value=value, unit=unit))
    # One understood quantity cannot stand in for a mixed query whose other
    # semantic number has an unsupported or contradictory unit.
    if typed_indexes != allowed_indexes:
        return []
    unique = list(dict.fromkeys(found))
    # Multiple point values have no subject binding at query level.  They may
    # be alternatives, a range, a sum, or facts about different objects; none
    # may safely drive a deterministic interval rerank.
    return unique if len(unique) == 1 else []


def _applicability_text(card: Mapping[str, Any]) -> str:
    title = str(card.get("title") or "")
    full_text = str(card.get("full_text") or "")

    def before_sanction(text: str) -> str:
        markers = [
            marker
            for marker in (
                _SANCTION_MARKER_RE.search(text),
                _SANCTION_CLAUSE_RE.search(text),
            )
            if marker is not None
        ]
        return (
            text[:min(marker.start() for marker in markers)]
            if markers
            else text
        )

    title = before_sanction(title)
    full_text = before_sanction(full_text)
    return f"{title}\n{full_text}".lower().replace("ё", "е").replace("\xa0", " ")


def extract_applicability_intervals(
    card: Mapping[str, Any],
) -> list[ApplicabilityInterval]:
    """Read typed applicability bounds before any punishment block."""

    text = _applicability_text(card)
    # Negated exact/range starters do not describe one representable interval
    # (``не ровно 10``, ``не от 5``).  Reject the card-level deterministic
    # interpretation instead of letting the inner bare operator match.
    if (
        _UNSUPPORTED_NEGATED_BOUND_RE.search(text)
        or _UNSUPPORTED_NEGATED_RANGE_RE.search(text)
        or _DISTRIBUTED_NEGATED_BOUND_RE.search(text)
    ):
        return []
    intervals: list[ApplicabilityInterval] = []
    # Reserve every syntactic numeric range before typed parsing.  A range
    # with an unsupported/conflicting unit must never split into a seemingly
    # valid one-sided `до ...` bound in the second pass.
    occupied: list[tuple[int, int]] = [
        match.span()
        for match in _BROAD_RANGE_RE.finditer(text)
    ]
    raw_bounds = list(_BOUND_RE.finditer(text))
    for left_match, right_match in zip(raw_bounds, raw_bounds[1:]):
        left_operator = re.sub(
            r"\s+",
            " ",
            left_match.group("operator").lower(),
        ).strip()
        right_operator = re.sub(
            r"\s+",
            " ",
            right_match.group("operator").lower(),
        ).strip()
        if left_operator == "от" and right_operator == "до":
            # The dedicated range parser owns ordinary ``от N до M`` syntax.
            continue
        between = text[left_match.end():right_match.start()]
        if (
            len(between) <= 48
            and not re.search(r"[.!?\n]|\d", between)
            and bool(
                left_match.group("prefix")
                or left_match.group("suffix")
            )
            != bool(
                right_match.group("prefix")
                or right_match.group("suffix")
            )
        ):
            # A nearby pair with a unit written only once may share that unit,
            # but source-level subject binding cannot be proved.  Refuse the
            # whole deterministic interpretation instead of accepting only
            # the typed half for an arbitrary connector.
            return []
    for match in _COORDINATED_BOUND_RE.finditer(text):
        left_typed = bool(
            match.group("left_prefix") or match.group("left_suffix")
        )
        right_typed = bool(
            match.group("right_prefix") or match.group("right_suffix")
        )
        if left_typed != right_typed:
            # A shared trailing/leading unit may bind both clauses, but
            # guessing that inheritance would be unsafe. Reserve the whole
            # coordinated span so one typed half cannot become a permissive
            # standalone interval.
            occupied.append(match.span())
    for match in _RANGE_RE.finditer(text):
        # Every syntactic range owns its whole span even when its units
        # conflict.  Otherwise the bound pass below could turn a rejected
        # ``от $5 до 10 граммов`` into a misleading one-sided interval.
        occupied.append(match.span())
        unit = _consistent_unit(
            match.group("lower_prefix"),
            match.group("lower_suffix"),
            match.group("upper_prefix"),
            match.group("upper_suffix"),
        )
        if unit is None:
            continue
        if _has_conflicting_trailing_unit(
            text,
            match.end(),
            unit=unit,
        ):
            continue
        lower_raw_unit = match.group("lower_prefix") or match.group("lower_suffix")
        upper_raw_unit = match.group("upper_prefix") or match.group("upper_suffix")
        inherited_raw_unit = lower_raw_unit or upper_raw_unit
        lower_scale = match.group("lower_scale")
        upper_scale = match.group("upper_scale")
        if bool(lower_scale) != bool(upper_scale):
            if lower_scale or lower_raw_unit:
                # Shared Russian range syntax is only the forward form
                # «от 5 до 10 тысяч долларов».  Never copy a lower-end scale
                # forward, and never overwrite an explicit lower unit.
                continue
            lower_scale = upper_scale
        lower = _typed_number_value(
            match.group("lower"),
            unit=unit,
            scale=lower_scale,
            raw_unit=lower_raw_unit or inherited_raw_unit,
        )
        upper = _typed_number_value(
            match.group("upper"),
            unit=unit,
            scale=upper_scale,
            raw_unit=upper_raw_unit or inherited_raw_unit,
        )
        if lower is None or upper is None or lower > upper:
            continue
        intervals.append(
            ApplicabilityInterval(
                unit=unit,
                lower=lower,
                upper=upper,
                lower_inclusive=True,
                upper_inclusive=bool(match.group("inclusive")),
            )
        )

    def overlaps_range(span: tuple[int, int]) -> bool:
        return any(span[0] < right and span[1] > left for left, right in occupied)

    bound_candidates: list[
        tuple[tuple[int, int], ApplicabilityInterval]
    ] = []
    for match in _BOUND_RE.finditer(text):
        if overlaps_range(match.span()):
            continue
        unit = _consistent_unit(
            match.group("prefix"),
            match.group("suffix"),
        )
        if unit is None:
            continue
        if _has_conflicting_trailing_unit(
            text,
            match.end(),
            unit=unit,
        ):
            continue
        value = _typed_number_value(
            match.group("value"),
            unit=unit,
            scale=match.group("scale"),
            raw_unit=match.group("suffix") or match.group("prefix"),
        )
        if value is None:
            continue
        operator = re.sub(r"\s+", " ", match.group("operator").lower()).strip()
        if operator in {
            "до",
            "не более",
            "не выше",
            "не свыше",
        }:
            interval = ApplicabilityInterval(unit=unit, upper=value)
        elif operator in {"менее", "ниже"}:
            interval = ApplicabilityInterval(
                unit=unit,
                upper=value,
                upper_inclusive=False,
            )
        elif operator in {"свыше", "более", "выше"}:
            interval = ApplicabilityInterval(
                unit=unit,
                lower=value,
                lower_inclusive=False,
            )
        elif operator in {"от", "не менее", "не ниже"}:
            interval = ApplicabilityInterval(unit=unit, lower=value)
        else:
            interval = ApplicabilityInterval(
                unit=unit,
                lower=value,
                upper=value,
            )
        bound_candidates.append((match.span(), interval))

    # Two complementary one-sided bounds joined by an explicit conjunctive
    # connector describe one interval, not two alternative intervals:
    # ``свыше 5 граммов, но не более 20 граммов`` means (5, 20].
    # Keep the connector grammar deliberately narrow so genuine ``или``
    # alternatives remain separate union branches.
    consumed_bounds: set[int] = set()
    for index in range(len(bound_candidates) - 1):
        if index in consumed_bounds:
            continue
        left_span, left_interval = bound_candidates[index]
        right_span, right_interval = bound_candidates[index + 1]
        connector = text[left_span[1]:right_span[0]]
        if re.fullmatch(
            r"\s*[,;:]?\s*(?:но|и(?:\s+одновременно)?)\s*",
            connector,
            re.IGNORECASE,
        ) is None:
            continue
        if left_interval.unit != right_interval.unit:
            continue
        lower_parts = [
            interval
            for interval in (left_interval, right_interval)
            if interval.lower is not None and interval.upper is None
        ]
        upper_parts = [
            interval
            for interval in (left_interval, right_interval)
            if interval.upper is not None and interval.lower is None
        ]
        if len(lower_parts) != 1 or len(upper_parts) != 1:
            continue
        lower_part = lower_parts[0]
        upper_part = upper_parts[0]
        consumed_bounds.update({index, index + 1})
        if (
            lower_part.lower is None
            or upper_part.upper is None
            or lower_part.lower > upper_part.upper
            or (
                lower_part.lower == upper_part.upper
                and (
                    not lower_part.lower_inclusive
                    or not upper_part.upper_inclusive
                )
            )
        ):
            # A proven conjunction with an empty intersection is malformed;
            # fail closed instead of turning it into a permissive union.
            continue
        intervals.append(
            ApplicabilityInterval(
                unit=left_interval.unit,
                lower=lower_part.lower,
                upper=upper_part.upper,
                lower_inclusive=lower_part.lower_inclusive,
                upper_inclusive=upper_part.upper_inclusive,
            )
        )
    intervals.extend(
        interval
        for index, (_, interval) in enumerate(bound_candidates)
        if index not in consumed_bounds
    )

    for match in _AND_ABOVE_RE.finditer(text):
        unit = _consistent_unit(
            match.group("prefix"),
            match.group("suffix"),
        )
        if unit is not None:
            if _has_conflicting_trailing_unit(
                text,
                match.end(),
                unit=unit,
            ):
                continue
            value = _typed_number_value(
                match.group("value"),
                unit=unit,
                scale=match.group("scale"),
                raw_unit=match.group("suffix") or match.group("prefix"),
            )
            if value is None:
                continue
            intervals.append(
                ApplicabilityInterval(
                    unit=unit,
                    lower=value,
                )
            )
    return list(dict.fromkeys(intervals))


def applicability_verdict(
    measures: Sequence[QueryMeasure],
    intervals: Sequence[ApplicabilityInterval],
) -> str:
    """Return match/mismatch/unknown without guessing missing evidence."""

    if not measures:
        return "unknown"
    query_units = {measure.unit for measure in measures}
    source_units = {interval.unit for interval in intervals}
    if source_units - query_units:
        # A source condition on another physical dimension (for example,
        # mass plus storage duration) cannot be proven from a one-value query.
        return "unknown"
    relevant_by_measure: list[tuple[QueryMeasure, list[ApplicabilityInterval]]] = []
    for measure in measures:
        relevant = [
            interval
            for interval in intervals
            if interval.unit == measure.unit
        ]
        if not relevant:
            # A mixed query such as «10 грамм на 5 дней» must not promote an
            # article which proves only the mass part.  Partial applicability
            # evidence is unknown, not a match or a mismatch.
            return "unknown"
        if len(relevant) != 1:
            # Without subject binding, two same-unit bounds may describe
            # different facts (for example, shift length and break length).
            # A true explicit conjunction is merged by the source parser
            # above; every remaining multi-bound case is deliberately
            # unknown instead of a permissive OR.
            return "unknown"
        relevant_by_measure.append((measure, relevant))

    for measure, relevant in relevant_by_measure:
        if not any(interval.contains(measure.value) for interval in relevant):
            return "mismatch"
    return "match"


def evaluate_applicability(
    cards: Mapping[int, Mapping[str, Any]],
    *,
    numeric_query: str,
    semantic_numbers: Iterable[str],
) -> dict[int, dict[str, Any]]:
    """Build compact evidence used by the caller for a stable post-RRF sort."""

    measures = extract_query_measures(numeric_query, semantic_numbers)
    evidence: dict[int, dict[str, Any]] = {}
    for article_id, card in cards.items():
        intervals = extract_applicability_intervals(card)
        evidence[int(article_id)] = {
            "verdict": applicability_verdict(measures, intervals),
            "intervals": [
                {
                    "unit": interval.unit,
                    "lower": interval.lower,
                    "upper": interval.upper,
                    "lower_inclusive": interval.lower_inclusive,
                    "upper_inclusive": interval.upper_inclusive,
                }
                for interval in intervals
            ],
        }
    return evidence


def build_evidence_query(
    original: str,
    cleaned: str,
    extra_terms: Iterable[str] = (),
) -> str:
    """Add narrow, domain-general semantic facets for deterministic evidence matching."""

    lowered = (original or "").lower().replace("ё", "е")
    additions: list[str] = []
    if re.search(r"\b(?:что\s+такое|кто\s+такой|что\s+считается|кто\s+относится)\b", lowered):
        additions.extend(("понятие", "определение"))
    if re.search(r"\b(?:для\s+чего|зачем)\b", lowered):
        additions.extend(("цель", "предназначение"))
    if re.search(
        r"\b(?:какие?\s+полномочия|имеет\s+ли\s+право|"
        r"вправе\s+ли|кто\s+вправе)\b",
        lowered,
    ):
        additions.extend(("полномочия", "вправе", "уполномочен"))
    if re.search(r"\b(?:кто\s+контролирует|контроль)\b", lowered):
        additions.extend(("контроль", "надзор", "поднадзорен"))

    return " ".join(
        dict.fromkeys(
            [
                cleaned,
                *additions,
                *(str(term).strip() for term in extra_terms if str(term).strip()),
            ]
        )
    ).strip()


def lexical_dominance_order(
    article_ids: Sequence[int],
    evidence: Mapping[int, Mapping[str, Any]],
    protected_ids: Iterable[int] = (),
    retrieval_sources: Mapping[int, Iterable[str]] | None = None,
) -> list[int]:
    """Move only strict source supersets with no loss of direct article evidence."""

    ordered = list(dict.fromkeys(int(article_id) for article_id in article_ids))
    protected = {int(article_id) for article_id in protected_ids}
    direct = {
        article_id: frozenset(
            str(term)
            for term in evidence.get(article_id, {}).get("direct_terms", [])
        )
        for article_id in ordered
    }
    context = {
        article_id: frozenset(
            str(term)
            for term in evidence.get(article_id, {}).get("context_terms", [])
        )
        for article_id in ordered
    }
    synonym = {
        article_id: frozenset(
            str(term)
            for term in evidence.get(article_id, {}).get(
                "synonym_terms",
                [],
            )
        )
        for article_id in ordered
    }
    matched = {
        article_id: (
            direct[article_id]
            | context[article_id]
            | synonym[article_id]
        )
        for article_id in ordered
    }
    source_map = {
        int(article_id): frozenset(str(source) for source in sources)
        for article_id, sources in (retrieval_sources or {}).items()
    }
    exact_sources = frozenset(
        {
            "code",
            "title",
            "phrase_title",
            "legal_title",
            "intent_phrase",
            "synonym_exact",
        }
    )
    synonym_sources = frozenset({"synonym", "synonym_exact"})
    # Empty evidence is not a topic cohort: an incidental one-word source hit
    # must not jump over a strong vector/synonym candidate that merely uses a
    # different paraphrase. Structural context may break a tie or add a
    # qualifier only when the winner retains every direct article hit of the
    # loser; a context-only candidate can never dominate a direct source hit.
    def dominates(winner: int, loser: int) -> bool:
        winner_sources = source_map.get(winner, frozenset())
        loser_sources = source_map.get(loser, frozenset())
        if (
            loser_sources & exact_sources
            and not winner_sources & exact_sources
        ):
            return False
        if (
            loser_sources & synonym_sources
            and not winner_sources & synonym_sources
        ):
            return False
        return bool(
            matched[loser]
            and direct[winner]
            and direct[winner].issuperset(direct[loser])
            and matched[winner] > matched[loser]
        )

    # Insert a proven winner immediately before the earliest loser it
    # dominates.  This is the minimal permutation: candidates that were
    # between loser and winner retain their relative order, and the old
    # baseline loser still stays ahead of those unrelated candidates.
    result = list(ordered)
    for winner_index in range(1, len(result)):
        winner = result[winner_index]
        earliest_allowed = 0
        if winner not in protected:
            earliest_allowed = 1 + max(
                (
                    index
                    for index in range(winner_index)
                    if result[index] in protected
                ),
                default=-1,
            )
        loser_index = next(
            (
                index
                for index in range(earliest_allowed, winner_index)
                if dominates(winner, result[index])
            ),
            None,
        )
        if loser_index is None:
            continue
        result.pop(winner_index)
        result.insert(loser_index, winner)
    return result
