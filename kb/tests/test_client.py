"""Tests for KickbaseClient (kb/client.py)."""

from __future__ import annotations

import json
import pickle
from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse

from kb.client import (
    BASE_URL,
    LOGIN_PATH,
    MAX_ERROR_BODY_CHARS,
    MAX_RETRY_AFTER_SECONDS,
    RETRY,
    TIMEOUT,
    KickbaseClient,
    _CappedRetry,
)
from kb.errors import (
    KickbaseAPIError,
    KickbaseAuthenticationError,
    KickbaseDataError,
    KickbaseNotFoundError,
    KickbaseRateLimitError,
    KickbaseRequestError,
)

EMAIL = "test@example.com"
PASSWORD = "password123"


# --- helpers ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_time():
    """Replace the time module in kb.client so the throttle never really sleeps.

    The throttle itself is tested in test_rate_limit.py.
    """
    clock = Mock()
    clock.monotonic.return_value = 1000.0
    with patch("kb.client.time", clock):
        yield clock


def make_response(
    status_code: int = 200,
    json_body: Any = None,
    *,
    text: str | None = None,
    headers: dict[str, str] | None = None,
) -> Mock:
    """Build a fake requests.Response.

    Pass json_body for a JSON response, text for a non-JSON body (e.g. an
    HTML error page), or neither for an empty body.
    """
    resp = Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    if text is not None:
        resp.content = text.encode()
        resp.text = text
        resp.json.side_effect = ValueError("not JSON")
    elif json_body is not None:
        raw = json.dumps(json_body)
        resp.content = raw.encode()
        resp.text = raw
        resp.json.return_value = json_body
    else:
        resp.content = b""
        resp.text = ""
        resp.json.side_effect = ValueError("empty body")
    return resp


def login_response(token: str = "abc") -> Mock:
    return make_response(200, {"tkn": token, "u": {"id": "1", "name": "Test"}})


def new_client() -> KickbaseClient:
    return KickbaseClient(EMAIL, PASSWORD)


def logged_in_client() -> KickbaseClient:
    """Client that counts as logged in, without a network call."""
    client = new_client()
    client._token = "test_token"
    return client


# --- init ------------------------------------------------------------------


class TestKickbaseClientInit:
    def test_initial_state(self):
        client = new_client()
        assert client._email == EMAIL
        assert client._password == PASSWORD
        assert client._token is None
        assert client.user is None

    def test_init_sets_accept_header(self):
        """Client should ask for JSON on every request."""
        client = new_client()
        assert client._session.headers.get("Accept") == "application/json"
        # Content-Type is set per request by requests when json= is used,
        # not session-wide (GET requests have no body).
        assert "Content-Type" not in client._session.headers

    def test_no_authorization_header_before_login(self):
        assert "Authorization" not in new_client()._session.headers

    def test_retry_adapter_mounted(self):
        adapter = new_client()._session.get_adapter(BASE_URL)
        assert isinstance(adapter, HTTPAdapter)
        assert adapter.max_retries is RETRY

    def test_repr_hides_password(self):
        r = repr(new_client())
        assert EMAIL in r
        assert PASSWORD not in r
        assert "logged_in=False" in r

    def test_repr_shows_logged_in(self):
        assert "logged_in=True" in repr(logged_in_client())

    def test_cannot_be_pickled(self):
        with pytest.raises(TypeError, match="must not be pickled"):
            pickle.dumps(new_client())


# --- retry configuration ---------------------------------------------------


class TestRetryConfig:
    def test_retries_transient_statuses(self):
        for code in (429, 500, 502, 503, 504):
            assert code in RETRY.status_forcelist

    @pytest.mark.parametrize("code", [400, 401, 403, 404])
    def test_does_not_retry_client_errors(self, code):
        assert code not in RETRY.status_forcelist

    def test_retry_is_capped_type(self):
        assert isinstance(RETRY, _CappedRetry)

    def test_retry_after_is_capped(self):
        resp = HTTPResponse(status=429, headers={"Retry-After": "3600"})
        assert _CappedRetry(total=1).get_retry_after(resp) == MAX_RETRY_AFTER_SECONDS

    def test_short_retry_after_unchanged(self):
        resp = HTTPResponse(status=429, headers={"Retry-After": "10"})
        assert _CappedRetry(total=1).get_retry_after(resp) == 10

    def test_missing_retry_after(self):
        resp = HTTPResponse(status=503)
        assert _CappedRetry(total=1).get_retry_after(resp) is None


