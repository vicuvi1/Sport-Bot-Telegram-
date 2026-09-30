import asyncio
import logging
from typing import Dict, Any, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from handlers.start import is_authorized
from services.workout_service import (
    get_or_create_daily_workout,
    update_workout_item,
    complete_all_exercises_for_workout,
    calculate_streaks,
    get_current_date_str,
    render_progress_bar
)

logger = logging.getLogger(__name__)

async def run_timer_alert(bot, chat_id: int, seconds: int, label: str) -> None:
    """Waits for specified duration and alerts the user."""
    await asyncio.sleep(seconds)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"🔔 *Time's Up!* ({label})\n\nYour *{seconds}s* interval is complete. Ready for the next set! 💪",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Timer alert failed: {e}")

def build_today_workout_view(workout: Dict[str, Any], date_str: str) -> tuple[str, InlineKeyboardMarkup]:
    """Generates the text and inline keyboard for the daily workout overview with visual progress bars."""
    current_streak, _ = calculate_streaks(today_str=date_str)

    if workout["status"] == "rest":
        text = (
            f"🏖 *Today is a Rest Day!* ({date_str})\n\n"
            f"No workout is scheduled for today. Rest and recharge!\n"
            f"🔥 Current streak: *{current_streak} days* (preserved)."
        )
        keyboard = [[InlineKeyboardButton("🔄 Check Schedule", callback_data="refresh_today")]]
        return text, InlineKeyboardMarkup(keyboard)

    all_completed = workout["status"] == "completed"
    header_status = "🎉 COMPLETED!" if all_completed else "IN PROGRESS"
    text = f"🏋️ *Today's Workout* ({date_str}) — *{header_status}*\n\n"

    keyboard = []
    for item in workout["items"]:
        status_icon = "✅" if item["status"] == "completed" else ("⏭" if item["status"] == "skipped" else "⏳")
        bar = render_progress_bar(item["completed_reps"], item["target_reps"], length=8)
        text += f"{status_icon} *{item['exercise_name']}* ({item['completed_reps']}/{item['target_reps']} {item['unit']})\n  `{bar}`\n"
        
        btn_label = f"{status_icon} {item['exercise_name']} ({item['completed_reps']}/{item['target_reps']})"
        keyboard.append([InlineKeyboardButton(btn_label, callback_data=f"ex_view:{item['id']}")])

    text += f"\n🔥 Streak: *{current_streak} days*\n"
    if all_completed:
        text += "\n🌟 *Amazing effort! All exercises for today are complete.*"
    else:
        text += "\nTap an exercise to log reps, or tap *Complete All*:"
        keyboard.insert(0, [InlineKeyboardButton("⚡ Complete All as Scheduled", callback_data="complete_all")])

    # NOTE: The Mini App (web_app) button was removed. Telegram requires an
    # HTTPS web_app URL and rejects the whole message for http://localhost,
    # which broke /today. A plain Refresh callback button is used instead.
    keyboard.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="refresh_today")
    ])
    keyboard.append([
        InlineKeyboardButton("⏱ 60s Rest", callback_data="start_timer:60:Rest"),
        InlineKeyboardButton("⏱ 90s Rest", callback_data="start_timer:90:Rest")
    ])
    return text, InlineKeyboardMarkup(keyboard)

