"""Runs the Mini App on your PC for fast development: python -m webapp.dev

Open http://localhost:8090 in a normal browser. It uses real code with its own
database (data/dev.db, created and filled with a sample crew on first run) and
never talks to Telegram: whatever the bot would send is printed here instead,
so it can't clash with the live bot on the server.

  ?as=2 in the address shows the app as the other crew member.
  --reset starts over with a fresh sample crew.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

from aiohttp import web

import config

DEV_DB = config.DATA_DIR / "dev.db"
OWNER, BROTHER = 1, 2

# Point everything at the dev database and owner before anything uses them.
config.DB_PATH = DEV_DB
config.USER_ID = OWNER

from database import add_user, as_user, get_connection, init_db, set_setting  # noqa: E402
from services import body_service  # noqa: E402
from services import progression_service as prog  # noqa: E402
from services.social_service import send_chat  # noqa: E402
from services.workout_service import (  # noqa: E402
    complete_all_exercises_for_workout,
    get_current_date_str,
    get_or_create_daily_workout,
    update_workout_item,
)
from webapp.server import create_webapp  # noqa: E402

logger = logging.getLogger("webapp.dev")


class PrintBot:
    """Stands in for telegram.Bot: prints each call instead of sending it."""

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            chat = kwargs.get("chat_id", args[0] if args else "?")
            text = kwargs.get("text") or kwargs.get("caption") or ""
            print(f"[bot.{name} -> {chat}] {text}", flush=True)
            return SimpleNamespace(message_id=0, chat=SimpleNamespace(id=chat))
        return call


def seed() -> None:
    """A sample crew with a few weeks of history, so every screen has data."""
    init_db()
    add_user(OWNER, "Victor", "owner")
    add_user(BROTHER, "Andrei")
    today = datetime.strptime(get_current_date_str(), "%Y-%m-%d").date()
    for uid, skip_every in ((OWNER, 9), (BROTHER, 4)):
        with as_user(uid):
            set_setting("onboarded", "1" if uid == BROTHER else "0")
            for back in range(20, 0, -1):
                if back % skip_every == 0:
                    continue
                workout = get_or_create_daily_workout((today - timedelta(days=back)).isoformat())
                complete_all_exercises_for_workout(workout["id"])
            for back, weight in enumerate((81.0, 81.4, 81.9, 82.3)):
                body_service.log_body(weight, None, (today - timedelta(days=7 * back)).isoformat())
            set_setting("seen_rank", prog.rank_for(prog.xp(uid))["letter"])
    # The history above would fill the fight log with "just now" entries; start clean.
    with get_connection() as conn:
        conn.execute("DELETE FROM activity;")
        conn.execute("UPDATE crew_events SET delivered = 1;")
        conn.commit()
    with as_user(BROTHER):
        items = get_or_create_daily_workout(today.isoformat())["items"]
        update_workout_item(items[0]["id"], delta_reps=20)
        send_chat(text="bet you can't beat me today 😏")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--reset", action="store_true", help="delete data/dev.db and start over")
    args = parser.parse_args()
    # Windows consoles default to a codepage without emoji; bot messages are full of them.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.reset and DEV_DB.exists():
        DEV_DB.unlink()
    if not DEV_DB.exists():
        seed()
        print("Created data/dev.db with a sample crew (Victor = 1, Andrei = 2).")
    init_db()

    app = create_webapp(bot=PrintBot(), bot_data={}, bot_token="dev", dev_user=OWNER)
    print(f"Dev app: http://localhost:{args.port}   (as Andrei: http://localhost:{args.port}/?as={BROTHER})")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
