"""Doing it together: crew chat, live training, proof clips, bets, overtake
alerts and the monthly report card.

Like the rest of the crew code, nothing here calls Telegram directly from the
workout logic: services record crew events, and the event handlers at the
bottom send the messages (see crew_service.deliver_events).
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from database import as_user, current_user_id, get_connection, get_setting, get_users, set_setting
from services import activity_service
from services import crew_service as cs
from services.workout_service import get_current_date_str, get_or_create_daily_workout

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def app_button(text: str, screen: str = ""):
    """A button that opens the Mini App (on a screen), or None when the app is off."""
    if not config.WEBAPP_ENABLED:
        return None
    from telegram import InlineKeyboardButton, WebAppInfo
    url = config.WEBAPP_URL + (("&" if "?" in config.WEBAPP_URL else "?") + f"screen={screen}" if screen else "")
    return InlineKeyboardButton(text, web_app=WebAppInfo(url=url))


def _buttons(*buttons) -> Optional[List[List[Any]]]:
    rows = [[b] for b in buttons if b is not None]
    return rows or None


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

CHAT_MAX_CHARS = 300
STICKERS = {"flex": "💪", "fire": "🔥", "sleepy": "😴", "smirk": "😏", "cry": "😭", "rage": "😤"}
QUICK_REPLIES = ["Lazy? 😴", "Watch this 😤", "Your move 👀", "GG 🤝", "Catch me if you can 🏃", "Not today 🛑"]
# No Telegram ping for chat when the person had the app open this recently.
IN_APP_SECONDS = 120
CHAT_PING_GAP_SECONDS = 120
_last_ping: Dict[Tuple[int, int], float] = {}


def send_chat(text: Optional[str] = None, sticker: Optional[str] = None,
              proof_id: Optional[int] = None) -> Tuple[Optional[int], Optional[str]]:
    """Posts to the crew chat as the current user. Returns (message id, error)."""
    if len(get_users()) < 2:
        return None, "Invite your brother first: the chat needs two."
    if proof_id is not None:
        kind, body, data = "proof", "", {"proof_id": proof_id}
    elif sticker:
        if sticker not in STICKERS:
            return None, "Unknown sticker."
        kind, body, data = "sticker", sticker, None
    else:
        body = " ".join((text or "").split())[:CHAT_MAX_CHARS]
        if not body:
            return None, "Type a message first."
        kind, data = "text", None
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO chat_messages (user_id, kind, text, data, created_at) VALUES (?, ?, ?, ?, ?);",
                           (current_user_id(), kind, body, json.dumps(data) if data else None, _now().isoformat()))
        conn.commit()
        message_id = cur.lastrowid
    set_setting("chat_seen", str(message_id))
    cs.record_event("chat", {"message_id": message_id})
    return message_id, None


def chat_messages(limit: int = 60) -> List[Dict[str, Any]]:
    me = current_user_id()
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?;", (limit,)).fetchall()
    names = {u["user_id"]: cs.display_name(u["user_id"]) for u in get_users()}
    out = []
    for r in reversed(rows):
        data = json.loads(r["data"]) if r["data"] else {}
        msg = {"id": r["id"], "user_id": r["user_id"], "who": names.get(r["user_id"], "Someone"),
               "me": r["user_id"] == me, "kind": r["kind"], "text": r["text"], "at": r["created_at"]}
        if r["kind"] == "proof":
            msg["proof"] = proof_info(data.get("proof_id"))
        out.append(msg)
    return out


def unread_count(user_id: Optional[int] = None) -> int:
    uid = current_user_id() if user_id is None else user_id
    with as_user(uid):
        seen = int(get_setting("chat_seen", "0") or 0)
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM chat_messages WHERE id > ? AND user_id != ?;",
                            (seen, uid)).fetchone()[0]


def mark_chat_seen(message_id: int) -> None:
    if message_id > int(get_setting("chat_seen", "0") or 0):
        set_setting("chat_seen", str(message_id))


def in_app(user_id: int) -> bool:
    seen = _parse(cs.user_setting(user_id, "app_seen", ""))
    return bool(seen and (_now() - seen).total_seconds() < IN_APP_SECONDS)


def touch_app_seen() -> None:
    """Remembers the app is open (written at most every 30 s)."""
    seen = _parse(get_setting("app_seen", ""))
    if not seen or (_now() - seen).total_seconds() > 30:
        set_setting("app_seen", _now().isoformat())


async def _announce_chat(bot, event: Dict[str, Any]) -> None:
    from views import esc
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM chat_messages WHERE id = ?;", (event["data"].get("message_id"),)).fetchone()
    if not row:
        return
    sender = row["user_id"]
    name = esc(cs.display_name(sender))
    if row["kind"] == "sticker":
        line = f"💬 <b>{name}</b>: {STICKERS.get(row['text'], '')}"
    elif row["kind"] == "proof":
        line = f"🎥 <b>{name}</b> posted a proof clip. Legit or cap? Rate it in the app."
    else:
        line = f"💬 <b>{name}</b>: {esc(row['text'])}"
    for member in cs.others(sender):
        uid = member["user_id"]
        if cs.user_setting(uid, "crew_feed", "1") != "1" or in_app(uid):
            continue
        if time.monotonic() - _last_ping.get((sender, uid), -1e9) < CHAT_PING_GAP_SECONDS and row["kind"] != "proof":
            continue
        _last_ping[(sender, uid)] = time.monotonic()
        await cs.send_html(bot, uid, line, _buttons(app_button("💬 Open chat", "chat")))


# ---------------------------------------------------------------------------
# Live training
# ---------------------------------------------------------------------------

LIVE_MINUTES = 90          # a live session ends by itself after this long
LIVE_PING_GAP_MINUTES = 30


def is_live(user_id: int) -> bool:
    since = _parse(cs.user_setting(user_id, "live_since", ""))
    return bool(since and _now() - since < timedelta(minutes=LIVE_MINUTES))


def rest_left(user_id: int) -> int:
    until = _parse(cs.user_setting(user_id, "rest_until", ""))
    return max(0, int((until - _now()).total_seconds())) if until else 0


def live_start() -> None:
    me = current_user_id()
    previous = _parse(get_setting("live_since", ""))
    set_setting("live_since", _now().isoformat())
    if not previous or _now() - previous > timedelta(minutes=LIVE_PING_GAP_MINUTES):
        activity_service.record("live", "started training live 🔴")
        cs.record_event("live", {}, user_id=me)


def live_stop() -> None:
    set_setting("live_since", "")
    set_setting("rest_until", "")


def start_rest(seconds: int) -> None:
    set_setting("rest_until", (_now() + timedelta(seconds=max(0, min(600, seconds)))).isoformat())


async def _announce_live(bot, event: Dict[str, Any]) -> None:
    from views import esc
    name = esc(cs.display_name(event["user_id"]))
    for member in cs.others(event["user_id"]):
        uid = member["user_id"]
        if cs.user_setting(uid, "crew_feed", "1") != "1" or is_live(uid):
            continue
        await cs.send_html(bot, uid, f"🔴 <b>{name}</b> is training live right now. Join in and race them!",
                           _buttons(app_button("⚡ Train live", "live")))


def fine_progress(items: List[Dict[str, Any]], status: str) -> float:
    if status == "completed":
        return 1.0
    parts = [min(1.0, it["completed_reps"] / it["target_reps"]) if it["target_reps"] else 1.0
             for it in items if it["status"] != "skipped"]
    return round(sum(parts) / len(parts), 3) if parts else 0.0


def pulse() -> Dict[str, Any]:
    """The small, frequently polled snapshot: who's training, progress, new chat.
    `sig` changes whenever something visible changed, so the app knows to reload."""
    me = current_user_id()
    touch_app_seen()
    today = get_current_date_str()
    crew = []
    for m in get_users():
        uid = m["user_id"]
        with as_user(uid):
            workout = get_or_create_daily_workout(today)
        items = workout["items"]
        crew.append({
            "id": uid, "status": workout["status"],
            "progress": fine_progress(items, workout["status"]),
            "reps": sum(it["completed_reps"] for it in items if it["unit"] == "reps"),
            "items": [{"name": it["exercise_name"], "done": it["completed_reps"], "target": it["target_reps"]}
                      for it in items],
            "live": is_live(uid), "rest_left": rest_left(uid), "in_app": in_app(uid) if uid != me else True,
        })
    with get_connection() as conn:
        chat_last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM chat_messages;").fetchone()[0]
        activity_last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity;").fetchone()[0]
        bets_sig = conn.execute("SELECT COUNT(*) || '-' || COALESCE(MAX(id), 0) || '-' || "
                                "COALESCE(SUM(status IN ('offered', 'accepted')), 0) FROM bets;").fetchone()[0]
        votes = conn.execute("SELECT COUNT(*) FROM proof_votes;").fetchone()[0]
    sig_parts = [chat_last, activity_last, bets_sig, votes] + [
        f"{c['id']}:{c['progress']}:{c['status']}:{c['live']}" for c in crew]
    return {"sig": "|".join(str(p) for p in sig_parts), "crew": crew,
            "chat_last": chat_last, "unread": unread_count(me)}


# ---------------------------------------------------------------------------
# Proof clips
# ---------------------------------------------------------------------------

MAX_PROOF_BYTES = 20 * 1024 * 1024
PROOF_KEEP_DAYS = 30
PROOF_POINTS = {"legit": 3, "cap": -5}   # only one legit proof a day earns points
VOTES = ("legit", "cap")
MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "mp4": "video/mp4", "mov": "video/quicktime",
               "webm": "video/webm"}


def proofs_dir(user_id: Optional[int] = None) -> Path:
    return config.DATA_DIR / "proofs" / str(current_user_id() if user_id is None else user_id)


def _sniff(data: bytes) -> Optional[str]:
    """File type from its first bytes (never from the name or a header the client sends)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if data[4:8] == b"ftyp":
        return "mov" if data[8:10] == b"qt" else "mp4"
    return None


