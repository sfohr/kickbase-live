# kickbase-live

Renders the current matchday scores of a Kickbase league into a static HTML page.

`kb/` is a small client for the unofficial Kickbase v4 API
([community docs](https://github.com/kevinskyba/kickbase-api-doc)).
`update_html.py` fetches the league ranking and renders
`templates/index_template.html.jinja` to `OUTPUT_PATH`.

## Setup

```sh
uv sync
cp .env.example .env   # fill in USER, PW, LEAGUE_ID, OUTPUT_PATH
uv run python update_html.py
```

Run it periodically (e.g. via cron) to keep the page up to date.

## Development

```sh
uv run pytest
uv run mypy kb
```
