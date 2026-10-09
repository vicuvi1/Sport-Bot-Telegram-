"""Long-term progression: levels and ranks (via the System), unlockable mascot gear, monthly seasons,
and skill ladders (harder exercise versions as you get stronger).

Everything is derived from logged data (points, streaks, reps, challenges),
so it can't drift out of sync and needs no separate bookkeeping.
"""

import calendar
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from database import as_user, current_user_id, get_connection, get_setting, get_users, set_setting
from services import compete_service as cmp
from services import leveling_service as lv
from services.workout_service import (
    calculate_streaks,
    get_current_date_str,
    get_exercises,
    update_exercise,
)

# ---------------------------------------------------------------------------
# XP, levels and ranks (the System, see leveling_service)
# ---------------------------------------------------------------------------

RANKS = [(r[1], r[0]) for r in lv.RANKS]   # (letter, first level)


def xp(user_id: Optional[int] = None) -> int:
    """Total EXP (5 per weekly point ever earned + quests and Daily Quests)."""
    return lv.total_exp(user_id)


def rank_for(exp: int) -> Dict[str, Any]:
    """Level and rank for a total EXP amount."""
    info = lv.level_info(exp)
    rank = lv.rank_for_level(info["level"])
    return {
        "letter": rank["letter"], "name": rank["name"], "index": rank["index"],
        "xp": exp, "level": info["level"], "into": info["into"], "needed": info["needed"],
        "floor": info["floor"], "next_at": info["floor"] + info["needed"],
        "next_letter": rank["next_letter"], "next_level": rank["next_level"],
    }


def rank_index(letter: str) -> int:
    return lv.rank_index(letter)


# ---------------------------------------------------------------------------
# Stats used by unlocks
# ---------------------------------------------------------------------------

def stats(user_id: Optional[int] = None) -> Dict[str, int]:
    uid = current_user_id() if user_id is None else user_id
    with as_user(uid):
        _, best = calculate_streaks()
    with get_connection() as conn:
        reps = conn.execute(
            "SELECT COALESCE(SUM(wi.completed_reps), 0) FROM workout_items wi "
            "JOIN daily_workouts dw ON dw.id = wi.daily_workout_id WHERE dw.user_id = ? AND wi.unit != 'sec';",
            (uid,)).fetchone()[0]
        won = conn.execute(
            "SELECT COUNT(*) FROM challenges WHERE (target = ? AND status = 'won') OR (challenger = ? AND status = 'lost');",
            (uid, uid)).fetchone()[0]
        wakes = conn.execute("SELECT COUNT(*) FROM wake_logs WHERE user_id = ? AND status = 'on_time';", (uid,)).fetchone()[0]
    parts = cmp.points_breakdown(uid, "0000-01-01", "9999-12-31")
    return {
        "best_streak": best,
        "lifetime_reps": reps,
        "challenges_won": won,
        "on_time_wakes": wakes,
        "test_records": parts["test_pr"] // cmp.POINTS["test_pr"],
        "rank": lv.rank_for_level(lv.level_info(lv.EXP_PER_POINT * parts["total"] + lv.ledger_exp(uid))["level"])["index"],
        "seasons_won": len(seasons_won(uid)),
    }


# ---------------------------------------------------------------------------
# Mascot gear
# ---------------------------------------------------------------------------
# (id, label, color, condition stat, threshold, how to unlock). threshold 0 = starter.

GEAR = {
    "hair": [
        ("lime", "Volt", "#C8F135", None, 0, "Starter"),
        ("orange", "Blaze", "#FF8A3D", None, 0, "Starter"),
        ("blue", "Azure", "#7FB2FF", None, 0, "Starter"),
        ("pink", "Sakura", "#F06BC6", None, 0, "Starter"),
        ("silver", "Silver", "#D9DEE7", "rank", rank_index("A"), "Reach rank A"),
        ("crimson", "Crimson", "#FF4D5E", "challenges_won", 3, "Win 3 challenges"),
        ("gold", "Golden", "#FFD23F", "lifetime_reps", 5000, "5,000 lifetime reps"),
    ],
    "band": [
        ("orange", "Orange band", "#FF8A3D", None, 0, "Starter"),
        ("lime", "Volt band", "#C8F135", None, 0, "Starter"),
        ("red", "Crimson band", "#E5484D", "best_streak", 7, "7-day streak"),
        ("gold", "Gold band", "#FFD23F", "rank", rank_index("B"), "Reach rank B"),
        ("ink", "Ninja band", "#1B1E24", "on_time_wakes", 10, "10 on-time wake-ups"),
    ],
    "aura": [
        ("none", "No aura", "", None, 0, "Starter"),
        ("spark", "Spark aura", "#FFD23F", "test_records", 1, "Beat a fitness test record"),
        ("flame", "Flame aura", "#FF8A3D", "best_streak", 21, "21-day streak"),
        ("lightning", "Lightning aura", "#7FB2FF", "challenges_won", 5, "Win 5 challenges"),
        ("royal", "Royal aura", "#C8A2FF", "seasons_won", 1, "Win a monthly season"),
    ],
}
DEFAULT_GEAR = {"hair": "lime", "band": "orange", "aura": "none"}


