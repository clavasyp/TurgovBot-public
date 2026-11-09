# START OF FILE services/qa_context.py

"""
Сборка контекста для Q&A v2 (эксперимент /qa2): ПОДБОРКА СТАТЕЙ вместо целых кодексов.

Две ноги параллельно, union, дедуп, потолок символов:
  - каталожная (law_articles, services.law_search_service.search): FTS+синонимы(+локальный
    вектор FRIDA, если включён) → карточки → полные тексты статей батчем из БД;
  - векторная (law_chunks, services.retrieval_service.retrieve): семантические семена →
    догрузка статей целиком (внутри retrieve действует свой потолок OVERLAY_CONTEXT_MAX_CHARS).

Философия: полнота важнее ранжирования — пороги похожести не применяются, лимиты широкие.
Главное, чтобы нужная статья ПОПАЛА в подборку; нерелевантное модель отбрасывает сама
(промпт это явно требует). Ранжирование влияет только на то, кто выживает при срезе
потолком (лучшие — в начале).

Используется дважды:
  - стартовая подборка по вопросу пользователя;
  - дозапросы инструмента search_laws (exclude_keys = уже переданные статьи, лимиты меньше).

Сбой любой ноги НЕ валит сборку (лог + пометка в legs) — вторая нога отдаёт что есть.
Обе пустые → None (у сервера нет ни каталога, ни векторного индекса).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import config
from database.manager import db_manager

logger = logging.getLogger(__name__)

# Ключ статьи для дедупа между ногами и раундами: (law_document_id, нормализованный ref).
ArticleKey = Tuple[int, str]

_REF_PREFIX_RE = re.compile(r'^\s*статья\s+', re.IGNORECASE)
_BARE_ARTICLE_REF_RE = re.compile(
    r'^\s*\d+(?:\s*[.-]\s*\d+)*(?:\s*\.)?(?:\s|$)',
)


def _norm_ref(ref: Optional[str], title: Optional[str] = None) -> str:
    """Нормализует article_ref к сопоставимому виду между law_articles и law_chunks:
    «Статья 12.1» и «12.1.» → «12.1». Пустой ref (преамбулы) → нормализованный титул,
    чтобы структурные блоки двух ног тоже дедупились между собой."""
    s = (ref or '').strip()
    s = _REF_PREFIX_RE.sub('', s).rstrip('.').strip().lower()
    if s:
        return s
    return (title or '').strip().lower()


def _display_ref(ref: Optional[str]) -> str:
    """Показывает ref ровно один раз.

    В production ``article_ref`` встречается как голый числовой код ``12.1``, готовый
    заголовок ``Статья 12.1``/``Глава IV`` и именованная единица ``Толкование ...``.
    Префикс нужен только первому варианту: иначе контекст получает ложные заголовки
    «Статья Статья 12.1», «Статья Глава IV» или «Статья Толкование ...».
    """
    value = (ref or '').strip()
    if not value:
        return ''
    if _BARE_ARTICLE_REF_RE.match(value):
        return f'Статья {value}'
    return value


async def _catalog_leg(server_id: str, query: str, limit: int) -> List[Dict[str, Any]]:
    """Каталожная нога: search() → карточки (results + related как бонус полноты) →
    полные тексты батчем. Порядок карточек (score) сохраняется."""
    # Ленивый импорт: не тянуть тяжёлые зависимости поиска при импорте модуля (тесты/скрипты).
    from services.law_search_service import search as law_search

    res = await law_search(server_id, query, limit=limit)
    cards = list(res.get('results') or []) + list(res.get('related') or [])
    ids: List[int] = []
    seen_ids: Set[int] = set()
    for c in cards:
        cid = c.get('id')
        if isinstance(cid, int) and cid not in seen_ids:
            seen_ids.add(cid)
            ids.append(cid)
    if not ids:
        return []

    rows = await db_manager.fetch(
        """
        SELECT id, law_document_id, article_ref, title, doc_title, penalty_summary, full_text
        FROM law_articles
        WHERE id = ANY($1::int[])
        """,
        ids,
    )
    by_id = {r['id']: dict(r) for r in rows}
    out: List[Dict[str, Any]] = []
    for aid in ids:
        row = by_id.get(aid)
        if not row or not (row.get('full_text') or '').strip():
            continue
        out.append({
            'id': row['id'],
            'key': (row['law_document_id'], _norm_ref(row.get('article_ref'), row.get('title'))),
            'doc_title': row.get('doc_title') or '',
            'article_ref': (row.get('article_ref') or '').strip(),
            'title': (row.get('title') or '').strip(),
            'penalty': (row.get('penalty_summary') or '').strip(),
            'text': row['full_text'].strip(),
        })
    return out


async def _expand_neighbors(
    server_id: str,
    catalog_items: List[Dict[str, Any]],
    radius: int,
    top_n: int,
) -> List[Dict[str, Any]]:
    """Соседние статьи документа (аналог «взять главу»): для топ-N каталожных находок —
    ±radius статей ТОГО ЖЕ документа по порядку каталога. Чинит пробелы ретривала, когда
    решающая норма стоит рядом с найденной, но лексически далека от запроса (ст. 16 ПК
    рядом со ст. 17). Порядок берём из parser-owned ``source_order``; ``id`` служит
    только детерминированным tie-breaker."""
    ids = [a['id'] for a in catalog_items[:top_n] if a.get('id')]
    if not ids or radius <= 0:
        return []
    rows = await db_manager.fetch(
        """
        WITH ordered AS (
            SELECT id, law_document_id, article_ref, title, doc_title, penalty_summary,
                   full_text,
                   row_number() OVER (
                       PARTITION BY law_document_id
                       ORDER BY source_order, id
                   ) AS rn
            FROM law_articles
            WHERE server_id = $1
        ), found AS (
            SELECT law_document_id, rn FROM ordered WHERE id = ANY($2::int[])
        )
        SELECT id, law_document_id, article_ref, title, doc_title,
               penalty_summary, full_text
        FROM (
            SELECT DISTINCT ON (o.id)
                   o.id, o.law_document_id, o.article_ref, o.title, o.doc_title,
                   o.penalty_summary, o.full_text, o.rn
            FROM ordered o
            JOIN found f ON o.law_document_id = f.law_document_id
                        AND o.rn BETWEEN f.rn - $3 AND f.rn + $3
            WHERE o.id <> ALL($2::int[])
            ORDER BY o.id
        ) neighbors
        ORDER BY law_document_id, rn, id
        """,
        server_id, ids, radius,
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not (row.get('full_text') or '').strip():
            continue
        out.append({
            'id': row['id'],
            'key': (row['law_document_id'], _norm_ref(row.get('article_ref'), row.get('title'))),
            'doc_title': row.get('doc_title') or '',
            'article_ref': (row.get('article_ref') or '').strip(),
            'title': (row.get('title') or '').strip(),
            'penalty': (row.get('penalty_summary') or '').strip(),
            'text': row['full_text'].strip(),
        })
    return out


async def _vector_leg(server_id: str, query: str, top_k: int) -> List[Dict[str, Any]]:
    """Векторная нога: retrieve() уже отдаёт статьи ЦЕЛИКОМ, отсортированные по similarity.
    На серверах без law_chunks-эмбеддингов вернёт [] — это штатно."""
    from services.retrieval_service import retrieve

    arts = await retrieve(server_id, query, top_k=top_k)
    out: List[Dict[str, Any]] = []
    for a in arts or []:
        content = (a.get('content') or '').strip()
        if not content:
            continue
        out.append({
            'key': (a.get('law_document_id'), _norm_ref(a.get('article_ref'))),
            'doc_title': (a.get('title') or '').strip(),   # в law_chunks title = титул документа
            'article_ref': (a.get('article_ref') or '').strip(),
            'title': '',
            'penalty': '',
            'text': content,
        })
    return out


async def collect_articles(
    server_id: str,
    query: str,
    *,
    search_limit: Optional[int] = None,
    vector_top_k: Optional[int] = None,
    exclude_keys: Optional[Set[ArticleKey]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Обе ноги параллельно → union с дедупом (каталожная имеет приоритет: у неё титулы
    статей и санкции). exclude_keys — статьи, уже переданные модели (раунды search_laws).

    Возвращает (статьи, статус ног {'catalog': 'ok|empty|error', 'vector': ...}).
    """
    search_limit = search_limit or config.QA_V2_SEARCH_LIMIT
    vector_top_k = vector_top_k or config.QA_V2_VECTOR_TOP_K
    exclude_keys = exclude_keys or set()

    async def _safe(name: str, coro):
        try:
            return await coro
        except Exception as e:
            # Сбой одной ноги не валит подборку: другая может дать достаточно.
            logger.warning("[QA_CTX] нога %s упала для server=%s: %s", name, server_id, e)
            return None

    catalog_res, vector_res = await asyncio.gather(
        _safe('catalog', _catalog_leg(server_id, query, search_limit)),
        _safe('vector', _vector_leg(server_id, query, vector_top_k)),
    )

    def _status(res) -> str:
        if res is None:
            return 'error'
        return 'ok' if res else 'empty'

    legs = {'catalog': _status(catalog_res), 'vector': _status(vector_res)}

    # Добор соседей документа для каталожных находок (см. _expand_neighbors). Спекулятивный
    # слой: идёт В КОНЕЦ подборки (срезается потолком первым), сбой не валит сборку.
    neighbors: List[Dict[str, Any]] = []
    radius = getattr(config, 'QA_V2_NEIGHBOR_RADIUS', 0)
    if catalog_res and radius > 0:
        try:
            neighbors = await _expand_neighbors(
                server_id, catalog_res, radius,
                getattr(config, 'QA_V2_NEIGHBOR_TOP', 12),
            )
            legs['neighbors'] = 'ok' if neighbors else 'empty'
        except Exception as e:
            logger.warning("[QA_CTX] добор соседей упал для server=%s: %s", server_id, e)
            legs['neighbors'] = 'error'

    merged: List[Dict[str, Any]] = []
    seen: Set[ArticleKey] = set(exclude_keys)
    for item in (catalog_res or []) + (vector_res or []) + neighbors:
        key = item['key']
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged, legs


