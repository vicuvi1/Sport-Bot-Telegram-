"""Accountability partner: one friend who receives your progress updates.

The partner joins through a one-time invite link (t.me/<bot>?start=partner_<code>).
They only ever receive what the owner chose to share (weekly summary, monthly
fitness test, optional "missed 3 in a row" alert) and can send high-fives
back. Every other message from them is ignored, like any other stranger.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from database import get_connection, get_setting, set_setting
from services.workout_service import (
    _to_date,
    get_current_date_str,
    get_paused_dates,
    is_day_active,
    sanitize_label,
)

logger = logging.getLogger(__name__)

INVITE_PREFIX = "partner_"
INVITE_VALID_HOURS = 48
MISSED_ALERT_AFTER = 3  # missed planned workouts in a row

# What the partner receives; (setting key, default, label)
SHARE_OPTIONS = {
    "weekly": ("partner_share_weekly", "1", "Weekly summary (Sundays)"),
    "tests": ("partner_share_tests", "1", "Monthly fitness test results"),
    "missed": ("partner_alert_missed", "0", f"Alert after {MISSED_ALERT_AFTER} missed workouts in a row"),
}


def owner_name(db_path: Optional[Path] = None) -> str:
    return get_setting("owner_name", "", db_path=db_path) or "Your friend"


def remember_owner_name(first_name: Optional[str], db_path: Optional[Path] = None) -> None:
    name = sanitize_label(first_name or "", max_length=30)
    if name:
        set_setting("owner_name", name, db_path=db_path)


def get_partner(db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    chat_id = get_setting("partner_chat_id", "", db_path=db_path)
    if not chat_id:
        return None
    partner = {
        "chat_id": int(chat_id),
        "name": get_setting("partner_name", "", db_path=db_path) or "Your partner",
    }
    for option, (key, default, _) in SHARE_OPTIONS.items():
        partner[option] = get_setting(key, default, db_path=db_path) == "1"
    return partner


def is_partner(user_id: Optional[int], db_path: Optional[Path] = None) -> bool:
    partner = get_partner(db_path)
    return bool(partner and user_id and partner["chat_id"] == user_id)


def shares(option: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The partner if they should receive `option`, else None."""
    partner = get_partner(db_path)
    return partner if partner and partner.get(option) else None


def toggle_share(option: str, db_path: Optional[Path] = None) -> bool:
    key, default, _ = SHARE_OPTIONS[option]
    new_value = "0" if get_setting(key, default, db_path=db_path) == "1" else "1"
    set_setting(key, new_value, db_path=db_path)
    return new_value == "1"


def create_invite(now: Optional[datetime] = None, db_path: Optional[Path] = None) -> str:
    """Creates a fresh single-use invite code (replacing any older one)."""
    now = now or datetime.now(timezone.utc)
    code = secrets.token_urlsafe(9)  # 12 URL-safe chars, fits Telegram's 64-char start payload
    set_setting("partner_invite_code", code, db_path=db_path)
    set_setting("partner_invite_expires", (now + timedelta(hours=INVITE_VALID_HOURS)).isoformat(), db_path=db_path)
    return code


def pending_invite_expiry(now: Optional[datetime] = None, db_path: Optional[Path] = None) -> Optional[datetime]:
    now = now or datetime.now(timezone.utc)
    if not get_setting("partner_invite_code", "", db_path=db_path):
        return None
    try:
        expires = datetime.fromisoformat(get_setting("partner_invite_expires", "", db_path=db_path))
    except ValueError:
        return None
    return expires if expires > now else None


