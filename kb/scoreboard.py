"""Render the live league scoreboard to a static HTML page.

Run with ``uv run kickbase`` from the directory that holds the ``.env``
file (see ``.env.example``). Exits non-zero on failure so cron can report it.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from jinja2 import Environment, PackageLoader

from kb.client import KickbaseClient
from kb.errors import KickbaseError

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Berlin")
TEMPLATE_NAME = "index.html.jinja"
REQUIRED_KEYS = ("USER", "PW", "LEAGUE_ID", "OUTPUT_PATH")

_env = Environment(loader=PackageLoader("kb", "templates"), autoescape=True)


class ConfigError(ValueError):
    """Raised when the .env file is missing required values."""


@dataclass(frozen=True)
class Config:
    email: str
    password: str = field(repr=False)  # keep it out of logs and tracebacks
    league_id: str
    output_path: Path

    @classmethod
    def load(cls, env_file: Path = Path(".env")) -> Config:
        """Read and validate the config from a .env file.

        Raises:
            ConfigError: The file is missing or a required key is empty.
        """
        if not env_file.is_file():
            raise ConfigError(f"Config file not found: {env_file.resolve()}")
        values = dotenv_values(env_file)
        missing = [key for key in REQUIRED_KEYS if not values.get(key)]
        if missing:
            raise ConfigError(f"Missing values in {env_file}: {', '.join(missing)}")
        return cls(
            email=str(values["USER"]),
            password=str(values["PW"]),
            league_id=str(values["LEAGUE_ID"]),
            output_path=Path(str(values["OUTPUT_PATH"])),
        )


def fetch_scores(client: KickbaseClient, league_id: str) -> list[tuple[str, Any]]:
    """Return (manager name, matchday points) pairs, highest score first."""
    ranking = client.league_ranking(league_id)
    scores = [(user["n"], user["mdp"]) for user in ranking["us"]]
    return sorted(scores, key=lambda item: item[1], reverse=True)


def render(scores: list[tuple[str, Any]], now: datetime) -> str:
    """Render the scoreboard page."""
    return _env.get_template(TEMPLATE_NAME).render(
        scores=scores, now=now.strftime("%d.%m.%y %H:%M")
    )


def write_atomic(path: Path, content: str) -> None:
    """Write a file so readers never see a half-written version.

    The content goes to a temp file in the same directory, which then
    replaces the target in one step.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; the web server must read it
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main() -> int:
    """Fetch the ranking and write the page. Returns the process exit code."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        config = Config.load()
        client = KickbaseClient(config.email, config.password)
        client.login()
        scores = fetch_scores(client, config.league_id)
        write_atomic(config.output_path, render(scores, datetime.now(LOCAL_TZ)))
    except ConfigError as e:
        log.error("%s", e)
        return 2
    except KickbaseError:
        log.exception("Kickbase API call failed")
        return 1
    # Commented out: only mail if cronjob fails
    # log.info("Wrote %d scores to %s", len(scores), config.output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
