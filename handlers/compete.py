"""/challenge and /points: 1-on-1 challenges, weekly points and forfeits."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import as_user, current_user_id, get_users, is_member
from handlers.start import is_authorized
from services import compete_service as cmp
from services import crew_service as cs
from services.summary_service import week_bounds
from services.workout_service import get_current_date_str, get_exercises
from views import HTML, card, esc, nice_date

logger = logging.getLogger(__name__)

PART_LABELS = [("workout", "🏋️ Workouts"), ("full", "🎯 Full targets"), ("wake", "☀️ On-time wake-ups"),
               ("challenge", "⚔️ Challenges"), ("test_pr", "🧪 Test PRs"), ("forfeit", "😬 Forfeits done"),
               ("proof", "🎥 Proof clips"), ("bets", "🎲 Bets")]


def build_points() -> tuple[str, InlineKeyboardMarkup]:
    today = get_current_date_str()
    start, end = week_bounds(today)
    table = cmp.standings(today)
    medals = ["👑", "🥈", "🥉"] + ["▫️"] * 10
    lines = [f"🏆 <b>Points this week</b> · {nice_date(start)} – {nice_date(end)}", "",
             card([f"{medals[i]} {esc(r['name'])}  <b>{r['points']}</b>" for i, r in enumerate(table)])]
    mine = next((r["parts"] for r in table if r["user_id"] == current_user_id()), None)
    if mine:
        lines += ["", "<b>Your points</b>",
                  card([f"{label}  {mine[key]:+d}" for key, label in PART_LABELS if mine.get(key)] or ["Nothing yet this week"])]
    owed = cmp.open_forfeits()
    if owed:
        lines.append("")
        for f in owed:
            task = esc(f["task"]) if f["task"] else "(being chosen)"
            lines.append(f"😬 {esc(cs.display_name(f['loser']))} owes: <b>{task}</b>")
    lines.append(f"\n<i>{cmp.POINTS_LEGEND}</i>\nResults every Sunday at 20:30. The loser gets a forfeit 😈")
    keyboard = [[InlineKeyboardButton("🎯 Challenge", callback_data="cmp_chal"),
                 InlineKeyboardButton("⚔️ Duel", callback_data="crew_duel")]]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def challenge_kinds_for(target: int):
    """Challenges that make sense for the target's exercises."""
    with as_user(target):
        names = " ".join(e["name"].lower() for e in get_exercises(active_only=True))
    return [(k, spec) for k, spec in cmp.CHALLENGES.items() if "match" not in spec or spec["match"] in names]


def build_kind_menu(target: int) -> tuple[str, InlineKeyboardMarkup]:
    keyboard = [[InlineKeyboardButton(spec["label"], callback_data=f"cmp_kind:{target}:{k}")]
                for k, spec in challenge_kinds_for(target)]
    keyboard.append([InlineKeyboardButton("← Back", callback_data="crew_menu")])
    return (f"🎯 <b>Challenge {esc(cs.display_name(target))}</b>\n\nPick today's challenge. "
            f"If they do it: +{cmp.POINTS['challenge']} for them. If they don't (or chicken out), the points are yours."
            ), InlineKeyboardMarkup(keyboard)


def build_target_menu() -> tuple[str, InlineKeyboardMarkup]:
    others = cs.others()
    if len(others) == 1:
        return build_kind_menu(others[0]["user_id"])
    keyboard = [[InlineKeyboardButton(o["name"] or str(o["user_id"]), callback_data=f"cmp_to:{o['user_id']}")]
                for o in others]
    return "🎯 <b>Who do you challenge?</b>", InlineKeyboardMarkup(keyboard)


async def points_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    text, markup = build_points()
    await update.message.reply_text(text, reply_markup=markup, parse_mode=HTML)


async def challenge_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    if len(get_users()) < 2:
        await update.message.reply_text("🎯 Challenges need a crew. Invite someone from /crew first!")
        return
    text, markup = build_target_menu()
    await update.message.reply_text(text, reply_markup=markup, parse_mode=HTML)


