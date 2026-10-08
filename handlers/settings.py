import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from database import get_setting, set_setting, create_backup
from handlers.start import is_authorized, is_owner
from handlers.workout import build_today_workout_view
from services.partner_service import get_partner
from views import HTML, card, esc, nice_date
from scheduler import check_backup
from services.workout_service import (
    get_exercises,
    get_exercise_by_id,
    add_exercise,
    update_exercise,
    delete_exercise,
    toggle_exercise_active,
    sanitize_label,
    get_active_pause,
    start_pause,
    resume_from_pause,
    get_current_date_str,
    get_or_create_daily_workout,
    MAX_PAUSE_DAYS
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

    pause = get_active_pause()
    partner = get_partner()
    on_off = lambda on: "<b>On</b>" if on else "Off"  # noqa: E731

    text = "\n".join([
        "⚙️ <b>Settings</b>",
        "",
        card([
            f"🔔 Reminders  {on_off(notif_enabled)} · {esc(workout_time)} <i>({esc(timezone)})</i>",
            f"🏋️ Exercises  <b>{active_count}</b> active of {len(exercises)}",
            f"🚀 Auto-progression  {on_off(prog_enabled)} · +{esc(prog_pct)}% after {esc(consec)} workouts",
            f"🏖 Pause  " + (f"<b>until {nice_date(pause['end_date'])}</b>" if pause else "Off"),
            f"🤝 Partner  " + (f"<b>{esc(partner['name'])}</b>" if partner else "None"),
        ]),
    ])

    keyboard = [
        [
            InlineKeyboardButton("🔔 Reminders: " + ("On" if notif_enabled else "Off"), callback_data="toggle_notif"),
            InlineKeyboardButton(f"⏰ {workout_time}", callback_data="menu_time"),
        ],
        [
            InlineKeyboardButton("🏋️ Exercises", callback_data="menu_exercises"),
            InlineKeyboardButton("🌍 Timezone", callback_data="menu_timezone"),
        ],
        [
            InlineKeyboardButton("🚀 Progression: " + ("On" if prog_enabled else "Off"), callback_data="toggle_prog"),
            InlineKeyboardButton(f"📈 +{prog_pct}%", callback_data="menu_prog_pct"),
        ],
        [
            InlineKeyboardButton("🏖 Pause", callback_data="menu_pause"),
            InlineKeyboardButton("👥 Crew", callback_data="crew_menu"),
        ],
        [InlineKeyboardButton("🧪 Fitness test", callback_data="test_menu")],
    ]
    if is_owner():
        # The partner and the whole-database backup belong to the bot owner.
        keyboard[-1].append(InlineKeyboardButton("🤝 Partner", callback_data="partner_menu"))
        keyboard.append([InlineKeyboardButton("💾 Backup", callback_data="backup_db")])

    return text, InlineKeyboardMarkup(keyboard)

PAUSE_PRESETS = [(3, "3 days"), (7, "1 week"), (14, "2 weeks")]

def build_pause_menu(notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """The vacation / sick pause menu."""
    pause = get_active_pause()
    text = f"{notice}\n\n" if notice else ""
    text += "🏖 *Vacation / Sick Pause*\n\n"
    if pause:
        text += (
            f"Paused from *{pause['start_date']}* until *{pause['end_date']}* (inclusive).\n"
            "No reminders are sent and your streak is frozen. "
            "Reminders restart automatically the day after.\n\n"
            "Extend the pause or resume now:"
        )
    else:
        text += (
            "Going on vacation or feeling sick? Pause starting today:\n"
            "• no reminders\n"
            "• your streak is frozen, not broken\n"
            "• paused days show as 🟦 instead of missed\n\n"
            "Reminders restart automatically when the pause ends."
        )

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"set_pause:{days}") for days, label in PAUSE_PRESETS],
        [InlineKeyboardButton("✏️ Custom (days or end date)", callback_data="set_pause_custom")],
    ]
    if pause:
        keyboard.append([InlineKeyboardButton("▶️ Resume Now", callback_data="set_resume")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")])
    return text, InlineKeyboardMarkup(keyboard)

def parse_pause_input(text: str, today_str: str) -> Optional[int]:
    """Turns '10' (days) or '2026-10-20' (last paused day) into a day count."""
    text = text.strip()
    if text.isdigit():
        days = int(text)
    else:
        try:
            end = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None
        days = (end - datetime.strptime(today_str, "%Y-%m-%d").date()).days + 1
    return days if 1 <= days <= MAX_PAUSE_DAYS else None

async def pause_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /pause: opens the vacation / sick pause menu."""
    if not is_authorized(update):
        return
    text, markup = build_pause_menu()
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /settings command or '⚙️ Settings' button."""
    if not is_authorized(update):
        return

    text, markup = build_settings_menu()
    if update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=HTML)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)

