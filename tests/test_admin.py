"""Admin (owner) corrections to any crew member's workouts."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
from database import add_user, as_user, get_connection, set_user_name
from services import activity_service
from services import admin_service as admin
from services import compete_service as cmp
from services.summary_service import week_bounds
from services.workout_service import get_current_date_str, get_or_create_daily_workout
from webapp.auth import sign_init_data
from webapp.server import create_webapp

TOKEN = "123456:TEST-TOKEN"
BRO = 222000333


@pytest.fixture
def crew():
    set_user_name(config.USER_ID, "Victor")
    add_user(BRO, "Andrei")


def today():
    return get_current_date_str()


def days_ago(n):
    return (datetime.strptime(today(), "%Y-%m-%d").date() - timedelta(days=n)).isoformat()


def run(coro):
    return asyncio.run(coro)


def events(kind):
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM crew_events WHERE kind = ?;", (kind,)).fetchone()[0]


def headers(user_id):
    init = sign_init_data({"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "X"})}, TOKEN)
    return {"Authorization": "tma " + init}


def call(requests, bot=None):
    async def go():
        client = TestClient(TestServer(create_webapp(bot=bot or AsyncMock(), bot_data={}, bot_token=TOKEN)))
        await client.start_server()
        out = []
        try:
            for method, path, user_id, body in requests:
                resp = await (client.post(path, json=body, headers=headers(user_id)) if method == "POST"
                              else client.get(path, headers=headers(user_id)))
                out.append((resp.status, await resp.json() if "json" in resp.headers.get("Content-Type", "") else None))
        finally:
            await client.close()
        return out
    return asyncio.run(go())


def test_fix_a_forgotten_day_quietly_but_visibly(crew):
    bot = AsyncMock()
    day = days_ago(2)
    assert admin.day_view(BRO, day)["exists"] is False          # looking creates nothing
    message = run(admin.set_day(bot, config.USER_ID, BRO, day, "create", "forgot to log"))
    assert message.startswith("Opened")
    items = admin.day_view(BRO, day)["items"]
    for item in items:
        run(admin.set_item(bot, config.USER_ID, BRO, item["id"], mark="done", reason="forgot to log"))
    view = admin.day_view(BRO, day)
    assert view["status"] == "completed"
    assert events("workout_done") == 0 and events("overtake") == 0   # no fake "just finished" alerts
    log = [a["text"] for a in activity_service.recent(50) if a["kind"] == "admin"]
    assert any("Andrei's" in t and "(forgot to log)" in t for t in log)
    assert all(a["kind"] != "progress" for a in activity_service.recent(50))
    sent = bot.send_message.await_args.kwargs
    assert sent["chat_id"] == BRO and "(admin) changed" in sent["text"]


def test_corrections_count_for_points_and_reset_takes_them_back(crew):
    bot = AsyncMock()
    with as_user(BRO):
        get_or_create_daily_workout(today())
    start, end = week_bounds(today())
    run(admin.set_day(bot, config.USER_ID, BRO, today(), "complete"))
    assert cmp.points_breakdown(BRO, start, end)["workout"] == cmp.POINTS["workout"]
    run(admin.set_day(bot, config.USER_ID, BRO, today(), "reset"))
    assert cmp.points_breakdown(BRO, start, end)["workout"] == 0
    assert admin.day_view(BRO, today())["status"] == "pending"


def test_item_reps_target_skip_and_unskip(crew):
    bot = AsyncMock()
    with as_user(BRO):
        item = get_or_create_daily_workout(today())["items"][0]
    run(admin.set_item(bot, config.USER_ID, BRO, item["id"], reps=12, target=40))
    row = admin.day_view(BRO, today())["items"][0]
    assert (row["done"], row["target"], row["status"]) == (12, 40, "pending")
    run(admin.set_item(bot, config.USER_ID, BRO, item["id"], mark="skip"))
    assert admin.day_view(BRO, today())["items"][0]["status"] == "skipped"
    run(admin.set_item(bot, config.USER_ID, BRO, item["id"], mark="unskip"))
    assert admin.day_view(BRO, today())["items"][0]["status"] == "pending"
    assert run(admin.set_item(bot, config.USER_ID, BRO, item["id"], reps=0)) == "Nothing changed."


def test_guards(crew):
    bot = AsyncMock()
    tomorrow = (datetime.strptime(today(), "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    with pytest.raises(admin.AdminError):
        run(admin.set_day(bot, config.USER_ID, BRO, tomorrow, "create"))
    with pytest.raises(admin.AdminError):
        admin.admin_view(999)
    with as_user(config.USER_ID):
        mine = get_or_create_daily_workout(today())["items"][0]
    with pytest.raises(admin.AdminError):                      # Victor's item isn't Andrei's
        run(admin.set_item(bot, config.USER_ID, BRO, mine["id"], reps=5))


def test_plan_changes_are_logged(crew):
    with as_user(BRO):
        exercise = admin.admin_view(BRO)["exercises"][0]
    run(admin.set_exercise(AsyncMock(), config.USER_ID, BRO, exercise["id"], target=exercise["target"] + 10))
    assert admin.admin_view(BRO)["exercises"][0]["target"] == exercise["target"] + 10
    assert "target" in admin.admin_view(BRO)["log"][0]["text"]


def test_api_is_owner_only(crew):
    results = call([
        ("GET", f"/api/admin?user={BRO}", config.USER_ID, None),
        ("GET", f"/api/admin?user={config.USER_ID}", BRO, None),
        ("POST", "/api/action", BRO, {"type": "admin_day", "user": config.USER_ID, "date": today(), "op": "reset"}),
        ("POST", "/api/action", config.USER_ID, {"type": "admin_day", "user": BRO, "date": days_ago(1), "op": "create"}),
    ])
    (ok, view), (denied, _), (denied_action, _), (done, res) = results
    assert ok == 200 and view["name"] == "Andrei" and len(view["days"]) == 14
    assert denied == 403 and denied_action == 403
    assert done == 200 and res["admin"]["day"]["exists"] is True
