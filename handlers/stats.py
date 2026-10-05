import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.start import is_authorized
from services.stats_service import get_stats_for_period, format_stats_message
from services.workout_service import (
    get_history,
    get_current_date_str,
    generate_workout_heatmap,
    get_user_badges,
    export_workouts_csv
)
from views import HTML, card, esc, nice_date, unit_label

logger = logging.getLogger(__name__)

PERIODS = [("Today", "today"), ("Week", "week"), ("Month", "month"), ("All", "all")]
BACK_TO_STATS = [InlineKeyboardButton("← Back to stats", callback_data="stats_period:week")]


def build_stats_keyboard(active_period: str = "week") -> InlineKeyboardMarkup:
    """Period switcher (active one marked) plus badges, heatmap and export."""
    period_row = [
        InlineKeyboardButton(f"• {label} •" if key == active_period else label,
                             callback_data=f"stats_period:{key}")
        for label, key in PERIODS
    ]
    return InlineKeyboardMarkup([
        period_row,
        [
            InlineKeyboardButton("🏆 Badges", callback_data="stats_show_badges"),
            InlineKeyboardButton("📅 Heatmap", callback_data="stats_show_heatmap"),
        ],
        [InlineKeyboardButton("📤 Export CSV", callback_data="stats_export_csv")],
    ])


def build_badges_text() -> str:
    badges = get_user_badges()
    unlocked = sum(1 for b in badges if b["unlocked"])
    blocks = []
    # Unlocked first: what you've achieved, then what's next.
    for b in sorted(badges, key=lambda b: not b["unlocked"]):
        if b["unlocked"]:
            blocks.append(f"{b['icon']} <b>{esc(b['name'])}</b> ✓\n<i>{esc(b['desc'])}</i>")
        else:
            blocks.append(f"🔒 <b>{esc(b['name'])}</b> · {esc(b['progress'])}\n<i>{esc(b['desc'])}</i>")
    return f"🏆 <b>Badges</b> · {unlocked} of {len(badges)} unlocked\n\n" + card(["\n\n".join(blocks)])


STATUS_TAGS = {
    "completed": "✅ done",
    "skipped": "⏭ skipped",
    "rest": "🌿 rest day",
    "pending": "🟥 not finished",
    "paused": "🟦 paused",
}


def build_history_text() -> str:
    """Heatmap plus the last 7 days, with per-exercise detail folded away."""
    lines = [generate_workout_heatmap(), "", "<b>Recent days</b>"]
    history = get_history(limit=7)
    today_str = get_current_date_str()
    if not history:
        lines.append("<i>Nothing logged yet. Start with /today!</i>")
        return "\n".join(lines)

    entries = []
    for w in history:
        tag = "⏳ today" if w["date"] == today_str and w["status"] == "pending" else STATUS_TAGS.get(w["status"], w["status"])
        entry = f"<b>{nice_date(w['date'])}</b> · {tag}"
        if w["items"]:
            entry += "\n" + " · ".join(
                f"{esc(it['exercise_name'])} {it['completed_reps']}/{it['target_reps']}"
                + ("" if it["unit"] == "reps" else esc(unit_label(it["unit"])))
                for it in w["items"]
            )
        entries.append(entry)
    lines.append(card(["\n\n".join(entries)], expandable=True))
    return "\n".join(lines)


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /progress, /stats commands or '📊 Progress' button."""
    if not is_authorized(update):
        return

    msg_text = format_stats_message(get_stats_for_period("week"))
    markup = build_stats_keyboard(active_period="week")

    if update.message:
        await update.message.reply_text(msg_text, reply_markup=markup, parse_mode=HTML)
    elif update.callback_query:
        await update.callback_query.edit_message_text(msg_text, reply_markup=markup, parse_mode=HTML)


async def stats_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles switching statistics time period or viewing extra analytics."""
    if not is_authorized(update):
        return

    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("stats_period:"):
        period = data.split(":")[1]
        msg_text = format_stats_message(get_stats_for_period(period))
        await query.edit_message_text(msg_text, reply_markup=build_stats_keyboard(active_period=period),
                                      parse_mode=HTML)
        return

    if data == "stats_show_heatmap":
        await query.edit_message_text(generate_workout_heatmap(), reply_markup=InlineKeyboardMarkup([BACK_TO_STATS]),
                                      parse_mode=HTML)
        return

    if data == "stats_show_badges":
        await query.edit_message_text(build_badges_text(), reply_markup=InlineKeyboardMarkup([BACK_TO_STATS]),
                                      parse_mode=HTML)
        return

    if data == "stats_export_csv":
        await send_csv_export(query.message)
        return


async def send_csv_export(message) -> None:
    csv_file = export_workouts_csv()
    try:
        with open(csv_file, "rb") as f:
            await message.reply_document(
                document=f,
                filename=csv_file.name,
                caption="📤 <b>Your workout history</b>\nOpens in Excel, Numbers and Google Sheets.",
                parse_mode=HTML
            )
    except Exception as e:
        logger.error(f"Failed to send CSV: {e}")
        await message.reply_text("⚠️ Couldn't create the export. The error was reported.")


async def badges_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /badges command directly."""
    if not is_authorized(update):
        return
    await update.message.reply_text(build_badges_text(), parse_mode=HTML)


async def export_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /export command directly."""
    if not is_authorized(update):
        return
    await send_csv_export(update.message)


async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /history command or '📅 History' button with visual heatmap."""
    if not is_authorized(update):
        return
    await update.message.reply_text(build_history_text(), parse_mode=HTML)