def save_proof(data: bytes) -> Dict[str, Any]:
    if not data or len(data) > MAX_PROOF_BYTES:
        return {"ok": False, "message": "Clip must be smaller than 20 MB. Keep it to about 5 seconds."}
    ext = _sniff(data)
    if not ext:
        return {"ok": False, "message": "Only videos (MP4, MOV, WebM) or photos (JPEG, PNG)."}
    if len(get_users()) < 2:
        return {"ok": False, "message": "Invite your brother first: proofs need a judge."}
    folder = proofs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    (folder / filename).write_bytes(data)
    media = "video" if ext in ("mp4", "mov", "webm") else "photo"
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO proof_clips (user_id, date, filename, media, created_at) VALUES (?, ?, ?, ?, ?);",
                           (current_user_id(), get_current_date_str(), filename, media, _now().isoformat()))
        conn.commit()
        proof_id = cur.lastrowid
    send_chat(proof_id=proof_id)
    activity_service.record("proof", f"posted a proof {'clip 🎥' if media == 'video' else 'photo 📸'}")
    return {"ok": True, "message": "Proof posted. Now let them judge it.", "id": proof_id}


def _proof_row(proof_id: Optional[int]):
    if proof_id is None:
        return None
    with get_connection() as conn:
        return conn.execute("SELECT * FROM proof_clips WHERE id = ?;", (proof_id,)).fetchone()


