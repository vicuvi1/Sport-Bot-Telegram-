from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path

from database import get_connection, get_setting, current_user_id
from services.workout_service import calculate_streaks, get_current_date_str

def get_date_range(period: str, today_str: Optional[str] = None) -> tuple[Optional[str], str]:
    """Calculates (start_date_str, end_date_str) for given period."""
    if not today_str:
        today_str = get_current_date_str()
    today = datetime.strptime(today_str, "%Y-%m-%d").date()

    if period == "today":
        return today_str, today_str
    elif period == "week":
        # Monday of current week
        start_date = today - timedelta(days=today.weekday())
        return start_date.strftime("%Y-%m-%d"), today_str
    elif period == "month":
        start_date = today.replace(day=1)
        return start_date.strftime("%Y-%m-%d"), today_str
    elif period == "all":
        return None, today_str
    else:
        # Default to week
        start_date = today - timedelta(days=today.weekday())
        return start_date.strftime("%Y-%m-%d"), today_str

def get_stats_for_period(
    period: str = "week",
    today_str: Optional[str] = None,
    db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """
    Computes aggregated workout metrics and exercise statistics for the given period.
    Supported periods: 'today', 'week', 'month', 'all'.
    """
    if not today_str:
        today_str = get_current_date_str(db_path=db_path)

    start_date, end_date = get_date_range(period, today_str)
    current_streak, best_streak = calculate_streaks(today_str=today_str, db_path=db_path)

    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Count completed and scheduled workouts in the date range
        date_filter = ""
        params: List[Any] = []
        uid = current_user_id()
        if start_date:
            date_filter = "WHERE user_id = ? AND date >= ? AND date <= ?"
            params = [uid, start_date, end_date]
        else:
            date_filter = "WHERE user_id = ? AND date <= ?"
            params = [uid, end_date]

        cursor.execute(
            f"""
            SELECT 
                COUNT(*) as total_days,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_workouts,
                SUM(CASE WHEN status NOT IN ('rest', 'paused') THEN 1 ELSE 0 END) as scheduled_workouts
            FROM daily_workouts
            {date_filter};
            """,
            params
        )
        counts = cursor.fetchone()
        completed_workouts = counts["completed_workouts"] or 0
        scheduled_workouts = counts["scheduled_workouts"] or 0

        # Exercise breakdowns
        wi_date_filter = ""
        wi_params: List[Any] = []
        if start_date:
            wi_date_filter = "WHERE dw.user_id = ? AND dw.date >= ? AND dw.date <= ?"
            wi_params = [uid, start_date, end_date]
        else:
            wi_date_filter = "WHERE dw.user_id = ? AND dw.date <= ?"
            wi_params = [uid, end_date]

        cursor.execute(
            f"""
            SELECT 
                wi.exercise_name,
                wi.unit,
                SUM(wi.target_reps) as total_target,
                SUM(wi.completed_reps) as total_completed
            FROM workout_items wi
            JOIN daily_workouts dw ON wi.daily_workout_id = dw.id
            {wi_date_filter}
            GROUP BY wi.exercise_name, wi.unit
            ORDER BY wi.exercise_name ASC;
            """,
            wi_params
        )
        exercise_rows = cursor.fetchall()

        exercises_stats = []
        for r in exercise_rows:
            target = r["total_target"] or 0
            completed = r["total_completed"] or 0
            pct = round((completed / target * 100)) if target > 0 else 0
            exercises_stats.append({
                "name": r["exercise_name"],
                "unit": r["unit"],
                "target": target,
                "completed": completed,
                "percentage": pct
            })

        period_labels = {
            "today": "Today",
            "week": "This Week",
            "month": "This Month",
            "all": "All Time"
        }

        return {
            "period": period,
            "period_label": period_labels.get(period, "This Week"),
            "start_date": start_date,
            "end_date": end_date,
            "completed_workouts": completed_workouts,
            "scheduled_workouts": scheduled_workouts,
            "current_streak": current_streak,
            "best_streak": best_streak,
            "exercises": exercises_stats
        }

def format_stats_message(stats: Dict[str, Any]) -> str:
    """Formats statistics as a Telegram HTML card."""
    from views import bar, card, esc, nice_date, percent, unit_label  # views imports this package

    if stats["period"] == "all" or not stats.get("start_date"):
        subtitle = "since you started"
    elif stats["start_date"] == stats["end_date"]:
        subtitle = nice_date(stats["end_date"])
    else:
        subtitle = f"{nice_date(stats['start_date'])} – {nice_date(stats['end_date'])}"
    lines = [f"📊 <b>{esc(stats.get('period_label', 'Statistics'))}</b> · {subtitle}", ""]

    if stats["exercises"]:
        blocks = []
        for ex in stats["exercises"]:
            unit = esc(unit_label(ex["unit"]))
            blocks.append(
                f"<b>{esc(ex['name'])}</b>  {ex['completed']} / {ex['target']} {unit}\n"
                f"{bar(ex['completed'], ex['target'])}  {percent(ex['completed'], ex['target'])}%"
            )
        lines.append(card(["\n\n".join(blocks)]))
    else:
        lines.append("<i>No workouts logged in this period yet.</i>")

    lines += [
        "",
        f"🏋️ Workouts <b>{stats['completed_workouts']}/{stats['scheduled_workouts']}</b> · "
        f"🔥 Streak <b>{stats['current_streak']}</b> · 🏆 Best <b>{stats['best_streak']}</b>",
    ]
    return "\n".join(lines)

