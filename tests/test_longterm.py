"""Tests for vacation/sick pause, weekly summary, backup verification,
error reports to Telegram, the external heartbeat and /cancel."""

import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

import config
import main as app_main
import monitoring
import scheduler as sched
from database import create_backup, get_connection, verify_backup
from handlers.settings import parse_pause_input
from handlers.workout import build_today_workout_view, workout_callback_handler
from services.stats_service import get_stats_for_period
from services.summary_service import build_weekly_summary, get_new_records, week_bounds
from services.workout_service import (
    calculate_streaks,
    generate_workout_heatmap,
    get_active_pause,
    get_current_date_str,
    get_or_create_daily_workout,
    get_paused_dates,
    resume_from_pause,
    start_pause,
    update_workout_item,
)


@pytest.fixture(autouse=True)
def reset_health():
    monitoring.health.__init__()
    yield


def log_day(date_str, reps=None):
    """Logs every exercise of `date_str` (target reps, or `reps` each)."""
    workout = get_or_create_daily_workout(date_str)
    for item in workout["items"]:
        update_workout_item(item["id"], completed_reps=item["target_reps"] if reps is None else reps)
    return get_or_create_daily_workout(date_str)


def shift(date_str, days):
    return (datetime.strptime(date_str, "%Y-%m-%d").date() + timedelta(days=days)).isoformat()


# --------------------------------------------------------------------------
# Vacation / sick pause
# --------------------------------------------------------------------------

def test_pause_marks_today_paused_and_new_days_have_no_exercises():
    today = "2026-10-10"
    get_or_create_daily_workout(today)  # existing pending workout
    pause = start_pause(3, today_str=today)

    assert (pause["start_date"], pause["end_date"]) == ("2026-10-10", "2026-10-12")
    assert get_or_create_daily_workout(today)["status"] == "paused"

    tomorrow = get_or_create_daily_workout("2026-10-11")
    assert tomorrow["status"] == "paused" and tomorrow["items"] == []
    assert get_or_create_daily_workout("2026-10-13")["status"] == "pending"


def test_paused_days_freeze_the_streak():
    log_day("2026-10-01")
    log_day("2026-10-02")
    start_pause(3, today_str="2026-10-03")  # 03..05, no rows created at all
    assert calculate_streaks(today_str="2026-10-06") == (2, 2)


def test_missed_days_without_pause_break_the_streak():
    log_day("2026-10-01")
    log_day("2026-10-02")
    assert calculate_streaks(today_str="2026-10-06")[0] == 0


def test_starting_a_pause_again_extends_it():
    start_pause(3, today_str="2026-10-10")
    pause = start_pause(10, today_str="2026-10-11")
    assert (pause["start_date"], pause["end_date"]) == ("2026-10-10", "2026-10-20")
    assert len(get_paused_dates()) == 11


def test_resume_mid_pause_keeps_past_days_paused():
    start_pause(7, today_str="2026-10-10")
    get_or_create_daily_workout("2026-10-12")  # created as paused, no items

    assert resume_from_pause(today_str="2026-10-12") is True
    assert get_active_pause("2026-10-12") is None
    assert "2026-10-11" in get_paused_dates() and "2026-10-12" not in get_paused_dates()
    # Today's workout is regenerated with exercises.
    today = get_or_create_daily_workout("2026-10-12")
    assert today["status"] == "pending" and len(today["items"]) == 4


def test_resume_on_first_day_removes_pause_and_restores_progress():
    workout = get_or_create_daily_workout("2026-10-10")
    update_workout_item(workout["items"][0]["id"], completed_reps=10)
    start_pause(5, today_str="2026-10-10")

    assert resume_from_pause(today_str="2026-10-10") is True
    assert get_paused_dates() == set()
    restored = get_or_create_daily_workout("2026-10-10")
    assert restored["status"] == "pending"
    assert restored["items"][0]["completed_reps"] == 10


def test_resume_without_pause_returns_false():
    assert resume_from_pause(today_str="2026-10-10") is False


def test_paused_days_excluded_from_scheduled_stats():
    log_day("2026-10-05")
    start_pause(2, today_str="2026-10-06")
    get_or_create_daily_workout("2026-10-06")
    get_or_create_daily_workout("2026-10-07")
    stats = get_stats_for_period("week", today_str="2026-10-07")
    assert (stats["completed_workouts"], stats["scheduled_workouts"]) == (1, 1)


def test_heatmap_shows_paused_days():
    start_pause(2, today_str=get_current_date_str())
    assert "🟦" in generate_workout_heatmap()


def test_today_view_while_paused_offers_resume():
    today = get_current_date_str()
    start_pause(3, today_str=today)
    text, markup = build_today_workout_view(get_or_create_daily_workout(today), today)
    assert "paused" in text.lower()
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "set_resume" in callbacks and "complete_all" not in callbacks