def proof_votes(proof_id: int) -> Dict[str, Any]:
    with get_connection() as conn:
        rows = conn.execute("SELECT user_id, vote FROM proof_votes WHERE proof_id = ?;", (proof_id,)).fetchall()
    counts = {v: sum(1 for r in rows if r["vote"] == v) for v in VOTES}
    verdict = "legit" if counts["legit"] > counts["cap"] else "cap" if counts["cap"] > counts["legit"] else None
    return {"counts": counts, "verdict": verdict,
            "mine": next((r["vote"] for r in rows if r["user_id"] == current_user_id()), None)}


def proof_info(proof_id: Optional[int]) -> Optional[Dict[str, Any]]:
    row = _proof_row(proof_id)
    if not row:
        return None
    return {"id": row["id"], "media": row["media"], "available": bool(row["filename"]),
            "mine": row["user_id"] == current_user_id(), **proof_votes(row["id"])}


def proof_file(proof_id: int) -> Optional[Tuple[Path, str]]:
    """Any crew member may watch a proof (that's the point); outsiders never reach this."""
    row = _proof_row(proof_id)
    if not row or not row["filename"]:
        return None
    path = proofs_dir(row["user_id"]) / row["filename"]
    ext = row["filename"].rsplit(".", 1)[-1]
    return (path, MEDIA_TYPES.get(ext, "application/octet-stream")) if path.is_file() else None


