"""Tests for Kickbase error classes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from kb.errors import (
    KickbaseAPIError,
    KickbaseAuthenticationError,
    KickbaseDataError,
    KickbaseError,
    KickbaseNotFoundError,
    KickbaseRateLimitError,
    KickbaseRequestError,
    parse_retry_after,
)


class TestHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [
            KickbaseAuthenticationError,
            KickbaseRequestError,
            KickbaseDataError,
            KickbaseAPIError,
            KickbaseNotFoundError,
            KickbaseRateLimitError,
        ],
        ids=lambda c: c.__name__,
    )
    def test_all_are_kickbase_errors(self, cls):
        assert issubclass(cls, KickbaseError)

    @pytest.mark.parametrize(
        "cls", [KickbaseNotFoundError, KickbaseRateLimitError], ids=lambda c: c.__name__
    )
    def test_http_status_errors_are_api_errors(self, cls):
        assert issubclass(cls, KickbaseAPIError)

    @pytest.mark.parametrize(
        "cls",
        [KickbaseAuthenticationError, KickbaseRequestError, KickbaseDataError],
        ids=lambda c: c.__name__,
    )
    def test_non_http_errors_are_not_api_errors(self, cls):
        # A generic "except KickbaseAPIError: retry" must not swallow these.
        assert not issubclass(cls, KickbaseAPIError)


class TestStatusCodes:
    def test_not_found_is_404(self):
        error = KickbaseNotFoundError("Not found", endpoint="/x", response_data={"e": 1})
        assert error.status_code == 404
        assert error.endpoint == "/x"
        assert error.response_data == {"e": 1}

    def test_rate_limit_is_429_with_retry_after(self):
        error = KickbaseRateLimitError("Slow down", retry_after=30, endpoint="/x")
        assert error.status_code == 429
        assert error.retry_after == 30
        assert error.endpoint == "/x"

    @pytest.mark.parametrize(
        "code, expected",
        [(500, True), (503, True), (404, False), (429, False), (None, False)],
    )
    def test_is_server_error(self, code, expected):
        assert KickbaseAPIError("x", status_code=code).is_server_error is expected


class TestParseRetryAfter:
    @pytest.mark.parametrize(
        "value, expected",
        [(None, None), ("", None), ("120", 120), (" 30 ", 30), ("garbage", None)],
    )
    def test_simple_values(self, value, expected):
        assert parse_retry_after(value) == expected

    def test_http_date_in_future(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=120)
        result = parse_retry_after(format_datetime(when, usegmt=True))
        assert result is not None
        assert 115 <= result <= 120

    def test_http_date_in_past_is_zero(self):
        assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0
