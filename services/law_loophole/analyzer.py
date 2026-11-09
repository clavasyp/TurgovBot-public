# START OF FILE services/law_loophole/analyzer.py

"""
Loophole Analyzer (v2-конвейер).

Пайплайн зависит от профиля (config.LOOPHOLE_FULL_PIPELINE_PROFILES):
- fast/balanced (лёгкий): single-doc + cross-doc -> validate -> python-дедуп/sort.
- deep/expert (полный v2): + S1 norm-extraction -> S2 goal-directed roles ->
  S3 exploit-chain -> S4 red-team -> S5 LLM-синтез (поверх single+cross).

thinking_level задаётся по стадиям (config.LOOPHOLE_THINKING_*; google-genai>=1.51).
Парсинг JSON устойчив к мусору вокруг блока. Находка без source_documents/source_fragments
отбрасывается. Стадии S1-S5 — best-effort: их сбой не роняет базовый single+cross.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

import config

from api.prompts.law_loopholes import (
    LAW_LOOPHOLE_SYSTEM_PROMPT,
    get_loophole_router_prompt,
    get_single_document_loophole_prompt,
    get_cross_document_contradiction_prompt,
    get_loophole_synthesis_prompt,
    get_norm_extraction_prompt,
    get_goal_directed_role_prompt,
    get_exploit_chain_prompt,
    get_red_team_prompt,
)
from .bundle import LawContextBundle, LawContextDocument, AnalysisType
from .model_policy import LoopholeModels

logger = logging.getLogger(__name__)


# Тип callable, выполняющего запрос к Gemini.
# Принимает: model_name, system_prompt, content_parts (см. ниже), text_prompt.
# Возвращает: текст ответа (str).
# content_parts — список dict вида {"file_uri": str, "mime_type": str}, может быть пустым.
ModelCall = Callable[..., Awaitable[str]]


class LoopholeModelUnavailable(Exception):
    """Модельные вызовы анализа провалились из-за временной недоступности (квота 429/
    RESOURCE_EXHAUSTED). Результат недостоверен — НЕ выдаём пустой отчёт и НЕ кэшируем;
    задачу следует молча повторить позже (квота освободится). См. обработку в queue._execute_job."""


def _is_quota_error(exc: BaseException) -> bool:
    """Похоже ли исключение на временную исчерпанность квоты Vertex (429)."""
    s = str(exc)
    return "429" in s or "RESOURCE_EXHAUSTED" in s


_VALID_VULN_TYPES = {
    "direct_contradiction",
    "regulatory_gap",
    "narrow_wording",
    "vague_term",
    "missing_procedure",
    "missing_sanction",
    "sanction_without_offense",
    "authority_collision",
    "hierarchy_unclear",
    "missing_deadline",
    "overbroad_exception",
    "missing_subject",
    "missing_object",
    "double_punishment",
    "proof_impossibility",
    "procedure_conflict",
    "deadline_conflict",
    "sanction_conflict",
    "rights_vs_power_conflict",
    "exploit_chain",
    "other",
}

_VALID_SEVERITY = ("low", "medium", "high", "critical")
_VALID_CONFIDENCE = ("low", "medium", "high")
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ---------------------------------------------------------------------------
# JSON-парсинг и валидация
# ---------------------------------------------------------------------------


def parse_json_response(raw_text: str) -> Optional[Any]:
    """
    Парсит JSON из ответа модели, устойчиво к лишнему тексту и markdown-обёрткам.
    Возвращает разобранный объект или None при неудаче.
    """
    if not raw_text:
        return None
    text = raw_text.strip()

    # Снимаем тройные бэктики ```json ... ```
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()

    # Прямая попытка
    try:
        return json.loads(text)
    except Exception:
        pass

    # Ищем самый "внешний" JSON-объект/массив по балансу скобок.
    candidate = _extract_balanced_json(text)
    if candidate:
        try:
            return json.loads(candidate)
        except Exception:
            return None
    return None


def _extract_balanced_json(text: str) -> Optional[str]:
    """Ищет первую сбалансированную пару фигурных или квадратных скобок."""
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def _normalize_severity(value: Any) -> str:
    if isinstance(value, str) and value.lower() in _VALID_SEVERITY:
        return value.lower()
    return "low"


def _normalize_confidence(value: Any) -> str:
    if isinstance(value, str) and value.lower() in _VALID_CONFIDENCE:
        return value.lower()
    return "low"


def _normalize_vuln_type(value: Any) -> str:
    if isinstance(value, str) and value.lower() in _VALID_VULN_TYPES:
        return value.lower()
    return "other"


def _coerce_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def validate_findings(
    findings: List[Dict[str, Any]],
    *,
    allowed_titles: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Нормализует список finding-словарей.
    - Отбрасывает находки без source_documents и source_fragments.
    - Помечает confidence=low, если хоть один из источников отсутствует.
    - Если задан allowed_titles, удаляет несуществующие источники.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(findings, list):
        return out

    for raw in findings:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "")).strip()
        if not title:
            continue

        src_docs = _coerce_str_list(raw.get("source_documents"))
        src_frags = _coerce_str_list(raw.get("source_fragments"))

        if allowed_titles is not None:
            src_docs = [d for d in src_docs if d in allowed_titles]

        if not src_docs or not src_frags:
            # Источника нет — находка не годится для финального отчета.
            continue

        confidence = _normalize_confidence(raw.get("confidence"))
        if not src_frags or not src_docs:
            confidence = "low"

        out.append(
            {
                "title": title,
                "vulnerability_type": _normalize_vuln_type(raw.get("vulnerability_type")),
                "source_documents": src_docs,
                "source_fragments": src_frags,
                "issue_summary": str(raw.get("issue_summary", "")).strip(),
                "abuse_scenarios": _coerce_str_list(raw.get("abuse_scenarios")),
                "affected_roles": _coerce_str_list(raw.get("affected_roles")),
                "gameplay_impact": str(raw.get("gameplay_impact", "")).strip(),
                "severity": _normalize_severity(raw.get("severity")),
                "confidence": confidence,
                "fix_recommendation": str(raw.get("fix_recommendation", "")).strip(),
            }
        )
    return out


def _dedup_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Объединяет дубликаты по (title, vulnerability_type)."""
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for f in findings:
        key = (f["title"].lower(), f["vulnerability_type"])
        if key in by_key:
            existing = by_key[key]
            for list_field in ("source_documents", "source_fragments", "abuse_scenarios", "affected_roles"):
                merged = list(dict.fromkeys(existing[list_field] + f[list_field]))
                existing[list_field] = merged
            # Поднимаем severity до максимального.
            if _SEVERITY_RANK[f["severity"]] < _SEVERITY_RANK[existing["severity"]]:
                existing["severity"] = f["severity"]
        else:
            by_key[key] = dict(f)
    return list(by_key.values())


