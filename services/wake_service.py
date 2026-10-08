"""Wake-up challenge: prove you got up on time, and let the crew see it.

At the user's wake time the bot sends "tap the 7" with shuffled number
buttons (a half-asleep thumb taps the wrong one). Answering within
WAKE_GRACE_MINUTES is on time, later is late, and no answer after
WAKE_DEADLINE_MINUTES is missed. Results go to the crew feed.
"""

import logging
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import config
from database import current_user_id, get_connection, get_setting
from services import crew_service as cs
from services.workout_service import get_current_date_str, is_date_paused

logger = logging.getLogger(__name__)

WAKE_GRACE_MINUTES = 10
WAKE_DEADLINE_MINUTES = 60
WAKE_PRESETS = ["05:30", "06:00", "06:30", "07:00", "07:30", "08:00"]


def _now_local() -> datetime:
    try:
        tz = ZoneInfo(get_setting("timezone", config.TIMEZONE))
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).replace(tzinfo=None)


def wake_enabled() -> bool:
    return get_setting("wake_enabled", "0") == "1"


def wake_time() -> str:
    return get_setting("wake_time", "07:00")


def deadline_time(target: str) -> tuple:
    """(hour, minute) WAKE_DEADLINE_MINUTES after the target time."""
    h, m = (int(p) for p in target.split(":"))
    total = (h * 60 + m + WAKE_DEADLINE_MINUTES) % (24 * 60)
    return total // 60, total % 60


