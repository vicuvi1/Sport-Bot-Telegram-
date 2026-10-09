"""The System (Solo Leveling): levels, EXP, ranks, stats, titles and job change.

EXP = 5 x every weekly point ever earned (workouts, wake-ups, challenges...)
    + the EXP ledger (approved quests, Daily Quest rewards).
So history counts from day one and nothing can drift out of sync.

Levels follow a long curve (level 10 in a few weeks, level 50 in about a year
and a half of daily training, 100 is a lifetime). Every level gives 5 stat
points, every cleared Daily Quest 1 more; you spend them on STR, AGI, VIT,
INT and PER in the Status window.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from database import as_user, current_user_id, get_connection, get_setting, get_users, set_setting
from services import crew_service as cs

EXP_PER_POINT = 5
MAX_LEVEL = 150
POINTS_PER_LEVEL = 5
BASE_STAT = 10
STATS = [("str", "STR", "Strength"), ("agi", "AGI", "Agility"), ("vit", "VIT", "Vitality"),
         ("int", "INT", "Intelligence"), ("per", "PER", "Perception")]

# (first level, letter, name). "N" is National Level.
RANKS = [(1, "E", "E-Rank"), (10, "D", "D-Rank"), (20, "C", "C-Rank"), (35, "B", "B-Rank"),
         (50, "A", "A-Rank"), (70, "S", "S-Rank"), (90, "N", "National Level")]
JOB_CHANGE_LEVEL = 40
JOBS = [
    ("fighter", "Fighter", "str", "Hits harder than he talks."),
    ("assassin", "Assassin", "agi", "Fast, quiet, done before you noticed."),
    ("tank", "Tank", "vit", "Never misses a day. Never breaks."),
    ("mage", "Mage", "int", "Trains the mind as hard as the body."),
    ("ranger", "Ranger", "per", "Sees everything coming."),
    ("necromancer", "Necromancer", None, "The hidden class. 40 Daily Quests cleared."),
]


def exp_to_next(level: int) -> int:
    return int(round(80 * level ** 1.3))


def level_info(total: int) -> Dict[str, Any]:
    level, floor = 1, 0
    while level < MAX_LEVEL and total >= floor + exp_to_next(level):
        floor += exp_to_next(level)
        level += 1
    need = exp_to_next(level)
    return {"level": level, "exp": total, "into": total - floor, "needed": need, "floor": floor}


def rank_for_level(level: int) -> Dict[str, Any]:
    current, nxt = RANKS[0], None
    for i, r in enumerate(RANKS):
        if level >= r[0]:
            current, nxt = r, (RANKS[i + 1] if i + 1 < len(RANKS) else None)
    return {"letter": current[1], "name": current[2], "index": RANKS.index(current),
            "next_letter": nxt[1] if nxt else None, "next_level": nxt[0] if nxt else None}


def rank_index(letter: str) -> int:
    letters = [r[1] for r in RANKS]
    return letters.index(letter) if letter in letters else 0


# ---------------------------------------------------------------------------
# EXP
# ---------------------------------------------------------------------------

def add_exp(user_id: int, amount: int, source: str, ref: Optional[str] = None) -> None:
    with get_connection() as conn:
        conn.execute("INSERT INTO exp_log (user_id, amount, source, ref, created_at) VALUES (?, ?, ?, ?, ?);",
                     (user_id, amount, source, ref, datetime.now(timezone.utc).isoformat()))
        conn.commit()


def ledger_exp(user_id: int) -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COALESCE(SUM(amount), 0) FROM exp_log WHERE user_id = ?;", (user_id,)).fetchone()[0]


def total_exp(user_id: Optional[int] = None) -> int:
    from services import compete_service as cmp
    uid = current_user_id() if user_id is None else user_id
    points = cmp.points_breakdown(uid, "0000-01-01", "9999-12-31")["total"]
    return max(0, EXP_PER_POINT * points + ledger_exp(uid))


def level_of(user_id: Optional[int] = None) -> int:
    return level_info(total_exp(user_id))["level"]


# ---------------------------------------------------------------------------
# Stats, titles, job
# ---------------------------------------------------------------------------

def daily_quests_cleared(user_id: int) -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND rewarded = 1;", (user_id,)).fetchone()[0]


def _allocated(user_id: int) -> Dict[str, int]:
    with as_user(user_id):
        return {key: int(get_setting(f"stat_{key}", "0") or 0) for key, _, _ in STATS}


def free_points(user_id: int, level: Optional[int] = None) -> int:
    level = level_of(user_id) if level is None else level
    earned = POINTS_PER_LEVEL * (level - 1) + daily_quests_cleared(user_id)
    return max(0, earned - sum(_allocated(user_id).values()))


def allocate(stat: str, amount: int = 1) -> Optional[str]:
    """Spends free stat points on one stat (current user). Returns an error or None."""
    uid = current_user_id()
    if stat not in [k for k, _, _ in STATS]:
        return "Unknown stat."
    amount = max(1, min(50, int(amount)))
    if free_points(uid) < amount:
        return "Not enough stat points. Level up or clear the Daily Quest."
    set_setting(f"stat_{stat}", str(_allocated(uid)[stat] + amount))
    return None


def _counts(user_id: int) -> Dict[str, int]:
    with get_connection() as conn:
        life = conn.execute(
            "SELECT COUNT(*) FROM quest_runs r JOIN quests q ON q.id = r.quest_id "
            "WHERE r.user_id = ? AND r.status = 'approved' AND q.category = 'life';", (user_id,)).fetchone()[0]
        quests = conn.execute("SELECT COUNT(*) FROM quest_runs WHERE user_id = ? AND status = 'approved';",
                              (user_id,)).fetchone()[0]
    return {"life_quests": life, "quests": quests}


# (id, name, how to unlock, check(ctx) -> bool)
TITLES = [
    ("rookie", "Rookie Hunter", "Starter", lambda c: True),
    ("dawn", "The One Who Wakes Before Dawn", "14 on-time wake-ups", lambda c: c["on_time_wakes"] >= 14),
    ("adversity", "The One Who Overcame Adversity", "30-day streak", lambda c: c["best_streak"] >= 30),
    ("daily", "Daily Quest Master", "Clear 30 Daily Quests", lambda c: c["dq"] >= 30),
    ("moms_pride", "Mother's Pride", "25 approved life quests", lambda c: c["life_quests"] >= 25),
    ("quester", "Quest Hunter", "100 approved quests", lambda c: c["quests"] >= 100),
    ("iron", "Iron Body", "VIT 50", lambda c: c["stats"]["vit"] >= 50),
    ("duelist", "Undefeated Duelist", "Win 10 challenges", lambda c: c["challenges_won"] >= 10),
    ("champion", "Season Champion", "Win a monthly season", lambda c: c["seasons_won"] >= 1),
    ("demon", "Demon Hunter", "Reach level 30", lambda c: c["level"] >= 30),
    ("monarch", "Shadow Monarch", "Reach level 100", lambda c: c["level"] >= 100),
]


def status(user_id: Optional[int] = None) -> Dict[str, Any]:
    """The Status window: level, EXP, rank, stats, titles, job."""
    from services import progression_service as prog
    uid = current_user_id() if user_id is None else user_id
    info = level_info(total_exp(uid))
    level = info["level"]
    alloc = _allocated(uid)
    stats = {key: BASE_STAT + alloc[key] for key, _, _ in STATS}
    unlock = prog.stats(uid)
    ctx = unlock | _counts(uid) | {"dq": daily_quests_cleared(uid), "stats": stats, "level": level}
    titles = [{"id": tid, "name": name, "how": how, "unlocked": check(ctx)} for tid, name, how, check in TITLES]
    with as_user(uid):
        chosen_title, job = get_setting("title", ""), get_setting("job", "")
    title = next((t["name"] for t in titles if t["id"] == chosen_title and t["unlocked"]), None)
    jobs = [{"id": jid, "name": name, "stat": stat, "about": about,
             "unlocked": level >= JOB_CHANGE_LEVEL and (stat is not None or ctx["dq"] >= 40)}
            for jid, name, stat, about in JOBS]
    return info | {
        "rank": rank_for_level(level), "stats": stats, "free_points": free_points(uid, level),
        "power": sum(stats.values()) * level, "titles": titles, "title": title,
        "job": next((j["name"] for j in jobs if j["id"] == job), None),
        "jobs": jobs, "job_change_level": JOB_CHANGE_LEVEL, "dq_cleared": ctx["dq"],
        "stat_names": [{"key": k, "short": s, "name": n} for k, s, n in STATS],
    }


def equip_title(title_id: str) -> Optional[str]:
    if title_id == "":
        set_setting("title", "")
        return None
    t = next((t for t in status()["titles"] if t["id"] == title_id), None)
    if not t or not t["unlocked"]:
        return "That title is still locked."
    set_setting("title", title_id)
    return None


def change_job(job_id: str) -> Optional[str]:
    s = status()
    if s["job"]:
        return "You already changed jobs."
    j = next((j for j in s["jobs"] if j["id"] == job_id), None)
    if not j or not j["unlocked"]:
        return f"Job change unlocks at level {JOB_CHANGE_LEVEL}."
    set_setting("job", job_id)
    return None


# ---------------------------------------------------------------------------
# Level-up announcements (Telegram + crew log), checked on every crew tick
# ---------------------------------------------------------------------------

async def announce_level_ups(bot) -> None:
    from services import activity_service
    from views import esc
    for m in get_users():
        uid = m["user_id"]
        level = level_of(uid)
        with as_user(uid):
            announced = get_setting("announced_level", "")
            if not announced:                      # first run: start from today's level, no spam
                set_setting("announced_level", str(level))
                continue
            if level <= int(announced):
                continue
            set_setting("announced_level", str(level))
        rank = rank_for_level(level)
        activity_service.record("level", f"reached Level {level} ⬆️", user_id=uid)
        await cs.send_html(bot, uid, f"🔷 <b>[System]</b>\n\n<b>LEVEL UP!</b> You are now <b>Level {level}</b> "
                                     f"({esc(rank['name'])}).\n+{POINTS_PER_LEVEL} stat points to spend in your Status window.")


cs.TICK_HOOKS.append(announce_level_ups)
