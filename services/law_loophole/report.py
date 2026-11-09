# START OF FILE services/law_loophole/report.py

"""
Локальное (без AI) форматирование финального отчета анализатора в Telegram HTML.

Используется как дешевый и предсказуемый формат вывода.
Промпт get_loophole_report_format_prompt оставлен как опция,
но в /test-режиме безопаснее не делать ещё один запрос к модели.
"""

from __future__ import annotations

import html
from typing import Any, Dict, Iterable, List


_TYPE_LABELS = {
    "direct_contradiction": "прямое противоречие",
    "regulatory_gap": "пробел регулирования",
    "narrow_wording": "слишком узкая формулировка",
    "vague_term": "размытый термин",
    "missing_procedure": "нет процедуры",
    "missing_sanction": "нет санкции",
    "sanction_without_offense": "санкция без состава",
    "authority_collision": "коллизия полномочий",
    "hierarchy_unclear": "иерархическая неопределенность",
    "missing_deadline": "нет срока",
    "overbroad_exception": "слишком широкое исключение",
    "missing_subject": "нет субъекта",
    "missing_object": "нет объекта",
    "double_punishment": "двойное наказание",
    "proof_impossibility": "невозможность доказывания",
    "procedure_conflict": "конфликт процедур",
    "deadline_conflict": "конфликт сроков",
    "sanction_conflict": "конфликт санкций",
    "rights_vs_power_conflict": "права vs полномочия",
    "exploit_chain": "🔗 эксплойт-цепочка",
    "other": "иное",
}

_SEVERITY_LABELS = {
    "critical": "🔴 КРИТИЧНО",
    "high": "🟠 Высокий",
    "medium": "🟡 Средний",
    "low": "🟢 Низкий",
}

_CONFIDENCE_LABELS = {
    "high": "уверенность: высокая",
    "medium": "уверенность: средняя",
    "low": "уверенность: низкая",
}


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def _format_finding(f: Dict[str, Any]) -> str:
    sev = _SEVERITY_LABELS.get(f.get("severity", "low"), f.get("severity", ""))
    vtype = _TYPE_LABELS.get(f.get("vulnerability_type", "other"), f.get("vulnerability_type", ""))
    conf = _CONFIDENCE_LABELS.get(f.get("confidence", "low"), "")

    lines: List[str] = []
    lines.append(f"<b>{_esc(f.get('title', ''))}</b>")
    lines.append(f"{sev} • <i>{_esc(vtype)}</i> • {_esc(conf)}")

    src_docs = f.get("source_documents") or []
    if src_docs:
        lines.append("<b>Источники:</b> " + _esc(", ".join(src_docs)))

    src_frags = f.get("source_fragments") or []
    if src_frags:
        first = src_frags[0]
        if len(first) > 350:
            first = first[:350] + "…"
        lines.append("<b>Фрагмент:</b> <code>" + _esc(first) + "</code>")

    if f.get("issue_summary"):
        lines.append("<b>Суть:</b> " + _esc(f["issue_summary"]))

    abuses = f.get("abuse_scenarios") or []
    if abuses:
        lines.append("<b>Абуз-сценарий:</b>")
        # до 6 шагов — цепочки (exploit_chain) бывают многоходовыми (~5 звеньев).
        for a in abuses[:6]:
            lines.append("• " + _esc(a))

    if f.get("gameplay_impact"):
        lines.append("<b>Игровой эффект:</b> " + _esc(f["gameplay_impact"]))

    if f.get("fix_recommendation"):
        lines.append("<b>Рекомендация:</b> " + _esc(f["fix_recommendation"]))

    return "\n".join(lines)


