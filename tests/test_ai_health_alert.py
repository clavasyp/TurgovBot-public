# START OF FILE tests/test_ai_health_alert.py
"""Тесты алертов о критичных сбоях доступа к Vertex (services/ai_health_alert.py).

Покрывают:
  * классификацию (биллинг/permission/suspended/api-off/auth/403/401 — критично;
    429-квота / 5xx / таймаут очереди — НЕ критично);
  * дедуп: первый алерт в окне уходит, повтор того же класса подавляется;
  * разные классы дедупятся независимо;
  * полную безопасность (отправка упала → исключение наружу не летит).
"""

from __future__ import annotations

import pytest

from services import ai_health_alert as aha


# --- классификация -------------------------------------------------------

@pytest.mark.parametrize("text", [
    "403 PERMISSION_DENIED. Permission 'aiplatform.endpoints.predict' denied",
    "{'error': {'code': 403, 'message': 'This API method requires billing to be "
    "enabled. Billing has been disabled', 'status': 'PERMISSION_DENIED'}}",
    "BILLING_DISABLED: project billing is disabled",
    "CONSUMER_SUSPENDED: Project has been suspended",
    "Vertex AI API has not been used in project 12345 before or it is disabled",
    "401 UNAUTHENTICATED: Could not refresh access token",
    "The caller does not have permission",
])
def test_classify_critical(text):
    assert aha.classify_gcp_error(Exception(text)) is not None


@pytest.mark.parametrize("text", [
    "429 RESOURCE_EXHAUSTED. Resource exhausted. Please try again later.",
    "503 The service is currently unavailable",
    "500 Internal error encountered",
    "Превышено время ожидания квоты для модели gemini-3-flash",
    "deadline exceeded",
    "",
])
def test_classify_non_critical(text):
    assert aha.classify_gcp_error(Exception(text)) is None


def test_429_with_403_digits_not_misclassified():
    # 429-сообщение не должно стать критичным даже если где-то мелькнёт число.
    assert aha.classify_gcp_error(Exception("429 RESOURCE_EXHAUSTED quota 403/min")) is None


def test_extract_code_from_attr():
    class E(Exception):
        code = 403
    assert aha.classify_gcp_error(E("denied something")) is not None


# --- снятая/переименованная модель (404 publisher model) — НЕ ops-инцидент -------
_MODEL_404 = (
    "404 NOT_FOUND. {'error': {'code': 404, 'message': 'Publisher model "
    "`projects/bot5-498212/locations/global/publishers/google/models/"
    "gemini-3.1-flash-lite-preview` was not found or your project does not have "
    "access to it.', 'status': 'NOT_FOUND'}}"
)


def test_model_not_found_classified_as_model_not_project():
    # Регресс инцидента 2026-07-09: model-404 не должен метиться как PROJECT_NOT_FOUND.
    label = aha.classify_gcp_error(Exception(_MODEL_404))
    assert label == aha.MODEL_UNAVAILABLE_LABEL


def test_plain_project_not_found_still_critical():
    # Настоящий project-not-found по конкретному маркеру всё ещё критичен.
    assert aha.classify_gcp_error(Exception("Failed to find the project `deadproj`")) is not None


# --- дедуп + отправка ----------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_dedup():
    aha._LAST_ALERT.clear()
    yield
    aha._LAST_ALERT.clear()


@pytest.fixture
def captured(monkeypatch):
    sent = []

    async def _fake_send(chat_id, html, attempts=3):
        sent.append((chat_id, html))
        return True

    monkeypatch.setattr(aha, "_send_telegram", _fake_send)
    monkeypatch.setattr(aha.config, "SUPER_ADMIN_ID", 777, raising=False)
    monkeypatch.setattr(aha.config, "AI_OUTAGE_ALERT_DEDUP_SECONDS", 600, raising=False)
    return sent


async def test_alert_sent_once_then_deduped(captured):
    exc = Exception("403 PERMISSION_DENIED billing has been disabled")
    assert await aha.maybe_alert_ai_outage("gemini-3-flash", exc) is True
    # Повтор того же класса в окне — подавлен.
    assert await aha.maybe_alert_ai_outage("gemini-3-flash", exc) is False
    assert len(captured) == 1
    chat_id, html = captured[0]
    assert chat_id == 777
    assert "AI НЕ РАБОТАЕТ У ВСЕХ" in html
    assert "gemini-3-flash" in html


async def test_non_critical_no_alert(captured):
    assert await aha.maybe_alert_ai_outage("gemini-3-flash", Exception("429 RESOURCE_EXHAUSTED")) is False
    assert captured == []


async def test_model_404_alert_is_not_billing(captured):
    # Алерт по снятой модели уходит, но БЕЗ «AI НЕ РАБОТАЕТ У ВСЕХ»/совета про биллинг.
    assert await aha.maybe_alert_ai_outage("gemini-3.1-flash-lite-preview",
                                           Exception(_MODEL_404)) is True
    assert len(captured) == 1
    _chat, html = captured[0]
    assert "AI НЕ РАБОТАЕТ У ВСЕХ" not in html
    assert "МОДЕЛЬ НЕДОСТУПНА" in html
    assert "gemini-3.1-flash-lite" in html


async def test_different_classes_not_shared_dedup(captured):
    assert await aha.maybe_alert_ai_outage("m", Exception("BILLING_DISABLED")) is True
    assert await aha.maybe_alert_ai_outage("m", Exception("CONSUMER_SUSPENDED")) is True
    assert len(captured) == 2


async def test_dedup_window_zero_allows_repeat(captured, monkeypatch):
    monkeypatch.setattr(aha.config, "AI_OUTAGE_ALERT_DEDUP_SECONDS", 0, raising=False)
    exc = Exception("PERMISSION_DENIED")
    assert await aha.maybe_alert_ai_outage("m", exc) is True
    assert await aha.maybe_alert_ai_outage("m", exc) is True
    assert len(captured) == 2


async def test_send_failure_is_swallowed(monkeypatch):
    async def _boom(chat_id, html, attempts=3):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(aha, "_send_telegram", _boom)
    monkeypatch.setattr(aha.config, "SUPER_ADMIN_ID", 1, raising=False)
    # Не должно бросить наружу.
    assert await aha.maybe_alert_ai_outage("m", Exception("BILLING_DISABLED")) is False
