"""Wake-up challenge: checks, answers, deadlines, streaks, crew feed, scheduling."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import scheduler as sched
from database import add_user, as_user, get_connection, set_setting, set_user_name
from handlers import crew as crew_handlers
from handlers import wake as wake_handlers
from services import crew_service as cs
from services import wake_service as ws
from services.workout_service import get_current_date_str, start_pause
from tests.test_html_views import assert_valid_html

BRO = 222000333


@pytest.fixture
def crew():
    set_user_name(config.USER_ID, "Victor")
    add_user(BRO, "Andrei")
    return config.USER_ID, BRO


def at(date_str, hhmm):
    h, m = (int(p) for p in hhmm.split(":"))
    return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=h, minute=m)


def run(coro):
    return asyncio.run(coro)


def new_check(target="06:30"):
    set_setting("wake_time", target)
    set_setting("wake_enabled", "1")
    return ws.start_check()


# --------------------------------------------------------------------------
# Checks and answers
# --------------------------------------------------------------------------

def test_check_is_created_once_per_day():
    log = new_check()
    assert 1 <= log["challenge"] <= 9 and log["status"] == "pending" and log["target"] == "06:30"
    assert ws.start_check()["challenge"] == log["challenge"]


def test_no_check_while_paused():
    start_pause(2)
    assert new_check() is None


def test_answer_choices_include_the_right_number():
    for challenge in range(1, 10):
        choices = ws.answer_choices(challenge)
        assert len(choices) == 4 and len(set(choices)) == 4 and challenge in choices


def test_on_time_within_grace_period():
    log = new_check("06:30")
    out = ws.answer(log["date"], log["challenge"], now=at(log["date"], "06:38"))
    assert out["result"] == "on_time" and out["log"]["minutes_late"] == 8
    assert ws.answer(log["date"], log["challenge"])["result"] == "answered"


def test_late_answer_counts_minutes():
    log = new_check("06:30")
    out = ws.answer(log["date"], log["challenge"], now=at(log["date"], "07:12"))
    assert out["result"] == "late" and out["log"]["minutes_late"] == 42


def test_wrong_number_keeps_the_check_open():
    log = new_check()
    wrong = next(n for n in range(1, 10) if n != log["challenge"])
    assert ws.answer(log["date"], wrong)["result"] == "wrong"
    assert ws.get_log(log["date"])["status"] == "pending"


def test_old_checks_expire():
    log = new_check()
    assert ws.answer("2020-01-01", log["challenge"])["result"] == "expired"


def test_deadline_marks_missed_once():
    new_check()
    assert ws.close_check() is True
    assert ws.get_log(get_current_date_str())["status"] == "missed"
    assert ws.close_check() is False


def test_deadline_does_not_touch_answered_checks():
    log = new_check("06:30")
    ws.answer(log["date"], log["challenge"], now=at(log["date"], "06:31"))
    assert ws.close_check() is False


def test_wake_streak_counts_on_time_in_a_row():
    with get_connection() as conn:
        for date, status in (("2026-10-01", "late"), ("2026-10-02", "on_time"),
                             ("2026-10-03", "on_time"), ("2026-10-04", "on_time")):
            conn.execute("INSERT INTO wake_logs (user_id, date, target, challenge, status) VALUES (?, ?, '06:30', 1, ?);",
                         (config.USER_ID, date, status))
        conn.commit()
    assert ws.wake_streak() == 3


def test_deadline_time_wraps_midnight():
    assert ws.deadline_time("06:30") == (7, 30)
    assert ws.deadline_time("23:30") == (0, 30)


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

def test_wake_check_message_has_number_buttons():
    set_setting("wake_enabled", "1")
    bot = AsyncMock()
    run(ws.send_wake_check(bot))
    kwargs = bot.send_message.await_args.kwargs
    log = ws.get_log(get_current_date_str())
    assert f"Tap the <b>{log['challenge']}</b>" in kwargs["text"]
    assert_valid_html(kwargs["text"])
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert f"wake:{log['date']}:{log['challenge']}" in [b.callback_data for b in buttons]


def test_no_wake_check_when_disabled():
    bot = AsyncMock()
    run(ws.send_wake_check(bot))
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("hhmm,expected,button", [
    ("06:31", "is up at 06:31 ✅", "crew_hype"),
    ("07:12", "42 min late", "crew_roast"),
])
def test_crew_hears_about_wake_ups(crew, hhmm, expected, button):
    owner, bro = crew
    with as_user(bro):
        log = new_check("06:30")
        ws.answer(log["date"], log["challenge"], now=at(log["date"], hhmm))
    bot = AsyncMock()
    run(cs.deliver_events(bot))
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == owner and expected in kwargs["text"]
    assert_valid_html(kwargs["text"])
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data.startswith(button)


def test_crew_hears_about_oversleeping(crew):
    owner, bro = crew
    bot = AsyncMock()
    with as_user(bro):
        new_check("07:00")
    run(sched.run_as(bro, ws.close_wake_check, bot))
    assert "slept through" in bot.send_message.await_args.kwargs["text"]  # told themselves
    bot = AsyncMock()
    run(cs.deliver_events(bot))
    assert "slept through the 07:00 wake-up check" in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["chat_id"] == owner


# --------------------------------------------------------------------------
# Buttons, menu, scheduling
# --------------------------------------------------------------------------

def press(data, context=None):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            message=SimpleNamespace(reply_text=AsyncMock()))
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=config.USER_ID))
    context = context or SimpleNamespace(bot=AsyncMock(), bot_data={}, user_data={})
    run(wake_handlers.wake_callback_handler(update, context))
    return query, context


def test_tapping_the_right_number(crew):
    log = new_check("00:00")  # long ago today: late, but answered
    query, _ = press(f"wake:{log['date']}:{log['challenge']}")
    text = query.edit_message_text.await_args.args[0]
    assert "Up at" in text
    assert_valid_html(text)


def test_tapping_the_wrong_number():
    log = new_check()
    wrong = next(n for n in range(1, 10) if n != log["challenge"])
    query, _ = press(f"wake:{log['date']}:{wrong}")
    assert "Wrong number" in query.answer.await_args.args[0]
    query.edit_message_text.assert_not_awaited()


def test_menu_presets_turn_it_on_and_schedule(crew):
    scheduler = AsyncIOScheduler()
    context = SimpleNamespace(bot=AsyncMock(), bot_data={"scheduler": scheduler}, user_data={})
    query, _ = press("wake_set:06:00", context)
    assert ws.wake_enabled() and ws.wake_time() == "06:00"
    assert scheduler.get_job(f"wake_check:{config.USER_ID}") is not None
    assert scheduler.get_job(f"wake_deadline:{config.USER_ID}") is not None
    assert_valid_html(query.edit_message_text.await_args.args[0])

    press("wake_toggle", context)  # off again
    assert not ws.wake_enabled()
    assert scheduler.get_job(f"wake_check:{config.USER_ID}") is None


@pytest.mark.parametrize("typed,ok", [("6:45", True), ("06:45", True), ("24:00", False), ("soon", False)])
def test_custom_wake_time(typed, ok):
    message = SimpleNamespace(text=typed, reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=config.USER_ID))
    context = SimpleNamespace(bot=AsyncMock(), bot_data={}, user_data={"awaiting_setting": "wake_time"})
    assert run(wake_handlers.wake_time_text_input(update, context)) is True
    if ok:
        assert ws.wake_time() == "06:45" and "awaiting_setting" not in context.user_data
    else:
        assert "HH:MM" in message.reply_text.await_args.args[0]


def test_duel_shows_wake_ups(crew):
    owner, _ = crew
    log = new_check("06:30")
    ws.answer(log["date"], log["challenge"], now=at(log["date"], "06:31"))
    text, _ = crew_handlers.build_duel()
    assert "☀️ 06:31 ✅" in text
