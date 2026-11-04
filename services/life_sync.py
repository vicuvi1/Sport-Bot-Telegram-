"""Shares water and workouts with Life System (one log for both).

Life System (https://github.com/vicuvi1/MEGA-SYSTEM) is the single source:
water and reps you log here go there, and what you log in the app (or with
the Mega coach) shows here. Only for the bot's owner (TELEGRAM_USER_ID), and
only when both settings are set on this server:

    LIFE_API_URL=https://mega-system-ten.vercel.app
    LIFE_SYNC_KEY=<the key from Life System>

Without them (or when Life System can't be reached) the bot keeps working
with its own data, as before.
"""

import logging
import os
import threading
from typing import Any, Dict, Optional

import httpx

import config
from database import current_user_id

logger = logging.getLogger(__name__)

TIMEOUT_S = 6.0


def _settings() -> tuple:
    return os.getenv("LIFE_API_URL", "").strip().rstrip("/"), os.getenv("LIFE_SYNC_KEY", "").strip()


def enabled() -> bool:
    """On when both settings are set and the current user is the owner."""
    url, key = _settings()
    return bool(url and key) and current_user_id() == config.USER_ID


def _call(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url, key = _settings()
    if not (url and key):
        return None
    try:
        r = httpx.post(
            f"{url}/api/cron?job=sport",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=TIMEOUT_S,
        )
        data = r.json()
        if r.status_code >= 400 or not data.get("ok"):
            logger.warning("Life System sync %s: %s", payload.get("op"), data.get("error") or r.status_code)
            return None
        return data
    except Exception as e:  # network, JSON: fall back to local data
        logger.warning("Life System sync %s failed: %s", payload.get("op"), e)
        return None


def call_raw(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Calls Life System outside a user's update (background jobs); None when off or unreachable."""
    return _call(payload)


def state() -> Optional[Dict[str, Any]]:
    """Today's water and reps in Life System, or None."""
    if not enabled():
        return None
    data = _call({"op": "state"})
    return data.get("state") if data else None


def add_water(ml: int) -> Optional[Dict[str, Any]]:
    """Adds (or removes, negative) water in Life System; returns the new state or None."""
    if not enabled() or not ml:
        return None
    data = _call({"op": "water", "ml": int(ml)})
    return data.get("state") if data else None


def _background(payload: Dict[str, Any]) -> None:
    """Fire and forget (a button tap shouldn't wait for the network)."""
    if not enabled():
        return
    threading.Thread(target=_call, args=(payload,), daemon=True).start()


def push_reps(exercise_name: str, amount: int) -> None:
    """Reps done here count in Life System's Sport Track too."""
    if amount:
        _background({"op": "reps", "name": exercise_name, "amount": int(amount)})


def _workout_payload(cursor, workout_id: int) -> Dict[str, Any]:
    """A finished day as Life System wants it: its date and what was done."""
    row = cursor.execute("SELECT date FROM daily_workouts WHERE id = ?;", (workout_id,)).fetchone()
    items = cursor.execute(
        "SELECT exercise_name, completed_reps FROM workout_items WHERE daily_workout_id = ? AND completed_reps > 0;",
        (workout_id,),
    ).fetchall()
    day = row[0] if row else None
    done = [{"exerciseId": str(r[0]).lower().replace(" ", "_"), "name": str(r[0]), "amount": int(r[1]), "unit": "reps"} for r in items]
    title = "Sporty: " + ", ".join(f"{d['amount']} {d['name']}" for d in done) if done else "Workout (Sporty)"
    return {"op": "workout", "day": day, "title": title[:80], "items": done, "source": "sporty"}


def push_workout_done(cursor=None, workout_id: Optional[int] = None) -> None:
    """A finished workout counts in Life System (Insights, streak, points), on its own day."""
    payload: Dict[str, Any] = {"op": "workout", "title": "Workout (Sporty)", "source": "sporty"}
    if cursor is not None and workout_id is not None:
        try:
            payload = _workout_payload(cursor, workout_id)
        except Exception as e:  # never block the workout itself
            logger.warning("Life System sync: could not read workout %s: %s", workout_id, e)
    _background(payload)


def push_history(days: int = 14) -> int:
    """Sends every completed day of the last `days` days (Life System keeps one per day).

    Runs when the bot starts, so workouts done before syncing was set up (or
    while Life System was unreachable) are not lost. Returns how many were sent.
    """
    if not enabled():
        return 0
    from datetime import date, timedelta

    from database import get_connection

    since = (date.today() - timedelta(days=days)).isoformat()
    sent = 0
    with get_connection() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id FROM daily_workouts WHERE user_id = ? AND status = 'completed' AND date >= ? ORDER BY date;",
            (current_user_id(), since),
        ).fetchall()
        payloads = [_workout_payload(cur, r[0]) for r in rows]
    for p in payloads:
        if _call(p):
            sent += 1
    if sent:
        logger.info("Life System sync: sent %d past workout day(s)", sent)
    return sent