# --- login -----------------------------------------------------------------


class TestLogin:
    def test_success_sets_token_header_and_user(self):
        client = new_client()
        with patch.object(
            client._session, "request", return_value=login_response("abc")
        ):
            data = client.login()

        assert data["tkn"] == "abc"
        assert client._token == "abc"
        assert client._session.headers["Authorization"] == "Bearer abc"
        assert client.user == {"id": "1", "name": "Test"}

    def test_sends_credentials(self):
        client = new_client()
        with patch.object(
            client._session, "request", return_value=login_response()
        ) as mock_request:
            client.login()

        args, kwargs = mock_request.call_args
        assert args == ("POST", f"{BASE_URL}{LOGIN_PATH}")
        assert kwargs["json"] == {
            "em": EMAIL,
            "pass": PASSWORD,
            "loy": False,
            "rep": {},
        }
        assert kwargs["timeout"] == TIMEOUT

    def test_login_sends_json_content_type(self):
        """The login POST must go out as JSON with the credentials."""
        client = new_client()
        # Patch send(), not request(): send() receives the fully prepared
        # request, so we see the headers and body exactly as they'd go out.
        with patch.object(
            client._session, "send", return_value=login_response()
        ) as mock_send:
            client.login()

        prepared = mock_send.call_args.args[0]
        assert prepared.method == "POST"
        assert prepared.headers["Content-Type"] == "application/json"
        assert json.loads(prepared.body)["em"] == EMAIL

    def test_wrong_credentials(self):
        client = new_client()
        with (
            patch.object(client._session, "request", return_value=make_response(401)),
            pytest.raises(KickbaseAuthenticationError, match="wrong email") as exc,
        ):
            client.login()

        assert exc.value.endpoint == LOGIN_PATH
        assert client._token is None
        assert "Authorization" not in client._session.headers

    def test_server_error_keeps_status_and_html_body(self):
        client = new_client()
        html = "<html>502 Bad Gateway</html>"
        with (
            patch.object(
                client._session, "request", return_value=make_response(502, text=html)
            ),
            pytest.raises(KickbaseAPIError) as exc,
        ):
            client.login()

        assert exc.value.status_code == 502
        assert exc.value.response_data == html
        assert exc.value.endpoint == LOGIN_PATH

    def test_rate_limited(self):
        client = new_client()
        resp = make_response(429, {"err": "slow down"}, headers={"Retry-After": "30"})
        with (
            patch.object(client._session, "request", return_value=resp),
            pytest.raises(KickbaseRateLimitError) as exc,
        ):
            client.login()

        assert exc.value.retry_after == 30
        assert exc.value.response_data == {"err": "slow down"}

    def test_body_not_json(self):
        client = new_client()
        with (
            patch.object(
                client._session,
                "request",
                return_value=make_response(200, text="<html>"),
            ),
            pytest.raises(KickbaseDataError, match="not valid JSON") as exc,
        ):
            client.login()
        assert exc.value.response_data == "<html>"

    def test_body_not_an_object(self):
        client = new_client()
        with (
            patch.object(
                client._session, "request", return_value=make_response(200, ["tkn"])
            ),
            pytest.raises(KickbaseDataError, match="got list"),
        ):
            client.login()

    def test_missing_token(self):
        client = new_client()
        with (
            patch.object(
                client._session, "request", return_value=make_response(200, {"u": {}})
            ),
            pytest.raises(KickbaseDataError, match="no token") as exc,
        ):
            client.login()
        assert "'u'" in str(exc.value)
        assert client._token is None

    @pytest.mark.parametrize("token", ["", None, 123])
    def test_invalid_token(self, token):
        client = new_client()
        with (
            patch.object(
                client._session,
                "request",
                return_value=make_response(200, {"tkn": token}),
            ),
            pytest.raises(KickbaseDataError),
        ):
            client.login()

    def test_timeout(self):
        client = new_client()
        cause = requests.Timeout("too slow")
        with (
            patch.object(client._session, "request", side_effect=cause),
            pytest.raises(KickbaseRequestError, match="timed out") as exc,
        ):
            client.login()
        assert exc.value.__cause__ is cause
        assert exc.value.endpoint == LOGIN_PATH

    def test_connection_error(self):
        client = new_client()
        with (
            patch.object(
                client._session, "request", side_effect=requests.ConnectionError("down")
            ),
            pytest.raises(KickbaseRequestError, match="Connection error"),
        ):
            client.login()


