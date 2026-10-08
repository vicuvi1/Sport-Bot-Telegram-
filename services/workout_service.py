from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, Tuple
import json
import math
from zoneinfo import ZoneInfo
from pathlib import Path

import config
from database import get_connection, get_setting, set_setting, current_user_id

def get_current_date_str(tz_name: Optional[str] = None, db_path: Optional[Path] = None) -> str:
    """Returns today's date string (YYYY-MM-DD) in the user's timezone."""
    tz_str = tz_name or get_setting("timezone", config.TIMEZONE, db_path=db_path)
    try:
        tz = ZoneInfo(tz_str)
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()
    return now.strftime("%Y-%m-%d")

def local_now_iso(db_path: Optional[Path] = None) -> str:
    """Returns the current wall-clock time in the user's timezone as a naive ISO string.

    Timestamps are stored without an offset on purpose: SQLite's time()/date()
    convert offset-suffixed values to UTC, which would break local-time checks
    such as the "Early Bird" (before 09:00) badge. Using the server clock
    (usually UTC) instead of the user's timezone had the same effect.
    """
    tz_str = get_setting("timezone", config.TIMEZONE, db_path=db_path)
    try:
        now = datetime.now(ZoneInfo(tz_str))
    except Exception:
        now = datetime.now()
    return now.replace(tzinfo=None).isoformat()

# ============================================================================
# VACATION / SICK PAUSES
# ============================================================================
# While paused: no reminders, the streak is frozen (paused days count like rest
# days), and the days show as paused in history instead of missed.

MAX_PAUSE_DAYS = 60

def _to_date(date_str: str) -> date:
    return datetime.strptime(date_str, "%Y-%m-%d").date()