def _sort_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(findings, key=lambda f: _SEVERITY_RANK.get(f["severity"], 99))


def build_summary(findings: List[Dict[str, Any]], coverage_note: str = "") -> Dict[str, Any]:
    counts = {s: 0 for s in _VALID_SEVERITY}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    main_risks = [f["title"] for f in findings if f["severity"] in ("critical", "high")][:5]
    return {
        "total_findings": len(findings),
        "critical_count": counts["critical"],
        "high_count": counts["high"],
        "medium_count": counts["medium"],
        "low_count": counts["low"],
        "main_risks": main_risks,
        "coverage_note": coverage_note,
    }


# ---------------------------------------------------------------------------
# Главный пайплайн
# ---------------------------------------------------------------------------


async def analyze_loopholes(
    *,
    bundle: LawContextBundle,
    analysis_type: AnalysisType,
    models: LoopholeModels,
    model_call: ModelCall,
) -> Dict[str, Any]:
    """
    Запускает пайплайн анализа.

    Args:
        bundle: собранный LawContextBundle (>=1 документ).
        analysis_type: тип анализа.
        models: профиль с именами моделей.
        model_call: callable, который реально дергает Gemini.
                    Сигнатура: await model_call(
                        model_name=str,
                        system_prompt=str,
                        text_prompt=str,
                        file_uris=list[str] | None,
                    ) -> str
                    Это инжектится извне, чтобы analyzer оставался тестируемым.

    Returns:
        dict вида {"summary": {...}, "findings": [...], "discarded_findings": [...]}
    """
    if not bundle.selected_laws:
        raise ValueError("LawContextBundle пуст: нет документов для анализа")

    allowed_titles = {d.title for d in bundle.selected_laws}

    # 1) Pass per-document. ВАЖНО: ограничиваем одновременные модельные вызовы семафором —
    #    раньше gather запускал ВСЕ документы разом (33–39), что насыщало квоту Vertex и
    #    давало шквал 429. Теперь не более LOOPHOLE_PASS1_CONCURRENCY в полёте → rate_limiter
    #    успевает пейсить под QPM модели.
    _sem = asyncio.Semaphore(max(1, int(getattr(config, "LOOPHOLE_PASS1_CONCURRENCY", 3))))
    analyzer_thinking = _loophole_thinking("analyzer")

    async def _guarded_single(doc: LawContextDocument) -> List[Dict[str, Any]]:
        async with _sem:
            return await _analyze_single_document(
                doc=doc,
                analysis_type=analysis_type,
                analyzer_model=models.analyzer_model,
                model_call=model_call,
                thinking_level=analyzer_thinking,
            )

    per_doc_findings: List[Dict[str, Any]] = []
    pass1_results = await asyncio.gather(
        *[_guarded_single(doc) for doc in bundle.selected_laws],
        return_exceptions=True,
    )
    # Считаем сбои отдельно от честного «находок нет»: квотный сбой (429) НЕ должен
    # выдаваться юзеру как пустой результат — это сигнал на молчаливый повтор.
    failures: List[BaseException] = []
    for res in pass1_results:
        if isinstance(res, BaseException):
            logger.warning("[LOOPHOLE] pass1 ошибка: %s", res)
            failures.append(res)
            continue
        per_doc_findings.extend(res or [])

    # S1 norm-extraction (Этап 5): только на профилях полного конвейера v2 (Этап 11
    # включит deep/expert). Даёт cross-doc «ручки для абуза» по документам. Извлечение
    # вспомогательное — его сбой не роняет анализ (_extract_all_norms глотает ошибки по-документно).
    norms_context: Optional[str] = None
    chain_findings: List[Dict[str, Any]] = []
    if bundle.model_profile in getattr(config, "LOOPHOLE_FULL_PIPELINE_PROFILES", ()):
        norms_by_doc = await _extract_all_norms(
            bundle=bundle,
            extraction_model=models.analyzer_model,
            model_call=model_call,
            thinking_level=_loophole_thinking("extraction"),
        )
        norms_context = format_norms_context(norms_by_doc) or None
        # S2 goal-roles → S3 exploit-chains (best-effort: их сбой НЕ роняет анализ —
        # это надстройка над базовым single+cross).
        if norms_context:
            try:
                role_plans = await _run_goal_directed_roles(
                    norms_context=norms_context,
                    role_model=models.analyzer_model,
                    model_call=model_call,
                    thinking_level=_loophole_thinking("roles"),
                )
                if role_plans:
                    chain_findings = await _build_exploit_chains(
                        role_plans_json=format_role_plans(role_plans),
                        norms_context=norms_context,
                        chain_model=models.analyzer_model,
                        model_call=model_call,
                        thinking_level=_loophole_thinking("chain"),
                    )
                    # S4 red-team: разваливаем собранные цепочки, режем галлюцинации.
                    if chain_findings:
                        chain_findings = await _red_team_findings(
                            findings=chain_findings,
                            redteam_model=models.analyzer_model,
                            model_call=model_call,
                            thinking_level=_loophole_thinking("redteam"),
                        )
            except Exception as e:
                logger.warning("[LOOPHOLE] roles/chains ошибка: %s", e)
                chain_findings = []

    # 2) Cross-document pass — пропускаем, если у нас один документ.
    cross_findings: List[Dict[str, Any]] = []
    cross_failure: Optional[BaseException] = None
    if len(bundle.selected_laws) > 1:
        try:
            cross_findings = await _analyze_cross_documents(
                bundle=bundle,
                analysis_type=analysis_type,
                analyzer_model=models.analyzer_model,
                model_call=model_call,
                thinking_level=analyzer_thinking,
                norms_context=norms_context,
            )
        except Exception as e:
            logger.warning("[LOOPHOLE] cross-pass ошибка: %s", e, exc_info=True)
            cross_failure = e

    # Решение по сбоям модельных вызовов. Честное «находок нет» (без сбоев) проходит дальше
    # как валидный результат. Но если хоть один вызов упал по КВОТЕ (429) — отчёт неполный и
    # недостоверный: НЕ выдаём пустой результат и НЕ кэшируем, а сигналим на молчаливый повтор
    # (квота временная). НЕ-квотные сбои — настоящая ошибка пайплайна, пробрасываем как есть.
    all_failures: List[BaseException] = list(failures)
    if cross_failure is not None:
        all_failures.append(cross_failure)
    if all_failures:
        quota_failures = [f for f in all_failures if _is_quota_error(f)]
        if quota_failures:
            raise LoopholeModelUnavailable(
                f"{len(quota_failures)} model call(s) hit quota 429 "
                f"(docs={len(bundle.selected_laws)}) — retry later, no partial result emitted"
            )
        raise all_failures[0]

    raw = per_doc_findings + cross_findings + chain_findings
    validated = validate_findings(raw, allowed_titles=allowed_titles)
    discarded = [
        {"title": str(r.get("title", "")), "reason": "missing source_documents or source_fragments"}
        for r in raw
        if isinstance(r, dict)
        and (not _coerce_str_list(r.get("source_documents")) or not _coerce_str_list(r.get("source_fragments")))
    ]

    deduped = _sort_findings(_dedup_findings(validated))

    # S5 LLM-синтез (Этап 9): только полный конвейер + есть что синтезировать. Склеивает
    # одиночные находки в цепочки и даёт executive summary. Best-effort: сбой → python-дедуп.
    if bundle.model_profile in getattr(config, "LOOPHOLE_FULL_PIPELINE_PROFILES", ()) and deduped:
        try:
            syn = await _llm_synthesize(
                findings=deduped,
                analysis_type=analysis_type,
                synthesis_model=models.synthesis_model,
                model_call=model_call,
                thinking_level=_loophole_thinking("synthesis"),
                allowed_titles=allowed_titles,
            )
            if syn is not None:
                deduped = syn["findings"]
                discarded = discarded + syn["discarded"]
        except Exception as e:
            logger.warning("[LOOPHOLE] LLM-синтез ошибка: %s", e)

    coverage_note = (
        f"selection_mode={bundle.selection_mode}, "
        f"docs={len(bundle.selected_laws)}, "
        f"profile={bundle.model_profile}"
    )
    summary = build_summary(deduped, coverage_note=coverage_note)

    return {
        "summary": summary,
        "findings": deduped,
        "discarded_findings": discarded,
    }