# --- get -------------------------------------------------------------------


class TestGet:
    def test_requires_login(self):
        client = new_client()
        with (
            patch.object(client._session, "request") as mock_request,
            pytest.raises(KickbaseAuthenticationError, match="Not logged in") as exc,
        ):
            client.get("/v4/test")
        mock_request.assert_not_called()
        assert exc.value.endpoint == "/v4/test"

    def test_success(self):
        client = logged_in_client()
        with patch.object(
            client._session, "request", return_value=make_response(200, {"data": 1})
        ) as mock_request:
            result = client.get("/v4/test", params={"a": "b"})

        assert result == {"data": 1}
        args, kwargs = mock_request.call_args
        assert args == ("GET", f"{BASE_URL}/v4/test")
        assert kwargs["params"] == {"a": "b"}
        assert kwargs["timeout"] == TIMEOUT

    def test_not_found(self):
        client = logged_in_client()
        with (
            patch.object(
                client._session,
                "request",
                return_value=make_response(404, {"err": "nope"}),
            ),
            pytest.raises(KickbaseNotFoundError) as exc,
        ):
            client.get("/v4/leagues/x/overview")

        assert exc.value.status_code == 404
        assert exc.value.endpoint == "/v4/leagues/x/overview"
        assert exc.value.response_data == {"err": "nope"}

    def test_rate_limited_with_retry_after(self):
        client = logged_in_client()
        resp = make_response(429, headers={"Retry-After": "45"})
        with (
            patch.object(client._session, "request", return_value=resp),
            pytest.raises(KickbaseRateLimitError) as exc,
        ):
            client.get("/v4/test")
        assert exc.value.retry_after == 45
        assert exc.value.status_code == 429

    def test_rate_limited_without_retry_after(self):
        client = logged_in_client()
        with (
            patch.object(client._session, "request", return_value=make_response(429)),
            pytest.raises(KickbaseRateLimitError) as exc,
        ):
            client.get("/v4/test")
        assert exc.value.retry_after is None

    def test_html_gateway_error_keeps_status(self):
        """Regression: a non-JSON 502 must not turn into a request error."""
        client = logged_in_client()
        html = "<html><body>502 Bad Gateway</body></html>"
        with (
            patch.object(
                client._session, "request", return_value=make_response(502, text=html)
            ),
            pytest.raises(KickbaseAPIError) as exc,
        ):
            client.get("/v4/test")

        assert type(exc.value) is KickbaseAPIError
        assert exc.value.status_code == 502
        assert exc.value.is_server_error
        assert exc.value.response_data == html

    def test_error_with_empty_body(self):
        client = logged_in_client()
        with (
            patch.object(client._session, "request", return_value=make_response(500)),
            pytest.raises(KickbaseAPIError) as exc,
        ):
            client.get("/v4/test")
        assert exc.value.status_code == 500
        assert exc.value.response_data is None

    def test_long_error_body_is_truncated(self):
        client = logged_in_client()
        with (
            patch.object(
                client._session,
                "request",
                return_value=make_response(503, text="x" * 5000),
            ),
            pytest.raises(KickbaseAPIError) as exc,
        ):
            client.get("/v4/test")
        assert exc.value.response_data == "x" * MAX_ERROR_BODY_CHARS

    def test_success_body_not_json(self):
        client = logged_in_client()
        with (
            patch.object(
                client._session, "request", return_value=make_response(200, text="oops")
            ),
            pytest.raises(KickbaseDataError, match="not valid JSON") as exc,
        ):
            client.get("/v4/test")
        assert exc.value.endpoint == "/v4/test"

    def test_success_body_empty(self):
        client = logged_in_client()
        with (
            patch.object(client._session, "request", return_value=make_response(200)),
            pytest.raises(KickbaseDataError),
        ):
            client.get("/v4/test")

    def test_success_body_not_an_object(self):
        client = logged_in_client()
        with (
            patch.object(
                client._session, "request", return_value=make_response(200, [1, 2])
            ),
            pytest.raises(KickbaseDataError, match="got list"),
        ):
            client.get("/v4/test")

    def test_timeout(self):
        client = logged_in_client()
        cause = requests.Timeout("slow")
        with (
            patch.object(client._session, "request", side_effect=cause),
            pytest.raises(KickbaseRequestError, match="timed out") as exc,
        ):
            client.get("/v4/test")
        assert exc.value.__cause__ is cause
        assert exc.value.endpoint == "/v4/test"

    def test_connection_error(self):
        client = logged_in_client()
        with (
            patch.object(
                client._session, "request", side_effect=requests.ConnectionError("down")
            ),
            pytest.raises(KickbaseRequestError, match="Connection error"),
        ):
            client.get("/v4/test")

    def test_other_request_exception(self):
        client = logged_in_client()
        with (
            patch.object(
                client._session,
                "request",
                side_effect=requests.TooManyRedirects("loop"),
            ),
            pytest.raises(KickbaseRequestError, match="failed"),
        ):
            client.get("/v4/test")


