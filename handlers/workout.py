import logging
from typing import Any, Dict, Optional

from telegram import Update
from telegram.ext import ContextTypes

from handlers.start import is_authorized
from scheduler import schedule_alert, ALERT_TIMER, ALERT_SNOOZE
from services.workout_service import (
    get_or_create_daily_workout,
    update_workout_item,
    complete_all_exercises_for_workout,
    get_current_date_str,
    start_quick_workout,
    undo_quick_workout,
    record_feedback,
)
# The screens live in views.py; re-exported here for existing imports.
from views import (  # noqa: F401
    HTML,
    MAX_CALLBACK_DATA_BYTES,
    build_exercise_detail_view,
    build_today_workout_view,
    esc,
    feedback_notice,
    quick_toggle_button,
    timer_callback_data,
    unit_label,
)

logger = logging.getLogger(__name__)

SNOOZE_SECONDS = 3600


def progression_notice(prog: Optional[Dict[str, Any]]) -> str:
    """One line announcing an auto-progression step (HTML), or ''."""
    if not prog:
        return ""
    return (f"🚀 <b>Level up!</b> {esc(prog['exercise_name'])} target "
            f"{prog['old_target']} → {prog['new_target']} {esc(unit_label(prog['unit']))}")


async def today_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /today command or main menu button."""
    if not is_authorized(update):
        return

    today_str = get_current_date_str()
    workout = get_or_create_daily_workout(today_str)
    text, markup = build_today_workout_view(workout, today_str)

    if update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=HTML)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)


async def workout_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles interactive button callbacks for exercises."""
    if not is_authorized(update):
        return

    query = update.callback_query
    data = query.data

    # --- Acknowledge the click IMMEDIATELY, before any database work, so the
    # Telegram loading spinner disappears instantly. Exactly ONE answer() call
    # is made per callback (never twice). Toasts, not pop-ups: nothing to dismiss.
    if data == "complete_all":
        await query.answer("⚡ All done. Great workout!")
    elif data == "snooze_reminder":
        await query.answer("💤 OK, I'll remind you again in an hour.")
    elif data.startswith("start_timer:"):
        timer_parts = data.split(":")
        await query.answer(f"⏱ {timer_parts[1]}s timer started. I'll ping you when it's up.")
    else:
        await query.answer()

    today_str = get_current_date_str()

    async def show_today(intro: str = "") -> None:
        workout = get_or_create_daily_workout(today_str)
        text, markup = build_today_workout_view(workout, today_str, intro=intro)
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)

    # Buttons on messages sent before a pause must not log workouts on a paused day.
    if data == "complete_all" or data.startswith("ex_"):
        if get_or_create_daily_workout(today_str)["status"] == "paused":
            await show_today()
            return

    if data == "complete_all":
        workout = get_or_create_daily_workout(today_str)
        _, progressions = complete_all_exercises_for_workout(workout["id"])
        await show_today("\n".join(progression_notice(p) for p in progressions))
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
        await show_today()
        return

    if data.startswith("fb:"):
        workout = get_or_create_daily_workout(today_str)
        result = record_feedback(workout["id"], data.split(":", 1)[1])
        await show_today(feedback_notice(result))
        return

    if data == "refresh_today":
        await show_today()
        return

    if data.startswith("ex_view:"):
        item_id = int(data.split(":")[1])
        workout = get_or_create_daily_workout(today_str)
        item = next((it for it in workout["items"] if it["id"] == item_id), None)
        if not item:
            await show_today("⚠️ That exercise isn't part of today's workout anymore.")
            return
        text, markup = build_exercise_detail_view(item)
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
        return

    if data.startswith("ex_done:"):
        item_id = int(data.split(":")[1])
        workout = get_or_create_daily_workout(today_str)
        item = next((it for it in workout["items"] if it["id"] == item_id), None)
        prog = None
        if item:
            _, prog = update_workout_item(item_id, completed_reps=item["target_reps"])
        await show_today(progression_notice(prog))
        return

    if data.startswith("ex_skip:"):
        item_id = int(data.split(":")[1])
        update_workout_item(item_id, status="skipped")
        await show_today()
        return

    if data.startswith("ex_delta:"):
        parts = data.split(":")
        item_id = int(parts[1])
        delta = int(parts[2])
        updated_item, prog = update_workout_item(item_id, delta_reps=delta)
        if updated_item:
            text, markup = build_exercise_detail_view(updated_item)
            notice = progression_notice(prog)
            await query.edit_message_text(f"{notice}\n\n{text}" if notice else text,
                                          reply_markup=markup, parse_mode=HTML)
        return

    if data.startswith("ex_enter:"):
        item_id = int(data.split(":")[1])
        workout = get_or_create_daily_workout(today_str)
        item = next((it for it in workout["items"] if it["id"] == item_id), None)
        if not item:
            return

        context.user_data["awaiting_reps_item_id"] = item_id
        await query.message.reply_text(
            f"✏️ How many <b>{esc(item['exercise_name'])}</b> did you do?\n"
            f"<i>Goal: {item['target_reps']} {esc(unit_label(item['unit']))}. "
            "Reply with a number, e.g. 65, or /cancel.</i>",
            parse_mode=HTML
        )
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
    today_str = get_current_date_str()
    if text.lower() in ("/cancel", "cancel"):
        context.user_data.pop("awaiting_reps_item_id", None)
        view_text, markup = build_today_workout_view(get_or_create_daily_workout(today_str), today_str,
                                                     intro="❌ Cancelled.")
        await update.message.reply_text(view_text, reply_markup=markup, parse_mode=HTML)
        return True

    try:
        amount = int(text)
        if amount < 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("⚠️ Please send a whole number, e.g. 45, or /cancel.")
        return True

    context.user_data.pop("awaiting_reps_item_id", None)
    updated_item, prog = update_workout_item(item_id, completed_reps=amount)

    if not updated_item:
        await update.message.reply_text("⚠️ That exercise couldn't be updated.")
        return True

    # One message: confirmation (+ level-up) on top of the refreshed workout.
    intro = (f"✅ Saved <b>{amount} {esc(unit_label(updated_item['unit']))}</b> "
             f"of {esc(updated_item['exercise_name'])}.")
    notice = progression_notice(prog)
    if notice:
        intro += f"\n{notice}"
    view_text, markup = build_today_workout_view(get_or_create_daily_workout(today_str), today_str, intro=intro)
    await update.message.reply_text(view_text, reply_markup=markup, parse_mode=HTML)
    return True
