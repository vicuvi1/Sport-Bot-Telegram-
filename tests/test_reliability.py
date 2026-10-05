"""Tests for persistent alerts, snooze, local timestamps, label sanitizing,
backup pruning, /status and duplicate-instance detection."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.error import Conflict

import config
import main as app_main
import scheduler as sched
from database import (
    add_scheduled_alert,
    get_scheduled_alerts,
    set_setting,
    create_backup,
    prune_backups,
)
from handlers.status import build_status_text, format_duration
from handlers.workout import timer_callback_data, MAX_CALLBACK_DATA_BYTES
from services.workout_service import (
    add_exercise,
    get_exercises,
    get_or_create_daily_workout,
    complete_all_exercises_for_workout,
    get_current_date_str,
    local_now_iso,
    sanitize_label,
)


def _iso(dt):
    return dt.isoformat()


# --------------------------------------------------------------------------
# Persistent alerts
# --------------------------------------------------------------------------

def test_schedule_alert_is_stored_and_scheduled():
    async def run():
        scheduler = AsyncIOScheduler()
        scheduler.start()
        try:
            alert_id = sched.schedule_alert(scheduler, AsyncMock(), sched.ALERT_TIMER, 60, "Rest", 42)
            assert scheduler.get_job(f"alert_{alert_id}") is not None
        finally:
            scheduler.shutdown(wait=False)
        return alert_id

    alert_id = asyncio.run(run())
    stored = get_scheduled_alerts()
    assert [a["id"] for a in stored] == [alert_id]
    assert stored[0]["kind"] == "timer" and stored[0]["seconds"] == 60


def test_restore_alerts_reschedules_future_and_drops_stale():
    now = datetime.now(timezone.utc)
    future_id = add_scheduled_alert("timer", _iso(now + timedelta(seconds=30)), "Rest", 60, 42)
    stale_timer = add_scheduled_alert("timer", _iso(now - timedelta(minutes=10)), "Rest", 60, 42)
    late_snooze = add_scheduled_alert("snooze", _iso(now - timedelta(minutes=10)), "Snooze", 3600, 42)
    stale_snooze = add_scheduled_alert("snooze", _iso(now - timedelta(hours=5)), "Snooze", 3600, 42)

    scheduler = AsyncIOScheduler()
    restored = sched.restore_alerts(scheduler, AsyncMock())

    assert restored == 2
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {f"alert_{future_id}", f"alert_{late_snooze}"}
    remaining = {a["id"] for a in get_scheduled_alerts()}
    assert stale_timer not in remaining and stale_snooze not in remaining


def test_fire_timer_alert_sends_message_and_deletes_row():
    alert_id = add_scheduled_alert("timer", _iso(datetime.now(timezone.utc)), "Rest", 90, 42)
    bot = AsyncMock()
    asyncio.run(sched.fire_alert(bot, alert_id))

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 42
    assert "90s" in bot.send_message.await_args.kwargs["text"]
    assert get_scheduled_alerts() == []


def test_snooze_resends_the_workout_while_pending():
    alert_id = add_scheduled_alert("snooze", _iso(datetime.now(timezone.utc)), "Snooze", 3600, config.USER_ID)
    bot = AsyncMock()
    asyncio.run(sched.fire_alert(bot, alert_id))

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "Snoozed reminder" in text
    assert "Push-ups" in text  # the real workout, not a generic "time's up"


def test_snooze_is_sent_even_when_scheduled_notifications_are_off():
    set_setting("notifications_enabled", "0")
    bot = AsyncMock()
    asyncio.run(sched.send_daily_workout_notification(bot, snoozed=True))
    bot.send_message.assert_awaited_once()


def test_snooze_skipped_when_workout_already_done():
    today = get_current_date_str()
    workout = get_or_create_daily_workout(today)
    if workout["status"] == "rest":
        return  # nothing to complete on a rest day
    complete_all_exercises_for_workout(workout["id"])

    bot = AsyncMock()
    asyncio.run(sched.send_daily_workout_notification(bot, snoozed=True))
    bot.send_message.assert_not_awaited()


# --------------------------------------------------------------------------
# Local timestamps (Early Bird badge)
# --------------------------------------------------------------------------

def test_local_now_iso_uses_user_timezone_without_offset():
    # A zone far from any server's clock makes a wrong-clock bug obvious.
    set_setting("timezone", "Pacific/Kiritimati")  # UTC+14
    stamp = local_now_iso()
    assert "+" not in stamp and not stamp.endswith("Z")

    expected = datetime.now(ZoneInfo("Pacific/Kiritimati")).replace(tzinfo=None)
    assert abs(datetime.fromisoformat(stamp) - expected) < timedelta(minutes=1)


# --------------------------------------------------------------------------
# Label sanitizing / callback data size
# --------------------------------------------------------------------------

def test_sanitize_label_removes_markdown_and_separator_chars():
    assert sanitize_label("Pull_ups *heavy* [x]: `max`") == "Pull ups heavy x max"
    assert sanitize_label("   ") == ""
    assert len(sanitize_label("A" * 100)) == 40


def test_add_exercise_sanitizes_name_and_unit():
    add_exercise("Dips_weighted*", 12, "re_ps")
    names = {ex["name"]: ex["unit"] for ex in get_exercises()}
    assert names["Dips weighted"] == "re ps"


def test_add_exercise_rejects_empty_name():
    try:
        add_exercise("**", 10)
    except ValueError:
        return
    raise AssertionError("expected ValueError for an empty name")


def test_timer_callback_data_fits_telegram_limit():
    data = timer_callback_data(120, "Планка с поворотами и подъёмом ног очень долго")
    assert data.startswith("start_timer:120:")
    assert len(data.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES
    assert data.count(":") == 2


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------

def test_prune_backups_keeps_newest(tmp_path):
    for i in range(15):
        (tmp_path / f"workout_backup_20260101_0000{i:02d}.db").write_bytes(b"x")
    removed = prune_backups(tmp_path, keep=10)
    assert len(removed) == 5
    left = sorted(p.name for p in tmp_path.glob("workout_backup_*.db"))
    assert left[0] == "workout_backup_20260101_000005.db"
    assert len(left) == 10


def test_create_backup_prunes_old_files():
    for i in range(12):
        (config.BACKUP_DIR / f"workout_backup_20200101_0000{i:02d}.db").write_bytes(b"x")
    create_backup()
    assert len(list(config.BACKUP_DIR.glob("workout_backup_*.db"))) == 10


# --------------------------------------------------------------------------
# /status
# --------------------------------------------------------------------------

def test_format_duration():
    assert format_duration(45) == "45s"
    assert format_duration(3 * 3600 + 14 * 60) == "3h 14m"
    assert format_duration(2 * 86400 + 60) == "2d 0h 1m"


def test_status_text_reports_health():
    now = datetime.now(timezone.utc)

    async def run():
        scheduler = AsyncIOScheduler()
        sched.reschedule_daily_job(scheduler, AsyncMock())
        scheduler.start()
        try:
            return build_status_text({
                "started_at": now - timedelta(hours=2),
                "version": "abc1234",
                "callbacks_received": 3,
                "last_callback_at": now - timedelta(seconds=30),
                "scheduler": scheduler,
            }, now=now)
        finally:
            scheduler.shutdown(wait=False)

    text = asyncio.run(run())
    assert "2h 0m" in text
    assert "abc1234" in text
    assert "Button clicks received: *3*" in text
    assert "Morning reminder" in text and "not scheduled" not in text


def test_status_text_without_scheduler_warns():
    text = build_status_text({})
    assert "Scheduler is not running" in text
    assert "Button clicks received: *0*" in text


# --------------------------------------------------------------------------
# Duplicate instance detection
# --------------------------------------------------------------------------

def test_conflict_error_warns_user_once():
    bot = AsyncMock()
    context = SimpleNamespace(error=Conflict("terminated by other getUpdates request"),
                              bot=bot, bot_data={})

    asyncio.run(app_main.global_error_handler(None, context))
    asyncio.run(app_main.global_error_handler(None, context))

    bot.send_message.assert_awaited_once()
    assert "Another copy" in bot.send_message.await_args.args[1]