def get_log(date_str: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    uid = current_user_id() if user_id is None else user_id
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM wake_logs WHERE user_id = ? AND date = ?;", (uid, date_str)).fetchone()
        return dict(row) if row else None


def start_check(date_str: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Creates today's check (if not already there). Returns it, or None if skipped."""
    date_str = date_str or get_current_date_str()
    if is_date_paused(date_str):
        return None
    existing = get_log(date_str)
    if existing:
        return existing
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO wake_logs (user_id, date, target, challenge) VALUES (?, ?, ?, ?);",
            (current_user_id(), date_str, wake_time(), random.randint(1, 9))
        )
        conn.commit()
    return get_log(date_str)


def answer_choices(challenge: int) -> List[int]:
    """The right number plus three others, shuffled."""
    choices = random.sample([n for n in range(1, 10) if n != challenge], 3) + [challenge]
    random.shuffle(choices)
    return choices


def answer(date_str: str, choice: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Handles a tap. Returns {"result": "expired"|"answered"|"wrong"|"on_time"|"late", "log": ...}."""
    log = get_log(date_str)
    today = get_current_date_str()
    if not log or date_str != today:
        return {"result": "expired", "log": log}
    if log["status"] != "pending":
        return {"result": "answered", "log": log}
    if choice != log["challenge"]:
        return {"result": "wrong", "log": log}

    now = now or _now_local()
    h, m = (int(p) for p in log["target"].split(":"))
    target_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=h, minute=m)
    late = max(0, int((now - target_dt).total_seconds() // 60))
    status = "on_time" if late <= WAKE_GRACE_MINUTES else "late"
    with get_connection() as conn:
        conn.execute(
            "UPDATE wake_logs SET status = ?, answered_at = ?, minutes_late = ? WHERE user_id = ? AND date = ?;",
            (status, now.isoformat(timespec="minutes"), late, current_user_id(), date_str)
        )
        conn.commit()
    log = get_log(date_str)
    cs.record_event("wake", {"status": status, "time": now.strftime("%H:%M"),
                             "target": log["target"], "minutes_late": late})
    return {"result": status, "log": log}


def close_check(date_str: Optional[str] = None) -> bool:
    """After the deadline: an unanswered check becomes missed. True if it was."""
    date_str = date_str or get_current_date_str()
    log = get_log(date_str)
    if not log or log["status"] != "pending":
        return False
    with get_connection() as conn:
        conn.execute("UPDATE wake_logs SET status = 'missed' WHERE user_id = ? AND date = ?;",
                     (current_user_id(), date_str))
        conn.commit()
    cs.record_event("wake", {"status": "missed", "target": log["target"]})
    return True


def wake_streak(user_id: Optional[int] = None) -> int:
    """On-time wake-ups in a row (days without a check are skipped)."""
    uid = current_user_id() if user_id is None else user_id
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status FROM wake_logs WHERE user_id = ? AND status != 'pending' ORDER BY date DESC LIMIT 400;",
            (uid,)
        ).fetchall()
    streak = 0
    for r in rows:
        if r["status"] != "on_time":
            break
        streak += 1
    return streak


def recent_logs(days: int = 7, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    uid = current_user_id() if user_id is None else user_id
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM wake_logs WHERE user_id = ? ORDER BY date DESC LIMIT ?;",
                            (uid, days)).fetchall()
    return [dict(r) for r in rows]


STATUS_ICON = {"on_time": "✅", "late": "⏰", "missed": "😴", "pending": "⏳"}


def today_label(user_id: int) -> str:
    """Short wake-up status for the duel, e.g. '☀️ 06:31 ✅', or '' if not in use."""
    if cs.user_setting(user_id, "wake_enabled", "0") != "1":
        return ""
    log = get_log(get_current_date_str(), user_id)
    if not log:
        return ""
    if log["status"] in ("on_time", "late"):
        return f"☀️ {log['answered_at'][11:16]} {STATUS_ICON[log['status']]}"
    return f"☀️ {STATUS_ICON[log['status']]}"


# ---------------------------------------------------------------------------
# Messages (scheduled jobs and crew feed)
# ---------------------------------------------------------------------------

async def send_wake_check(bot) -> None:
    """Scheduled at the user's wake time."""
    from telegram import InlineKeyboardButton
    if not wake_enabled():
        return
    log = start_check()
    if not log or log["status"] != "pending":
        return
    buttons = [[InlineKeyboardButton(str(n), callback_data=f"wake:{log['date']}:{n}")
                for n in answer_choices(log["challenge"])]]
    await cs.send_html(
        bot, current_user_id(),
        f"☀️ <b>Wake-up check!</b> · {log['target']}\n\n"
        f"Tap the <b>{log['challenge']}</b> to prove you're up. "
        f"You have {WAKE_GRACE_MINUTES} minutes to be on time.",
        buttons)


async def close_wake_check(bot) -> None:
    """Scheduled WAKE_DEADLINE_MINUTES after the wake time."""
    if close_check():
        await cs.send_html(bot, current_user_id(),
                           "😴 You slept through your wake-up check. Your crew will know 😏")


async def _announce_wake(bot, event: Dict[str, Any]) -> None:
    from telegram import InlineKeyboardButton
    from views import esc

    data = event["data"]
    who = event["user_id"]
    name = esc(cs.display_name(who))
    status = data["status"]
    if status == "on_time":
        text = f"☀️ <b>{name}</b> is up at {data['time']} ✅ (target {data['target']})"
        buttons = [[InlineKeyboardButton(f"💪 Hype {cs.display_name(who)}", callback_data=f"crew_hype:{who}")]]
    elif status == "late":
        text = (f"🐢 <b>{name}</b> dragged out of bed at {data['time']}: "
                f"<b>{data['minutes_late']} min late</b> 😂 (target {data['target']})")
        buttons = [[InlineKeyboardButton(f"😈 Roast {cs.display_name(who)}", callback_data=f"crew_roast:{who}")]]
    else:
        text = f"😴 <b>{name}</b> slept through the {data['target']} wake-up check 😂"
        buttons = [[InlineKeyboardButton(f"😈 Roast {cs.display_name(who)}", callback_data=f"crew_roast:{who}")]]
    for member in cs.others(who):
        if cs.user_setting(member["user_id"], "crew_feed", "1") == "1":
            await cs.send_html(bot, member["user_id"], text, buttons)


cs.EVENT_HANDLERS["wake"] = _announce_wake
