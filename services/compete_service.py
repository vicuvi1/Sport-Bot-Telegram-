"""Crew competition: weekly points, forfeits for the loser, and 1-on-1 challenges.

Points are computed from what actually happened (workouts, wake-ups, tests,
challenges, forfeits), so there is no separate score to drift out of sync.
"""

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from database import as_user, current_user_id, get_connection, get_users
from services import activity_service
from services import crew_service as cs
from services.summary_service import week_bounds
from services.workout_service import get_current_date_str, get_or_create_daily_workout

logger = logging.getLogger(__name__)

POINTS = {
    "workout": 10,      # a completed day
    "full": 5,          # ...at full targets (not a quick workout)
    "wake": 5,          # woke up on time
    "test_pr": 20,      # new fitness-test personal best
    "challenge": 15,    # challenge won (target did it, or challenger when they didn't)
    "chicken": 5,       # opponent chickened out of your challenge
    "forfeit": 5,       # did your forfeit
}
POINTS_LEGEND = ("workout +10 · full targets +5 · woke on time +5 · challenge +15 · "
                 "test PR +20 · forfeit done +5")

FORFEITS = [
    "💪 50 extra push-ups",
    "🧘 3-minute plank",
    "🦵 100 squats",
    "🥶 Cold shower",
    "🍫 Buy the winner a snack",
]

CHALLENGES = {
    "push100": {"label": "💪 100 push-ups today", "match": "push", "amount": 100},
    "squat150": {"label": "🦵 150 squats today", "match": "squat", "amount": 150},
    "plank180": {"label": "🧘 3 minutes of plank today", "match": "plank", "amount": 180},
    "full": {"label": "🔥 Full workout today"},
    "early": {"label": "⚡ Full workout before 09:00"},
}

CHALLENGE_DEADLINE_HOUR = 23


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------

def points_breakdown(user_id: int, start: str, end: str) -> Dict[str, int]:
    """Points per category for one member between two dates (inclusive)."""
    with get_connection() as conn:
        days = conn.execute(
            "SELECT status, quick FROM daily_workouts WHERE user_id = ? AND date >= ? AND date <= ?;",
            (user_id, start, end)).fetchall()
        wakes = conn.execute(
            "SELECT COUNT(*) FROM wake_logs WHERE user_id = ? AND date >= ? AND date <= ? AND status = 'on_time';",
            (user_id, start, end)).fetchone()[0]
        tests = conn.execute(
            "SELECT test_key, month, value, substr(recorded_at, 1, 10) AS day FROM fitness_results "
            "WHERE user_id = ? AND value IS NOT NULL ORDER BY month;", (user_id,)).fetchall()
        won = conn.execute(
            "SELECT COUNT(*) FROM challenges WHERE date >= ? AND date <= ? AND "
            "((target = ? AND status = 'won') OR (challenger = ? AND status = 'lost'));",
            (start, end, user_id, user_id)).fetchone()[0]
        chickens = conn.execute(
            "SELECT COUNT(*) FROM challenges WHERE date >= ? AND date <= ? AND challenger = ? AND status = 'declined';",
            (start, end, user_id)).fetchone()[0]
        forfeits = conn.execute(
            "SELECT COUNT(*) FROM forfeits WHERE loser = ? AND status = 'done' "
            "AND substr(done_at, 1, 10) >= ? AND substr(done_at, 1, 10) <= ?;",
            (user_id, start, end)).fetchone()[0]

    prs, best = 0, {}
    for t in tests:
        previous = best.get(t["test_key"])
        if previous is not None and t["value"] > previous and start <= (t["day"] or "") <= end:
            prs += 1
        best[t["test_key"]] = max(previous or 0, t["value"])

    completed = [d for d in days if d["status"] == "completed"]
    parts = {
        "workout": len(completed) * POINTS["workout"],
        "full": sum(1 for d in completed if not d["quick"]) * POINTS["full"],
        "wake": wakes * POINTS["wake"],
        "test_pr": prs * POINTS["test_pr"],
        "challenge": won * POINTS["challenge"] + chickens * POINTS["chicken"],
        "forfeit": forfeits * POINTS["forfeit"],
    }
    parts["total"] = sum(parts.values())
    return parts


def standings(today_str: Optional[str] = None) -> List[Dict[str, Any]]:
    """This week's points for everyone, best first."""
    today_str = today_str or get_current_date_str()
    start, end = week_bounds(today_str)
    table = []
    for m in get_users():
        p = points_breakdown(m["user_id"], start, end)
        table.append({"user_id": m["user_id"], "name": cs.display_name(m["user_id"]), "points": p["total"], "parts": p})
    return sorted(table, key=lambda r: r["points"], reverse=True)


# ---------------------------------------------------------------------------
# Forfeits
# ---------------------------------------------------------------------------