def test_old_complete_all_button_does_not_log_a_paused_day():
    today = get_current_date_str()
    get_or_create_daily_workout(today)
    start_pause(3, today_str=today)

    query = SimpleNamespace(data="complete_all", answer=AsyncMock(), edit_message_text=AsyncMock(),
                            message=SimpleNamespace(reply_text=AsyncMock()))
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=config.USER_ID))
    context = SimpleNamespace(bot=AsyncMock(), bot_data={}, user_data={})
    asyncio.run(workout_callback_handler(update, context))

    assert get_or_create_daily_workout(today)["status"] == "paused"


@pytest.mark.parametrize("text,expected", [
    ("10", 10), ("1", 1), ("0", None), ("61", None),
    ("2026-10-19", 10), ("2026-10-09", None), ("next week", None),
])
def test_parse_pause_input(text, expected):
    assert parse_pause_input(text, "2026-10-10") == expected


def test_morning_reminder_skipped_while_paused():
    start_pause(2)
    bot = AsyncMock()
    asyncio.run(sched.send_daily_workout_notification(bot))
    bot.send_message.assert_not_awaited()


def test_morning_reminder_welcomes_back_after_pause():
    yesterday = shift(get_current_date_str(), -1)
    with get_connection() as conn:
        conn.execute("INSERT INTO pauses (user_id, start_date, end_date, created_at) VALUES (?, ?, ?, 'x');",
                     (config.USER_ID, yesterday, yesterday))
        conn.commit()
    bot = AsyncMock()
    asyncio.run(sched.send_daily_workout_notification(bot))
    assert "Welcome back" in bot.send_message.await_args.kwargs["text"]


# --------------------------------------------------------------------------
# Weekly summary
# --------------------------------------------------------------------------

def test_week_bounds():
    assert week_bounds("2026-10-11") == ("2026-10-05", "2026-10-11")  # Sunday
    assert week_bounds("2026-10-05") == ("2026-10-05", "2026-10-11")  # Monday


def test_weekly_summary_compares_weeks_and_finds_records():
    log_day("2026-10-01", reps=40)  # last week
    log_day("2026-10-06", reps=60)  # this week
    log_day("2026-10-07")
    text = build_weekly_summary(today_str="2026-10-11", health_line="🩺 Bot health: ok")

    assert "<b>Your week</b> · Mon 5 Oct – Sun 11 Oct" in text
    assert "Workouts  <b>2/2</b>" in text
    assert "Push-ups  <b>110</b> reps (+70 vs last week)" in text
    assert "New personal records" in text and "Push-ups: <b>60 reps</b> in a day (was 40)" in text
    assert "Perfect week" in text
    assert text.endswith("<i>🩺 Bot health: ok</i>")


def test_first_week_has_no_records():
    log_day("2026-10-06")
    assert get_new_records("2026-10-05", "2026-10-11") == []


def test_build_health_line_reports_errors_and_alarm_state(monkeypatch):
    monkeypatch.setattr(config, "HEALTHCHECK_URL", "")
    monitoring.health.errors_since_summary = 2
    monitoring.health.last_backup_ok = True
    line = sched.build_health_line()
    assert "2 error(s) this week" in line
    assert "last backup verified" in line
    assert "outside alarm not set up" in line


def test_weekly_summary_resets_error_counter():
    monitoring.health.errors_since_summary = 3
    bot = AsyncMock()
    asyncio.run(sched.send_weekly_summary(bot))
    bot.send_message.assert_awaited_once()
    assert monitoring.health.errors_since_summary == 0


# --------------------------------------------------------------------------
# Backup verification (restore test)
# --------------------------------------------------------------------------

def test_verify_backup_ok():
    log_day("2026-10-06")
    result = verify_backup(create_backup())
    assert result["ok"], result
    assert result["counts"]["daily_workouts"] == 1


def test_verify_backup_detects_mismatch():
    backup = create_backup()
    log_day("2026-10-06")  # live DB changes after the backup was taken
    result = verify_backup(backup)
    assert not result["ok"] and "live database" in result["message"]


def test_verify_backup_detects_corrupt_file(tmp_path):
    bad = tmp_path / "workout_backup_bad.db"
    bad.write_bytes(b"this is not a database" * 100)
    assert verify_backup(bad)["ok"] is False


def test_check_backup_failure_is_logged_as_error(tmp_path, caplog):
    bad = tmp_path / "workout_backup_bad.db"
    bad.write_bytes(b"junk" * 100)
    with caplog.at_level(logging.ERROR):
        verdict = sched.check_backup(bad)
    assert "FAILED" in verdict
    assert monitoring.health.last_backup_ok is False
    assert any("verification failed" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# Error reports to Telegram
# --------------------------------------------------------------------------

def _record(msg, name="scheduler"):
    return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, None, None)