def vote_proof(proof_id: int, vote: str) -> Optional[str]:
    """Rates someone else's proof (tap the same vote again to take it back). Returns an error or None."""
    row = _proof_row(proof_id)
    if not row:
        return "That proof is gone."
    if vote not in VOTES:
        return "Vote legit or cap."
    me = current_user_id()
    if row["user_id"] == me:
        return "You can't judge your own proof 😅"
    with get_connection() as conn:
        current = conn.execute("SELECT vote FROM proof_votes WHERE proof_id = ? AND user_id = ?;", (proof_id, me)).fetchone()
        if current and current["vote"] == vote:
            conn.execute("DELETE FROM proof_votes WHERE proof_id = ? AND user_id = ?;", (proof_id, me))
        else:
            conn.execute("INSERT INTO proof_votes (proof_id, user_id, vote) VALUES (?, ?, ?) "
                         "ON CONFLICT(proof_id, user_id) DO UPDATE SET vote = excluded.vote;", (proof_id, me, vote))
        conn.commit()
    return None


def proof_points(user_id: int, start: str, end: str) -> int:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, date FROM proof_clips WHERE user_id = ? AND date >= ? AND date <= ? ORDER BY id;",
                            (user_id, start, end)).fetchall()
    total, legit_days = 0, set()
    with as_user(user_id):
        for r in rows:
            verdict = proof_votes(r["id"])["verdict"]
            if verdict == "cap":
                total += PROOF_POINTS["cap"]
            elif verdict == "legit" and r["date"] not in legit_days:
                legit_days.add(r["date"])
                total += PROOF_POINTS["legit"]
    return total


def cleanup_proofs(now: Optional[datetime] = None) -> int:
    """Deletes proof files older than PROOF_KEEP_DAYS (the votes and points stay)."""
    cutoff = ((now or _now()) - timedelta(days=PROOF_KEEP_DAYS)).isoformat()
    with get_connection() as conn:
        rows = conn.execute("SELECT id, user_id, filename FROM proof_clips WHERE filename IS NOT NULL AND created_at < ?;",
                            (cutoff,)).fetchall()
        for r in rows:
            (proofs_dir(r["user_id"]) / r["filename"]).unlink(missing_ok=True)
            conn.execute("UPDATE proof_clips SET filename = NULL WHERE id = ?;", (r["id"],))
        conn.commit()
    return len(rows)


async def cleanup_proofs_job(bot=None) -> None:
    removed = cleanup_proofs()
    if removed:
        logger.info("Removed %s old proof clip files.", removed)


# ---------------------------------------------------------------------------
# Bets
# ---------------------------------------------------------------------------

BET_KINDS = {
    "reps": "More total reps today",
    "push": "More push-ups today",
    "first": "Finish today's workout first",
}
STAKES = (5, 10, 20)
BETS_PER_DAY = 3


