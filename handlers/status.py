import logging
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

import config
from database import get_setting, get_latest_backup, get_scheduled_alerts
from handlers.start import is_authorized
from monitoring import health
from services.workout_service import get_active_pause

logger = logging.getLogger(__name__)

# Human-readable names for the recurring scheduler jobs.
JOB_LABELS = {
    "daily_morning_workout": "Morning reminder",
    "daily_evening_nudge": "Evening nudge",
    "weekly_summary": "Weekly summary",
    "weekly_sunday_backup": "Weekly backup",
}


def get_version() -> str:
    """Returns the short git commit the bot is running from, or 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=config.BASE_DIR, capture_output=True, text=True, timeout=5
        )
        version = result.stdout.strip()
        return version if result.returncode == 0 and version else "unknown"
    except Exception:
        return "unknown"


def format_duration(seconds: float) -> str:
    """Formats a duration like '2d 3h 14m' (or '45s' when under a minute)."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _fmt_local(dt: Optional[datetime], tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%a %d %b %H:%M") if dt else "not scheduled"


def build_status_text(bot_data: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """Builds the /status health report from the running application's state."""
    now = now or datetime.now(timezone.utc)
    tz_name = get_setting("timezone", config.TIMEZONE)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    started_at = bot_data.get("started_at")
    uptime = format_duration((now - started_at).total_seconds()) if started_at else "unknown"

    lines = [
        "🩺 *Bot Status*",
        "",
        f"✅ Running for *{uptime}*",
        f"🏷 Version: `{bot_data.get('version', 'unknown')}`",
    ]

    # Button clicks: if this stays at 0 after tapping buttons, Telegram is not
    # delivering callback queries to this bot process.
    clicks = bot_data.get("callbacks_received", 0)
    last_click = bot_data.get("last_callback_at")
    if last_click:
        ago = format_duration((now - last_click).total_seconds())
        lines.append(f"👆 Button clicks received: *{clicks}* (last {ago} ago)")
    else:
        lines.append(f"👆 Button clicks received: *{clicks}*")

    lines.append(f"🚨 Errors since start: *{health.errors_total}*")

    if not config.HEALTHCHECK_URL:
        lines.append("📡 Outside alarm: not set up (`HEALTHCHECK_URL`)")
    elif health.last_heartbeat_ok is None:
        lines.append("📡 Outside alarm: waiting for first ping")
    else:
        ago = format_duration((now - health.last_heartbeat_at).total_seconds())
        state = "✅ OK" if health.last_heartbeat_ok else "⚠️ failing"
        lines.append(f"📡 Outside alarm: {state} (last ping {ago} ago)")

    pause = get_active_pause()
    if pause:
        lines.append(f"🟦 Paused until {pause['end_date']}")

    scheduler = bot_data.get("scheduler")
    lines += ["", "⏰ *Upcoming*"]
    if scheduler is None:
        lines.append("⚠️ Scheduler is not running")
    else:
        for job_id, label in JOB_LABELS.items():
            job = scheduler.get_job(job_id)
            # Pending (not yet started) jobs have no next_run_time attribute.
            next_run = getattr(job, "next_run_time", None) if job else None
            lines.append(f"• {label}: {_fmt_local(next_run, tz)}")

    pending = get_scheduled_alerts()
    if pending:
        lines.append(f"• Pending timers/snoozes: {len(pending)}")

    lines += ["", "💾 *Data*"]
    try:
        size_kb = config.DB_PATH.stat().st_size / 1024
        lines.append(f"• Database: {size_kb:.0f} KB")
    except OSError:
        lines.append("• Database: not found")

    latest = get_latest_backup()
    if latest:
        backup_time = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
        verified = {True: " ✅ verified", False: " ⚠️ check failed"}.get(health.last_backup_ok, "")
        lines.append(f"• Last backup: {_fmt_local(backup_time, tz)}{verified}")
    else:
        lines.append("• Last backup: none yet")

    # Code span: zone names like America/New_York contain "_" (Markdown italics).
    lines.append(f"• Timezone: `{tz_name}`")
    return "\n".join(lines)


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /status command: a quick health check of the running bot."""
    if not is_authorized(update):
        return
    await update.message.reply_text(build_status_text(context.bot_data), parse_mode="Markdown")
