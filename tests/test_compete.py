"""Crew competition: points, challenges, weekly results and forfeits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from database import add_user, as_user, get_connection, set_user_name
from handlers import compete as compete_handlers
from handlers import crew as crew_handlers
from handlers.start import is_authorized
from services import compete_service as cmp
from services.fitness_test_service import save_result
from services.summary_service import week_bounds
from services.workout_service import (
    complete_all_exercises_for_workout,
    delete_exercise,
    get_current_date_str,
    get_exercises,
    get_or_create_daily_workout,
    start_quick_workout,
    update_workout_item,
)
from tests.test_html_views import assert_valid_html

BRO = 222000333
WEEK = ("2026-10-05", "2026-10-11")  # Mon..Sun


@pytest.fixture
def crew():
    set_user_name(config.USER_ID, "Victor")
    add_user(BRO, "Andrei")
    return config.USER_ID, BRO


def run(coro):
    return asyncio.run(coro)


def finish(user_id, date_str=None, quick=False):
    with as_user(user_id):
        workout = get_or_create_daily_workout(date_str or get_current_date_str())
        if quick:
            start_quick_workout(workout["id"])
        complete_all_exercises_for_workout(workout["id"])


def messages(bot):
    return [(c.kwargs["chat_id"], c.kwargs["text"]) for c in bot.send_message.await_args_list]


# --------------------------------------------------------------------------
# Points
# --------------------------------------------------------------------------

def test_points_for_workouts_and_full_targets(crew):
    owner, _ = crew
    finish(owner, "2026-10-05")
    finish(owner, "2026-10-06", quick=True)
    p = cmp.points_breakdown(owner, *WEEK)
    assert (p["workout"], p["full"]) == (20, 5)
    assert p["total"] == 25


def test_points_for_wake_ups_and_test_records(crew):
    owner, _ = crew
    with get_connection() as conn:
        conn.execute("INSERT INTO wake_logs (user_id, date, target, challenge, status) VALUES (?, '2026-10-06', '06:30', 1, 'on_time');", (owner,))
        conn.execute("INSERT INTO wake_logs (user_id, date, target, challenge, status) VALUES (?, '2026-10-07', '06:30', 1, 'late');", (owner,))
        conn.commit()
    save_result("2026-09", "pushups", 20)
    save_result("2026-10", "pushups", 25)
    with get_connection() as conn:  # recorded during the week
        conn.execute("UPDATE fitness_results SET recorded_at = '2026-10-06T08:00:00' WHERE month = '2026-10';")
        conn.commit()
    p = cmp.points_breakdown(owner, *WEEK)
    assert p["wake"] == 5 and p["test_pr"] == 20


def test_first_test_is_not_a_record(crew):
    owner, _ = crew
    save_result("2026-10", "pushups", 25)
    with get_connection() as conn:
        conn.execute("UPDATE fitness_results SET recorded_at = '2026-10-06T08:00:00';")
        conn.commit()
    assert cmp.points_breakdown(owner, *WEEK)["test_pr"] == 0


def test_challenge_points_go_to_the_right_person(crew):
    owner, bro = crew
    with get_connection() as conn:
        for status in ("won", "lost", "declined"):
            conn.execute("INSERT INTO challenges (challenger, target, kind, date, status, created_at) "
                         "VALUES (?, ?, 'full', '2026-10-06', ?, 'x');", (owner, bro, status))
        conn.commit()
    assert cmp.points_breakdown(bro, *WEEK)["challenge"] == 15           # won as target
    assert cmp.points_breakdown(owner, *WEEK)["challenge"] == 15 + 5     # target lost + chickened out


def test_standings_best_first(crew):
    owner, bro = crew
    finish(bro, get_current_date_str())
    table = cmp.standings()
    assert [r["user_id"] for r in table] == [bro, owner]
    assert table[0]["points"] == 15


# --------------------------------------------------------------------------
# Challenges
# --------------------------------------------------------------------------

def test_offer_rules(crew):
    owner, bro = crew
    assert cmp.offer_challenge(owner, owner, "full") is None
    assert cmp.offer_challenge(owner, bro, "nope") is None
    first = cmp.offer_challenge(owner, bro, "full")
    assert first and cmp.offer_challenge(owner, bro, "push100") is None  # one open per day
    assert cmp.respond_challenge(first, owner, True) is None             # only the target answers
    assert cmp.respond_challenge(first, bro, True)["status"] == "accepted"
    assert cmp.respond_challenge(first, bro, False) is None              # already answered


@pytest.mark.parametrize("kind,setup,expected", [
    ("push100", lambda items: update_workout_item(items[0]["id"], completed_reps=100), True),
    ("push100", lambda items: update_workout_item(items[0]["id"], completed_reps=99), False),
    ("plank180", lambda items: update_workout_item(items[3]["id"], completed_reps=180), True),
    ("full", lambda items: None, False),
])
def test_challenge_conditions(crew, kind, setup, expected):
    owner, bro = crew
    today = get_current_date_str()
    with as_user(bro):
        setup(get_or_create_daily_workout(today)["items"])
    challenge = {"kind": kind, "target": bro, "date": today}
    assert cmp.challenge_met(challenge) is expected


def test_early_bird_challenge_uses_completion_time(crew):
    owner, bro = crew
    today = get_current_date_str()
    finish(bro, today)
    with get_connection() as conn:
        conn.execute("UPDATE daily_workouts SET completed_at = ? WHERE user_id = ?;", (today + "T08:45:00", bro))
        conn.commit()
    assert cmp.challenge_met({"kind": "early", "target": bro, "date": today}) is True
    with get_connection() as conn:
        conn.execute("UPDATE daily_workouts SET completed_at = ? WHERE user_id = ?;", (today + "T09:15:00", bro))
        conn.commit()
    assert cmp.challenge_met({"kind": "early", "target": bro, "date": today}) is False


def test_accepted_challenge_is_won_on_the_next_tick(crew):
    owner, bro = crew
    cid = cmp.offer_challenge(owner, bro, "full")
    cmp.respond_challenge(cid, bro, True)
    finish(bro)
    bot = AsyncMock()
    assert run(cmp.check_challenges(bot)) == 1
    assert cmp.get_challenge(cid)["status"] == "won"
    assert {chat for chat, _ in messages(bot)} == {owner, bro}


def test_deadline_loses_unfinished_and_expires_unanswered(crew):
    owner, bro = crew
    accepted = cmp.offer_challenge(owner, bro, "full")
    cmp.respond_challenge(accepted, bro, True)
    with get_connection() as conn:  # a second, never answered offer from yesterday's style
        conn.execute("INSERT INTO challenges (challenger, target, kind, date, status, created_at) "
                     "VALUES (?, ?, 'push100', ?, 'offered', 'x');", (owner, bro, get_current_date_str()))
        conn.commit()
    bot = AsyncMock()
    with as_user(bro):
        run(cmp.close_challenges(bot))
    assert cmp.get_challenge(accepted)["status"] == "lost"
    statuses = {r["status"] for r in cmp.open_challenges("expired")}
    assert statuses == {"expired"}
    texts = dict(messages(bot))
    assert "Challenge lost" in texts[bro] and "+15 pts for you" in texts[owner]
    for _, text in messages(bot):
        assert_valid_html(text)


def test_only_sensible_challenges_are_offered(crew):
    owner, bro = crew
    with as_user(bro):
        push = next(e for e in get_exercises() if e["name"] == "Push-ups")
        delete_exercise(push["id"])
    kinds = [k for k, _ in compete_handlers.challenge_kinds_for(bro)]
    assert "push100" not in kinds and "full" in kinds and "squat150" in kinds


# --------------------------------------------------------------------------
# Weekly results & forfeits
# --------------------------------------------------------------------------

def test_weekly_results_create_a_forfeit_for_the_loser(crew):
    owner, bro = crew
    finish(owner, "2026-10-06")
    bot = AsyncMock()
    fid = run(cmp.send_weekly_results(bot, today_str="2026-10-11"))
    forfeit = cmp.get_forfeit(fid)
    assert (forfeit["winner"], forfeit["loser"], forfeit["status"]) == (owner, bro, "choosing")
    sent = messages(bot)
    assert [chat for chat, _ in sent] == [owner, bro, owner]  # results to both, pick menu to the winner
    assert "Victor</b> wins the week" in sent[0][1]
    for _, text in sent:
        assert_valid_html(text)
    pick_buttons = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert pick_buttons[0][0].callback_data == f"cmp_fpick:{fid}:0"
    assert run(cmp.send_weekly_results(AsyncMock(), today_str="2026-10-11")) is None  # once per week


def test_tie_means_no_forfeit(crew):
    bot = AsyncMock()
    assert run(cmp.send_weekly_results(bot, today_str="2026-10-11")) is None
    assert "tie" in messages(bot)[0][1]


def test_no_results_without_a_crew():
    bot = AsyncMock()
    assert run(cmp.send_weekly_results(bot)) is None
    bot.send_message.assert_not_awaited()


def test_forfeit_pick_and_done_rules(crew):
    owner, bro = crew
    fid = cmp.create_forfeit("2026-10-05", owner, bro)
    assert cmp.pick_forfeit(fid, bro, 0) is None                 # loser can't pick
    picked = cmp.pick_forfeit(fid, owner, 0)
    assert picked["task"] == cmp.FORFEITS[0] and picked["status"] == "pending"
    assert cmp.complete_forfeit(fid, owner) is None              # winner can't mark it done
    done = cmp.complete_forfeit(fid, bro)
    assert done["status"] == "done"
    assert cmp.open_forfeits() == []
    week = week_bounds(get_current_date_str())
    assert cmp.points_breakdown(bro, *week)["forfeit"] == 5


def test_auto_pick_when_winner_forgets(crew):
    owner, bro = crew
    fid = cmp.create_forfeit("2026-10-05", owner, bro)
    bot = AsyncMock()
    run(cmp.auto_pick_forfeits(bot))
    assert cmp.get_forfeit(fid)["task"] in cmp.FORFEITS
    assert [chat for chat, _ in messages(bot)] == [bro, owner]


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------

def press(user_id, data, bot=None):
    user = SimpleNamespace(id=user_id, first_name="X")
    query = SimpleNamespace(data=data, from_user=user, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            message=SimpleNamespace(reply_text=AsyncMock()))
    update = SimpleNamespace(effective_user=user, callback_query=query)
    context = SimpleNamespace(bot=bot or AsyncMock(), bot_data={}, user_data={})

    async def go():
        assert is_authorized(update)
        await compete_handlers.compete_callback_handler(update, context)
    run(go())
    return query, context.bot


def test_challenge_flow_through_buttons(crew):
    owner, bro = crew
    query, bot = press(owner, f"cmp_kind:{bro}:full")
    chat, text = messages(bot)[0]
    assert chat == bro and "Victor challenges you" in text
    assert_valid_html(text)
    accept = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    cid = int(accept.split(":")[1])

    _, bot = press(bro, f"cmp_dec:{cid}")
    chat, text = messages(bot)[0]
    assert chat == owner and "chickened out" in text
    assert cmp.get_challenge(cid)["status"] == "declined"


def test_forfeit_buttons(crew):
    owner, bro = crew
    fid = cmp.create_forfeit("2026-10-05", owner, bro)
    _, bot = press(owner, f"cmp_fpick:{fid}:r")
    assert messages(bot)[0][0] == bro and "Forfeit time" in messages(bot)[0][1]
    _, bot = press(bro, f"cmp_fdone:{fid}")
    assert messages(bot)[0][0] == owner and "completed the forfeit" in messages(bot)[0][1]


def test_points_and_duel_screens(crew):
    owner, bro = crew
    finish(owner)
    cmp.pick_forfeit(cmp.create_forfeit("2026-10-05", owner, bro), owner, 1)
    text, _ = compete_handlers.build_points()
    assert_valid_html(text)
    assert "Andrei owes" in text
    duel, _ = crew_handlers.build_duel()
    assert_valid_html(duel)
    assert "🏆 This week: Victor <b>15</b>" in duel
    assert "still owes" in duel
