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

logger = logging.getLogger(__name__)

def build_stats_keyboard(active_period: str = "week") -> InlineKeyboardMarkup:
    """Creates buttons for selecting time periods and extra analytics."""
    periods = [
        ("📅 Today", "today"),
        ("📊 This Week", "week"),
        ("🗓 This Month", "month"),
        ("🏆 All Time", "all"),
    ]
    period_row = []
    for label, period_key in periods:
        text = f"• {label} •" if period_key == active_period else label
        period_row.append(InlineKeyboardButton(text, callback_data=f"stats_period:{period_key}"))

    extra_row_1 = [
        InlineKeyboardButton("🏆 Milestones & Badges", callback_data="stats_show_badges"),
        InlineKeyboardButton("📅 4-Week Heatmap", callback_data="stats_show_heatmap")
    ]
    extra_row_2 = [
        InlineKeyboardButton("📤 Export CSV", callback_data="stats_export_csv")
    ]

    return InlineKeyboardMarkup([
        period_row[:2],
        period_row[2:],
        extra_row_1,
        extra_row_2
    ])

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /progress, /stats commands or '📊 Progress' button."""
    if not is_authorized(update):
        return

    stats = get_stats_for_period("week")
    msg_text = format_stats_message(stats)
    markup = build_stats_keyboard(active_period="week")

    if update.message:
        await update.message.reply_text(msg_text, reply_markup=markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(msg_text, reply_markup=markup, parse_mode="Markdown")

async def stats_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles switching statistics time period or viewing extra analytics."""
    if not is_authorized(update):
        return

    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("stats_period:"):
        period = data.split(":")[1]
        stats = get_stats_for_period(period)
        msg_text = format_stats_message(stats)
        markup = build_stats_keyboard(active_period=period)
        await query.edit_message_text(msg_text, reply_markup=markup, parse_mode="Markdown")
        return

    if data == "stats_show_heatmap":
        heatmap_text = generate_workout_heatmap()
        keyboard = [[InlineKeyboardButton("⬅️ Back to Stats", callback_data="stats_period:week")]]
        await query.edit_message_text(heatmap_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "stats_show_badges":
        badges = get_user_badges()
        text = "🏆 *Your Workout Badges & Milestones*\n\n"
        for b in badges:
            status = "✅ *UNLOCKED*" if b["unlocked"] else f"🔒 *LOCKED* ({b['progress']})"
            text += f"{b['icon']} *{b['name']}* — {status}\n  _{b['desc']}_\n\n"

        keyboard = [[InlineKeyboardButton("⬅️ Back to Stats", callback_data="stats_period:week")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "stats_export_csv":
        csv_file = export_workouts_csv()
        try:
            with open(csv_file, "rb") as f:
                await query.message.reply_document(
                    document=f,
                    filename=csv_file.name,
                    caption="📤 *Workout History Export (CSV)*\n\nCompatible with Microsoft Excel, Apple Numbers, and Google Sheets.",
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Failed to send CSV: {e}")
            await query.message.reply_text(f"⚠️ Error exporting CSV: {e}")
        return

async def badges_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /badges command directly."""
    if not is_authorized(update):
        return

    badges = get_user_badges()
    text = "🏆 *Your Workout Badges & Milestones*\n\n"
    for b in badges:
        status = "✅ *UNLOCKED*" if b["unlocked"] else f"🔒 *LOCKED* ({b['progress']})"
        text += f"{b['icon']} *{b['name']}* — {status}\n  _{b['desc']}_\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def export_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /export command directly."""
    if not is_authorized(update):
        return

    csv_file = export_workouts_csv()
    try:
        with open(csv_file, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=csv_file.name,
                caption="📤 *Workout History Export (CSV)*\n\nCompatible with Microsoft Excel, Apple Numbers, and Google Sheets.",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Failed to send CSV: {e}")
        await update.message.reply_text(f"⚠️ Error exporting CSV: {e}")

async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /history command or '📅 History' button with visual heatmap."""
    if not is_authorized(update):
        return

    # First add the heatmap
    heatmap_text = generate_workout_heatmap()

    history = get_history(limit=7)
    msg = heatmap_text + "\n━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += "📅 *Recent Workouts (Last 7 Days)*\n\n"

    if not history:
        msg += "_No past workouts recorded yet. Start tracking with /today!_"
    else:
        for w in history:
            status_tag = {
                "completed": "✅ Completed",
                "skipped": "⏭ Skipped",
                "rest": "🏖 Rest Day",
                "pending": "⏳ Incomplete"
            }.get(w["status"], w["status"].capitalize())

            msg += f"🗓 *{w['date']}* — {status_tag}\n"
            if w["items"]:
                for it in w["items"]:
                    item_icon = "✅" if it["status"] == "completed" else ("⏭" if it["status"] == "skipped" else "⏳")
                    msg += f"  {item_icon} {it['exercise_name']}: {it['completed_reps']}/{it['target_reps']} {it['unit']}\n"
            elif w["status"] == "rest":
                msg += "  _Scheduled rest day_\n"
            msg += "\n"

    await update.message.reply_text(msg, parse_mode="Markdown")
