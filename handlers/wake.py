"""/wake: the wake-up challenge menu and the "tap the number" answers."""

import logging
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import current_user_id, set_setting
from handlers.start import is_authorized
from services import wake_service as ws
from views import HTML, card

logger = logging.getLogger(__name__)


def build_wake_menu(notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    enabled = ws.wake_enabled()
    target = ws.wake_time()
    lines = [notice, ""] if notice else []
    lines.append("☀️ <b>Wake-up challenge</b>")
    lines.append(
        f"\nEvery morning at your wake time I send a quick check: tap the right number "
        f"within {ws.WAKE_GRACE_MINUTES} min to count as on time. Your crew sees the result "
        "(and can roast you if you're late 😏)."
    )
    status = f"<b>On</b> · {target}" if enabled else "Off"
    recent = ws.recent_logs(7)
    strip = "".join(ws.STATUS_ICON.get(r["status"], "▫️") for r in reversed(recent)) or "no checks yet"
    lines.append("")
    lines.append(card([
        f"⏰ Status  {status}",
        f"🔥 On-time streak  <b>{ws.wake_streak()}</b>",
        f"📅 Last 7  {strip}",
    ]))

    keyboard = [[InlineKeyboardButton("Turn off" if enabled else "✅ Turn on", callback_data="wake_toggle")]]
    presets = [InlineKeyboardButton(("• " + t + " •") if t == target else t, callback_data=f"wake_set:{t}")
               for t in ws.WAKE_PRESETS]
    keyboard += [presets[:3], presets[3:]]
    keyboard.append([InlineKeyboardButton("✏️ Custom time", callback_data="wake_custom")])
    keyboard.append([InlineKeyboardButton("👥 Crew", callback_data="crew_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def _reschedule(context) -> None:
    scheduler = context.bot_data.get("scheduler")
    if scheduler:
        from scheduler import reschedule_user_jobs
        reschedule_user_jobs(scheduler, context.bot, current_user_id())


async def wake_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    text, markup = build_wake_menu()
    await update.message.reply_text(text, reply_markup=markup, parse_mode=HTML)


async def wake_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """wake_* menu buttons and wake:<date>:<n> answers."""
    query = update.callback_query
    data = query.data

    if data.startswith("wake:"):
        _, date_str, choice = data.split(":")
        outcome = ws.answer(date_str, int(choice))
        result, log = outcome["result"], outcome["log"]
        if result == "wrong":
            await query.answer("❌ Wrong number. Are you even awake? 😴", show_alert=True)
            return
        if result == "expired":
            await query.answer("This wake-up check has expired.")
            return
        if result == "answered":
            await query.answer("Already done ✅")
            return
        await query.answer("☀️ Good morning!")
        time_str = log["answered_at"][11:16]
        if result == "on_time":
            text = (f"✅ <b>Up at {time_str}</b>, on time! 🔥 On-time streak: <b>{ws.wake_streak()}</b>\n"
                    "Your crew has been told.")
        else:
            text = (f"⏰ <b>Up at {time_str}</b>, {log['minutes_late']} min late. "
                    "Your crew has been told... brace for roasts 😅")
        await query.edit_message_text(text, parse_mode=HTML)
        from services.crew_service import deliver_events
        await deliver_events(context.bot)
        return

    await query.answer()
    if data == "wake_menu":
        text, markup = build_wake_menu()
    elif data == "wake_toggle":
        set_setting("wake_enabled", "0" if ws.wake_enabled() else "1")
        _reschedule(context)
        text, markup = build_wake_menu("✅ Wake-up challenge on." if ws.wake_enabled() else "Wake-up challenge off.")
    elif data.startswith("wake_set:"):
        set_setting("wake_time", data.split(":", 1)[1])
        set_setting("wake_enabled", "1")
        _reschedule(context)
        text, markup = build_wake_menu(f"✅ Wake-up check at {ws.wake_time()}.")
    elif data == "wake_custom":
        context.user_data["awaiting_setting"] = "wake_time"
        await query.message.reply_text("✏️ Send your wake-up time as HH:MM (24h), e.g. 06:45. Or /cancel.")
        return
    else:
        logger.warning("wake_callback_handler got unknown data %r", data)
        return
    await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)


async def wake_time_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Custom wake time typed by the user. Returns True if handled."""
    if context.user_data.get("awaiting_setting") != "wake_time":
        return False
    text = update.message.text.strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", text):
        await update.message.reply_text("⚠️ Please use HH:MM, e.g. 06:45, or /cancel.")
        return True
    h, m = text.split(":")
    context.user_data.pop("awaiting_setting", None)
    set_setting("wake_time", f"{int(h):02d}:{m}")
    set_setting("wake_enabled", "1")
    _reschedule(context)
    menu_text, markup = build_wake_menu(f"✅ Wake-up check at {ws.wake_time()}.")
    await update.message.reply_text(menu_text, reply_markup=markup, parse_mode=HTML)
    return True
