import logging
from typing import Dict, Any, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from handlers.start import is_authorized
from scheduler import schedule_alert, ALERT_TIMER, ALERT_SNOOZE
from services.workout_service import (
    get_or_create_daily_workout,
    update_workout_item,
    complete_all_exercises_for_workout,
    calculate_streaks,
    get_current_date_str,
    get_active_pause,
    render_progress_bar,
    start_quick_workout,
    undo_quick_workout,
    record_feedback,
    FEEDBACK_LABELS,
    COMEBACK_FACTORS
)

logger = logging.getLogger(__name__)

SNOOZE_SECONDS = 3600

# Telegram rejects the WHOLE message if any button's callback_data exceeds 64 bytes.
MAX_CALLBACK_DATA_BYTES = 64

def timer_callback_data(seconds: int, label: str) -> str:
    """Builds `start_timer:<secs>:<label>`, trimming the label to fit 64 bytes."""
    prefix = f"start_timer:{seconds}:"
    budget = MAX_CALLBACK_DATA_BYTES - len(prefix.encode("utf-8"))
    label = label.replace(":", " ")
    while len(label.encode("utf-8")) > budget:
        label = label[:-1]
    return prefix + (label.strip() or "Timer")

def quick_toggle_button(is_quick: bool) -> InlineKeyboardButton:
    """"Short on time" button, or its undo while a quick workout is active."""
    if is_quick:
        return InlineKeyboardButton("↩️ Back to Full Workout", callback_data="quick_undo")
    return InlineKeyboardButton("⏱ Short on Time (50%)", callback_data="quick_workout")

def feedback_notice(result: Dict[str, Any]) -> str:
    """Explains what a feedback answer changed (Markdown)."""
    if not result["recorded"]:
        return "ℹ️ Feedback for today is already saved."
    changes = ", ".join(f"{c['name']} {c['old']}→{c['new']}" for c in result["changes"])
    if result["feedback"] == "easy":
        if changes:
            return f"📈 *Targets raised:* {changes}"
        if result["easy_streak"]:
            return "👍 Noted. If it feels too easy next time too, I'll raise your targets."
        return "👍 Noted. Comeback days don't change your normal targets."
    if result["feedback"] == "hard":
        return f"📉 *Targets lowered for next time:* {changes}" if changes else "📉 Noted. Targets are already at the minimum."
    return "👌 Great, keeping your targets as they are."

def build_today_workout_view(workout: Dict[str, Any], date_str: str) -> tuple[str, InlineKeyboardMarkup]:
    """Generates the text and inline keyboard for the daily workout overview with visual progress bars."""
    current_streak, _ = calculate_streaks(today_str=date_str)

    if workout["status"] == "paused":
        pause = get_active_pause(date_str)
        until = f" until *{pause['end_date']}*" if pause else ""
        text = (
            f"🟦 *Workouts are paused{until}* ({date_str})\n\n"
            "No reminders while you're away, and your streak is frozen.\n"
            f"🔥 Current streak: *{current_streak} days* (safe)."
        )
        keyboard = [
            [InlineKeyboardButton("▶️ Resume Now", callback_data="set_resume")],
            [InlineKeyboardButton("🏖 Change Pause", callback_data="menu_pause")],
        ]
        return text, InlineKeyboardMarkup(keyboard)

    if workout["status"] == "rest":
        text = (
            f"🏖 *Today is a Rest Day!* ({date_str})\n\n"
            f"No workout is scheduled for today. Rest and recharge!\n"
            f"🔥 Current streak: *{current_streak} days* (preserved)."
        )
        keyboard = [[InlineKeyboardButton("🔄 Check Schedule", callback_data="refresh_today")]]
        return text, InlineKeyboardMarkup(keyboard)

    all_completed = workout["status"] == "completed"
    is_quick = bool(workout.get("quick"))
    comeback_stage = workout.get("comeback_stage") or 0
    header_status = "🎉 COMPLETED!" if all_completed else "IN PROGRESS"
    text = f"🏋️ *Today's Workout* ({date_str}) — *{header_status}*\n\n"
    if is_quick:
        text += "⏱ *Quick workout:* targets halved, still counts for your streak.\n\n"
    elif comeback_stage in COMEBACK_FACTORS:
        pct = int(COMEBACK_FACTORS[comeback_stage] * 100)
        text += f"🔄 *Comeback day:* targets at {pct}% to ease back in after your break.\n\n"

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
        feedback = workout.get("feedback")
        if feedback in FEEDBACK_LABELS:
            text += f"\n📝 You said: {FEEDBACK_LABELS[feedback]}"
        elif not is_quick:
            text += "\n\n*How did it feel?* Your answer tunes future targets."
            keyboard.insert(0, [
                InlineKeyboardButton(label, callback_data=f"fb:{key}")
                for key, label in FEEDBACK_LABELS.items()
            ])
    else:
        text += "\nTap an exercise to log reps, or tap *Complete All*:"
        keyboard.insert(0, [InlineKeyboardButton("⚡ Complete All as Scheduled", callback_data="complete_all")])
        keyboard.append([quick_toggle_button(is_quick)])

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
            InlineKeyboardButton(f"⏱ Start {item['target_reps']}s Timer",
                                 callback_data=timer_callback_data(item['target_reps'], item['exercise_name'])),
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

    # Buttons on messages sent before a pause must not log workouts on a paused day.
    if data == "complete_all" or data.startswith("ex_"):
        workout = get_or_create_daily_workout(today_str)
        if workout["status"] == "paused":
            text, markup = build_today_workout_view(workout, today_str)
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
            return

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
        schedule_alert(context.bot_data.get("scheduler"), context.bot, ALERT_TIMER,
                       secs, label, query.from_user.id)
        return

    if data == "snooze_reminder":
        schedule_alert(context.bot_data.get("scheduler"), context.bot, ALERT_SNOOZE,
                       SNOOZE_SECONDS, "Snoozed Workout Reminder", query.from_user.id)
        return

    if data in ("quick_workout", "quick_undo"):
        workout = get_or_create_daily_workout(today_str)
        change = start_quick_workout if data == "quick_workout" else undo_quick_workout
        change(workout["id"])  # no-op if it doesn't apply (e.g. already done)
        workout = get_or_create_daily_workout(today_str)
        text, markup = build_today_workout_view(workout, today_str)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data.startswith("fb:"):
        workout = get_or_create_daily_workout(today_str)
        result = record_feedback(workout["id"], data.split(":", 1)[1])
        workout = get_or_create_daily_workout(today_str)
        text, markup = build_today_workout_view(workout, today_str)
        await query.edit_message_text(f"{feedback_notice(result)}\n\n{text}",
                                      reply_markup=markup, parse_mode="Markdown")
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
