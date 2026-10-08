"""/crew and /duel: training together with a brother (or friends)."""

import logging
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from database import bind_user, current_user_id, get_users, is_member, remove_user, set_setting
from handlers.start import get_main_menu_keyboard, is_authorized, is_owner
from services import compete_service as cmp
from services import crew_service as cs
from services import wake_service as ws
from services.workout_service import get_current_date_str
from views import HTML, bar, card, esc, nice_date

logger = logging.getLogger(__name__)

ROAST_LEVEL_LABELS = {"savage": "😈 Savage", "friendly": "🙂 Friendly", "off": "🔇 Off"}


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def _member_line(snap) -> str:
    if snap["status"] == "completed":
        today = "✅ done"
    elif snap["status"] == "rest":
        today = "🌿 rest day"
    elif snap["status"] == "paused":
        today = "🏖 paused"
    else:
        today = f"{snap['done']}/{snap['total']}"
    crown = "👑 " if snap["user_id"] == config.USER_ID else "💪 "
    return f"{crown}<b>{esc(snap['name'])}</b> · today {today} · 🔥 {snap['streak']}"


def _versus_buttons() -> List[List[InlineKeyboardButton]]:
    rows = []
    for other in cs.others():
        name = other["name"] or "crew mate"
        rows.append([
            InlineKeyboardButton(f"😈 Roast {name}", callback_data=f"crew_roast:{other['user_id']}"),
            InlineKeyboardButton(f"💪 Hype {name}", callback_data=f"crew_hype:{other['user_id']}"),
        ])
    return rows