async def compete_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cmp_* buttons (the caller's wrapper has checked the user is in the crew)."""
    query = update.callback_query
    data = query.data
    me = current_user_id()
    my_name = esc(cs.display_name(me))
    parts = data.split(":")

    if data == "cmp_pts":
        await query.answer()
        text, markup = build_points()
    elif data == "cmp_chal":
        await query.answer()
        if len(get_users()) < 2:
            return
        text, markup = build_target_menu()
    elif data.startswith("cmp_to:"):
        await query.answer()
        text, markup = build_kind_menu(int(parts[1]))
    elif data.startswith("cmp_kind:"):
        target, kind = int(parts[1]), parts[2]
        if not is_member(target):
            await query.answer("They're not in the crew anymore.")
            return
        challenge_id = cmp.offer_challenge(me, target, kind)
        if not challenge_id:
            await query.answer("You already have an open challenge with them today.", show_alert=True)
            return
        await query.answer("🎯 Challenge sent!")
        label = cmp.CHALLENGES[kind]["label"]
        await cs.send_html(
            context.bot, target,
            f"🎯 <b>{my_name} challenges you:</b> {label}!\n\n"
            f"Do it today: +{cmp.POINTS['challenge']} pts for you. Fail: they get the points. "
            f"Chicken out: +{cmp.POINTS['chicken']} for them 🐔",
            [[InlineKeyboardButton("✅ Accept", callback_data=f"cmp_acc:{challenge_id}"),
              InlineKeyboardButton("🐔 Chicken out", callback_data=f"cmp_dec:{challenge_id}")]])
        text = f"🎯 Challenge sent to <b>{esc(cs.display_name(target))}</b>: {label}. Waiting for an answer..."
        markup = None
    elif data.startswith(("cmp_acc:", "cmp_dec:")):
        accept = data.startswith("cmp_acc:")
        challenge = cmp.respond_challenge(int(parts[1]), me, accept)
        if not challenge:
            await query.answer("This challenge isn't open anymore.")
            return
        await query.answer("Game on! 💪" if accept else "🐔 Bawk bawk")
        label = cmp.CHALLENGES[challenge["kind"]]["label"]
        if accept:
            text = f"✅ Accepted: <b>{label}</b>. Log it before 23:00!"
            await cs.send_html(context.bot, challenge["challenger"], f"⚔️ <b>{my_name}</b> accepted: {label}. Game on!")
        else:
            text = f"🐔 You chickened out of: {label}."
            await cs.send_html(context.bot, challenge["challenger"],
                               f"🐔 <b>{my_name}</b> chickened out of {label}. +{cmp.POINTS['chicken']} pts for you!",
                               [[InlineKeyboardButton(f"😈 Roast {cs.display_name(me)}",
                                                      callback_data=f"crew_roast:{me}")]])
        markup = None
    elif data.startswith("cmp_fpick:"):
        index = None if parts[2] == "r" else int(parts[2])
        forfeit = cmp.pick_forfeit(int(parts[1]), me, index)
        if not forfeit:
            await query.answer("Already picked.")
            return
        await query.answer("😈 Forfeit chosen")
        await cmp.announce_forfeit(context.bot, forfeit)
        text = f"😈 {esc(cs.display_name(forfeit['loser']))} owes you: <b>{esc(forfeit['task'])}</b>"
        markup = None
    elif data.startswith("cmp_fdone:"):
        forfeit = cmp.complete_forfeit(int(parts[1]), me)
        if not forfeit:
            await query.answer("Nothing to mark here.")
            return
        await query.answer(f"✅ +{cmp.POINTS['forfeit']} pts")
        text = f"✅ Forfeit done: <b>{esc(forfeit['task'])}</b>. Respect. +{cmp.POINTS['forfeit']} pts"
        markup = None
        await cs.send_html(context.bot, forfeit["winner"],
                           f"✅ <b>{my_name}</b> completed the forfeit: {esc(forfeit['task'])}")
    else:
        await query.answer()
        logger.warning("compete_callback_handler got unknown data %r", data)
        return

    await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