def gear_catalog(user_id: Optional[int] = None) -> Dict[str, List[Dict[str, Any]]]:
    s = stats(user_id)
    return {
        slot: [{"id": gid, "label": label, "color": color, "how": how,
                "unlocked": stat is None or s.get(stat, 0) >= need}
               for gid, label, color, stat, need, how in items]
        for slot, items in GEAR.items()
    }


def _gear_color(slot: str, gid: str) -> str:
    return next((g[2] for g in GEAR[slot] if g[0] == gid), "")


def equipped(user_id: Optional[int] = None) -> Dict[str, str]:
    """{"hair": color, "band": color, "aura": color or ""} for drawing the mascot."""
    uid = current_user_id() if user_id is None else user_id
    with as_user(uid):
        chosen = {slot: get_setting(f"gear_{slot}", "") for slot in GEAR}
    if not chosen["hair"]:
        # Before choosing: the crew's color (owner volt, brother orange, ...).
        ids = [u["user_id"] for u in get_users()]
        chosen["hair"] = ["lime", "orange", "blue", "pink"][ids.index(uid) % 4] if uid in ids else "lime"
    if not chosen["band"]:
        chosen["band"] = "lime" if chosen["hair"] == "orange" else "orange"
    if not chosen["aura"]:
        chosen["aura"] = "none"
    return {slot: _gear_color(slot, gid) for slot, gid in chosen.items()} | {"ids": chosen}


def equip(slot: str, gear_id: str) -> bool:
    """Equips an unlocked item for the current user."""
    if slot not in GEAR:
        return False
    item = next((g for g in gear_catalog()[slot] if g["id"] == gear_id), None)
    if not item or not item["unlocked"]:
        return False
    set_setting(f"gear_{slot}", gear_id)
    return True


# ---------------------------------------------------------------------------
# Monthly seasons
# ---------------------------------------------------------------------------

