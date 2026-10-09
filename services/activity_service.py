"""Crew activity log: the Duel's live "fight log" and the data behind ghost pace.

Every visible thing the crew does lands here: reps logged, workouts finished,
wake-ups, roasts, hypes, challenges, ranks. Rapid taps on the same exercise
are merged into one entry ("+25 push-ups") so the log stays readable.
Everyone in the crew sees the log; it holds no private data (no weights,
no photos).
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from database import current_user_id, get_connection, get_setting
from zoneinfo import ZoneInfo

import config

# Taps on the same exercise within this window are merged into one entry.
MERGE_MINUTES = 10
REACTIONS = ["🔥", "💀", "😂", "🐔", "👑"]


def _now_local() -> datetime:
    try:
        tz = ZoneInfo(get_setting("timezone", config.TIMEZONE))
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).replace(tzinfo=None)


def record(kind: str, text: str, data: Optional[Dict[str, Any]] = None, user_id: Optional[int] = None) -> int:
    """Adds an entry. `text` is plain text (escaped when shown)."""
    uid = current_user_id() if user_id is None else user_id
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO activity (user_id, kind, text, data, created_at, local_time) VALUES (?, ?, ?, ?, ?, ?);",
            (uid, kind, text[:200], json.dumps(data or {}),
             datetime.now(timezone.utc).isoformat(), _now_local().isoformat(timespec="seconds")))
        conn.commit()
        return cur.lastrowid


def record_progress(exercise: str, unit: str, delta: int, date_str: str) -> None:
    """Reps logged on an exercise; merges with the user's last entry for it if recent."""
    if delta == 0:
        return
    uid = current_user_id()
    now = _now_local()
    with get_connection() as conn:
        last = conn.execute(
            "SELECT id, data, local_time FROM activity WHERE user_id = ? AND kind = 'progress' ORDER BY id DESC LIMIT 1;",
            (uid,)).fetchone()
        if last:
            data = json.loads(last["data"])
            then = datetime.fromisoformat(last["local_time"])
            if (data.get("exercise") == exercise and data.get("date") == date_str
                    and now - then < timedelta(minutes=MERGE_MINUTES)):
                data["delta"] += delta
                conn.execute("UPDATE activity SET data = ?, text = ?, local_time = ? WHERE id = ?;",
                             (json.dumps(data), _progress_text(exercise, unit, data["delta"]),
                              now.isoformat(timespec="seconds"), last["id"]))
                conn.commit()
                return
    record("progress", _progress_text(exercise, unit, delta),
           {"exercise": exercise, "unit": unit, "delta": delta, "date": date_str})


def _progress_text(exercise: str, unit: str, delta: int) -> str:
    sign = "+" if delta > 0 else "−"
    unit_text = "s" if unit == "sec" else ""
    return f"{sign}{abs(delta)}{unit_text} {exercise}"


def recent(limit: int = 30) -> List[Dict[str, Any]]:
    """Latest entries for the whole crew, newest first, with reactions."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?;", (limit,)).fetchall()
        ids = [r["id"] for r in rows]
        reactions: Dict[int, List[Dict[str, Any]]] = {i: [] for i in ids}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in conn.execute(f"SELECT * FROM activity_reactions WHERE activity_id IN ({marks});", ids).fetchall():
                reactions[r["activity_id"]].append({"user_id": r["user_id"], "emoji": r["emoji"]})
    out = []
    for r in rows:
        out.append({"id": r["id"], "user_id": r["user_id"], "kind": r["kind"], "text": r["text"],
                    "created_at": r["created_at"], "reactions": reactions.get(r["id"], [])})
    return out


def react(activity_id: int, emoji: str) -> bool:
    """Toggles the current user's reaction on an entry. Returns False if invalid."""
    if emoji not in REACTIONS:
        return False
    uid = current_user_id()
    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM activity WHERE id = ?;", (activity_id,)).fetchone():
            return False
        existing = conn.execute("SELECT emoji FROM activity_reactions WHERE activity_id = ? AND user_id = ?;",
                                (activity_id, uid)).fetchone()
        if existing and existing["emoji"] == emoji:
            conn.execute("DELETE FROM activity_reactions WHERE activity_id = ? AND user_id = ?;", (activity_id, uid))
        else:
            conn.execute("INSERT INTO activity_reactions (activity_id, user_id, emoji) VALUES (?, ?, ?) "
                         "ON CONFLICT(activity_id, user_id) DO UPDATE SET emoji = excluded.emoji;",
                         (activity_id, uid, emoji))
        conn.commit()
    return True


def reps_by_time(user_id: int, date_str: str, until: str) -> int:
    """Reps (not seconds) `user_id` had logged on `date_str` up to local time `until` (HH:MM)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT data FROM activity WHERE user_id = ? AND kind = 'progress' AND substr(local_time, 1, 10) = ? "
            "AND substr(local_time, 12, 5) <= ?;", (user_id, date_str, until)).fetchall()
    total = 0
    for r in rows:
        data = json.loads(r["data"])
        if data.get("unit") != "sec":
            total += data.get("delta", 0)
    return max(0, total)


def ghost_pace(rival_id: int, today_str: str) -> Optional[Dict[str, Any]]:
    """What the rival had done yesterday by this time (None without data)."""
    yesterday = (datetime.strptime(today_str, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    now_hhmm = _now_local().strftime("%H:%M")
    them = reps_by_time(rival_id, yesterday, now_hhmm)
    if them == 0:
        return None
    return {"time": now_hhmm, "rival_yesterday": them, "me_today": reps_by_time(current_user_id(), today_str, now_hhmm)}
