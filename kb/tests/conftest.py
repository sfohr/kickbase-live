"""Shared pytest configuration for the kb test suite."""

import pytest
import requests


@pytest.fixture(autouse=True)
def _block_real_http(monkeypatch):
    """Fail any test that would send a real HTTP request.

    Tests must mock session.request / session.send (or client.get).
    If a patch goes stale, this turns a silent live API call into a
    clear test failure.
    """

    def blocked(self, request, **kwargs):
        raise RuntimeError(
            f"Unmocked HTTP request in test: {request.method} {request.url}"
        )

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", blocked)
