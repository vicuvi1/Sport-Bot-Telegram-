"""Everything the Mini App shows, for the current (bound) user, as one JSON-able dict."""

from datetime import datetime, timedelta
from typing import Any, Dict, List

import config
from database import current_user_id, get_connection, get_setting, get_users
from services import activity_service, body_service
from services import compete_service as cmp
from services import crew_service as cs
from services import fitness_test_service as fts
from services import progression_service as prog
from services import social_service as social
from services import wake_service as ws
from services.workout_service import (
    COMEBACK_FACTORS,
    FEEDBACK_LABELS,
    calculate_streaks,
    get_active_pause,
    get_current_date_str,
    get_exercises,
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


def _activity(me: int) -> List[Dict[str, Any]]:
    names = {u["user_id"]: cs.display_name(u["user_id"]) for u in get_users()}
    out = []
    for a in activity_service.recent(30):
        mine = next((r["emoji"] for r in a["reactions"] if r["user_id"] == me), None)
        counts: Dict[str, int] = {}
        for r in a["reactions"]:
            counts[r["emoji"]] = counts.get(r["emoji"], 0) + 1
        out.append({"id": a["id"], "who": names.get(a["user_id"], "Someone"), "me": a["user_id"] == me,
                    "text": a["text"], "kind": a["kind"], "at": a["created_at"],
                    "reactions": [{"emoji": e, "count": c} for e, c in counts.items()], "my_reaction": mine})
    return out


def _fitness_test(today: str) -> Dict[str, Any]:
    month = fts.current_month(today)
    results = fts.get_month_results(month)
    return {
        "month": month,
        "pullups": get_setting("test_pullups", "0") == "1",
        "complete": fts.is_month_complete(month),
        "tests": [{"key": t["key"], "name": t["name"], "unit": t["unit"], "how": t["how"], "timer": t["timer"],
                   "done": t["key"] in results, "result": results.get(t["key"])} for t in fts.active_tests()],
    }


def _settings() -> Dict[str, Any]:
    pause = get_active_pause()
    return {
        "workout_time": get_setting("workout_time", "07:00"),
        "timezone": get_setting("timezone", config.TIMEZONE),
        "notifications": get_setting("notifications_enabled", "1") == "1",
        "wake_enabled": ws.wake_enabled(),
        "wake_time": ws.wake_time(),
        "roast_level": cs.roast_level(current_user_id()),
        "feed": get_setting("crew_feed", "1") == "1",
        "paused_until": pause["end_date"] if pause else None,
    }


def _seasons(today: str) -> Dict[str, Any]:
    month = today[:7]
    start, end = prog.month_bounds(month)
    champions = []
    for m in prog.finished_months(today, back=6):
        champ = prog.season_champion(m)
        if champ:
            champions.append({"month": m, "title": prog.month_title(m), "name": cs.display_name(champ)})
    return {
        "month": month,
        "standings": [{"name": cs.display_name(r["user_id"]), "points": r["points"], "color": _color_for(r["user_id"])}
                      for r in prog.season_standings(month)],
        "champions": champions,
    }


def build_state() -> Dict[str, Any]:
    me = current_user_id()
    today = get_current_date_str()
    streak, best = calculate_streaks(today_str=today)
    start = datetime.strptime(today, "%Y-%m-%d").date()
    monday = start - timedelta(days=start.weekday())

    last_season = prog.finished_months(today, back=1)[0]
    last_champion = prog.season_champion(last_season)

    crew = []
    for m in get_users():
        uid = m["user_id"]
        snap = cs.day_snapshot(uid, today)
        crew.append({
            "id": uid, "name": snap["name"], "me": uid == me,
            "color": _color_for(uid), "status": snap["status"], "done": snap["done"],
            "total": snap["total"], "reps": snap["reps"], "streak": snap["streak"],
            "progress": social.fine_progress(snap["items"], snap["status"]), "wake": ws.today_label(uid),
            "live": social.is_live(uid), "rest_left": social.rest_left(uid),
            "rank": prog.rank_for(prog.xp(uid))["letter"],
            "gear": prog.equipped(uid),
            "title": prog.month_title(last_season) if last_champion == uid else None,
        })
    standings = cmp.standings(today)
    pulse = social.pulse()
    my_rank = prog.rank_for(prog.xp(me))
    others = [c for c in crew if not c["me"]]

    return {
        "me": {
            "id": me, "name": cs.display_name(me), "is_owner": me == config.USER_ID,
            "color": _color_for(me),
            "anime": get_setting("ui_anime", "1") == "1",
            "roast_level": cs.roast_level(me),
            "feed": get_setting("crew_feed", "1") == "1",
            "onboarded": get_setting("onboarded", "0") == "1",
            "rank": my_rank,
            "seen_rank": get_setting("seen_rank", "E"),
            "gear": prog.equipped(me),
        },
        "settings": _settings(),
        "exercises": [{"id": e["id"], "name": e["name"], "target": e["target_reps"], "unit": unit_label(e["unit"]),
                       "active": bool(e["is_active"]), "days": e["days_of_week"]} for e in get_exercises()],
        "fitness_test": _fitness_test(today),
        "ladders": prog.ladder_status(today),
        "gear_catalog": prog.gear_catalog(me),
        "body": body_service.body_summary(today) | {"photos": body_service.list_photos()},
        "seasons": _seasons(today),
        "activity": _activity(me),
        "reactions": activity_service.REACTIONS,
        "ghost": activity_service.ghost_pace(others[0]["id"], today) if others else None,
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
        "sig": pulse["sig"],
        "chat": social.chat_messages(60),
        "unread": pulse["unread"],
        "stickers": social.STICKERS,
        "quick_replies": social.QUICK_REPLIES,
        "bets": social.bets_for(me, today),
        "bet_kinds": [{"key": k, "label": v} for k, v in social.BET_KINDS.items()],
        "stakes": list(social.STAKES),
        "report": {"last": social.report_card(me, last_season), "current": social.report_card(me, today[:7])},
    }
