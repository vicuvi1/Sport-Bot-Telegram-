import sys
import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from telegram import Update
from telegram.error import Conflict, NetworkError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

import config
from database import init_db, create_backup
from scheduler import setup_scheduler, check_backup
from handlers.start import start_handler, help_handler, is_authorized
from handlers.workout import today_handler, workout_callback_handler, custom_amount_message_handler, build_today_workout_view
from handlers.stats import (
    stats_handler,
    stats_callback_handler,
    history_handler,
    badges_command_handler,
    export_command_handler
)
from handlers.settings import (
    settings_handler,
    settings_callback_handler,
    settings_text_input_handler,
    pause_command_handler
)
from handlers.status import status_handler, get_version
from handlers.fitness import (
    fitness_command_handler,
    fitness_test_callback_handler,
    fitness_test_text_input_handler
)
from handlers.partner import (
    partner_command_handler,
    partner_callback_handler,
    partner_cheer_handler,
    partner_stop_handler,
    reply_to_partner_message
)
from monitoring import health, install_error_reporter
from services.summary_service import build_weekly_summary
from services.workout_service import (
    get_or_create_daily_workout,
    update_workout_item,
    get_current_date_str
)

# Configure Windows console encoding for UTF-8 compatibility
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Routes incoming text messages to appropriate handlers or workflows."""
    if not is_authorized(update):
        await reply_to_partner_message(update)
        return

    text = update.message.text.strip()

    # Main menu persistent button shortcuts
    if text == "🏋️ Today's Workout":
        await today_handler(update, context)
        return
    elif text == "📊 Progress":
        await stats_handler(update, context)
        return
    elif text == "📅 History":
        await history_handler(update, context)
        return
    elif text == "⚙️ Settings":
        await settings_handler(update, context)
        return
    elif text == "💾 Backup Data":
        await backup_command_handler(update, context)
        return
    elif text in ("🏆 Badges", "🏆 Milestones & Badges"):
        await badges_command_handler(update, context)
        return
    elif text in ("📤 Export CSV", "📤 Export Data"):
        await export_command_handler(update, context)
        return

    # Check active input states (fitness test result, reps, or settings)
    if await fitness_test_text_input_handler(update, context):
        return

    if await custom_amount_message_handler(update, context):
        return

    if await settings_text_input_handler(update, context):
        return

    # Fallback response for unhandled text
    await update.message.reply_text(
        "👋 Use the buttons below or send /help to view available commands.",
        parse_mode="Markdown"
    )

async def cancel_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /cancel: leaves any "type a value" prompt.

    Commands never reach text_router (it filters them out), so without this
    handler /cancel did nothing and the prompt stayed active.
    """
    if not is_authorized(update):
        return
    was_waiting = bool(context.user_data.pop("awaiting_reps_item_id", None))
    was_waiting = bool(context.user_data.pop("awaiting_setting", None)) or was_waiting
    was_waiting = bool(context.user_data.pop("fitness_test", None)) or was_waiting
    await update.message.reply_text("❌ Cancelled." if was_waiting else "Nothing to cancel.")

async def summary_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /summary: this week's summary on demand (also sent Sundays at 20:00)."""
    if not is_authorized(update):
        return
    from scheduler import build_health_line
    text = build_weekly_summary(health_line=build_health_line())
    await update.message.reply_text(text, parse_mode="Markdown")

