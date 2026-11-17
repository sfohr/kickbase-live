"""Minimal client for the unofficial Kickbase v4 API.

Community docs: https://github.com/kevinskyba/kickbase-api-doc
Adapted from: https://github.com/anton-schedel/kickbase-advisor/
"""

from __future__ import annotations

import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from kb.errors import (
    KickbaseAPIError,
    KickbaseAuthenticationError,
    KickbaseDataError,
    KickbaseNotFoundError,
    KickbaseRateLimitError,
    KickbaseRequestError,
    parse_retry_after,
)

BASE_URL = "https://api.kickbase.com"
LOGIN_PATH = "/v4/user/login"

# Delay between requests so we behave like a normal app user, not a crawler.
REQUEST_DELAY_SECONDS = 0.5
# (connect, read): fail fast when the host is unreachable, but give slow
# responses time to arrive.
TIMEOUT = (5, 30)
# Upper bound for a server-sent Retry-After, so one call can't block for hours.
MAX_RETRY_AFTER_SECONDS = 60
# Length of a non-JSON body kept on errors for debugging.
MAX_ERROR_BODY_CHARS = 500


class _CappedRetry(Retry):
    """Retry that honours Retry-After, but never waits longer than the cap."""

    def get_retry_after(self, response: Any) -> float | None:
        retry_after = super().get_retry_after(response)
        if retry_after is None:
            return None
        return min(retry_after, MAX_RETRY_AFTER_SECONDS)


# A full run makes several hundred requests, so an occasional dropped
# connection is normal and must not abort the pipeline.
# POST is retried only because login is idempotent. Never send bids, sales
# or other state-changing POSTs through this session: a retried timeout
# could submit them twice. Use a separate session without retries for those.
RETRY = _CappedRetry(
    total=4,
    backoff_factor=1.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    raise_on_status=False,
)


def _safe_json(resp: requests.Response) -> Any:
    """Parse an error body as JSON, falling back to truncated text.

    Gateway errors (502/503) often return HTML. Parsing must never raise
    here, or the real status code would be hidden behind a decode error.
    """
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text[:MAX_ERROR_BODY_CHARS] or None


