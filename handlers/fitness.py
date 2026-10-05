"""/test: the guided monthly fitness test."""

import logging
from typing import Any, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_setting, set_setting
from handlers.start import is_authorized
from scheduler import schedule_alert, ALERT_TIMER
from services import partner_service
from services.fitness_test_service import (
    TESTS_BY_KEY,
    active_tests,
    current_month,
    format_history,
    format_month_report,
    get_month_results,
    is_month_complete,
    month_label,
    next_test_index,
    parse_result,
    save_result,
)

logger = logging.getLogger(__name__)

STATE_KEY = "fitness_test"  # context.user_data: {"month": "YYYY-MM", "index": int}


def build_test_menu() -> tuple[str, InlineKeyboardMarkup]:
    month = current_month()
    done = is_month_complete(month)
    results = get_month_results(month)
    tests = active_tests()

    text = (
        "🧪 *Monthly Fitness Test*\n\n"
        "Once a month, about 10 minutes: a few max-effort tests that show whether "
        "your body is really getting stronger.\n\n"
        "Tests: " + ", ".join(t["name"] for t in tests) + ".\n\n"
    )
    if done:
        text += f"✅ Done for {month_label(month)}.\n\n" + format_month_report(month)
    elif results:
        text += f"⏸ Started for {month_label(month)}: {len(results)}/{len(tests)} done. Tap Continue."
    else:
        text += f"Not done yet for {month_label(month)}. Warm up for a few minutes first."

    start_label = "🔁 Retake This Month" if done else ("▶️ Continue Test" if results else "▶️ Start Test")
    pullups_on = get_setting("test_pullups", "0") == "1"
    keyboard = [
        [InlineKeyboardButton(start_label, callback_data="test_start")],
        [InlineKeyboardButton("📈 My Progress", callback_data="test_history")],
        [InlineKeyboardButton(
            "Pull-up test: ON (tap to remove)" if pullups_on else "Pull-up test: OFF (tap to add; needs a bar)",
            callback_data="test_toggle_pullups")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def build_test_card(index: int) -> tuple[str, InlineKeyboardMarkup]:
    tests = active_tests()
    test = tests[index]
    text = (
        f"🧪 *Test {index + 1}/{len(tests)}: {test['name']}*\n\n"
        f"{test['how']}\n\n"
        f"👉 Reply with your result in *{test['unit']}*"
        + (" (e.g. `95` or `1:35`)." if test["unit"] == "sec" else " (e.g. `25`).")
    )
    keyboard = []
    if test["timer"]:
        keyboard.append([InlineKeyboardButton(
            f"⏱ Start {test['timer'] // 60}-min Timer", callback_data=f"test_timer:{test['key']}")])
    keyboard.append([
        InlineKeyboardButton("⏭ Skip This Test", callback_data="test_skip"),
        InlineKeyboardButton("✖ Stop", callback_data="test_stop"),
    ])
    return text, InlineKeyboardMarkup(keyboard)


def _begin(context) -> int:
    """Starts (or resumes) this month's test. Returns the first test index."""
    month = current_month()
    index = next_test_index(month)
    # Typed numbers must go to the test, not to an older prompt.
    context.user_data.pop("awaiting_reps_item_id", None)
    context.user_data.pop("awaiting_setting", None)
    context.user_data[STATE_KEY] = {"month": month, "index": index}
    return index


def _current_test(context) -> Optional[Dict[str, Any]]:
    state = context.user_data.get(STATE_KEY)
    tests = active_tests()
    if not state or state["index"] >= len(tests):
        return None
    return tests[state["index"]]


async def _advance(context) -> tuple[str, InlineKeyboardMarkup, bool]:
    """Moves to the next test. Returns (text, markup, finished)."""
    state = context.user_data[STATE_KEY]
    state["index"] += 1
    if state["index"] < len(active_tests()):
        text, markup = build_test_card(state["index"])
        return text, markup, False

    context.user_data.pop(STATE_KEY, None)
    month = state["month"]
    report = format_month_report(month)
    if partner_service.shares("tests"):
        sent = await partner_service.send_to_partner(
            context.bot,
            f"🧪 *{partner_service.owner_name()} just finished their monthly fitness test!*\n\n{report}")
        if sent:
            report += f"\n\n🤝 Shared with {partner_service.get_partner()['name']}."
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("📈 My Progress", callback_data="test_history")]])
    return f"🎉 *Test complete!*\n\n{report}", markup, True


async def fitness_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /test: the fitness test menu."""
    if not is_authorized(update):
        return
    text, markup = build_test_menu()
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def fitness_test_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles test_* buttons."""
    query = update.callback_query
    data = query.data

    if data.startswith("test_timer:"):
        test = TESTS_BY_KEY.get(data.split(":", 1)[1])
        await query.answer("⏱ Timer started. Go! I'll ping you when time is up." if test else None)
        if test and test["timer"]:
            schedule_alert(context.bot_data.get("scheduler"), context.bot, ALERT_TIMER,
                           test["timer"], f"{test['name']} test", query.from_user.id)
        return

    await query.answer()

    if data == "test_menu":
        text, markup = build_test_menu()
    elif data == "test_toggle_pullups":
        set_setting("test_pullups", "0" if get_setting("test_pullups", "0") == "1" else "1")
        text, markup = build_test_menu()
    elif data == "test_history":
        text = format_history()
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="test_menu")]])
    elif data == "test_start":
        text, markup = build_test_card(_begin(context))
    elif data == "test_skip":
        test = _current_test(context)
        if not test:
            text, markup = build_test_menu()
        else:
            save_result(context.user_data[STATE_KEY]["month"], test["key"], None)
            text, markup, _ = await _advance(context)
    elif data == "test_stop":
        context.user_data.pop(STATE_KEY, None)
        text = "✖ Test stopped. Results so far are saved; /test lets you continue anytime this month."
        markup = None
    else:
        logger.warning("fitness_test_callback_handler got unknown data %r", data)
        return

    await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")


async def fitness_test_text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles a typed test result. Returns True if the message was consumed."""
    if not is_authorized(update):
        return False
    test = _current_test(context)
    if not test:
        return False

    value = parse_result(update.message.text, test["unit"])
    if value is None:
        example = "`95` or `1:35`" if test["unit"] == "sec" else "`25`"
        await update.message.reply_text(
            f"⚠️ Please reply with a number of {test['unit']}, e.g. {example}. "
            "Or tap Skip / Stop on the test message.",
            parse_mode="Markdown"
        )
        return True

    save_result(context.user_data[STATE_KEY]["month"], test["key"], value)
    text, markup, _ = await _advance(context)
    await update.message.reply_text(f"✅ Saved: {value} {test['unit']}.\n\n{text}",
                                    reply_markup=markup, parse_mode="Markdown")
    return True
