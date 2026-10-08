import logging
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes

import config
from database import bind_user, current_user_id, is_member
from services import crew_service as cs
from services import fitness_test_service as fts
from services import partner_service as ps
from services.workout_service import get_current_date_str, get_or_create_daily_workout, sanitize_label
from views import build_home, esc, nice_date

logger = logging.getLogger(__name__)

def is_authorized(update: Update) -> bool:
    """True for the owner (TELEGRAM_USER_ID) and invited crew members.

    Also binds the sender as the current user, so everything the handler
    reads or writes is theirs.
    """
    user = update.effective_user
    if not user:
        return False
    if not config.USER_ID or config.USER_ID == 0:
        logger.warning("TELEGRAM_USER_ID is not configured in .env!")
        return False
    if user.id == config.USER_ID or is_member(user.id):
        bind_user(user.id)
        return True
    return False


def is_owner() -> bool:
    """True when the current user is the bot owner (owner-only features)."""
    return current_user_id() == config.USER_ID

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
        [KeyboardButton("⚔️ Duel"), KeyboardButton("👥 Crew")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command (and crew / accountability-partner invite links)."""
    # Imported here: handlers.partner and handlers.crew import this module.
    from handlers.crew import handle_crew_join
    from handlers.partner import handle_partner_start, reply_to_partner_message
    from services.crew_service import INVITE_PREFIX as CREW_PREFIX, remember_name
    from services.partner_service import INVITE_PREFIX, remember_owner_name

    args = getattr(context, "args", None) or []
    if args and args[0].startswith(CREW_PREFIX):
        await handle_crew_join(update, context, args[0][len(CREW_PREFIX):])
        return
    if args and args[0].startswith(INVITE_PREFIX):
        await handle_partner_start(update, context, args[0][len(INVITE_PREFIX):])
        return

    if not is_authorized(update):
        if await reply_to_partner_message(update):
            return
        logger.warning(f"Unauthorized access attempt by user {update.effective_user.id if update.effective_user else 'unknown'}")
        return

    # Names used in crew and partner messages ("Victor finished...").
    remember_name(current_user_id(), update.effective_user.first_name)
    if is_owner():
        remember_owner_name(update.effective_user.first_name)

    await update.message.reply_text(
        build_home_text(update.effective_user.first_name),
        reply_markup=get_main_menu_keyboard(),
        parse_mode="HTML"
    )


def build_home_text(first_name: str) -> str:
    """The /start dashboard: today, this week, streak, and what's coming up."""
    today_str = get_current_date_str()
    workout = get_or_create_daily_workout(today_str)

    extras = []
    month = fts.current_month(today_str)
    if fts.is_month_complete(month):
        next_month = (datetime.strptime(month + "-01", "%Y-%m-%d") + timedelta(days=32)).strftime("%Y-%m-01")
        extras.append(f"🧪 Next fitness test: {nice_date(next_month)}")
    else:
        extras.append("🧪 This month's fitness test is waiting: /test")
    # Crew mates' day at a glance.
    for other in cs.others():
        snap = cs.day_snapshot(other["user_id"], today_str)
        today = "✅ done" if snap["status"] == "completed" else f"{snap['done']}/{snap['total']}"
        extras.append(f"⚔️ <b>{esc(snap['name'])}</b> · today {today} · 🔥 {snap['streak']}  (/duel)")
    if is_owner():
        partner = ps.get_partner()
        extras.append(f"🤝 Partner: <b>{esc(partner['name'])}</b>" if partner else "🤝 No partner yet: /partner")

    return build_home(sanitize_label(first_name or "", max_length=30), workout, today_str, extras)

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
        "• *🧪 Monthly Fitness Test* (`/test`)\n"
        "  10 minutes of max-effort tests once a month, to see your real progress month by month.\n\n"
        "• *🤝 Accountability Partner* (`/partner`)\n"
        "  Invite a friend to get your weekly summary and test results and send you high-fives.\n\n"
        "• *👥 Crew & ⚔️ Duel* (`/crew`, `/duel`)\n"
        "  Train with your brother: see each other's progress, compete, roast 😈 or hype 💪.\n\n"
        "• *☀️ Wake-up challenge* (`/wake`)\n"
        "  Prove you got up on time. Your crew sees it.\n\n"
        "• *🏆 Points & 🎯 Challenges* (`/points`, `/challenge`)\n"
        "  Weekly points, Sunday winner, a forfeit for the loser, 1-on-1 challenges.\n\n"
        "• *🩺 Status* (`/status`)\n"
        "  Check the bot is healthy: uptime, version, button clicks received, and upcoming reminders.\n\n"
        "Rest days never break your streak! Keep going strong. 💪"
    )

    await update.message.reply_text(
        help_text,
        reply_markup=get_main_menu_keyboard(),
        parse_mode="Markdown"
    )

