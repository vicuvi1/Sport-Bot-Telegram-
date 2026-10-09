"""Admin tools for the owner: see and fix any crew member's workouts on any day.

Every change is written to the crew's fight log (and the member gets a Telegram
message), so corrections are always visible. Admin edits never trigger the
"just finished" feed, overtake alerts or the progress log: they fix the record,
they don't pretend someone trained.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config
from database import as_user, get_connection, get_users
from services import activity_service
from services import crew_service as cs
from services.workout_service import (
    _refresh_workout_status,
    get_current_date_str,
    get_exercise_by_id,
    get_exercises,
    get_or_create_daily_workout,
    local_now_iso,
    toggle_exercise_active,
    update_exercise,
)
from views import unit_label

MAX_REPS = 5000
REASON_CHARS = 80


class AdminError(Exception):
    """A refused admin action; the message is shown to the admin."""


def is_admin(user_id: int) -> bool:
    return user_id == config.USER_ID


def _member(user_id: int) -> int:
    if user_id not in [u["user_id"] for u in get_users()]:
        raise AdminError("That person isn't in the crew.")
    return user_id


def _member_today(user_id: int) -> str:
    with as_user(user_id):
        return get_current_date_str()


def _check_date(user_id: int, date_str: str) -> str:
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise AdminError("Pick a valid date.")
    if day.isoformat() > _member_today(user_id):
        raise AdminError("You can't fix days that haven't happened yet.")
    return day.isoformat()


def _nice(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b").replace(" 0", " ")


def _reason(reason: Optional[str]) -> str:
    text = " ".join(str(reason or "").split())[:REASON_CHARS]
    return f" ({text})" if text else ""


def day_view(user_id: int, date_str: str) -> Dict[str, Any]:
    """One member's day as stored (nothing is created just by looking)."""
    _member(user_id)
    date_str = _check_date(user_id, date_str)
    with get_connection() as conn:
        day = conn.execute("SELECT id, status, quick, completed_at FROM daily_workouts WHERE user_id = ? AND date = ?;",
                           (user_id, date_str)).fetchone()
        items = conn.execute("SELECT id, exercise_name, completed_reps, target_reps, unit, status FROM workout_items "
                             "WHERE daily_workout_id = ? ORDER BY id;", (day["id"],)).fetchall() if day else []
    return {
        "exists": bool(day), "status": day["status"] if day else None, "quick": bool(day["quick"]) if day else False,
        "items": [{"id": i["id"], "name": i["exercise_name"], "done": i["completed_reps"], "target": i["target_reps"],
                   "unit": unit_label(i["unit"]), "status": i["status"]} for i in items],
    }


def admin_view(user_id: int, date_str: Optional[str] = None) -> Dict[str, Any]:
    """Everything the admin screen shows for one member and day."""
    _member(user_id)
    date_str = date_str or _member_today(user_id)
    with as_user(user_id):
        exercises = get_exercises()
    return {
        "members": [{"id": u["user_id"], "name": cs.display_name(u["user_id"])} for u in get_users()],
        "user": user_id, "name": cs.display_name(user_id), "date": date_str, "today": _member_today(user_id),
        "day": day_view(user_id, date_str),
        "days": history_dates(user_id),
        "exercises": [{"id": e["id"], "name": e["name"], "target": e["target_reps"], "unit": unit_label(e["unit"]),
                       "active": bool(e["is_active"])} for e in exercises],
        "log": [a for a in activity_service.recent(60) if a["kind"] == "admin"][:15],
    }


def _log(admin: int, member: int, text: str) -> str:
    """Fight-log line, visible to the whole crew."""
    who = "their own" if admin == member else f"{cs.display_name(member)}'s"
    line = f"🛠 fixed {who} {text}"
    activity_service.record("admin", line, {"member": member}, user_id=admin)
    return line


async def _tell(bot, admin: int, member: int, text: str, reason: str) -> None:
    if bot is None or admin == member:
        return
    from views import esc
    await cs.send_html(bot, member, f"🛠 <b>{esc(cs.display_name(admin))}</b> (admin) changed {esc(text)}{esc(reason)}. "
                                    "It's in the fight log too.")


def _item_row(cursor, user_id: int, item_id: int):
    row = cursor.execute(
        "SELECT wi.*, dw.date, dw.id AS workout_id FROM workout_items wi JOIN daily_workouts dw ON dw.id = wi.daily_workout_id "
        "WHERE wi.id = ? AND dw.user_id = ?;", (item_id, user_id)).fetchone()
    if not row:
        raise AdminError("That exercise isn't part of their workout.")
    return row


def _item_status(reps: int, target: int, current: str, mark: Optional[str]) -> str:
    if mark == "skip":
        return "skipped"
    if mark != "unskip" and current == "skipped" and reps == 0:
        return "skipped"
    return "completed" if reps >= target else "pending"