# --- re-authentication -----------------------------------------------------


class TestReauthentication:
    def test_relogin_on_401_then_retry(self):
        client = logged_in_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                make_response(401),
                login_response("fresh"),
                make_response(200, {"data": "ok"}),
            ],
        ) as mock_request:
            result = client.get("/v4/test")

        assert result == {"data": "ok"}
        assert mock_request.call_count == 3
        assert mock_request.call_args_list[1].args[0] == "POST"
        assert client._token == "fresh"
        assert client._session.headers["Authorization"] == "Bearer fresh"

    def test_second_401_raises_without_looping(self):
        client = logged_in_client()
        with (
            patch.object(
                client._session,
                "request",
                side_effect=[make_response(401), login_response(), make_response(401)],
            ) as mock_request,
            pytest.raises(KickbaseAuthenticationError, match="after re-login"),
        ):
            client.get("/v4/test")
        assert mock_request.call_count == 3

    def test_failed_relogin_raises_login_error(self):
        client = logged_in_client()
        with (
            patch.object(
                client._session,
                "request",
                side_effect=[make_response(401), make_response(401)],
            ) as mock_request,
            pytest.raises(KickbaseAuthenticationError, match="wrong email"),
        ):
            client.get("/v4/test")
        assert mock_request.call_count == 2


# --- endpoint wrappers -----------------------------------------------------


class TestEndpointWrappers:
    @pytest.mark.parametrize(
        "method, args, path",
        [
            ("leagues", (), "/v4/leagues/selection"),
            ("league_overview", ("L1",), "/v4/leagues/L1/overview"),
            ("league_me", ("L1",), "/v4/leagues/L1/me"),
            ("league_budget", ("L1",), "/v4/leagues/L1/me/budget"),
            ("league_ranking", ("L1",), "/v4/leagues/L1/ranking"),
            ("squad", ("L1",), "/v4/leagues/L1/squad"),
            ("market", ("L1",), "/v4/leagues/L1/market"),
            (
                "player_market_value",
                ("L1", "P1"),
                "/v4/leagues/L1/players/P1/marketValue/365",
            ),
            (
                "player_market_value",
                ("L1", "P1", 30),
                "/v4/leagues/L1/players/P1/marketValue/30",
            ),
            (
                "player_performance",
                ("L1", "P1"),
                "/v4/leagues/L1/players/P1/performance",
            ),
            ("team_profile", ("T1",), "/v4/competitions/1/teams/T1/teamprofile"),
            ("team_profile", ("T1", "2"), "/v4/competitions/2/teams/T1/teamprofile"),
        ],
    )
    def test_calls_get_with_path(self, method, args, path):
        client = logged_in_client()
        with patch.object(client, "get", return_value={"ok": True}) as mock_get:
            result = getattr(client, method)(*args)
        mock_get.assert_called_once_with(path)
        assert result == {"ok": True}
