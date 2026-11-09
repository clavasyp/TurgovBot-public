# START OF FILE tests/test_web_auth.py
"""Тесты веб-входа сайта (services/telegram_login.py) + паритет лимитера с ботом.

Покрывают:
  * verify_telegram_login: валидная подпись / подделка hash / подмена поля / чужой токен /
    устаревший auth_date (replay) / битый ввод;
  * сессия: round-trip, проверка typ=='web', FAIL-CLOSED при небезопасном секрете
    (config.WEB_SESSION_SECRET_OK=False) — и на выпуске, и на проверке;
  * паритет: веб-роутер /ask использует ТУ ЖЕ функцию лимитера, что и бот (общий аккаунт).
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import time

import pytest

import config
from services import telegram_login as tl


def _sign(data: dict, token: str) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    dcs = "\n".join(f"{k}={data[k]}" for k in sorted(data) if k != "hash")
    return _hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()


def _payload(token: str, **over) -> dict:
    d = {
        "id": "123",
        "first_name": "Иван",
        "username": "ivan",
        "auth_date": str(int(time.time())),
    }
    d.update(over)
    d["hash"] = _sign(d, token)
    return d


# ---- verify_telegram_login ----

def test_verify_valid(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token", raising=False)
    monkeypatch.setattr(config, "AUTH_DATE_MAX_AGE", 900, raising=False)
    assert tl.verify_telegram_login(_payload("test-token")) is True


def test_verify_tampered_hash(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token", raising=False)
    p = _payload("test-token")
    p["hash"] = "deadbeef"
    assert tl.verify_telegram_login(p) is False


def test_verify_tampered_field(monkeypatch):
    """Подмена поля после подписи → HMAC не сходится."""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token", raising=False)
    monkeypatch.setattr(config, "AUTH_DATE_MAX_AGE", 900, raising=False)
    p = _payload("test-token")
    p["id"] = "999"
    assert tl.verify_telegram_login(p) is False


def test_verify_wrong_token(monkeypatch):
    """Подпись чужим токеном не проходит против настоящего токена бота."""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "real-token", raising=False)
    monkeypatch.setattr(config, "AUTH_DATE_MAX_AGE", 900, raising=False)
    assert tl.verify_telegram_login(_payload("attacker-token")) is False


def test_verify_stale_replay(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token", raising=False)
    monkeypatch.setattr(config, "AUTH_DATE_MAX_AGE", 900, raising=False)
    p = _payload("test-token", auth_date=str(int(time.time()) - 5000))
    assert tl.verify_telegram_login(p) is False


def test_verify_garbage(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token", raising=False)
    assert tl.verify_telegram_login({"id": "1"}) is False
    assert tl.verify_telegram_login("not-a-dict") is False
    assert tl.verify_telegram_login({}) is False


# ---- сессия ----

def _ok_secret(monkeypatch):
    monkeypatch.setattr(config, "WEB_SESSION_SECRET", "unit-secret", raising=False)
    monkeypatch.setattr(config, "WEB_SESSION_SECRET_OK", True, raising=False)
    monkeypatch.setattr(config, "WEB_SESSION_TTL_SECONDS", 3600, raising=False)


def test_session_roundtrip(monkeypatch):
    _ok_secret(monkeypatch)
    tok = tl.issue_web_session(777)
    claims = tl.verify_web_session(tok)
    assert claims is not None
    assert claims["uid"] == 777
    assert claims["typ"] == "web"


def test_session_failclosed_issue(monkeypatch):
    monkeypatch.setattr(config, "WEB_SESSION_SECRET_OK", False, raising=False)
    with pytest.raises(RuntimeError):
        tl.issue_web_session(1)


def test_session_failclosed_verify(monkeypatch):
    """Токен выпущен валидно, но затем секрет 'небезопасен' → не принимаем (нельзя подделать)."""
    _ok_secret(monkeypatch)
    tok = tl.issue_web_session(5)
    monkeypatch.setattr(config, "WEB_SESSION_SECRET_OK", False, raising=False)
    assert tl.verify_web_session(tok) is None


def test_session_typ_enforced(monkeypatch):
    """JWT с тем же секретом, но typ!='web' (как device-токен) → отклоняем."""
    import jwt
    _ok_secret(monkeypatch)
    bad = jwt.encode(
        {"uid": 1, "typ": "access", "iat": int(time.time()), "exp": int(time.time()) + 999},
        "unit-secret",
        algorithm="HS256",
    )
    assert tl.verify_web_session(bad) is None


def test_session_empty(monkeypatch):
    _ok_secret(monkeypatch)
    assert tl.verify_web_session("") is None
    assert tl.verify_web_session("garbage.token.value") is None


# ---- паритет лимитера с ботом ----

def test_limiter_parity():
    """Веб /ask использует РОВНО те же функции лимита/отката, что и бот → общий аккаунт и счётчики.
    (Импорт в web_auth ленивый — во избежание циклического импорта; проверяем по исходнику + наличию.)"""
    import inspect
    from user_handlers import core
    from api.routers import web_auth

    assert callable(core.check_and_update_usage) and callable(core.refund_usage)
    src = inspect.getsource(web_auth.ask)
    assert "from user_handlers.core import check_and_update_usage, refund_usage" in src
    assert "check_and_update_usage(" in src and "refund_usage(" in src