def _loophole_thinking(stage: str) -> Optional[str]:
    """thinking_level для стадии анализатора из config; None если выключено/пусто.
    stage ∈ {router, analyzer, extraction, synthesis, chain, redteam}."""
    if not getattr(config, "LOOPHOLE_THINKING_ENABLED", True):
        return None
    key = {
        "router": "LOOPHOLE_THINKING_ROUTER",
        "analyzer": "LOOPHOLE_THINKING_ANALYZER",
        "extraction": "LOOPHOLE_THINKING_EXTRACTION",
        "roles": "LOOPHOLE_THINKING_ROLES",
        "synthesis": "LOOPHOLE_THINKING_SYNTHESIS",
        "chain": "LOOPHOLE_THINKING_CHAIN",
        "redteam": "LOOPHOLE_THINKING_REDTEAM",
    }.get(stage)
    val = getattr(config, key, None) if key else None
    val = str(val).strip().lower() if val else ""
    return val or None


def _make_thinking_config(types: Any, level: Optional[str]):
    """types.ThinkingConfig(thinking_level=...) или None. types передаётся, т.к.
    google.genai импортируется лениво внутри make_vertex_model_call."""
    if not level:
        return None
    mapping = {
        "minimal": types.ThinkingLevel.MINIMAL,
        "low": types.ThinkingLevel.LOW,
        "medium": types.ThinkingLevel.MEDIUM,
        "high": types.ThinkingLevel.HIGH,
    }
    lv = mapping.get(str(level).strip().lower())
    if lv is None:
        return None
    return types.ThinkingConfig(thinking_level=lv)


