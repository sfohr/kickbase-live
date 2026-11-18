"""Tests for the client's request throttle (REQUEST_DELAY_SECONDS)."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests

from kb.client import REQUEST_DELAY_SECONDS, KickbaseClient
from kb.errors import KickbaseRequestError


class FakeClock:
    """Stands in for the `time` module inside kb.client.

    Time only moves when a test calls advance() or the client calls sleep(),
    so the tests don't depend on how often the client reads the clock.
    """

    def __init__(self, start: float = 100.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_client() -> KickbaseClient:
    """Create a client that counts as logged in, without a network call.

    get() raises KickbaseAuthenticationError when _token is missing, and the
    real login() would POST to the Kickbase API, so the token is set directly.
    A fresh client per test keeps _last_request_at from leaking between tests.
    """
    client = KickbaseClient("test@example.com", "password123")
    client._token = "test_token"
    return client


def _response(status_code: int = 200, body: dict[str, Any] | None = None) -> Mock:
    """Fake response for the mocked session.request().

    status_code must be a real int, because the client compares it with
    401 and 400. json() must return a dict, because the client rejects
    non-object bodies.
    """
    resp = Mock(status_code=status_code)
    resp.json.return_value = body if body is not None else {"data": "test"}
    return resp


@pytest.fixture
def clock():
    fake = FakeClock()
    with patch("kb.client.time", fake):
        yield fake


class TestThrottle:
    def test_first_request_does_not_sleep(self, clock):
        client = _make_client()
        with patch.object(client._session, "request", return_value=_response()):
            client.get("/test")
        assert clock.sleeps == []

    def test_sleeps_remaining_delay_between_calls(self, clock):
        client = _make_client()
        with patch.object(
            client._session, "request", return_value=_response()
        ) as mock_request:
            client.get("/test")
            clock.advance(0.2)
            client.get("/test")

        assert mock_request.call_count == 2
        assert clock.sleeps == [pytest.approx(REQUEST_DELAY_SECONDS - 0.2)]

    def test_does_not_sleep_when_delay_already_passed(self, clock):
        client = _make_client()
        with patch.object(client._session, "request", return_value=_response()):
            client.get("/test")
            clock.advance(REQUEST_DELAY_SECONDS + 0.5)
            client.get("/test")
        assert clock.sleeps == []

    def test_does_not_sleep_exactly_at_delay(self, clock):
        client = _make_client()
        with patch.object(client._session, "request", return_value=_response()):
            client.get("/test")
            clock.advance(REQUEST_DELAY_SECONDS)
            client.get("/test")
        assert clock.sleeps == []

    def test_delay_applies_after_failed_request(self, clock):
        client = _make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[requests.ConnectionError("down"), _response()],
        ):
            with pytest.raises(KickbaseRequestError):
                client.get("/test")
            clock.advance(0.1)
            client.get("/test")

        assert clock.sleeps == [pytest.approx(REQUEST_DELAY_SECONDS - 0.1)]

    def test_delay_applies_after_login(self, clock):
        client = KickbaseClient("test@example.com", "password123")
        login_resp = _response(body={"tkn": "abc", "u": {"id": "1"}})
        with patch.object(
            client._session, "request", side_effect=[login_resp, _response()]
        ):
            client.login()
            clock.advance(0.1)
            client.get("/test")

        assert clock.sleeps == [pytest.approx(REQUEST_DELAY_SECONDS - 0.1)]
