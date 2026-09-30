import logging
import re
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from database import get_setting, set_setting, create_backup
from handlers.start import is_authorized
from services.workout_service import (
    get_exercises,
    get_exercise_by_id,
    add_exercise,
    update_exercise,
    delete_exercise,
    toggle_exercise_active
)

logger = logging.getLogger(__name__)

def build_settings_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Generates the main settings dashboard."""
    notif_enabled = get_setting("notifications_enabled", "1") == "1"
    workout_time = get_setting("workout_time", config.WORKOUT_TIME or "07:00")
    timezone = get_setting("timezone", config.TIMEZONE or "Europe/Chisinau")
    prog_enabled = get_setting("auto_progression_enabled", "0") == "1"
    prog_pct = get_setting("progression_percentage", "5")
    consec = get_setting("progression_consecutive_workouts", "3")

    exercises = get_exercises()
    active_count = sum(1 for e in exercises if e["is_active"] == 1)

    notif_status = "🟢 Enabled" if notif_enabled else "🔴 Disabled"
    prog_status = "🟢 Enabled" if prog_enabled else "🔴 Disabled"

    text = (
        "⚙️ *Bot Settings & Configuration*\n\n"
        f"• *Notifications:* {notif_status}\n"
        f"• *Daily Workout Time:* `{workout_time}` ({timezone})\n"
        f"• *Auto Progression:* {prog_status} (+{prog_pct}% after {consec} workouts)\n"
        f"• *Exercises:* {active_count} active / {len(exercises)} total\n\n"
        "Select an option below to customize:"
    )

    notif_btn_label = "🔔 Turn Notifications OFF" if notif_enabled else "🔔 Turn Notifications ON"
    prog_btn_label = "🚀 Turn Progression OFF" if prog_enabled else "🚀 Turn Progression ON"

    keyboard = [
        [InlineKeyboardButton(notif_btn_label, callback_data="toggle_notif")],
        [
            InlineKeyboardButton(f"⏰ Time: {workout_time}", callback_data="menu_time"),
            InlineKeyboardButton("🌍 Timezone", callback_data="menu_timezone")
        ],
        [InlineKeyboardButton(prog_btn_label, callback_data="toggle_prog")],
        [InlineKeyboardButton(f"📈 Progression Increase (+{prog_pct}%)", callback_data="menu_prog_pct")],
        [InlineKeyboardButton("🏋️ Manage Exercises & Targets", callback_data="menu_exercises")],
        [InlineKeyboardButton("💾 Backup Database", callback_data="backup_db")],
    ]

    return text, InlineKeyboardMarkup(keyboard)

async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /settings command or '⚙️ Settings' button."""
    if not is_authorized(update):
        return

    text, markup = build_settings_menu()
    if update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")