# === S1: извлечение норм (ENGINE v2) ===========================================
# Из каждого документа достаём структуру норм + «ручки для абуза» (handles): что НЕ
# определено, какой термин резиновый, где нет срока/процедуры/состава, на чьём слове
# держится доказывание, куда уходит жалоба. Это заготовки, из которых cross-doc /
# goal-roles / exploit-chain собирают многоходовые цепочки.
# На Этапе 4 функции ОПРЕДЕЛЕНЫ, но НЕ подключены в analyze_loopholes (wiring — Этапы 5/11).

async def _extract_norms_for_document(
    *,
    doc: LawContextDocument,
    extraction_model: str,
    model_call: ModelCall,
    thinking_level: Optional[str] = None,
) -> Dict[str, Any]:
    """S1 для одного документа: get_norm_extraction_prompt -> {document_title, norms, missing_structure_notes}."""
    prompt = get_norm_extraction_prompt(doc.title)
    raw = await model_call(
        model_name=extraction_model,
        system_prompt=LAW_LOOPHOLE_SYSTEM_PROMPT,
        text_prompt=prompt,
        file_uris=[doc.gemini_file_uri],
        thinking_level=thinking_level,
    )
    parsed = parse_json_response(raw)
    norms_raw = parsed.get("norms") if isinstance(parsed, dict) else None
    norms = [n for n in norms_raw if isinstance(n, dict)] if isinstance(norms_raw, list) else []
    notes = _coerce_str_list(parsed.get("missing_structure_notes")) if isinstance(parsed, dict) else []
    return {"document_title": doc.title, "norms": norms, "missing_structure_notes": notes}


