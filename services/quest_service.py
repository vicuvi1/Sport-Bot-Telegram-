"""Quests, the Daily Quest and penalties, approved by a moderator (Mom).

- Moderators join with their own invite link. They are NOT crew members: no
  workouts, reminders or roasts. They create and assign quests and approve
  (or reject) completed ones. With no moderator yet, the owner approves.
- Quests: life tasks or fitness side quests, ranked E-S for EXP, one-time,
  daily or weekly. Crew members can propose their own; EXP is only given
  once a moderator approves the completion.
- Daily Quest "Preparation to Become Strong": push-ups, sit-ups, squats and a
  run, scaling with level up to 100/100/100 and 10 km. Reps from the normal
  workout count. Clear it for EXP and a stat point. Miss it and the next day
  brings a Penalty Quest; until it's approved, Daily Quest rewards wait.
"""

import json
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from database import as_user, current_user_id, get_connection, get_setting, get_users, set_setting
from services import activity_service
from services import crew_service as cs
from services import leveling_service as lv
from services.summary_service import week_bounds
from services.workout_service import (
    get_current_date_str,
    get_or_create_daily_workout,
    is_date_paused,
    sanitize_label,
    update_workout_item,
)

QUEST_RANKS = {"E": 25, "D": 50, "C": 100, "B": 200, "A": 400, "S": 800}
CATEGORIES = ("life", "fitness")
REPEATS = ("once", "daily", "weekly")
TITLE_CHARS = 60
NOTE_CHARS = 140
MAX_PROOF_BYTES = 6 * 1024 * 1024
MOD_INVITE_PREFIX = "mod_"
MOD_INVITE_HOURS = 48
TEMPLATES = [
    ("🛏 Make your bed", "life", "E", "daily"),
    ("🧹 Clean your room", "life", "D", "weekly"),
    ("📚 Homework done", "life", "D", "daily"),
    ("📖 Read 30 minutes", "life", "D", "daily"),
    ("🍽 Wash the dishes", "life", "E", "once"),
    ("🛒 Help with the groceries", "life", "D", "once"),
    ("🗑 Take out the trash", "life", "E", "once"),
    ("🏃 5 km run", "fitness", "C", "once"),
    ("🧊 Cold shower", "fitness", "D", "once"),
    ("🧘 Stretch 15 minutes", "fitness", "E", "daily"),
    ("💯 100 burpees", "fitness", "B", "once"),
]
PENALTIES = [
    "Penalty Quest: 100 burpees",
    "Penalty Quest: 5 minutes of plank (in total)",
    "Penalty Quest: 200 squats",
    "Penalty Quest: cold shower",
    "Penalty Quest: clean the whole kitchen",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Moderators
# ---------------------------------------------------------------------------

def moderators() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM moderators ORDER BY created_at;").fetchall()]


def is_moderator(user_id: Optional[int]) -> bool:
    return bool(user_id) and any(m["user_id"] == user_id for m in moderators())


def can_review(user_id: int) -> bool:
    """Moderators review; while there is none, the owner does."""
    return is_moderator(user_id) or (user_id == config.USER_ID and not moderators())


def reviewers() -> List[int]:
    mods = [m["user_id"] for m in moderators()]
    return mods or [config.USER_ID]


def moderator_name(user_id: int) -> str:
    return next((m["name"] for m in moderators() if m["user_id"] == user_id), "") or "Mom"


def create_mod_invite(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    code = secrets.token_urlsafe(9)
    set_setting("mod_invite_code", code)
    set_setting("mod_invite_expires", (now + timedelta(hours=MOD_INVITE_HOURS)).isoformat())
    return code


def accept_mod_invite(code: str, user_id: int, first_name: Optional[str], now: Optional[datetime] = None) -> str:
    """Returns 'ok', 'already', 'crew' (crew members can't moderate), 'expired' or 'invalid'."""
    from database import is_member
    if is_moderator(user_id):
        return "already"
    if user_id == config.USER_ID or is_member(user_id):
        return "crew"
    stored = get_setting("mod_invite_code", "")
    if not stored or not secrets.compare_digest(stored, code):
        return "invalid"
    try:
        expires = datetime.fromisoformat(get_setting("mod_invite_expires", ""))
    except ValueError:
        return "invalid"
    if expires < (now or datetime.now(timezone.utc)):
        return "expired"
    with get_connection() as conn:
        conn.execute("INSERT INTO moderators (user_id, name, created_at) VALUES (?, ?, ?);",
                     (user_id, sanitize_label(first_name or "Mom", max_length=30), _now()))
        conn.commit()
    set_setting("mod_invite_code", "")
    set_setting("mod_invite_expires", "")
    return "ok"


def remove_moderator(user_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM moderators WHERE user_id = ?;", (user_id,))
        conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Quests
# ---------------------------------------------------------------------------

def _crew_ids() -> List[int]:
    return [u["user_id"] for u in get_users()]


def period_for(repeat: str, today: str) -> str:
    if repeat == "daily":
        return today
    if repeat == "weekly":
        return week_bounds(today)[0]
    return "once"


def create_quest(creator: int, title: str, category: str, rank: str, repeat: str = "once",
                 assignee: Optional[int] = None, penalty: bool = False) -> Tuple[Optional[int], Optional[str]]:
    """Moderators/owner assign to anyone (None = everyone); crew members propose for themselves."""
    title = sanitize_label(title or "", max_length=TITLE_CHARS)
    if not title:
        return None, "Give the quest a name."
    if category not in CATEGORIES or repeat not in REPEATS or (rank not in QUEST_RANKS and not penalty):
        return None, "Pick a category, rank and how often."
    crew = _crew_ids()
    if assignee is not None and assignee not in crew:
        return None, "Assign it to someone in the crew."
    if not (is_moderator(creator) or creator == config.USER_ID):
        if creator not in crew:
            return None, "Only the crew and moderators can create quests."
        if assignee not in (None, creator):
            return None, "You can only propose quests for yourself."
        assignee = creator
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO quests (title, category, rank, repeat, assignee, created_by, penalty, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                           (title, category, rank if not penalty else "P", repeat, assignee, creator, int(penalty), _now()))
        conn.commit()
        quest_id = cur.lastrowid
    if not penalty:
        cs.record_event("quest_new", {"quest_id": quest_id}, user_id=creator)
    return quest_id, None


def get_quest(quest_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM quests WHERE id = ?;", (quest_id,)).fetchone()
    return dict(row) if row else None


def archive_quest(user_id: int, quest_id: int) -> Optional[str]:
    q = get_quest(quest_id)
    if not q:
        return "That quest is gone."
    if q["penalty"]:   # nobody escapes a penalty by deleting it
        allowed = can_review(user_id) and q["assignee"] != user_id
    else:
        allowed = can_review(user_id) or user_id == config.USER_ID or q["created_by"] == user_id
    if not allowed:
        return "Only Mom can remove that quest."
    with get_connection() as conn:
        conn.execute("UPDATE quests SET active = 0 WHERE id = ?;", (quest_id,))
        conn.commit()
    return None


def _run(quest_id: int, user_id: int, period: str):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM quest_runs WHERE quest_id = ? AND user_id = ? AND period = ?;",
                            (quest_id, user_id, period)).fetchone()


def quest_state(q: Dict[str, Any], user_id: int, today: str) -> Dict[str, Any]:
    period = period_for(q["repeat"], today)
    run = _run(q["id"], user_id, period)
    if q["repeat"] == "once" and not run:
        with get_connection() as conn:   # a one-time quest done once stays done
            run = conn.execute("SELECT * FROM quest_runs WHERE quest_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1;",
                               (q["id"], user_id)).fetchone()
    state = run["status"] if run else "open"
    return {
        "id": q["id"], "title": q["title"], "category": q["category"], "rank": q["rank"], "repeat": q["repeat"],
        "exp": 0 if q["penalty"] else QUEST_RANKS.get(q["rank"], 0), "penalty": bool(q["penalty"]),
        "from": "System" if q["penalty"] else (moderator_name(q["created_by"]) if is_moderator(q["created_by"])
                                               else cs.display_name(q["created_by"])),
        "mine": q["created_by"] == user_id,
        "state": state, "run_id": run["id"] if run else None,
        "review_note": run["review_note"] if run else "", "note": run["note"] if run else "",
    }


def quests_for(user_id: int, today: Optional[str] = None) -> List[Dict[str, Any]]:
    today = today or _today(user_id)
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM quests WHERE active = 1 AND (assignee = ? OR assignee IS NULL) "
                            "ORDER BY penalty DESC, id DESC;", (user_id,)).fetchall()
    out = [quest_state(dict(r), user_id, today) for r in rows]
    # Finished one-time quests drop off after they're approved.
    return [q for q in out if not (q["repeat"] == "once" and q["state"] == "approved")]


def _today(user_id: int) -> str:
    with as_user(user_id):
        return get_current_date_str()


def proofs_dir(user_id: int) -> Path:
    return config.DATA_DIR / "quest_proofs" / str(user_id)


def submit(user_id: int, quest_id: int, note: str = "", proof: Optional[bytes] = None) -> Tuple[Optional[int], Optional[str]]:
    q = get_quest(quest_id)
    if not q or not q["active"] or q["assignee"] not in (None, user_id) or user_id not in _crew_ids():
        return None, "That quest isn't yours."
    today = _today(user_id)
    period = period_for(q["repeat"], today)
    existing = _run(quest_id, user_id, period)
    if existing and existing["status"] in ("submitted", "approved"):
        return None, "Already sent to Mom." if existing["status"] == "submitted" else "Already done."
    filename = None
    if proof:
        if len(proof) > MAX_PROOF_BYTES:
            return None, "Photo must be smaller than 6 MB."
        ext = "jpg" if proof.startswith(b"\xff\xd8\xff") else "png" if proof.startswith(b"\x89PNG\r\n\x1a\n") else None
        if not ext:
            return None, "Only JPEG or PNG photos."
        folder = proofs_dir(user_id)
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.{ext}"
        (folder / filename).write_bytes(proof)
    note = " ".join(str(note or "").split())[:NOTE_CHARS]
    with get_connection() as conn:
        if existing:
            conn.execute("UPDATE quest_runs SET status = 'submitted', note = ?, proof = COALESCE(?, proof), review_note = '', "
                         "submitted_at = ?, reviewed_at = NULL, reviewed_by = NULL WHERE id = ?;",
                         (note, filename, _now(), existing["id"]))
            run_id = existing["id"]
        else:
            cur = conn.execute("INSERT INTO quest_runs (quest_id, user_id, period, status, note, proof, submitted_at) "
                               "VALUES (?, ?, ?, 'submitted', ?, ?, ?);", (quest_id, user_id, period, note, filename, _now()))
            run_id = cur.lastrowid
        conn.commit()
    cs.record_event("quest_submitted", {"run_id": run_id}, user_id=user_id)
    return run_id, None


def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT r.*, q.title, q.rank, q.category, q.penalty, q.repeat FROM quest_runs r "
                           "JOIN quests q ON q.id = r.quest_id WHERE r.id = ?;", (run_id,)).fetchone()
    return dict(row) if row else None


def review(reviewer: int, run_id: int, approve: bool, rank: Optional[str] = None,
           note: str = "") -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Approve (EXP is given) or reject (they can redo it). Returns (run, error)."""
    if not can_review(reviewer):
        return None, "Only Mom can approve quests."
    run = get_run(run_id)
    if not run or run["status"] != "submitted":
        return None, "Nothing to review there."
    if run["user_id"] == reviewer:
        return None, "You can't approve your own quest."
    note = " ".join(str(note or "").split())[:NOTE_CHARS]
    exp = 0
    if approve and not run["penalty"]:
        exp = QUEST_RANKS.get(rank if rank in QUEST_RANKS else run["rank"], 0)
    with get_connection() as conn:
        conn.execute("UPDATE quest_runs SET status = ?, review_note = ?, exp = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?;",
                     ("approved" if approve else "rejected", note, exp, _now(), reviewer, run_id))
        conn.commit()
    if approve:
        if exp:
            lv.add_exp(run["user_id"], exp, "quest", str(run_id))
        activity_service.record("quest", f"completed the quest {run['title']} ✅ (+{exp} EXP)" if exp
                                else f"cleared the {run['title']} ✅", user_id=run["user_id"])
        if run["penalty"]:
            release_pending_rewards(run["user_id"])
    cs.record_event("quest_reviewed", {"run_id": run_id}, user_id=run["user_id"])
    return get_run(run_id), None


def pending_reviews() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT r.*, q.title, q.rank, q.category, q.penalty FROM quest_runs r JOIN quests q ON q.id = r.quest_id "
                            "WHERE r.status = 'submitted' ORDER BY r.submitted_at;").fetchall()
    return [dict(r) | {"who": cs.display_name(r["user_id"]), "exp": 0 if r["penalty"] else QUEST_RANKS.get(r["rank"], 0),
                       "has_proof": bool(r["proof"])} for r in rows]


def recent_reviews(limit: int = 20) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT r.*, q.title, q.rank FROM quest_runs r JOIN quests q ON q.id = r.quest_id "
                            "WHERE r.status IN ('approved', 'rejected') ORDER BY r.reviewed_at DESC LIMIT ?;", (limit,)).fetchall()
    return [dict(r) | {"who": cs.display_name(r["user_id"])} for r in rows]


def proof_file(viewer: int, run_id: int) -> Optional[Path]:
    """The submitter, reviewers and the owner can see a quest photo."""
    run = get_run(run_id)
    if not run or not run["proof"]:
        return None
    if viewer not in (run["user_id"], config.USER_ID) and not can_review(viewer):
        return None
    path = proofs_dir(run["user_id"]) / run["proof"]
    return path if path.is_file() else None


def all_quests() -> List[Dict[str, Any]]:
    """Every active quest with who it's for (moderator view)."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM quests WHERE active = 1 ORDER BY penalty DESC, id DESC;").fetchall()
    out = []
    for q in (dict(r) for r in rows):
        who = cs.display_name(q["assignee"]) if q["assignee"] else "Everyone"
        out.append({"id": q["id"], "title": q["title"], "category": q["category"], "rank": q["rank"], "repeat": q["repeat"],
                    "for": who, "penalty": bool(q["penalty"]),
                    "by": moderator_name(q["created_by"]) if is_moderator(q["created_by"]) else cs.display_name(q["created_by"])})
    return out


# ---------------------------------------------------------------------------
# Daily Quest and penalties
# ---------------------------------------------------------------------------

DQ_PARTS = [("pushups", "Push-ups", "push"), ("situps", "Sit-ups", "sit"), ("squats", "Squats", "squat")]


def dq_targets(level: int) -> Dict[str, Any]:
    """Grows from 20/20/20 + 1 km at level 1 to the original 100/100/100 + 10 km at level 50."""
    f = min(1.0, (level - 1) / 49)
    reps = int(round((20 + 80 * f) / 5) * 5)
    return {"pushups": reps, "situps": reps, "squats": reps, "run_km": round(1 + 9 * f, 1)}


def dq_enabled(user_id: int) -> bool:
    with as_user(user_id):
        return get_setting("dq_enabled", "1") == "1"


def _dq_row(user_id: int, date_str: str):
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO daily_quests (user_id, date) VALUES (?, ?);", (user_id, date_str))
        conn.commit()
        return conn.execute("SELECT * FROM daily_quests WHERE user_id = ? AND date = ?;", (user_id, date_str)).fetchone()


def _workout_reps(user_id: int, date_str: str) -> Dict[str, int]:
    from services.social_service import reps_on
    return {key: reps_on(user_id, date_str, match) for key, _, match in DQ_PARTS}


def open_penalty(user_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM quests WHERE assignee = ? AND penalty = 1 AND active = 1;", (user_id,)).fetchall()
    for q in (dict(r) for r in rows):
        with get_connection() as conn:
            done = conn.execute("SELECT 1 FROM quest_runs WHERE quest_id = ? AND status = 'approved';", (q["id"],)).fetchone()
        if not done:
            return q
    return None


def daily_quest(user_id: int, date_str: Optional[str] = None) -> Dict[str, Any]:
    date_str = date_str or _today(user_id)
    row = _dq_row(user_id, date_str)
    level = row["level"]
    if level is None:   # the day's targets are fixed at the level you started it with
        level = lv.level_of(user_id)
        with get_connection() as conn:
            conn.execute("UPDATE daily_quests SET level = ? WHERE user_id = ? AND date = ?;", (level, user_id, date_str))
            conn.commit()
    targets = dq_targets(level)
    from_workout = _workout_reps(user_id, date_str)
    parts = []
    for key, label, _ in DQ_PARTS:
        done = from_workout[key] + row[key]
        parts.append({"key": key, "label": label, "done": done, "target": targets[key]})
    run_km = round(row["run_m"] / 1000, 1)
    parts.append({"key": "run", "label": "Running", "done": run_km, "target": targets["run_km"], "unit": "km"})
    complete = bool(row["completed_at"]) or all(p["done"] >= p["target"] for p in parts)
    penalty = open_penalty(user_id)
    return {
        "date": date_str, "parts": parts, "complete": complete, "rewarded": bool(row["rewarded"]),
        "reward_exp": 100 + 5 * level, "enabled": dq_enabled(user_id),
        "penalty": quest_state(penalty, user_id, date_str) if penalty else None,
    }


def dq_log(user_id: int, part: str, amount: float) -> Optional[str]:
    """Adds reps (or km for the run). Reps go into today's workout when it has that exercise."""
    today = _today(user_id)
    if part == "run":
        meters = int(round(max(-50.0, min(50.0, float(amount))) * 1000))
        with get_connection() as conn:
            _dq_row(user_id, today)
            conn.execute("UPDATE daily_quests SET run_m = MAX(0, run_m + ?) WHERE user_id = ? AND date = ?;", (meters, user_id, today))
            conn.commit()
    else:
        match = next((m for k, _, m in DQ_PARTS if k == part), None)
        if not match:
            return "Unknown part."
        reps = int(max(-500, min(500, amount)))
        with as_user(user_id):
            items = get_or_create_daily_workout(today)["items"]
            item = next((i for i in items if match in i["exercise_name"].lower() and i["unit"] == "reps"), None)
            if item and today == get_current_date_str():
                update_workout_item(item["id"], delta_reps=reps)
            else:
                with get_connection() as conn:
                    _dq_row(user_id, today)
                    conn.execute(f"UPDATE daily_quests SET {part} = MAX(0, {part} + ?) WHERE user_id = ? AND date = ?;",
                                 (reps, user_id, today))
                    conn.commit()
    check_daily_quest(user_id)
    return None


def check_daily_quest(user_id: int, date_str: Optional[str] = None) -> bool:
    """Marks today's Daily Quest cleared and gives the reward (unless a penalty is open). True when newly cleared."""
    date_str = date_str or _today(user_id)
    dq = daily_quest(user_id, date_str)
    if not dq["complete"] or not dq["enabled"]:
        return False
    with get_connection() as conn:
        cur = conn.execute("UPDATE daily_quests SET completed_at = ? WHERE user_id = ? AND date = ? AND completed_at IS NULL;",
                           (_now(), user_id, date_str))
        conn.commit()
    newly = cur.rowcount > 0
    if newly:
        activity_service.record("dq", "cleared the Daily Quest ⚔️", user_id=user_id)
    release_pending_rewards(user_id)
    return newly


def release_pending_rewards(user_id: int) -> int:
    """Gives Daily Quest rewards that were waiting (no open penalty). Returns how many."""
    if open_penalty(user_id):
        return 0
    with get_connection() as conn:
        rows = conn.execute("SELECT date FROM daily_quests WHERE user_id = ? AND completed_at IS NOT NULL AND rewarded = 0;",
                            (user_id,)).fetchall()
        conn.execute("UPDATE daily_quests SET rewarded = 1 WHERE user_id = ? AND completed_at IS NOT NULL AND rewarded = 0;",
                     (user_id,))
        conn.commit()
    level = lv.level_of(user_id)
    for r in rows:
        lv.add_exp(user_id, 100 + 5 * level, "daily_quest", r["date"])
    return len(rows)


def _was_training_day(user_id: int, date_str: str) -> bool:
    with as_user(user_id):
        if is_date_paused(date_str):
            return False
    with get_connection() as conn:
        row = conn.execute("SELECT status FROM daily_workouts WHERE user_id = ? AND date = ?;", (user_id, date_str)).fetchone()
    return not row or row["status"] not in ("paused", "rest")


async def penalty_check(bot) -> Optional[int]:
    """Runs just after midnight (as each member): yesterday's Daily Quest missed -> Penalty Quest."""
    from views import esc
    uid = current_user_id()
    if not dq_enabled(uid) or open_penalty(uid):
        return None
    yesterday = (datetime.strptime(_today(uid), "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    if not _was_training_day(uid, yesterday):
        return None
    check_daily_quest(uid, yesterday)
    if daily_quest(uid, yesterday)["complete"]:
        return None
    title = random.choice(PENALTIES)
    quest_id, _ = create_quest(config.USER_ID, title, "fitness", "E", "once", assignee=uid, penalty=True)
    activity_service.record("penalty", f"failed the Daily Quest and got a {title} ☠️", user_id=uid)
    await cs.send_html(bot, uid, "🟥 <b>[System]</b>\n\nYou did not complete yesterday's Daily Quest.\n"
                                 f"<b>{esc(title)}</b> has been issued.\n\n"
                                 "Daily Quest rewards are locked until Mom approves it.")
    return quest_id


# ---------------------------------------------------------------------------
# Telegram messages (crew events)
# ---------------------------------------------------------------------------

def review_buttons(run_id: int):
    from telegram import InlineKeyboardButton
    from services.social_service import app_button
    rows = [[InlineKeyboardButton("✅ Approve", callback_data=f"quest_ok:{run_id}"),
             InlineKeyboardButton("❌ Reject", callback_data=f"quest_no:{run_id}")]]
    app = app_button("📜 Open quests", "quests")
    if app:
        rows.append([app])
    return rows


async def _announce_submitted(bot, event: Dict[str, Any]) -> None:
    from views import esc
    run = get_run(event["data"].get("run_id"))
    if not run or run["status"] != "submitted":
        return
    exp = 0 if run["penalty"] else QUEST_RANKS.get(run["rank"], 0)
    text = (f"📜 <b>{esc(cs.display_name(run['user_id']))}</b> says they completed:\n<b>{esc(run['title'])}</b>"
            + (f" ({run['rank']}-rank, +{exp} EXP)" if exp else "")
            + (f"\n💬 “{esc(run['note'])}”" if run["note"] else "")
            + ("\n📷 Photo proof attached (open the app to see it)." if run["proof"] else "")
            + "\n\nDid they really do it?")
    for uid in reviewers():
        if uid != run["user_id"]:
            await cs.send_html(bot, uid, text, review_buttons(run["id"]))


async def _announce_reviewed(bot, event: Dict[str, Any]) -> None:
    from views import esc
    run = get_run(event["data"].get("run_id"))
    if not run:
        return
    who = moderator_name(run["reviewed_by"]) if is_moderator(run["reviewed_by"]) else cs.display_name(run["reviewed_by"])
    if run["status"] == "approved":
        reward = f"\n\n<b>+{run['exp']} EXP</b>" if run["exp"] else ("\n\nPenalty cleared. Daily Quest rewards unlocked." if run["penalty"] else "")
        text = f"🔷 <b>[System]</b> Quest complete!\n<b>{esc(run['title'])}</b> was approved by {esc(who)}.{reward}"
    else:
        text = (f"❌ {esc(who)} didn't approve <b>{esc(run['title'])}</b>"
                + (f": “{esc(run['review_note'])}”" if run["review_note"] else ".") + "\nDo it properly and send it again.")
    await cs.send_html(bot, run["user_id"], text)


async def _announce_new(bot, event: Dict[str, Any]) -> None:
    from views import esc
    q = get_quest(event["data"].get("quest_id"))
    if not q or q["penalty"]:
        return
    creator = q["created_by"]
    if is_moderator(creator) or creator == config.USER_ID:
        targets = [q["assignee"]] if q["assignee"] else _crew_ids()
        who = moderator_name(creator) if is_moderator(creator) else cs.display_name(creator)
        for uid in targets:
            if uid != creator:
                await cs.send_html(bot, uid, f"📜 <b>[System] New quest</b> from {esc(who)}:\n<b>{esc(q['title'])}</b> "
                                             f"({q['rank']}-rank, +{QUEST_RANKS.get(q['rank'], 0)} EXP, {q['repeat']}).")
    else:
        for uid in reviewers():
            if uid != creator:
                await cs.send_html(bot, uid, f"📜 <b>{esc(cs.display_name(creator))}</b> added a quest for themselves: "
                                             f"<b>{esc(q['title'])}</b> ({q['rank']}-rank). You'll approve it when they finish.")


cs.EVENT_HANDLERS.update({
    "quest_submitted": _announce_submitted,
    "quest_reviewed": _announce_reviewed,
    "quest_new": _announce_new,
})


async def notify_moderators_only(bot, text: str) -> None:
    for m in moderators():
        await cs.send_html(bot, m["user_id"], text)


def moderator_state(user_id: int) -> Dict[str, Any]:
    """Everything the moderator's app shows."""
    hunters = []
    for m in get_users():
        uid = m["user_id"]
        st = lv.status(uid)
        snap = cs.day_snapshot(uid)
        dq = daily_quest(uid)
        hunters.append({
            "id": uid, "name": cs.display_name(uid), "level": st["level"], "rank": st["rank"],
            "into": st["into"], "needed": st["needed"], "title": st["title"], "job": st["job"],
            "today": {"status": snap["status"], "done": snap["done"], "total": snap["total"], "reps": snap["reps"]},
            "streak": snap["streak"], "dq_complete": dq["complete"], "penalty": dq["penalty"]["title"] if dq["penalty"] else None,
        })
    return {
        "role": "moderator",
        "me": {"id": user_id, "name": moderator_name(user_id)},
        "hunters": hunters, "pending": pending_reviews(), "quests": all_quests(), "recent": recent_reviews(),
        "templates": [{"title": t, "category": c, "rank": r, "repeat": rp} for t, c, r, rp in TEMPLATES],
        "ranks": [{"rank": r, "exp": e} for r, e in QUEST_RANKS.items()],
        "sig": _mod_sig(),
    }


def _mod_sig() -> str:
    with get_connection() as conn:
        a = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(MAX(submitted_at), '') FROM quest_runs;").fetchone()
        b = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM quests WHERE active = 1;").fetchone()
    return json.dumps([list(a), list(b)])