def format_articles_block(
    articles: List[Dict[str, Any]],
    max_chars: Optional[int] = None,
    *,
    header: str = 'ПОДБОРКА СТАТЕЙ ЗАКОНОДАТЕЛЬСТВА СЕРВЕРА',
) -> Tuple[str, List[Dict[str, Any]], int]:
    """Собирает текстовый блок для модели с потолком символов.

    Возвращает (текст, вошедшие статьи, сколько отброшено потолком).
    Статьи нумеруются — модели удобнее ссылаться, а нам читать её ответы в логах.
    """
    max_chars = max_chars or config.QA_V2_CONTEXT_MAX_CHARS
    parts: List[str] = [
        f'=== {header} ===',
        '(найдено автоматическим поиском по вопросу; НЕрелевантные статьи отбрасывай)',
        '',
    ]
    total = sum(len(p) + 1 for p in parts)
    used: List[Dict[str, Any]] = []
    dropped = 0
    for i, art in enumerate(articles, start=1):
        head_bits = [b for b in (
            art.get('doc_title'),
            _display_ref(art.get('article_ref')),
            art.get('title'),
        ) if b]
        entry_lines = [f"[{i}] " + ' — '.join(head_bits)]
        if art.get('penalty'):
            entry_lines.append(f"Санкция: {art['penalty']}")
        entry_lines.append(art['text'])
        entry = '\n'.join(entry_lines)
        # Потолок: хвост отбрасывается целиком (лучшие статьи идут первыми).
        if used and total + len(entry) > max_chars:
            dropped = len(articles) - len(used)
            break
        total += len(entry) + 2
        used.append(art)
        parts.append(entry)
        parts.append('---')
    if dropped:
        logger.info(
            "[QA_CTX] потолок %d символов: вошло %d статей, отброшено %d",
            max_chars, len(used), dropped,
        )
    if parts and parts[-1] == '---':
        parts.pop()
    parts.append('=== КОНЕЦ ПОДБОРКИ ===')
    return '\n'.join(parts), used, dropped


