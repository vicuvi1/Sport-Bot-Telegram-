"""Doing it together: chat, live training, proof clips, bets, overtake alerts, report cards."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
from database import add_user, as_user, get_connection, set_setting, set_user_name
from services import compete_service as cmp
from services import crew_service as cs
from services import social_service as social
from services.summary_service import week_bounds
from services.workout_service import (
    complete_all_exercises_for_workout,
    get_current_date_str,
    get_or_create_daily_workout,
    update_workout_item,
)
from webapp.auth import sign_init_data
from webapp.server import create_webapp

TOKEN = "123456:TEST-TOKEN"
BRO = 222000333
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 300
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


@pytest.fixture
def crew():
    set_user_name(config.USER_ID, "Victor")
    add_user(BRO, "Andrei")


def items(user_id=None):
    with as_user(user_id or config.USER_ID):
        return get_or_create_daily_workout(get_current_date_str())["items"]


def log(user_id, reps, index=0):
    with as_user(user_id):
        update_workout_item(items(user_id)[index]["id"], delta_reps=reps)


def events(kind):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM crew_events WHERE kind = ? ORDER BY id;", (kind,)).fetchall()
    return [dict(r) | {"data": json.loads(r["data"])} for r in rows]


def headers(user_id):
    init = sign_init_data({"auth_date": str(int(time.time())),
                           "user": json.dumps({"id": user_id, "first_name": "X"})}, TOKEN)
    return {"Authorization": "tma " + init}


def call(requests, bot=None):
    async def run():
        app = create_webapp(bot=bot or AsyncMock(), bot_data={}, bot_token=TOKEN)
        client = TestClient(TestServer(app))
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
    return asyncio.run(run())


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------

def test_chat_needs_a_crew():
    assert social.send_chat(text="hi") == (None, "Invite your brother first: the chat needs two.")


def test_chat_messages_unread_and_stickers(crew):
    msg_id, error = social.send_chat(text="  watch   this  ")
    assert error is None
    assert social.send_chat(sticker="pizza")[1] == "Unknown sticker."
    assert social.send_chat(text="   ")[1] == "Type a message first."
    social.send_chat(sticker="flex")
    assert social.unread_count(config.USER_ID) == 0      # your own messages are never unread
    assert social.unread_count(BRO) == 2
    with as_user(BRO):
        msgs = social.chat_messages()
        assert [(m["who"], m["kind"], m["text"], m["me"]) for m in msgs] == [
            ("Victor", "text", "watch this", False), ("Victor", "sticker", "flex", False)]
        social.mark_chat_seen(msgs[-1]["id"])
    assert social.unread_count(BRO) == 0


def test_chat_pings_telegram_only_when_they_are_not_in_the_app(crew):
    bot = AsyncMock()
    social._last_ping.clear()
    social.send_chat(text="yo")
    asyncio.run(cs.deliver_events(bot))
    assert bot.send_message.await_count == 1
    assert "Victor" in bot.send_message.await_args.kwargs["text"]

    with as_user(BRO):
        social.touch_app_seen()
    social._last_ping.clear()
    social.send_chat(text="still there?")
    asyncio.run(cs.deliver_events(bot))
    assert bot.send_message.await_count == 1   # Andrei had the app open: no ping


# --------------------------------------------------------------------------
# Live training and pulse
# --------------------------------------------------------------------------

def test_live_rest_and_pulse_signature(crew):
    before = social.pulse()["sig"]
    social.live_start()
    social.start_rest(60)
    assert social.is_live(config.USER_ID) and not social.is_live(BRO)
    assert 55 <= social.rest_left(config.USER_ID) <= 60
    log(BRO, 10)
    after = social.pulse()
    assert after["sig"] != before
    bro = next(c for c in after["crew"] if c["id"] == BRO)
    assert bro["reps"] == 10 and 0 < bro["progress"] < 1
    assert len(events("live")) == 1
    social.live_start()                       # again within 30 minutes: no second ping
    assert len(events("live")) == 1
    social.live_stop()
    assert not social.is_live(config.USER_ID) and social.rest_left(config.USER_ID) == 0


# --------------------------------------------------------------------------
# Proof clips
# --------------------------------------------------------------------------

def test_proofs_are_sniffed_shared_and_judged(crew):
    assert social.save_proof(b"GIF89a" + b"0" * 50)["ok"] is False
    assert social.save_proof(b"")["ok"] is False
    result = social.save_proof(MP4)
    assert result["ok"]
    proof_id = result["id"]
    assert social.vote_proof(proof_id, "legit") == "You can't judge your own proof 😅"
    with as_user(BRO):
        path, mime = social.proof_file(proof_id)      # the crew can watch it
        assert path.read_bytes() == MP4 and mime == "video/mp4"
        assert social.chat_messages()[-1]["proof"]["media"] == "video"
        assert social.vote_proof(proof_id, "maybe") == "Vote legit or cap."
        assert social.vote_proof(proof_id, "legit") is None
    start, end = week_bounds(get_current_date_str())
    assert social.proof_points(config.USER_ID, start, end) == 3
    assert cmp.points_breakdown(config.USER_ID, start, end)["proof"] == 3
    with as_user(BRO):
        social.vote_proof(proof_id, "cap")           # changes the vote
    assert social.proof_points(config.USER_ID, start, end) == -5


def test_only_one_legit_proof_a_day_earns_points(crew):
    ids = [social.save_proof(JPEG)["id"] for _ in range(3)]
    with as_user(BRO):
        for proof_id in ids:
            social.vote_proof(proof_id, "legit")
    start, end = week_bounds(get_current_date_str())
    assert social.proof_points(config.USER_ID, start, end) == 3


def test_old_proof_files_are_cleaned_up(crew):
    proof_id = social.save_proof(MP4)["id"]
    assert social.cleanup_proofs() == 0
    assert social.cleanup_proofs(now=datetime.now(timezone.utc) + timedelta(days=31)) == 1
    assert social.proof_file(proof_id) is None
    assert social.proof_info(proof_id)["available"] is False


# --------------------------------------------------------------------------
# Bets
# --------------------------------------------------------------------------

def test_bet_rules(crew):
    me = config.USER_ID
    assert social.offer_bet(me, me, "reps", 10)[1] == "Bet against someone in your crew."
    assert social.offer_bet(me, BRO, "reps", 7)[1] == "Pick a bet and a stake."
    bet_id, error = social.offer_bet(me, BRO, "reps", 10)
    assert error is None
    assert social.offer_bet(me, BRO, "reps", 5)[1] == "There's already a bet like that today."
    assert social.respond_bet(bet_id, me, True) == "That bet isn't open any more."  # only the target answers
    assert social.respond_bet(bet_id, BRO, True) is None
    assert social.get_bet(bet_id)["status"] == "accepted"


def test_reps_bet_is_settled_at_the_deadline_and_moves_points(crew):
    bet_id, _ = social.offer_bet(config.USER_ID, BRO, "reps", 10)
    social.respond_bet(bet_id, BRO, True)
    log(config.USER_ID, 30)
    log(BRO, 20)
    asyncio.run(social.check_bets(AsyncMock()))           # reps bets wait for the deadline
    assert social.get_bet(bet_id)["status"] == "accepted"
    with as_user(BRO):
        asyncio.run(social.close_bets(AsyncMock()))
    assert social.get_bet(bet_id)["winner"] == config.USER_ID
    start, end = week_bounds(get_current_date_str())
    assert social.bet_points(config.USER_ID, start, end) == 10
    assert social.bet_points(BRO, start, end) == -10
    assert cmp.points_breakdown(BRO, start, end)["bets"] == -10


def test_finish_first_bet_settles_right_away_and_unanswered_bets_expire(crew):
    first, _ = social.offer_bet(config.USER_ID, BRO, "first", 20)
    social.respond_bet(first, BRO, True)
    unanswered, _ = social.offer_bet(config.USER_ID, BRO, "push", 5)
    with as_user(BRO):
        complete_all_exercises_for_workout(get_or_create_daily_workout(get_current_date_str())["id"])
    asyncio.run(social.check_bets(AsyncMock()))
    assert social.get_bet(first)["winner"] == BRO
    with as_user(BRO):
        asyncio.run(social.close_bets(AsyncMock()))
    assert social.get_bet(unanswered)["status"] == "expired"


def test_tied_bet_is_a_draw(crew):
    bet_id, _ = social.offer_bet(config.USER_ID, BRO, "reps", 5)
    social.respond_bet(bet_id, BRO, True)
    with as_user(BRO):
        asyncio.run(social.close_bets(AsyncMock()))
    assert social.get_bet(bet_id)["status"] == "draw"


# --------------------------------------------------------------------------
# Overtake alerts
# --------------------------------------------------------------------------

def test_passing_your_brother_alerts_him_a_few_times_a_day_at_most(crew):
    log(BRO, 20)
    log(config.USER_ID, 15)
    assert events("overtake") == []                       # not past him yet
    log(config.USER_ID, 10)
    (event,) = events("overtake")
    assert event["data"] | {"date": None} == {"victim": BRO, "date": None, "mine": 25, "theirs": 20}
    bot = AsyncMock()
    asyncio.run(cs.deliver_events(bot))
    assert "just passed you" in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["chat_id"] == BRO
    for _ in range(5):                                   # back and forth all day
        log(BRO, 20)
        log(config.USER_ID, 25)
    passers = [e["user_id"] for e in events("overtake")]
    assert passers.count(config.USER_ID) == passers.count(BRO) == social.OVERTAKE_ALERTS_PER_DAY


def test_no_overtake_alert_against_someone_at_zero(crew):
    log(config.USER_ID, 30)
    assert events("overtake") == []


# --------------------------------------------------------------------------
# Report card
# --------------------------------------------------------------------------

def test_report_card_counts_the_month(crew):
    today = datetime.strptime(get_current_date_str(), "%Y-%m-%d").date()
    month = today.strftime("%Y-%m")
    first = today.replace(day=1)
    days = [first + timedelta(days=i) for i in range(min(3, today.day))]
    for d in days:
        complete_all_exercises_for_workout(get_or_create_daily_workout(d.isoformat())["id"])
    card = social.report_card(config.USER_ID, month)
    assert card["workouts"] == len(days) and card["best_streak"] == len(days)
    assert card["reps"] > 0 and card["name"] == "Victor"
    assert card["rank"] in "EDCBAS"


def test_report_cards_are_sent_on_the_first(crew):
    today = datetime.strptime(get_current_date_str(), "%Y-%m-%d").date()
    last_month_day = today.replace(day=1) - timedelta(days=3)
    complete_all_exercises_for_workout(get_or_create_daily_workout(last_month_day.isoformat())["id"])
    bot = AsyncMock()
    sent = asyncio.run(social.send_report_cards(bot, today.replace(day=1).isoformat()))
    assert sent == 1                                      # Andrei had no workouts that month
    assert "report card" in bot.send_message.await_args.kwargs["text"]


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def test_api_chat_bets_live_and_pulse(crew):
    results = call([
        ("POST", "/api/action", config.USER_ID, {"type": "chat_send", "text": "first!"}),
        ("POST", "/api/action", config.USER_ID, {"type": "bet_offer", "target": BRO, "kind": "push", "stake": 10}),
        ("POST", "/api/action", config.USER_ID, {"type": "live_start"}),
        ("GET", "/api/pulse", BRO, None),
        ("GET", "/api/state", BRO, None),
    ])
    assert [r[0] for r in results] == [200, 200, 200, 200, 200]
    pulse, state = results[3][1], results[4][1]
    assert pulse["unread"] == 1
    assert next(c for c in pulse["crew"] if c["id"] == config.USER_ID)["live"] is True
    assert state["chat"][0]["text"] == "first!"
    (bet,) = state["bets"]
    assert bet["mine_to_answer"] and bet["stake"] == 10
    answer, = call([("POST", "/api/action", BRO, {"type": "bet_answer", "id": bet["id"], "accept": True})])
    assert answer[1]["message"] == "Bet's on! 🎲"


def test_api_proof_upload_watch_and_vote(crew):
    upload, = call([("UPLOAD", "/api/proof", config.USER_ID, MP4)])
    assert upload[0] == 200
    proof_id = upload[1]["id"]
    watch, vote = call([("GET", f"/api/proof/{proof_id}", BRO, None),
                        ("POST", "/api/action", BRO, {"type": "proof_vote", "id": proof_id, "vote": "legit"})])
    assert watch == (200, MP4)
    assert vote[0] == 200
    assert vote[1]["state"]["chat"][-1]["proof"]["counts"]["legit"] == 1
    outsider, = call([("GET", f"/api/proof/{proof_id}", 999, None)])
    assert outsider[0] == 403
