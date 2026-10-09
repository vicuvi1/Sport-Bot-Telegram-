"""The System: levels, quests approved by the moderator (Mom), Daily Quest, penalties."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
from database import add_user, as_user, get_connection, set_user_name
from services import leveling_service as lv
from services import quest_service as qs
from services.workout_service import get_current_date_str, get_or_create_daily_workout, update_workout_item
from webapp.auth import sign_init_data
from webapp.server import create_webapp

TOKEN = "123456:TEST-TOKEN"
BRO = 222000333
MOM = 444000555
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


@pytest.fixture
def crew():
    set_user_name(config.USER_ID, "Victor")
    add_user(BRO, "Andrei")


@pytest.fixture
def mom(crew):
    code = qs.create_mod_invite()
    assert qs.accept_mod_invite(code, MOM, "Mom") == "ok"


def run(coro):
    return asyncio.run(coro)


def headers(user_id):
    init = sign_init_data({"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "X"})}, TOKEN)
    return {"Authorization": "tma " + init}


def call(requests):
    async def go():
        client = TestClient(TestServer(create_webapp(bot=AsyncMock(), bot_data={}, bot_token=TOKEN)))
        await client.start_server()
        out = []
        try:
            for method, path, user_id, body in requests:
                if method == "UPLOAD":
                    resp = await client.post(path, data=body, headers=headers(user_id))
                elif method == "POST":
                    resp = await client.post(path, json=body, headers=headers(user_id))
                else:
                    resp = await client.get(path, headers=headers(user_id))
                ctype = resp.headers.get("Content-Type", "")
                out.append((resp.status, await resp.json() if "json" in ctype else await resp.read()))
        finally:
            await client.close()
        return out
    return asyncio.run(go())


# --------------------------------------------------------------------------
# Levels and stats
# --------------------------------------------------------------------------

def test_level_curve_is_long_and_steady():
    assert lv.level_info(0)["level"] == 1
    costs = [lv.exp_to_next(level) for level in range(1, 100)]
    assert costs == sorted(costs) and costs[0] == 80
    assert lv.level_info(sum(costs[:9]))["level"] == 10
    assert lv.rank_for_level(90)["name"] == "National Level"


def test_stat_points_come_from_levels_and_daily_quests():
    assert lv.free_points(config.USER_ID) == 0
    assert lv.allocate("str") == "Not enough stat points. Level up or clear the Daily Quest."
    lv.add_exp(config.USER_ID, lv.exp_to_next(1), "quest")                 # -> level 2: +5 points
    assert lv.free_points(config.USER_ID) == lv.POINTS_PER_LEVEL
    assert lv.allocate("str", 3) is None and lv.allocate("luck") == "Unknown stat."
    st = lv.status(config.USER_ID)
    assert st["stats"]["str"] == lv.BASE_STAT + 3 and st["free_points"] == 2
    assert lv.change_job("fighter") == f"Job change unlocks at level {lv.JOB_CHANGE_LEVEL}."
    assert lv.equip_title("monarch") == "That title is still locked." and lv.equip_title("rookie") is None


# --------------------------------------------------------------------------
# Moderator and quests
# --------------------------------------------------------------------------

def test_moderator_invite_rules(crew):
    code = qs.create_mod_invite()
    assert qs.accept_mod_invite(code, BRO, "Andrei") == "crew"         # crew members can't moderate
    assert qs.accept_mod_invite("wrong", MOM, "Mom") == "invalid"
    assert qs.accept_mod_invite(code, MOM, "Mom") == "ok"
    assert qs.accept_mod_invite(code, 999, "X") == "invalid"           # single use
    assert qs.is_moderator(MOM) and not qs.can_review(config.USER_ID)  # with Mom here, the owner doesn't review


def test_mom_assigns_and_approves(mom):
    quest_id, error = qs.create_quest(MOM, "Clean your room", "life", "C", "once", assignee=BRO)
    assert error is None
    assert [q["title"] for q in qs.quests_for(BRO)] == ["Clean your room"]
    assert qs.quests_for(config.USER_ID) == []
    run_id, error = qs.submit(BRO, quest_id, "done, even under the bed")
    assert error is None and qs.submit(BRO, quest_id)[1] == "Already sent to Mom."
    assert qs.review(config.USER_ID, run_id, True)[1] == "Only Mom can approve quests."
    before = lv.total_exp(BRO)
    run_, error = qs.review(MOM, run_id, True)
    assert error is None and run_["exp"] == qs.QUEST_RANKS["C"]
    assert lv.total_exp(BRO) == before + qs.QUEST_RANKS["C"]
    assert qs.quests_for(BRO) == []                                    # one-time quest is done


def test_rejected_quests_can_be_redone(mom):
    quest_id, _ = qs.create_quest(MOM, "Homework", "life", "D", "daily")   # everyone
    run_id, _ = qs.submit(config.USER_ID, quest_id)
    qs.review(MOM, run_id, False, note="math is missing")
    state = next(q for q in qs.quests_for(config.USER_ID) if q["id"] == quest_id)
    assert state["state"] == "rejected" and state["review_note"] == "math is missing"
    assert qs.submit(config.USER_ID, quest_id)[1] is None
    assert lv.ledger_exp(config.USER_ID) == 0


def test_proposals_and_who_can_review(crew):
    assert qs.create_quest(BRO, "Read 30 minutes", "life", "D", "daily", assignee=config.USER_ID)[1] ==         "You can only propose quests for yourself."
    quest_id, error = qs.create_quest(BRO, "Read 30 minutes", "life", "D", "daily")   # lands on himself
    assert error is None
    assert next(q for q in qs.quests_for(BRO) if q["id"] == quest_id)["mine"]
    assert qs.quests_for(config.USER_ID) == []
    run_id, _ = qs.submit(BRO, quest_id)
    # No moderator yet: the owner reviews, but nobody approves their own quest.
    assert qs.review(BRO, run_id, True)[1] == "Only Mom can approve quests."
    assert qs.review(config.USER_ID, run_id, True, rank="B")[0]["exp"] == qs.QUEST_RANKS["B"]
    own, _ = qs.create_quest(config.USER_ID, "Stretch", "fitness", "E", "once", assignee=config.USER_ID)
    own_run, _ = qs.submit(config.USER_ID, own)
    assert qs.review(config.USER_ID, own_run, True)[1] == "You can't approve your own quest."


# --------------------------------------------------------------------------
# Daily Quest and penalties
# --------------------------------------------------------------------------

def test_daily_quest_counts_workout_reps_and_rewards(crew):
    targets = qs.dq_targets(1)
    assert targets == {"pushups": 20, "situps": 20, "squats": 20, "run_km": 1.0}
    assert qs.dq_targets(50) == {"pushups": 100, "situps": 100, "squats": 100, "run_km": 10.0}
    items = get_or_create_daily_workout(get_current_date_str())["items"]
    for item in items:
        update_workout_item(item["id"], completed_reps=item["target_reps"])  # 50 push-ups, 50 squats, 30 sit-ups
    dq = qs.daily_quest(config.USER_ID)
    assert all(p["done"] >= p["target"] for p in dq["parts"] if p["key"] != "run") and not dq["complete"]
    assert qs.dq_log(config.USER_ID, "run", 1.0) is None
    dq = qs.daily_quest(config.USER_ID)
    assert dq["complete"] and dq["rewarded"]
    assert lv.ledger_exp(config.USER_ID) == dq["reward_exp"]
    assert lv.daily_quests_cleared(config.USER_ID) == 1


def test_missed_daily_quest_brings_a_penalty_that_locks_rewards(mom):
    bot = AsyncMock()
    with as_user(BRO):
        quest_id = run(qs.penalty_check(bot))
    assert quest_id and "Penalty Quest" in qs.get_quest(quest_id)["title"]
    assert qs.archive_quest(BRO, quest_id) == "Only Mom can remove that quest."
    assert qs.quests_for(BRO)[0]["penalty"] and qs.quests_for(BRO)[0]["exp"] == 0
    with as_user(BRO):
        assert run(qs.penalty_check(bot)) is None                      # one open penalty at a time
    # Clearing today's Daily Quest while the penalty is open: the reward waits.
    with as_user(BRO):
        qs.dq_log(BRO, "run", 1.0)
        for part in ("pushups", "situps", "squats"):
            qs.dq_log(BRO, part, 20)
    assert qs.daily_quest(BRO)["complete"] and not qs.daily_quest(BRO)["rewarded"]
    run_id, _ = qs.submit(BRO, quest_id)
    qs.review(MOM, run_id, True)
    assert qs.daily_quest(BRO)["rewarded"] and lv.ledger_exp(BRO) > 0


def test_no_penalty_after_a_paused_day(crew):
    yesterday = (datetime.strptime(get_current_date_str(), "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    with get_connection() as conn:
        conn.execute("INSERT INTO pauses (user_id, start_date, end_date, created_at) VALUES (?, ?, ?, 'x');",
                     (config.USER_ID, yesterday, yesterday))
        conn.commit()
    assert run(qs.penalty_check(AsyncMock())) is None


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def test_moderator_api_is_quests_only(mom):
    results = call([
        ("GET", "/api/state", MOM, None),
        ("POST", "/api/action", MOM, {"type": "item_delta", "item_id": 1, "delta": 5}),
        ("POST", "/api/action", MOM, {"type": "quest_create", "title": "Wash the dishes", "category": "life",
                                      "rank": "E", "repeat": "once", "assignee": BRO}),
    ])
    (s1, state), (s2, _), (s3, created) = results
    assert s1 == 200 and state["role"] == "moderator" and [h["name"] for h in state["hunters"]] == ["Victor", "Andrei"]
    assert s2 == 403
    assert s3 == 200 and created["state"]["quests"][0]["title"] == "Wash the dishes"


def test_quest_photo_proof_is_seen_by_mom_not_the_brother(mom):
    quest_id, _ = qs.create_quest(MOM, "Make your bed", "life", "E", "daily", assignee=BRO)
    upload, = call([("UPLOAD", f"/api/quest_submit?quest_id={quest_id}&note=look", BRO, JPEG)])
    assert upload[0] == 200
    run_id = qs.pending_reviews()[0]["id"]
    mom_view, brother_view, owner_view = call([
        ("GET", f"/api/quest_proof/{run_id}", MOM, None),
        ("GET", f"/api/quest_proof/{run_id}", BRO, None),
        ("GET", f"/api/quest_proof/{run_id}", config.USER_ID, None),
    ])
    assert mom_view == (200, JPEG) and brother_view[0] == 200 and owner_view[0] == 200
    review, = call([("POST", "/api/action", MOM, {"type": "quest_review", "run_id": run_id, "approve": True})])
    assert review[1]["message"] == "Approved ✅"


def test_player_state_has_the_system(crew):
    (status, state), = call([("GET", "/api/state", BRO, None)])
    assert status == 200
    assert state["system"]["level"] == 1 and state["system"]["rank"]["letter"] == "E"
    assert [p["key"] for p in state["daily_quest"]["parts"]] == ["pushups", "situps", "squats", "run"]
    assert state["quests"] == [] and state["can_review"] is False
