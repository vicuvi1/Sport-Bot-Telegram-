from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, Tuple
import math
from zoneinfo import ZoneInfo
from pathlib import Path

import config
from database import get_connection, get_setting, set_setting

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
            cursor.execute("SELECT * FROM exercises WHERE is_active = 1 ORDER BY id ASC;")
        else:
            cursor.execute("SELECT * FROM exercises ORDER BY id ASC;")
        return [dict(r) for r in cursor.fetchall()]

def get_exercise_by_id(exercise_id: int, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Finds an exercise by its ID."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM exercises WHERE id = ?;", (exercise_id,))
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
            INSERT INTO exercises (name, target_reps, unit, is_active, days_of_week, created_at)
            VALUES (?, ?, ?, 1, ?, ?);
            """,
            (name, int(target_reps), unit, days_of_week.strip(), now_iso)
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

    params.append(exercise_id)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE exercises SET {', '.join(fields)} WHERE id = ?;",
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
        cursor.execute("DELETE FROM exercises WHERE id = ?;", (exercise_id,))
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

    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Check existing workout
        cursor.execute("SELECT * FROM daily_workouts WHERE date = ?;", (date_str,))
        workout_row = cursor.fetchone()

        if workout_row:
            workout_id = workout_row["id"]
        else:
            # Check global workout_days setting
            workout_days_setting = get_setting("workout_days", "0,1,2,3,4,5,6", db_path=db_path)
            is_global_workout_day = is_day_active(weekday, workout_days_setting)

            # Find matching active exercises for this day
            cursor.execute("SELECT * FROM exercises WHERE is_active = 1 ORDER BY id ASC;")
            all_active = [dict(r) for r in cursor.fetchall()]
            eligible_exercises = [
                ex for ex in all_active if is_day_active(weekday, ex.get("days_of_week", "0,1,2,3,4,5,6"))
            ]

            if not is_global_workout_day or not eligible_exercises:
                # Rest day
                cursor.execute(
                    "INSERT INTO daily_workouts (date, status, created_at) VALUES (?, 'rest', ?);",
                    (date_str, now_iso)
                )
                conn.commit()
                workout_id = cursor.lastrowid
            else:
                # Active workout day
                cursor.execute(
                    "INSERT INTO daily_workouts (date, status, created_at) VALUES (?, 'pending', ?);",
                    (date_str, now_iso)
                )
                workout_id = cursor.lastrowid

                for ex in eligible_exercises:
                    cursor.execute(
                        """
                        INSERT INTO workout_items (
                            daily_workout_id, exercise_id, exercise_name, target_reps, completed_reps, unit, status, updated_at
                        ) VALUES (?, ?, ?, ?, 0, ?, 'pending', ?);
                        """,
                        (workout_id, ex["id"], ex["name"], ex["target_reps"], ex["unit"], now_iso)
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
        cursor.execute("SELECT * FROM workout_items WHERE id = ?;", (item_id,))
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

        # Check if entire workout is complete
        cursor.execute("SELECT status, completed_reps, target_reps FROM workout_items WHERE daily_workout_id = ?;", (workout_id,))
        all_items = [dict(r) for r in cursor.fetchall()]
        
        all_done = all(it["status"] in ("completed", "skipped") for it in all_items)
        has_completed = any(it["status"] == "completed" for it in all_items)

        if all_done and has_completed:
            cursor.execute(
                "UPDATE daily_workouts SET status = 'completed', completed_at = ? WHERE id = ?;",
                (now_iso, workout_id)
            )
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
        cursor.execute("SELECT * FROM exercises WHERE id = ?;", (exercise_id,))
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

        # Check if all recent N items were successfully completed
        for item in recent_items:
            if item["status"] != "completed" or item["completed_reps"] < item["target_reps"]:
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
    Rest days DO NOT break streaks.
    """
    if not today_str:
        today_str = get_current_date_str(db_path=db_path)

    if workout_days_setting is None:
        workout_days_setting = get_setting("workout_days", "0,1,2,3,4,5,6", db_path=db_path)

    today_date = datetime.strptime(today_str, "%Y-%m-%d").date()

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT date, status FROM daily_workouts ORDER BY date ASC;")
        rows = cursor.fetchall()
        workouts_by_date = {r["date"]: r["status"] for r in rows}

    if not workouts_by_date:
        return 0, 0

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
            ORDER BY dw.date DESC
            LIMIT ?;
            """,
            (limit,)
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
        cursor.execute("SELECT id, date, status FROM daily_workouts WHERE date >= ? AND date <= ?;", (start_date.isoformat(), end_date.isoformat()))
        rows = cursor.fetchall()
        workouts_map = {r["date"]: r["status"] for r in rows}

    output = "📅 *Workout Heatmap (Last 4 Weeks)*\n\n` M   T   W   T   F   S   S`\n"

    curr = start_date
    while curr <= end_date:
        d_str = curr.strftime("%Y-%m-%d")
        w_status = workouts_map.get(d_str)
        is_workout_day = is_day_active(curr.weekday(), workout_days_setting)

        if curr > today:
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
        "\n*Legend:*\n"
        "🟩 Done   🟨 Partial   ⬜ Rest\n"
        "🟥 Missed ⏳ Today     ▫️ Future\n"
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

    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Total completed reps per exercise
        cursor.execute(
            """
            SELECT exercise_name, SUM(completed_reps) as total_reps
            FROM workout_items
            GROUP BY exercise_name;
            """
        )
        ex_reps = {r["exercise_name"]: (r["total_reps"] or 0) for r in cursor.fetchall()}
        max_single_ex_reps = max(ex_reps.values()) if ex_reps else 0
        total_lifetime_reps = sum(ex_reps.values())

        # Check total workouts completed
        cursor.execute("SELECT COUNT(*) as cnt FROM daily_workouts WHERE status = 'completed';")
        total_completed_workouts = cursor.fetchone()["cnt"]

        # Check Early Bird: completed before 09:00 AM
        cursor.execute(
            """
            SELECT COUNT(*) as cnt FROM daily_workouts 
            WHERE status = 'completed' AND completed_at IS NOT NULL AND time(completed_at) < '09:00:00';
            """
        )
        early_bird_count = cursor.fetchone()["cnt"]

        # Check Weekend Warrior: completed both Saturday (5) and Sunday (6) workouts
        cursor.execute(
            """
            SELECT strftime('%w', date) as dow, COUNT(*) as cnt
            FROM daily_workouts
            WHERE status = 'completed' AND strftime('%w', date) IN ('0', '6')
            GROUP BY dow;
            """
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
    csv_file = out_dir / "workout_history_export.csv"

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
            ORDER BY dw.date DESC, wi.id ASC;
            """
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