async def backup_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles direct /backup command."""
    if not is_authorized(update):
        return

    await update.message.reply_text("⏳ Generating database backup...")
    try:
        backup_file = create_backup()
        verdict = check_backup(backup_file)
        with open(backup_file, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=backup_file.name,
                caption=(
                    "💾 *Database Backup Completed!*\n\n"
                    f"File: `{backup_file.name}`\n\n"
                    "To restore manually:\n"
                    "1. Stop the bot\n"
                    "2. Copy this file to `data/workout.db`\n"
                    "3. Restart the bot\n\n"
                    f"{verdict}"
                ),
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        await update.message.reply_text(f"⚠️ Backup creation failed: {e}")

DUPLICATE_INSTANCE_WARNING = (
    "⚠️ *Another copy of this bot is running* with the same token.\n\n"
    "Telegram splits messages and button clicks between the copies, so some "
    "clicks will seem to do nothing. Stop the other copy (check other machines, "
    "terminals, or a second service on the server)."
)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches unhandled errors gracefully so the bot remains resilient.

    The full exception and traceback are always written to the log (and thus to
    ``journalctl -u workout-bot``) via ``exc_info``. The user only ever receives
    a short, friendly message that leaks no internal details.
    """
    err = context.error

    # Brief network blips while polling (e.g. "Bad Gateway") are retried
    # automatically by PTB; they are not worth an error report.
    if update is None and isinstance(err, NetworkError):
        logger.warning("Transient network error while polling: %s", err)
        return

    # Polling errors arrive here with update=None. Conflict means a second
    # process is calling getUpdates with this token at the same time.
    if isinstance(err, Conflict):
        logger.error("CONFLICT: another instance of this bot is polling with the same token. "
                     "Updates (including button clicks) are being split between instances.")
        if not context.bot_data.get("conflict_warned") and config.USER_ID:
            context.bot_data["conflict_warned"] = True
            try:
                await context.bot.send_message(config.USER_ID, DUPLICATE_INSTANCE_WARNING,
                                               parse_mode="Markdown")
            except Exception as send_err:
                logger.error("Failed to send duplicate-instance warning: %s", send_err)
        return

    logger.error(
        "Exception while handling an update: %s: %s",
        type(err).__name__ if err else "UnknownError",
        err,
        exc_info=err if err else True,
    )

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong while processing that request. Please try again."
            )
        except Exception as reply_err:
            logger.error("Failed to deliver error notice to user: %s", reply_err, exc_info=reply_err)

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles data sent back from the Telegram Mini App."""
    if not is_authorized(update):
        return

    try:
        import json
        raw_data = update.effective_message.web_app_data.data
        logger.info(f"Received web_app_data: {raw_data}")
        data = json.loads(raw_data)

        # Sync items sent from Mini App
        if "items" in data:
            for it in data["items"]:
                update_workout_item(it["id"], completed_reps=it.get("completed_reps"))

        today_str = get_current_date_str()
        workout = get_or_create_daily_workout(today_str)
        text, markup = build_today_workout_view(workout, today_str)

        await update.message.reply_text(
            "📱 *Workout Synchronized via Mini App!*\n\nAll exercise progress has been saved to your local database.",
            parse_mode="Markdown"
        )
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error handling web_app_data: {e}")

async def on_startup(application) -> None:
    """Initializes APScheduler on bot startup.

    NOTE: The aiohttp Telegram Mini App web server is intentionally NOT started.
    The Mini App is disabled (its UI needs a public HTTPS URL to be usable from
    Telegram), so there is no reason to bind port 8080 or run an extra service.
    Re-enable by starting ``webapp.server.start_webapp_server`` here if needed.
    """
    scheduler = setup_scheduler(application.bot)
    application.bot_data["scheduler"] = scheduler
    application.bot_data["started_at"] = datetime.now(timezone.utc)
    health.started_at = application.bot_data["started_at"]
    # Forward ERROR log lines to the user's chat (rate limited).
    install_error_reporter(application.bot, config.USER_ID, asyncio.get_running_loop())
    # Captured once at startup: after a `git pull` without a restart, git would
    # report the new commit while the old code is still running.
    application.bot_data["version"] = get_version()
    application.bot_data["callbacks_received"] = 0
    _instrument_bot(application.bot)
    await send_startup_message(application)


async def send_startup_message(application) -> None:
    """Tells the user the bot (re)started; repeated messages reveal a crash loop."""
    if not config.USER_ID:
        return
    version = application.bot_data.get("version", "unknown")
    try:
        await application.bot.send_message(
            chat_id=config.USER_ID,
            text=f"✅ Bot started (version `{version}`). Send /status for a health check.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Could not send startup message: %s", e)

async def on_shutdown(application) -> None:
    """Cleans up background web server runner upon shutdown."""
    runner = application.bot_data.get("webapp_runner")
    if runner:
        await runner.cleanup()

# ---------------------------------------------------------------------------
# Callback routing
# ---------------------------------------------------------------------------
# Every feature has an explicit, non-overlapping callback namespace. There is
# deliberately NO generic catch-all CallbackQueryHandler: an unmatched callback
# must never be silently swallowed by another feature's handler.
WORKOUT_CALLBACK_PATTERN = (
    r"^(ex_(view|done|skip|delta|enter):|refresh_today$|complete_all$"
    r"|start_timer:|snooze_reminder$|quick_workout$|quick_undo$|fb:)"
)
STATS_CALLBACK_PATTERN = r"^stats_(period:|show_|export_)"
SETTINGS_CALLBACK_PATTERN = (
    r"^(menu_|toggle_|set_|manage_ex:|edit_ex_target:|del_ex_"
    r"|add_exercise_prompt$|backup_db$)"
)

FITNESS_CALLBACK_PATTERN = r"^test_(menu$|start$|skip$|stop$|history$|toggle_pullups$|timer:)"
PARTNER_CALLBACK_PATTERN = r"^partner_(menu$|invite$|remove$|remove_confirm$|toggle:)"
CHEER_CALLBACK_PATTERN = r"^partner_cheer$"


def _log_callback(namespace: str, data: str, duration: float) -> None:
    """Logs callback name + processing time so slow buttons are easy to spot."""
    if duration >= 1.0:
        logger.warning("callback=%s namespace=%s duration=%.3fs (SLOW)", data, namespace, duration)
    else:
        logger.info("callback=%s namespace=%s duration=%.3fs", data, namespace, duration)


def _record_click(context) -> None:
    """Counts received button clicks so /status can show whether clicks arrive."""
    bot_data = context.bot_data
    bot_data["callbacks_received"] = bot_data.get("callbacks_received", 0) + 1
    bot_data["last_callback_at"] = datetime.now(timezone.utc)


def _timed(namespace: str, handler, owner_only: bool = True):
    """Wraps a callback handler to log its callback_data and elapsed time.

    With owner_only=False the handler does its own authorization (used for
    the accountability partner's high-five button).
    """
    async def wrapper(update, context):
        started = time.perf_counter()
        query = getattr(update, "callback_query", None)
        data = query.data if query else "-"
        user_id = getattr(getattr(query, "from_user", None), "id", "?")
        # Logged the instant a REAL callback arrives from Telegram.
        logger.info("LIVE CALLBACK RECEIVED data=%s user=%s namespace=%s", data, user_id, namespace)
        _record_click(context)
        try:
            if owner_only and not is_authorized(update):
                # Still answer, otherwise Telegram shows a spinner forever.
                logger.warning("Ignoring callback %s from unauthorized user %s", data, user_id)
                await query.answer()
                return
            await handler(update, context)
        finally:
            _log_callback(namespace, data, time.perf_counter() - started)
    return wrapper


async def unknown_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answers callbacks no feature handler claimed (e.g. buttons on old messages).

    Registered LAST: PTB stops at the first matching handler in a group, so this
    can never take a callback away from the workout/stats/settings handlers. It
    only guarantees that no button is ever left spinning without an answer.
    """
    query = update.callback_query
    logger.warning("UNROUTED CALLBACK data=%r — no handler matched", query.data)
    _record_click(context)
    await query.answer("This button is no longer active. Send /today to get a fresh menu.",
                       show_alert=True)


def _instrument_bot(bot) -> None:
    """Times the Telegram API calls that back button clicks, and logs each one.

    Gives per-stage latency such as ``telegram_api=answer_callback_query duration=41ms``
    and ``telegram_api=edit_message_text duration=210ms`` so the exact source of any
    perceived button lag is visible in journalctl.
    """
    def _make(name, original):
        async def wrapped(self, *args, **kwargs):
            started = time.perf_counter()
            try:
                return await original(self, *args, **kwargs)
            finally:
                logger.info("telegram_api=%s duration=%.0fms",
                            name, (time.perf_counter() - started) * 1000.0)
        wrapped._hermes_timed = True
        return wrapped

    # PTB forbids setting these on the *instance*, so patch the class instead.
    cls = type(bot)
    for name in ("answer_callback_query", "edit_message_text", "send_message"):
        original = getattr(cls, name, None)
        if original is None or getattr(original, "_hermes_timed", False):
            continue
        try:
            setattr(cls, name, _make(name, original))
        except Exception as exc:  # never let instrumentation break the bot
            logger.warning("Could not instrument bot.%s: %s", name, exc)


def register_handlers(app) -> None:
    """Registers every command, callback and message handler on the application."""
    # 1. Command handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("today", today_handler))
    app.add_handler(CommandHandler("progress", stats_handler))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("history", history_handler))
    app.add_handler(CommandHandler("settings", settings_handler))
    app.add_handler(CommandHandler("backup", backup_command_handler))
    app.add_handler(CommandHandler("badges", badges_command_handler))
    app.add_handler(CommandHandler("export", export_command_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("pause", pause_command_handler))
    app.add_handler(CommandHandler("summary", summary_command_handler))
    app.add_handler(CommandHandler("cancel", cancel_command_handler))
    app.add_handler(CommandHandler("test", fitness_command_handler))
    app.add_handler(CommandHandler("partner", partner_command_handler))
    app.add_handler(CommandHandler("stop", partner_stop_handler))

    # 2. Callback query handlers — one explicit namespace each, plus a final
    #    fallback that only answers callbacks none of them claimed.
    app.add_handler(CallbackQueryHandler(_timed("workout", workout_callback_handler),
                                         pattern=WORKOUT_CALLBACK_PATTERN))
    app.add_handler(CallbackQueryHandler(_timed("stats", stats_callback_handler),
                                         pattern=STATS_CALLBACK_PATTERN))
    app.add_handler(CallbackQueryHandler(_timed("settings", settings_callback_handler),
                                         pattern=SETTINGS_CALLBACK_PATTERN))
    app.add_handler(CallbackQueryHandler(_timed("fitness", fitness_test_callback_handler),
                                         pattern=FITNESS_CALLBACK_PATTERN))
    app.add_handler(CallbackQueryHandler(_timed("partner", partner_callback_handler),
                                         pattern=PARTNER_CALLBACK_PATTERN))
    # Pressed by the partner, not the owner: authorization happens inside.
    app.add_handler(CallbackQueryHandler(_timed("cheer", partner_cheer_handler, owner_only=False),
                                         pattern=CHEER_CALLBACK_PATTERN))
    # Must stay the last callback handler: answers anything unclaimed above.
    app.add_handler(CallbackQueryHandler(unknown_callback_handler))

    # 3. Message handlers
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # 4. Error handler
    app.add_error_handler(global_error_handler)


# Update types requested from Telegram on every poll (see main()).
POLLING_UPDATE_TYPES = Update.ALL_TYPES


# Placeholder values that mean "no real token has been configured yet".
# "your_bot_token_from_botfather" is what .env.example ships with.
PLACEHOLDER_BOT_TOKENS = frozenset({
    "your_bot_token_from_botfather",
    "PUT_YOUR_NEW_TOKEN_HERE",
})


def token_is_configured(token: str) -> bool:
    """Returns True only if the bot token is set and is not a known placeholder."""
    token = (token or "").strip()
    return bool(token) and token not in PLACEHOLDER_BOT_TOKENS


def main() -> None:
    """Initializes and runs the workout tracker bot."""
    print("==================================================")
    print("      Personal Telegram Workout Tracker Bot       ")
    print("==================================================")

    # 1. Initialize SQLite Database
    logger.info("Initializing database...")
    init_db()
    logger.info(f"Database ready at: {config.DB_PATH}")

    # CLI check mode (for CI or automated smoke verification)
    if "--check" in sys.argv:
        print("✅ Startup smoke test passed: database and config initialized successfully.")
        return

    # 2. Verify Bot Token
    token = config.BOT_TOKEN
    if not token_is_configured(token):
        print("\n" + "!" * 58)
        print("⚠️  ACTION REQUIRED: Telegram Bot Token not set!")
        print("!" * 58)
        print("Please edit the `.env` file located at:")
        print(f"  {config.BASE_DIR / '.env'}\n")
        print("Replace:")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather")
        print("With your real token obtained from @BotFather in Telegram.")
        print("!" * 58 + "\n")
        return

    # 3. Build Telegram Application with post_init and post_shutdown
    logger.info("Starting Telegram Bot Application...")
    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    # 5-8. Register all command, callback and message handlers
    register_handlers(app)

    # 9. Start Polling
    logger.info(f"Bot started! Authorized User ID: {config.USER_ID}")
    # allowed_updates MUST be explicit: when omitted, Telegram reuses whatever
    # list was last sent with this token. If anything ever polled with e.g.
    # ["message"], button clicks (callback_query) are silently never delivered,
    # while typed messages keep working.
    app.run_polling(drop_pending_updates=True, allowed_updates=POLLING_UPDATE_TYPES)

if __name__ == "__main__":
    main()
