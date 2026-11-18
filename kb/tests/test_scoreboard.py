"""Tests for the scoreboard entry point (config, fetch, render, write)."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from kb import scoreboard
from kb.errors import KickbaseAuthenticationError
from kb.scoreboard import (
    Config,
    ConfigError,
    fetch_scores,
    render,
    write_atomic,
)

ENV = "USER=me@example.com\nPW=secret\nLEAGUE_ID=42\nOUTPUT_PATH={out}\n"


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(ENV.format(out=tmp_path / "index.html"))
    return path


# --- Config ------------------------------------------------------------------


class TestConfig:
    def test_load(self, env_file, tmp_path):
        config = Config.load(env_file)
        assert config.email == "me@example.com"
        assert config.password == "secret"
        assert config.league_id == "42"
        assert config.output_path == tmp_path / "index.html"

    def test_repr_hides_password(self, env_file):
        assert "secret" not in repr(Config.load(env_file))

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            Config.load(tmp_path / "nope.env")

    def test_missing_and_empty_keys_are_listed(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("USER=me@example.com\nPW=\n")
        with pytest.raises(ConfigError, match="PW, LEAGUE_ID, OUTPUT_PATH"):
            Config.load(path)


# --- fetch / render ------------------------------------------------------------


def test_fetch_scores_sorted_descending():
    client = Mock()
    client.league_ranking.return_value = {
        "us": [{"n": "Ann", "mdp": 10}, {"n": "Bob", "mdp": 30}, {"n": "Cy", "mdp": 20}]
    }
    assert fetch_scores(client, "42") == [("Bob", 30), ("Cy", 20), ("Ann", 10)]
    client.league_ranking.assert_called_once_with("42")


def test_fetch_scores_keeps_duplicate_names():
    client = Mock()
    client.league_ranking.return_value = {
        "us": [{"n": "Max", "mdp": 1}, {"n": "Max", "mdp": 2}]
    }
    assert len(fetch_scores(client, "42")) == 2


def test_render_lists_scores_in_order_with_timestamp():
    html = render([("Bob", 30), ("Ann", 10)], datetime(2026, 11, 17, 20, 5))
    assert html.index("Bob") < html.index("Ann")
    assert "17.11.26 20:05" in html


def test_render_escapes_names():
    html = render([("<script>x</script>", 1)], datetime(2026, 1, 1))
    assert "<script>x" not in html
    assert "&lt;script&gt;" in html


# --- write_atomic ------------------------------------------------------------


class TestWriteAtomic:
    def test_writes_readable_file(self, tmp_path):
        target = tmp_path / "index.html"
        write_atomic(target, "<p>hi</p>")
        assert target.read_text() == "<p>hi</p>"
        assert target.stat().st_mode & 0o777 == 0o644

    def test_replaces_existing_file(self, tmp_path):
        target = tmp_path / "index.html"
        target.write_text("old")
        write_atomic(target, "new")
        assert target.read_text() == "new"

    def test_failure_keeps_old_file_and_cleans_up(self, tmp_path):
        target = tmp_path / "index.html"
        target.write_text("old")
        with (
            patch("kb.scoreboard.os.replace", side_effect=OSError("disk")),
            pytest.raises(OSError),
        ):
            write_atomic(target, "new")
        assert target.read_text() == "old"
        assert os.listdir(tmp_path) == ["index.html"]


# --- main ----------------------------------------------------------------------


class TestMain:
    @pytest.fixture(autouse=True)
    def _in_tmp(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

    def test_success_writes_page(self, env_file, tmp_path):
        with patch.object(scoreboard, "KickbaseClient") as client_cls:
            client = client_cls.return_value
            client.league_ranking.return_value = {"us": [{"n": "Ann", "mdp": 7}]}
            assert scoreboard.main() == 0

        client_cls.assert_called_once_with("me@example.com", "secret")
        client.login.assert_called_once()
        assert "Ann" in (tmp_path / "index.html").read_text()

    def test_api_error_returns_1_and_writes_nothing(self, env_file, tmp_path):
        with patch.object(scoreboard, "KickbaseClient") as client_cls:
            client_cls.return_value.login.side_effect = KickbaseAuthenticationError(
                "bad"
            )
            assert scoreboard.main() == 1
        assert not (tmp_path / "index.html").exists()

    def test_missing_config_returns_2(self):
        assert scoreboard.main() == 2