async def set_item(bot, admin: int, user_id: int, item_id: int, reps: Optional[int] = None,
                   target: Optional[int] = None, mark: Optional[str] = None, reason: Optional[str] = None) -> str:
    """Changes one exercise of one day. mark: done | reset | skip | unskip."""
    _member(user_id)
    if mark not in (None, "done", "reset", "skip", "unskip"):
        raise AdminError("Unknown change.")
    with get_connection() as conn:
        cursor = conn.cursor()
        row = _item_row(cursor, user_id, item_id)
        old_reps, old_target = row["completed_reps"], row["target_reps"]
        new_target = old_target if target is None else max(1, min(MAX_REPS, int(target)))
        new_reps = old_reps if reps is None else max(0, min(MAX_REPS, int(reps)))
        if mark == "done":
            new_reps = max(new_reps, new_target)
        elif mark in ("reset", "skip"):
            new_reps = 0
        status = _item_status(new_reps, new_target, row["status"], mark)
        if (new_reps, new_target, status) == (old_reps, old_target, row["status"]):
            return "Nothing changed."
        cursor.execute("UPDATE workout_items SET completed_reps = ?, target_reps = ?, status = ? WHERE id = ?;",
                       (new_reps, new_target, status, item_id))
        with as_user(user_id):
            _refresh_workout_status(cursor, row["workout_id"], local_now_iso(), announce=False)
        conn.commit()
    unit = unit_label(row["unit"])
    parts = []
    if new_reps != old_reps:
        parts.append(f"{old_reps} → {new_reps}{'' if unit == 'reps' else unit}")
    if new_target != old_target:
        parts.append(f"target {old_target} → {new_target}")
    if status == "skipped" and row["status"] != "skipped":
        parts.append("skipped")
    elif row["status"] == "skipped" and status != "skipped":
        parts.append("unskipped")
    text = f"{row['exercise_name']} on {_nice(row['date'])}: {', '.join(parts)}"
    why = _reason(reason)
    _log(admin, user_id, text + why)
    await _tell(bot, admin, user_id, f"your {text}", why)
    return f"Saved: {text}"


async def set_day(bot, admin: int, user_id: int, date_str: str, op: str, reason: Optional[str] = None) -> str:
    """op: create (a day they forgot to open) | complete (all targets met) | reset (back to zero)."""
    _member(user_id)
    date_str = _check_date(user_id, date_str)
    why = _reason(reason)
    if op == "create":
        if day_view(user_id, date_str)["exists"]:
            return "That day already exists."
        with as_user(user_id):
            workout = get_or_create_daily_workout(date_str)
        if not workout["items"]:
            return f"No exercises were planned for {_nice(date_str)} (rest day)."
        _log(admin, user_id, f"workout day {_nice(date_str)}: opened it to log it{why}")
        return f"Opened {_nice(date_str)}. Now set the reps."
    if op not in ("complete", "reset"):
        raise AdminError("Unknown change.")
    with get_connection() as conn:
        cursor = conn.cursor()
        day = cursor.execute("SELECT id FROM daily_workouts WHERE user_id = ? AND date = ?;", (user_id, date_str)).fetchone()
        if not day:
            raise AdminError("No workout on that day yet. Open it first.")
        if op == "complete":
            cursor.execute("UPDATE workout_items SET completed_reps = MAX(completed_reps, target_reps), status = 'completed' "
                           "WHERE daily_workout_id = ? AND status != 'skipped';", (day["id"],))
        else:
            cursor.execute("UPDATE workout_items SET completed_reps = 0, status = 'pending' WHERE daily_workout_id = ?;",
                           (day["id"],))
        with as_user(user_id):
            _refresh_workout_status(cursor, day["id"], local_now_iso(), announce=False)
        conn.commit()
    text = f"workout on {_nice(date_str)}: {'marked complete' if op == 'complete' else 'reset to zero'}"
    _log(admin, user_id, text + why)
    await _tell(bot, admin, user_id, f"your {text}", why)
    return "Day marked complete." if op == "complete" else "Day reset."


async def set_exercise(bot, admin: int, user_id: int, exercise_id: int, target: Optional[int] = None,
                       toggle: bool = False, reason: Optional[str] = None) -> str:
    """Changes a member's exercise plan (applies from their next workout day)."""
    _member(user_id)
    with as_user(user_id):
        exercise = get_exercise_by_id(exercise_id)
        if not exercise:
            raise AdminError("That exercise isn't theirs.")
        if toggle:
            active = toggle_exercise_active(exercise_id)
            text = f"plan: {exercise['name']} {'on' if active else 'off'}"
        elif target is not None:
            new = max(1, min(MAX_REPS, int(target)))
            if new == exercise["target_reps"]:
                return "Nothing changed."
            update_exercise(exercise_id, target_reps=new)
            text = f"plan: {exercise['name']} target {exercise['target_reps']} → {new}"
        else:
            return "Nothing changed."
    why = _reason(reason)
    _log(admin, user_id, text + why)
    await _tell(bot, admin, user_id, f"your {text}", why)
    return "Plan updated. It applies from their next workout day."


def shift_date(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d").date() + timedelta(days=days)).isoformat()


def history_dates(user_id: int, days: int = 14) -> List[Dict[str, Any]]:
    """Recent days with their status, for the date strip (read-only)."""
    today = datetime.strptime(_member_today(user_id), "%Y-%m-%d").date()
    start = (today - timedelta(days=days - 1)).isoformat()
    with get_connection() as conn:
        rows = conn.execute("SELECT date, status FROM daily_workouts WHERE user_id = ? AND date >= ?;",
                            (user_id, start)).fetchall()
    found = {r["date"]: r["status"] for r in rows}
    out = []
    for n in range(days):
        d = (today - timedelta(days=days - 1 - n)).isoformat()
        out.append({"date": d, "status": found.get(d)})
    return out
