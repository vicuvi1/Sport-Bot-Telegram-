"""The System in the chat: /quests for the crew, approvals for the moderator (Mom).

Works without the Mini App: crew members see their level, the Daily Quest and
their quests and tap Done; Mom approves or rejects with the buttons on the
message she gets for each completed quest.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import bind_user, current_user_id
from handlers.start import is_authorized
from services import leveling_service as lv
from services import quest_service as qs
from views import HTML, bar, card, esc

logger = logging.getLogger(__name__)


def build_quests() -> tuple[str, InlineKeyboardMarkup]:
    me = current_user_id()
    st = lv.status(me)
    dq = qs.daily_quest(me)
    lines = [
        "🔷 <b>[System] Status</b>",
        card([
            f"<b>Level {st['level']}</b> · {esc(st['rank']['name'])}" + (f" · {esc(st['job'])}" if st["job"] else ""),
            f"EXP {bar(st['into'], st['needed'])} {st['into']}/{st['needed']}",
            " · ".join(f"{n['short']} {st['stats'][n['key']]}" for n in st["stat_names"])
            + (f"\n✨ {st['free_points']} stat points to spend (in the app)" if st["free_points"] else ""),
        ]),
    ]
    if dq["enabled"]:
        parts = [f"{'✅' if p['done'] >= p['target'] else '▫️'} {p['label']} {p['done']}/{p['target']}{' km' if p.get('unit') == 'km' else ''}"
                 for p in dq["parts"]]
        lines += ["", "⚔️ <b>Daily Quest: Preparation to Become Strong</b>", card(parts),
                  ("✅ Cleared! " + ("+" + str(dq["reward_exp"]) + " EXP" if dq["rewarded"] else "Reward waits for the penalty."))
                  if dq["complete"] else f"Reward: +{dq['reward_exp']} EXP, +1 stat point. ⚠️ Fail it and a penalty follows."]
    quests = qs.quests_for(me)
    buttons = []
    if quests:
        lines += ["", "📜 <b>Quests</b>"]
        for q in quests:
            state = {"open": "", "submitted": " · ⏳ waiting for Mom", "approved": " · ✅ done", "rejected": " · ❌ redo"}[q["state"]]
            tag = "☠️ " if q["penalty"] else f"[{q['rank']}] "
            lines.append(f"{tag}{esc(q['title'])}" + (f" (+{q['exp']} EXP)" if q["exp"] else "") + state
                         + (f"\n   ↳ Mom: “{esc(q['review_note'])}”" if q["state"] == "rejected" and q["review_note"] else ""))
            if q["state"] in ("open", "rejected"):
                buttons.append([InlineKeyboardButton(f"✅ Done: {q['title'][:28]}", callback_data=f"qst_done:{q['id']}")])
    else:
        lines += ["", "📜 No quests yet. Mom can assign some, or add your own in the app."]
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="qst_refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def quests_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    text, markup = build_quests()
    await update.message.reply_text(text, reply_markup=markup, parse_mode=HTML)


async def quests_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """qst_* buttons (crew members)."""
    query = update.callback_query
    data = query.data or ""
    if data.startswith("qst_done:"):
        _, error = qs.submit(current_user_id(), int(data.split(":")[1]))
        await query.answer(error or "Sent to Mom for approval ⏳", show_alert=bool(error))
        from services import crew_service as cs
        await cs.tick(context.bot)
    else:
        await query.answer()
    text, markup = build_quests()
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
    except Exception as e:  # "message is not modified"
        logger.debug("quests refresh: %s", e)


async def review_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """quest_ok:<run> / quest_no:<run> from the moderator (or the owner while there's no moderator)."""
    query = update.callback_query
    user_id = query.from_user.id
    if not qs.can_review(user_id):
        await query.answer("Only the quest moderator can do that.", show_alert=True)
        return
    bind_user(user_id)
    action, run_id = query.data.split(":")
    run, error = qs.review(user_id, int(run_id), approve=action == "quest_ok")
    if error:
        await query.answer(error, show_alert=True)
        return
    await query.answer("Approved ✅" if run["status"] == "approved" else "Sent back ❌")
    verdict = (f"✅ <b>Approved</b>: {esc(run['title'])}" + (f" (+{run['exp']} EXP)" if run["exp"] else "")
               if run["status"] == "approved" else f"❌ <b>Sent back</b>: {esc(run['title'])}")
    try:
        await query.edit_message_text(f"{query.message.text_html}\n\n{verdict}", parse_mode=HTML)
    except Exception as e:
        logger.debug("review edit: %s", e)
    from services import crew_service as cs
    await cs.tick(context.bot)


async def handle_moderator_join(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    """/start mod_<code>: Mom opened her moderator invite."""
    user = update.effective_user
    result = qs.accept_mod_invite(code, user.id, user.first_name)
    if result == "already":
        await update.message.reply_text("You're already the quest moderator 👩‍⚖️ Completed quests will arrive here.")
        return
    if result != "ok":
        reason = {"crew": "crew members can't be moderators", "expired": "this invite has expired"}.get(
            result, "this invite link isn't valid")
        await update.message.reply_text(f"⚠️ Sorry, {reason}. Ask for a new link.")
        return
    from services.social_service import app_button
    names = ", ".join(esc(u) for u in [h["name"] for h in qs.moderator_state(user.id)["hunters"]])
    board = app_button("📜 Open the quest board")
    markup = InlineKeyboardMarkup([[board]]) if board else None
    await update.message.reply_text(
        f"👩‍⚖️ <b>Welcome, {esc(user.first_name or 'Mom')}! You are the Quest Moderator.</b>\n\n"
        f"Your hunters: {names}.\n\n"
        "• When they finish a quest, you get a message here with ✅ Approve / ❌ Reject. EXP only counts after you approve.\n"
        "• In the app (the <b>App</b> button) you can give them new quests: chores, homework, a 5 km run…\n"
        "• You don't get workouts or reminders. Just the final word. 😉",
        parse_mode=HTML, reply_markup=markup)
