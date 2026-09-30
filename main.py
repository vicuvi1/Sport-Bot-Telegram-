import sys
import logging
from pathlib import Path
from telegram import Update
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
from scheduler import setup_scheduler
from handlers.start import start_handler, help_handler, is_authorized
from handlers.workout import today_handler, workout_callback_handler, custom_amount_message_handler, build_today_workout_view
from handlers.stats import (
    stats_handler,
    stats_callback_handler,
    history_handler,
    badges_command_handler,
    export_command_handler
)
from handlers.settings import settings_handler, settings_callback_handler, settings_text_input_handler
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

    # Check active input states (entering reps or entering settings)
    if await custom_amount_message_handler(update, context):
        return

    if await settings_text_input_handler(update, context):
        return

    # Fallback response for unhandled text
    await update.message.reply_text(
        "👋 Use the buttons below or send /help to view available commands.",
        parse_mode="Markdown"
    )

async def backup_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles direct /backup command."""
    if not is_authorized(update):
        return

    await update.message.reply_text("⏳ Generating database backup...")
    try:
        backup_file = create_backup()
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
                    "3. Restart the bot"
                ),
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        await update.message.reply_text(f"⚠️ Backup creation failed: {e}")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches unhandled errors gracefully so the bot remains resilient.

    The full exception and traceback are always written to the log (and thus to
    ``journalctl -u workout-bot``) via ``exc_info``. The user only ever receives
    a short, friendly message that leaks no internal details.
    """
    err = context.error
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

async def on_shutdown(application) -> None:
    """Cleans up background web server runner upon shutdown."""
    runner = application.bot_data.get("webapp_runner")
    if runner:
        await runner.cleanup()

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
    if not token or token == "PUT_YOUR_NEW_TOKEN_HERE":
        print("\n" + "!" * 58)
        print("⚠️  ACTION REQUIRED: Telegram Bot Token not set!")
        print("!" * 58)
        print("Please edit the `.env` file located at:")
        print(f"  {config.BASE_DIR / '.env'}\n")
        print("Replace:")
        print("  TELEGRAM_BOT_TOKEN=PUT_YOUR_NEW_TOKEN_HERE")
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

    # 5. Register Command Handlers
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

    # 6. Register Callback Query Handlers
    app.add_handler(CallbackQueryHandler(workout_callback_handler, pattern=r"^(ex_|refresh_today)"))
    app.add_handler(CallbackQueryHandler(stats_callback_handler, pattern=r"^stats_period:"))
    app.add_handler(CallbackQueryHandler(settings_callback_handler))

    # 7. Register Message Handlers
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # 8. Register Error Handler
    app.add_error_handler(global_error_handler)

    # 9. Start Polling
    logger.info(f"Bot started! Authorized User ID: {config.USER_ID}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
