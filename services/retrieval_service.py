# START OF FILE services/retrieval_service.py

"""
Семантический ретривал релевантных норм законки (RAG) для голосового ассистента.

Эмбеддит запрос (gemini-embedding-001, task=RETRIEVAL_QUERY) и достаёт top-k ближайших
чанков из law_chunks по косинусной дистанции (pgvector <=>), с фильтром по server_id.

Два входа:
  - retrieve()       — один запрос (классический путь);
  - retrieve_multi() — несколько переформулировок от риврайтера → ОДИН батч-эмбеддинг,
    семена по каждому запросу, union статей и ЕДИНАЯ сборка с одним потолком.
Общие фазы вынесены в хелперы: _seed_search (вектор → семена SQL) и _assemble
(семена → догрузка статей целиком + role-bias + потолок + формат результата).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import config
from database.manager import db_manager
from rate_limiter import RequestPriority
from services.embeddings import embed_one, embed_texts, to_pgvector, TASK_QUERY
from services.roles import retrieval_bias_terms

logger = logging.getLogger(__name__)


async def _seed_search(server_ids: List[str], qvec: List[float], top_k: int) -> List[Dict[str, Any]]:
    """Вектор запроса → top-k чанков-«семян» по косинусной близости (SQL, фильтр по server_id).
    server_ids — список namespace: обычно один (законка сервера), для «Обзвона» GTA5RP два
    слоя (проектный + серверный) через `= ANY(...)`."""
    # Тип каста вектора (vector/halfvec) — из config (halfvec для 3072d модели -2).
    pg_type = getattr(config, "EMBEDDING_PG_TYPE", "vector")
    seeds = await db_manager.fetch(
        f"""
        SELECT law_document_id, title, article_ref, content, chunk_index,
               1 - (embedding <=> $1::{pg_type}) AS similarity
        FROM law_chunks
        WHERE server_id = ANY($2::text[])
        ORDER BY embedding <=> $1::{pg_type}
        LIMIT $3
        """,
        to_pgvector(qvec), server_ids, top_k,
    )
    return [dict(r) for r in seeds]


async def _assemble(
    server_ids: List[str],
    seeds: List[Dict[str, Any]],
    *,
    mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Семена → готовый контекст: группировка по статье (best_sim с max(similarity)),
    догрузка статей ЦЕЛИКОМ, мягкий role-bias, потолок OVERLAY_CONTEXT_MAX_CHARS (ОДИН раз)
    и формат результата как у retrieve()."""
    # Группируем «семена» по статье (law_document_id, article_ref); запоминаем лучшую similarity.
    best_sim: Dict[tuple, float] = {}
    meta: Dict[tuple, Dict[str, Any]] = {}
    for s in seeds:
        ref = (s.get("article_ref") or "").strip()
        key = (s["law_document_id"], ref)
        sim = float(s["similarity"])
        if key not in best_sim or sim > best_sim[key]:
            best_sim[key] = sim
        if key not in meta:
            meta[key] = {"title": s.get("title"), "article_ref": s.get("article_ref")}

    # Догрузка полных статей (непустой ref) одним запросом: все чанки тех же статей.
    full_by_key: Dict[tuple, str] = {}
    ne_keys = [k for k in best_sim if k[1]]
    if ne_keys:
        doc_ids = [k[0] for k in ne_keys]
        refs = [k[1] for k in ne_keys]
        rows = await db_manager.fetch(
            """
            SELECT law_document_id, COALESCE(article_ref, '') AS ref, content, chunk_index
            FROM law_chunks
            WHERE server_id = ANY($1::text[])
              AND (law_document_id, COALESCE(article_ref, '')) IN (
                    SELECT d, r FROM unnest($2::int[], $3::text[]) AS t(d, r))
            ORDER BY law_document_id, chunk_index
            """,
            server_ids, doc_ids, refs,
        )
        buckets: Dict[tuple, List[str]] = {}
        for r in rows:
            buckets.setdefault((r["law_document_id"], r["ref"]), []).append(r["content"])
        for k, parts in buckets.items():
            full_by_key[k] = "\n".join(parts).strip()

    # Пустой ref (преамбула/структурный блок) — берём сами найденные семена этой группы.
    for k in best_sim:
        if not k[1]:
            full_by_key[k] = "\n".join(
                s["content"] for s in seeds
                if s["law_document_id"] == k[0] and not (s.get("article_ref") or "").strip()
            ).strip()

    # Мягкий буст профильных документов под роль (advocate/prosecutor) — НЕ меняет фильтр server_id.
    bias = retrieval_bias_terms(mode)
    if bias:
        for key in best_sim:
            title = (meta[key].get("title") or "").lower()
            if any(t in title for t in bias):
                best_sim[key] = min(1.0, best_sim[key] + 0.05)

    # Сборка по убыванию similarity, с потолком суммарного объёма.
    results: List[Dict[str, Any]] = []
    total = 0
    max_total = getattr(config, "OVERLAY_CONTEXT_MAX_CHARS", 24000)
    keys_sorted = sorted(best_sim, key=lambda k: best_sim[k], reverse=True)
    for i, key in enumerate(keys_sorted):
        content = full_by_key.get(key, "")
        if not content:
            continue
        if results and total + len(content) > max_total:
            # Потолок: отбрасывается весь хвост — логируем срез (раньше резалось молча).
            dropped = [full_by_key.get(k, "") for k in keys_sorted[i:]]
            dropped = [c for c in dropped if c]
            logger.info(
                "[RETRIEVAL] потолок контекста %d: оставлено %d статей (%d символов), "
                "отброшено %d статей (%d символов)",
                max_total, len(results), total, len(dropped), sum(len(c) for c in dropped),
            )
            break
        total += len(content)
        results.append({
            "law_document_id": key[0],
            "title": meta[key].get("title"),
            "article_ref": meta[key].get("article_ref"),
            "content": content,
            "similarity": best_sim[key],
        })
    return results


