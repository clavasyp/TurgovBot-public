# START OF FILE services/law_loophole/metrics.py

"""
Метрики качества анализатора дыр — для golden-харнесса (A/B старый промпт ↔ v2).

Программно-вычислимые метрики (без эксперта), которые ловят, реально ли v2 «думает
цепочками» и не фантазирует:
- chain_rate     — доля находок-цепочек (>=2 источника И >=3 шага). Прокси «цепочечности».
- grounding_rate — доля source_fragments, дословно встречающихся в исходных текстах.
                   Падение = рост галлюцинаций. Цель >= 0.95.
- noise_rate     — доля отброшенных находок (без источников / развалены).
- exploit_chain_count, severity-распределение.

Метод A/B (owner-side, нужны прод-Vertex + 10-20 golden-кейсов):
прогнать ОДИН bundle через старый и v2-конвейер, сравнить метрики
(compare_reports). Golden-набор + реальный прогон — вне unit-тестов.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def chain_rate(findings: List[Dict[str, Any]]) -> float:
    """Доля находок-цепочек: >=2 source_documents И >=3 шага в abuse_scenarios."""
    if not findings:
        return 0.0
    n = sum(
        1
        for f in findings
        if len(f.get("source_documents") or []) >= 2 and len(f.get("abuse_scenarios") or []) >= 3
    )
    return n / len(findings)


def grounding_rate(findings: List[Dict[str, Any]], doc_texts: str) -> Optional[float]:
    """Доля source_fragments, дословно (нормализованный substring) встречающихся в
    исходных текстах. None — если текстов/фрагментов нет."""
    if not doc_texts:
        return None
    hay = _norm(doc_texts)
    frags = [fr for f in findings for fr in (f.get("source_fragments") or [])]
    frags = [fr for fr in frags if _norm(fr)]
    if not frags:
        return None
    hit = sum(1 for fr in frags if _norm(fr) in hay)
    return hit / len(frags)


def severity_counts(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        s = f.get("severity")
        if s in out:
            out[s] += 1
    return out


def summarize_report_metrics(
    report: Dict[str, Any], *, doc_texts: Optional[str] = None
) -> Dict[str, Any]:
    """Сводка метрик по одному отчёту analyze_loopholes."""
    findings = report.get("findings") or []
    discarded = report.get("discarded_findings") or []
    total = len(findings) + len(discarded)
    gr = grounding_rate(findings, doc_texts) if doc_texts else None
    return {
        "total_findings": len(findings),
        "chain_rate": round(chain_rate(findings), 3),
        "grounding_rate": (round(gr, 3) if gr is not None else None),
        "exploit_chain_count": sum(1 for f in findings if f.get("vulnerability_type") == "exploit_chain"),
        "severity": severity_counts(findings),
        "noise_rate": round(len(discarded) / total, 3) if total else 0.0,
    }


def compare_reports(
    old_report: Dict[str, Any],
    new_report: Dict[str, Any],
    *,
    doc_texts: Optional[str] = None,
) -> Dict[str, Any]:
    """A/B: метрики старого и нового отчёта по одному bundle + дельты ключевых метрик."""
    old_m = summarize_report_metrics(old_report, doc_texts=doc_texts)
    new_m = summarize_report_metrics(new_report, doc_texts=doc_texts)
    deltas = {
        "chain_rate": round(new_m["chain_rate"] - old_m["chain_rate"], 3),
        "total_findings": new_m["total_findings"] - old_m["total_findings"],
        "exploit_chain_count": new_m["exploit_chain_count"] - old_m["exploit_chain_count"],
    }
    if old_m["grounding_rate"] is not None and new_m["grounding_rate"] is not None:
        deltas["grounding_rate"] = round(new_m["grounding_rate"] - old_m["grounding_rate"], 3)
    return {"old": old_m, "new": new_m, "delta": deltas}


# END OF FILE services/law_loophole/metrics.py