def get_active_pause(today_str: Optional[str] = None, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Returns the pause covering today, or None."""
    today_str = today_str or get_current_date_str(db_path=db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM pauses WHERE user_id = ? AND start_date <= ? AND end_date >= ? "
            "ORDER BY end_date DESC LIMIT 1;",
            (current_user_id(), today_str, today_str)
        ).fetchone()
        return dict(row) if row else None

def get_paused_dates(db_path: Optional[Path] = None) -> set:
    """All dates (YYYY-MM-DD) covered by any pause, past or planned."""
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT start_date, end_date FROM pauses WHERE user_id = ?;",
                            (current_user_id(),)).fetchall()
    dates = set()
    for r in rows:
        d, end = _to_date(r["start_date"]), _to_date(r["end_date"])
        # Bounded loop: a corrupt row can't make this run forever.
        for _ in range(MAX_PAUSE_DAYS * 2):
            if d > end:
                break
            dates.add(d.isoformat())
            d += timedelta(days=1)
    return dates

def is_date_paused(date_str: str, db_path: Optional[Path] = None) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM pauses WHERE user_id = ? AND start_date <= ? AND end_date >= ? LIMIT 1;",
            (current_user_id(), date_str, date_str)
        ).fetchone()
        return row is not None

def start_pause(days: int, today_str: Optional[str] = None, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Pauses from today for `days` days (today included). Extends an active pause.

    Today's unfinished workout is marked paused so it no longer counts as missed.
    """
    days = max(1, min(int(days), MAX_PAUSE_DAYS))
    today_str = today_str or get_current_date_str(db_path=db_path)
    end_str = (_to_date(today_str) + timedelta(days=days - 1)).isoformat()
    active = get_active_pause(today_str, db_path=db_path)

    with get_connection(db_path) as conn:
        if active:
            conn.execute("UPDATE pauses SET end_date = ? WHERE id = ?;", (end_str, active["id"]))
            pause_id = active["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO pauses (user_id, start_date, end_date, created_at) VALUES (?, ?, ?, ?);",
                (current_user_id(), today_str, end_str, local_now_iso(db_path))
            )
            pause_id = cursor.lastrowid
        conn.execute(
            "UPDATE daily_workouts SET status = 'paused' WHERE user_id = ? AND date = ? AND status = 'pending';",
            (current_user_id(), today_str)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM pauses WHERE id = ?;", (pause_id,)).fetchone())

def resume_from_pause(today_str: Optional[str] = None, db_path: Optional[Path] = None) -> bool:
    """Ends the active pause now. Returns False if nothing was paused."""
    today_str = today_str or get_current_date_str(db_path=db_path)
    active = get_active_pause(today_str, db_path=db_path)
    if not active:
        return False

    yesterday_str = (_to_date(today_str) - timedelta(days=1)).isoformat()
    with get_connection(db_path) as conn:
        if active["start_date"] <= yesterday_str:
            conn.execute("UPDATE pauses SET end_date = ? WHERE id = ?;", (yesterday_str, active["id"]))
        else:
            conn.execute("DELETE FROM pauses WHERE id = ?;", (active["id"],))

        row = conn.execute(
            "SELECT id, (SELECT COUNT(*) FROM workout_items WHERE daily_workout_id = dw.id) AS n "
            "FROM daily_workouts dw WHERE user_id = ? AND date = ? AND status = 'paused';",
            (current_user_id(), today_str)
        ).fetchone()
        if row:
            if row["n"]:
                conn.execute("UPDATE daily_workouts SET status = 'pending' WHERE id = ?;", (row["id"],))
            else:
                # Created while paused (no exercises): drop it so it regenerates.
                conn.execute("DELETE FROM daily_workouts WHERE id = ?;", (row["id"],))
        conn.commit()
    return True

# Characters that break Telegram's legacy Markdown or the ":"-separated
# callback_data format when they appear inside user-entered labels.
_UNSAFE_LABEL_CHARS = "*_`[]:"
MAX_EXERCISE_NAME_LENGTH = 40

def sanitize_label(text: str, max_length: int = MAX_EXERCISE_NAME_LENGTH) -> str:
    """Cleans a user-entered exercise name or unit so it can be shown safely.

    An unmatched "_" or "*" in a Markdown message makes Telegram reject the
    whole message, which would break /today for every exercise.
    """
    for ch in _UNSAFE_LABEL_CHARS:
        text = text.replace(ch, " ")
    text = " ".join(text.split())
    return text[:max_length].strip()

# ============================================================================
# EXERCISE MANAGEMENT
# ============================================================================

def get_exercises(active_only: bool = False, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Retrieves all or only active exercises."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        if active_only:
            cursor.execute("SELECT * FROM exercises WHERE user_id = ? AND is_active = 1 ORDER BY id ASC;",
                           (current_user_id(),))
        else:
            cursor.execute("SELECT * FROM exercises WHERE user_id = ? ORDER BY id ASC;", (current_user_id(),))
        return [dict(r) for r in cursor.fetchall()]

def get_exercise_by_id(exercise_id: int, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Finds an exercise by its ID."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM exercises WHERE id = ? AND user_id = ?;", (exercise_id, current_user_id()))
        row = cursor.fetchone()
        return dict(row) if row else None

def add_exercise(
    name: str,
    target_reps: int,
    unit: str = "reps",
    days_of_week: str = "0,1,2,3,4,5,6",
    db_path: Optional[Path] = None
) -> int:
    """Adds a new exercise. Name and unit are sanitized for safe display."""
    name = sanitize_label(name)
    unit = sanitize_label(unit, max_length=12) or "reps"
    if not name:
        raise ValueError("exercise name is empty")
    now_iso = local_now_iso(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO exercises (user_id, name, target_reps, unit, is_active, days_of_week, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?);
            """,
            (current_user_id(), name, int(target_reps), unit, days_of_week.strip(), now_iso)
        )
        conn.commit()
        return cursor.lastrowid

def update_exercise(
    exercise_id: int,
    name: Optional[str] = None,
    target_reps: Optional[int] = None,
    unit: Optional[str] = None,
    is_active: Optional[int] = None,
    days_of_week: Optional[str] = None,
    db_path: Optional[Path] = None
) -> bool:
    """Updates fields of an existing exercise."""
    fields = []
    params = []
    if name is not None:
        fields.append("name = ?")
        params.append(name.strip())
    if target_reps is not None:
        fields.append("target_reps = ?")
        params.append(int(target_reps))
    if unit is not None:
        fields.append("unit = ?")
        params.append(unit.strip())
    if is_active is not None:
        fields.append("is_active = ?")
        params.append(int(is_active))
    if days_of_week is not None:
        fields.append("days_of_week = ?")
        params.append(days_of_week.strip())

    if not fields:
        return False

    params += [exercise_id, current_user_id()]
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE exercises SET {', '.join(fields)} WHERE id = ? AND user_id = ?;",
            params
        )
        conn.commit()
        return cursor.rowcount > 0

def toggle_exercise_active(exercise_id: int, db_path: Optional[Path] = None) -> Optional[bool]:
    """Toggles the active state of an exercise."""
    ex = get_exercise_by_id(exercise_id, db_path=db_path)
    if not ex:
        return None
    new_state = 0 if ex["is_active"] == 1 else 1
    update_exercise(exercise_id, is_active=new_state, db_path=db_path)
    return bool(new_state)

def delete_exercise(exercise_id: int, db_path: Optional[Path] = None) -> bool:
    """Deletes an exercise from the catalog."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM exercises WHERE id = ? AND user_id = ?;", (exercise_id, current_user_id()))
        conn.commit()
        return cursor.rowcount > 0

# ============================================================================
# DAILY WORKOUT CREATION & RETRIEVAL
# ============================================================================

def is_day_active(weekday: int, active_days_str: str) -> bool:
    """Checks if a weekday (0=Mon, ..., 6=Sun) is in the comma-separated days string."""
    try:
        active_days = [int(d.strip()) for d in active_days_str.split(",") if d.strip()]
        return weekday in active_days
    except Exception:
        return True

# ============================================================================
# COMEBACK RAMP
# ============================================================================
# After missing several planned workout days (vacation pauses included: you
# still lose fitness), the next workouts ease back in: 70% → 85% → 100%.

COMEBACK_AFTER_MISSED_DAYS = 4
COMEBACK_FACTORS = {1: 0.70, 2: 0.85}

def scale_target(target: int, factor: float) -> int:
    """Scales a target, rounding up, never below 1."""
    if factor >= 1.0:
        return target
    return max(1, math.ceil(target * factor))

def comeback_stage_for(cursor, workout_date: date, workout_days_setting: str) -> int:
    """Returns 1 or 2 if the workout on `workout_date` should be eased in, else 0."""
    row = cursor.execute(
        "SELECT date, comeback_stage FROM daily_workouts "
        "WHERE user_id = ? AND status = 'completed' AND date < ? ORDER BY date DESC LIMIT 1;",
        (current_user_id(), workout_date.isoformat())
    ).fetchone()
    if not row:
        return 0  # brand-new user: nothing to come back from

    last = _to_date(row["date"])
    gap = (workout_date - last).days - 1
    missed = sum(
        1 for i in range(1, min(gap, 30) + 1)
        if is_day_active((last + timedelta(days=i)).weekday(), workout_days_setting)
    )
    if missed >= COMEBACK_AFTER_MISSED_DAYS:
        return 1
    if row["comeback_stage"] == 1:
        return 2
    return 0

def get_or_create_daily_workout(
    date_str: Optional[str] = None,
    db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """
    Retrieves or generates the daily workout for a given date (defaults to today).
    Populates workout_items based on active exercises configured for this weekday.
    """
    if not date_str:
        date_str = get_current_date_str(db_path=db_path)

    now_iso = local_now_iso(db_path)
    workout_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    weekday = workout_date.weekday()  # 0 = Monday, 6 = Sunday
    uid = current_user_id()

    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Check existing workout
        cursor.execute("SELECT * FROM daily_workouts WHERE user_id = ? AND date = ?;", (uid, date_str))
        workout_row = cursor.fetchone()

        if workout_row:
            workout_id = workout_row["id"]
        else:
            # Check global workout_days setting
            workout_days_setting = get_setting("workout_days", "0,1,2,3,4,5,6", db_path=db_path)
            is_global_workout_day = is_day_active(weekday, workout_days_setting)

            # Find matching active exercises for this day
            cursor.execute("SELECT * FROM exercises WHERE user_id = ? AND is_active = 1 ORDER BY id ASC;", (uid,))
            all_active = [dict(r) for r in cursor.fetchall()]
            eligible_exercises = [
                ex for ex in all_active if is_day_active(weekday, ex.get("days_of_week", "0,1,2,3,4,5,6"))
            ]

            if is_date_paused(date_str, db_path=db_path):
                cursor.execute(
                    "INSERT INTO daily_workouts (user_id, date, status, created_at) VALUES (?, ?, 'paused', ?);",
                    (uid, date_str, now_iso)
                )
                conn.commit()
                workout_id = cursor.lastrowid
            elif not is_global_workout_day or not eligible_exercises:
                # Rest day
                cursor.execute(
                    "INSERT INTO daily_workouts (user_id, date, status, created_at) VALUES (?, ?, 'rest', ?);",
                    (uid, date_str, now_iso)
                )
                conn.commit()
                workout_id = cursor.lastrowid
            else:
                # Active workout day (eased in after a break, see comeback_stage_for)
                stage = comeback_stage_for(cursor, workout_date, workout_days_setting)
                factor = COMEBACK_FACTORS.get(stage, 1.0)
                cursor.execute(
                    "INSERT INTO daily_workouts (user_id, date, status, created_at, comeback_stage) "
                    "VALUES (?, ?, 'pending', ?, ?);",
                    (uid, date_str, now_iso, stage)
                )
                workout_id = cursor.lastrowid

                for ex in eligible_exercises:
                    cursor.execute(
                        """
                        INSERT INTO workout_items (
                            daily_workout_id, exercise_id, exercise_name, target_reps, completed_reps, unit, status, updated_at
                        ) VALUES (?, ?, ?, ?, 0, ?, 'pending', ?);
                        """,
                        (workout_id, ex["id"], ex["name"], scale_target(ex["target_reps"], factor), ex["unit"], now_iso)
                    )
                conn.commit()

        # Re-fetch full workout with items
        cursor.execute("SELECT * FROM daily_workouts WHERE id = ?;", (workout_id,))
        workout = dict(cursor.fetchone())

        cursor.execute("SELECT * FROM workout_items WHERE daily_workout_id = ? ORDER BY id ASC;", (workout_id,))
        items = [dict(r) for r in cursor.fetchall()]
        workout["items"] = items
        return workout

# ============================================================================
# EXERCISE COMPLETION & UPDATES
# ============================================================================

def _record_workout_done(cursor, workout_id: int) -> None:
    """Crew live feed: tell the others this day was just finished (see crew_service)."""
    cursor.execute(
        "INSERT INTO crew_events (user_id, kind, data, created_at) VALUES (?, 'workout_done', ?, ?);",
        (current_user_id(), json.dumps({"workout_id": workout_id}), datetime.now(ZoneInfo("UTC")).isoformat())
    )

def _refresh_workout_status(cursor, workout_id: int, now_iso: str) -> None:
    """Sets the day's status from its items: completed, skipped or pending."""
    cursor.execute("SELECT status FROM workout_items WHERE daily_workout_id = ?;", (workout_id,))
    statuses = [r["status"] for r in cursor.fetchall()]

    all_done = all(s in ("completed", "skipped") for s in statuses)
    has_completed = any(s == "completed" for s in statuses)

    if all_done and has_completed:
        cursor.execute(
            "UPDATE daily_workouts SET status = 'completed', completed_at = COALESCE(completed_at, ?) "
            "WHERE id = ? AND status != 'completed';",
            (now_iso, workout_id)
        )
        if cursor.rowcount:
            _record_workout_done(cursor, workout_id)
    elif all_done and not has_completed:
        cursor.execute(
            "UPDATE daily_workouts SET status = 'skipped', completed_at = ? WHERE id = ?;",
            (now_iso, workout_id)
        )
    else:
        cursor.execute(
            "UPDATE daily_workouts SET status = 'pending', completed_at = NULL WHERE id = ?;",
            (workout_id,)
        )

def update_workout_item(
    item_id: int,
    completed_reps: Optional[int] = None,
    delta_reps: Optional[int] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Updates completion progress or status for a single workout item.
    Also re-checks if the daily workout is completed, and tests progression.
    Returns: (updated_item_dict, progression_result_dict_or_None)
    """
    now_iso = local_now_iso(db_path)
    progression_info = None

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        # Only the current user's items (ids arrive in button data).
        cursor.execute(
            "SELECT wi.* FROM workout_items wi JOIN daily_workouts dw ON dw.id = wi.daily_workout_id "
            "WHERE wi.id = ? AND dw.user_id = ?;",
            (item_id, current_user_id())
        )
        item_row = cursor.fetchone()
        if not item_row:
            return None, None

        current_item = dict(item_row)
        target = current_item["target_reps"]
        new_reps = current_item["completed_reps"]

        if delta_reps is not None:
            new_reps = max(0, new_reps + delta_reps)
        elif completed_reps is not None:
            new_reps = max(0, completed_reps)

        new_status = status or current_item["status"]
        if status is None:
            if new_reps >= target:
                new_status = "completed"
            elif new_reps > 0 and current_item["status"] == "pending":
                new_status = "pending"

        cursor.execute(
            """
            UPDATE workout_items
            SET completed_reps = ?, status = ?, updated_at = ?
            WHERE id = ?;
            """,
            (new_reps, new_status, now_iso, item_id)
        )

        workout_id = current_item["daily_workout_id"]
        _refresh_workout_status(cursor, workout_id, now_iso)
        conn.commit()

        # Check progression for this exercise if completed
        if new_status == "completed" and current_item["exercise_id"]:
            progression_info = check_and_apply_progression(current_item["exercise_id"], db_path=db_path)

        # Retrieve updated item
        cursor.execute("SELECT * FROM workout_items WHERE id = ?;", (item_id,))
        updated_item = dict(cursor.fetchone())
        return updated_item, progression_info

# ============================================================================
# AUTOMATIC PROGRESSION LOGIC
# ============================================================================

def check_and_apply_progression(exercise_id: int, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """
    Checks if an exercise was successfully completed in N consecutive workouts.
    If so, increases target repetitions by configured percentage.
    """
    auto_prog_enabled = get_setting("auto_progression_enabled", "0", db_path=db_path)
    if auto_prog_enabled != "1":
        return None

    try:
        percentage = float(get_setting("progression_percentage", "5", db_path=db_path))
        consecutive_req = int(get_setting("progression_consecutive_workouts", "3", db_path=db_path))
    except (ValueError, TypeError):
        percentage = 5.0
        consecutive_req = 3

    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Get the exercise
        cursor.execute("SELECT * FROM exercises WHERE id = ? AND user_id = ?;", (exercise_id, current_user_id()))
        ex_row = cursor.fetchone()
        if not ex_row:
            return None
        current_target = ex_row["target_reps"]

        # Fetch last N workout items for this exercise ordered by date desc
        cursor.execute(
            """
            SELECT wi.id, wi.target_reps, wi.completed_reps, wi.status, dw.date
            FROM workout_items wi
            JOIN daily_workouts dw ON wi.daily_workout_id = dw.id
            WHERE wi.exercise_id = ?
            ORDER BY dw.date DESC, wi.id DESC
            LIMIT ?;
            """,
            (exercise_id, consecutive_req)
        )
        recent_items = [dict(r) for r in cursor.fetchall()]

        if len(recent_items) < consecutive_req:
            return None

        # Check if all recent N items were successfully completed at the full
        # target (quick / comeback days have reduced targets and don't count).
        for item in recent_items:
            if item["status"] != "completed" or item["completed_reps"] < item["target_reps"]:
                return None
            if item["target_reps"] < current_target:
                return None

        # Calculate new target
        increase = max(1, math.ceil(current_target * (percentage / 100.0)))
        new_target = current_target + increase

        cursor.execute(
            "UPDATE exercises SET target_reps = ? WHERE id = ?;",
            (new_target, exercise_id)
        )
        conn.commit()

        return {
            "exercise_id": exercise_id,
            "exercise_name": ex_row["name"],
            "unit": ex_row["unit"],
            "old_target": current_target,
            "new_target": new_target,
            "percentage": percentage,
            "consecutive_count": consecutive_req
        }

# ============================================================================
# STREAK CALCULATIONS
# ============================================================================

def calculate_streaks(
    today_str: Optional[str] = None,
    workout_days_setting: Optional[str] = None,
    db_path: Optional[Path] = None
) -> Tuple[int, int]:
    """
    Calculates (current_streak, best_streak) in days.
    Rest days and paused (vacation/sick) days DO NOT break streaks.
    """
    if not today_str:
        today_str = get_current_date_str(db_path=db_path)

    if workout_days_setting is None:
        workout_days_setting = get_setting("workout_days", "0,1,2,3,4,5,6", db_path=db_path)

    today_date = datetime.strptime(today_str, "%Y-%m-%d").date()

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT date, status FROM daily_workouts WHERE user_id = ? ORDER BY date ASC;",
                       (current_user_id(),))
        rows = cursor.fetchall()
        workouts_by_date = {r["date"]: r["status"] for r in rows}

    if not workouts_by_date:
        return 0, 0

    paused_dates = get_paused_dates(db_path=db_path)

    first_date_str = min(workouts_by_date.keys())
    first_date = datetime.strptime(first_date_str, "%Y-%m-%d").date()

    # Iterate through all dates from first recorded workout up to today
    current_running_streak = 0
    best_streak = 0
    d = first_date

    while d <= today_date:
        d_str = d.strftime("%Y-%m-%d")
        w_status = workouts_by_date.get(d_str)
        is_global_active = is_day_active(d.weekday(), workout_days_setting)

        if w_status == "completed":
            current_running_streak += 1
            if current_running_streak > best_streak:
                best_streak = current_running_streak
        elif w_status == "rest" or (w_status is None and not is_global_active):
            # Rest day: streak is preserved, neither incremented nor broken!
            pass
        elif w_status == "paused" or d_str in paused_dates:
            # Vacation / sick pause: frozen like a rest day.
            pass
        elif d == today_date:
            # Today is in progress! Do not break current streak yet.
            pass
        else:
            # Past workout day that was missed or skipped
            current_running_streak = 0

        d += timedelta(days=1)

    return current_running_streak, best_streak

def get_history(limit: int = 10, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Retrieves previous workouts with their exercise summary."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT dw.id, dw.date, dw.status, dw.created_at, dw.completed_at
            FROM daily_workouts dw
            WHERE dw.user_id = ?
            ORDER BY dw.date DESC
            LIMIT ?;
            """,
            (current_user_id(), limit)
        )
        workouts = [dict(r) for r in cursor.fetchall()]

        for w in workouts:
            cursor.execute(
                """
                SELECT exercise_name, target_reps, completed_reps, unit, status
                FROM workout_items
                WHERE daily_workout_id = ?
                ORDER BY id ASC;
                """,
                (w["id"],)
            )
            w["items"] = [dict(r) for r in cursor.fetchall()]

        return workouts

# ============================================================================
# VISUAL PROGRESS BARS & HEATMAP CALENDAR
# ============================================================================

def render_progress_bar(completed: int, target: int, length: int = 8) -> str:
    """Renders a clean Unicode progress bar, e.g. [████░░░░] 50%."""
    if target <= 0:
        return "[████████] 100% ✅"

    ratio = min(1.0, max(0.0, completed / target))
    filled_len = int(round(ratio * length))
    bar = "█" * filled_len + "░" * (length - filled_len)
    pct = int(round(ratio * 100))
    check = " ✅" if completed >= target else ""
    return f"[{bar}] {pct}%{check}"

def complete_all_exercises_for_workout(
    workout_id: int,
    db_path: Optional[Path] = None
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Marks all pending exercises in the workout completed to their target values.
    Returns (updated_workout, list_of_progression_alerts).
    """
    now_iso = local_now_iso(db_path)
    progressions = []

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        _check_workout_owner(cursor, workout_id)
        before = cursor.execute("SELECT status FROM daily_workouts WHERE id = ?;", (workout_id,)).fetchone()
        cursor.execute("SELECT * FROM workout_items WHERE daily_workout_id = ?;", (workout_id,))
        items = [dict(r) for r in cursor.fetchall()]

        for item in items:
            cursor.execute(
                """
                UPDATE workout_items
                SET completed_reps = target_reps, status = 'completed', updated_at = ?
                WHERE id = ?;
                """,
                (now_iso, item["id"])
            )

        cursor.execute(
            "UPDATE daily_workouts SET status = 'completed', completed_at = ? WHERE id = ?;",
            (now_iso, workout_id)
        )
        if before and before["status"] != "completed":
            _record_workout_done(cursor, workout_id)
        conn.commit()

        # Check progression for all exercises
        for item in items:
            if item["exercise_id"]:
                prog = check_and_apply_progression(item["exercise_id"], db_path=db_path)
                if prog:
                    progressions.append(prog)

        cursor.execute("SELECT * FROM daily_workouts WHERE id = ?;", (workout_id,))
        workout = dict(cursor.fetchone())
        cursor.execute("SELECT * FROM workout_items WHERE daily_workout_id = ? ORDER BY id ASC;", (workout_id,))
        workout["items"] = [dict(r) for r in cursor.fetchall()]

        return workout, progressions

# ============================================================================
# QUICK WORKOUT ("short on time")
# ============================================================================

QUICK_FACTOR = 0.5

def _check_workout_owner(cursor, workout_id: int) -> None:
    """Workout ids arrive via buttons; refuse to touch another user's day."""
    row = cursor.execute("SELECT user_id FROM daily_workouts WHERE id = ?;", (workout_id,)).fetchone()
    if row and row["user_id"] != current_user_id():
        raise PermissionError(f"workout {workout_id} belongs to another user")

def _load_workout(cursor, workout_id: int) -> Optional[Dict[str, Any]]:
    row = cursor.execute("SELECT * FROM daily_workouts WHERE id = ? AND user_id = ?;",
                         (workout_id, current_user_id())).fetchone()
    if not row:
        return None
    workout = dict(row)
    cursor.execute("SELECT * FROM workout_items WHERE daily_workout_id = ? ORDER BY id ASC;", (workout_id,))
    workout["items"] = [dict(r) for r in cursor.fetchall()]
    return workout

def start_quick_workout(workout_id: int, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Halves the targets of today's unfinished exercises. Still counts for the streak.

    Returns the updated workout, or None if it isn't a pending, full workout.
    """
    now_iso = local_now_iso(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        workout = _load_workout(cursor, workout_id)
        if not workout or workout["status"] != "pending" or workout["quick"]:
            return None
        for item in workout["items"]:
            if item["status"] != "pending":
                continue
            new_target = scale_target(item["target_reps"], QUICK_FACTOR)
            new_status = "completed" if item["completed_reps"] >= new_target else "pending"
            cursor.execute(
                "UPDATE workout_items SET target_reps = ?, full_target_reps = ?, status = ?, updated_at = ? WHERE id = ?;",
                (new_target, item["target_reps"], new_status, now_iso, item["id"])
            )
        cursor.execute("UPDATE daily_workouts SET quick = 1 WHERE id = ?;", (workout_id,))
        _refresh_workout_status(cursor, workout_id, now_iso)
        conn.commit()
        return _load_workout(cursor, workout_id)

def undo_quick_workout(workout_id: int, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Restores the full targets after a quick workout was started by mistake."""
    now_iso = local_now_iso(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        workout = _load_workout(cursor, workout_id)
        if not workout or not workout["quick"]:
            return None
        for item in workout["items"]:
            if item["full_target_reps"] is None:
                continue
            full = item["full_target_reps"]
            if item["status"] == "skipped":
                new_status = "skipped"
            else:
                new_status = "completed" if item["completed_reps"] >= full else "pending"
            cursor.execute(
                "UPDATE workout_items SET target_reps = ?, full_target_reps = NULL, status = ?, updated_at = ? WHERE id = ?;",
                (full, new_status, now_iso, item["id"])
            )
        cursor.execute("UPDATE daily_workouts SET quick = 0 WHERE id = ?;", (workout_id,))
        _refresh_workout_status(cursor, workout_id, now_iso)
        conn.commit()
        return _load_workout(cursor, workout_id)

# ============================================================================
# DIFFICULTY FEEDBACK
# ============================================================================
# After a full workout: "too easy" twice in a row raises the exercise targets,
# "too hard" lowers them right away. Quick workouts don't ask for feedback.

FEEDBACK_LABELS = {"easy": "😴 Too easy", "ok": "👌 Just right", "hard": "🥵 Too hard"}
FEEDBACK_ADJUST_PCT = 10
EASY_ANSWERS_TO_RAISE = 2

def _adjust_targets(cursor, exercise_ids: List[int], raise_targets: bool) -> List[Dict[str, Any]]:
    changes = []
    for ex_id in exercise_ids:
        ex = cursor.execute("SELECT id, name, unit, target_reps FROM exercises WHERE id = ? AND user_id = ?;",
                            (ex_id, current_user_id())).fetchone()
        if not ex:
            continue
        old = ex["target_reps"]
        step = max(1, round(old * FEEDBACK_ADJUST_PCT / 100))
        new = old + step if raise_targets else max(1, old - step)
        if new == old:
            continue
        cursor.execute("UPDATE exercises SET target_reps = ? WHERE id = ?;", (new, ex_id))
        changes.append({"name": ex["name"], "unit": ex["unit"], "old": old, "new": new})
    return changes

def record_feedback(workout_id: int, feedback: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Saves how a completed workout felt and adjusts exercise targets.

    Returns {"recorded": bool, "feedback", "changes": [...], "easy_streak": int}.
    """
    result = {"recorded": False, "feedback": feedback, "changes": [], "easy_streak": 0}
    if feedback not in FEEDBACK_LABELS:
        return result

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        workout = _load_workout(cursor, workout_id)
        if not workout or workout["status"] != "completed" or workout["feedback"] or workout["quick"]:
            return result

        cursor.execute("UPDATE daily_workouts SET feedback = ? WHERE id = ?;", (feedback, workout_id))
        row = cursor.execute("SELECT value FROM user_settings WHERE user_id = ? AND key = 'easy_feedback_streak';",
                             (current_user_id(),)).fetchone()
        try:
            easy_streak = int(row["value"]) if row else 0
        except ValueError:
            easy_streak = 0

        exercise_ids = sorted({it["exercise_id"] for it in workout["items"] if it["exercise_id"]})
        if feedback == "easy":
            # On a comeback day the targets were reduced on purpose, so "easy"
            # says nothing about the normal targets.
            if not workout["comeback_stage"]:
                easy_streak += 1
                if easy_streak >= EASY_ANSWERS_TO_RAISE:
                    result["changes"] = _adjust_targets(cursor, exercise_ids, raise_targets=True)
                    easy_streak = 0
        elif feedback == "hard":
            result["changes"] = _adjust_targets(cursor, exercise_ids, raise_targets=False)
            easy_streak = 0
        else:
            easy_streak = 0

        cursor.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?, 'easy_feedback_streak', ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value;",
            (current_user_id(), str(easy_streak))
        )
        conn.commit()

    result.update(recorded=True, easy_streak=easy_streak)
    return result

def generate_workout_heatmap(
    weeks_count: int = 4,
    today_str: Optional[str] = None,
    db_path: Optional[Path] = None
) -> str:
    """
    Generates a GitHub-style 4-week calendar heatmap in text.
    🟩 = Completed | 🟨 = Partial | ⬜ = Rest | 🟥 = Missed | ⏳ = Today | ▫️ = Future
    """
    if not today_str:
        today_str = get_current_date_str(db_path=db_path)

    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    workout_days_setting = get_setting("workout_days", "0,1,2,3,4,5,6", db_path=db_path)

    # Current week Monday
    current_week_mon = today - timedelta(days=today.weekday())
    # Start date is (weeks_count - 1) weeks before current week Monday
    start_date = current_week_mon - timedelta(weeks=weeks_count - 1)
    end_date = current_week_mon + timedelta(days=6)  # Sunday of this week

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, date, status FROM daily_workouts WHERE user_id = ? AND date >= ? AND date <= ?;",
                       (current_user_id(), start_date.isoformat(), end_date.isoformat()))
        rows = cursor.fetchall()
        workouts_map = {r["date"]: r["status"] for r in rows}

    paused_dates = get_paused_dates(db_path=db_path)
    # Telegram HTML.
    output = "📅 <b>Workout Heatmap</b> · last 4 weeks\n\n<code> M   T   W   T   F   S   S</code>\n"

    curr = start_date
    while curr <= end_date:
        d_str = curr.strftime("%Y-%m-%d")
        w_status = workouts_map.get(d_str)
        is_workout_day = is_day_active(curr.weekday(), workout_days_setting)

        if w_status != "completed" and (w_status == "paused" or d_str in paused_dates):
            icon = "🟦"  # paused (past, today, or planned)
        elif curr > today:
            icon = "▫️"
        elif curr == today:
            if w_status == "completed":
                icon = "🟩"
            elif w_status == "rest" or not is_workout_day:
                icon = "⬜"
            else:
                icon = "⏳"
        else: # Past day
            if w_status == "completed":
                icon = "🟩"
            elif w_status == "rest" or (w_status is None and not is_workout_day):
                icon = "⬜"
            elif w_status == "skipped":
                icon = "🟨"
            else:
                icon = "🟥"

        output += f"{icon} "
        if curr.weekday() == 6:  # Sunday
            output += "\n"
        curr += timedelta(days=1)

    output += (
        "\n<i>🟩 done · 🟨 partial · ⬜ rest · 🟦 paused\n"
        "🟥 missed · ⏳ today · ▫️ coming up</i>"
    )
    return output

# ============================================================================
# GAMIFICATION & BADGES
# ============================================================================

def get_user_badges(
    today_str: Optional[str] = None,
    db_path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """Calculates achievements and milestone badges based on actual history."""
    if not today_str:
        today_str = get_current_date_str(db_path=db_path)

    current_streak, best_streak = calculate_streaks(today_str=today_str, db_path=db_path)

    uid = current_user_id()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Total completed reps per exercise
        cursor.execute(
            """
            SELECT wi.exercise_name, SUM(wi.completed_reps) as total_reps
            FROM workout_items wi JOIN daily_workouts dw ON dw.id = wi.daily_workout_id
            WHERE dw.user_id = ?
            GROUP BY wi.exercise_name;
            """,
            (uid,)
        )
        ex_reps = {r["exercise_name"]: (r["total_reps"] or 0) for r in cursor.fetchall()}
        max_single_ex_reps = max(ex_reps.values()) if ex_reps else 0
        total_lifetime_reps = sum(ex_reps.values())

        # Check total workouts completed
        cursor.execute("SELECT COUNT(*) as cnt FROM daily_workouts WHERE user_id = ? AND status = 'completed';",
                       (uid,))
        total_completed_workouts = cursor.fetchone()["cnt"]

        # Check Early Bird: completed before 09:00 AM
        cursor.execute(
            """
            SELECT COUNT(*) as cnt FROM daily_workouts
            WHERE user_id = ? AND status = 'completed' AND completed_at IS NOT NULL
              AND time(completed_at) < '09:00:00';
            """,
            (uid,)
        )
        early_bird_count = cursor.fetchone()["cnt"]

        # Check Weekend Warrior: completed both Saturday (5) and Sunday (6) workouts
        cursor.execute(
            """
            SELECT strftime('%w', date) as dow, COUNT(*) as cnt
            FROM daily_workouts
            WHERE user_id = ? AND status = 'completed' AND strftime('%w', date) IN ('0', '6')
            GROUP BY dow;
            """,
            (uid,)
        )
        weekend_rows = cursor.fetchall()
        has_weekend_warrior = len(weekend_rows) >= 2

    badges = [
        {
            "name": "First Step",
            "icon": "🌱",
            "desc": "Complete your very first workout",
            "unlocked": total_completed_workouts >= 1,
            "progress": f"{min(1, total_completed_workouts)}/1"
        },
        {
            "name": "Consistent 7",
            "icon": "🔥",
            "desc": "Reach a 7-day workout streak",
            "unlocked": max(current_streak, best_streak) >= 7,
            "progress": f"{min(7, max(current_streak, best_streak))}/7 days"
        },
        {
            "name": "Iron Habit 30",
            "icon": "🏆",
            "desc": "Reach a 30-day streak",
            "unlocked": max(current_streak, best_streak) >= 30,
            "progress": f"{min(30, max(current_streak, best_streak))}/30 days"
        },
        {
            "name": "Century Club",
            "icon": "🥉",
            "desc": "Log 100 total reps of any exercise",
            "unlocked": max_single_ex_reps >= 100,
            "progress": f"{min(100, max_single_ex_reps)}/100 reps"
        },
        {
            "name": "Titan 1,000",
            "icon": "🥇",
            "desc": "Reach 1,000 total lifetime reps across all exercises",
            "unlocked": total_lifetime_reps >= 1000,
            "progress": f"{min(1000, total_lifetime_reps)}/1,000 reps"
        },
        {
            "name": "Early Bird",
            "icon": "⚡",
            "desc": "Finish a workout before 09:00 AM",
            "unlocked": early_bird_count >= 1,
            "progress": f"{min(1, early_bird_count)}/1"
        },
        {
            "name": "Weekend Warrior",
            "icon": "🛡️",
            "desc": "Complete workouts on both Saturday and Sunday",
            "unlocked": has_weekend_warrior,
            "progress": "2/2 weekend days" if has_weekend_warrior else "Incomplete"
        }
    ]

    return badges

# ============================================================================
# CSV EXPORT
# ============================================================================

def export_workouts_csv(db_path: Optional[Path] = None, export_dir: Optional[Path] = None) -> Path:
    """Exports the entire workout history to a CSV file."""
    import csv

    out_dir = export_dir or config.DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_file = out_dir / f"workout_history_{current_user_id()}.csv"

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 
                dw.date,
                dw.status as workout_status,
                dw.completed_at,
                wi.exercise_name,
                wi.target_reps,
                wi.completed_reps,
                wi.unit,
                wi.status as exercise_status
            FROM daily_workouts dw
            LEFT JOIN workout_items wi ON dw.id = wi.daily_workout_id
            WHERE dw.user_id = ?
            ORDER BY dw.date DESC, wi.id ASC;
            """,
            (current_user_id(),)
        )
        rows = cursor.fetchall()

    with open(csv_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Date", "Workout Status", "Completed At",
            "Exercise Name", "Target", "Completed", "Unit", "Exercise Status"
        ])
        for r in rows:
            writer.writerow([
                r["date"], r["workout_status"], r["completed_at"] or "",
                r["exercise_name"] or "", r["target_reps"] or 0,
                r["completed_reps"] or 0, r["unit"] or "", r["exercise_status"] or ""
            ])

    return csv_file

