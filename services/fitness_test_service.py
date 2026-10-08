"""Monthly fitness test: a few max-effort benchmarks, tracked month by month.

Reps and streaks show effort; these numbers show whether the body is actually
changing. One result per test per month (retaking overwrites it).
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from database import get_connection, get_setting, current_user_id
from services.workout_service import get_current_date_str, local_now_iso

FITNESS_TESTS = [
    {
        "key": "pushups", "name": "Max push-ups", "unit": "reps", "timer": None,
        "how": "One set, good form: chest close to the floor, body straight. Stop when your form breaks.",
    },
    {
        "key": "squats", "name": "Squats in 2 minutes", "unit": "reps", "timer": 120,
        "how": "As many full squats as you can in 2 minutes (thighs at least parallel). Tap the timer, then go.",
    },
    {
        "key": "plank", "name": "Longest plank", "unit": "sec", "timer": None,
        "how": "Forearm plank, body in a straight line, until your hips drop. Time yourself.",
    },
    {
        "key": "pullups", "name": "Max pull-ups", "unit": "reps", "timer": None, "optional": True,
        "how": "Start from a dead hang, chin over the bar each rep, no kipping.",
    },
]
TESTS_BY_KEY = {t["key"]: t for t in FITNESS_TESTS}

# Sanity limits for typed results.
MAX_RESULT = {"reps": 2000, "sec": 3600}

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def active_tests(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Tests included this month (pull-ups only if the user has a bar)."""
    pullups_on = get_setting("test_pullups", "0", db_path=db_path) == "1"
    return [t for t in FITNESS_TESTS if not t.get("optional") or pullups_on]


def current_month(today_str: Optional[str] = None, db_path: Optional[Path] = None) -> str:
    return (today_str or get_current_date_str(db_path=db_path))[:7]


def month_label(month: str) -> str:
    """'2026-10' -> 'Oct 2026'."""
    return datetime.strptime(month, "%Y-%m").strftime("%b %Y")


def parse_result(text: str, unit: str) -> Optional[int]:
    """Parses '31', or for seconds also '1:45' / '1m45s'. None if invalid."""
    text = text.strip().lower()
    value = None
    if re.fullmatch(r"\d{1,4}", text):
        value = int(text)
    elif unit == "sec":
        m = re.fullmatch(r"(\d{1,2})\s*(?::|m|min)\s*(\d{1,2})\s*s?", text)
        if m and int(m.group(2)) < 60:
            value = int(m.group(1)) * 60 + int(m.group(2))
    if value is None or not 0 <= value <= MAX_RESULT.get(unit, 2000):
        return None
    return value


def save_result(month: str, key: str, value: Optional[int], db_path: Optional[Path] = None) -> None:
    """Stores a result (None = skipped). Retaking the test overwrites it."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO fitness_results (user_id, month, test_key, value, recorded_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, month, test_key) DO UPDATE SET value = excluded.value, recorded_at = excluded.recorded_at;
            """,
            (current_user_id(), month, key, value, local_now_iso(db_path))
        )
        conn.commit()


def get_month_results(month: str, db_path: Optional[Path] = None) -> Dict[str, Optional[int]]:
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT test_key, value FROM fitness_results WHERE user_id = ? AND month = ?;",
                            (current_user_id(), month)).fetchall()
    return {r["test_key"]: r["value"] for r in rows}


def is_month_complete(month: str, db_path: Optional[Path] = None) -> bool:
    results = get_month_results(month, db_path)
    return all(t["key"] in results for t in active_tests(db_path))


def next_test_index(month: str, db_path: Optional[Path] = None) -> int:
    """Index of the first test without a result this month (resume point)."""
    results = get_month_results(month, db_path)
    for i, t in enumerate(active_tests(db_path)):
        if t["key"] not in results:
            return i
    return 0  # all done: a new start is a retake


def get_history(key: str, db_path: Optional[Path] = None) -> List[Tuple[str, int]]:
    """[(month, value), ...] oldest first, skipped months left out."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT month, value FROM fitness_results WHERE user_id = ? AND test_key = ? AND value IS NOT NULL "
            "ORDER BY month;",
            (current_user_id(), key)
        ).fetchall()
    return [(r["month"], r["value"]) for r in rows]


def sparkline(values: List[int]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    if high == low:
        return SPARK_CHARS[3] * len(values)
    span = high - low
    return "".join(SPARK_CHARS[round((v - low) / span * (len(SPARK_CHARS) - 1))] for v in values)


def _previous_value(key: str, month: str, db_path: Optional[Path]) -> Optional[int]:
    earlier = [v for m, v in get_history(key, db_path) if m < month]
    return earlier[-1] if earlier else None


def _best_before(key: str, month: str, db_path: Optional[Path]) -> Optional[int]:
    earlier = [v for m, v in get_history(key, db_path) if m < month]
    return max(earlier) if earlier else None


def format_month_report(month: str, db_path: Optional[Path] = None) -> str:
    """Results of one month vs the previous test (Markdown)."""
    results = get_month_results(month, db_path)
    lines = [f"🧪 *Fitness Test — {month_label(month)}*", ""]
    new_bests = 0
    for t in active_tests(db_path):
        if t["key"] not in results:
            continue
        value = results[t["key"]]
        if value is None:
            lines.append(f"• {t['name']}: skipped")
            continue
        line = f"• {t['name']}: *{value} {t['unit']}*"
        prev = _previous_value(t["key"], month, db_path)
        if prev is not None:
            diff = value - prev
            line += f" ({'+' if diff > 0 else ''}{diff} vs last test)" if diff else " (same as last test)"
        best = _best_before(t["key"], month, db_path)
        if best is not None and value > best:
            line += " 🏅"
            new_bests += 1
        lines.append(line)

    if new_bests:
        lines += ["", f"🏅 {new_bests} new personal best{'s' if new_bests > 1 else ''}!"]
    elif not any(_previous_value(k, month, db_path) is not None for k in results):
        lines += ["", "📍 This is your baseline. Next month you'll see how far you've come."]
    return "\n".join(lines)


def format_history(db_path: Optional[Path] = None) -> str:
    """Month-by-month progress for every test with results (Markdown)."""
    lines = ["📈 *Your Fitness Test Progress*", ""]
    any_results = False
    for t in FITNESS_TESTS:
        history = get_history(t["key"], db_path)[-12:]
        if not history:
            continue
        any_results = True
        values = [v for _, v in history]
        trail = " → ".join(str(v) for v in values[-6:])
        lines.append(f"*{t['name']}* ({t['unit']})")
        lines.append(f"`{sparkline(values)}` {trail}")
        if len(values) > 1:
            first, last = values[0], values[-1]
            diff = last - first
            pct = f" ({'+' if diff >= 0 else ''}{round(diff / first * 100)}%)" if first else ""
            lines.append(f"Since {month_label(history[0][0])}: {'+' if diff >= 0 else ''}{diff}{pct}, best {max(values)}")
        lines.append("")
    if not any_results:
        lines.append("No results yet. Take your first test with /test. It becomes your baseline.")
    return "\n".join(lines).rstrip()
