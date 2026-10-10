"""Simple mode (the default) and logging reps by typing them."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import main
import scheduler as sched
from database import add_user, get_connection, set_setting
from handlers import quicklog
from handlers.start import get_main_menu_keyboard, help_handler
from services import crew_service as cs
from services.workout_service import get_current_date_str, get_or_create_daily_workout, start_pause

BRO = 222000333


def run(coro):
    return asyncio.run(coro)


def fake_update(text=None, data=None):
    user = SimpleNamespace(id=config.USER_ID, first_name="Victor")
    message = SimpleNamespace(reply_text=AsyncMock(), text=text)
    query = SimpleNamespace(data=data, from_user=user, answer=AsyncMock(), edit_message_text=AsyncMock()) if data else None
    return SimpleNamespace(effective_user=user, message=message, callback_query=query, effective_message=message)


def context():
    return SimpleNamespace(bot=AsyncMock(), args=[], user_data={}, bot_data={})


def items():
    return {it["exercise_name"]: it for it in get_or_create_daily_workout(get_current_date_str())["items"]}


@pytest.fixture
def simple(monkeypatch):
    monkeypatch.setattr(config, "FULL_MODE", False)
    monkeypatch.setattr(config, "WEBAPP_ENABLED", False)


# --------------------------------------------------------------------------
# Typing reps
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("35 push-ups", [("Push-ups", 35)]),
    ("push 35", [("Push-ups", 35)]),
    ("pushups: 40", [("Push-ups", 40)]),
    ("30 push 40 squats", [("Push-ups", 30), ("Squats", 40)]),
    ("squats 20, situps 15", [("Squats", 20), ("Sit-ups", 15)]),
    ("I did 50 squats", [("Squats", 50)]),
    ("plank 2 min", [("Plank", 120)]),
    ("plank 90s", [("Plank", 90)]),
    ("-10 push", [("Push-ups", -10)]),
    ("20 burpees", []),
    ("hello 5", []),
])
def test_parse(text, expected):
    found, _ = quicklog.parse(text, list(items().values()))
    assert [(it["exercise_name"], n) for it, n in found] == expected


def test_typing_logs_reps_and_shows_today():
    update = fake_update("35 push-ups and 50 squats")
    assert run(quicklog.quick_log_handler(update, context())) is True
    now = items()
    assert now["Push-ups"]["completed_reps"] == 35 and now["Squats"]["completed_reps"] == 50
    assert now["Squats"]["status"] == "completed"
    reply = update.message.reply_text.await_args
    assert "Push-ups +35" in reply.args[0] and "✅ Squats +50" in reply.args[0]
    assert reply.kwargs["reply_markup"] is not None          # today's buttons come back too


def test_a_bare_number_asks_which_exercise_then_logs_it():
    update = fake_update("25")
    assert run(quicklog.quick_log_handler(update, context())) is True
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    data = [row[0].callback_data for row in markup.inline_keyboard]
    push = next(d for d in data if d.endswith(f":{items()['Push-ups']['id']}:25") or f":{items()['Push-ups']['id']}:" in d)
    assert re.match(main.QUICKLOG_CALLBACK_PATTERN, push)
    tap = fake_update(data=push)
    run(quicklog.quick_log_callback_handler(tap, context()))
    assert items()["Push-ups"]["completed_reps"] == 25
    assert "Push-ups +25" in tap.callback_query.edit_message_text.await_args.args[0]


def test_text_that_isnt_a_log_falls_through():
    assert run(quicklog.quick_log_handler(fake_update("hello"), context())) is False
    assert run(quicklog.quick_log_handler(fake_update("20 burpees"), context())) is False
    assert run(quicklog.quick_log_handler(fake_update("0"), context())) is False


def test_nothing_is_logged_on_a_paused_day():
    start_pause(3)
    update = fake_update("30 push")
    assert run(quicklog.quick_log_handler(update, context())) is True
    assert "rest or paused" in update.message.reply_text.await_args.args[0]


# --------------------------------------------------------------------------
# Simple mode
# --------------------------------------------------------------------------

def test_simple_menu_and_help(simple):
    rows = [[b.text for b in row] for row in get_main_menu_keyboard().keyboard]
    assert rows == [["🏋️ Today's Workout", "📊 Progress"], ["📅 History", "⚙️ Settings"]]
    update = fake_update()
    run(help_handler(update, context()))
    text = update.message.reply_text.await_args.args[0]
    assert "35 push-ups" in text and "/duel" not in text and "/crew" not in text


def test_crew_stays_quiet_in_simple_mode(simple):
    add_user(BRO, "Andrei")
    cs.record_event("workout_done", {}, user_id=BRO)
    bot = AsyncMock()
    run(cs.tick(bot))
    bot.send_message.assert_not_called()
    assert cs.pending_events() == []                         # dropped, not piled up for later


def test_simple_mode_schedules_only_personal_jobs(simple):
    scheduler = AsyncIOScheduler()
    set_setting("wake_enabled", "1")
    sched.reschedule_user_jobs(scheduler, AsyncMock(), config.USER_ID)
    sched.schedule_global_jobs(scheduler, AsyncMock())
    ids = {job.id.split(":")[0] for job in scheduler.get_jobs()}
    assert "daily_morning_workout" in ids and "daily_evening_nudge" in ids
    assert not ids & {"crew_auto_roast", "challenge_deadline", "bet_deadline", "penalty_check", "wake_check",
                      "crew_weekly_results", "crew_season_results", "crew_report_cards"}


def test_simple_mode_removes_the_app_button(simple):
    application = SimpleNamespace(bot=AsyncMock(), bot_data={})
    run(main.start_mini_app(application))
    button = application.bot.set_chat_menu_button.await_args.kwargs["menu_button"]
    assert type(button).__name__ == "MenuButtonCommands"
    assert "webapp_runner" not in application.bot_data