def test_reporter_cooldown_and_hourly_cap():
    reporter = monitoring.TelegramErrorReporter(AsyncMock(), 1, loop=None, cooldown=3600, max_per_hour=3)
    assert reporter.should_send(_record("boom"), now=0)
    assert not reporter.should_send(_record("boom"), now=10)       # same error, cooling down
    assert reporter.should_send(_record("boom"), now=3700)         # cooldown over
    assert reporter.should_send(_record("other"), now=3701)
    assert reporter.should_send(_record("third"), now=3702)        # 3 sent in the last hour
    assert not reporter.should_send(_record("fourth"), now=3703)   # hourly cap (3) reached
    assert "(+1 similar errors not reported)" in reporter.format_report(_record("boom"))


def test_reporter_sends_error_to_chat_and_counts_it():
    bot = AsyncMock()

    async def run_via_handle():
        reporter = monitoring.TelegramErrorReporter(bot, 42, asyncio.get_running_loop())
        reporter.handle(_record("Error sending daily workout notification: timeout"))
        reporter.handle(logging.LogRecord("x", logging.WARNING, __file__, 1, "just a warning", None, None))
        await asyncio.sleep(0.05)

    asyncio.run(run_via_handle())
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 42
    assert "daily workout notification" in bot.send_message.await_args.kwargs["text"]
    assert monitoring.health.errors_total == 1


def test_reporter_ignores_its_own_failures():
    bot = AsyncMock()

    async def run():
        reporter = monitoring.TelegramErrorReporter(bot, 42, asyncio.get_running_loop())
        record = _record("could not send")
        record.skip_report = True
        reporter.handle(record)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    bot.send_message.assert_not_awaited()


def test_install_error_reporter_replaces_previous():
    async def run():
        loop = asyncio.get_running_loop()
        first = monitoring.install_error_reporter(AsyncMock(), 1, loop)
        second = monitoring.install_error_reporter(AsyncMock(), 1, loop)
        root = logging.getLogger()
        try:
            installed = [h for h in root.handlers if isinstance(h, monitoring.TelegramErrorReporter)]
            assert installed == [second] and first is not second
        finally:
            root.removeHandler(second)

    asyncio.run(run())
    assert monitoring.install_error_reporter(AsyncMock(), 0, None) is None


def test_polling_network_blip_is_not_reported_to_user():
    bot = AsyncMock()
    context = SimpleNamespace(error=NetworkError("Bad Gateway"), bot=bot, bot_data={})
    asyncio.run(app_main.global_error_handler(None, context))
    bot.send_message.assert_not_awaited()


# --------------------------------------------------------------------------
# External heartbeat
# --------------------------------------------------------------------------

class FakeClient:
    calls = []

    def __init__(self, *args, fail=False, **kwargs):
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        FakeClient.calls.append(url)
        if FakeClient.fail_network:
            raise OSError("network down")


@pytest.fixture
def fake_http(monkeypatch):
    FakeClient.calls = []
    FakeClient.fail_network = False
    monkeypatch.setattr(monitoring.httpx, "AsyncClient", FakeClient)
    return FakeClient


def test_heartbeat_pings_url_when_telegram_reachable(fake_http):
    ok = asyncio.run(monitoring.ping_healthcheck("https://hc-ping.com/abc", AsyncMock()))
    assert ok and fake_http.calls == ["https://hc-ping.com/abc"]
    assert monitoring.health.last_heartbeat_ok is True


def test_heartbeat_reports_fail_when_telegram_unreachable(fake_http):
    bot = AsyncMock()
    bot.get_me.side_effect = NetworkError("down")
    ok = asyncio.run(monitoring.ping_healthcheck("https://hc-ping.com/abc", bot))
    assert not ok and fake_http.calls == ["https://hc-ping.com/abc/fail"]


def test_heartbeat_network_failure_is_only_a_warning(fake_http, caplog):
    fake_http.fail_network = True
    with caplog.at_level(logging.WARNING):
        ok = asyncio.run(monitoring.ping_healthcheck("https://hc-ping.com/abc", AsyncMock()))
    assert ok is False
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_heartbeat_job_only_scheduled_with_url(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    monkeypatch.setattr(config, "HEALTHCHECK_URL", "")
    scheduler = AsyncIOScheduler()
    sched.schedule_global_jobs(scheduler, AsyncMock())
    sched.reschedule_daily_job(scheduler, AsyncMock())
    assert scheduler.get_job("heartbeat") is None
    assert scheduler.get_job(f"weekly_summary:{config.USER_ID}") is not None

    monkeypatch.setattr(config, "HEALTHCHECK_URL", "https://hc-ping.com/abc")
    sched.schedule_global_jobs(scheduler, AsyncMock())
    assert scheduler.get_job("heartbeat") is not None


# --------------------------------------------------------------------------
# /cancel
# --------------------------------------------------------------------------

def test_cancel_command_clears_pending_prompts():
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=config.USER_ID))
    context = SimpleNamespace(user_data={"awaiting_setting": "pause_custom"})
    asyncio.run(app_main.cancel_command_handler(update, context))
    assert "awaiting_setting" not in context.user_data
    assert message.reply_text.await_args.args[0] == "❌ Cancelled."
