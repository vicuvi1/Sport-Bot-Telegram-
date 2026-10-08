import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

import config
from database import (
    as_user,
    current_user_id,
    get_users,
    get_setting,
    create_backup,
    verify_backup,
    add_scheduled_alert,
    get_scheduled_alert,
    get_scheduled_alerts,
    delete_scheduled_alert,
)
from monitoring import health, ping_healthcheck
from services import compete_service, crew_service, fitness_test_service, partner_service, wake_service
from services.summary_service import build_weekly_summary
from views import HTML, build_evening_nudge, build_today_workout_view, esc, quick_toggle_button

HEARTBEAT_MINUTES = 5

from services.workout_service import (
    get_or_create_daily_workout,
    get_current_date_str,
    is_date_paused
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

    if not current_user_id():
        logger.warning("No USER_ID configured, cannot send daily workout notification.")
        return

    tz_str = get_setting("timezone", config.TIMEZONE)
    today_str = get_current_date_str(tz_name=tz_str)
    workout = get_or_create_daily_workout(date_str=today_str)

    if snoozed and workout["status"] != "pending":
        logger.info("Snoozed reminder skipped: today's workout is %s.", workout["status"])
        return

    if workout["status"] == "paused":
        logger.info("Morning workout notification skipped: workouts are paused.")
        return

    # Greeting above the normal Today screen.
    intro = ["⏰ <b>Snoozed reminder:</b> here's today's workout." if snoozed
             else "☀️ <b>Good morning!</b> Here's today's plan."]
    yesterday_str = (datetime.strptime(today_str, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    if is_date_paused(yesterday_str):
        intro.append("👋 <b>Welcome back!</b> Your streak was kept safe while you were away.")
    partner = partner_service.shares("missed")
    if (workout["status"] == "pending" and partner
            and partner_service.count_missed_in_a_row(today_str) == partner_service.MISSED_ALERT_AFTER - 1):
        intro.append(f"⚠️ You've missed {partner_service.MISSED_ALERT_AFTER - 1} workouts in a row. "
                     f"Miss today too and {esc(partner['name'])} gets a heads-up.")

    text, markup = build_today_workout_view(workout, today_str, intro="\n".join(intro))
    if workout["status"] == "pending":
        markup = InlineKeyboardMarkup(list(markup.inline_keyboard) + [[
            InlineKeyboardButton("💤 Snooze 1h", callback_data="snooze_reminder")]])

    try:
        await bot.send_message(chat_id=current_user_id(), text=text, parse_mode=HTML, reply_markup=markup)
        logger.info(f"Daily workout notification sent successfully for {today_str}.")
    except Exception as e:
        logger.error(f"Error sending daily workout notification: {e}")

async def send_evening_nudge_notification(bot: Bot) -> None:
    """Sends an evening streak-saver reminder if today's workout is still incomplete."""
    notifications_on = get_setting("notifications_enabled", "1")
    if notifications_on != "1" or not current_user_id():
        return

    tz_str = get_setting("timezone", config.TIMEZONE)
    today_str = get_current_date_str(tz_name=tz_str)
    workout = get_or_create_daily_workout(date_str=today_str)

    # Only nudge if workout is pending (not completed and not rest)
    if workout["status"] != "pending":
        return

    if not any(it["status"] == "pending" and it["completed_reps"] < it["target_reps"]
               for it in workout["items"]):
        return

    text, markup = build_evening_nudge(workout, today_str)
    try:
        await bot.send_message(
            chat_id=current_user_id(),
            text=text,
            parse_mode=HTML,
            reply_markup=markup
        )
        logger.info(f"Evening nudge sent for {today_str}.")
    except Exception as e:
        logger.error(f"Error sending evening nudge: {e}")

def check_backup(backup_file) -> str:
    """Restore-tests a fresh backup and returns a one-line verdict (plain text).

    A failure is logged at ERROR level, so it is also reported to the user.
    """
    check = verify_backup(backup_file)
    health.last_backup_ok = check["ok"]
    health.last_backup_message = check["message"]
    if check["ok"]:
        return f"✅ Verified: the backup opens and holds {check['message']}."
    logger.error("Backup verification failed: %s", check["message"])
    return "⚠️ Backup check FAILED. Don't rely on this file; see the error report."

async def send_weekly_backup_notification(bot: Bot) -> None:
    """Automated weekly Sunday database backup sent directly to Telegram."""
    if not config.USER_ID:
        return

    try:
        backup_file = create_backup()
        verdict = check_backup(backup_file)
        with open(backup_file, "rb") as f:
            await bot.send_document(
                chat_id=config.USER_ID,
                document=f,
                filename=backup_file.name,
                caption=f"💾 Automated Weekly Backup\n\n{verdict}",
            )
        logger.info("Weekly Sunday automated backup sent successfully.")
    except Exception as e:
        logger.error(f"Error sending weekly Sunday backup: {e}")

def build_health_line() -> str:
    """One-line bot health report for the weekly summary."""
    parts = []
    if health.started_at:
        days = (datetime.now(timezone.utc) - health.started_at).days
        parts.append(f"running {days}d without restart" if days else "restarted this week")
    errors = health.errors_since_summary
    parts.append("no errors this week" if errors == 0 else f"{errors} error(s) this week")
    if health.last_backup_ok is True:
        parts.append("last backup verified")
    elif health.last_backup_ok is False:
        parts.append("⚠️ last backup check failed")
    if config.HEALTHCHECK_URL:
        parts.append("outside alarm on" if health.last_heartbeat_ok is not False else "⚠️ outside alarm ping failing")
    else:
        parts.append("outside alarm not set up")
    return "🩺 Bot health: " + ", ".join(parts)

async def send_weekly_summary(bot: Bot) -> None:
    """Sunday evening summary: the week's progress (plus bot health for the owner)."""
    if not current_user_id() or get_setting("notifications_enabled", "1") != "1":
        return
    is_owner = current_user_id() == config.USER_ID
    # The accountability partner and the bot-health line belong to the owner.
    partner = partner_service.shares("weekly") if is_owner else None
    try:
        health_line = build_health_line() if is_owner else ""
        if partner:
            health_line = f"🤝 Shared with {esc(partner['name'])}.\n{health_line}"
        text = build_weekly_summary(health_line=health_line)
        await bot.send_message(chat_id=current_user_id(), text=text, parse_mode=HTML)
        if is_owner:
            health.errors_since_summary = 0
        logger.info("Weekly summary sent.")
    except Exception as e:
        logger.error(f"Error sending weekly summary: {e}")
        return

    if partner:
        # The partner's copy has no bot-health line (that's only for the owner).
        await partner_service.send_to_partner(
            bot, f"🤝 <b>Weekly update from {esc(partner_service.owner_name())}</b>\n\n{build_weekly_summary()}",
            parse_mode=HTML)

async def send_monthly_test_reminder(bot: Bot) -> None:
    """On the 1st (and again on the 4th if still not done): time for the fitness test."""
    if not current_user_id() or get_setting("notifications_enabled", "1") != "1":
        return
    today_str = get_current_date_str()
    month = fitness_test_service.current_month(today_str)
    if fitness_test_service.is_month_complete(month) or is_date_paused(today_str):
        return
    first_reminder = today_str.endswith("-01")
    text = (
        ("🧪 *It's Fitness Test day!*\n\n" if first_reminder else "🧪 *Your monthly fitness test is still waiting.*\n\n")
        + "About 10 minutes of max-effort tests. They show how much stronger you're getting, "
          "month after month. Warm up first, then tap Start."
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start Test", callback_data="test_start")]])
    try:
        await bot.send_message(chat_id=current_user_id(), text=text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        logger.error(f"Error sending fitness test reminder: {e}")

async def check_missed_workouts(bot: Bot) -> None:
    """Daily: tells the accountability partner (if opted in) after 3 missed workouts in a row."""
    today_str = get_current_date_str()
    if is_date_paused(today_str) or not partner_service.should_alert_missed(today_str):
        return
    owner = partner_service.owner_name()
    sent = await partner_service.send_to_partner(
        bot,
        f"👀 *{owner} has missed {partner_service.MISSED_ALERT_AFTER} planned workouts in a row.*\n\n"
        "A quick message or a high-five from you could get them back on track 💪")
    if sent:
        partner = partner_service.get_partner()
        try:
            await bot.send_message(
                chat_id=config.USER_ID,
                text=f"📣 {partner['name']} was told you missed {partner_service.MISSED_ALERT_AFTER} workouts "
                     "in a row. Let's get back to it today. Even ⏱ Short on Time counts!",
                reply_markup=InlineKeyboardMarkup([[quick_toggle_button(False)]]))
        except Exception as e:
            logger.error(f"Error sending missed-workouts notice: {e}")

async def send_heartbeat(bot: Bot) -> None:
    """Pings the external dead-man's switch (only when HEALTHCHECK_URL is set)."""
    if config.HEALTHCHECK_URL:
        await ping_healthcheck(config.HEALTHCHECK_URL, bot)

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
        # The alert's chat is the user who snoozed.
        with as_user(alert["chat_id"]):
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
    """Configures and starts APScheduler: global jobs plus one set per crew member."""
    # APScheduler's default misfire grace time is 1 second: if the event loop is
    # briefly busy at 07:00:00, the morning reminder would be skipped for the day.
    scheduler = AsyncIOScheduler(job_defaults={"misfire_grace_time": 300, "coalesce": True})
    schedule_global_jobs(scheduler, bot)
    for user in get_users():
        reschedule_user_jobs(scheduler, bot, user["user_id"])
    restore_alerts(scheduler, bot)
    scheduler.start()
    return scheduler


def user_job_id(name: str, user_id: int) -> str:
    """Per-user job ids, e.g. 'daily_morning_workout:123'."""
    return f"{name}:{user_id}"


async def run_as(user_id: int, job, *args) -> None:
    """Runs a scheduled job as `user_id` (their settings, data and chat)."""
    with as_user(user_id):
        await job(*args)


def _user_time_and_tz():
    time_str = get_setting("workout_time", config.WORKOUT_TIME or "07:00")
    tz_str = get_setting("timezone", config.TIMEZONE or "Europe/Chisinau")
    try:
        hour, minute = [int(p) for p in time_str.split(":")[:2]]
    except Exception:
        hour, minute = 7, 0
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz, tz_str = ZoneInfo("UTC"), "UTC"
    return hour, minute, tz, tz_str


# Extra per-user jobs registered by other modules (e.g. wake-up checks):
# callables (scheduler, bot, user_id) called from reschedule_user_jobs.
USER_JOB_HOOKS = []


def reschedule_user_jobs(scheduler: AsyncIOScheduler, bot: Bot, user_id: int) -> None:
    """(Re)creates one person's reminders from their own time and timezone."""
    with as_user(user_id):
        hour, minute, tz, tz_str = _user_time_and_tz()
    jobs = [
        ("daily_morning_workout", send_daily_workout_notification, CronTrigger(hour=hour, minute=minute, timezone=tz)),
        ("daily_evening_nudge", send_evening_nudge_notification, CronTrigger(hour=19, minute=0, timezone=tz)),
        ("weekly_summary", send_weekly_summary, CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=tz)),
        # Fitness test reminder on the 1st, repeated on the 4th if not done
        ("monthly_test_reminder", send_monthly_test_reminder,
         CronTrigger(day="1,4", hour=hour, minute=minute, timezone=tz)),
        # Crew: unfinished challenges are lost at 23:00 (their time)
        ("challenge_deadline", compete_service.close_challenges,
         CronTrigger(hour=compete_service.CHALLENGE_DEADLINE_HOUR, minute=0, timezone=tz)),
        # Crew: roast them at 21:00 if a crew mate trained today and they didn't
        ("crew_auto_roast", crew_service.send_auto_roast,
         CronTrigger(hour=crew_service.AUTO_ROAST_HOUR, minute=0, timezone=tz)),
    ]
    # Wake-up challenge: the check at their wake time, and its deadline.
    with as_user(user_id):
        wake_on, wake_at = wake_service.wake_enabled(), wake_service.wake_time()
    for name in ("wake_check", "wake_deadline"):
        if scheduler.get_job(user_job_id(name, user_id)):
            scheduler.remove_job(user_job_id(name, user_id))
    if wake_on:
        wh, wm = (int(p) for p in wake_at.split(":"))
        dh, dm = wake_service.deadline_time(wake_at)
        jobs += [
            ("wake_check", wake_service.send_wake_check, CronTrigger(hour=wh, minute=wm, timezone=tz)),
            ("wake_deadline", wake_service.close_wake_check, CronTrigger(hour=dh, minute=dm, timezone=tz)),
        ]

    for name, func, trigger in jobs:
        scheduler.add_job(run_as, trigger=trigger, id=user_job_id(name, user_id),
                          args=[user_id, func, bot], replace_existing=True)
    for hook in USER_JOB_HOOKS:
        hook(scheduler, bot, user_id)
    logger.info(f"Scheduled user {user_id}: morning {hour:02d}:{minute:02d}, nudge 19:00, "
                f"Sunday summary 20:00 ({tz_str}).")


def remove_user_jobs(scheduler: AsyncIOScheduler, user_id: int) -> None:
    for job in scheduler.get_jobs():
        if job.id.endswith(f":{user_id}"):
            scheduler.remove_job(job.id)


def reschedule_daily_job(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Reschedules the current user's jobs (after they change time or timezone)."""
    reschedule_user_jobs(scheduler, bot, current_user_id())


def schedule_global_jobs(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Bot-wide jobs, run as / delivered to the owner."""
    owner = config.USER_ID
    with as_user(owner):
        _, _, tz, _ = _user_time_and_tz()

    # Weekly Sunday backup of the whole database (owner only).
    scheduler.add_job(run_as, trigger=CronTrigger(day_of_week="sun", hour=23, minute=55, timezone=tz),
                      id="weekly_sunday_backup", args=[owner, send_weekly_backup_notification, bot],
                      replace_existing=True)
    # Owner's accountability partner: missed-workouts check at midday.
    scheduler.add_job(run_as, trigger=CronTrigger(hour=12, minute=0, timezone=tz),
                      id="partner_missed_check", args=[owner, check_missed_workouts, bot],
                      replace_existing=True)
    # Crew live feed: deliver anything not sent right away (e.g. after an error).
    scheduler.add_job(crew_service.tick, trigger=IntervalTrigger(minutes=1),
                      id="crew_events", args=[bot], replace_existing=True)
    # Crew competition: Sunday results + forfeit, Monday auto-pick if the winner didn't.
    scheduler.add_job(run_as, trigger=CronTrigger(day_of_week="sun", hour=20, minute=30, timezone=tz),
                      id="crew_weekly_results", args=[owner, compete_service.send_weekly_results, bot],
                      replace_existing=True)
    scheduler.add_job(run_as, trigger=CronTrigger(day_of_week="mon", hour=12, minute=0, timezone=tz),
                      id="crew_forfeit_autopick", args=[owner, compete_service.auto_pick_forfeits, bot],
                      replace_existing=True)
    # External heartbeat every 5 minutes (only with HEALTHCHECK_URL).
    if config.HEALTHCHECK_URL:
        scheduler.add_job(send_heartbeat, trigger=IntervalTrigger(minutes=HEARTBEAT_MINUTES),
                          id="heartbeat", args=[bot], next_run_time=datetime.now(timezone.utc),
                          replace_existing=True)
