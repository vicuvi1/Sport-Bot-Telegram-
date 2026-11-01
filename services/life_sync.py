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


def push_workout_done(title: str = "Workout (Sporty)") -> None:
    """A finished workout counts in Life System (Insights, streak, points)."""
    _background({"op": "workout", "title": title})