def format_loophole_summary(report: Dict[str, Any]) -> str:
    """Краткая сводка (шапка) отчёта одним HTML-сообщением: всего/по severity/риски/охват.
    Используется и в начале сообщений, и как подпись при отправке HTML-документом."""
    summary = report.get("summary") or {}
    findings = report.get("findings") or []
    discarded = report.get("discarded_findings") or []

    lines = [
        "<b>🔎 Анализ дыр и противоречий в законке</b>",
        f"Найдено уязвимостей: <b>{summary.get('total_findings', len(findings))}</b>",
        (
            f"🔴 {summary.get('critical_count', 0)} • "
            f"🟠 {summary.get('high_count', 0)} • "
            f"🟡 {summary.get('medium_count', 0)} • "
            f"🟢 {summary.get('low_count', 0)}"
        ),
    ]
    if summary.get("main_risks"):
        lines.append(
            "<b>Главные риски:</b>\n• " + "\n• ".join(_esc(r) for r in summary["main_risks"])
        )
    if summary.get("coverage_note"):
        lines.append("<i>Охват: " + _esc(summary["coverage_note"]) + "</i>")
    if discarded:
        lines.append(f"<i>Отброшено без источников: {len(discarded)}</i>")
    return "\n".join(lines)


def format_loophole_report(report: Dict[str, Any]) -> List[str]:
    """
    Форматирует финальный отчет анализатора в список Telegram-сообщений (HTML).

    Args:
        report: dict вида {"summary": {...}, "findings": [...], "discarded_findings": [...]}

    Returns:
        Список готовых HTML-блоков. Каждое сообщение влезает в Telegram-лимит 4096 символов.
    """
    findings = report.get("findings") or []
    header = format_loophole_summary(report)

    if not findings:
        return [
            header
            + "\n\n<b>Явных уязвимостей по выбранным документам не найдено.</b>\n"
            "Учтите ограничения охвата выше."
        ]

    messages: List[str] = [header]
    current = ""
    for f in findings:
        block = _format_finding(f)
        candidate = (current + "\n\n" + block) if current else block
        if len(candidate) > 3500:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


_DOC_CSS = """
:root{--bg:#0f1115;--card:#1a1d24;--muted:#9aa3b2;--txt:#e8ecf2;--line:#2a2f3a;
--crit:#ff5757;--high:#ff9f43;--med:#ffd23f;--low:#4fd06a;--accent:#5b8dff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font-family:'Segoe UI',Roboto,system-ui,sans-serif;line-height:1.5;padding:24px}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.counts{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
.pill{padding:4px 10px;border-radius:999px;font-size:13px;font-weight:600;background:var(--card);border:1px solid var(--line)}
.pill.c{color:var(--crit)}.pill.h{color:var(--high)}.pill.m{color:var(--med)}.pill.l{color:var(--low)}
.risks{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
.risks ul{margin:6px 0 0;padding-left:20px}
.cov{color:var(--muted);font-style:italic;font-size:13px;margin:8px 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);
border-radius:10px;padding:14px 16px;margin:12px 0}
.card.sev-critical{border-left-color:var(--crit)}.card.sev-high{border-left-color:var(--high)}
.card.sev-medium{border-left-color:var(--med)}.card.sev-low{border-left-color:var(--low)}
.card-h{font-size:16px;font-weight:700;margin-bottom:6px}
.num{color:var(--muted);font-weight:600;margin-right:6px}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;font-size:12px}
.badge{padding:2px 8px;border-radius:6px;font-weight:700}
.badge.b-critical{background:rgba(255,87,87,.15);color:var(--crit)}
.badge.b-high{background:rgba(255,159,67,.15);color:var(--high)}
.badge.b-medium{background:rgba(255,210,63,.15);color:var(--med)}
.badge.b-low{background:rgba(79,208,106,.15);color:var(--low)}
.tag{color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:2px 8px}
.conf{color:var(--muted)}
.row{margin:6px 0}.row b{color:#fff}
.frag{background:#0b0d11;border:1px solid var(--line);border-radius:8px;padding:8px 10px;
margin:6px 0;font-family:'Cascadia Code',Consolas,monospace;font-size:12.5px;white-space:pre-wrap;color:#c8d2e0}
.fix{background:rgba(91,141,255,.08);border:1px solid rgba(91,141,255,.25);border-radius:8px;padding:8px 10px}
footer{color:var(--muted);font-size:12px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}
"""


