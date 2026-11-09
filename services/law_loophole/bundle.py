# START OF FILE services/law_loophole/bundle.py

"""
Source Selection Policy + LawContextBundle.

Единый слой, отвечающий на вопрос "какие законы пойдут в анализатор":
- auto_rag      — router-модель выбирает релевантные документы по запросу;
- full_server   — все законы выбранного сервера;
- manual_select — пользователь выбирает документы вручную;
- hybrid        — ручная выборка + расширение через router.

Все режимы возвращают одинаковую структуру LawContextBundle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable, Iterable, List, Literal, Optional, Sequence

import config
from database.manager import db_manager

logger = logging.getLogger(__name__)


SelectionMode = Literal["auto_rag", "full_server", "manual_select", "hybrid"]
ModelProfile = Literal["fast", "balanced", "deep", "expert"]
AnalysisType = Literal[
    "quick_scan",
    "deep_abuse",
    "case_analysis",
    "new_law_review",
    "full_audit",
    "devil_advocate",
]

# Разрешенные значения для валидации
_VALID_SELECTION_MODES = {"auto_rag", "full_server", "manual_select", "hybrid"}
_VALID_MODEL_PROFILES = {"fast", "balanced", "deep", "expert"}


# Тип роутер-функции: получает (user_request, [(idx, title)]) и возвращает множество выбранных индексов.
RouterFn = Callable[[str, Sequence[tuple[int, str]]], Awaitable[set]]


@dataclass
class LawContextDocument:
    """Один документ, попадающий в LawContextBundle."""
    id: int
    server_id: str
    title: str
    source_url: Optional[str]
    gemini_file_uri: str
    content_hash: Optional[str] = None
    last_updated: Optional[datetime] = None
    estimated_size: int = 0
    selection_source: Literal["manual", "rag", "full_server", "fallback"] = "rag"


@dataclass
class LawContextBundle:
    """Результат Source Selection Policy."""
    server_id: str
    project_id: Optional[str]
    selection_mode: SelectionMode
    selected_laws: List[LawContextDocument] = field(default_factory=list)
    selection_reason: str = ""
    coverage_level: str = ""
    estimated_size: int = 0
    requires_batching: bool = False
    model_profile: ModelProfile = "balanced"

    def file_uris(self) -> List[str]:
        return [d.gemini_file_uri for d in self.selected_laws if d.gemini_file_uri]

    def titles(self) -> List[str]:
        return [d.title for d in self.selected_laws]


# ---------------------------------------------------------------------------
# DB-доступ
# ---------------------------------------------------------------------------


async def list_laws_for_selection(server_id: str) -> List[LawContextDocument]:
    """
    Возвращает список законов сервера, пригодных для анализа
    (только с непустым gemini_file_uri).
    """
    if not server_id:
        raise ValueError("server_id не задан")

    rows = await db_manager.fetch(
        """
        SELECT id, server_id, source_url, title, gemini_file_uri,
               content_hash, last_updated,
               COALESCE(LENGTH(content), 0) AS content_len
        FROM law_documents
        WHERE server_id = $1 AND gemini_file_uri IS NOT NULL AND gemini_file_uri <> ''
        ORDER BY title
        """,
        server_id,
    )

    docs: List[LawContextDocument] = []
    for r in rows:
        docs.append(
            LawContextDocument(
                id=r["id"],
                server_id=r["server_id"],
                title=r["title"],
                source_url=r["source_url"],
                gemini_file_uri=r["gemini_file_uri"],
                content_hash=r["content_hash"],
                last_updated=r["last_updated"],
                estimated_size=int(r["content_len"] or 0),
            )
        )
    return docs


# ---------------------------------------------------------------------------
# Сборка LawContextBundle
# ---------------------------------------------------------------------------


def _dedup(docs: Iterable[LawContextDocument]) -> List[LawContextDocument]:
    seen = set()
    out: List[LawContextDocument] = []
    for d in docs:
        if d.id in seen:
            continue
        seen.add(d.id)
        out.append(d)
    return out


def _resolve_doc_limit(model_profile: ModelProfile) -> int:
    return {
        "fast": config.LOOPHOLE_MAX_MANUAL_DOCS_FAST,
        "balanced": config.LOOPHOLE_MAX_MANUAL_DOCS_BALANCED,
        "deep": config.LOOPHOLE_MAX_MANUAL_DOCS_DEEP,
        "expert": config.LOOPHOLE_MAX_MANUAL_DOCS_EXPERT,
    }[model_profile]


def _finalize_bundle(bundle: LawContextBundle) -> LawContextBundle:
    """Подсчет размера + флага batching. Не мутирует selected_laws."""
    bundle.selected_laws = _dedup(bundle.selected_laws)
    bundle.estimated_size = sum(d.estimated_size for d in bundle.selected_laws)
    bundle.requires_batching = bundle.estimated_size > config.LOOPHOLE_BATCH_THRESHOLD_CHARS \
        or len(bundle.selected_laws) > _resolve_doc_limit(bundle.model_profile)
    return bundle


async def build_law_context_bundle(
    *,
    server_id: str,
    project_id: Optional[str],
    selection_mode: SelectionMode,
    model_profile: ModelProfile = "balanced",
    user_request: Optional[str] = None,
    manual_law_ids: Optional[Sequence[int]] = None,
    router_fn: Optional[RouterFn] = None,
) -> LawContextBundle:
    """
    Собирает LawContextBundle для анализатора.

    Параметры:
        server_id: целевой сервер (обязательно).
        project_id: проект для контекста (опционально).
        selection_mode: один из 4 режимов.
        model_profile: профиль моделей.
        user_request: текст запроса пользователя (нужен для auto_rag/hybrid).
        manual_law_ids: список id документов для manual_select/hybrid.
        router_fn: callable, реализующий router-этап (auto_rag/hybrid).
                   Принимает (user_request, [(idx, title)]) и возвращает set индексов.

    Raises:
        ValueError: при невалидных параметрах или пустой/несовместимой выборке.
    """
    if selection_mode not in _VALID_SELECTION_MODES:
        raise ValueError(f"Неизвестный selection_mode: {selection_mode}")
    if model_profile not in _VALID_MODEL_PROFILES:
        raise ValueError(f"Неизвестный model_profile: {model_profile}")

    all_docs = await list_laws_for_selection(server_id)
    if not all_docs:
        raise ValueError(
            f"База законов сервера '{server_id}' пуста или ни один документ не имеет gemini_file_uri."
        )

    by_id = {d.id: d for d in all_docs}
    bundle = LawContextBundle(
        server_id=server_id,
        project_id=project_id,
        selection_mode=selection_mode,
        model_profile=model_profile,
    )

    if selection_mode == "full_server":
        for d in all_docs:
            d.selection_source = "full_server"
        bundle.selected_laws = list(all_docs)
        bundle.selection_reason = "Полный набор законов сервера"
        bundle.coverage_level = "full_server"

    elif selection_mode == "manual_select":
        if not manual_law_ids:
            raise ValueError("manual_select: не выбран ни один документ")
        chosen: List[LawContextDocument] = []
        for law_id in manual_law_ids:
            if law_id not in by_id:
                raise ValueError(
                    f"Документ id={law_id} не принадлежит серверу '{server_id}' "
                    "или не имеет gemini_file_uri"
                )
            doc = by_id[law_id]
            doc.selection_source = "manual"
            chosen.append(doc)

        limit = _resolve_doc_limit(model_profile)
        if len(chosen) > limit:
            raise ValueError(
                f"Превышен лимит документов для профиля '{model_profile}': "
                f"{len(chosen)} > {limit}"
            )
        bundle.selected_laws = chosen
        bundle.selection_reason = f"Ручной выбор пользователя ({len(chosen)} документов)"
        bundle.coverage_level = "manual"

    elif selection_mode == "auto_rag":
        if not user_request:
            raise ValueError("auto_rag требует user_request")
        if router_fn is None:
            raise ValueError("auto_rag требует router_fn")
        idx_pairs = [(i + 1, d.title) for i, d in enumerate(all_docs)]
        try:
            picked_indices = await router_fn(user_request, idx_pairs)
        except Exception as e:
            logger.warning("[LOOPHOLE] router_fn упал: %s", e, exc_info=True)
            picked_indices = set()

        chosen = []
        for i, d in enumerate(all_docs):
            if (i + 1) in picked_indices:
                d.selection_source = "rag"
                chosen.append(d)
        if not chosen:
            # Fallback: берем первые до лимита fast/balanced.
            limit = _resolve_doc_limit("balanced")
            for d in all_docs[:limit]:
                d.selection_source = "fallback"
                chosen.append(d)
            bundle.selection_reason = "auto_rag не выбрал документы; использован fallback"
        else:
            bundle.selection_reason = f"auto_rag: router выбрал {len(chosen)} документов"
        bundle.selected_laws = chosen
        bundle.coverage_level = "rag"

    elif selection_mode == "hybrid":
        if not manual_law_ids:
            raise ValueError("hybrid: не выбран ни один документ вручную")
        chosen = []
        manual_set = set()
        for law_id in manual_law_ids:
            if law_id not in by_id:
                raise ValueError(f"Документ id={law_id} не принадлежит серверу '{server_id}'")
            doc = by_id[law_id]
            doc.selection_source = "manual"
            chosen.append(doc)
            manual_set.add(law_id)

        added_via_rag = 0
        if router_fn is not None and user_request:
            idx_pairs = [(i + 1, d.title) for i, d in enumerate(all_docs)]
            try:
                picked_indices = await router_fn(user_request, idx_pairs)
            except Exception as e:
                logger.warning("[LOOPHOLE] hybrid router_fn упал: %s", e, exc_info=True)
                picked_indices = set()
            for i, d in enumerate(all_docs):
                if (i + 1) in picked_indices and d.id not in manual_set:
                    d.selection_source = "rag"
                    chosen.append(d)
                    added_via_rag += 1

        limit = _resolve_doc_limit(model_profile)
        if len(chosen) > limit:
            chosen = chosen[:limit]

        bundle.selected_laws = chosen
        bundle.selection_reason = (
            f"hybrid: ручной выбор ({len(manual_set)}) + router (+{added_via_rag})"
        )
        bundle.coverage_level = "hybrid"

    return _finalize_bundle(bundle)


# END OF FILE services/law_loophole/bundle.py