def get_bet(bet_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM bets WHERE id = ?;", (bet_id,)).fetchone()
    return dict(row) if row else None


def offer_bet(challenger: int, target: int, kind: str, stake: int,
              date_str: Optional[str] = None) -> Tuple[Optional[int], Optional[str]]:
    if kind not in BET_KINDS or stake not in STAKES:
        return None, "Pick a bet and a stake."
    if challenger == target or target not in [u["user_id"] for u in get_users()]:
        return None, "Bet against someone in your crew."
    date_str = date_str or get_current_date_str()
    with get_connection() as conn:
        today_count = conn.execute("SELECT COUNT(*) FROM bets WHERE challenger = ? AND date = ?;",
                                   (challenger, date_str)).fetchone()[0]
        same = conn.execute("SELECT COUNT(*) FROM bets WHERE date = ? AND kind = ? AND status IN ('offered', 'accepted') "
                            "AND ((challenger = ? AND target = ?) OR (challenger = ? AND target = ?));",
                            (date_str, kind, challenger, target, target, challenger)).fetchone()[0]
        if today_count >= BETS_PER_DAY:
            return None, f"That's {BETS_PER_DAY} bets today. Save some for tomorrow."
        if same:
            return None, "There's already a bet like that today."
        cur = conn.execute("INSERT INTO bets (date, challenger, target, kind, stake, created_at) VALUES (?, ?, ?, ?, ?, ?);",
                           (date_str, challenger, target, kind, stake, _now().isoformat()))
        conn.commit()
        bet_id = cur.lastrowid
    cs.record_event("bet_offer", {"bet_id": bet_id}, user_id=challenger)
    return bet_id, None


def respond_bet(bet_id: int, by_user: int, accept: bool) -> Optional[str]:
    bet = get_bet(bet_id)
    if not bet or bet["target"] != by_user or bet["status"] != "offered":
        return "That bet isn't open any more."
    _set_bet(bet_id, "accepted" if accept else "declined")
    verb = "accepted" if accept else "backed out of"
    activity_service.record("bet", f"{verb} a {bet['stake']}-pt bet: {BET_KINDS[bet['kind']]} 🎲", user_id=by_user)
    return None


def _set_bet(bet_id: int, status: str, winner: Optional[int] = None) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE bets SET status = ?, winner = ? WHERE id = ?;", (status, winner, bet_id))
        conn.commit()


def reps_on(user_id: int, date_str: str, name_contains: str = "") -> int:
    """Reps logged on a day, read-only (never creates a workout for that day)."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(wi.completed_reps), 0) FROM workout_items wi JOIN daily_workouts dw "
            "ON dw.id = wi.daily_workout_id WHERE dw.user_id = ? AND dw.date = ? AND wi.unit = 'reps' "
            "AND LOWER(wi.exercise_name) LIKE ?;", (user_id, date_str, f"%{name_contains}%")).fetchone()[0]


def _bet_score(user_id: int, kind: str, date_str: str) -> int:
    return reps_on(user_id, date_str, "push" if kind == "push" else "")


def _finish_time(user_id: int, date_str: str) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT completed_at FROM daily_workouts WHERE user_id = ? AND date = ? AND status = 'completed';",
                           (user_id, date_str)).fetchone()
    return row["completed_at"] if row else None


def bet_winner(bet: Dict[str, Any], final: bool) -> Tuple[bool, Optional[int]]:
    """(decided, winner). winner None with decided True is a draw."""
    a, b = bet["challenger"], bet["target"]
    if bet["kind"] == "first":
        ta, tb = _finish_time(a, bet["date"]), _finish_time(b, bet["date"])
        if ta and tb:
            return True, a if ta <= tb else b
        if ta or tb:
            return True, a if ta else b
        return (True, None) if final else (False, None)
    if not final:
        return False, None
    sa, sb = _bet_score(a, bet["kind"], bet["date"]), _bet_score(b, bet["kind"], bet["date"])
    return True, (a if sa > sb else b if sb > sa else None)


async def _settle(bot, bet: Dict[str, Any], winner: Optional[int]) -> None:
    from views import esc
    label = BET_KINDS[bet["kind"]]
    if winner is None:
        _set_bet(bet["id"], "draw")
        for uid in (bet["challenger"], bet["target"]):
            await cs.send_html(bot, uid, f"🤝 <b>Bet is a draw:</b> {label}. Nobody pays.")
        return
    loser = bet["target"] if winner == bet["challenger"] else bet["challenger"]
    _set_bet(bet["id"], "won", winner)
    activity_service.record("bet", f"won a {bet['stake']}-pt bet: {label} 🎲💰", user_id=winner)
    await cs.send_html(bot, winner, f"💰 <b>Bet won!</b> {label}. +{bet['stake']} pts from "
                                    f"{esc(cs.display_name(loser))}.")
    await cs.send_html(bot, loser, f"💸 <b>Bet lost:</b> {label}. {esc(cs.display_name(winner))} "
                                   f"takes {bet['stake']} of your points.")


async def check_bets(bot) -> None:
    """Every tick: settles 'finish first' bets as soon as someone finished."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM bets WHERE status = 'accepted' AND kind = 'first';").fetchall()
    for bet in (dict(r) for r in rows):
        decided, winner = bet_winner(bet, final=False)
        if decided:
            await _settle(bot, bet, winner)


cs.TICK_HOOKS.append(check_bets)


