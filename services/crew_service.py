"""Crew: brothers (or friends) training together in one bot.

Members join through a single-use invite link (t.me/<bot>?start=crew_<code>).
Each member has their own workouts, settings and streaks (see database.py);
this module adds what connects them: a live feed of finished workouts, the
/duel scoreboard, a shared streak, and roasts / hypes.
"""

import json
import logging
import random
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from database import (
    add_user,
    as_user,
    current_user_id,
    get_connection,
    get_setting,
    get_user,
    get_users,
    is_member,
    set_setting,
    set_user_name,
)
from services.workout_service import (
    calculate_streaks,
    get_current_date_str,
    get_or_create_daily_workout,
    get_paused_dates,
    is_day_active,
    sanitize_label,
)

logger = logging.getLogger(__name__)

CREW_MAX = 6
INVITE_PREFIX = "crew_"
INVITE_VALID_HOURS = 48
ROASTS_PER_DAY = 5  # per sender → target, so nobody gets spammed

ROAST_LEVELS = ["savage", "friendly", "off"]


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

def display_name(user_id: int) -> str:
    user = get_user(user_id)
    return (user and user["name"]) or "Your crew mate"


def remember_name(user_id: int, first_name: Optional[str]) -> None:
    name = sanitize_label(first_name or "", max_length=30)
    if name and is_member(user_id):
        set_user_name(user_id, name)


def others(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Everyone in the crew except `user_id` (default: the current user)."""
    me = current_user_id() if user_id is None else user_id
    return [u for u in get_users() if u["user_id"] != me]


def user_setting(user_id: int, key: str, default: str) -> str:
    with as_user(user_id):
        return get_setting(key, default)


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def create_invite(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    code = secrets.token_urlsafe(9)
    set_setting("crew_invite_code", code)
    set_setting("crew_invite_expires", (now + timedelta(hours=INVITE_VALID_HOURS)).isoformat())
    return code


def pending_invite_expiry(now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.now(timezone.utc)
    if not get_setting("crew_invite_code", ""):
        return None
    try:
        expires = datetime.fromisoformat(get_setting("crew_invite_expires", ""))
    except ValueError:
        return None
    return expires if expires > now else None


def accept_invite(code: str, user_id: int, first_name: Optional[str],
                  now: Optional[datetime] = None) -> str:
    """Adds `user_id` to the crew. Returns 'ok', 'member', 'full', 'invalid' or 'expired'."""
    if is_member(user_id):
        return "member"
    stored = get_setting("crew_invite_code", "")
    if not stored or not secrets.compare_digest(stored, code):
        return "invalid"
    if pending_invite_expiry(now) is None:
        return "expired"
    if len(get_users()) >= CREW_MAX:
        return "full"
    add_user(user_id, sanitize_label(first_name or "", max_length=30))
    # Single use: a forwarded link can't add anyone else.
    set_setting("crew_invite_code", "")
    set_setting("crew_invite_expires", "")
    return "ok"


# ---------------------------------------------------------------------------
# Today's numbers per member
# ---------------------------------------------------------------------------

def day_snapshot(user_id: int, date_str: Optional[str] = None) -> Dict[str, Any]:
    """One member's day: status, exercises done, total reps, streak."""
    with as_user(user_id):
        date_str = date_str or get_current_date_str()
        workout = get_or_create_daily_workout(date_str)
        current, best = calculate_streaks(today_str=date_str)
    items = workout["items"]
    return {
        "user_id": user_id,
        "name": display_name(user_id),
        "date": date_str,
        "status": workout["status"],
        "quick": bool(workout.get("quick")),
        "done": sum(1 for it in items if it["status"] == "completed"),
        "total": len(items),
        "reps": sum(it["completed_reps"] for it in items if it["unit"] == "reps"),
        "items": items,
        "streak": current,
        "best": best,
    }


def progress_ratio(snap: Dict[str, Any]) -> float:
    if snap["status"] == "completed":
        return 1.0
    return snap["done"] / snap["total"] if snap["total"] else 0.0


def crew_streak(today_str: Optional[str] = None) -> int:
    """Days in a row on which *everyone* trained (rest/paused days don't break it).

    Today only counts once everybody is done; until then it neither adds nor breaks.
    """
    members = get_users()
    if len(members) < 2:
        return 0
    today_str = today_str or get_current_date_str()
    today = datetime.strptime(today_str, "%Y-%m-%d").date()

    per_member = []
    for m in members:
        with as_user(m["user_id"]):
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT date, status FROM daily_workouts WHERE user_id = ? AND date <= ? AND date >= ?;",
                    (m["user_id"], today_str, (today - timedelta(days=400)).isoformat())
                ).fetchall()
            per_member.append({
                "status": {r["date"]: r["status"] for r in rows},
                "paused": get_paused_dates(),
                "days": get_setting("workout_days", "0,1,2,3,4,5,6"),
                "joined": min((r["date"] for r in rows), default=today_str),
            })

    streak = 0
    for i in range(0, 400):
        day = today - timedelta(days=i)
        ds = day.isoformat()
        if any(ds < pm["joined"] for pm in per_member):
            break  # before someone joined
        states = []
        for pm in per_member:
            s = pm["status"].get(ds)
            if s == "completed":
                states.append("done")
            elif s in ("rest", "paused") or ds in pm["paused"] or not is_day_active(day.weekday(), pm["days"]):
                states.append("free")
            else:
                states.append("missed")
        if i == 0 and "missed" in states:
            continue  # today isn't over yet
        if "missed" in states:
            break
        if "done" in states:
            streak += 1
    return streak


