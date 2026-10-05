"""Sunday evening weekly summary: progress vs last week, records and bot health."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from database import get_connection
from services.workout_service import calculate_streaks, get_current_date_str, get_paused_dates


def week_bounds(today_str: str) -> tuple[str, str]:
    """Monday..Sunday (inclusive) of the week containing `today_str`."""
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _week_numbers(start: str, end: str, db_path: Optional[Path]) -> Dict[str, Any]:
    with get_connection(db_path) as conn:
        counts = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN status NOT IN ('rest', 'paused') THEN 1 ELSE 0 END) AS scheduled
            FROM daily_workouts WHERE date >= ? AND date <= ?;
            """,
            (start, end)
        ).fetchone()
        rows = conn.execute(
            """
            SELECT wi.exercise_name AS name, wi.unit AS unit, SUM(wi.completed_reps) AS total
            FROM workout_items wi JOIN daily_workouts dw ON wi.daily_workout_id = dw.id
            WHERE dw.date >= ? AND dw.date <= ?
            GROUP BY wi.exercise_name, wi.unit;
            """,
            (start, end)
        ).fetchall()
    return {
        "done": counts["done"] or 0,
        "scheduled": counts["scheduled"] or 0,
        "totals": {(r["name"], r["unit"]): r["total"] or 0 for r in rows},
    }


def get_new_records(start: str, end: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Exercises whose best single day this week beats every earlier day."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT wi.exercise_name AS name, wi.unit AS unit,
                   MAX(CASE WHEN dw.date >= ? AND dw.date <= ? THEN wi.completed_reps END) AS this_week,
                   MAX(CASE WHEN dw.date < ? THEN wi.completed_reps END) AS before
            FROM workout_items wi JOIN daily_workouts dw ON wi.daily_workout_id = dw.id
            WHERE dw.date <= ?
            GROUP BY wi.exercise_name, wi.unit;
            """,
            (start, end, start, end)
        ).fetchall()
    # A "record" needs earlier history to beat; the very first week has none.
    return [
        {"name": r["name"], "unit": r["unit"], "value": r["this_week"], "previous": r["before"]}
        for r in rows
        if r["this_week"] and r["before"] and r["this_week"] > r["before"]
    ]


def _delta(now: int, before: int) -> str:
    diff = now - before
    if before == 0 or diff == 0:
        return ""
    return f" ({'+' if diff > 0 else ''}{diff} vs last week)"


def build_weekly_summary(today_str: Optional[str] = None, health_line: str = "",
                         db_path: Optional[Path] = None) -> str:
    """Builds the weekly summary message (Telegram HTML).

    `health_line` is HTML too (callers escape any names in it).
    """
    from views import card, esc, nice_date, unit_label  # views imports this package

    today_str = today_str or get_current_date_str(db_path=db_path)
    start, end = week_bounds(today_str)
    prev_start, prev_end = week_bounds(
        (datetime.strptime(start, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    )

    this_week = _week_numbers(start, end, db_path)
    last_week = _week_numbers(prev_start, prev_end, db_path)
    current_streak, best_streak = calculate_streaks(today_str=today_str, db_path=db_path)
    paused = sum(1 for d in get_paused_dates(db_path=db_path) if start <= d <= end)

    overview = [
        f"🏋️ Workouts  <b>{this_week['done']}/{this_week['scheduled']}</b>"
        + _delta(this_week["done"], last_week["done"]),
        f"🔥 Streak  <b>{current_streak}</b> days · best {best_streak}",
    ]
    if paused:
        overview.append(f"🟦 Paused  {paused} day{'s' if paused != 1 else ''}")
    lines = [f"📆 <b>Your week</b> · {nice_date(start)} – {nice_date(end)}", "", card(overview)]

    if this_week["totals"]:
        totals = []
        for (name, unit), total in sorted(this_week["totals"].items()):
            before = last_week["totals"].get((name, unit), 0)
            totals.append(f"{esc(name)}  <b>{total}</b> {esc(unit_label(unit))}{_delta(total, before)}")
        lines += ["", "<b>Totals</b>", card(totals)]

    records = get_new_records(start, end, db_path)
    if records:
        lines += ["", "🏅 <b>New personal records</b>"]
        for r in records:
            lines.append(f"• {esc(r['name'])}: <b>{r['value']} {esc(unit_label(r['unit']))}</b> "
                         f"in a day (was {r['previous']})")

    if this_week["scheduled"] and this_week["done"] == this_week["scheduled"]:
        lines += ["", "🌟 Perfect week. Every scheduled workout done!"]
    elif this_week["scheduled"] and this_week["done"] == 0 and not paused:
        lines += ["", "💪 New week, fresh start. Even one workout tomorrow restarts the habit."]

    if health_line:
        lines += ["", f"<i>{health_line}</i>"]
    return "\n".join(lines)
