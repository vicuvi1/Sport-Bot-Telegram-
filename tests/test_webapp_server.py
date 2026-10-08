"""The Mini App server: Telegram login check, crew-only access, state and actions."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
from database import add_user, as_user, set_setting, set_user_name
from services import compete_service as cmp
from services import wake_service as ws
from services.workout_service import get_current_date_str, get_or_create_daily_workout
from tests.test_html_views import assert_valid_html
from webapp.auth import AuthError, sign_init_data, verify_init_data
from webapp.server import create_webapp

TOKEN = "123456:TEST-TOKEN"
BRO = 222000333


def init_data(user_id, name="X", token=TOKEN, age=0):
    return sign_init_data({
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": name}),
    }, token)


@pytest.fixture
def crew():
    set_user_name(config.USER_ID, "Victor")
    add_user(BRO, "Andrei")


def call(requests, bot=None):
    """Runs (method, path, user_id, body) requests against a fresh app; returns [(status, json)]."""
    async def run():
        app = create_webapp(bot=bot or AsyncMock(), bot_data={}, bot_token=TOKEN)
        client = TestClient(TestServer(app))
        await client.start_server()
        out = []
        try:
            for method, path, user_id, body in requests:
                headers = {"Authorization": "tma " + init_data(user_id)} if user_id else {}
                resp = await (client.post(path, json=body, headers=headers) if method == "POST"
                              else client.get(path, headers=headers))
                ctype = resp.headers.get("Content-Type", "")
                out.append((resp.status, await resp.json() if "json" in ctype else await resp.text(), resp.headers))
        finally:
            await client.close()
        return out
    return asyncio.run(run())


# --------------------------------------------------------------------------
# Login check
# --------------------------------------------------------------------------

def test_valid_signature_returns_the_user():
    assert verify_init_data(init_data(42, "Ana"), TOKEN)["first_name"] == "Ana"


@pytest.mark.parametrize("bad", [
    lambda: init_data(42, token="999:OTHER"),          # signed by another bot
    lambda: init_data(42, age=2 * 24 * 3600),          # too old
    lambda: init_data(42).replace("first_name", "frst_name"),  # tampered
    lambda: "",
    lambda: "user=%7B%22id%22%3A42%7D",                # no hash
])
def test_bad_init_data_is_rejected(bad):
    with pytest.raises(AuthError):
        verify_init_data(bad(), TOKEN)


def test_api_needs_telegram_login_and_crew_membership():
    (no_auth, *_), (stranger, *_), (owner, data, _) = call([
        ("GET", "/api/state", None, None),
        ("GET", "/api/state", 999, None),
        ("GET", "/api/state", config.USER_ID, None),
    ])
    assert no_auth == 401 and stranger == 403 and owner == 200
    assert data["me"]["is_owner"] is True and len(data["today"]["items"]) == 4


# --------------------------------------------------------------------------
# State and isolation
# --------------------------------------------------------------------------

def test_each_brother_sees_his_own_day(crew):
    (_, owner, _), (_, bro, _) = call([
        ("GET", "/api/state", config.USER_ID, None),
        ("GET", "/api/state", BRO, None),
    ])
    owner_ids = {i["id"] for i in owner["today"]["items"]}
    bro_ids = {i["id"] for i in bro["today"]["items"]}
    assert owner_ids.isdisjoint(bro_ids)
    assert bro["me"]["name"] == "Andrei" and bro["me"]["is_owner"] is False
    assert [m["name"] for m in bro["crew"]] == ["Victor", "Andrei"]
    assert bro["crew"][1]["me"] is True


def test_brother_cannot_change_owner_items(crew):
    owner_item = get_or_create_daily_workout(get_current_date_str())["items"][0]["id"]
    ((status, res, _),) = call([("POST", "/api/action", BRO, {"type": "item_delta", "item_id": owner_item, "delta": 10})])
    assert status == 200 and res["message"] == "That exercise isn't yours."
    assert get_or_create_daily_workout(get_current_date_str())["items"][0]["completed_reps"] == 0


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def test_logging_and_completing(crew):
    item = get_or_create_daily_workout(get_current_date_str())["items"][0]["id"]
    bot = AsyncMock()
    (_, after_delta, _), (_, after_done, _), (_, after_all, _) = call([
        ("POST", "/api/action", config.USER_ID, {"type": "item_delta", "item_id": item, "delta": 10}),
        ("POST", "/api/action", config.USER_ID, {"type": "item_done", "item_id": item}),
        ("POST", "/api/action", config.USER_ID, {"type": "complete_all"}),
    ], bot=bot)
    assert after_delta["state"]["today"]["items"][0]["done"] == 10
    assert after_done["state"]["today"]["done_count"] == 1
    assert after_all["state"]["today"]["status"] == "completed"
    assert after_all["state"]["today"]["can_give_feedback"] is True
    # The brother is told right away (crew feed).
    feed = [c.kwargs for c in bot.send_message.await_args_list if c.kwargs["chat_id"] == BRO]
    assert feed and "just finished" in feed[0]["text"]
    assert_valid_html(feed[0]["text"])


def test_quick_and_feedback():
    (_, quick, _), = call([("POST", "/api/action", config.USER_ID, {"type": "quick", "on": True})])
    assert quick["state"]["today"]["quick"] is True and quick["state"]["today"]["items"][0]["target"] == 25
    (_, undo, _), (_, done, _), (_, fb, _) = call([
        ("POST", "/api/action", config.USER_ID, {"type": "quick", "on": False}),
        ("POST", "/api/action", config.USER_ID, {"type": "complete_all"}),
        ("POST", "/api/action", config.USER_ID, {"type": "feedback", "value": "hard"}),
    ])
    assert undo["state"]["today"]["items"][0]["target"] == 50
    assert fb["state"]["today"]["feedback"] == "hard" and "Targets updated" in fb["message"]


def test_roast_and_hype_from_the_app(crew):
    bot = AsyncMock()
    (_, roast, _), (_, hype, _) = call([
        ("POST", "/api/action", config.USER_ID, {"type": "roast", "target": BRO}),
        ("POST", "/api/action", config.USER_ID, {"type": "hype", "target": BRO}),
    ], bot=bot)
    assert "Roast delivered to Andrei" in roast["message"]
    assert "Hype sent" in hype["message"]
    assert [c.kwargs["chat_id"] for c in bot.send_message.await_args_list] == [BRO, BRO]


def test_wake_up_from_the_app():
    set_setting("wake_enabled", "1")
    set_setting("wake_time", "00:00")
    ws.start_check()
    (_, state, _), = call([("GET", "/api/state", config.USER_ID, None)])
    wake = state["wake"]["today"]
    assert wake["status"] == "pending" and wake["challenge"] in wake["choices"]
    wrong = next(n for n in range(1, 10) if n != wake["challenge"])
    (_, w1, _), (_, w2, _) = call([
        ("POST", "/api/action", config.USER_ID, {"type": "wake_answer", "choice": wrong}),
        ("POST", "/api/action", config.USER_ID, {"type": "wake_answer", "choice": wake["challenge"]}),
    ])
    assert "Wrong number" in w1["message"]
    assert w2["state"]["wake"]["today"]["status"] in ("on_time", "late")


def test_challenge_round_trip(crew):
    bot = AsyncMock()
    (_, offer, _), = call([("POST", "/api/action", config.USER_ID, {"type": "challenge", "target": BRO, "kind": "full"})], bot=bot)
    assert offer["message"] == "Challenge sent!"
    assert bot.send_message.await_args.kwargs["chat_id"] == BRO
    (_, bro_state, _), = call([("GET", "/api/state", BRO, None)])
    challenge = bro_state["challenges"][0]
    assert challenge["mine_to_answer"] is True
    (_, answered, _), = call([("POST", "/api/action", BRO, {"type": "challenge_answer", "id": challenge["id"], "accept": False})])
    assert answered["message"] == "Bawk bawk 🐔"
    assert cmp.get_challenge(challenge["id"])["status"] == "declined"


def test_forfeit_done_only_by_the_loser(crew):
    fid = cmp.create_forfeit("2026-10-05", config.USER_ID, BRO)
    cmp.pick_forfeit(fid, config.USER_ID, 0)
    (_, owner_try, _), (_, bro_does, _) = call([
        ("POST", "/api/action", config.USER_ID, {"type": "forfeit_done", "id": fid}),
        ("POST", "/api/action", BRO, {"type": "forfeit_done", "id": fid}),
    ])
    assert owner_try["message"] == "Nothing to mark here."
    assert "Forfeit done" in bro_does["message"]


def test_anime_setting_is_per_person(crew):
    (_, off, _), = call([("POST", "/api/action", BRO, {"type": "settings", "anime": False})])
    assert off["state"]["me"]["anime"] is False
    (_, owner, _), = call([("GET", "/api/state", config.USER_ID, None)])
    assert owner["me"]["anime"] is True


def test_bad_requests():
    (unknown, *_), = call([("POST", "/api/action", config.USER_ID, {"type": "explode"})])
    (missing, *_), = call([("POST", "/api/action", config.USER_ID, {"type": "item_delta"})])
    assert unknown == 400 and missing == 400


# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------

def test_app_page_and_assets_are_served_with_security_headers():
    (status, html, headers), (js_status, js, _), (health, data, _) = call([
        ("GET", "/", None, None),
        ("GET", "/static/app.js", None, None),
        ("GET", "/health", None, None),
    ])
    assert status == 200 and "telegram-web-app.js" in html and "/static/app.js" in html
    assert "frame-ancestors" in headers["Content-Security-Policy"]
    assert js_status == 200 and "initData" in js
    assert health == 200 and data["status"] == "healthy"
