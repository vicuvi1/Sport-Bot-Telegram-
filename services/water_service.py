"""Water hydration tracking and personalized reminder service.

Calibrated for user parameters (75 kg, 186 cm, active training):
Baseline formula: ~36 ml per kg bodyweight = ~2,700 ml / day (~11 glasses of 250ml).
Fully configurable in user settings (morning reminder at 06:30 AM, daytime intervals).
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import current_user_id, get_connection, get_setting, set_setting
from views import HTML, bar, card, esc, nice_date, percent, title

DEFAULT_WEIGHT_KG = 75
DEFAULT_HEIGHT_CM = 186
DEFAULT_WATER_TARGET_ML = 2700
DEFAULT_MORNING_WATER_TIME = "06:30"
DEFAULT_WATER_INTERVAL_HOURS = 2


def ensure_water_table(conn) -> None:
    """Creates the water_logs table if not already present."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS water_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            amount_ml INTEGER NOT NULL,
            logged_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def calculate_personalized_target(weight_kg: float = 75.0) -> int:
    """Calculates optimal daily hydration target in ml based on active training weight."""
    # 36 ml per kg bodyweight for active calisthenics/strength training
    raw = round(weight_kg * 36)
    # Round to nearest 50 ml
    return round(raw / 50) * 50


def get_water_target(user_id: Optional[int] = None, db_path: Optional[Path] = None) -> int:
    """Gets the user's daily water target from settings or calculates it."""
    uid = user_id or current_user_id()
    val = get_setting("water_daily_target", db_path=db_path)
    if val:
        try:
            return max(500, int(val))
        except ValueError:
            pass

    # Fallback: check weight setting
    weight_val = get_setting("user_weight_kg", str(DEFAULT_WEIGHT_KG), db_path=db_path)
    try:
        weight = float(weight_val)
    except ValueError:
        weight = float(DEFAULT_WEIGHT_KG)

    return calculate_personalized_target(weight)


def log_water(
    amount_ml: int,
    date_str: Optional[str] = None,
    user_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Logs water intake (positive to add, negative to subtract)."""
    uid = user_id or current_user_id()
    from services.workout_service import get_current_date_str

    day = date_str or get_current_date_str(db_path=db_path)
    now_iso = datetime.now().isoformat()

    with get_connection(db_path) as conn:
        ensure_water_table(conn)
        conn.execute(
            """
            INSERT INTO water_logs (user_id, date, amount_ml, logged_at)
            VALUES (?, ?, ?, ?);
            """,
            (uid, day, amount_ml, now_iso),
        )
        conn.commit()

    # One shared log: the glass also counts in Life System (and its total is the one shown).
    if date_str is None and db_path is None:
        from services import life_sync
        shared = life_sync.add_water(amount_ml)
        if shared:
            return _from_shared(shared, day, uid, db_path)

    return get_today_water(date_str=day, user_id=uid, db_path=db_path)


def get_today_water(
    date_str: Optional[str] = None,
    user_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Retrieves hydration status and logged entries for today."""
    uid = user_id or current_user_id()
    from services.workout_service import get_current_date_str

    day = date_str or get_current_date_str(db_path=db_path)

    # Today's total comes from Life System when it's connected (water logged in
    # the app or with the Mega coach counts here too).
    if date_str is None and db_path is None:
        from services import life_sync
        shared = life_sync.state()
        if shared:
            return _from_shared(shared, day, uid, db_path)

    target = get_water_target(user_id=uid, db_path=db_path)

    with get_connection(db_path) as conn:
        ensure_water_table(conn)
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_ml), 0) as total
            FROM water_logs
            WHERE user_id = ? AND date = ?;
            """,
            (uid, day),
        ).fetchone()
        total_ml = max(0, row["total"] if row else 0)

        logs_rows = conn.execute(
            """
            SELECT id, amount_ml, logged_at
            FROM water_logs
            WHERE user_id = ? AND date = ?
            ORDER BY id DESC LIMIT 5;
            """,
            (uid, day),
        ).fetchall()
        recent_logs = [dict(r) for r in logs_rows]

    pct = percent(total_ml, target)
    glasses = round(total_ml / 250, 1)
    target_glasses = round(target / 250)

    return {
        "date": day,
        "total_ml": total_ml,
        "target_ml": target,
        "percentage": pct,
        "glasses": glasses,
        "target_glasses": target_glasses,
        "recent_logs": recent_logs,
        "is_completed": total_ml >= target,
    }


def _from_shared(shared: Dict[str, Any], day: str, uid: int, db_path: Optional[Path]) -> Dict[str, Any]:
    """The hydration status from Life System's shared log (its total and target)."""
    water = shared.get("water") or {}
    total_ml = max(0, int(water.get("ml") or 0))
    target = int(water.get("target") or 0) or get_water_target(user_id=uid, db_path=db_path)
    return {
        "date": day,
        "total_ml": total_ml,
        "target_ml": target,
        "percentage": percent(total_ml, target),
        "glasses": round(total_ml / 250, 1),
        "target_glasses": round(target / 250),
        "recent_logs": [],
        "is_completed": total_ml >= target,
        "shared": True,
    }


def build_water_view(
    status: Dict[str, Any],
    intro: str = "",
) -> Tuple[str, InlineKeyboardMarkup]:
    """Builds the HTML card and inline keyboard for the water tracker."""
    day = nice_date(status["date"])
    total = status["total_ml"]
    target = status["target_ml"]
    pct = status["percentage"]

    lines = []
    lines.append(title("💧", "Hydration Tracker", day))
    lines.append("")

    card_lines = [
        f"<b>{total:,}</b> / {target:,} ml ({pct}%)",
        f"{bar(total, target)}",
        f"🥛 {status['glasses']} of {status['target_glasses']} glasses (250 ml each)",
    ]
    lines.append(card(card_lines))
    lines.append("")

    if status["is_completed"]:
        lines.append("🌟 <b>Daily Hydration Goal Smashed!</b> Your body thanks you.")
    else:
        left = max(0, target - total)
        lines.append(f"<b>{left:,} ml</b> left to reach today's target.")

    if status.get("shared"):
        lines.append("<i>🔗 Shared with Life System: water from the app counts here too.</i>")
    else:
        lines.append("<i>Calibrated for 75 kg / 186 cm active training.</i>")
    lines.append("<i>✍️ Or just type: <b>water 250</b>, <b>drink 500</b></i>")

    top = f"{intro}\n\n" if intro else ""
    text = top + "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("💧 +250 ml (1 glass)", callback_data="water_log:250"),
            InlineKeyboardButton("💧 +500 ml (bottle)", callback_data="water_log:500"),
        ],
        [
            InlineKeyboardButton("💧 +1000 ml", callback_data="water_log:1000"),
            InlineKeyboardButton("➖ -250 ml", callback_data="water_log:-250"),
        ],
        [
            InlineKeyboardButton("⚙️ Water Settings", callback_data="menu_water_settings"),
            InlineKeyboardButton("↻ Refresh", callback_data="refresh_water"),
        ],
        [
            InlineKeyboardButton("🏋️ Back to Workout", callback_data="refresh_today"),
        ],
    ]

    return text, InlineKeyboardMarkup(keyboard)