def month_bounds(month: str) -> tuple:
    year, mon = (int(p) for p in month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def season_standings(month: str) -> List[Dict[str, Any]]:
    start, end = month_bounds(month)
    table = [{"user_id": m["user_id"], "points": cmp.points_breakdown(m["user_id"], start, end)["total"]}
             for m in get_users()]
    return sorted(table, key=lambda r: r["points"], reverse=True)


def season_champion(month: str) -> Optional[int]:
    """The single top scorer of a month (None for a tie, no points or a solo crew)."""
    table = season_standings(month)
    if len(table) < 2 or table[0]["points"] == 0 or table[0]["points"] == table[1]["points"]:
        return None
    return table[0]["user_id"]


def finished_months(today_str: Optional[str] = None, back: int = 24) -> List[str]:
    today = datetime.strptime(today_str or get_current_date_str(), "%Y-%m-%d").date()
    months, d = [], today.replace(day=1)
    for _ in range(back):
        d = (d - timedelta(days=1)).replace(day=1)
        months.append(d.strftime("%Y-%m"))
    return months


def seasons_won(user_id: int, today_str: Optional[str] = None) -> List[str]:
    with get_connection() as conn:
        first = conn.execute("SELECT MIN(date) FROM daily_workouts;").fetchone()[0]
    if not first:
        return []
    return [m for m in finished_months(today_str) if m >= first[:7] and season_champion(m) == user_id]


def month_title(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B") + " Champion"


async def send_season_results(bot, today_str: Optional[str] = None) -> Optional[int]:
    """1st of the month: last month's champion to everyone. Returns the champion id."""
    from services import activity_service
    from services import crew_service as cs
    from views import card, esc

    members = get_users()
    if len(members) < 2:
        return None
    month = finished_months(today_str, back=1)[0]
    table = season_standings(month)
    champion = season_champion(month)
    name = datetime.strptime(month, "%Y-%m").strftime("%B")
    medals = ["👑", "🥈", "🥉"] + ["▫️"] * 10
    lines = [f"🏆 <b>Season {name} is over!</b>", "",
             card([f"{medals[i]} {esc(cs.display_name(r['user_id']))}  <b>{r['points']}</b> pts" for i, r in enumerate(table)])]
    if champion:
        lines.append(f"\n👑 <b>{esc(cs.display_name(champion))}</b> is the {name} Champion! New season starts now.")
        activity_service.record("season", f"won the {name} season 👑", user_id=champion)
    else:
        lines.append("\n🤝 A tie at the top. New season starts now.")
    for m in members:
        await cs.send_html(bot, m["user_id"], "\n".join(lines))
    return champion


# ---------------------------------------------------------------------------
# Skill ladders: harder versions instead of endless reps
# ---------------------------------------------------------------------------
# key -> (levels, unit, unlock target, start target factor)

LADDERS = {
    "push": (["Knee push-ups", "Push-ups", "Diamond push-ups", "Decline push-ups", "Archer push-ups",
              "One-arm push-ups"], "reps", 60, 0.35),
    "squat": (["Squats", "Jump squats", "Split squats", "Bulgarian split squats", "Shrimp squats",
               "Pistol squats"], "reps", 60, 0.35),
    "core": (["Crunches", "Sit-ups", "Leg raises", "V-ups", "Hollow rocks"], "reps", 50, 0.4),
    "plank": (["Plank", "Long-lever plank", "Side plank", "Hollow hold", "RKC plank"], "sec", 120, 0.5),
}
SOLID_DAYS_NEEDED = 3  # full-target days in the last 7, at the current target


def _ladder_for(name: str):
    lname = name.strip().lower()
    for key, (levels, unit, unlock, factor) in LADDERS.items():
        for i, level in enumerate(levels):
            if level.lower() == lname:
                return key, i
    return None, None


def ladder_status(today_str: Optional[str] = None) -> List[Dict[str, Any]]:
    """Each active exercise that is on a ladder: level, next step, and whether it's unlocked."""
    today = datetime.strptime(today_str or get_current_date_str(), "%Y-%m-%d").date()
    since = (today - timedelta(days=6)).isoformat()
    out = []
    for ex in get_exercises(active_only=True):
        key, index = _ladder_for(ex["name"])
        if key is None:
            continue
        levels, unit, unlock, _ = LADDERS[key]
        with get_connection() as conn:
            solid = conn.execute(
                "SELECT COUNT(DISTINCT dw.date) FROM workout_items wi JOIN daily_workouts dw ON dw.id = wi.daily_workout_id "
                "WHERE dw.user_id = ? AND wi.exercise_id = ? AND dw.date >= ? AND wi.status = 'completed' "
                "AND wi.completed_reps >= ?;", (current_user_id(), ex["id"], since, unlock)).fetchone()[0]
        has_next = index + 1 < len(levels)
        out.append({
            "exercise_id": ex["id"], "name": ex["name"], "level": index + 1, "levels": len(levels),
            "next": levels[index + 1] if has_next else None,
            "unlock_target": unlock, "unit": "s" if unit == "sec" else "reps",
            "solid_days": solid, "days_needed": SOLID_DAYS_NEEDED,
            "can_level_up": has_next and ex["target_reps"] >= unlock and solid >= SOLID_DAYS_NEEDED,
        })
    return out


def level_up(exercise_id: int) -> Optional[Dict[str, Any]]:
    """Swaps an exercise for its next ladder step (with a fresh, lower target)."""
    status = next((s for s in ladder_status() if s["exercise_id"] == exercise_id), None)
    if not status or not status["can_level_up"]:
        return None
    key, _ = _ladder_for(status["name"])
    levels, unit, unlock, factor = LADDERS[key]
    new_target = max(30 if unit == "sec" else 8, round(unlock * factor))
    try:
        update_exercise(exercise_id, name=status["next"], target_reps=new_target)
    except Exception:
        return None  # e.g. they already have an exercise with that name
    from services import activity_service
    activity_service.record("level", f"leveled up to {status['next']} 🆙")
    return {"old": status["name"], "new": status["next"], "target": new_target}