def build_crew_menu(notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    members = get_users()
    me = current_user_id()
    lines = [notice, ""] if notice else []
    lines.append("👥 <b>Your crew</b>")

    keyboard: List[List[InlineKeyboardButton]] = []
    if len(members) < 2:
        lines.append(
            "\nIt's just you so far. Invite your brother (or a friend) and you'll:\n"
            "• see each other's workouts as they happen\n"
            "• compete on the /duel scoreboard\n"
            "• roast 😈 or hype 💪 each other with one tap"
        )
    else:
        snaps = [cs.day_snapshot(m["user_id"]) for m in members]
        lines.append(card([_member_line(s) for s in snaps]))
        lines.append(f"🤜🤛 Crew streak: <b>{cs.crew_streak()}</b> days (everyone trained)")
        keyboard.append([InlineKeyboardButton("⚔️ Duel", callback_data="crew_duel"),
                         InlineKeyboardButton("🏆 Points", callback_data="cmp_pts")])
        keyboard.append([InlineKeyboardButton("🎯 Challenge", callback_data="cmp_chal")])
        keyboard.extend(_versus_buttons())

    keyboard.append([InlineKeyboardButton("☀️ Wake-up challenge", callback_data="wake_menu")])
    feed_on = cs.user_setting(me, "crew_feed", "1") == "1"
    level = cs.roast_level(me)
    keyboard.append([
        InlineKeyboardButton("📰 Feed: " + ("On" if feed_on else "Off"), callback_data="crew_feed"),
        InlineKeyboardButton("Roasts I get: " + ROAST_LEVEL_LABELS[level], callback_data="crew_roastlvl"),
    ])
    if is_owner():
        row = [InlineKeyboardButton("📨 Invite", callback_data="crew_invite")]
        if len(members) > 1:
            row.append(InlineKeyboardButton("⚙️ Manage", callback_data="crew_manage"))
        keyboard.append(row)
    else:
        keyboard.append([InlineKeyboardButton("🚪 Leave crew", callback_data="crew_leave")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def build_duel() -> tuple[str, InlineKeyboardMarkup]:
    today = get_current_date_str()
    snaps = sorted((cs.day_snapshot(m["user_id"]) for m in get_users()),
                   key=lambda s: (cs.progress_ratio(s), s["reps"]), reverse=True)
    lines = [f"⚔️ <b>Duel</b> · {nice_date(today)}", ""]
    blocks = []
    for rank, s in enumerate(snaps):
        medal = "🥇" if rank == 0 and cs.progress_ratio(s) > 0 else "▫️"
        status = "✅" if s["status"] == "completed" else ("🌿 rest" if s["status"] == "rest" else
                                                          "🏖 paused" if s["status"] == "paused" else "")
        wake = ws.today_label(s["user_id"])
        blocks.append(
            f"{medal} <b>{esc(s['name'])}</b>  {s['done']}/{s['total']} {status}  🔥 {s['streak']}\n"
            f"{bar(s['done'], s['total'] or 1)}  {s['reps']} reps" + (f"\n{wake}" if wake else "")
        )
    lines.append(card(["\n\n".join(blocks)]))
    lines.append(f"\n🤜🤛 Crew streak: <b>{cs.crew_streak()}</b> days")

    if len(snaps) > 1:
        leader, last = snaps[0], snaps[-1]
        if cs.progress_ratio(leader) == cs.progress_ratio(last) and leader["reps"] == last["reps"]:
            lines.append("Dead even. Who blinks first? 👀")
        elif last["status"] == "pending":
            lines.append(f"<b>{esc(leader['name'])}</b> is ahead. {esc(last['name'])}, your move!")
        else:
            lines.append(f"<b>{esc(leader['name'])}</b> leads today.")

    table = cmp.standings(today)
    lines.append("🏆 This week: " + " · ".join(f"{esc(r['name'])} <b>{r['points']}</b>" for r in table))
    for f in cmp.open_forfeits():
        if f["task"]:
            lines.append(f"😬 {esc(cs.display_name(f['loser']))} still owes: {esc(f['task'])}")

    keyboard = _versus_buttons()
    keyboard.append([
        InlineKeyboardButton("🎯 Challenge", callback_data="cmp_chal"),
        InlineKeyboardButton("🏆 Points", callback_data="cmp_pts"),
    ])
    keyboard.append([
        InlineKeyboardButton("↻ Refresh", callback_data="crew_duel"),
        InlineKeyboardButton("👥 Crew", callback_data="crew_menu"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def build_manage_menu() -> tuple[str, InlineKeyboardMarkup]:
    keyboard = [[InlineKeyboardButton(f"❌ Remove {m['name'] or m['user_id']}",
                                      callback_data=f"crew_remove:{m['user_id']}")]
                for m in cs.others()]
    keyboard.append([InlineKeyboardButton("← Back", callback_data="crew_menu")])
    return ("⚙️ <b>Manage crew</b>\n\nRemoving someone deletes their workouts from this bot. "
            "They're told."), InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def crew_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    cs.remember_name(current_user_id(), update.effective_user.first_name)
    text, markup = build_crew_menu()
    await update.message.reply_text(text, reply_markup=markup, parse_mode=HTML)


async def duel_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    if len(get_users()) < 2:
        text, markup = build_crew_menu("⚔️ A duel needs two. Invite someone first!")
    else:
        text, markup = build_duel()
    await update.message.reply_text(text, reply_markup=markup, parse_mode=HTML)


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

async def _roast(update: Update, context: ContextTypes.DEFAULT_TYPE, target: int) -> None:
    query = update.callback_query
    me = current_user_id()
    if target == me or not is_member(target):
        await query.answer("Roasting yourself? Bold move 😅")
        return
    target_name = cs.display_name(target)
    level = cs.roast_level(target)
    if level == "off":
        await query.answer(f"{target_name} turned roasts off 🐔", show_alert=True)
        return

    mine, theirs = cs.day_snapshot(me), cs.day_snapshot(target)
    if theirs["status"] == "completed" and mine["status"] != "completed":
        # Backfire: you can't roast someone who's done when you aren't.
        await query.answer(f"😅 {target_name} is already done today. You're at {mine['done']}/{mine['total']}... "
                           "who's the loser now?", show_alert=True)
        return
    if not cs.can_send_roast(context.bot_data, me, target, get_current_date_str()):
        await query.answer(f"Easy 😅 That's {cs.ROASTS_PER_DAY} roasts today.", show_alert=True)
        return

    ahead = cs.progress_ratio(mine) > cs.progress_ratio(theirs)
    line = cs.pick_roast(level, me=target_name, them=mine["name"], their_reps=mine["reps"], sender_ahead=ahead)
    sent = await cs.send_html(
        context.bot, target, f"😈 <b>Roast from {esc(mine['name'])}</b>\n\n{esc(line)}",
        [[InlineKeyboardButton(f"😈 Roast {mine['name']} back", callback_data=f"crew_roast:{me}")],
         [InlineKeyboardButton("🏋️ My workout", callback_data="refresh_today")]])
    await query.answer(f"😈 Roast delivered to {target_name}" if sent else "Couldn't reach them right now.")


async def _hype(update: Update, context: ContextTypes.DEFAULT_TYPE, target: int) -> None:
    query = update.callback_query
    me = current_user_id()
    if target == me or not is_member(target):
        await query.answer("Self-hype noted 😄")
        return
    my_name = cs.display_name(me)
    line = cs.pick_line(cs.HYPES, me=cs.display_name(target), them=my_name)
    sent = await cs.send_html(
        context.bot, target, esc(line),
        [[InlineKeyboardButton(f"💪 Hype {my_name} back", callback_data=f"crew_hype:{me}")]])
    await query.answer(f"💪 Hype sent to {cs.display_name(target)}" if sent else "Couldn't reach them right now.")


async def crew_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """crew_* buttons (the caller's wrapper has checked the user is in the crew)."""
    query = update.callback_query
    data = query.data

    # Roast / hype answer with their own toast.
    if data.startswith("crew_roast:"):
        await _roast(update, context, int(data.split(":")[1]))
        return
    if data.startswith("crew_hype:"):
        await _hype(update, context, int(data.split(":")[1]))
        return

    await query.answer()
    me = current_user_id()
    text, markup = None, None

    if data == "crew_menu":
        text, markup = build_crew_menu()
    elif data == "crew_duel":
        text, markup = build_duel() if len(get_users()) > 1 else build_crew_menu("⚔️ A duel needs two.")
    elif data == "crew_feed":
        set_setting("crew_feed", "0" if cs.user_setting(me, "crew_feed", "1") == "1" else "1")
        text, markup = build_crew_menu()
    elif data == "crew_roastlvl":
        levels = cs.ROAST_LEVELS
        set_setting("roast_level", levels[(levels.index(cs.roast_level(me)) + 1) % len(levels)])
        text, markup = build_crew_menu()
    elif data == "crew_invite" and is_owner():
        code = cs.create_invite()
        link = f"https://t.me/{context.bot.username}?start={cs.INVITE_PREFIX}{code}"
        # Plain text: the link contains "_" and must stay clickable.
        await query.message.reply_text(
            "📨 Send this link to your brother (or a friend). It works once and expires in "
            f"{cs.INVITE_VALID_HOURS} hours:\n\n{link}\n\n"
            "They get their own workouts, reminders and streak, and you'll see each other's progress."
        )
        text, markup = build_crew_menu()
    elif data == "crew_manage" and is_owner():
        text, markup = build_manage_menu()
    elif data.startswith("crew_remove:") and is_owner():
        target = int(data.split(":")[1])
        text = f"Remove <b>{esc(cs.display_name(target))}</b> and delete their workouts from this bot?"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, remove", callback_data=f"crew_rmyes:{target}"),
            InlineKeyboardButton("Cancel", callback_data="crew_manage"),
        ]])
    elif data.startswith("crew_rmyes:") and is_owner():
        target = int(data.split(":")[1])
        await _remove_member(context, target, removed_by_owner=True)
        text, markup = build_crew_menu("✅ Removed.")
    elif data == "crew_leave" and not is_owner():
        text = "Leave the crew? Your workouts in this bot will be deleted."
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, leave", callback_data="crew_leaveyes"),
            InlineKeyboardButton("Cancel", callback_data="crew_menu"),
        ]])
    elif data == "crew_leaveyes" and not is_owner():
        await _remove_member(context, me, removed_by_owner=False)
        await query.edit_message_text("👋 You left the crew. Send /start if you ever want back in (you'll need a new invite).")
        return
    else:
        logger.warning("crew_callback_handler: unknown or not allowed %r for %s", data, me)
        return

    await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)