async def retrieve(
    server_id,   # str | List[str] — один namespace законки или несколько (слои свода «Обзвона»)
    query_text: str,
    top_k: Optional[int] = None,
    *,
    mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Возвращает релевантные нормы под запрос, ДОГРУЖАЯ найденные статьи ЦЕЛИКОМ.

    Зачем: семантический поиск возвращает обрезанные чанки (~1200 символов), и длинная
    статья может прийти кусками — модель цитирует неполную норму. Поэтому:
      1) берём семантический top-k чанков-«семян»;
      2) для каждой уникальной статьи (law_document_id, article_ref) собираем ВСЕ её чанки
         и склеиваем по chunk_index → в модель уходит ПОЛНАЯ статья, а не обрывок;
      3) сортируем статьи по лучшей similarity, режем по суммарному объёму (OVERLAY_CONTEXT_MAX_CHARS).

    Каждый элемент: law_document_id, title, article_ref, content (полная статья), similarity (0..1).
    """
    top_k = top_k or config.OVERLAY_RETRIEVAL_TOP_K
    q = (query_text or "").strip()
    if not q:
        return []

    # Эмбеддинг запроса с высоким приоритетом (горячий путь, ждёт пользователь).
    qvec = await embed_one(q, task_type=TASK_QUERY, priority=RequestPriority.HIGH)
    if not qvec:
        logger.warning("[RETRIEVAL] пустой эмбеддинг запроса")
        return []

    # Один namespace (законка) или список слоёв (свод «Обзвона» GTA5RP: проект + сервер).
    server_ids = [server_id] if isinstance(server_id, str) else list(server_id)
    seeds = await _seed_search(server_ids, qvec, top_k)
    if not seeds:
        return []
    return await _assemble(server_ids, seeds, mode=mode)


async def retrieve_multi(
    server_id,   # str | List[str] — один namespace законки или несколько (слои свода «Обзвона»)
    queries: List[str],
    top_k_per_query: Optional[int] = None,
    *,
    mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Мульти-ретривал: несколько переформулировок одного запроса → ЕДИНЫЙ контекст.

    Зачем: риврайтер разбивает «грязную» голосовую сцену на 2-4 чётких поисковых запроса —
    каждый ловит свою грань ситуации (квалификация деяния, процедура, права). Механика:
      1) эмбеддинг ВСЕХ запросов ОДНИМ батч-вызовом (embed_texts; путь -001 батчует до 100,
         для rate_limiter это один запрос);
      2) семена по каждому вектору (LIMIT top_k_per_query, дефолт OVERLAY_REWRITE_TOPK_PER_QUERY);
      3) union семян по статье (law_document_id, article_ref) с max(similarity) — внутри
         единой сборки _assemble: догрузка статей целиком, role-bias и потолок
         OVERLAY_CONTEXT_MAX_CHARS применяются ОДИН раз к объединённому набору
         (а не по разу на запрос — иначе потолок умножался бы на число запросов).

    Формат элементов результата — ровно как у retrieve().
    """
    top_k = top_k_per_query or config.OVERLAY_REWRITE_TOPK_PER_QUERY
    qs = [q for q in ((q or "").strip() for q in (queries or [])) if q]
    if not qs:
        return []

    # ОДИН батч-эмбеддинг всех запросов (горячий путь, ждёт пользователь).
    vecs = await embed_texts(qs, task_type=TASK_QUERY, priority=RequestPriority.HIGH)
    vecs = [v for v in (vecs or []) if v]
    if not vecs:
        logger.warning("[RETRIEVAL] пустой батч-эмбеддинг запросов (%d шт.)", len(qs))
        return []

    # Семена по каждому вектору; параллельно — пул соединений БД это позволяет.
    server_ids = [server_id] if isinstance(server_id, str) else list(server_id)
    seed_lists = await asyncio.gather(*[_seed_search(server_ids, v, top_k) for v in vecs])

    # Дедуп общих чанков разных запросов по (law_document_id, article_ref, chunk_index)
    # с max(similarity) — иначе чанк-«семя», найденный двумя запросами, задвоил бы текст
    # преамбулы в _assemble. Union по СТАТЬЕ с max(similarity) дальше делает сама _assemble.
    merged: Dict[tuple, Dict[str, Any]] = {}
    for s in (s for lst in seed_lists for s in lst):
        key = (s["law_document_id"], (s.get("article_ref") or "").strip(), s.get("chunk_index"))
        prev = merged.get(key)
        if prev is None or float(s["similarity"]) > float(prev["similarity"]):
            merged[key] = s
    if not merged:
        return []
    return await _assemble(server_ids, list(merged.values()), mode=mode)


def format_chunks_for_prompt(chunks: List[Dict[str, Any]]) -> str:
    """Готовит найденные чанки в текст для подачи модели (с пометкой источника/статьи)."""
    parts: List[str] = []
    for ch in chunks:
        header = ch.get("article_ref") or ch.get("title") or "Норма"
        src = ch.get("title") or ""
        parts.append(f"[{header}{(' — ' + src) if src and src != header else ''}]\n{ch.get('content', '')}")
    return "\n\n---\n\n".join(parts)


# END OF FILE services/retrieval_service.py