def accept_invite(code: str, chat_id: int, name: Optional[str], owner_id: int,
                  now: Optional[datetime] = None, db_path: Optional[Path] = None) -> str:
    """Tries to register `chat_id` as partner. Returns 'ok', 'own', 'invalid' or 'expired'."""
    if chat_id == owner_id:
        return "own"
    stored = get_setting("partner_invite_code", "", db_path=db_path)
    if not stored or not secrets.compare_digest(stored, code):
        return "invalid"
    if pending_invite_expiry(now, db_path) is None:
        return "expired"

    set_setting("partner_chat_id", str(chat_id), db_path=db_path)
    set_setting("partner_name", sanitize_label(name or "", max_length=30) or "Your partner", db_path=db_path)
    # Single use: a forwarded link can't add anyone else.
    set_setting("partner_invite_code", "", db_path=db_path)
    set_setting("partner_invite_expires", "", db_path=db_path)
    return "ok"


def remove_partner(db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Removes the partner. Returns who it was (to say goodbye), or None."""
    partner = get_partner(db_path)
    set_setting("partner_chat_id", "", db_path=db_path)
    set_setting("partner_name", "", db_path=db_path)
    set_setting("partner_last_cheer", "", db_path=db_path)
    set_setting("partner_last_missed_alert", "", db_path=db_path)
    return partner


def can_cheer_today(today_str: Optional[str] = None, db_path: Optional[Path] = None) -> bool:
    """One high-five per day, so a tap-happy partner can't spam."""
    today_str = today_str or get_current_date_str(db_path=db_path)
    if get_setting("partner_last_cheer", "", db_path=db_path) == today_str:
        return False
    set_setting("partner_last_cheer", today_str, db_path=db_path)
    return True


CHEER_CALLBACK = "partner_cheer"


async def send_to_partner(bot, text: str, with_cheer: bool = True,
                          db_path: Optional[Path] = None) -> bool:
    """Sends a Markdown message to the partner, optionally with a high-five button.

    Failures (e.g. the partner blocked the bot) are logged as warnings: they
    are not bot errors and must not trigger error reports.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    partner = get_partner(db_path)
    if not partner:
        return False
    markup = None
    if with_cheer:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            f"👏 Send {owner_name(db_path)} a High-Five", callback_data=CHEER_CALLBACK)]])
    try:
        await bot.send_message(chat_id=partner["chat_id"], text=text,
                               parse_mode="Markdown", reply_markup=markup)
        return True
    except Exception as e:
        logger.warning("Could not message accountability partner: %s", e)
        return False


def count_missed_in_a_row(today_str: Optional[str] = None, db_path: Optional[Path] = None) -> int:
    """Planned workouts missed in a row, counting back from yesterday.

    Rest days and paused days are skipped over (they neither count nor break
    the run); a completed workout ends it.
    """
    today_str = today_str or get_current_date_str(db_path=db_path)
    today = _to_date(today_str)
    workout_days = get_setting("workout_days", "0,1,2,3,4,5,6", db_path=db_path)
    paused = get_paused_dates(db_path=db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT date, status FROM daily_workouts WHERE date < ? AND date >= ?;",
            (today_str, (today - timedelta(days=30)).isoformat())
        ).fetchall()
        first = conn.execute("SELECT MIN(date) AS d FROM daily_workouts;").fetchone()["d"]
    status_by_date = {r["date"]: r["status"] for r in rows}

    missed = 0
    for i in range(1, 31):
        day = today - timedelta(days=i)
        day_str = day.isoformat()
        if first is None or day_str < first:
            break  # before the bot was used
        status = status_by_date.get(day_str)
        if status == "completed":
            break
        if status in ("rest", "paused") or day_str in paused or not is_day_active(day.weekday(), workout_days):
            continue
        missed += 1
    return missed


def should_alert_missed(today_str: Optional[str] = None, db_path: Optional[Path] = None) -> bool:
    """True once per missed run, the day it reaches MISSED_ALERT_AFTER."""
    if not shares("missed", db_path):
        return False
    today_str = today_str or get_current_date_str(db_path=db_path)
    if count_missed_in_a_row(today_str, db_path) != MISSED_ALERT_AFTER:
        return False
    if get_setting("partner_last_missed_alert", "", db_path=db_path) == today_str:
        return False
    set_setting("partner_last_missed_alert", today_str, db_path=db_path)
    return True
