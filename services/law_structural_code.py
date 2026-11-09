"""Dependency-free helpers for structural Roman numerals in law sources."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


_ROMAN_TOKEN_RE = re.compile(r"^[IVXLCM]+$", re.IGNORECASE)
_CANONICAL_ROMAN_RE = re.compile(
    r"^M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})"
    r"(?:IX|IV|V?I{0,3})$"
)
_ROMAN_LOOKALIKE_TRANSLATION = str.maketrans(
    {
        "І": "I",
        "і": "I",
        "Х": "X",
        "х": "X",
        "С": "C",
        "с": "C",
        "М": "M",
        "м": "M",
    }
)
_ROMAN_VALUES = {
    "I": 1,
    "V": 5,
    "X": 10,
    "L": 50,
    "C": 100,
    "D": 500,
    "M": 1000,
}


def normalize_roman_structural_token(value: object) -> Optional[str]:
    """Return the established canonical token, or ``None`` for non-Roman input.

    NFKC resolves Unicode Roman numeral glyphs.  Cyrillic lookalikes are
    translated explicitly.  The narrow lowercase-ASCII ``l`` repair mirrors
    the audited catalog rule: isolated ``l`` and mixed-case ``lX`` mean
    ``I``/``IX``, while genuine fully-lowercase ``lx``/``liv`` remain
    ``LX``/``LIV``.
    """

    token = unicodedata.normalize("NFKC", str(value or ""))
    token = token.translate(_ROMAN_LOOKALIKE_TRANSLATION)
    suffix = token[1:]
    if token == "l" or (
        token.startswith("l")
        and suffix
        and re.fullmatch(r"[IVXLCM]+", suffix) is not None
    ):
        token = "I" + suffix
    if _ROMAN_TOKEN_RE.fullmatch(token) is None:
        return None
    return token.upper()


def canonical_roman_value(value: object) -> Optional[int]:
    """Return an integer only for a strictly canonical Roman numeral."""

    token = normalize_roman_structural_token(value)
    if not token or _CANONICAL_ROMAN_RE.fullmatch(token) is None:
        return None

    total = 0
    for index, character in enumerate(token):
        current = _ROMAN_VALUES[character]
        following = (
            _ROMAN_VALUES[token[index + 1]]
            if index + 1 < len(token)
            else 0
        )
        total += -current if current < following else current
    return total