async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatches all configuration button callbacks."""
    if not is_authorized(update):
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_settings":
        text, markup = build_settings_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
        return

    if data == "menu_pause":
        text, markup = build_pause_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data.startswith("set_pause:"):
        pause = start_pause(int(data.split(":")[1]))
        text, markup = build_pause_menu(f"✅ Paused until *{pause['end_date']}*.")
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if data == "set_pause_custom":
        context.user_data["awaiting_setting"] = "pause_custom"
        await query.message.reply_text(
            "✏️ *Custom Pause*\n\nReply with a number of days (e.g. `10`) or the last "
            f"paused day as a date (e.g. `2026-12-31`). Maximum {MAX_PAUSE_DAYS} days.\n\n"
            "Or type `/cancel` to abort.",
            parse_mode="Markdown"
        )
        return

    if data == "set_resume":
        resumed = resume_from_pause()
        today_str = get_current_date_str()
        workout = get_or_create_daily_workout(today_str)
        intro = "▶️ <b>Welcome back!</b> Reminders are on again." if resumed else ""
        text, markup = build_today_workout_view(workout, today_str, intro=intro)
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
        return

    if data == "toggle_notif":
        current = get_setting("notifications_enabled", "1")
        new_val = "0" if current == "1" else "1"
        set_setting("notifications_enabled", new_val)
        text, markup = build_settings_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
        return

    if data == "toggle_prog":
        current = get_setting("auto_progression_enabled", "0")
        new_val = "1" if current == "0" else "0"
        set_setting("auto_progression_enabled", new_val)
        text, markup = build_settings_menu()
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
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
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
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
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
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
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
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
        if not is_owner():
            await query.message.reply_text("🔒 Only the bot owner can use this. Your crew features are in /crew.")
            return
        await query.message.reply_text("⏳ Generating database backup...")
        backup_file = create_backup()
        verdict = check_backup(backup_file)
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
                        "3. Restart the bot\n\n"
                        f"{verdict}"
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
        await update.message.reply_text(settings_text, reply_markup=markup, parse_mode=HTML)
        return True

    if setting_key == "pause_custom":
        days = parse_pause_input(text, get_current_date_str())
        if days is None:
            await update.message.reply_text(
                f"⚠️ Please send a number of days (1–{MAX_PAUSE_DAYS}) or a future date like `2026-12-31`, "
                "or type `/cancel`.",
                parse_mode="Markdown"
            )
            return True
        context.user_data.pop("awaiting_setting", None)
        pause = start_pause(days)
        p_text, markup = build_pause_menu(f"✅ Paused until *{pause['end_date']}*.")
        await update.message.reply_text(p_text, reply_markup=markup, parse_mode="Markdown")
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
        await update.message.reply_text(s_text, reply_markup=markup, parse_mode=HTML)
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
        await update.message.reply_text(s_text, reply_markup=markup, parse_mode=HTML)
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
        await update.message.reply_text(s_text, reply_markup=markup, parse_mode=HTML)
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

        # Strip characters that would break Markdown messages and button data.
        name = sanitize_label(name)
        unit = sanitize_label(unit, max_length=12) or "reps"
        if not name:
            await update.message.reply_text("⚠️ Exercise name can't be empty. Try again or type /cancel.")
            return True

        try:
            add_exercise(name, target, unit)
        except Exception as e:
            logger.warning("Could not add exercise %r: %s", name, e)
            await update.message.reply_text("⚠️ Could not add exercise. An exercise with this name may already exist.")
            return True

        context.user_data.pop("awaiting_setting", None)
        await update.message.reply_text(f"✅ Successfully added *{name}* ({target} {unit}) to your workout catalog!", parse_mode="Markdown")
        s_text, markup = build_settings_menu()
        await update.message.reply_text(s_text, reply_markup=markup, parse_mode=HTML)
        return True

    return False
