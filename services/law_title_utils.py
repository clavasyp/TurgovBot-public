"""Лёгкие общие helpers для служебных заголовков каталога законов."""

from __future__ import annotations

import re
from typing import Optional


# Диапазон приоритета розыска и необязательная юрисдикция — метаданные, а не
# название статьи: «от ★ до ★★★★★ [Федеральная/Региональная]». Fullmatch
# намеренно не позволяет съесть содержательный текст после маркера.
RATING_METADATA_RE = re.compile(
    r'^\s*от\s+[★☆*✱]+\s+до\s+[★☆*✱]+'
    r'(?:\s*(?:\[[^\]\r\n]+\]|\([^\)\r\n]+\)))?\s*[.\-–—:]?\s*$',
    re.IGNORECASE,
)
_BRACKET_METADATA_RE = re.compile(r'^\s*(?:\[[^\]\r\n]+\]|\([^\)\r\n]+\))\s*$')
_SYMBOL_METADATA_RE = re.compile(r'^[★☆*✱•·.\-–—\s]+$')


def is_rating_metadata(value: Optional[str]) -> bool:
    return bool(RATING_METADATA_RE.fullmatch(value or ''))


def recover_rating_metadata_title(
    title: Optional[str],
    full_text: Optional[str],
) -> Optional[str]:
    """Показывает смысловую строку старой статьи, не меняя её стабильный DB id.

    Новая индексация сохраняет правильный title сразу. Этот fallback нужен для уже
    построенных каталогов: опасная полная DELETE+INSERT-пересборка сбросила бы ссылки
    на избранные статьи у пользователей Overlay.
    """
    if not is_rating_metadata(title):
        return title
    lines = (full_text or '').splitlines()
    for raw_line in lines[1:]:
        candidate = re.sub(r'\s+', ' ', raw_line.replace('\u200b', ' ')).strip()
        if (
            not candidate
            or is_rating_metadata(candidate)
            or _BRACKET_METADATA_RE.fullmatch(candidate)
            or _SYMBOL_METADATA_RE.fullmatch(candidate)
        ):
            continue
        if candidate.casefold().startswith(('наказание', 'примечание', 'исключение')):
            break
        return candidate.rstrip(' .;:') or title
    return title