class KickbaseClient:
    """Client for the Kickbase v4 API.

    All endpoint methods go through :meth:`get` and raise the same
    exceptions; see its docstring. Every exception is a subclass of
    ``kb.errors.KickbaseError``.
    """

    def __init__(self, email: str, password: str) -> None:
        """Initialize the Kickbase client with credentials.

        The password is kept in memory so an expired session can be
        renewed automatically. The client refuses to be pickled, and its
        repr never shows the password.

        Args:
            email: The user's email address for authentication.
            password: The user's password for authentication.
        """
        self._email: str = email
        self._password: str = password
        self._session: requests.Session = requests.Session()
        self._session.mount("https://", HTTPAdapter(max_retries=RETRY))
        # Content-Type is set per request by requests when json= is used.
        self._session.headers.update({"Accept": "application/json"})
        self._token: str | None = None
        self._last_request_at: float = 0.0
        self.user: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(email={self._email!r}, "
            f"logged_in={self._token is not None})"
        )

    def __getstate__(self) -> Any:
        raise TypeError(
            "KickbaseClient holds credentials and must not be pickled."
        )

    # --- transport ---------------------------------------------------------

    def _send(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Send a throttled request and turn network failures into KickbaseRequestError.

        Only transport errors are handled here. Status codes and body
        parsing are handled by the caller, so the two kinds of failure
        never mix.
        """
        wait = REQUEST_DELAY_SECONDS - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

        try:
            return self._session.request(
                method, f"{BASE_URL}{path}", timeout=TIMEOUT, **kwargs
            )
        except requests.Timeout as e:
            raise KickbaseRequestError(
                f"Request to {path} timed out.", endpoint=path
            ) from e
        except requests.ConnectionError as e:
            raise KickbaseRequestError(
                f"Connection error when calling {path}.", endpoint=path
            ) from e
        except requests.RequestException as e:
            raise KickbaseRequestError(
                f"Request to {path} failed: {e}", endpoint=path
            ) from e
        finally:
            # Also set on failure, so the next call still waits.
            self._last_request_at = time.monotonic()

    @staticmethod
    def _raise_for_status(resp: requests.Response, path: str) -> None:
        """Raise the matching KickbaseAPIError subclass for an error status.

        401 is handled by the callers, because login and get treat it
        differently.
        """
        code = resp.status_code
        if code < 400:
            return
        data = _safe_json(resp)
        if code == 404:
            raise KickbaseNotFoundError(
                f"Resource not found: {path}", endpoint=path, response_data=data
            )
        if code == 429:
            raise KickbaseRateLimitError(
                f"Rate limit exceeded on {path}",
                retry_after=parse_retry_after(resp.headers.get("Retry-After")),
                endpoint=path,
                response_data=data,
            )
        raise KickbaseAPIError(
            f"API error on {path}: {code}",
            status_code=code,
            endpoint=path,
            response_data=data,
        )

    @staticmethod
    def _parse_json(resp: requests.Response, path: str) -> dict[str, Any]:
        """Parse a successful response body, which must be a JSON object."""
        try:
            data = resp.json()
        except ValueError as e:
            raise KickbaseDataError(
                f"Response from {path} is not valid JSON.",
                endpoint=path,
                response_data=resp.text[:MAX_ERROR_BODY_CHARS] or None,
            ) from e
        if not isinstance(data, dict):
            raise KickbaseDataError(
                f"Expected a JSON object from {path}, got {type(data).__name__}.",
                endpoint=path,
                response_data=data,
            )
        return data

    # --- authentication ----------------------------------------------------

    def login(self) -> dict[str, Any]:
        """Authenticate with the Kickbase API.

        Returns:
            The full login response containing token and user data.

        Raises:
            KickbaseAuthenticationError: Wrong email or password (HTTP 401).
            KickbaseRateLimitError: Too many login attempts (HTTP 429).
            KickbaseAPIError: Any other HTTP error status.
            KickbaseDataError: The response is not a JSON object or has no token.
            KickbaseRequestError: Network failure or timeout.
        """
        resp = self._send(
            "POST",
            LOGIN_PATH,
            json={"em": self._email, "pass": self._password, "loy": False, "rep": {}},
        )
        if resp.status_code == 401:
            raise KickbaseAuthenticationError(
                "Login failed: wrong email or password.", endpoint=LOGIN_PATH
            )
        self._raise_for_status(resp, LOGIN_PATH)
        data = self._parse_json(resp, LOGIN_PATH)

        token = data.get("tkn")
        if not isinstance(token, str) or not token:
            raise KickbaseDataError(
                f"Login response had no token. Keys: {list(data.keys())}",
                endpoint=LOGIN_PATH,
            )
        self._token = token
        self._session.headers["Authorization"] = f"Bearer {token}"
        self.user = data.get("u")
        return data

    # --- generic GET -------------------------------------------------------

    def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make an authenticated GET request to the Kickbase API.

        If the token is rejected (e.g. it expired during a long run), the
        client logs in again once and repeats the request.

        Args:
            path: The API endpoint path (e.g., "/v4/leagues/selection").
            params: Optional query parameters to include in the request.

        Returns:
            The parsed JSON object from the response.

        Raises:
            KickbaseAuthenticationError: Not logged in, or the token is still
                rejected after re-login, or re-login itself failed.
            KickbaseNotFoundError: The resource does not exist (HTTP 404).
            KickbaseRateLimitError: Rate limit still exceeded after the
                transport-level retries (HTTP 429).
            KickbaseAPIError: Any other HTTP error status.
            KickbaseDataError: The body is not a JSON object.
            KickbaseRequestError: Network failure or timeout.
        """
        if not self._token:
            raise KickbaseAuthenticationError(
                "Not logged in — call login() first.", endpoint=path
            )

        resp = self._send("GET", path, params=params)
        if resp.status_code == 401:
            # Re-login once and retry. A second 401 means it's not just expiry.
            self.login()
            resp = self._send("GET", path, params=params)
            if resp.status_code == 401:
                raise KickbaseAuthenticationError(
                    f"Token rejected on {path} even after re-login.", endpoint=path
                )

        self._raise_for_status(resp, path)
        return self._parse_json(resp, path)

    # --- endpoint wrappers (all raise as documented on get()) --------------

    def leagues(self) -> dict[str, Any]:
        """Get the list of leagues the user is part of."""
        return self.get("/v4/leagues/selection")

    def league_overview(self, league_id: str) -> dict[str, Any]:
        """Get the overview of a league."""
        return self.get(f"/v4/leagues/{league_id}/overview")

    def league_me(self, league_id: str) -> dict[str, Any]:
        """Get the user's own data in a league."""
        return self.get(f"/v4/leagues/{league_id}/me")

    def league_budget(self, league_id: str) -> dict[str, Any]:
        """Get the user's budget in a league."""
        return self.get(f"/v4/leagues/{league_id}/me/budget")

    def league_ranking(self, league_id: str) -> dict[str, Any]:
        """Get the ranking of a league."""
        return self.get(f"/v4/leagues/{league_id}/ranking")

    def squad(self, league_id: str) -> dict[str, Any]:
        """Get the user's squad in a league."""
        return self.get(f"/v4/leagues/{league_id}/squad")

    def market(self, league_id: str) -> dict[str, Any]:
        """Get the transfer market of a league."""
        return self.get(f"/v4/leagues/{league_id}/market")

    def player_market_value(
        self, league_id: str, player_id: str, timeframe: int = 365
    ) -> dict[str, Any]:
        """Get a player's market value history.

        Args:
            league_id: The league the player is viewed in.
            player_id: The player's ID.
            timeframe: Number of days of history. Defaults to 365.
        """
        return self.get(
            f"/v4/leagues/{league_id}/players/{player_id}/marketValue/{timeframe}"
        )

    def player_performance(self, league_id: str, player_id: str) -> dict[str, Any]:
        """Get a player's performance data."""
        return self.get(f"/v4/leagues/{league_id}/players/{player_id}/performance")

    def team_profile(
        self, team_id: str, competition_id: str = "1"
    ) -> dict[str, Any]:
        """Get a club's full squad with values and start ratings.

        This is the basis for comparing a player against the team-mates
        he would displace.

        Args:
            team_id: The club's ID.
            competition_id: The competition. Defaults to "1" (Bundesliga).
        """
        return self.get(
            f"/v4/competitions/{competition_id}/teams/{team_id}/teamprofile"
        )