def build_exercise_detail_view(item: Dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Generates the action card for a specific exercise item with presets and timers."""
    status_icon = "✅" if item["status"] == "completed" else ("⏭" if item["status"] == "skipped" else "⏳")
    bar = render_progress_bar(item["completed_reps"], item["target_reps"], length=8)
    text = (
        f"*{item['exercise_name']}*\n\n"
        f"🎯 *Goal:* {item['target_reps']} {item['unit']}\n"
        f"💪 *Completed:* {item['completed_reps']} {item['unit']}\n"
        f"📊 *Progress:* `{bar}`\n"
        f"📌 *Status:* {status_icon} {item['status'].capitalize()}\n"
    )

    keyboard = [
        [
            InlineKeyboardButton(f"✅ {item['target_reps']} Done", callback_data=f"ex_done:{item['id']}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"ex_skip:{item['id']}")
        ],
        [
            InlineKeyboardButton("➕ +1", callback_data=f"ex_delta:{item['id']}:1"),
            InlineKeyboardButton("➕ +5", callback_data=f"ex_delta:{item['id']}:5"),
            InlineKeyboardButton("➕ +10", callback_data=f"ex_delta:{item['id']}:10"),
            InlineKeyboardButton("➕ +25", callback_data=f"ex_delta:{item['id']}:25"),
        ],
        [
            InlineKeyboardButton("➖ -1", callback_data=f"ex_delta:{item['id']}:-1"),
            InlineKeyboardButton("➖ -5", callback_data=f"ex_delta:{item['id']}:-5"),
            InlineKeyboardButton("✏️ Enter Amount", callback_data=f"ex_enter:{item['id']}")
        ]
    ]

    # Add quick timer if unit is seconds or plank
    if item["unit"] == "sec" or "plank" in item["exercise_name"].lower():
        keyboard.append([
            InlineKeyboardButton(f"⏱ Start {item['target_reps']}s Timer", callback_data=f"start_timer:{item['target_reps']}:{item['exercise_name']}"),
            InlineKeyboardButton("⏱ 30s Timer", callback_data="start_timer:30:Plank")
        ])

    keyboard.append([
        InlineKeyboardButton("⬅️ Back to Workout List", callback_data="refresh_today")
    ])

    return text, InlineKeyboardMarkup(keyboard)

async def today_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /today command or main menu button."""
    if not is_authorized(update):
        return

    today_str = get_current_date_str()
    workout = get_or_create_daily_workout(today_str)
    text, markup = build_today_workout_view(workout, today_str)

    if update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")

async def workout_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles interactive button callbacks for exercises."""
    if not is_authorized(update):
        return

    query = update.callback_query
    data = query.data

    # --- Acknowledge the click IMMEDIATELY, before any database work, so the
    # Telegram loading spinner disappears instantly. Exactly ONE answer() call
    # is made per callback (never twice).
    if data == "complete_all":
        await query.answer("⚡ All exercises logged as completed! Great workout!", show_alert=True)
    elif data == "snooze_reminder":
        await query.answer("💤 Reminder snoozed for 60 minutes.", show_alert=True)
    elif data.startswith("start_timer:"):
        timer_parts = data.split(":")
        timer_label = timer_parts[2] if len(timer_parts) > 2 else "Timer"
        await query.answer(f"⏱ {timer_label} timer started ({timer_parts[1]}s). You will be notified when it expires!")
    else:
        await query.answer()

    today_str = get_current_date_str()

    if data == "complete_all":
        workout = get_or_create_daily_workout(today_str)
        updated_workout, progressions = complete_all_exercises_for_workout(workout["id"])

        if progressions:
            for prog in progressions:
                alert_text = (
                    f"🚀 *Progression Triggered!*\n"
                    f"{prog['exercise_name']} increased from {prog['old_target']} to {prog['new_target']} {prog['unit']}!"
                )
                await query.message.reply_text(alert_text, parse_mode="Markdown")

        text, markup = build_today_workout_view(updated_workout, today_str)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data.startswith("start_timer:"):
        parts = data.split(":")
        secs = int(parts[1])
        label = parts[2] if len(parts) > 2 else "Timer"
        asyncio.create_task(run_timer_alert(context.bot, query.from_user.id, secs, label))
        return

    if data == "snooze_reminder":
        asyncio.create_task(run_timer_alert(context.bot, query.from_user.id, 3600, "Snoozed Workout Reminder"))
        return

    if data == "refresh_today":
        workout = get_or_create_daily_workout(today_str)
        text, markup = build_today_workout_view(workout, today_str)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data.startswith("ex_view:"):
        item_id = int(data.split(":")[1])
        workout = get_or_create_daily_workout(today_str)
        item = next((it for it in workout["items"] if it["id"] == item_id), None)
        if not item:
            await query.edit_message_text("⚠️ Exercise item not found.", parse_mode="Markdown")
            return
        text, markup = build_exercise_detail_view(item)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data.startswith("ex_done:"):
        item_id = int(data.split(":")[1])
        workout = get_or_create_daily_workout(today_str)
        item = next((it for it in workout["items"] if it["id"] == item_id), None)
        if item:
            updated_item, prog = update_workout_item(item_id, completed_reps=item["target_reps"])
            if prog:
                alert_text = (
                    f"🚀 *Progression Triggered!*\n"
                    f"{prog['exercise_name']} increased from {prog['old_target']} to {prog['new_target']} {prog['unit']}!"
                )
                await query.message.reply_text(alert_text, parse_mode="Markdown")
        workout = get_or_create_daily_workout(today_str)
        text, markup = build_today_workout_view(workout, today_str)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data.startswith("ex_skip:"):
        item_id = int(data.split(":")[1])
        update_workout_item(item_id, status="skipped")
        workout = get_or_create_daily_workout(today_str)
        text, markup = build_today_workout_view(workout, today_str)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data.startswith("ex_delta:"):
        parts = data.split(":")
        item_id = int(parts[1])
        delta = int(parts[2])
        updated_item, prog = update_workout_item(item_id, delta_reps=delta)
        if prog:
            alert_text = (
                f"🚀 *Progression Triggered!*\n"
                f"{prog['exercise_name']} increased from {prog['old_target']} to {prog['new_target']} {prog['unit']}!"
            )
            await query.message.reply_text(alert_text, parse_mode="Markdown")
        if updated_item:
            text, markup = build_exercise_detail_view(updated_item)
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data.startswith("ex_enter:"):
        item_id = int(data.split(":")[1])
        workout = get_or_create_daily_workout(today_str)
        item = next((it for it in workout["items"] if it["id"] == item_id), None)
        if not item:
            return

        context.user_data["awaiting_reps_item_id"] = item_id
        prompt_text = (
            f"✏️ *Enter Completed Amount*\n\n"
            f"How many *{item['exercise_name']}* did you complete? (Goal: {item['target_reps']} {item['unit']})\n\n"
            f"Reply with a number (e.g. `65`), or type `/cancel` to abort."
        )
        await query.message.reply_text(prompt_text, parse_mode="Markdown")
        return

    logger.warning("workout_callback_handler received an unrouted callback_data=%r", data)

async def custom_amount_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles text message input when the user is prompted to enter an exact completion amount."""
    if not is_authorized(update):
        return False

    item_id = context.user_data.get("awaiting_reps_item_id")
    if not item_id:
        return False

    text = update.message.text.strip()
    if text.lower() in ("/cancel", "cancel"):
        context.user_data.pop("awaiting_reps_item_id", None)
        await update.message.reply_text("❌ Input cancelled.")
        today_str = get_current_date_str()
        workout = get_or_create_daily_workout(today_str)
        msg_text, markup = build_today_workout_view(workout, today_str)
        await update.message.reply_text(msg_text, reply_markup=markup, parse_mode="Markdown")
        return True

    try:
        amount = int(text)
        if amount < 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid non-negative number (e.g. `45`), or type `/cancel`.")
        return True

    context.user_data.pop("awaiting_reps_item_id", None)
    updated_item, prog = update_workout_item(item_id, completed_reps=amount)
    
    if not updated_item:
        await update.message.reply_text("⚠️ Exercise item could not be updated.")
        return True

    confirm_msg = (
        f"✅ Saved! You logged *{amount} {updated_item['unit']}* for *{updated_item['exercise_name']}* "
        f"(Goal: {updated_item['target_reps']})."
    )
    await update.message.reply_text(confirm_msg, parse_mode="Markdown")

    if prog:
        alert_text = (
            f"🚀 *Progression Level Up!*\n"
            f"{prog['exercise_name']} target increased from {prog['old_target']} to {prog['new_target']} {prog['unit']}!"
        )
        await update.message.reply_text(alert_text, parse_mode="Markdown")

    today_str = get_current_date_str()
    workout = get_or_create_daily_workout(today_str)
    view_text, markup = build_today_workout_view(workout, today_str)
    await update.message.reply_text(view_text, reply_markup=markup, parse_mode="Markdown")
    return True
