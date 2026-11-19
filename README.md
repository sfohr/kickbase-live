# kickbase-live

Renders matchday scores of a Kickbase league into a static HTML page.

- `kb/client.py`: small client for the unofficial Kickbase v4 API
  ([community docs](https://github.com/kevinskyba/kickbase-api-doc))
- `kb/errors.py`: exception hierarchy, all subclasses of `KickbaseError`
- `kb/scoreboard.py`: fetches the league ranking and renders
  `kb/templates/index.html.jinja` to `OUTPUT_PATH`

## Setup

```sh
uv sync
cp .env.example .env   # fill in USER, PW, LEAGUE_ID, OUTPUT_PATH
uv run update_scoreboard
```

`kickbase` reads `.env` from the current directory and exits non-zero on
failure (1 = API error, 2 = bad config). To keep the page up to date, run it
from cron, e.g. every 5 minutes. Attention: cron does not read .bash_profile etc., so you might need to set the path manually in the cron file.

```cron
*/5 * * * * cd ~/kickbase-live && uv run update_scoreboard
```

## Development

```sh
uv sync
uv run pre-commit install   # ruff, mypy and pytest before every commit
```

Run the checks manually:

```sh
uv run ruff check
uv run ruff format
uv run mypy
uv run pytest
```

The same checks run in CI (`.github/workflows/ci.yml`).
