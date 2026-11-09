"""Regression tests for bounded retries around transient Vertex/Gemini failures."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from google.genai import errors as genai_errors

import rate_limiter as rate_limiter_module
from rate_limiter import TransientVertexError, VertexAIRateLimiter


def _server_error(code: int = 502) -> genai_errors.ServerError:
    return genai_errors.ServerError(
        code,
        {
            "message": "<!DOCTYPE html><title>Error 502 (Server Error)</title>",
            "status": "BAD_GATEWAY",
        },
    )


def _prepared_limiter(monkeypatch) -> tuple[VertexAIRateLimiter, AsyncMock]:
    limiter = VertexAIRateLimiter()
    limiter._wait_for_capacity = AsyncMock(return_value=True)
    limiter._fire_outage_alert = Mock()
    sleep = AsyncMock()
    monkeypatch.setattr(rate_limiter_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(rate_limiter_module.random, "uniform", lambda _a, _b: 0.0)
    return limiter, sleep


async def test_server_502_retries_then_succeeds(monkeypatch):
    limiter, sleep = _prepared_limiter(monkeypatch)
    expected = object()
    call = AsyncMock(side_effect=[_server_error(), expected])

    result = await limiter.execute_with_rate_limit(
        call,
        model_name="gemini-3-flash",
        max_retries=3,
    )

    assert result is expected
    assert call.await_count == 2
    assert limiter._wait_for_capacity.await_count == 2
    assert sleep.await_count == 1
    assert len(limiter.request_history["gemini-3-flash"]) == 2
    assert limiter.stats["total_requests"] == 1
    assert limiter.stats["successful_requests"] == 1
    assert limiter.stats["failed_requests"] == 0
    assert limiter.stats["resource_exhausted_errors"] == 0
    assert limiter.error_counts["gemini-3-flash"] == 0
    assert limiter.adaptive_throttling["gemini-3-flash"] is False
    assert "gemini-3-flash" not in limiter.last_error_time
    limiter._fire_outage_alert.assert_not_called()


async def test_server_502_exhaustion_becomes_safe_temporary_error(monkeypatch):
    limiter, sleep = _prepared_limiter(monkeypatch)
    errors = [_server_error(), _server_error(), _server_error()]
    call = AsyncMock(side_effect=errors)

    with pytest.raises(TransientVertexError) as raised:
        await limiter.execute_with_rate_limit(
            call,
            model_name="gemini-3-flash",
            max_retries=3,
        )

    assert raised.value.status_code == 502
    assert raised.value.attempts == 3
    assert raised.value.__cause__ is errors[-1]
    assert "<!DOCTYPE" not in str(raised.value)
    assert call.await_count == 3
    assert limiter._wait_for_capacity.await_count == 3
    assert sleep.await_count == 2
    assert len(limiter.request_history["gemini-3-flash"]) == 3
    assert limiter.stats["total_requests"] == 1
    assert limiter.stats["successful_requests"] == 0
    assert limiter.stats["failed_requests"] == 1
    assert limiter.stats["resource_exhausted_errors"] == 0
    assert "gemini-3-flash" not in limiter.last_error_time
    limiter._fire_outage_alert.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        genai_errors.ClientError(400, {"message": "bad request", "status": "INVALID_ARGUMENT"}),
        genai_errors.ClientError(403, {"message": "denied", "status": "PERMISSION_DENIED"}),
        _server_error(501),
        RuntimeError("local programming error"),
    ],
)
async def test_permanent_error_is_not_retried(monkeypatch, error):
    limiter, sleep = _prepared_limiter(monkeypatch)
    call = AsyncMock(side_effect=error)

    with pytest.raises(type(error)) as raised:
        await limiter.execute_with_rate_limit(
            call,
            model_name="gemini-3-flash",
            max_retries=3,
        )

    assert raised.value is error
    assert call.await_count == 1
    assert limiter._wait_for_capacity.await_count == 1
    assert sleep.await_count == 0
    assert len(limiter.request_history["gemini-3-flash"]) == 1
    assert limiter.stats["total_requests"] == 1
    assert limiter.stats["successful_requests"] == 0
    assert limiter.stats["failed_requests"] == 1
