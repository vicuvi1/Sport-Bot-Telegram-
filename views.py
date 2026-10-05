"""How the bot looks: shared design helpers and the main screens.

All screens here are Telegram HTML (send them with parse_mode=HTML). HTML
gives bold/italic and quote-style cards (<blockquote>), and user text only
needs <, > and & escaped, unlike Markdown where a stray "_" or "*" breaks
the whole message.

Design rules:
- A screen starts with "<icon> <b>Title</b> · subtitle".
- Lists of numbers sit in a <blockquote> card.
- Progress is a 10-step ▰▱ bar; finished items get ✅.
- Buttons: the main action first and full width, items in a 2-column grid,
  small secondary actions last.
"""

import html
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_connection, get_setting
from services.workout_service import (
    COMEBACK_FACTORS,
    FEEDBACK_LABELS,
    calculate_streaks,
    get_active_pause,
    get_paused_dates,
    is_day_active,
)

HTML = "HTML"

# Telegram rejects the WHOLE message if any button's callback_data exceeds 64 bytes.
MAX_CALLBACK_DATA_BYTES = 64

FILLED, EMPTY = "▰", "▱"
ICON_DONE, ICON_STARTED, ICON_TODO, ICON_SKIPPED = "✅", "🔸", "⚪", "⏭"

# Heatmap / week strip squares (same legend everywhere).
SQ_DONE, SQ_PARTIAL, SQ_REST, SQ_MISSED, SQ_TODAY, SQ_FUTURE, SQ_PAUSED = "🟩", "🟨", "⬜", "🟥", "⏳", "▫️", "🟦"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def esc(value: Any) -> str:
    """Escapes user/dynamic text for Telegram HTML."""
    return html.escape(str(value), quote=False)


def bar(done: int, target: int, length: int = 10) -> str:
    """▰▰▰▰▰▱▱▱▱▱ progress bar."""
    if target <= 0:
        return FILLED * length
    filled = round(min(1.0, max(0.0, done / target)) * length)
    return FILLED * filled + EMPTY * (length - filled)


def percent(done: int, target: int) -> int:
    return 100 if target <= 0 else round(min(1.0, max(0.0, done / target)) * 100)


def unit_label(unit: str) -> str:
    return "s" if unit == "sec" else unit


def nice_date(date_str: str) -> str:
    """'2026-10-12' -> 'Mon 12 Oct'."""
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b").replace(" 0", " ")


def title(icon: str, text: str, subtitle: str = "") -> str:
    line = f"{icon} <b>{esc(text)}</b>"
    return f"{line} · {esc(subtitle)}" if subtitle else line


def card(lines: List[str], expandable: bool = False) -> str:
    tag = "blockquote expandable" if expandable else "blockquote"
    return f"<{tag}>" + "\n".join(lines) + "</blockquote>"


def grid(buttons: List[InlineKeyboardButton], columns: int = 2) -> List[List[InlineKeyboardButton]]:
    return [buttons[i:i + columns] for i in range(0, len(buttons), columns)]


def item_icon(item: Dict[str, Any]) -> str:
    if item["status"] == "completed":
        return ICON_DONE
    if item["status"] == "skipped":
        return ICON_SKIPPED
    return ICON_STARTED if item["completed_reps"] > 0 else ICON_TODO


def item_line(item: Dict[str, Any]) -> str:
    """One line: '✅ <b>Push-ups</b>  30 / 50 reps'."""
    unit = unit_label(item["unit"])
    return f"{item_icon(item)} <b>{esc(item['exercise_name'])}</b>  {item['completed_reps']} / {item['target_reps']} {esc(unit)}"


def item_block(item: Dict[str, Any]) -> str:
    """Two lines: the item line and its progress bar."""
    head = item_line(item)
    if item["status"] == "skipped":
        return f"{head}\n<i>skipped</i>"
    tail = "" if item["status"] == "completed" else f"  {percent(item['completed_reps'], item['target_reps'])}%"
    return f"{head}\n{bar(item['completed_reps'], item['target_reps'])}{tail}"