# ---------------------------------------------------------------------------
# Events (live feed)
# ---------------------------------------------------------------------------
# Services record events in the database; deliver_events() sends them. This
# keeps the workout logic free of Telegram calls, and an event can't get lost
# if sending fails: a periodic job retries undelivered ones.

def record_event(kind: str, data: Optional[Dict[str, Any]] = None, user_id: Optional[int] = None,
                 cursor=None) -> None:
    uid = current_user_id() if user_id is None else user_id
    params = (uid, kind, json.dumps(data or {}), datetime.now(timezone.utc).isoformat())
    sql = "INSERT INTO crew_events (user_id, kind, data, created_at) VALUES (?, ?, ?, ?);"
    if cursor is not None:
        cursor.execute(sql, params)
        return
    with get_connection() as conn:
        conn.execute(sql, params)
        conn.commit()


def pending_events(limit: int = 50) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM crew_events WHERE delivered = 0 ORDER BY id LIMIT ?;", (limit,)).fetchall()
    events = []
    for r in rows:
        event = dict(r)
        event["data"] = json.loads(event["data"] or "{}")
        events.append(event)
    return events


def mark_delivered(event_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE crew_events SET delivered = 1 WHERE id = ?;", (event_id,))
        conn.commit()


async def send_html(bot, chat_id: int, text: str, buttons: Optional[List[List[Any]]] = None) -> bool:
    """Sends to one crew member; failures (e.g. they blocked the bot) are only warnings."""
    from telegram import InlineKeyboardMarkup
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
        return True
    except Exception as e:
        logger.warning("Could not message crew member %s: %s", chat_id, e)
        return False


def exercise_summary(items: List[Dict[str, Any]]) -> str:
    """'Push-ups 50 · Squats 50 · Plank 60s' (escaped)."""
    from views import esc, unit_label
    return " · ".join(
        f"{esc(it['exercise_name'])} {it['completed_reps']}"
        + ("" if it["unit"] == "reps" else esc(unit_label(it["unit"])))
        for it in items if it["status"] != "skipped"
    )


async def _announce_workout_done(bot, event: Dict[str, Any]) -> None:
    from telegram import InlineKeyboardButton
    from views import card, esc

    finisher = day_snapshot(event["user_id"])
    name = esc(finisher["name"])
    for member in others(event["user_id"]):
        uid = member["user_id"]
        if user_setting(uid, "crew_feed", "1") != "1":
            continue
        mine = day_snapshot(uid)
        lines = [
            f"🏁 <b>{name}</b> just finished today's workout!",
            card([exercise_summary(finisher["items"]), f"🔥 {finisher['streak']}-day streak"]),
        ]
        buttons = [[InlineKeyboardButton(f"💪 Hype {finisher['name']}", callback_data=f"crew_hype:{event['user_id']}")]]
        if mine["status"] == "completed":
            lines.append("You're both done today 🤜🤛")
        elif mine["status"] == "pending":
            lines.append(f"Your move 👀 You're at <b>{mine['done']}/{mine['total']}</b>.")
            buttons.append([InlineKeyboardButton("🏋️ My workout", callback_data="refresh_today")])
        await send_html(bot, uid, "\n".join(lines), buttons)


# kind -> async handler(bot, event). Other crew features register theirs here.
EVENT_HANDLERS = {
    "workout_done": _announce_workout_done,
}


# Extra things to do on every tick (e.g. resolving challenges): async fn(bot).
TICK_HOOKS = []


async def tick(bot) -> None:
    """Runs after every workout action and once a minute: feed, then hooks."""
    await deliver_events(bot)
    for hook in TICK_HOOKS:
        try:
            await hook(bot)
        except Exception as e:
            logger.warning("Crew tick hook %s failed: %s", getattr(hook, "__name__", hook), e)


async def deliver_events(bot) -> int:
    """Sends all undelivered crew events. Returns how many were processed."""
    events = pending_events()
    for event in events:
        handler = EVENT_HANDLERS.get(event["kind"])
        try:
            if handler and len(get_users()) > 1:
                await handler(bot, event)
        except Exception as e:
            logger.warning("Crew event %s (%s) failed: %s", event["id"], event["kind"], e)
        # Marked even after a failure: a broken event must not repeat forever.
        mark_delivered(event["id"])
    return len(events)


# ---------------------------------------------------------------------------
# Roasts & hypes
# ---------------------------------------------------------------------------
# {me} = the person receiving it, {them} = the sender (or whoever is ahead),
# {their_reps} = the sender's reps today. Brother-grade teasing, never hateful.

# "ahead" lines only make sense when the sender has done more today.
ROASTS = {
    "savage": {
        "ahead": [
            "{them} already did {their_reps} reps today. You? Loser 😂",
            "{them} is getting stronger while you're getting comfier 🛋️",
            "{them} says: \"Imagine being this lazy.\" Prove {them} wrong, {me}.",
        ],
        "any": [
            "Breaking news 📰 The couch filed a restraining order against {me}. Get up.",
            "Your grandma called. She finished her push-ups. Did you? 👵💪",
            "Even your phone did more reps than you today (scrolling counts, right?) 📱",
            "🚨 Loser alert 🚨 {me} hasn't trained today.",
            "🐔 Bawk bawk. Today's workout is waiting, {me}.",
            "Fun fact: muscles don't grow on the sofa, {me}. Science 🧪",
            "{me}, the only thing you lifted today was the remote 📺",
        ],
    },
    "friendly": {
        "ahead": [
            "Someone's slacking 👀 {them} already got moving today.",
            "{them} is ahead today. Catch up? 💨",
        ],
        "any": [
            "Hey {me}, your workout misses you 🥺",
            "Psst {me}... ten minutes, that's all it takes ⏱",
            "{them} is waiting for you to join the party 🎉",
        ],
    },
}


def pick_roast(level: str, me: str, them: str, their_reps: int, sender_ahead: bool) -> str:
    pool = ROASTS[level]["any"] + (ROASTS[level]["ahead"] if sender_ahead else [])
    return pick_line(pool, me, them, their_reps)

HYPES = [
    "💪 {them} says: you're a machine, {me}!",
    "🔥 {them} is proud of you. Keep it going!",
    "👑 {them} bows to you. Respect.",
    "🚀 {them} thinks you're unstoppable, {me}.",
    "🦍 Beast mode: activated. Signed, {them}.",
]

AUTO_ROASTS = {
    "savage": [
        "🤖 Auto-roast: {them} finished today and you haven't. Loser 😂 There's still time.",
        "🤖 It's late and {them} trained. You didn't. The couch wins again 🛋️",
        "🤖 {them}: done ✅. {me}: still \"about to start\" 🐢",
    ],
    "friendly": [
        "🤖 Friendly reminder: {them} trained today. Still time for a quick one, {me}!",
        "🤖 {them} is done for today. Want to keep up? Even a short workout counts.",
    ],
}

AUTO_ROAST_HOUR = 21


async def send_auto_roast(bot) -> None:
    """Evening job: roast the current user if a crew mate trained today and they didn't."""
    from telegram import InlineKeyboardButton
    from views import esc, quick_toggle_button

    me = current_user_id()
    level = roast_level(me)
    if len(get_users()) < 2 or level == "off":
        return
    mine = day_snapshot(me)
    if mine["status"] != "pending":
        return
    finished = [s for s in (day_snapshot(m["user_id"]) for m in others(me)) if s["status"] == "completed"]
    if not finished:
        return
    line = pick_line(AUTO_ROASTS[level], me=mine["name"], them=finished[0]["name"])
    buttons = [[quick_toggle_button(mine["quick"])],
               [InlineKeyboardButton("🏋️ My workout", callback_data="refresh_today")]]
    await send_html(bot, me, esc(line), buttons)


def pick_line(lines: List[str], me: str, them: str, their_reps: int = 0) -> str:
    return random.choice(lines).format(me=me, them=them, their_reps=their_reps)


def roast_level(user_id: int) -> str:
    level = user_setting(user_id, "roast_level", "savage")
    return level if level in ROAST_LEVELS else "savage"


async def send_roast(bot, bot_data: Dict[str, Any], sender: int, target: int) -> Dict[str, Any]:
    """Roasts `target` from `sender` (bot buttons and the app share this).

    Returns {"sent": bool, "message": text for the sender, "alert": show it prominently}.
    """
    from telegram import InlineKeyboardButton
    from views import esc

    if target == sender or not is_member(target):
        return {"sent": False, "message": "Roasting yourself? Bold move 😅", "alert": False}
    target_name = display_name(target)
    level = roast_level(target)
    if level == "off":
        return {"sent": False, "message": f"{target_name} turned roasts off 🐔", "alert": True}

    mine, theirs = day_snapshot(sender), day_snapshot(target)
    if theirs["status"] == "completed" and mine["status"] != "completed":
        # Backfire: you can't roast someone who's done when you aren't.
        return {"sent": False, "alert": True,
                "message": f"😅 {target_name} is already done today. You're at {mine['done']}/{mine['total']}... "
                           "who's the loser now?"}
    if not can_send_roast(bot_data, sender, target, get_current_date_str()):
        return {"sent": False, "message": f"Easy 😅 That's {ROASTS_PER_DAY} roasts today.", "alert": True}

    ahead = progress_ratio(mine) > progress_ratio(theirs)
    line = pick_roast(level, me=target_name, them=mine["name"], their_reps=mine["reps"], sender_ahead=ahead)
    sent = await send_html(
        bot, target, f"😈 <b>Roast from {esc(mine['name'])}</b>\n\n{esc(line)}",
        [[InlineKeyboardButton(f"😈 Roast {mine['name']} back", callback_data=f"crew_roast:{sender}")],
         [InlineKeyboardButton("🏋️ My workout", callback_data="refresh_today")]])
    return {"sent": sent, "alert": False,
            "message": f"😈 Roast delivered to {target_name}" if sent else "Couldn't reach them right now."}


async def send_hype(bot, sender: int, target: int) -> Dict[str, Any]:
    """Hypes `target` from `sender`. Same return shape as send_roast."""
    from telegram import InlineKeyboardButton
    from views import esc

    if target == sender or not is_member(target):
        return {"sent": False, "message": "Self-hype noted 😄", "alert": False}
    my_name = display_name(sender)
    line = pick_line(HYPES, me=display_name(target), them=my_name)
    sent = await send_html(bot, target, esc(line),
                           [[InlineKeyboardButton(f"💪 Hype {my_name} back", callback_data=f"crew_hype:{sender}")]])
    return {"sent": sent, "alert": False,
            "message": f"💪 Hype sent to {display_name(target)}" if sent else "Couldn't reach them right now."}


def can_send_roast(bot_data: Dict[str, Any], sender: int, target: int, date_str: str) -> bool:
    """At most ROASTS_PER_DAY roasts from one person to another per day."""
    counts = bot_data.setdefault("roast_counts", {})
    key = (sender, target, date_str)
    if counts.get(key, 0) >= ROASTS_PER_DAY:
        return False
    counts[key] = counts.get(key, 0) + 1
    return True
