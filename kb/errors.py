"""Exception hierarchy for the Kickbase API client.

All exceptions inherit from KickbaseError, so callers can catch every
client failure with a single ``except KickbaseError``:

    KickbaseError
    ├── KickbaseAuthenticationError   bad credentials, rejected/expired token
    ├── KickbaseRequestError          network failure, no usable HTTP response
    ├── KickbaseDataError             response arrived but content is unusable
    └── KickbaseAPIError              API answered with an HTTP error status
        ├── KickbaseNotFoundError     404
        └── KickbaseRateLimitError    429
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

__all__ = [
    "KickbaseAPIError",
    "KickbaseAuthenticationError",
    "KickbaseDataError",
    "KickbaseError",
    "KickbaseNotFoundError",
    "KickbaseRateLimitError",
    "KickbaseRequestError",
    "parse_retry_after",
]


class KickbaseError(Exception):
    """Base exception for all Kickbase client errors.

    Catch this to handle every failure raised by the client in one place.

    Attributes:
        message: The human-readable error message.
        endpoint: The API path involved, if known.
    """

    def __init__(self, message: str, endpoint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint

    def __repr__(self) -> str:
        fields = ", ".join(
            f"{k}={v!r}"
            for k, v in self.__dict__.items()
            if k != "message" and v is not None
        )
        extra = f", {fields}" if fields else ""
        return f"{self.__class__.__name__}({self.message!r}{extra})"


class KickbaseAuthenticationError(KickbaseError):
    """Raised when authentication fails or the session is no longer valid.

    This includes:
        - Wrong email or password during login
        - A token rejected by the API (HTTP 401), e.g. an expired session
        - Calling an endpoint before ``login()``

    Example:
        try:
            client.login()
        except KickbaseAuthenticationError as e:
            print(f"Authentication failed: {e}")
    """


class KickbaseRequestError(KickbaseError):
    """Raised when a request fails before a usable HTTP response arrives.

    This includes:
        - Connection and read timeouts
        - DNS failures, refused or dropped connections
        - Retries exhausted at the transport level

    These are usually transient, so retrying later is reasonable.

    Example:
        try:
            client.leagues()
        except KickbaseRequestError as e:
            print(f"Network problem: {e}")
    """


class KickbaseDataError(KickbaseError):
    """Raised when a response arrives but its content is unusable.

    This includes:
        - A body that is not valid JSON
        - Missing required keys (e.g. no token in the login response)
        - Unexpected data types

    Attributes:
        response_data: The raw (possibly truncated) response, if available.

    Example:
        try:
            data = client.league_overview(league_id)
        except KickbaseDataError as e:
            print(f"Invalid data received: {e}")
    """

    def __init__(
        self,
        message: str,
        endpoint: str | None = None,
        response_data: Any | None = None,
    ) -> None:
        super().__init__(message, endpoint=endpoint)
        self.response_data = response_data


class KickbaseAPIError(KickbaseError):
    """Raised when the API answers with an HTTP error status.

    Attributes:
        status_code: The HTTP status code returned by the API.
        endpoint: The API path that was called.
        response_data: The parsed error body, raw text, or None.

    Example:
        try:
            client.league_overview(league_id)
        except KickbaseAPIError as e:
            print(f"API error at {e.endpoint}: {e}")
            if e.is_server_error:
                ...  # transient, safe to retry later
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        endpoint: str | None = None,
        response_data: Any | None = None,
    ) -> None:
        super().__init__(message, endpoint=endpoint)
        self.status_code = status_code
        self.response_data = response_data

    @property
    def is_server_error(self) -> bool:
        """True for 5xx responses, which are usually worth retrying."""
        return self.status_code is not None and 500 <= self.status_code < 600


class KickbaseNotFoundError(KickbaseAPIError):
    """Raised when the API returns 404 Not Found.

    The requested resource (league, player, team, ...) does not exist
    or is not visible to this account.

    Example:
        try:
            client.league_overview("invalid-league-id")
        except KickbaseNotFoundError as e:
            print(f"Resource not found: {e.endpoint}")
    """

    def __init__(
        self,
        message: str,
        endpoint: str | None = None,
        response_data: Any | None = None,
    ) -> None:
        super().__init__(
            message, status_code=404, endpoint=endpoint, response_data=response_data
        )


class KickbaseRateLimitError(KickbaseAPIError):
    """Raised when the API returns 429 Too Many Requests.

    Attributes:
        retry_after: Seconds to wait before retrying, if the server said so.

    Example:
        try:
            client.leagues()
        except KickbaseRateLimitError as e:
            time.sleep(e.retry_after or 60)
    """

    def __init__(
        self,
        message: str,
        retry_after: int | None = None,
        endpoint: str | None = None,
        response_data: Any | None = None,
    ) -> None:
        super().__init__(
            message, status_code=429, endpoint=endpoint, response_data=response_data
        )
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> int | None:
    """Parse a Retry-After header into whole seconds.

    Handles both forms allowed by RFC 9110: delay-seconds ("120") and an
    HTTP date ("Wed, 21 Oct 2026 07:28:00 GMT"). Returns None when the
    header is missing or unparseable; past dates give 0.
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0, int((when - datetime.now(UTC)).total_seconds()))