async def close_bets(bot) -> None:
    """23:00 (run as each member): settles their accepted bets, expires unanswered ones."""
    me = current_user_id()
    today = get_current_date_str()
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM bets WHERE target = ? AND date <= ? AND status IN ('offered', 'accepted');",
                            (me, today)).fetchall()
    for bet in (dict(r) for r in rows):
        if bet["status"] == "offered":
            _set_bet(bet["id"], "expired")
            continue
        _, winner = bet_winner(bet, final=True)
        await _settle(bot, bet, winner)


def bet_points(user_id: int, start: str, end: str) -> int:
    with get_connection() as conn:
        rows = conn.execute("SELECT stake, winner FROM bets WHERE status = 'won' AND date >= ? AND date <= ? "
                            "AND (challenger = ? OR target = ?);", (start, end, user_id, user_id)).fetchall()
    return sum(r["stake"] if r["winner"] == user_id else -r["stake"] for r in rows)


def bets_for(user_id: int, date_str: Optional[str] = None) -> List[Dict[str, Any]]:
    date_str = date_str or get_current_date_str()
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM bets WHERE date = ? AND (challenger = ? OR target = ?) ORDER BY id DESC;",
                            (date_str, user_id, user_id)).fetchall()
    out = []
    for b in (dict(r) for r in rows):
        rival = b["target"] if b["challenger"] == user_id else b["challenger"]
        out.append({
            "id": b["id"], "kind": b["kind"], "label": BET_KINDS[b["kind"]], "stake": b["stake"], "status": b["status"],
            "rival": cs.display_name(rival), "mine_to_answer": b["target"] == user_id and b["status"] == "offered",
            "i_offered": b["challenger"] == user_id,
            "result": None if b["status"] != "won" else ("won" if b["winner"] == user_id else "lost"),
            "score": {"me": _bet_score(user_id, b["kind"], date_str), "rival": _bet_score(rival, b["kind"], date_str)}
            if b["kind"] != "first" else None,
        })
    return out


async def _announce_bet(bot, event: Dict[str, Any]) -> None:
    from views import esc
    bet = get_bet(event["data"].get("bet_id"))
    if not bet or bet["status"] != "offered":
        return
    await cs.send_html(bot, bet["target"],
                       f"🎲 <b>{esc(cs.display_name(bet['challenger']))}</b> bets <b>{bet['stake']} pts</b>: "
                       f"{BET_KINDS[bet['kind']]}. Accept or back out in the app (open until 23:00).",
                       _buttons(app_button("🎲 Answer the bet", "duel")))


# ---------------------------------------------------------------------------
# Overtake alerts
# ---------------------------------------------------------------------------

OVERTAKE_ALERTS_PER_DAY = 3


def check_overtake(gained: int, date_str: str) -> None:
    """After the current user logged reps: did they just pass someone today?"""
    if gained <= 0 or date_str != get_current_date_str():
        return
    me = current_user_id()
    mine = reps_on(me, date_str)
    before = mine - gained
    for member in cs.others(me):
        theirs = reps_on(member["user_id"], date_str)
        if theirs <= 0 or not before <= theirs < mine:
            continue
        if _overtakes_today(me, member["user_id"], date_str) >= OVERTAKE_ALERTS_PER_DAY:
            continue
        cs.record_event("overtake", {"victim": member["user_id"], "date": date_str, "mine": mine, "theirs": theirs})
        activity_service.record("overtake", f"overtook {cs.display_name(member['user_id'])} ⚡ {mine} vs {theirs} reps")


def _overtakes_today(passer: int, victim: int, date_str: str) -> int:
    with get_connection() as conn:
        rows = conn.execute("SELECT data FROM crew_events WHERE user_id = ? AND kind = 'overtake';", (passer,)).fetchall()
    return sum(1 for r in rows if (d := json.loads(r["data"] or "{}")).get("victim") == victim and d.get("date") == date_str)


async def _announce_overtake(bot, event: Dict[str, Any]) -> None:
    from telegram import InlineKeyboardButton
    from views import esc
    data = event["data"]
    victim = data["victim"]
    if cs.user_setting(victim, "crew_feed", "1") != "1":
        return
    name = esc(cs.display_name(event["user_id"]))
    revenge = app_button("🔥 Revenge", "today") or InlineKeyboardButton("🔥 Revenge", callback_data="refresh_today")
    await cs.send_html(bot, victim, f"⚡ <b>{name}</b> just passed you: <b>{data['mine']}</b> vs "
                                    f"<b>{data['theirs']}</b> reps today. Are you letting that slide?", [[revenge]])


