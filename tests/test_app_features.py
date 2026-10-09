"""Activity log, progression (XP, ranks, gear, seasons, ladders), body tracking,
and the Mini App actions built on them."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from database import add_user, as_user, get_connection, set_setting, set_user_name
from services import activity_service as act
from services import body_service as body
from services import progression_service as prog
from services.fitness_test_service import save_result
from services.workout_service import (
    complete_all_exercises_for_workout,
    get_current_date_str,
    get_exercises,
    get_or_create_daily_workout,
    update_exercise,
    update_workout_item,
)
from webapp.auth import sign_init_data
from webapp.server import create_webapp

TOKEN = "123456:TEST-TOKEN"
BRO = 222000333
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


@pytest.fixture
def crew():
    set_user_name(config.USER_ID, "Victor")
    add_user(BRO, "Andrei")


def today_items(user_id=None):
    with as_user(user_id or config.USER_ID):
        return get_or_create_daily_workout(get_current_date_str())["items"]


def headers(user_id):
    init = sign_init_data({"auth_date": str(int(time.time())),
                           "user": json.dumps({"id": user_id, "first_name": "X"})}, TOKEN)
    return {"Authorization": "tma " + init}


def call(requests, bot_data=None):
    async def run():
        app = create_webapp(bot=AsyncMock(), bot_data=bot_data if bot_data is not None else {}, bot_token=TOKEN)
        client = TestClient(TestServer(app))
        await client.start_server()
        out = []
        try:
            for method, path, user_id, body_ in requests:
                if method == "UPLOAD":
                    resp = await client.post(path, data=body_, headers=headers(user_id))
                elif method == "POST":
                    resp = await client.post(path, json=body_, headers=headers(user_id))
                else:
                    resp = await client.get(path, headers=headers(user_id))
                ctype = resp.headers.get("Content-Type", "")
                out.append((resp.status, await resp.json() if "json" in ctype else await resp.read()))
        finally:
            await client.close()
        return out
    return asyncio.run(run())


def act_(user_id, **body_):
    (status, res), = call([("POST", "/api/action", user_id, body_)])
    assert status == 200, res
    return res


# --------------------------------------------------------------------------
# Activity log
# --------------------------------------------------------------------------

def test_taps_on_one_exercise_merge_into_one_entry():
    item = today_items()[0]
    update_workout_item(item["id"], delta_reps=5)
    update_workout_item(item["id"], delta_reps=10)
    entries = act.recent()
    assert [e["text"] for e in entries] == ["+15 Push-ups"]


def test_finishing_shows_in_the_log_and_both_brothers_see_it(crew):
    with as_user(BRO):
        complete_all_exercises_for_workout(get_or_create_daily_workout(get_current_date_str())["id"])
    texts = [e["text"] for e in act.recent()]
    assert "finished today's workout 🏁" in texts
    owner_view = act_(config.USER_ID, type="settings")["state"]["activity"]
    assert any(e["who"] == "Andrei" and "finished" in e["text"] for e in owner_view)


def test_reactions_toggle(crew):
    entry = act.record("hype", "hyped Andrei 💪")
    assert act.react(entry, "🔥")
    assert act.recent()[0]["reactions"] == [{"user_id": config.USER_ID, "emoji": "🔥"}]
    assert act.react(entry, "🔥")  # tapping again removes it
    assert act.recent()[0]["reactions"] == []
    assert act.react(entry, "🍕") is False


def test_ghost_pace_uses_yesterday_until_now(crew):
    today = get_current_date_str()
    yesterday = (datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    with get_connection() as conn:
        for t, delta in (("00:00:01", 30), ("23:59:59", 999)):
            conn.execute("INSERT INTO activity (user_id, kind, text, data, created_at, local_time) "
                         "VALUES (?, 'progress', 'x', ?, 'x', ?);",
                         (BRO, json.dumps({"delta": delta, "unit": "reps"}), f"{yesterday}T{t}"))
        conn.commit()
    ghost = act.ghost_pace(BRO, today)
    assert ghost["rival_yesterday"] in (30, 30 + 999)  # 999 only at the very end of the day
    assert ghost["me_today"] == 0


def test_lowering_reps_after_done_reopens_the_exercise():
    item = today_items()[0]
    update_workout_item(item["id"], completed_reps=item["target_reps"])
    updated, _ = update_workout_item(item["id"], delta_reps=-5)
    assert updated["status"] == "pending"


# --------------------------------------------------------------------------
# XP, ranks, gear, seasons
# --------------------------------------------------------------------------

def test_rank_thresholds():
    assert prog.rank_for(0)["letter"] == "E"
    assert prog.rank_for(299)["letter"] == "D" and prog.rank_for(299)["next_at"] == 300
    assert prog.rank_for(3000)["letter"] == "S" and prog.rank_for(3000)["next_letter"] is None


def test_xp_is_all_points_ever():
    for d in ("2026-09-01", "2026-10-01"):
        complete_all_exercises_for_workout(get_or_create_daily_workout(d)["id"])
    assert prog.xp() == 30


def test_gear_unlocks_and_equipping():
    catalog = prog.gear_catalog()
    red = next(g for g in catalog["band"] if g["id"] == "red")
    assert red["unlocked"] is False
    assert prog.equip("band", "red") is False
    assert prog.equip("hair", "pink") is True
    assert prog.equipped()["hair"] == "#F06BC6"
    for i in range(7):
        complete_all_exercises_for_workout(get_or_create_daily_workout(f"2026-10-0{i + 1}")["id"])
    with as_user(config.USER_ID):
        assert next(g for g in prog.gear_catalog()["band"] if g["id"] == "red")["unlocked"]


def test_brother_defaults_to_orange_hair(crew):
    assert prog.equipped(BRO)["hair"] == "#FF8A3D"
    assert prog.equipped(BRO)["band"] == "#C8F135"


def test_season_champion_and_results(crew):
    complete_all_exercises_for_workout(get_or_create_daily_workout("2026-09-10")["id"])
    assert prog.season_champion("2026-09") == config.USER_ID
    assert prog.season_champion("2026-08") is None  # nobody scored
    bot = AsyncMock()
    assert asyncio.run(prog.send_season_results(bot, today_str="2026-10-01")) == config.USER_ID
    assert {c.kwargs["chat_id"] for c in bot.send_message.await_args_list} == {config.USER_ID, BRO}
    assert "September Champion" in bot.send_message.await_args.kwargs["text"]


# --------------------------------------------------------------------------
# Skill ladders
# --------------------------------------------------------------------------

def test_ladder_needs_high_target_and_solid_days():
    push = next(e for e in get_exercises() if e["name"] == "Push-ups")
    status = next(s for s in prog.ladder_status() if s["exercise_id"] == push["id"])
    assert status["level"] == 2 and status["next"] == "Diamond push-ups" and status["can_level_up"] is False
    assert prog.level_up(push["id"]) is None

    update_exercise(push["id"], target_reps=60)
    today = datetime.strptime(get_current_date_str(), "%Y-%m-%d").date()
    for i in range(3):
        day = get_or_create_daily_workout((today - timedelta(days=i)).isoformat())
        complete_all_exercises_for_workout(day["id"])
    assert next(s for s in prog.ladder_status() if s["exercise_id"] == push["id"])["can_level_up"]
    result = prog.level_up(push["id"])
    assert result == {"old": "Push-ups", "new": "Diamond push-ups", "target": 21}


# --------------------------------------------------------------------------
# Body tracking
# --------------------------------------------------------------------------

def test_body_log_validation_and_summary():
    assert "between" in body.log_body(weight=5)
    assert body.log_body(weight=80.4, date_str="2026-10-05") is None
    assert body.log_body(weight=79.6, waist=88, date_str="2026-10-06") is None
    summary = body.body_summary(today_str="2026-10-07")
    assert summary["weekly"] == [{"week": "2026-10-05", "avg": 80.0}]
    assert summary["weight_change"] == -0.8 and summary["waist"][0]["value"] == 88


def test_photos_are_private(crew):
    assert body.save_photo(b"not an image")["ok"] is False
    saved = body.save_photo(JPEG)
    assert saved["ok"]
    assert body.photo_file(saved["id"]).read_bytes() == JPEG
    with as_user(BRO):
        assert body.photo_file(saved["id"]) is None
        assert body.delete_photo(saved["id"]) is False
    assert body.delete_photo(saved["id"]) is True


# --------------------------------------------------------------------------
# Mini App actions
# --------------------------------------------------------------------------

def test_settings_reschedule_and_validate():
    scheduler = AsyncIOScheduler()
    (_, bad), (_, ok) = call([
        ("POST", "/api/action", config.USER_ID, {"type": "settings", "workout_time": "7am"}),
        ("POST", "/api/action", config.USER_ID, {"type": "settings", "workout_time": "06:15", "wake_time": "05:45",
                                                 "wake_enabled": True, "roast_level": "friendly"}),
    ], bot_data={"scheduler": scheduler})
    assert bad["message"] == "Use a time like 07:30."
    s = ok["state"]["settings"]
    assert (s["workout_time"], s["wake_time"], s["wake_enabled"], s["roast_level"]) == ("06:15", "05:45", True, "friendly")
    assert scheduler.get_job(f"wake_check:{config.USER_ID}") is not None


def test_pause_and_resume():
    paused = act_(config.USER_ID, type="pause", days=3)
    assert "Paused until" in paused["message"] and paused["state"]["settings"]["paused_until"]
    assert act_(config.USER_ID, type="resume")["state"]["settings"]["paused_until"] is None


def test_exercise_editor():
    res = act_(config.USER_ID, type="ex_add", name="Burpees", target=20, unit="reps")
    burpees = next(e for e in res["state"]["exercises"] if e["name"] == "Burpees")
    act_(config.USER_ID, type="ex_target", id=burpees["id"], target=25)
    act_(config.USER_ID, type="ex_days", id=burpees["id"], days="0,2,4")
    res = act_(config.USER_ID, type="ex_toggle", id=burpees["id"])
    edited = next(e for e in res["state"]["exercises"] if e["id"] == burpees["id"])
    assert (edited["target"], edited["days"], edited["active"]) == (25, "0,2,4", False)
    assert act_(config.USER_ID, type="ex_days", id=burpees["id"], days="")["message"] == "Pick at least one day."
    res = act_(config.USER_ID, type="ex_delete", id=burpees["id"])
    assert all(e["name"] != "Burpees" for e in res["state"]["exercises"])


def test_undo_restores_values():
    items = today_items()
    act_(config.USER_ID, type="complete_all")
    res = act_(config.USER_ID, type="items_set", values={str(i["id"]): 0 for i in items})
    today = res["state"]["today"]
    assert today["done_count"] == 0 and today["status"] == "pending"


def test_fitness_test_in_the_app_flags_records():
    save_result("2026-01", "pushups", 20)
    first = act_(config.USER_ID, type="fitness_save", key="pushups", value=25)
    assert first.get("pr") is True and "New record" in first["message"]
    skip = act_(config.USER_ID, type="fitness_save", key="squats", value=None)
    assert skip["message"] == "Skipped."
    tests = {t["key"]: t for t in skip["state"]["fitness_test"]["tests"]}
    assert tests["pushups"]["result"] == 25 and tests["squats"]["done"] is True


def test_onboarding_equip_and_rank_ack():
    res = act_(config.USER_ID, type="onboard", hair="blue")
    assert res["state"]["me"]["onboarded"] is True and res["state"]["me"]["gear"]["hair"] == "#7FB2FF"
    assert act_(config.USER_ID, type="equip", slot="aura", id="royal")["message"] == "Not unlocked yet."
    set_setting("seen_rank", "E")
    with get_connection() as conn:  # enough XP for rank D
        for i in range(7):
            conn.execute("INSERT INTO daily_workouts (user_id, date, status, created_at) VALUES (?, ?, 'completed', 'x');",
                         (config.USER_ID, f"2026-09-1{i}"))
        conn.commit()
    res = act_(config.USER_ID, type="ack_rank")
    assert res["state"]["me"]["seen_rank"] == "D"
    assert any("reached rank D" in e["text"] for e in res["state"]["activity"])


def test_custom_roast_and_reaction(crew):
    res = act_(config.USER_ID, type="roast", target=BRO, text="Wake up, sleepy_head!")
    assert "Roast delivered" in res["message"]
    roast = next(e for e in res["state"]["activity"] if e["kind"] == "roast")
    assert "Wake up, sleepy head!" in roast["text"]  # "_" stripped like any label
    reacted = act_(BRO, type="react", activity_id=roast["id"], emoji="💀")
    entry = next(e for e in reacted["state"]["activity"] if e["id"] == roast["id"])
    assert entry["my_reaction"] == "💀" and entry["reactions"] == [{"emoji": "💀", "count": 1}]


def test_body_photos_and_day_details_over_the_api(crew):
    assert act_(config.USER_ID, type="body_log", weight="81.5")["state"]["body"]["latest_weight"] == 81.5
    (status, uploaded), = call([("UPLOAD", "/api/photo", config.USER_ID, JPEG)])
    assert status == 200 and uploaded["ok"]
    photo_id = uploaded["state"]["body"]["photos"][0]["id"]
    (own, data), (theirs, _) = call([
        ("GET", f"/api/photo/{photo_id}", config.USER_ID, None),
        ("GET", f"/api/photo/{photo_id}", BRO, None),
    ])
    assert own == 200 and data == JPEG and theirs == 404
    (bad, _), = call([("UPLOAD", "/api/photo", config.USER_ID, b"GIF89a....")])
    assert bad == 400

    act_(config.USER_ID, type="complete_all")
    (status, day), (_, empty) = call([
        ("GET", f"/api/day?date={get_current_date_str()}", config.USER_ID, None),
        ("GET", "/api/day?date=2001-01-01", config.USER_ID, None),
    ])
    assert day["status"] == "completed" and len(day["items"]) == 4
    assert empty == {"date": "2001-01-01", "status": None, "items": []}


def test_dev_login_only_on_the_dev_server(crew):
    async def run():
        out = []
        for dev_user in (None, config.USER_ID):
            app = create_webapp(bot=AsyncMock(), bot_data={}, bot_token=TOKEN, dev_user=dev_user)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                for extra in ({}, {"X-Dev-User": str(BRO)}, {"X-Dev-User": "999"}):
                    resp = await client.get("/api/state", headers=extra)
                    data = await resp.json()
                    out.append((resp.status, data.get("me", {}).get("name")))
            finally:
                await client.close()
        return out
    results = asyncio.run(run())
    real, dev = results[:3], results[3:]
    assert [s for s, _ in real] == [401, 401, 401]  # the real bot never skips the Telegram check
    assert dev == [(200, "Victor"), (200, "Andrei"), (403, None)]