async def _extract_all_norms(
    *,
    bundle: LawContextBundle,
    extraction_model: str,
    model_call: ModelCall,
    thinking_level: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """S1 параллельно по всем документам (семафор LOOPHOLE_PASS1_CONCURRENCY).
    Извлечение ВСПОМОГАТЕЛЬНОЕ: сбойный документ просто пропускается (без норм) —
    в отличие от pass1, квотный сбой здесь НЕ роняет весь анализ (downstream работает
    и на file-Part'ах документов). Документы без норм в результат не кладём."""
    sem = asyncio.Semaphore(max(1, int(getattr(config, "LOOPHOLE_PASS1_CONCURRENCY", 3))))

    async def _guard(doc: LawContextDocument) -> Dict[str, Any]:
        async with sem:
            return await _extract_norms_for_document(
                doc=doc,
                extraction_model=extraction_model,
                model_call=model_call,
                thinking_level=thinking_level,
            )

    results = await asyncio.gather(
        *[_guard(d) for d in bundle.selected_laws], return_exceptions=True
    )
    out: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, BaseException):
            logger.warning("[LOOPHOLE] norm-extraction ошибка: %s", r)
            continue
        if isinstance(r, dict) and r.get("norms"):
            out.append(r)
    return out


def format_norms_context(
    norms_by_doc: List[Dict[str, Any]],
    *,
    max_norms_per_doc: int = 40,
    max_chars: int = 60_000,
) -> str:
    """Текстовый скаффолдинг из извлечённых норм для cross-doc / goal-roles / exploit-chain:
    по документам — тип нормы, дословный фрагмент-источник и «ручки для абуза»."""
    lines: List[str] = []
    for entry in norms_by_doc or []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("document_title") or "?")
        lines.append(f"## {title}")
        for n in (entry.get("norms") or [])[:max_norms_per_doc]:
            if not isinstance(n, dict):
                continue
            nid = str(n.get("norm_id") or "").strip()
            typ = str(n.get("norm_type") or "").strip()
            frag = str(n.get("source_fragment") or "").strip()
            handles = _coerce_str_list(n.get("handles"))
            head = f"- [{typ}]" + (f" {nid}" if nid else "")
            if frag:
                head += f": {frag[:300]}"
            lines.append(head)
            if handles:
                lines.append(f"    ручки: {('; '.join(handles))[:300]}")
        notes = _coerce_str_list(entry.get("missing_structure_notes"))
        if notes:
            lines.append(f"  (пробелы структуры: {('; '.join(notes))[:200]})")
    return "\n".join(lines)[:max_chars]


# === S2: goal-directed role engine (ENGINE v2) =================================
# Не post-hoc оценка готовой находки, а ГЕНЕРАТОР: даём актору цель + извлечённые
# нормы (S1), и он отыгрывает ПУТЬ к цели (means-ends, обратный ход). Планы — вход
# для exploit-chain (Этап 7). На Этапе 6 функция ОПРЕДЕЛЕНА, но НЕ подключена.

