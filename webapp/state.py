"""Everything the Mini App shows, for the current (bound) user, as one JSON-able dict."""

from datetime import datetime, timedelta
from typing import Any, Dict, List

import config
from database import current_user_id, get_connection, get_setting, get_users
from services import compete_service as cmp
from services import crew_service as cs
from services import fitness_test_service as fts
from services import wake_service as ws
from services.workout_service import (
    COMEBACK_FACTORS,
    FEEDBACK_LABELS,
    calculate_streaks,
    get_active_pause,
    get_current_date_str,
    get_or_create_daily_workout,
)
from views import day_codes, unit_label, week_codes

# Fixed colors per crew slot, so each person keeps "their" color everywhere.
CREW_COLORS = ["#C8F135", "#FF8A3D", "#7FB2FF", "#F06BC6", "#5EE6C5", "#FFD23F"]


def _color_for(user_id: int) -> str:
    ids = [u["user_id"] for u in get_users()]
    return CREW_COLORS[ids.index(user_id) % len(CREW_COLORS)] if user_id in ids else CREW_COLORS[0]


def _today(today: str) -> Dict[str, Any]:
    workout = get_or_create_daily_workout(today)
    items = [{
        "id": it["id"], "name": it["exercise_name"], "done": it["completed_reps"],
        "target": it["target_reps"], "unit": unit_label(it["unit"]), "status": it["status"],
    } for it in workout["items"]]
    ratio = (sum(min(1, i["done"] / i["target"]) if i["target"] else 1 for i in items) / len(items)) if items else 0
    pause = get_active_pause(today)
    return {
        "date": today,
        "status": workout["status"],
        "items": items,
        "done_count": sum(1 for i in items if i["status"] == "completed"),
        "progress": 1.0 if workout["status"] == "completed" else round(ratio, 3),
        "quick": bool(workout.get("quick")),
        "comeback_pct": int(COMEBACK_FACTORS[workout["comeback_stage"]] * 100)
        if workout.get("comeback_stage") in COMEBACK_FACTORS else None,
        "feedback": workout.get("feedback"),
        "can_give_feedback": workout["status"] == "completed" and not workout.get("feedback") and not workout.get("quick"),
        "paused_until": pause["end_date"] if pause else None,
    }


def _wake(today: str) -> Dict[str, Any]:
    log = ws.get_log(today)
    data = {"enabled": ws.wake_enabled(), "time": ws.wake_time(), "streak": ws.wake_streak(), "today": None}
    if log:
        data["today"] = {
            "status": log["status"],
            "challenge": log["challenge"] if log["status"] == "pending" else None,
            "choices": ws.answer_choices(log["challenge"]) if log["status"] == "pending" else [],
            "answered_at": (log["answered_at"] or "")[11:16] or None,
            "minutes_late": log["minutes_late"],
        }
    return data


def _fitness() -> List[Dict[str, Any]]:
    tests = []
    for t in fts.FITNESS_TESTS:
        history = fts.get_history(t["key"])[-12:]
        if history:
            tests.append({"key": t["key"], "name": t["name"], "unit": t["unit"],
                          "points": [{"month": m, "value": v} for m, v in history]})
    return tests


def _records() -> List[Dict[str, Any]]:
    """Best single day per exercise."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT wi.exercise_name AS name, wi.unit AS unit, MAX(wi.completed_reps) AS best "
            "FROM workout_items wi JOIN daily_workouts dw ON dw.id = wi.daily_workout_id "
            "WHERE dw.user_id = ? AND wi.completed_reps > 0 GROUP BY wi.exercise_name, wi.unit "
            "ORDER BY best DESC LIMIT 4;", (current_user_id(),)).fetchall()
    return [{"name": r["name"], "unit": unit_label(r["unit"]), "best": r["best"]} for r in rows]


def _challenges(me: int, today: str) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM challenges WHERE date = ? AND (challenger = ? OR target = ?) "
            "AND status IN ('offered', 'accepted', 'won', 'lost', 'declined') ORDER BY id DESC LIMIT 5;",
            (today, me, me)).fetchall()
    out = []
    for c in (dict(r) for r in rows):
        out.append({
            "id": c["id"], "label": cmp.CHALLENGES[c["kind"]]["label"], "status": c["status"],
            "from": cs.display_name(c["challenger"]), "to": cs.display_name(c["target"]),
            "mine_to_answer": c["target"] == me and c["status"] == "offered",
            "i_am_target": c["target"] == me,
        })
    return out


def build_state() -> Dict[str, Any]:
    me = current_user_id()
    today = get_current_date_str()
    streak, best = calculate_streaks(today_str=today)
    start = datetime.strptime(today, "%Y-%m-%d").date()
    monday = start - timedelta(days=start.weekday())

    crew = []
    for m in get_users():
        snap = cs.day_snapshot(m["user_id"], today)
        crew.append({
            "id": m["user_id"], "name": snap["name"], "me": m["user_id"] == me,
            "color": _color_for(m["user_id"]), "status": snap["status"], "done": snap["done"],
            "total": snap["total"], "reps": snap["reps"], "streak": snap["streak"],
            "progress": round(cs.progress_ratio(snap), 3), "wake": ws.today_label(m["user_id"]),
        })
    standings = cmp.standings(today)

    return {
        "me": {
            "id": me, "name": cs.display_name(me), "is_owner": me == config.USER_ID,
            "color": _color_for(me),
            "anime": get_setting("ui_anime", "1") == "1",
            "roast_level": cs.roast_level(me),
            "feed": get_setting("crew_feed", "1") == "1",
        },
        "today": _today(today),
        "streak": streak,
        "best_streak": best,
        "week": week_codes(today),
        "last4weeks": day_codes(monday - timedelta(weeks=3), monday + timedelta(days=6), today),
        "crew": crew,
        "crew_streak": cs.crew_streak(today),
        "points": [{"id": r["user_id"], "name": r["name"], "points": r["points"],
                    "color": _color_for(r["user_id"])} for r in standings],
        "my_points": next((r["parts"] for r in standings if r["user_id"] == me), {}),
        "wake": _wake(today),
        "fitness": _fitness(),
        "records": _records(),
        "challenges": _challenges(me, today),
        "challenge_kinds": [{"key": k, "label": v["label"]} for k, v in cmp.CHALLENGES.items()],
        "forfeits": [{
            "id": f["id"], "task": f["task"], "status": f["status"],
            "loser": cs.display_name(f["loser"]), "winner": cs.display_name(f["winner"]),
            "mine_to_do": f["loser"] == me and f["status"] == "pending",
        } for f in cmp.open_forfeits()],
        "feedback_options": [{"key": k, "label": v} for k, v in FEEDBACK_LABELS.items()],
    }