# ---------------------------------------------------------------------------
# Buttons shared by several screens
# ---------------------------------------------------------------------------

def quick_toggle_button(is_quick: bool) -> InlineKeyboardButton:
    """"Short on time" button, or its undo while a quick workout is active."""
    if is_quick:
        return InlineKeyboardButton("↩️ Back to full workout", callback_data="quick_undo")
    return InlineKeyboardButton("⏱ Short on time (50%)", callback_data="quick_workout")


def timer_callback_data(seconds: int, label: str) -> str:
    """Builds `start_timer:<secs>:<label>`, trimming the label to fit 64 bytes."""
    prefix = f"start_timer:{seconds}:"
    budget = MAX_CALLBACK_DATA_BYTES - len(prefix.encode("utf-8"))
    label = label.replace(":", " ")
    while len(label.encode("utf-8")) > budget:
        label = label[:-1]
    return prefix + (label.strip() or "Timer")


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------

def build_today_workout_view(workout: Dict[str, Any], date_str: str,
                             intro: str = "") -> Tuple[str, InlineKeyboardMarkup]:
    """The main screen. `intro` (HTML) is placed above it, e.g. a greeting."""
    current_streak, best_streak = calculate_streaks(today_str=date_str)
    streak = f"🔥 {current_streak}-day streak" if current_streak else "🔥 Start a streak today"
    day = nice_date(date_str)
    top = f"{intro}\n\n" if intro else ""

    if workout["status"] == "paused":
        pause = get_active_pause(date_str)
        until = f" until {nice_date(pause['end_date'])}" if pause else ""
        text = top + (
            f"{title('🏖', 'Paused' + until, day)}\n\n"
            "No reminders while you're away, and your streak is frozen.\n"
            f"{streak} <i>(safe)</i>"
        )
        keyboard = [
            [InlineKeyboardButton("▶️ Resume now", callback_data="set_resume")],
            [InlineKeyboardButton("🏖 Change pause", callback_data="menu_pause")],
        ]
        return text, InlineKeyboardMarkup(keyboard)

    if workout["status"] == "rest":
        text = top + (
            f"{title('🌿', 'Rest day', day)}\n\n"
            "Recovery is part of training. Sleep well and eat some protein.\n"
            f"{streak} <i>(safe)</i>"
        )
        return text, InlineKeyboardMarkup([[InlineKeyboardButton("↻ Check schedule", callback_data="refresh_today")]])

    items = workout["items"]
    is_quick = bool(workout.get("quick"))
    comeback_stage = workout.get("comeback_stage") or 0
    all_completed = workout["status"] == "completed"
    done_count = sum(1 for it in items if it["status"] == "completed")

    lines = []
    if all_completed:
        lines.append(title("🎉", "Workout complete!", day))
    else:
        lines.append(title("🏋️", "Today", day))
    if is_quick:
        lines.append("<i>⏱ Quick workout: targets halved, still counts for your streak.</i>")
    elif comeback_stage in COMEBACK_FACTORS:
        pct = int(COMEBACK_FACTORS[comeback_stage] * 100)
        lines.append(f"<i>🔄 Comeback day: targets at {pct}% to ease back in.</i>")

    lines.append("")
    if all_completed:
        # Everything is done: one compact line each, no bars.
        lines.append(card([item_line(it) for it in items]))
    else:
        lines.append(card(["\n\n".join(item_block(it) for it in items)]))
    lines.append("")
    if all_completed:
        lines.append(f"<b>All {len(items)} done</b> · {streak}")
    else:
        lines.append(f"<b>{done_count} of {len(items)} done</b> · {streak}")

    keyboard: List[List[InlineKeyboardButton]] = []
    if all_completed:
        feedback = workout.get("feedback")
        if feedback in FEEDBACK_LABELS:
            lines.append(f"\n📝 You said: {esc(FEEDBACK_LABELS[feedback])}")
        elif not is_quick:
            lines.append("\n<b>How did it feel?</b> Your answer tunes your next targets.")
            keyboard.append([
                InlineKeyboardButton(label, callback_data=f"fb:{key}")
                for key, label in FEEDBACK_LABELS.items()
            ])
        keyboard.append([InlineKeyboardButton("↻ Refresh", callback_data="refresh_today")])
        return top + "\n".join(lines), InlineKeyboardMarkup(keyboard)

    keyboard.append([InlineKeyboardButton("⚡ Complete all", callback_data="complete_all")])
    item_buttons = []
    for it in items:
        if it["status"] == "completed":
            label = f"✅ {it['exercise_name']}"
        elif it["status"] == "skipped":
            label = f"⏭ {it['exercise_name']}"
        else:
            label = f"{it['exercise_name']} · {it['completed_reps']}/{it['target_reps']}"
        item_buttons.append(InlineKeyboardButton(label, callback_data=f"ex_view:{it['id']}"))
    keyboard.extend(grid(item_buttons))
    keyboard.append([quick_toggle_button(is_quick)])
    keyboard.append([
        InlineKeyboardButton("⏱ Rest 60s", callback_data="start_timer:60:Rest"),
        InlineKeyboardButton("⏱ Rest 90s", callback_data="start_timer:90:Rest"),
        InlineKeyboardButton("↻", callback_data="refresh_today"),
    ])
    return top + "\n".join(lines), InlineKeyboardMarkup(keyboard)