# Набор целей акторов (RP-сценарные, внутриигровые). Тюнится; можно генерировать из
# норм с handles (research), но MVP — фиксированный набор архетипов абуза.
_ROLE_GOALS: List[Dict[str, str]] = [
    {"actor": "силовик-абузер", "goal": "легально изъять бизнес/имущество конкурента и не вернуть назад"},
    {"actor": "силовик-абузер", "goal": "задержать неугодного игрока и держать максимально долго без последствий для себя"},
    {"actor": "прокурор", "goal": "натянуть состав на конкретного игрока через резиновую формулировку"},
    {"actor": "адвокат", "goal": "развалить готовое дело/задержание/изъятие через процедурную дыру или пропущенный срок"},
    {"actor": "игрок", "goal": "спровоцировать конфликт и уйти от наказания, спрятавшись за слабость закона"},
]


async def _run_goal_directed_roles(
    *,
    norms_context: str,
    role_model: str,
    model_call: ModelCall,
    thinking_level: Optional[str] = None,
    goals: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """S2: для набора целей акторов отыгрывает путь к цели по нормам S1
    (get_goal_directed_role_prompt). Возвращает список планов
    {actor, goal, plan[], expected_result, reversibility, survives_appeal, ...}.
    Работает ТОЛЬКО по извлечённым нормам (norms_context), без file-Part'ов."""
    goals = goals or _ROLE_GOALS
    if not norms_context:
        return []
    sem = asyncio.Semaphore(max(1, int(getattr(config, "LOOPHOLE_PASS1_CONCURRENCY", 3))))

    async def _one(actor: str, goal: str) -> Optional[Dict[str, Any]]:
        prompt = get_goal_directed_role_prompt(actor, goal, norms_context)
        async with sem:
            raw = await model_call(
                model_name=role_model,
                system_prompt=LAW_LOOPHOLE_SYSTEM_PROMPT,
                text_prompt=prompt,
                file_uris=None,
                thinking_level=thinking_level,
            )
        parsed = parse_json_response(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("plan"), list) and parsed["plan"]:
            parsed.setdefault("actor", actor)
            parsed.setdefault("goal", goal)
            return parsed
        return None

    results = await asyncio.gather(
        *[_one(g["actor"], g["goal"]) for g in goals], return_exceptions=True
    )
    out: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, BaseException):
            logger.warning("[LOOPHOLE] goal-role ошибка: %s", r)
            continue
        if r:
            out.append(r)
    return out


def format_role_plans(plans: List[Dict[str, Any]], *, max_chars: int = 40_000) -> str:
    """Сериализует планы ролей в компактный JSON-текст — вход для exploit-chain (Этап 7)."""
    try:
        s = json.dumps(plans or [], ensure_ascii=False)
    except Exception:
        s = "[]"
    return s[:max_chars]


# === S3: exploit-chain builder (ENGINE v2) =====================================
# Из планов ролей (S2) + норм (S1) собирает многоходовые ЦЕПОЧКИ и приводит к
# finding-схеме (совместимой с validate_findings: цепочка = одна находка с несколькими
# source_documents/fragments, шаги — в abuse_scenarios). Лишние поля (links/reversibility/
# survives_appeal) validate_findings срежет — суть цепочки остаётся в abuse_scenarios.

