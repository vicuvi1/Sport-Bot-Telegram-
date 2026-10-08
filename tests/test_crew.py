"""Crew features: invites and joining, live feed, duel, crew streak,
roasts / hypes, auto-roast, leaving and removing."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import scheduler as sched
from database import add_user, as_user, get_users, set_setting
from handlers import crew as crew_handlers
from handlers.start import start_handler
from services import crew_service as cs
from services.workout_service import complete_all_exercises_for_workout, get_current_date_str, get_or_create_daily_workout
from tests.test_html_views import assert_valid_html

BRO = 222000333


@pytest.fixture
def crew():
    """Owner 'Victor' plus brother 'Andrei'."""
    from database import set_user_name
    set_user_name(config.USER_ID, "Victor")
    add_user(BRO, "Andrei")
    return config.USER_ID, BRO


def finish_today(user_id):
    with as_user(user_id):
        workout = get_or_create_daily_workout(get_current_date_str())
        complete_all_exercises_for_workout(workout["id"])


def run(coro):
    return asyncio.run(coro)


def fake_query_update(user_id, data):
    user = SimpleNamespace(id=user_id, first_name="X")
    query = SimpleNamespace(data=data, from_user=user, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            message=SimpleNamespace(reply_text=AsyncMock()))
    return SimpleNamespace(effective_user=user, callback_query=query)


def ctx(bot=None, user_data=None, args=None):
    bot = bot or AsyncMock()
    bot.username = "WorkoutTestBot"
    return SimpleNamespace(bot=bot, bot_data={}, user_data=user_data or {}, args=args or [])


def sent_to(bot):
    return [c.kwargs["chat_id"] for c in bot.send_message.await_args_list]


def texts_to(bot, chat_id):
    return [c.kwargs["text"] for c in bot.send_message.await_args_list if c.kwargs["chat_id"] == chat_id]


# --------------------------------------------------------------------------
# Invites & joining
# --------------------------------------------------------------------------

def test_invite_single_use_and_rejections():
    code = cs.create_invite()
    assert cs.accept_invite("nope", BRO, "Andrei") == "invalid"
    assert cs.accept_invite(code, BRO, "Andrei") == "ok"
    assert cs.accept_invite(code, 444, "Mallory") == "invalid"  # used up
    assert cs.accept_invite(cs.create_invite(), BRO, "Andrei") == "member"
    old = cs.create_invite(now=datetime.now(timezone.utc) - timedelta(hours=49))
    assert cs.accept_invite(old, 555, "Late") == "expired"


def test_crew_is_capped():
    for i in range(cs.CREW_MAX - 1):
        add_user(1000 + i, f"M{i}")
    assert cs.accept_invite(cs.create_invite(), 999, "Extra") == "full"


def test_joining_via_start_link_sets_everything_up():
    code = cs.create_invite()
    user = SimpleNamespace(id=BRO, first_name="Andrei")
    update = SimpleNamespace(effective_user=user, message=SimpleNamespace(reply_text=AsyncMock()))
    context = ctx(args=[f"crew_{code}"])
    scheduler = AsyncIOScheduler()
    context.bot_data["scheduler"] = scheduler

    run(start_handler(update, context))

    assert [u["user_id"] for u in get_users()] == [config.USER_ID, BRO]
    assert scheduler.get_job(f"daily_morning_workout:{BRO}") is not None
    welcome = update.message.reply_text.await_args.args[0]
    assert "Welcome to the crew, Andrei" in welcome
    assert_valid_html(welcome)
    assert sent_to(context.bot) == [config.USER_ID]  # the owner is told


# --------------------------------------------------------------------------
# Live feed
# --------------------------------------------------------------------------

def test_finishing_notifies_the_other_brother(crew):
    owner, bro = crew
    finish_today(bro)
    bot = AsyncMock()
    assert run(cs.deliver_events(bot)) == 1

    assert sent_to(bot) == [owner]
    text = texts_to(bot, owner)[0]
    assert "<b>Andrei</b> just finished today's workout!" in text
    assert "Your move" in text and "0/4" in text
    assert_valid_html(text)
    assert run(cs.deliver_events(bot)) == 0  # delivered only once


def test_feed_says_both_done(crew):
    owner, bro = crew
    finish_today(owner)
    run(cs.deliver_events(AsyncMock()))
    finish_today(bro)
    bot = AsyncMock()
    run(cs.deliver_events(bot))
    assert "You're both done today" in texts_to(bot, owner)[0]


def test_feed_respects_the_off_switch(crew):
    owner, bro = crew
    set_setting("crew_feed", "0")  # owner turns the feed off
    finish_today(bro)
    bot = AsyncMock()
    run(cs.deliver_events(bot))
    bot.send_message.assert_not_awaited()


def test_no_feed_without_a_crew():
    finish_today(config.USER_ID)
    bot = AsyncMock()
    run(cs.deliver_events(bot))
    bot.send_message.assert_not_awaited()


def test_finishing_through_the_buttons_delivers_immediately(crew):
    owner, bro = crew
    update = fake_query_update(bro, "complete_all")
    context = ctx()
    from handlers.workout import workout_callback_handler
    run(workout_callback_handler(update, context))
    assert owner in sent_to(context.bot)


# --------------------------------------------------------------------------
# Duel & crew streak
# --------------------------------------------------------------------------

def test_duel_ranks_and_calls_out(crew):
    owner, bro = crew
    finish_today(owner)

    async def build():
        with as_user(bro):
            return crew_handlers.build_duel()
    text, markup = run(build())
    assert text.index("Victor") < text.index("Andrei")
    assert "<b>Victor</b> is ahead. Andrei, your move!" in text
    assert_valid_html(text)
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"crew_roast:{owner}" in callbacks and f"crew_hype:{owner}" in callbacks


def test_crew_menu_is_valid_for_owner_and_member(crew):
    owner, bro = crew
    text, markup = crew_handlers.build_crew_menu()
    assert_valid_html(text)
    owner_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "crew_invite" in owner_buttons and "crew_leave" not in owner_buttons
    with as_user(bro):
        text, markup = crew_handlers.build_crew_menu()
    assert_valid_html(text)
    member_buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "crew_leave" in member_buttons and "crew_invite" not in member_buttons


def test_crew_streak_counts_days_everyone_trained(crew):
    owner, bro = crew
    for d in ("2026-10-01", "2026-10-02", "2026-10-03"):
        for uid in (owner, bro):
            with as_user(uid):
                complete_all_exercises_for_workout(get_or_create_daily_workout(d)["id"])
    # Day 4: only the owner trains -> today (unfinished) doesn't break it yet.
    with as_user(owner):
        complete_all_exercises_for_workout(get_or_create_daily_workout("2026-10-04")["id"])
    assert cs.crew_streak("2026-10-04") == 3
    # Once day 4 is over without the brother, the streak is broken.
    assert cs.crew_streak("2026-10-05") == 0


def test_crew_streak_needs_two_people():
    finish_today(config.USER_ID)
    assert cs.crew_streak() == 0


# --------------------------------------------------------------------------
# Roasts & hypes
# --------------------------------------------------------------------------

def press(user_id, data, context=None):
    update = fake_query_update(user_id, data)
    context = context or ctx()

    async def go():
        from handlers.start import is_authorized
        assert is_authorized(update)
        await crew_handlers.crew_callback_handler(update, context)
    run(go())
    return update, context


def test_roast_is_delivered_with_a_roast_back_button(crew):
    owner, bro = crew
    finish_today(owner)
    update, context = press(owner, f"crew_roast:{bro}")
    assert sent_to(context.bot) == [bro]
    text = texts_to(context.bot, bro)[0]
    assert "Roast from Victor" in text
    assert_valid_html(text)
    markup = context.bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"crew_roast:{owner}"
    assert "delivered" in update.callback_query.answer.await_args.args[0]


def test_roast_backfires_when_the_target_is_done_and_you_are_not(crew):
    owner, bro = crew
    finish_today(bro)
    update, context = press(owner, f"crew_roast:{bro}")
    context.bot.send_message.assert_not_awaited()
    assert "who's the loser now" in update.callback_query.answer.await_args.args[0]


def test_roasts_off_means_no_roasts(crew):
    owner, bro = crew
    with as_user(bro):
        set_setting("roast_level", "off")
    update, context = press(owner, f"crew_roast:{bro}")
    context.bot.send_message.assert_not_awaited()
    assert "turned roasts off" in update.callback_query.answer.await_args.args[0]


def test_friendly_level_uses_friendly_lines(crew):
    owner, bro = crew
    with as_user(bro):
        set_setting("roast_level", "friendly")
    _, context = press(owner, f"crew_roast:{bro}")
    text = texts_to(context.bot, bro)[0]
    friendly = cs.ROASTS["friendly"]["any"] + cs.ROASTS["friendly"]["ahead"]
    assert any(line.split("{")[0].strip()[:12] in text for line in friendly)
    assert "Loser" not in text


def test_roasts_are_rate_limited(crew):
    owner, bro = crew
    context = ctx()
    for _ in range(cs.ROASTS_PER_DAY):
        press(owner, f"crew_roast:{bro}", context)
    update, _ = press(owner, f"crew_roast:{bro}", context)
    assert context.bot.send_message.await_count == cs.ROASTS_PER_DAY
    assert "Easy" in update.callback_query.answer.await_args.args[0]


def test_hype_always_gets_through(crew):
    owner, bro = crew
    with as_user(bro):
        set_setting("roast_level", "off")
    _, context = press(owner, f"crew_hype:{bro}")
    assert sent_to(context.bot) == [bro]
    assert "Victor" in texts_to(context.bot, bro)[0]


def test_roast_levels_cycle(crew):
    owner, _ = crew
    seen = []
    for _ in range(3):
        press(owner, "crew_roastlvl")
        seen.append(cs.roast_level(owner))
    assert seen == ["friendly", "off", "savage"]


# --------------------------------------------------------------------------
# Auto-roast
# --------------------------------------------------------------------------

def test_auto_roast_when_brother_trained_and_you_did_not(crew):
    owner, bro = crew
    finish_today(owner)
    bot = AsyncMock()
    run(sched.run_as(bro, cs.send_auto_roast, bot))
    assert sent_to(bot) == [bro]
    assert "Victor" in texts_to(bot, bro)[0]
    assert_valid_html(texts_to(bot, bro)[0])


def test_no_auto_roast_if_nobody_trained_or_roasts_off(crew):
    owner, bro = crew
    bot = AsyncMock()
    run(sched.run_as(bro, cs.send_auto_roast, bot))
    bot.send_message.assert_not_awaited()

    finish_today(owner)
    with as_user(bro):
        set_setting("roast_level", "off")
    run(sched.run_as(bro, cs.send_auto_roast, bot))
    bot.send_message.assert_not_awaited()


def test_auto_roast_job_is_scheduled_per_user(crew):
    scheduler = AsyncIOScheduler()
    for user in get_users():
        sched.reschedule_user_jobs(scheduler, AsyncMock(), user["user_id"])
    assert scheduler.get_job(f"crew_auto_roast:{BRO}") is not None


# --------------------------------------------------------------------------
# Leaving & removing
# --------------------------------------------------------------------------

def test_member_can_leave_and_data_is_removed(crew):
    owner, bro = crew
    finish_today(bro)
    _, context = press(bro, "crew_leaveyes")
    assert [u["user_id"] for u in get_users()] == [owner]
    assert owner in sent_to(context.bot)


def test_owner_can_remove_member_but_member_cannot_remove_owner(crew):
    owner, bro = crew
    press(bro, f"crew_rmyes:{owner}")  # not allowed for members
    assert len(get_users()) == 2
    _, context = press(owner, f"crew_rmyes:{bro}")
    assert [u["user_id"] for u in get_users()] == [owner]
    assert bro in sent_to(context.bot)  # told they were removed


def test_owner_cannot_leave(crew):
    owner, _ = crew
    press(owner, "crew_leaveyes")
    assert len(get_users()) == 2
