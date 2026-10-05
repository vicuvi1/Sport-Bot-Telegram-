import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

import config
from database import (
    get_setting,
    create_backup,
    add_scheduled_alert,
    get_scheduled_alert,
    get_scheduled_alerts,
    delete_scheduled_alert,
)
from services.workout_service import (
    get_or_create_daily_workout,
    calculate_streaks,
    get_current_date_str,
    render_progress_bar
)

logger = logging.getLogger(__name__)

async def send_daily_workout_notification(bot: Bot, snoozed: bool = False) -> None:
    """Sends the morning workout notification to the authorized user.

    With ``snoozed=True`` this is the reminder the user asked for via
    "Snooze 1h": it is sent even if scheduled notifications are turned off,
    and skipped if the workout was finished (or is a rest day) in the meantime.
    """
    notifications_on = get_setting("notifications_enabled", "1")
    if notifications_on != "1" and not snoozed:
        logger.info("Morning workout notification skipped: notifications disabled.")
        return

    if not config.USER_ID:
        logger.warning("No USER_ID configured, cannot send daily workout notification.")
        return

    tz_str = get_setting("timezone", config.TIMEZONE)
    today_str = get_current_date_str(tz_name=tz_str)
    workout = get_or_create_daily_workout(date_str=today_str)
    current_streak, _ = calculate_streaks(today_str=today_str)

    if snoozed and workout["status"] != "pending":
        logger.info("Snoozed reminder skipped: today's workout is %s.", workout["status"])
        return

    if workout["status"] == "rest":
        text = (
            f"🏖 *Good morning! Today is a Rest Day.*\n\n"
            f"No workout scheduled for today. Enjoy your rest and recover!\n"
            f"🔥 Current streak preserved: *{current_streak} days*."
        )
        try:
            await bot.send_message(chat_id=config.USER_ID, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error sending rest day notification: {e}")
        return

    # Active workout with visual progress bars
    if snoozed:
        msg = f"⏰ *Snoozed reminder: time for Today's Workout* ({today_str})\n\n"
    else:
        msg = f"🏋️ *Good morning! Time for Today's Workout* ({today_str})\n\n"
    keyboard = []
    
    for item in workout["items"]:
        status_icon = "✅" if item["status"] == "completed" else ("⏭" if item["status"] == "skipped" else "⏳")
        bar = render_progress_bar(item["completed_reps"], item["target_reps"], length=8)
        msg += f"{status_icon} *{item['exercise_name']}* ({item['completed_reps']}/{item['target_reps']} {item['unit']})\n  `{bar}`\n"
        
        btn_text = f"{status_icon} {item['exercise_name']} ({item['completed_reps']}/{item['target_reps']})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"ex_view:{item['id']}")])

    msg += f"\n🔥 Streak: *{current_streak} days*\n"
    msg += "Tap an exercise below to log, or tap *Complete All*:"
    
    keyboard.insert(0, [InlineKeyboardButton("⚡ Complete All as Scheduled", callback_data="complete_all")])
    keyboard.append([
        InlineKeyboardButton("💤 Snooze 1h", callback_data="snooze_reminder"),
        InlineKeyboardButton("🔄 Refresh", callback_data="refresh_today")
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await bot.send_message(
            chat_id=config.USER_ID,
            text=msg,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        logger.info(f"Daily workout notification sent successfully for {today_str}.")
    except Exception as e:
        logger.error(f"Error sending daily workout notification: {e}")

async def send_evening_nudge_notification(bot: Bot) -> None:
    """Sends an evening streak-saver reminder if today's workout is still incomplete."""
    notifications_on = get_setting("notifications_enabled", "1")
    if notifications_on != "1" or not config.USER_ID:
        return

    tz_str = get_setting("timezone", config.TIMEZONE)
    today_str = get_current_date_str(tz_name=tz_str)
    workout = get_or_create_daily_workout(date_str=today_str)

    # Only nudge if workout is pending (not completed and not rest)
    if workout["status"] != "pending":
        return

    incomplete_items = [
        it for it in workout["items"]
        if it["status"] == "pending" and it["completed_reps"] < it["target_reps"]
    ]
    if not incomplete_items:
        return

    current_streak, _ = calculate_streaks(today_str=today_str)
    text = (
        f"⏰ *Evening Streak-Saver Nudge!* ({today_str})\n\n"
        f"Only a few hours left today! Don't let your *{current_streak}-day streak* break.\n\n"
        "Remaining exercises:\n"
    )
    for it in incomplete_items:
        text += f"• *{it['exercise_name']}*: {it['completed_reps']}/{it['target_reps']} {it['unit']}\n"

    text += "\nTap below to finish your workout strong! 💪"
    keyboard = [
        [InlineKeyboardButton("⚡ Complete All as Scheduled", callback_data="complete_all")],
        [InlineKeyboardButton("🏋️ Open Today's Workout", callback_data="refresh_today")]
    ]

    try:
        await bot.send_message(
            chat_id=config.USER_ID,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.info(f"Evening nudge sent for {today_str}.")
    except Exception as e:
        logger.error(f"Error sending evening nudge: {e}")

async def send_weekly_backup_notification(bot: Bot) -> None:
    """Automated weekly Sunday database backup sent directly to Telegram."""
    if not config.USER_ID:
        return

    try:
        backup_file = create_backup()
        with open(backup_file, "rb") as f:
            await bot.send_document(
                chat_id=config.USER_ID,
                document=f,
                filename=backup_file.name,
                caption="💾 *Automated Weekly Sunday Backup*\n\nYour weekly SQLite database backup is archived here safely.",
                parse_mode="Markdown"
            )
        logger.info("Weekly Sunday automated backup sent successfully.")
    except Exception as e:
        logger.error(f"Error sending weekly Sunday backup: {e}")

# ---------------------------------------------------------------------------
# One-off alerts: rest timers and snoozed reminders
# ---------------------------------------------------------------------------
# Alerts are stored in the database and scheduled as APScheduler date jobs, so
# a bot restart (deploy, crash, reboot) no longer silently drops them.
ALERT_TIMER = "timer"
ALERT_SNOOZE = "snooze"

# How late an alert may still be delivered after a restart. A rest timer that
# is minutes late is useless; a snoozed workout reminder is still worth sending.
ALERT_STALE_AFTER = {
    ALERT_TIMER: timedelta(minutes=2),
    ALERT_SNOOZE: timedelta(hours=3),
}

def _alert_job_id(alert_id: int) -> str:
    return f"alert_{alert_id}"

async def fire_alert(bot: Bot, alert_id: int) -> None:
    """Delivers a stored alert and removes it from the database."""
    alert = get_scheduled_alert(alert_id)
    if not alert:
        return
    delete_scheduled_alert(alert_id)

    if alert["kind"] == ALERT_SNOOZE:
        await send_daily_workout_notification(bot, snoozed=True)
        return

    try:
        await bot.send_message(
            chat_id=alert["chat_id"],
            text=(
                f"🔔 *Time's Up!* ({alert['label']})\n\n"
                f"Your *{alert['seconds']}s* interval is complete. Ready for the next set! 💪"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Timer alert failed: {e}")

def schedule_alert(scheduler: Optional[AsyncIOScheduler], bot: Bot, kind: str,
                   seconds: int, label: str, chat_id: int) -> int:
    """Stores an alert that fires `seconds` from now and schedules it."""
    fire_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    alert_id = add_scheduled_alert(kind, fire_at.isoformat(), label, seconds, chat_id)
    if scheduler is None:
        # Only happens outside the running bot (e.g. tests); the alert is
        # still stored and will be picked up by restore_alerts() on startup.
        logger.warning("No scheduler available; alert %s stored for later.", alert_id)
        return alert_id
    scheduler.add_job(
        fire_alert,
        trigger=DateTrigger(run_date=fire_at),
        id=_alert_job_id(alert_id),
        args=[bot, alert_id],
        replace_existing=True
    )
    logger.info("Scheduled %s alert %s (%ss, %s).", kind, alert_id, seconds, label)
    return alert_id

def restore_alerts(scheduler: AsyncIOScheduler, bot: Bot) -> int:
    """Re-schedules alerts stored before a restart. Returns how many were restored."""
    now = datetime.now(timezone.utc)
    restored = 0
    for alert in get_scheduled_alerts():
        try:
            fire_at = datetime.fromisoformat(alert["fire_at"])
        except ValueError:
            delete_scheduled_alert(alert["id"])
            continue
        stale_after = ALERT_STALE_AFTER.get(alert["kind"], timedelta(minutes=2))
        if now - fire_at > stale_after:
            logger.info("Dropping stale %s alert %s (was due %s).", alert["kind"], alert["id"], alert["fire_at"])
            delete_scheduled_alert(alert["id"])
            continue
        scheduler.add_job(
            fire_alert,
            trigger=DateTrigger(run_date=max(fire_at, now)),
            id=_alert_job_id(alert["id"]),
            args=[bot, alert["id"]],
            replace_existing=True
        )
        restored += 1
    if restored:
        logger.info("Restored %s pending alert(s) after restart.", restored)
    return restored

def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Configures and starts APScheduler for daily reminders, evening nudges, and weekly backups."""
    # APScheduler's default misfire grace time is 1 second: if the event loop is
    # briefly busy at 07:00:00, the morning reminder would be skipped for the day.
    scheduler = AsyncIOScheduler(job_defaults={"misfire_grace_time": 300, "coalesce": True})
    reschedule_daily_job(scheduler, bot)
    restore_alerts(scheduler, bot)
    scheduler.start()
    return scheduler

def reschedule_daily_job(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Reschedules all cron jobs based on the current settings."""
    time_str = get_setting("workout_time", config.WORKOUT_TIME or "07:00")
    tz_str = get_setting("timezone", config.TIMEZONE or "Europe/Chisinau")

    try:
        hour, minute = [int(p) for p in time_str.split(":")[:2]]
    except Exception:
        hour, minute = 7, 0

    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("UTC")

    # 1. Morning Workout Reminder
    morning_job_id = "daily_morning_workout"
    if scheduler.get_job(morning_job_id):
        scheduler.remove_job(morning_job_id)

    scheduler.add_job(
        send_daily_workout_notification,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
        id=morning_job_id,
        args=[bot],
        replace_existing=True
    )

    # 2. Evening Streak-Saver Nudge at 19:00
    nudge_job_id = "daily_evening_nudge"
    if scheduler.get_job(nudge_job_id):
        scheduler.remove_job(nudge_job_id)

    scheduler.add_job(
        send_evening_nudge_notification,
        trigger=CronTrigger(hour=19, minute=0, timezone=tz),
        id=nudge_job_id,
        args=[bot],
        replace_existing=True
    )

    # 3. Weekly Sunday Backup at 23:55
    backup_job_id = "weekly_sunday_backup"
    if scheduler.get_job(backup_job_id):
        scheduler.remove_job(backup_job_id)

    scheduler.add_job(
        send_weekly_backup_notification,
        trigger=CronTrigger(day_of_week="sun", hour=23, minute=55, timezone=tz),
        id=backup_job_id,
        args=[bot],
        replace_existing=True
    )

    logger.info(f"Scheduled morning reminder ({hour:02d}:{minute:02d}), evening nudge (19:00), and Sunday backup in {tz_str}.")