async def _build_exploit_chains(
    *,
    role_plans_json: str,
    norms_context: str,
    chain_model: str,
    model_call: ModelCall,
    thinking_level: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """S3: get_exploit_chain_prompt -> {chains[], discarded[]}; возвращает chains
    (finding-совместимые dict'ы). Пустой вход → []."""
    if not role_plans_json:
        return []
    prompt = get_exploit_chain_prompt(role_plans_json, norms_context or "")
    raw = await model_call(
        model_name=chain_model,
        system_prompt=LAW_LOOPHOLE_SYSTEM_PROMPT,
        text_prompt=prompt,
        file_uris=None,
        thinking_level=thinking_level,
    )
    parsed = parse_json_response(raw)
    chains = parsed.get("chains") if isinstance(parsed, dict) else None
    return [c for c in chains if isinstance(c, dict)] if isinstance(chains, list) else []


# === S4: red-team / self-critique (ENGINE v2) ==================================
# «Адвокатский» проход поверх готовых схем: пытается их РАЗВАЛИТЬ (модель меняет цель —
# теперь хочет, чтобы схема НЕ работала). Корректирует severity/confidence, выкидывает
# развалённые. Лучший анти-галлюцинационный фильтр. Сбой/непарсинг → вход не трогаем.

async def _red_team_findings(
    *,
    findings: List[Dict[str, Any]],
    redteam_model: str,
    model_call: ModelCall,
    thinking_level: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """S4: get_red_team_prompt -> {verdicts[]}; матчит по title, применяет
    adjusted_severity/confidence, выкидывает keep=false / holds_up=no.
    Нет вердикта на находку → оставляем как есть; вердикты не разобрались → вход без изменений."""
    if not findings:
        return findings
    payload = json.dumps(findings, ensure_ascii=False)[:60_000]
    raw = await model_call(
        model_name=redteam_model,
        system_prompt=LAW_LOOPHOLE_SYSTEM_PROMPT,
        text_prompt=get_red_team_prompt(payload),
        file_uris=None,
        thinking_level=thinking_level,
    )
    parsed = parse_json_response(raw)
    verdicts = parsed.get("verdicts") if isinstance(parsed, dict) else None
    if not isinstance(verdicts, list):
        return findings
    vmap: Dict[str, Dict[str, Any]] = {}
    for v in verdicts:
        if isinstance(v, dict) and v.get("title"):
            vmap[str(v["title"]).strip().lower()] = v
    out: List[Dict[str, Any]] = []
    for f in findings:
        v = vmap.get(str(f.get("title", "")).strip().lower())
        if v is None:
            out.append(f)
            continue
        if v.get("keep") is False or str(v.get("holds_up", "")).strip().lower() == "no":
            continue
        nf = dict(f)
        sev = str(v.get("adjusted_severity", "")).strip().lower()
        if sev in _VALID_SEVERITY:
            nf["severity"] = sev
        conf = str(v.get("adjusted_confidence", "")).strip().lower()
        if conf in _VALID_CONFIDENCE:
            nf["confidence"] = conf
        out.append(nf)
    return out


# === S5: LLM-синтез (ENGINE v2, был мёртвым кодом) =============================
# Раньше get_loophole_synthesis_prompt импортировался, но НЕ вызывался («синтез» =
# python-дедуп). Теперь подключён: модель СКЛЕИВАЕТ одиночные находки в цепочки, если
# они ведут к одной цели актора, и даёт executive summary. Результат ре-валидируется.
# Best-effort: сбой/пустой/непарсинг → возвращаем None (остаётся python-дедуп).

async def _llm_synthesize(
    *,
    findings: List[Dict[str, Any]],
    analysis_type: AnalysisType,
    synthesis_model: str,
    model_call: ModelCall,
    thinking_level: Optional[str] = None,
    allowed_titles: Optional[Set[str]] = None,
) -> Optional[Dict[str, Any]]:
    """S5: get_loophole_synthesis_prompt -> {summary, findings, discarded_findings}.
    Возвращает {findings: validated+dedup+sort, discarded: [...]} или None при сбое."""
    if not findings:
        return None
    payload = json.dumps(findings, ensure_ascii=False)[:120_000]
    raw = await model_call(
        model_name=synthesis_model,
        system_prompt=LAW_LOOPHOLE_SYSTEM_PROMPT,
        text_prompt=get_loophole_synthesis_prompt(payload, analysis_type),
        file_uris=None,
        thinking_level=thinking_level,
    )
    parsed = parse_json_response(raw)
    if not isinstance(parsed, dict):
        return None
    syn_findings = parsed.get("findings")
    if not isinstance(syn_findings, list):
        return None
    validated = validate_findings(syn_findings, allowed_titles=allowed_titles)
    if not validated:
        # синтез всё выкинул/испортил источники — не доверяем, оставляем python-дедуп.
        return None
    discarded: List[Dict[str, Any]] = []
    for d in (parsed.get("discarded_findings") or []):
        if isinstance(d, dict) and d.get("title"):
            discarded.append({"title": str(d.get("title", "")), "reason": str(d.get("reason", ""))})
    return {"findings": _sort_findings(_dedup_findings(validated)), "discarded": discarded}


async def _analyze_single_document(
    *,
    doc: LawContextDocument,
    analysis_type: AnalysisType,
    analyzer_model: str,
    model_call: ModelCall,
    thinking_level: Optional[str] = None,
) -> List[Dict[str, Any]]:
    prompt = get_single_document_loophole_prompt(doc.title, analysis_type)
    raw_text = await model_call(
        model_name=analyzer_model,
        system_prompt=LAW_LOOPHOLE_SYSTEM_PROMPT,
        text_prompt=prompt,
        file_uris=[doc.gemini_file_uri],
        thinking_level=thinking_level,
    )
    parsed = parse_json_response(raw_text) or {}
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    if not isinstance(findings, list):
        return []
    # Убедимся, что поле source_documents хотя бы содержит сам документ.
    for f in findings:
        if isinstance(f, dict):
            sd = _coerce_str_list(f.get("source_documents"))
            if not sd:
                f["source_documents"] = [doc.title]
    return findings


async def _analyze_cross_documents(
    *,
    bundle: LawContextBundle,
    analysis_type: AnalysisType,
    analyzer_model: str,
    model_call: ModelCall,
    thinking_level: Optional[str] = None,
    norms_context: Optional[str] = None,
) -> List[Dict[str, Any]]:
    grouped_context = "\n".join(f"- {d.title}" for d in bundle.selected_laws)
    # S1-скаффолдинг (Этап 5): если есть извлечённые нормы — даём модели «ручки для
    # абуза» по документам, иначе (как раньше) только список заголовков + file-Part'ы.
    if norms_context:
        grouped_context += (
            "\n\nИзвлечённые нормы и «ручки для абуза» по документам "
            "(используй для сборки цепочек на стыке документов):\n" + norms_context
        )
    prompt = get_cross_document_contradiction_prompt(analysis_type, grouped_context)
    raw_text = await model_call(
        model_name=analyzer_model,
        system_prompt=LAW_LOOPHOLE_SYSTEM_PROMPT,
        text_prompt=prompt,
        file_uris=bundle.file_uris(),
        thinking_level=thinking_level,
    )
    parsed = parse_json_response(raw_text) or {}
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    if not isinstance(findings, list):
        return []
    return findings


# ---------------------------------------------------------------------------
# Реальный model_call для интеграции с Vertex/Gemini.
# Не используется в unit-тестах — там подсовывается mock.
# ---------------------------------------------------------------------------


async def make_vertex_model_call(
    *,
    model_name: str,
    system_prompt: str,
    text_prompt: str,
    file_uris: Optional[List[str]] = None,
    thinking_level: Optional[str] = None,
) -> str:
    """
    Дефолтная реализация model_call, использующая google.genai.

    Импорт lazy — чтобы unit-тесты могли импортировать analyzer без google-genai.
    """
    from google.genai import types  # type: ignore
    from rate_limiter import (
        execute_vertex_request,
        RequestPriority,
        get_current_client,
        mark_current_region_success,
        mark_current_region_error,
    )

    parts: List[Any] = []
    for uri in file_uris or []:
        if not uri:
            continue
        parts.append(types.Part.from_uri(file_uri=uri, mime_type="text/plain"))
    parts.append(types.Part(text=text_prompt))
    contents = [types.Content(role="user", parts=parts)]

    async def _call() -> Any:
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        cfg_kwargs: Dict[str, Any] = {"system_instruction": system_prompt}
        _tcfg = _make_thinking_config(types, thinking_level)
        if _tcfg is not None:
            cfg_kwargs["thinking_config"] = _tcfg
        return await client.aio.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )

    try:
        response = await execute_vertex_request(
            func=_call,
            model_name=model_name,
            priority=RequestPriority.NORMAL,
            max_retries=3,
        )
        await mark_current_region_success()
        return getattr(response, "text", "") or ""
    except Exception as e:
        await mark_current_region_error()
        logger.warning("[LOOPHOLE] vertex call failed: %s", e)
        # ВАЖНО: НЕ возвращаем "" — иначе сбой вызова (напр. 429 квота) тихо
        # превратился бы в "0 находок" и выдался юзеру как чистый закон + закэшировался.
        # Пробрасываем — analyze_loopholes отличит квотный сбой и сигналит на повтор.
        raise


# END OF FILE services/law_loophole/analyzer.py
