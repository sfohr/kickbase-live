from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values
from jinja2 import Template
from pytz import timezone

from kb.client import KickbaseClient

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "templates" / "index_template.html.jinja"

config = dotenv_values(BASE_DIR / ".env")
USER = config["USER"]
PW = config["PW"]
LEAGUE_ID = config["LEAGUE_ID"]
OUTPUT_PATH = config["OUTPUT_PATH"]

local_tz = timezone("Europe/Berlin")

if __name__ == "__main__":
    kickbase = KickbaseClient(USER, PW)
    kickbase.login()

    now_as_string = str(datetime.now(local_tz).strftime("%d.%m.%y %H:%M"))

    league_ranking = kickbase.league_ranking(league_id=LEAGUE_ID)
    user_scores = {user["n"]: user["mdp"] for user in league_ranking["us"]}
    user_scores_sorted = dict(
        sorted(user_scores.items(), key=lambda item: item[1], reverse=True)
    )

    with open(TEMPLATE_PATH, "r") as f:
        tmpl = Template(f.read())

    rendered = tmpl.render(scores=user_scores_sorted, now=now_as_string)

    with open(OUTPUT_PATH, "w") as f:
        f.write(rendered)