def _finding_html_card(f: Dict[str, Any], idx: int) -> str:
    sev = f.get("severity", "low")
    sev_key = sev if sev in _SEVERITY_LABELS else "low"
    sev_label = _SEVERITY_LABELS.get(sev_key, sev_key)
    vtype = _TYPE_LABELS.get(f.get("vulnerability_type", "other"), f.get("vulnerability_type", ""))
    conf = _CONFIDENCE_LABELS.get(f.get("confidence", "low"), "")

    p: List[str] = [f'<div class="card sev-{sev_key}">']
    p.append(f'<div class="card-h"><span class="num">#{idx}</span>{_esc(f.get("title", ""))}</div>')
    p.append(
        f'<div class="meta"><span class="badge b-{sev_key}">{_esc(sev_label)}</span>'
        f'<span class="tag">{_esc(vtype)}</span><span class="conf">{_esc(conf)}</span></div>'
    )
    src_docs = f.get("source_documents") or []
    if src_docs:
        p.append(f'<div class="row"><b>Источники:</b> {_esc(", ".join(src_docs))}</div>')
    for frag in (f.get("source_fragments") or [])[:2]:
        p.append(f'<div class="frag">{_esc(frag)}</div>')
    if f.get("issue_summary"):
        p.append(f'<div class="row"><b>Суть:</b> {_esc(f["issue_summary"])}</div>')
    abuses = f.get("abuse_scenarios") or []
    if abuses:
        p.append('<div class="row"><b>Абуз-сценарии:</b><ul>'
                 + "".join(f"<li>{_esc(a)}</li>" for a in abuses[:5]) + "</ul></div>")
    if f.get("gameplay_impact"):
        p.append(f'<div class="row"><b>Игровой эффект:</b> {_esc(f["gameplay_impact"])}</div>')
    if f.get("fix_recommendation"):
        p.append(f'<div class="row fix"><b>Рекомендация:</b> {_esc(f["fix_recommendation"])}</div>')
    p.append("</div>")
    return "\n".join(p)


def build_loophole_report_html_doc(report: Dict[str, Any], *, title_suffix: str = "") -> bytes:
    """Полноценный оформленный HTML-документ отчёта (для send_document). Без внешних
    зависимостей и без сети (инлайн-CSS). Возвращает utf-8 байты."""
    summary = report.get("summary") or {}
    findings = report.get("findings") or []
    discarded = report.get("discarded_findings") or []

    head: List[str] = [f'<h1>🔎 Анализ дыр и противоречий в законке</h1>']
    sub = f"Найдено уязвимостей: <b>{summary.get('total_findings', len(findings))}</b>"
    if title_suffix:
        sub += " • " + _esc(title_suffix)
    head.append(f'<div class="sub">{sub}</div>')
    head.append(
        '<div class="counts">'
        f'<span class="pill c">🔴 критич.: {summary.get("critical_count", 0)}</span>'
        f'<span class="pill h">🟠 высок.: {summary.get("high_count", 0)}</span>'
        f'<span class="pill m">🟡 средн.: {summary.get("medium_count", 0)}</span>'
        f'<span class="pill l">🟢 низк.: {summary.get("low_count", 0)}</span>'
        '</div>'
    )
    if summary.get("main_risks"):
        head.append('<div class="risks"><b>Главные риски</b><ul>'
                    + "".join(f"<li>{_esc(r)}</li>" for r in summary["main_risks"]) + "</ul></div>")
    if summary.get("coverage_note"):
        head.append(f'<div class="cov">Охват: {_esc(summary["coverage_note"])}</div>')

    if findings:
        cards = "\n".join(_finding_html_card(f, i + 1) for i, f in enumerate(findings))
    else:
        cards = ('<div class="card"><div class="card-h">Явных уязвимостей не найдено</div>'
                 '<div class="row">Учтите ограничения охвата выше — возможна недоступность модели '
                 'или узкая выборка документов.</div></div>')

    foot = "Сгенерировано анализатором дыр в законке"
    if discarded:
        foot += f" • отброшено без источников: {len(discarded)}"

    doc = (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Анализ дыр в законке</title><style>" + _DOC_CSS + "</style></head><body>"
        '<div class="wrap">' + "\n".join(head) + "\n" + cards
        + f'<footer>{_esc(foot)}</footer></div></body></html>'
    )
    return doc.encode("utf-8")


# END OF FILE services/law_loophole/report.py