async def _remove_member(context, user_id: int, removed_by_owner: bool) -> None:
    name = cs.display_name(user_id)
    remaining = [m["user_id"] for m in cs.others(user_id)]
    scheduler = context.bot_data.get("scheduler")
    if scheduler:
        from scheduler import remove_user_jobs
        remove_user_jobs(scheduler, user_id)
    remove_user(user_id)
    if removed_by_owner:
        await cs.send_html(context.bot, user_id, "👋 You were removed from the workout crew.")
    for uid in remaining:
        if uid != current_user_id():
            await cs.send_html(context.bot, uid, f"👋 <b>{esc(name)}</b> left the crew.")


# ---------------------------------------------------------------------------
# Joining
# ---------------------------------------------------------------------------

async def handle_crew_join(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    """/start crew_<code>: someone opened the invite link."""
    user = update.effective_user
    result = cs.accept_invite(code, user.id, user.first_name)
    if result == "member":
        await update.message.reply_text("You're already in the crew 💪 Send /start for your dashboard.")
        return
    if result != "ok":
        reason = {"full": f"the crew is full ({cs.CREW_MAX} people)",
                  "expired": "this invite has expired"}.get(result, "this invite link isn't valid")
        logger.info("Rejected crew invite from %s: %s", user.id, result)
        await update.message.reply_text(f"⚠️ Sorry, {reason}. Ask for a new invite link.")
        return

    bind_user(user.id)
    scheduler = context.bot_data.get("scheduler")
    if scheduler:
        from scheduler import reschedule_user_jobs
        reschedule_user_jobs(scheduler, context.bot, user.id)

    crew_names = ", ".join(esc(m["name"] or "?") for m in cs.others(user.id))
    await update.message.reply_text(
        f"🎉 <b>Welcome to the crew, {esc(cs.display_name(user.id))}!</b>\n\n"
        f"You're training with {crew_names}. You have your own workouts, reminders and streak "
        "(default: 4 exercises, reminder at 07:00, change it in ⚙️ Settings).\n\n"
        "• /today: your workout\n"
        "• /duel: who's ahead today\n"
        "• /crew: roast 😈 or hype 💪 each other\n\n"
        "Roasts you receive are set to <b>savage</b>. Change it in /crew if it gets too spicy 🌶",
        reply_markup=get_main_menu_keyboard(), parse_mode=HTML)
    for other in cs.others(user.id):
        await cs.send_html(context.bot, other["user_id"],
                           f"🎉 <b>{esc(cs.display_name(user.id))}</b> joined the crew! /duel to see who's ahead.")