def build_exercise_detail_view(item: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    """The card for logging one exercise."""
    unit = unit_label(item["unit"])
    done, target = item["completed_reps"], item["target_reps"]
    if item["status"] == "completed":
        status = "✅ Target reached. Nice work!"
    elif item["status"] == "skipped":
        status = "⏭ Skipped today."
    else:
        status = f"{target - done} {esc(unit)} to go."

    text = (
        f"{item_icon(item)} <b>{esc(item['exercise_name'])}</b>\n\n"
        + card([f"<b>{done}</b> / {target} {esc(unit)}",
                f"{bar(done, target)}  {percent(done, target)}%"])
        + f"\n{status}"
    )

    iid = item["id"]
    keyboard = [
        [
            InlineKeyboardButton(f"✅ Done ({target})", callback_data=f"ex_done:{iid}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"ex_skip:{iid}"),
        ],
        [InlineKeyboardButton(f"+{n}", callback_data=f"ex_delta:{iid}:{n}") for n in (1, 5, 10, 25)],
        [
            InlineKeyboardButton("−1", callback_data=f"ex_delta:{iid}:-1"),
            InlineKeyboardButton("−5", callback_data=f"ex_delta:{iid}:-5"),
            InlineKeyboardButton("✏️ Amount", callback_data=f"ex_enter:{iid}"),
        ],
    ]
    if item["unit"] == "sec" or "plank" in item["exercise_name"].lower():
        keyboard.append([
            InlineKeyboardButton(f"⏱ {target}s timer", callback_data=timer_callback_data(target, item["exercise_name"])),
            InlineKeyboardButton("⏱ 30s timer", callback_data="start_timer:30:Plank"),
        ])
    keyboard.append([InlineKeyboardButton("← Back to today", callback_data="refresh_today")])
    return text, InlineKeyboardMarkup(keyboard)


def feedback_notice(result: Dict[str, Any]) -> str:
    """Explains what a feedback answer changed (HTML)."""
    if not result["recorded"]:
        return "ℹ️ Feedback for today is already saved."
    changes = ", ".join(f"{esc(c['name'])} {c['old']}→{c['new']}" for c in result["changes"])
    if result["feedback"] == "easy":
        if changes:
            return f"📈 <b>Targets raised:</b> {changes}"
        if result["easy_streak"]:
            return "👍 Noted. If it feels too easy next time too, I'll raise your targets."
        return "👍 Noted. Comeback days don't change your normal targets."
    if result["feedback"] == "hard":
        return f"📉 <b>Targets lowered for next time:</b> {changes}" if changes else "📉 Noted. Targets are already at the minimum."
    return "👌 Great, keeping your targets as they are."


def build_evening_nudge(workout: Dict[str, Any], date_str: str) -> Tuple[str, InlineKeyboardMarkup]:
    """19:00 reminder listing only what's left."""
    current_streak, _ = calculate_streaks(today_str=date_str)
    left = [it for it in workout["items"] if it["status"] == "pending" and it["completed_reps"] < it["target_reps"]]
    stake = (f"Your 🔥 <b>{current_streak}-day streak</b> is on the line."
             if current_streak else "A few minutes now starts a new streak.")
    text = (
        f"{title('⏰', 'Still time today', nice_date(date_str))}\n\n"
        f"{stake} Left to do:\n"
        + card(["\n\n".join(item_block(it) for it in left)])
    )
    keyboard = [
        [InlineKeyboardButton("⚡ Complete all", callback_data="complete_all")],
        [InlineKeyboardButton("🏋️ Open workout", callback_data="refresh_today")],
    ]
    if not workout.get("quick"):
        keyboard.append([quick_toggle_button(False)])
    return text, InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

def week_strip(today_str: str) -> str:
    """This week Mon..Sun as squares, e.g. 🟩🟩🟥⏳▫️▫️▫️."""
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    monday = today - timedelta(days=today.weekday())
    workout_days = get_setting("workout_days", "0,1,2,3,4,5,6")
    paused = get_paused_dates()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT date, status FROM daily_workouts WHERE date >= ? AND date <= ?;",
            (monday.isoformat(), (monday + timedelta(days=6)).isoformat())
        ).fetchall()
    status = {r["date"]: r["status"] for r in rows}

    squares = []
    for i in range(7):
        d = monday + timedelta(days=i)
        s = status.get(d.isoformat())
        if s == "completed":
            squares.append(SQ_DONE)
        elif s == "paused" or d.isoformat() in paused:
            squares.append(SQ_PAUSED)
        elif s == "rest" or not is_day_active(d.weekday(), workout_days):
            squares.append(SQ_REST)
        elif d > today:
            squares.append(SQ_FUTURE)
        elif d == today:
            squares.append(SQ_TODAY)
        else:
            squares.append(SQ_PARTIAL if s == "skipped" else SQ_MISSED)
    return "".join(squares)


def build_home(name: str, workout: Dict[str, Any], today_str: str,
               extras: Optional[List[str]] = None) -> str:
    """The /start dashboard (HTML)."""
    current_streak, best_streak = calculate_streaks(today_str=today_str)
    items = workout.get("items", [])
    done = sum(1 for it in items if it["status"] == "completed")

    if workout["status"] == "paused":
        today_line = "🏖 Paused. Enjoy the break."
    elif workout["status"] == "rest":
        today_line = "🌿 Rest day"
    elif workout["status"] == "completed":
        today_line = f"✅ Done! All {len(items)} exercises"
    else:
        today_line = f"🏋️ {done} of {len(items)} done  {bar(done, len(items) or 1)}"

    lines = [
        f"👋 <b>Hi {esc(name)}!</b>" if name else "👋 <b>Welcome back!</b>",
        "",
        card([
            f"<b>Today</b> · {nice_date(today_str)}",
            today_line,
            "",
            f"<b>This week</b>  {week_strip(today_str)}",
            f"🔥 Streak <b>{current_streak}</b> · best {best_streak}",
        ]),
    ]
    if extras:
        lines.append("")
        lines.extend(extras)
    lines.append("\nTap <b>🏋️ Today's Workout</b> below to get going.")
    return "\n".join(lines)
