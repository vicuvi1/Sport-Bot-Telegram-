import logging
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes

import config
from database import get_setting
from services.workout_service import calculate_streaks

logger = logging.getLogger(__name__)

def is_authorized(update: Update) -> bool:
    """Verifies that the update comes from the authorized TELEGRAM_USER_ID."""
    user = update.effective_user
    if not user:
        return False
    if not config.USER_ID or config.USER_ID == 0:
        logger.warning("TELEGRAM_USER_ID is not configured in .env!")
        return False
    return user.id == config.USER_ID

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Returns the persistent main menu reply keyboard.

    NOTE: The Telegram Mini App (WebApp) button was intentionally removed.
    Telegram only accepts HTTPS web_app URLs; a http://localhost URL makes
    Telegram reject the whole message with
    ``BadRequest: Keyboard button web app url ... only https links are allowed``,
    which broke /start and /help entirely.
    """
    keyboard = [
        [KeyboardButton("🏋️ Today's Workout"), KeyboardButton("📊 Progress")],
        [KeyboardButton("📅 History"), KeyboardButton("⚙️ Settings")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    if not is_authorized(update):
        logger.warning(f"Unauthorized access attempt by user {update.effective_user.id if update.effective_user else 'unknown'}")
        return

    current_streak, best_streak = calculate_streaks()
    welcome_text = (
        "👋 *Welcome to your Workout Tracker Bot!*\n\n"
        "I'll help you track daily exercises, build consistency, and level up.\n\n"
        f"🔥 *Current Streak:* {current_streak} days\n"
        f"🏆 *Best Streak:* {best_streak} days\n\n"
        "Use the buttons below to navigate:"
    )

    await update.message.reply_text(
        welcome_text,
        reply_markup=get_main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command."""
    if not is_authorized(update):
        return

    help_text = (
        "ℹ️ *Workout Bot Help & Features*\n\n"
        "• *🏋️ Today's Workout* (`/today`)\n"
        "  View exercise targets with progress bars, mark done, tap `Complete All`, or start rest timers.\n"
        "  Busy? Tap *⏱ Short on Time* for a 50% workout that still counts for your streak.\n"
        "  After finishing, tell the bot how it felt (too easy / just right / too hard) to tune your targets.\n\n"
        "• *📊 Progress & Stats* (`/progress` or `/stats`)\n"
        "  Inspect workout completion rates and reps for Today, This Week, This Month, and All Time.\n\n"
        "• *📅 History* (`/history`)\n"
        "  Review a 4-week GitHub-style heatmap and your recent workout activity.\n\n"
        "• *🏆 Milestones & Badges* (`/badges`)\n"
        "  Track your lifetime rep clubs, streak achievements, and unlock badges.\n\n"
        "• *📤 Export Data* (`/export`)\n"
        "  Download an Excel/CSV spreadsheet of your full workout log.\n\n"
        "• *⚙️ Settings* (`/settings`)\n"
        "  Customize notification time, timezone, active exercises, targets, and automatic progression.\n\n"
        "• *💾 Database Backup* (`/backup`)\n"
        "  Generate an instant SQLite database backup and receive it right here in Telegram.\n\n"
        "• *🏖 Vacation / Sick Pause* (`/pause`)\n"
        "  Pause reminders for a few days; your streak is frozen, not broken.\n\n"
        "• *📆 Weekly Summary* (`/summary`)\n"
        "  This week vs last week, personal records, and bot health. Sent automatically Sundays at 20:00.\n\n"
        "• *🩺 Status* (`/status`)\n"
        "  Check the bot is healthy: uptime, version, button clicks received, and upcoming reminders.\n\n"
        "Rest days never break your streak! Keep going strong. 💪"
    )

    await update.message.reply_text(
        help_text,
        reply_markup=get_main_menu_keyboard(),
        parse_mode="Markdown"
    )