cs.EVENT_HANDLERS.update({
    "chat": _announce_chat,
    "live": _announce_live,
    "bet_offer": _announce_bet,
    "overtake": _announce_overtake,
})


# ---------------------------------------------------------------------------
# Monthly report card
# ---------------------------------------------------------------------------

def report_card(user_id: int, month: str) -> Dict[str, Any]:
    """One member's month in numbers (for the app's report card and the 1st-of-month message)."""
    from services import compete_service as cmp
    from services import progression_service as prog

    start, end = prog.month_bounds(month)
    with get_connection() as conn:
        days = conn.execute("SELECT id, date, status FROM daily_workouts WHERE user_id = ? AND date >= ? AND date <= ? "
                            "ORDER BY date;", (user_id, start, end)).fetchall()
        reps = conn.execute(
            "SELECT COALESCE(SUM(wi.completed_reps), 0) FROM workout_items wi JOIN daily_workouts dw "
            "ON dw.id = wi.daily_workout_id WHERE dw.user_id = ? AND dw.date >= ? AND dw.date <= ? AND wi.unit = 'reps';",
            (user_id, start, end)).fetchone()[0]
        best_day = conn.execute(
            "SELECT dw.date, SUM(wi.completed_reps) AS total FROM workout_items wi JOIN daily_workouts dw "
            "ON dw.id = wi.daily_workout_id WHERE dw.user_id = ? AND dw.date >= ? AND dw.date <= ? AND wi.unit = 'reps' "
            "GROUP BY dw.date ORDER BY total DESC LIMIT 1;", (user_id, start, end)).fetchone()
        weights = conn.execute("SELECT weight FROM body_logs WHERE user_id = ? AND date >= ? AND date <= ? "
                               "AND weight IS NOT NULL ORDER BY date;", (user_id, start, end)).fetchall()

    completed = [d["date"] for d in days if d["status"] == "completed"]
    best_run = run = 0
    previous = None
    for d in completed:
        day = datetime.strptime(d, "%Y-%m-%d").date()
        run = run + 1 if previous and day - previous == timedelta(days=1) else 1
        best_run, previous = max(best_run, run), day

    champion = prog.season_champion(month)
    points = cmp.points_breakdown(user_id, start, end)["total"]
    with as_user(user_id):
        rank = prog.rank_for(prog.xp(user_id))
        gear = prog.equipped(user_id)
    return {
        "month": month, "name": cs.display_name(user_id),
        "workouts": len(completed), "planned": len(days), "reps": reps,
        "best_streak": best_run, "best_day": {"date": best_day["date"], "reps": best_day["total"]} if best_day else None,
        "points": points, "rank": rank["letter"], "xp": rank["xp"], "gear": gear,
        "weight_change": round(weights[-1]["weight"] - weights[0]["weight"], 1) if len(weights) > 1 else None,
        "champion": cs.display_name(champion) if champion else None, "is_champion": champion == user_id,
    }


async def send_report_cards(bot, today_str: Optional[str] = None) -> int:
    """On the 1st: everyone gets last month's report card."""
    from services import progression_service as prog
    from views import card, esc

    month = prog.finished_months(today_str or get_current_date_str(), back=1)[0]
    sent = 0
    for m in get_users():
        r = report_card(m["user_id"], month)
        if not r["planned"]:
            continue
        lines = [
            f"🏋️ {r['workouts']} of {r['planned']} workouts · {r['reps']} reps",
            f"🔥 best streak {r['best_streak']} days · ⭐ {r['points']} pts · rank {r['rank']}",
        ]
        if r["weight_change"] is not None:
            lines.append(f"⚖️ weight {r['weight_change']:+.1f} kg")
        if r["champion"]:
            lines.append(f"👑 champion: {esc(r['champion'])}")
        title = datetime.strptime(month + "-15", "%Y-%m-%d").strftime("%B")
        await cs.send_html(bot, m["user_id"], f"📊 <b>Your {title} report card</b>\n{card(lines)}",
                           _buttons(app_button("📊 See the full card", "report")))
        sent += 1
    return sent