async def build_qa_context(
    server_id: str,
    query: str,
    *,
    max_chars: Optional[int] = None,
    search_limit: Optional[int] = None,
    vector_top_k: Optional[int] = None,
    exclude_keys: Optional[Set[ArticleKey]] = None,
    header: str = 'ПОДБОРКА СТАТЕЙ ЗАКОНОДАТЕЛЬСТВА СЕРВЕРА',
) -> Optional[Dict[str, Any]]:
    """Полный цикл: обе ноги → union → текстовый блок с потолком.

    None — статей нет вообще (нет каталога и векторного индекса, либо всё в exclude_keys):
    вызывающий решает, что делать (стартовая сборка → честная ошибка админу;
    раунд search_laws → сообщить модели «ничего нового не найдено»).
    """
    articles, legs = await collect_articles(
        server_id, query,
        search_limit=search_limit, vector_top_k=vector_top_k, exclude_keys=exclude_keys,
    )
    if not articles:
        logger.info("[QA_CTX] пустая подборка: server=%s legs=%s q=%r", server_id, legs, query[:80])
        return None
    text, used, dropped = format_articles_block(articles, max_chars, header=header)
    return {
        'text': text,
        'articles': used,
        'keys': {a['key'] for a in used},
        'count': len(used),
        'chars': len(text),
        'dropped': dropped,
        'legs': legs,
    }

# END OF FILE services/qa_context.py
