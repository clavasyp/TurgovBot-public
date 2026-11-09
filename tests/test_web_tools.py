# START OF FILE tests/test_web_tools.py
"""Тесты веб-инструментов сайта (interview/criminal/complaints/assistant/access/support).

Без БД/AI: проверяем регистрацию роутов, чистые функции и инвариант «ленивый импорт
user_handlers.* внутри обработчиков» (иначе циклический импорт telegram↔api)."""

from __future__ import annotations

import inspect

import pytest


# ---- регистрация роутов в приложении ----

def test_web_routes_registered():
    from api.main import app
    paths = {getattr(r, "path", None) for r in app.routes}
    for p in (
        "/v1/web/interview", "/v1/web/interview/options",
        "/v1/web/criminal/start", "/v1/web/criminal/{session_id}/generate",
        "/v1/web/complaint", "/v1/web/complaint/options",
        "/v1/web/assistant/cases", "/v1/web/assistant/cases/{case_id}/message",
        "/v1/web/access/status", "/v1/web/access/vip-application",
        "/v1/web/support/options", "/v1/web/support/ticket",
    ):
        assert p in paths, f"маршрут не зарегистрирован: {p}"


# ---- инвариант ленивого импорта лимитера (как в web_auth.ask) ----

_LAZY = "from user_handlers.core import check_and_update_usage, refund_usage"


def test_lazy_limiter_import_in_handlers():
    from api.routers import web_tools, web_assistant
    for fn in (web_tools.interview, web_tools.complaint, web_assistant.create_case):
        src = inspect.getsource(fn)
        assert _LAZY in src, f"{fn.__name__} должен лениво импортировать лимитер"
        assert "check_and_update_usage(" in src and "refund_usage(" in src


def _module_level_imports_user_handlers(mod) -> bool:
    """True, если в модуле есть import-СТЕЙТМЕНТ user_handlers с НУЛЕВЫМ отступом
    (т.е. на уровне модуля, не внутри функции). Докстринги/комментарии игнорируем."""
    for line in inspect.getsource(mod).splitlines():
        if line[:1] in (" ", "\t"):
            continue  # отступ → внутри функции/класса, не модульный уровень
        s = line.strip()
        if s.startswith("import user_handlers") or s.startswith("from user_handlers"):
            return True
    return False


def test_services_no_module_level_user_handlers():
    """Сервисы не должны тянуть user_handlers на уровне модуля (только лениво внутри функций)."""
    import services.interview_service as s1
    import services.criminal_case_service as s2
    import services.assistant_service as s3
    import services.complaint_service as s4
    for mod in (s1, s2, s3, s4):
        assert not _module_level_imports_user_handlers(mod), \
            f"{mod.__name__}: модульный импорт user_handlers (риск цикла telegram↔api)"

    # complaint_service переиспользует хелперы бота — но ТОЛЬКО лениво внутри функции.
    assert "from user_handlers.complaint_flow import" in inspect.getsource(s4.generate_complaint)


# ---- чистые функции ----

def test_parse_interview_questions():
    from api.routers.web_tools import _parse_questions
    qs = _parse_questions("1. Первый?\n2) Второй\nпросто текст\n3.   Третий вопрос")
    assert [q.number for q in qs] == [1, 2, 3]
    assert qs[0].text == "Первый?"
    assert qs[2].text == "Третий вопрос"
    assert _parse_questions("без нумерации совсем") == []


def test_assistant_usage_type_and_validation():
    from services import assistant_service as a
    assert a.usage_type_for("judge", "court_case") == "judge_court_case"
    assert a.usage_type_for("advocate", "field_work") == "advocate_field_work"
    # валидные пары — без исключения
    a.validate_role_stage("advocate", "field_work")
    a.validate_role_stage("judge", "court_case")
    # судья не имеет field_work
    with pytest.raises(ValueError):
        a.validate_role_stage("judge", "field_work")
    with pytest.raises(ValueError):
        a.validate_role_stage("unknown_role", "field_work")


def test_assistant_options_shape():
    from services import assistant_service as a
    o = a.get_options()
    assert {r["key"] for r in o["roles"]} == {"advocate", "prosecutor", "judge"}
    assert o["stages_by_role"]["judge"] == ["court_case"]
    # инструменты адвоката на field_work — непустой список {name,label}
    tools = o["tools"]["advocate"]["field_work"]
    assert tools and all("name" in t and "label" in t for t in tools)


def test_web_notify_ext_for():
    from services.web_notify import _ext_for
    assert _ext_for("a.png", "image/png") == ".png"
    assert _ext_for("clip", "video/mp4") == ".mp4"
    assert _ext_for("noext", "application/octet-stream") == ""
    assert _ext_for("file.JPEG", "") == ".JPEG"


def test_criminal_parts_match_config():
    """Веб-обёртка уголовки опирается на CRIMINAL_CASE_PARTS_ORDER/DISPLAY из config."""
    import config
    assert config.CRIMINAL_CASE_PARTS_ORDER == ["facts", "charges", "evidence", "demands"]
    assert set(config.CRIMINAL_CASE_PARTS_DISPLAY) == set(config.CRIMINAL_CASE_PARTS_ORDER)


# END OF FILE tests/test_web_tools.py