def get_forfeit(forfeit_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM forfeits WHERE id = ?;", (forfeit_id,)).fetchone()
        return dict(row) if row else None


def open_forfeits(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Forfeits not done yet (optionally only those owed by `user_id`)."""
    with get_connection() as conn:
        sql = "SELECT * FROM forfeits WHERE status IN ('choosing', 'pending')"
        params: tuple = ()
        if user_id is not None:
            sql += " AND loser = ?"
            params = (user_id,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY id;", params).fetchall()]


def create_forfeit(week_start: str, winner: int, loser: int) -> Optional[int]:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO forfeits (week_start, winner, loser, created_at) VALUES (?, ?, ?, ?);",
            (week_start, winner, loser, _now_iso()))
        conn.commit()
        return cur.lastrowid if cur.rowcount else None


def pick_forfeit(forfeit_id: int, by_user: int, task_index: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The winner picks (or the bot picks at random). Returns the forfeit, or None if not allowed."""
    f = get_forfeit(forfeit_id)
    if not f or f["status"] != "choosing" or by_user != f["winner"]:
        return None
    task = FORFEITS[task_index] if task_index is not None and 0 <= task_index < len(FORFEITS) else random.choice(FORFEITS)
    with get_connection() as conn:
        conn.execute("UPDATE forfeits SET task = ?, status = 'pending' WHERE id = ?;", (task, forfeit_id))
        conn.commit()
    return get_forfeit(forfeit_id)


def complete_forfeit(forfeit_id: int, by_user: int) -> Optional[Dict[str, Any]]:
    f = get_forfeit(forfeit_id)
    if not f or f["status"] != "pending" or by_user != f["loser"]:
        return None
    activity_service.record("forfeit", f"did the forfeit: {f['task']} 😬", user_id=by_user)
    with get_connection() as conn:
        conn.execute("UPDATE forfeits SET status = 'done', done_at = ? WHERE id = ?;",
                     (get_current_date_str() + "T" + datetime.now().strftime("%H:%M"), forfeit_id))
        conn.commit()
    return get_forfeit(forfeit_id)


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------

def get_challenge(challenge_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM challenges WHERE id = ?;", (challenge_id,)).fetchone()
        return dict(row) if row else None


def offer_challenge(challenger: int, target: int, kind: str, date_str: Optional[str] = None) -> Optional[int]:
    """Creates a challenge for today. None if invalid or one is already open between them today."""
    if kind not in CHALLENGES or challenger == target:
        return None
    date_str = date_str or get_current_date_str()
    with get_connection() as conn:
        open_one = conn.execute(
            "SELECT 1 FROM challenges WHERE challenger = ? AND target = ? AND date = ? "
            "AND status IN ('offered', 'accepted');", (challenger, target, date_str)).fetchone()
        if open_one:
            return None
        cur = conn.execute(
            "INSERT INTO challenges (challenger, target, kind, date, created_at) VALUES (?, ?, ?, ?, ?);",
            (challenger, target, kind, date_str, _now_iso()))
        conn.commit()
        return cur.lastrowid


def _set_challenge_status(challenge_id: int, status: str) -> Dict[str, Any]:
    with get_connection() as conn:
        conn.execute("UPDATE challenges SET status = ?, resolved_at = ? WHERE id = ?;",
                     (status, _now_iso(), challenge_id))
        conn.commit()
    return get_challenge(challenge_id)


def respond_challenge(challenge_id: int, by_user: int, accept: bool) -> Optional[Dict[str, Any]]:
    c = get_challenge(challenge_id)
    if not c or c["status"] != "offered" or by_user != c["target"] or c["date"] != get_current_date_str():
        return None
    label = CHALLENGES[c["kind"]]["label"]
    activity_service.record("challenge", f"accepted: {label} ⚔️" if accept else f"chickened out of: {label} 🐔",
                            user_id=by_user)
    return _set_challenge_status(challenge_id, "accepted" if accept else "declined")


def challenge_met(challenge: Dict[str, Any]) -> bool:
    """Has the target done what the challenge asked (on the challenge's day)?"""
    spec = CHALLENGES[challenge["kind"]]
    with as_user(challenge["target"]):
        workout = get_or_create_daily_workout(challenge["date"])
    if challenge["kind"] == "full":
        return workout["status"] == "completed"
    if challenge["kind"] == "early":
        done_at = workout.get("completed_at") or ""
        return workout["status"] == "completed" and done_at[11:16] < "09:00"
    total = sum(it["completed_reps"] for it in workout["items"] if spec["match"] in it["exercise_name"].lower())
    return total >= spec["amount"]


def open_challenges(status: str = "accepted") -> List[Dict[str, Any]]:
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM challenges WHERE status = ?;", (status,)).fetchall()]


async def check_challenges(bot) -> int:
    """Marks accepted challenges as won as soon as the target has done it."""
    from views import esc
    won = 0
    for c in open_challenges("accepted"):
        if not challenge_met(c):
            continue
        _set_challenge_status(c["id"], "won")
        won += 1
        activity_service.record("challenge", f"beat the challenge: {CHALLENGES[c['kind']]['label']} 🏆",
                                user_id=c["target"])
        label = CHALLENGES[c["kind"]]["label"]
        target_name, challenger_name = esc(cs.display_name(c["target"])), esc(cs.display_name(c["challenger"]))
        await cs.send_html(bot, c["target"], f"🏆 <b>Challenge won!</b> {label}. +{POINTS['challenge']} pts 💪")
        await cs.send_html(bot, c["challenger"],
                           f"😤 <b>{target_name}</b> beat your challenge: {label}. +{POINTS['challenge']} pts for them.")
    return won


cs.TICK_HOOKS.append(check_challenges)


async def close_challenges(bot) -> None:
    """Deadline (23:00, run as the target): unfinished accepted challenges are lost,
    unanswered offers expire."""
    from views import esc
    me = current_user_id()
    today = get_current_date_str()
    await check_challenges(bot)
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM challenges WHERE target = ? AND date <= ? AND status IN ('offered', 'accepted');",
                            (me, today)).fetchall()
    for c in (dict(r) for r in rows):
        label = CHALLENGES[c["kind"]]["label"]
        if c["status"] == "offered":
            _set_challenge_status(c["id"], "expired")
            continue
        _set_challenge_status(c["id"], "lost")
        activity_service.record("challenge", f"failed the challenge: {CHALLENGES[c['kind']]['label']} 😬", user_id=me)
        await cs.send_html(bot, me, f"😬 <b>Challenge lost:</b> {label}. "
                                    f"{esc(cs.display_name(c['challenger']))} gets +{POINTS['challenge']} pts.")
        await cs.send_html(bot, c["challenger"],
                           f"😎 <b>{esc(cs.display_name(me))}</b> failed your challenge ({label}). "
                           f"+{POINTS['challenge']} pts for you!")


# ---------------------------------------------------------------------------
# Weekly results
# ---------------------------------------------------------------------------

async def send_weekly_results(bot, today_str: Optional[str] = None) -> Optional[int]:
    """Sunday evening: standings to everyone, and a forfeit for the loser.
    Returns the forfeit id when one was created."""
    from telegram import InlineKeyboardButton
    from views import card, esc, nice_date

    members = get_users()
    if len(members) < 2:
        return None
    today_str = today_str or get_current_date_str()
    start, end = week_bounds(today_str)
    table = standings(today_str)
    medals = ["👑", "🥈", "🥉"] + ["▫️"] * 10
    lines = [f"🏆 <b>Week results</b> · {nice_date(start)} – {nice_date(end)}", "",
             card([f"{medals[i]} {esc(r['name'])}  <b>{r['points']}</b> pts" for i, r in enumerate(table)]),
             f"<i>{POINTS_LEGEND}</i>"]

    winner, loser = table[0], table[-1]
    forfeit_id = None
    if winner["points"] > loser["points"] and sum(1 for r in table if r["points"] == winner["points"]) == 1:
        forfeit_id = create_forfeit(start, winner["user_id"], loser["user_id"])
        lines.append(f"\n👑 <b>{esc(winner['name'])}</b> wins the week! "
                     f"{esc(loser['name'])} gets a forfeit 😬")
    else:
        lines.append("\n🤝 It's a tie at the top. No forfeit this week.")

    text = "\n".join(lines)
    for m in members:
        await cs.send_html(bot, m["user_id"], text)
    if forfeit_id:
        buttons = [[InlineKeyboardButton(task, callback_data=f"cmp_fpick:{forfeit_id}:{i}")]
                   for i, task in enumerate(FORFEITS)]
        buttons.append([InlineKeyboardButton("🎲 Surprise me", callback_data=f"cmp_fpick:{forfeit_id}:r")])
        await cs.send_html(bot, winner["user_id"],
                           f"👑 You won! Pick <b>{esc(loser['name'])}</b>'s forfeit:", buttons)
    return forfeit_id


async def auto_pick_forfeits(bot) -> None:
    """Monday midday: if a winner never picked, the bot does."""
    for f in open_forfeits():
        if f["status"] == "choosing":
            picked = pick_forfeit(f["id"], f["winner"])
            if picked:
                await announce_forfeit(bot, picked, auto=True)


async def announce_forfeit(bot, forfeit: Dict[str, Any], auto: bool = False) -> None:
    from telegram import InlineKeyboardButton
    from views import esc
    winner = esc(cs.display_name(forfeit["winner"]))
    how = "The bot picked" if auto else f"{winner} picked"
    await cs.send_html(
        bot, forfeit["loser"],
        f"😬 <b>Forfeit time!</b> {how} for you: <b>{esc(forfeit['task'])}</b>\n"
        f"Do it this week, then tap Done (+{POINTS['forfeit']} pts).",
        [[InlineKeyboardButton("✅ Done it", callback_data=f"cmp_fdone:{forfeit['id']}")]])
    if not auto:
        return
    await cs.send_html(bot, forfeit["winner"],
                       f"🎲 You didn't pick, so I did: {esc(cs.display_name(forfeit['loser']))} owes "
                       f"<b>{esc(forfeit['task'])}</b>.")
