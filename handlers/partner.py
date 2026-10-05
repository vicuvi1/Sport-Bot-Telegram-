"""Accountability partner: owner menu (/partner) and the partner's side."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from handlers.start import is_authorized
from services import partner_service as ps

logger = logging.getLogger(__name__)

PARTNER_HELP = (
    "🤝 You're {owner}'s accountability partner.\n\n"
    "You'll get their progress updates here and can send them a high-five. "
    "Send /stop if you no longer want these messages."
)


def build_partner_menu(notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    partner = ps.get_partner()
    text = f"{notice}\n\n" if notice else ""
    keyboard = []

    if partner:
        text += (
            f"🤝 *Accountability Partner: {partner['name']}*\n\n"
            "They receive only what's ticked below, and can send you high-fives. "
            "They can't see or change anything else."
        )
        for option, (_, _, label) in ps.SHARE_OPTIONS.items():
            mark = "✅" if partner[option] else "⬜"
            keyboard.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"partner_toggle:{option}")])
        keyboard.append([InlineKeyboardButton("❌ Remove Partner", callback_data="partner_remove")])
    else:
        text += (
            "🤝 *Accountability Partner*\n\n"
            "Pick one friend to keep you honest. They'll get:\n"
            "• your weekly summary every Sunday\n"
            "• your monthly fitness test results\n"
            f"• optionally, a heads-up if you miss {ps.MISSED_ALERT_AFTER} planned workouts in a row\n\n"
            "They can send you 👏 high-fives, and they can't see or change anything else. "
            "Either of you can end it anytime."
        )
        expiry = ps.pending_invite_expiry()
        if expiry:
            text += f"\n\n⏳ An invite link is waiting (valid until {expiry:%d %b %H:%M} UTC)."
        keyboard.append([InlineKeyboardButton(
            "📨 Create New Invite Link" if expiry else "📨 Create Invite Link", callback_data="partner_invite")])

    keyboard.append([InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")])
    return text, InlineKeyboardMarkup(keyboard)


async def partner_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /partner (owner): the accountability partner menu."""
    if not is_authorized(update):
        return
    ps.remember_owner_name(update.effective_user.first_name)
    text, markup = build_partner_menu()
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def partner_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-side partner_* buttons (authorization is checked by the caller's wrapper)."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "partner_menu":
        text, markup = build_partner_menu()
    elif data == "partner_invite":
        # The partner is told whose partner they are, so make sure we know.
        ps.remember_owner_name(getattr(query.from_user, "first_name", None))
        code = ps.create_invite()
        username = context.bot.username
        link = f"https://t.me/{username}?start={ps.INVITE_PREFIX}{code}"
        # Plain text on purpose: the link contains "_" (Markdown italics).
        await query.message.reply_text(
            "📨 Send this link to your friend. It works once and expires in "
            f"{ps.INVITE_VALID_HOURS} hours:\n\n{link}\n\n"
            "When they open it and tap Start, I'll let you know."
        )
        text, markup = build_partner_menu()
    elif data.startswith("partner_toggle:"):
        option = data.split(":", 1)[1]
        if option in ps.SHARE_OPTIONS and ps.get_partner():
            ps.toggle_share(option)
        text, markup = build_partner_menu()
    elif data == "partner_remove":
        partner = ps.get_partner()
        if not partner:
            text, markup = build_partner_menu()
        else:
            text = f"Remove *{partner['name']}* as your accountability partner? They'll be told."
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, remove", callback_data="partner_remove_confirm"),
                InlineKeyboardButton("Cancel", callback_data="partner_menu"),
            ]])
    elif data == "partner_remove_confirm":
        old = ps.remove_partner()
        if old:
            try:
                await context.bot.send_message(
                    chat_id=old["chat_id"],
                    text=f"👋 {ps.owner_name()} ended the accountability partnership. Thanks for the support!")
            except Exception as e:
                logger.warning("Could not tell partner about removal: %s", e)
        text, markup = build_partner_menu("✅ Partner removed." if old else "")
    else:
        logger.warning("partner_callback_handler got unknown data %r", data)
        return

    await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# The partner's side. These handlers run for a user who is NOT the owner, so
# each one checks explicitly what that user is allowed to do.
# ---------------------------------------------------------------------------

async def handle_partner_start(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    """/start partner_<code>: a friend opened the invite link."""
    user = update.effective_user
    result = ps.accept_invite(code, user.id, user.first_name, config.USER_ID)
    if result == "own":
        await update.message.reply_text("This invite link is for your friend. Send it to them. 🙂")
        return
    if result != "ok":
        logger.info("Rejected partner invite from user %s: %s", user.id, result)
        await update.message.reply_text(
            "⚠️ This invite link is invalid or has expired. Ask your friend for a new one.")
        return

    partner = ps.get_partner()
    await update.message.reply_text(
        PARTNER_HELP.format(owner=ps.owner_name()) + "\n\nThanks for helping them stay on track! 💪")
    try:
        await context.bot.send_message(
            chat_id=config.USER_ID,
            text=f"🤝 *{partner['name']} is now your accountability partner!*\n\n"
                 "Change what they receive anytime with /partner.",
            parse_mode="Markdown")
    except Exception as e:
        logger.warning("Could not notify owner about new partner: %s", e)


async def partner_stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop: the partner leaves. Ignored for strangers; explained to the owner."""
    user = update.effective_user
    if is_authorized(update):
        await update.message.reply_text("/stop is for your partner. To remove them, use /partner.")
        return
    if not ps.is_partner(user.id if user else None):
        return
    old = ps.remove_partner()
    await update.message.reply_text("👋 Done. You won't get any more updates. Thanks for the support!")
    try:
        await context.bot.send_message(
            chat_id=config.USER_ID,
            text=f"👋 {old['name']} stopped being your accountability partner. "
                 "You can invite someone else with /partner.")
    except Exception as e:
        logger.warning("Could not notify owner about partner leaving: %s", e)


async def partner_cheer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The partner tapped "👏 High-Five" on an update."""
    query = update.callback_query
    user = update.effective_user
    if not ps.is_partner(user.id if user else None):
        await query.answer("This button is for the accountability partner.")
        return
    if not ps.can_cheer_today():
        await query.answer("You already sent a high-five today. 🙌")
        return
    await query.answer("👏 High-five sent!")
    partner = ps.get_partner()
    try:
        await context.bot.send_message(
            chat_id=config.USER_ID,
            text=f"👏 *{partner['name']} sent you a high-five!* Keep it up 💪",
            parse_mode="Markdown")
    except Exception as e:
        logger.warning("Could not deliver high-five: %s", e)


async def reply_to_partner_message(update: Update) -> bool:
    """If a non-owner message comes from the partner, explain their role. Returns True if handled."""
    user = update.effective_user
    if not user or not ps.is_partner(user.id) or not update.message:
        return False
    await update.message.reply_text(PARTNER_HELP.format(owner=ps.owner_name()))
    return True
