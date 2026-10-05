"""Tests for the monthly fitness test and the accountability partner."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import scheduler as sched
from database import set_setting
from handlers import fitness, partner as partner_handlers
from handlers.start import start_handler
from services import fitness_test_service as fts
from services import partner_service as ps
from services.workout_service import get_or_create_daily_workout, start_pause, update_workout_item

PARTNER_ID = 555000111


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

def fake_message():
    return SimpleNamespace(reply_text=AsyncMock())


def fake_update(user_id=None, text=None, data=None, first_name="Victor"):
    user = SimpleNamespace(id=user_id or config.USER_ID, first_name=first_name)
    message = fake_message()
    if text is not None:
        message.text = text
    query = None
    if data is not None:
        query = SimpleNamespace(data=data, from_user=user, answer=AsyncMock(),
                                edit_message_text=AsyncMock(), message=fake_message())
    return SimpleNamespace(effective_user=user, message=message, callback_query=query,
                           effective_message=message)


def fake_context(args=None, user_data=None):
    bot = AsyncMock()
    bot.username = "WorkoutTestBot"
    return SimpleNamespace(bot=bot, args=args or [], user_data=user_data if user_data is not None else {},
                           bot_data={})


def run(coro):
    return asyncio.run(coro)


def add_partner(**options):
    code = ps.create_invite()
    assert ps.accept_invite(code, PARTNER_ID, "Ana", config.USER_ID) == "ok"
    for option, on in options.items():
        set_setting(ps.SHARE_OPTIONS[option][0], "1" if on else "0")


def complete(date_str):
    workout = get_or_create_daily_workout(date_str)
    for item in workout["items"]:
        update_workout_item(item["id"], completed_reps=item["target_reps"])


# --------------------------------------------------------------------------
# Fitness test: service
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,unit,expected", [
    ("31", "reps", 31), (" 0 ", "reps", 0), ("1:45", "sec", 105), ("1m45s", "sec", 105),
    ("2 min 05", "sec", 125), ("95", "sec", 95), ("1:45", "reps", None), ("1:75", "sec", None),
    ("abc", "reps", None), ("-3", "reps", None), ("99999", "reps", None), ("4000", "sec", None),
])
def test_parse_result(text, unit, expected):
    assert fts.parse_result(text, unit) == expected


def test_pullups_only_when_enabled():
    assert [t["key"] for t in fts.active_tests()] == ["pushups", "squats", "plank"]
    set_setting("test_pullups", "1")
    assert [t["key"] for t in fts.active_tests()][-1] == "pullups"


def test_month_completion_counts_skips_and_resumes():
    fts.save_result("2026-10", "pushups", 20)
    assert fts.next_test_index("2026-10") == 1
    fts.save_result("2026-10", "squats", None)  # skipped
    assert not fts.is_month_complete("2026-10")
    fts.save_result("2026-10", "plank", 90)
    assert fts.is_month_complete("2026-10")
    fts.save_result("2026-10", "pushups", 22)  # retake overwrites
    assert fts.get_month_results("2026-10")["pushups"] == 22


def test_first_report_is_a_baseline():
    fts.save_result("2026-08", "pushups", 18)
    report = fts.format_month_report("2026-08")
    assert "Max push-ups: *18 reps*" in report and "baseline" in report


def test_report_compares_with_last_test_and_marks_bests():
    fts.save_result("2026-08", "pushups", 18)
    fts.save_result("2026-08", "plank", 90)
    fts.save_result("2026-09", "pushups", 24)
    fts.save_result("2026-09", "plank", 90)
    fts.save_result("2026-09", "squats", None)
    report = fts.format_month_report("2026-09")
    assert "Max push-ups: *24 reps* (+6 vs last test) 🏅" in report
    assert "Longest plank: *90 sec* (same as last test)" in report
    assert "Squats in 2 minutes: skipped" in report
    assert "1 new personal best!" in report


def test_history_shows_trend_since_first_test():
    for month, value in (("2026-08", 18), ("2026-09", 24), ("2026-10", 31)):
        fts.save_result(month, "pushups", value)
    history = fts.format_history()
    assert "18 → 24 → 31" in history
    assert "Since Aug 2026: +13 (+72%), best 31" in history
    assert "`▁▄█`" in history


def test_history_empty_state():
    assert "No results yet" in fts.format_history()


def test_sparkline():
    assert fts.sparkline([5, 5, 5]) == "▄▄▄"
    assert fts.sparkline([1, 8]) == "▁█"
    assert fts.sparkline([]) == ""


# --------------------------------------------------------------------------
# Fitness test: guided flow
# --------------------------------------------------------------------------

def test_full_guided_test_flow():
    context = fake_context()
    start = fake_update(data="test_start")
    run(fitness.fitness_test_callback_handler(start, context))
    assert "Test 1/3: Max push-ups" in start.callback_query.edit_message_text.await_args.args[0]

    for answer, expected_next in (("25", "Test 2/3"), ("40", "Test 3/3"), ("1:30", "Test complete")):
        msg = fake_update(text=answer)
        assert run(fitness.fitness_test_text_input_handler(msg, context)) is True
        assert expected_next in msg.message.reply_text.await_args.args[0]

    month = fts.current_month()
    assert fts.get_month_results(month) == {"pushups": 25, "squats": 40, "plank": 90}
    assert "fitness_test" not in context.user_data
    # No test running: typed numbers are left for other handlers.
    assert run(fitness.fitness_test_text_input_handler(fake_update(text="12"), context)) is False


def test_invalid_result_asks_again():
    context = fake_context(user_data={"fitness_test": {"month": fts.current_month(), "index": 0}})
    msg = fake_update(text="lots")
    assert run(fitness.fitness_test_text_input_handler(msg, context)) is True
    assert "Please reply with a number" in msg.message.reply_text.await_args.args[0]
    assert context.user_data["fitness_test"]["index"] == 0


def test_skip_and_stop():
    context = fake_context()
    run(fitness.fitness_test_callback_handler(fake_update(data="test_start"), context))
    skip = fake_update(data="test_skip")
    run(fitness.fitness_test_callback_handler(skip, context))
    assert "Test 2/3" in skip.callback_query.edit_message_text.await_args.args[0]
    assert fts.get_month_results(fts.current_month()) == {"pushups": None}

    run(fitness.fitness_test_callback_handler(fake_update(data="test_stop"), context))
    assert "fitness_test" not in context.user_data
    # Starting again resumes at the first test without a result.
    again = fake_update(data="test_start")
    run(fitness.fitness_test_callback_handler(again, context))
    assert "Test 2/3" in again.callback_query.edit_message_text.await_args.args[0]


def test_starting_a_test_clears_other_prompts():
    context = fake_context(user_data={"awaiting_setting": "workout_time", "awaiting_reps_item_id": 3})
    run(fitness.fitness_test_callback_handler(fake_update(data="test_start"), context))
    assert "awaiting_setting" not in context.user_data and "awaiting_reps_item_id" not in context.user_data


def test_completed_test_is_shared_with_partner():
    add_partner(tests=True)
    month = fts.current_month()
    fts.save_result(month, "pushups", 20)
    fts.save_result(month, "squats", 40)
    context = fake_context(user_data={"fitness_test": {"month": month, "index": 2}})
    msg = fake_update(text="60")
    run(fitness.fitness_test_text_input_handler(msg, context))

    partner_call = context.bot.send_message.await_args.kwargs
    assert partner_call["chat_id"] == PARTNER_ID
    assert "just finished their monthly fitness test" in partner_call["text"]
    assert "Shared with Ana" in msg.message.reply_text.await_args.args[0]


def test_monthly_reminder_only_until_done():
    bot = AsyncMock()
    run(sched.send_monthly_test_reminder(bot))
    bot.send_message.assert_awaited_once()

    month = fts.current_month()
    for key in ("pushups", "squats", "plank"):
        fts.save_result(month, key, 10)
    bot = AsyncMock()
    run(sched.send_monthly_test_reminder(bot))
    bot.send_message.assert_not_awaited()


# --------------------------------------------------------------------------
# Partner: invites
# --------------------------------------------------------------------------

def test_invite_is_single_use():
    code = ps.create_invite()
    assert ps.accept_invite(code, PARTNER_ID, "Ana", config.USER_ID) == "ok"
    assert ps.accept_invite(code, 777, "Mallory", config.USER_ID) == "invalid"
    assert ps.get_partner()["chat_id"] == PARTNER_ID


def test_invite_rejections():
    code = ps.create_invite(now=datetime.now(timezone.utc) - timedelta(hours=49))
    assert ps.accept_invite(code, PARTNER_ID, "Ana", config.USER_ID) == "expired"
    code = ps.create_invite()
    assert ps.accept_invite("wrong-code", PARTNER_ID, "Ana", config.USER_ID) == "invalid"
    assert ps.accept_invite(code, config.USER_ID, "Me", config.USER_ID) == "own"
    assert ps.get_partner() is None


def test_partner_defaults_and_toggles():
    add_partner()
    partner = ps.get_partner()
    assert (partner["weekly"], partner["tests"], partner["missed"]) == (True, True, False)
    assert ps.toggle_share("missed") is True
    assert ps.shares("missed")["name"] == "Ana"


def test_partner_name_is_sanitized():
    code = ps.create_invite()
    ps.accept_invite(code, PARTNER_ID, "*An_a*", config.USER_ID)
    assert ps.get_partner()["name"] == "An a"


def test_start_link_registers_partner_and_tells_owner():
    code = ps.create_invite()
    update = fake_update(user_id=PARTNER_ID, first_name="Ana")
    context = fake_context(args=[f"partner_{code}"])
    run(start_handler(update, context))

    assert ps.is_partner(PARTNER_ID)
    assert "accountability partner" in update.message.reply_text.await_args.args[0]
    assert context.bot.send_message.await_args.kwargs["chat_id"] == config.USER_ID


def test_partner_invite_button_sends_link():
    query_update = fake_update(data="partner_invite")
    context = fake_context()
    run(partner_handlers.partner_callback_handler(query_update, context))
    link_text = query_update.callback_query.message.reply_text.await_args.args[0]
    assert "https://t.me/WorkoutTestBot?start=partner_" in link_text
    assert ps.pending_invite_expiry() is not None


def test_partner_messages_get_role_explanation_and_strangers_nothing():
    add_partner()
    update = fake_update(user_id=PARTNER_ID, text="hi")
    assert run(partner_handlers.reply_to_partner_message(update)) is True
    assert "accountability partner" in update.message.reply_text.await_args.args[0]

    stranger = fake_update(user_id=999, text="hi")
    assert run(partner_handlers.reply_to_partner_message(stranger)) is False
    stranger.message.reply_text.assert_not_awaited()


def test_partner_can_stop():
    add_partner()
    update = fake_update(user_id=PARTNER_ID)
    context = fake_context()
    run(partner_handlers.partner_stop_handler(update, context))
    assert ps.get_partner() is None
    assert context.bot.send_message.await_args.kwargs["chat_id"] == config.USER_ID


def test_stranger_stop_does_nothing():
    add_partner()
    update = fake_update(user_id=999)
    run(partner_handlers.partner_stop_handler(update, fake_context()))
    assert ps.is_partner(PARTNER_ID)
    update.message.reply_text.assert_not_awaited()


def test_owner_can_remove_partner_and_partner_is_told():
    add_partner()
    update = fake_update(data="partner_remove_confirm")
    context = fake_context()
    run(partner_handlers.partner_callback_handler(update, context))
    assert ps.get_partner() is None
    assert context.bot.send_message.await_args.kwargs["chat_id"] == PARTNER_ID


# --------------------------------------------------------------------------
# Partner: high-fives
# --------------------------------------------------------------------------

def test_high_five_once_per_day():
    add_partner()
    context = fake_context()
    first = fake_update(user_id=PARTNER_ID, data="partner_cheer")
    run(partner_handlers.partner_cheer_handler(first, context))
    assert "High-five sent" in first.callback_query.answer.await_args.args[0]
    assert context.bot.send_message.await_args.kwargs["chat_id"] == config.USER_ID

    second = fake_update(user_id=PARTNER_ID, data="partner_cheer")
    run(partner_handlers.partner_cheer_handler(second, context))
    assert "already" in second.callback_query.answer.await_args.args[0]
    assert context.bot.send_message.await_count == 1


def test_high_five_from_stranger_is_refused():
    add_partner()
    context = fake_context()
    update = fake_update(user_id=999, data="partner_cheer")
    run(partner_handlers.partner_cheer_handler(update, context))
    context.bot.send_message.assert_not_awaited()


# --------------------------------------------------------------------------
# Partner: missed workouts
# --------------------------------------------------------------------------

def test_count_missed_in_a_row():
    complete("2026-10-01")
    get_or_create_daily_workout("2026-10-02")  # pending, never done
    get_or_create_daily_workout("2026-10-03")
    assert ps.count_missed_in_a_row("2026-10-04") == 2
    assert ps.count_missed_in_a_row("2026-10-02") == 0


def test_rest_and_paused_days_do_not_count_as_missed():
    set_setting("workout_days", "0,1,2,3,4")  # weekdays only
    complete("2026-10-02")  # Friday
    # Sat/Sun are rest days; Mon 10-05 paused; Tue 10-06 missed
    start_pause(1, today_str="2026-10-05")
    get_or_create_daily_workout("2026-10-06")
    assert ps.count_missed_in_a_row("2026-10-07") == 1


def test_no_history_means_nothing_missed():
    assert ps.count_missed_in_a_row("2026-10-07") == 0


def test_missed_alert_only_when_opted_in_and_only_once(monkeypatch):
    complete("2026-10-01")
    for day in ("2026-10-02", "2026-10-03", "2026-10-04"):
        get_or_create_daily_workout(day)
    add_partner()
    assert ps.should_alert_missed("2026-10-05") is False  # opted out by default

    ps.toggle_share("missed")
    assert ps.should_alert_missed("2026-10-05") is True
    assert ps.should_alert_missed("2026-10-05") is False  # same day: already sent


def test_missed_check_job_alerts_partner_and_owner(monkeypatch):
    add_partner(missed=True)
    monkeypatch.setattr(sched, "get_current_date_str", lambda *a, **k: "2026-10-05")
    complete("2026-10-01")
    for day in ("2026-10-02", "2026-10-03", "2026-10-04"):
        get_or_create_daily_workout(day)

    bot = AsyncMock()
    run(sched.check_missed_workouts(bot))
    recipients = [c.kwargs["chat_id"] for c in bot.send_message.await_args_list]
    assert recipients == [PARTNER_ID, config.USER_ID]


# --------------------------------------------------------------------------
# Partner: weekly summary
# --------------------------------------------------------------------------

def test_weekly_summary_goes_to_partner_without_health_line():
    add_partner(weekly=True)
    bot = AsyncMock()
    run(sched.send_weekly_summary(bot))
    owner_call, partner_call = bot.send_message.await_args_list
    assert "Shared with Ana" in owner_call.kwargs["text"]
    assert partner_call.kwargs["chat_id"] == PARTNER_ID
    assert "Weekly update from" in partner_call.kwargs["text"]
    assert "Bot health" not in partner_call.kwargs["text"]
    assert partner_call.kwargs["reply_markup"] is not None  # high-five button


def test_weekly_summary_not_shared_when_turned_off():
    add_partner(weekly=False)
    bot = AsyncMock()
    run(sched.send_weekly_summary(bot))
    assert [c.kwargs["chat_id"] for c in bot.send_message.await_args_list] == [config.USER_ID]