async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatches all configuration button callbacks."""
    if not is_authorized(update):
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_settings":
        text, markup = build_settings_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data == "toggle_notif":
        current = get_setting("notifications_enabled", "1")
        new_val = "0" if current == "1" else "1"
        set_setting("notifications_enabled", new_val)
        text, markup = build_settings_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data == "toggle_prog":
        current = get_setting("auto_progression_enabled", "0")
        new_val = "1" if current == "0" else "0"
        set_setting("auto_progression_enabled", new_val)
        text, markup = build_settings_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data == "menu_prog_pct":
        text = "📈 *Select Progression Increment Percentage:*\n\nWhen enabled, your target reps increase by this % after 3 consecutive completed workouts:"
        keyboard = [
            [
                InlineKeyboardButton("3%", callback_data="set_prog_pct:3"),
                InlineKeyboardButton("5% (Default)", callback_data="set_prog_pct:5"),
                InlineKeyboardButton("10%", callback_data="set_prog_pct:10"),
            ],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("set_prog_pct:"):
        pct = data.split(":")[1]
        set_setting("progression_percentage", pct)
        text, markup = build_settings_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data == "menu_time":
        current_time = get_setting("workout_time", "07:00")
        text = f"⏰ *Change Daily Workout Notification Time*\n\nCurrent time: `{current_time}`\n\nPick a quick preset or tap custom to enter your preferred time:"
        keyboard = [
            [
                InlineKeyboardButton("06:00", callback_data="set_time:06:00"),
                InlineKeyboardButton("07:00", callback_data="set_time:07:00"),
                InlineKeyboardButton("08:00", callback_data="set_time:08:00"),
                InlineKeyboardButton("09:00", callback_data="set_time:09:00"),
            ],
            [InlineKeyboardButton("✏️ Enter Custom Time", callback_data="set_time_custom")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("set_time:"):
        new_time = data.split(":")[1] + ":" + data.split(":")[2]
        set_setting("workout_time", new_time)
        # Notify scheduler if available in bot_data
        scheduler = context.bot_data.get("scheduler")
        if scheduler:
            from scheduler import reschedule_daily_job
            reschedule_daily_job(scheduler, context.bot)
        text, markup = build_settings_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data == "set_time_custom":
        context.user_data["awaiting_setting"] = "workout_time"
        await query.message.reply_text(
            "⏰ Please reply with your desired workout time in 24-hour format `HH:MM` (e.g. `06:30` or `18:00`), or type `/cancel`.",
            parse_mode="Markdown"
        )
        return

    if data == "menu_timezone":
        text = (
            f"🌍 *Timezone Configuration*\n\n"
            f"Current timezone: `{get_setting('timezone', config.TIMEZONE)}`\n\n"
            f"Select a region or tap custom to enter your tz database name (e.g. `Europe/Chisinau`, `UTC`, `America/New_York`):"
        )
        keyboard = [
            [
                InlineKeyboardButton("Europe/Chisinau", callback_data="set_tz:Europe/Chisinau"),
                InlineKeyboardButton("UTC", callback_data="set_tz:UTC"),
            ],
            [
                InlineKeyboardButton("Europe/Bucharest", callback_data="set_tz:Europe/Bucharest"),
                InlineKeyboardButton("Europe/London", callback_data="set_tz:Europe/London"),
            ],
            [InlineKeyboardButton("✏️ Custom Timezone", callback_data="set_tz_custom")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("set_tz:"):
        new_tz = data.split(":", 1)[1]
        set_setting("timezone", new_tz)
        scheduler = context.bot_data.get("scheduler")
        if scheduler:
            from scheduler import reschedule_daily_job
            reschedule_daily_job(scheduler, context.bot)
        text, markup = build_settings_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data == "set_tz_custom":
        context.user_data["awaiting_setting"] = "timezone"
        await query.message.reply_text(
            "🌍 Please reply with your valid IANA timezone name (e.g. `Europe/Chisinau`), or type `/cancel`.",
            parse_mode="Markdown"
        )
        return

    if data == "menu_exercises":
        exercises = get_exercises()
        text = "🏋️ *Manage Exercises & Targets*\n\nTap an exercise to edit its goal, toggle it on/off, or delete it:"
        keyboard = []
        for ex in exercises:
            icon = "✅" if ex["is_active"] == 1 else "⏸"
            label = f"{icon} {ex['name']} ({ex['target_reps']} {ex['unit']})"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"manage_ex:{ex['id']}")])

        keyboard.append([InlineKeyboardButton("➕ Add New Exercise", callback_data="add_exercise_prompt")])
        keyboard.append([InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("manage_ex:"):
        ex_id = int(data.split(":")[1])
        ex = get_exercise_by_id(ex_id)
        if not ex:
            return
        status_text = "Active ✅" if ex["is_active"] == 1 else "Inactive ⏸"
        toggle_label = "⏸ Deactivate" if ex["is_active"] == 1 else "▶️ Activate"
        text = (
            f"*{ex['name']}*\n\n"
            f"🎯 *Current Target:* {ex['target_reps']} {ex['unit']}\n"
            f"📌 *Status:* {status_text}\n"
        )
        keyboard = [
            [InlineKeyboardButton("🎯 Edit Target Reps", callback_data=f"edit_ex_target:{ex['id']}")],
            [InlineKeyboardButton(toggle_label, callback_data=f"toggle_ex_active:{ex['id']}")],
            [InlineKeyboardButton("🗑 Delete Exercise", callback_data=f"del_ex_confirm:{ex['id']}")],
            [InlineKeyboardButton("⬅️ Back to Exercises", callback_data="menu_exercises")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("toggle_ex_active:"):
        ex_id = int(data.split(":")[1])
        toggle_exercise_active(ex_id)
        # Refresh exercise detail view
        ex = get_exercise_by_id(ex_id)
        status_text = "Active ✅" if ex["is_active"] == 1 else "Inactive ⏸"
        toggle_label = "⏸ Deactivate" if ex["is_active"] == 1 else "▶️ Activate"
        text = (
            f"*{ex['name']}*\n\n"
            f"🎯 *Current Target:* {ex['target_reps']} {ex['unit']}\n"
            f"📌 *Status:* {status_text}\n"
        )
        keyboard = [
            [InlineKeyboardButton("🎯 Edit Target Reps", callback_data=f"edit_ex_target:{ex['id']}")],
            [InlineKeyboardButton(toggle_label, callback_data=f"toggle_ex_active:{ex['id']}")],
            [InlineKeyboardButton("🗑 Delete Exercise", callback_data=f"del_ex_confirm:{ex['id']}")],
            [InlineKeyboardButton("⬅️ Back to Exercises", callback_data="menu_exercises")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("edit_ex_target:"):
        ex_id = int(data.split(":")[1])
        ex = get_exercise_by_id(ex_id)
        if not ex:
            return
        context.user_data["awaiting_setting"] = f"target_ex:{ex_id}"
        await query.message.reply_text(
            f"🎯 Enter new target repetitions for *{ex['name']}* (current: {ex['target_reps']} {ex['unit']}):\nReply with a number, or type `/cancel`.",
            parse_mode="Markdown"
        )
        return

    if data.startswith("del_ex_confirm:"):
        ex_id = int(data.split(":")[1])
        ex = get_exercise_by_id(ex_id)
        text = f"⚠️ Are you sure you want to delete *{ex['name']}*? This will remove it from future daily workouts."
        keyboard = [
            [InlineKeyboardButton("🗑 Yes, Delete", callback_data=f"del_ex_do:{ex_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"manage_ex:{ex_id}")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("del_ex_do:"):
        ex_id = int(data.split(":")[1])
        delete_exercise(ex_id)
        # Return to exercises list
        exercises = get_exercises()
        text = "✅ Exercise deleted.\n\n🏋️ *Manage Exercises & Targets*:"
        keyboard = []
        for ex in exercises:
            icon = "✅" if ex["is_active"] == 1 else "⏸"
            label = f"{icon} {ex['name']} ({ex['target_reps']} {ex['unit']})"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"manage_ex:{ex['id']}")])
        keyboard.append([InlineKeyboardButton("➕ Add New Exercise", callback_data="add_exercise_prompt")])
        keyboard.append([InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "add_exercise_prompt":
        context.user_data["awaiting_setting"] = "add_exercise"
        await query.message.reply_text(
            "➕ *Add New Exercise*\n\nPlease reply with the exercise details formatted as:\n`Name, Target, Unit`\n\nExample:\n`Pull-ups, 15, reps`\nor\n`Jumping Jacks, 100, reps`\n\nOr type `/cancel` to abort.",
            parse_mode="Markdown"
        )
        return

    if data == "backup_db":
        await query.message.reply_text("⏳ Generating database backup...")
        backup_file = create_backup()
        try:
            with open(backup_file, "rb") as f:
                await query.message.reply_document(
                    document=f,
                    filename=backup_file.name,
                    caption=(
                        "💾 *Database Backup Completed!*\n\n"
                        f"File: `{backup_file.name}`\n\n"
                        "To restore manually:\n"
                        "1. Stop the bot\n"
                        "2. Copy this file into `data/workout.db`\n"
                        "3. Restart the bot"
                    ),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Error sending backup document: {e}")
            await query.message.reply_text(f"⚠️ Error creating backup: {e}")
        return

async def settings_text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles textual replies for changing settings or creating exercises."""
    if not is_authorized(update):
        return False

    setting_key = context.user_data.get("awaiting_setting")
    if not setting_key:
        return False

    text = update.message.text.strip()
    if text.lower() in ("/cancel", "cancel"):
        context.user_data.pop("awaiting_setting", None)
        await update.message.reply_text("❌ Configuration change cancelled.")
        settings_text, markup = build_settings_menu()
        await update.message.reply_text(settings_text, reply_markup=markup, parse_mode="Markdown")
        return True

    if setting_key == "workout_time":
        if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", text):
            await update.message.reply_text("⚠️ Invalid time format. Please use 24h format like `07:30` or `19:15`, or type `/cancel`.")
            return True

        # Normalize HH:MM
        parts = text.split(":")
        formatted_time = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        set_setting("workout_time", formatted_time)
        context.user_data.pop("awaiting_setting", None)

        scheduler = context.bot_data.get("scheduler")
        if scheduler:
            from scheduler import reschedule_daily_job
            reschedule_daily_job(scheduler, context.bot)

        await update.message.reply_text(f"✅ Notification time updated to `{formatted_time}`.", parse_mode="Markdown")
        s_text, markup = build_settings_menu()
        await update.message.reply_text(s_text, reply_markup=markup, parse_mode="Markdown")
        return True

    if setting_key == "timezone":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, Exception):
            await update.message.reply_text("⚠️ Invalid timezone name. Example valid names: `Europe/Chisinau`, `UTC`, `America/New_York`. Or type `/cancel`.")
            return True

        set_setting("timezone", text)
        context.user_data.pop("awaiting_setting", None)

        scheduler = context.bot_data.get("scheduler")
        if scheduler:
            from scheduler import reschedule_daily_job
            reschedule_daily_job(scheduler, context.bot)

        await update.message.reply_text(f"✅ Timezone updated to `{text}`.", parse_mode="Markdown")
        s_text, markup = build_settings_menu()
        await update.message.reply_text(s_text, reply_markup=markup, parse_mode="Markdown")
        return True

    if setting_key.startswith("target_ex:"):
        ex_id = int(setting_key.split(":")[1])
        try:
            new_target = int(text)
            if new_target <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("⚠️ Please enter a positive whole number for target reps, or type `/cancel`.")
            return True

        update_exercise(ex_id, target_reps=new_target)
        context.user_data.pop("awaiting_setting", None)
        ex = get_exercise_by_id(ex_id)
        await update.message.reply_text(f"✅ Target for *{ex['name']}* updated to *{new_target} {ex['unit']}*.", parse_mode="Markdown")
        s_text, markup = build_settings_menu()
        await update.message.reply_text(s_text, reply_markup=markup, parse_mode="Markdown")
        return True

    if setting_key == "add_exercise":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 2:
            await update.message.reply_text(
                "⚠️ Format must be `Name, Target` or `Name, Target, Unit` (e.g. `Pull-ups, 15, reps`). Please try again or type `/cancel`."
            )
            return True

        name = parts[0]
        try:
            target = int(parts[1])
            if target <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("⚠️ Target repetitions must be a positive number. Try again or type `/cancel`.")
            return True

        unit = parts[2] if len(parts) > 2 else "reps"

        try:
            add_exercise(name, target, unit)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Could not add exercise: {e}. An exercise with this name may already exist.")
            return True

        context.user_data.pop("awaiting_setting", None)
        await update.message.reply_text(f"✅ Successfully added *{name}* ({target} {unit}) to your workout catalog!", parse_mode="Markdown")
        s_text, markup = build_settings_menu()
        await update.message.reply_text(s_text, reply_markup=markup, parse_mode="Markdown")
        return True

    return False
