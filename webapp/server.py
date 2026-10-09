"""The Telegram Mini App: static app files plus an authenticated JSON API.

Runs inside the bot process (same database, same bot for notifications) on
127.0.0.1; a reverse proxy (Caddy) serves it over HTTPS, which Telegram
requires for Mini Apps.

API (all require "Authorization: tma <initData>", see webapp/auth.py):
  GET  /api/state   everything the screens show
  POST /api/action  {"type": ..., ...}; returns {"ok", "message", "state"}
"""

import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from aiohttp import web

import config
from database import as_user, current_user_id, get_connection, get_setting, is_member, set_setting
from services import activity_service, body_service
from services import fitness_test_service as fts
from services import progression_service as prog
from services import compete_service as cmp
from services import crew_service as cs
from services import wake_service as ws
from services.workout_service import (
    MAX_PAUSE_DAYS,
    add_exercise,
    delete_exercise,
    resume_from_pause,
    start_pause,
    toggle_exercise_active,
    update_exercise,
    complete_all_exercises_for_workout,
    get_current_date_str,
    get_or_create_daily_workout,
    record_feedback,
    start_quick_workout,
    undo_quick_workout,
    update_workout_item,
)
from views import esc
from webapp.auth import AuthError, verify_init_data
from webapp.state import build_state

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

BOT_KEY = web.AppKey("bot", object)
BOT_DATA_KEY = web.AppKey("bot_data", dict)
TOKEN_KEY = web.AppKey("bot_token", str)

# Telegram loads the app in an iframe on web/desktop; everything else is ours.
CSP = ("default-src 'self'; script-src 'self' https://telegram.org; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data: blob:; connect-src 'self'; "
       "frame-ancestors https://web.telegram.org https://*.telegram.org")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@web.middleware
async def auth_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
    if not request.path.startswith("/api/"):
        response = await handler(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    header = request.headers.get("Authorization", "")
    if not header.startswith("tma "):
        return web.json_response({"ok": False, "message": "Open the app from Telegram."}, status=401)
    try:
        user = verify_init_data(header[4:], request.app[TOKEN_KEY])
    except AuthError as e:
        logger.warning("Mini App auth failed: %s", e)
        return web.json_response({"ok": False, "message": "Session expired. Close and reopen the app."}, status=401)

    uid = int(user["id"])
    if uid != config.USER_ID and not is_member(uid):
        logger.warning("Mini App: user %s is not in the crew", uid)
        return web.json_response({"ok": False, "message": "You're not in this crew. Ask for an invite."}, status=403)

    with as_user(uid):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            logger.error("Mini App API error on %s: %s", request.path, e, exc_info=True)
            return web.json_response({"ok": False, "message": "Something went wrong. Try again."}, status=500)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _today_workout() -> Dict[str, Any]:
    return get_or_create_daily_workout(get_current_date_str())


def _int(data: Dict[str, Any], key: str) -> int:
    try:
        return int(data[key])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"missing or bad '{key}'")


async def _act(request: web.Request, data: Dict[str, Any]) -> Optional[str]:
    """Performs one action for the current user. Returns a message to show, if any."""
    kind = data.get("type")
    bot = request.app.get(BOT_KEY)
    paused = _today_workout()["status"] == "paused"

    if kind in ("item_delta", "item_set", "item_done", "item_skip", "complete_all") and paused:
        return "Workouts are paused. Resume from the bot's /pause menu."

    if kind == "item_delta":
        item, level_up = update_workout_item(_int(data, "item_id"), delta_reps=max(-100, min(100, _int(data, "delta"))))
        return _progression(level_up) if item else "That exercise isn't yours."
    if kind == "item_set":
        item, level_up = update_workout_item(_int(data, "item_id"), completed_reps=max(0, min(5000, _int(data, "value"))))
        return _progression(level_up) if item else "That exercise isn't yours."
    if kind == "item_done":
        item_id = _int(data, "item_id")
        target = next((it["target_reps"] for it in _today_workout()["items"] if it["id"] == item_id), None)
        if target is None:
            return "That exercise isn't part of today."
        _, level_up = update_workout_item(item_id, completed_reps=target)
        return _progression(level_up)
    if kind == "item_skip":
        update_workout_item(_int(data, "item_id"), status="skipped")
        return None
    if kind == "complete_all":
        _, progressions = complete_all_exercises_for_workout(_today_workout()["id"])
        return "\n".join(_progression(p) for p in progressions) or None
    if kind == "quick":
        (start_quick_workout if data.get("on", True) else undo_quick_workout)(_today_workout()["id"])
        return None
    if kind == "feedback":
        result = record_feedback(_today_workout()["id"], str(data.get("value")))
        if not result["recorded"]:
            return "Feedback for today is already saved."
        changes = ", ".join(f"{c['name']} {c['old']}→{c['new']}" for c in result["changes"])
        return f"Targets updated: {changes}" if changes else "Thanks! Noted."
    if kind in ("roast", "hype"):
        target = _int(data, "target")
        if kind == "roast":
            result = await cs.send_roast(bot, request.app[BOT_DATA_KEY], current_user_id(), target,
                                         custom_text=data.get("text") if isinstance(data.get("text"), str) else None)
        else:
            result = await cs.send_hype(bot, current_user_id(), target)
        return result["message"]
    if kind == "wake_answer":
        outcome = ws.answer(get_current_date_str(), _int(data, "choice"))
        return {"wrong": "Wrong number. Are you even awake?", "expired": "This check has expired.",
                "answered": "Already done.", "on_time": "On time! Good morning.",
                "late": f"Up, but {outcome['log']['minutes_late'] if outcome['log'] else '?'} min late."}[outcome["result"]]
    if kind == "challenge":
        target = _int(data, "target")
        challenge_id = cmp.offer_challenge(current_user_id(), target, str(data.get("kind")))
        if not challenge_id:
            return "You already have an open challenge with them today."
        await _notify_challenge(bot, challenge_id)
        return "Challenge sent!"
    if kind == "challenge_answer":
        challenge = cmp.respond_challenge(_int(data, "id"), current_user_id(), bool(data.get("accept")))
        if not challenge:
            return "This challenge isn't open anymore."
        await cs.send_html(bot, challenge["challenger"],
                           f"{'⚔️' if challenge['status'] == 'accepted' else '🐔'} "
                           f"<b>{esc(cs.display_name(challenge['target']))}</b> "
                           f"{'accepted' if challenge['status'] == 'accepted' else 'chickened out of'} "
                           f"{cmp.CHALLENGES[challenge['kind']]['label']}")
        return "Game on!" if challenge["status"] == "accepted" else "Bawk bawk 🐔"
    if kind == "forfeit_done":
        forfeit = cmp.complete_forfeit(_int(data, "id"), current_user_id())
        if not forfeit:
            return "Nothing to mark here."
        await cs.send_html(bot, forfeit["winner"],
                           f"✅ <b>{esc(cs.display_name(forfeit['loser']))}</b> completed the forfeit: "
                           f"{esc(forfeit['task'])}")
        return f"Forfeit done. +{cmp.POINTS['forfeit']} pts"
    if kind == "settings":
        return _settings(request, data)
    if kind == "items_set":  # undo: put items back to earlier values
        values = data.get("values")
        if not isinstance(values, dict) or len(values) > 20:
            raise web.HTTPBadRequest(text="bad values")
        for item_id, value in values.items():
            update_workout_item(int(item_id), completed_reps=max(0, min(5000, int(value))))
        return "Undone."
    if kind == "pause":
        days = _int(data, "days")
        if not 1 <= days <= MAX_PAUSE_DAYS:
            return f"Pause for 1 to {MAX_PAUSE_DAYS} days."
        pause = start_pause(days)
        return f"Paused until {pause['end_date']}. Your streak is safe."
    if kind == "resume":
        return "Welcome back! Reminders are on again." if resume_from_pause() else "You weren't paused."
    if kind == "ex_add":
        try:
            add_exercise(str(data.get("name", "")), max(1, min(5000, _int(data, "target"))),
                         "sec" if data.get("unit") in ("s", "sec") else "reps")
        except Exception:
            return "Couldn't add it. Maybe you already have an exercise with that name?"
        return "Exercise added. It starts from your next workout."
    if kind == "ex_target":
        update_exercise(_int(data, "id"), target_reps=max(1, min(5000, _int(data, "target"))))
        return None
    if kind == "ex_toggle":
        toggle_exercise_active(_int(data, "id"))
        return None
    if kind == "ex_delete":
        return "Exercise removed." if delete_exercise(_int(data, "id")) else "Not found."
    if kind == "ex_days":
        days = str(data.get("days", ""))
        if not days or not all(d in "0123456" for d in days.split(",")):
            return "Pick at least one day."
        update_exercise(_int(data, "id"), days_of_week=",".join(sorted(set(days.split(",")))))
        return None
    if kind == "fitness_save":
        return _fitness_save(data)
    if kind == "fitness_pullups":
        set_setting("test_pullups", "1" if data.get("on") else "0")
        return None
    if kind == "equip":
        return None if prog.equip(str(data.get("slot")), str(data.get("id"))) else "Not unlocked yet."
    if kind == "onboard":
        if str(data.get("hair")) in ("lime", "orange", "blue", "pink"):
            prog.equip("hair", str(data["hair"]))
        set_setting("onboarded", "1")
        set_setting("seen_rank", prog.rank_for(prog.xp())["letter"])
        return "Welcome to the crew! Let's train."
    if kind == "ack_rank":
        letter = prog.rank_for(prog.xp())["letter"]
        if prog.rank_index(letter) > prog.rank_index(get_setting("seen_rank", "E")):
            activity_service.record("rank", f"reached rank {letter} 🎖")
        set_setting("seen_rank", letter)
        return None
    if kind == "react":
        return None if activity_service.react(_int(data, "activity_id"), str(data.get("emoji"))) else "Can't react to that."
    if kind == "level_up":
        result = prog.level_up(_int(data, "exercise_id"))
        if not result:
            return "Not unlocked yet. Keep hitting your target!"
        return f"Level up! {result['old']} → {result['new']} (target {result['target']})."
    if kind == "body_log":
        def num(key):
            try:
                return float(data[key]) if data.get(key) not in (None, "") else None
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text=f"bad {key}")
        error = body_service.log_body(num("weight"), num("waist"))
        return error or "Saved. Only you can see this."
    if kind == "photo_delete":
        return "Photo deleted." if body_service.delete_photo(_int(data, "id")) else "Not found."
    raise web.HTTPBadRequest(text="unknown action")


def _settings(request: web.Request, data: Dict[str, Any]) -> Optional[str]:
    reschedule = False
    for key in ("workout_time", "wake_time"):
        if key in data:
            value = str(data[key])
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
                return "Use a time like 07:30."
            set_setting(key, value)
            reschedule = True
    if "wake_enabled" in data:
        set_setting("wake_enabled", "1" if data["wake_enabled"] else "0")
        reschedule = True
    if "roast_level" in data:
        if data["roast_level"] not in cs.ROAST_LEVELS:
            return "Unknown roast level."
        set_setting("roast_level", data["roast_level"])
    flags = {"anime": "ui_anime", "feed": "crew_feed", "notifications": "notifications_enabled"}
    for key, setting in flags.items():
        if key in data:
            set_setting(setting, "1" if data[key] else "0")
    scheduler = request.app[BOT_DATA_KEY].get("scheduler")
    if reschedule and scheduler is not None:
        from scheduler import reschedule_user_jobs
        reschedule_user_jobs(scheduler, request.app.get(BOT_KEY), current_user_id())
    return "Saved." if reschedule else None


def _fitness_save(data: Dict[str, Any]) -> Dict[str, Any]:
    key = str(data.get("key"))
    test = fts.TESTS_BY_KEY.get(key)
    if not test:
        raise web.HTTPBadRequest(text="unknown test")
    value = data.get("value")
    month = fts.current_month()
    if value is None:
        fts.save_result(month, key, None)
        return {"message": "Skipped."}
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="bad value")
    if not 0 <= value <= fts.MAX_RESULT.get(test["unit"], 2000):
        return {"message": "That number looks wrong."}
    earlier = [v for m, v in fts.get_history(key) if m < month]
    fts.save_result(month, key, value)
    if earlier and value > max(earlier):
        activity_service.record("record", f"new {test['name'].lower()} record: {value} {test['unit']} 🏅")
        return {"message": f"🏅 New record! {value} {test['unit']}", "pr": True}
    return {"message": f"Saved: {value} {test['unit']}"}


def _progression(prog: Optional[Dict[str, Any]]) -> Optional[str]:
    if not prog:
        return None
    return f"Level up! {prog['exercise_name']} target {prog['old_target']} → {prog['new_target']}"


async def _notify_challenge(bot, challenge_id: int) -> None:
    from telegram import InlineKeyboardButton
    c = cmp.get_challenge(challenge_id)
    label = cmp.CHALLENGES[c["kind"]]["label"]
    await cs.send_html(
        bot, c["target"],
        f"🎯 <b>{esc(cs.display_name(c['challenger']))} challenges you:</b> {label}!\n\n"
        f"Do it today: +{cmp.POINTS['challenge']} pts for you. Fail: they get the points.",
        [[InlineKeyboardButton("✅ Accept", callback_data=f"cmp_acc:{challenge_id}"),
          InlineKeyboardButton("🐔 Chicken out", callback_data=f"cmp_dec:{challenge_id}")]])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def handle_state(request: web.Request) -> web.Response:
    return web.json_response(build_state())


async def handle_action(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="expected JSON")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="expected an object")
    result = await _act(request, data)
    extra = result if isinstance(result, dict) else {"message": result}
    bot = request.app.get(BOT_KEY)
    if bot is not None:
        await cs.tick(bot)  # crew feed + challenge results, right away
    return web.json_response({"ok": True, **extra, "state": build_state()})


async def handle_photo_upload(request: web.Request) -> web.Response:
    """Raw JPEG/PNG body (the app resizes it first). Private to the uploader."""
    data = await request.read()
    result = body_service.save_photo(data)
    return web.json_response({**result, "state": build_state()}, status=200 if result["ok"] else 400)


async def handle_photo(request: web.Request) -> web.StreamResponse:
    try:
        photo_id = int(request.match_info["photo_id"])
    except ValueError:
        raise web.HTTPNotFound()
    path = body_service.photo_file(photo_id)
    if not path:
        raise web.HTTPNotFound()
    response = web.FileResponse(path)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


async def handle_day(request: web.Request) -> web.Response:
    """What the current user did on one day (existing days only; nothing is created)."""
    date = request.query.get("date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise web.HTTPBadRequest(text="bad date")
    with get_connection() as conn:
        day = conn.execute("SELECT id, status, feedback, quick FROM daily_workouts WHERE user_id = ? AND date = ?;",
                           (current_user_id(), date)).fetchone()
        items = conn.execute("SELECT exercise_name, completed_reps, target_reps, unit, status FROM workout_items "
                             "WHERE daily_workout_id = ? ORDER BY id;", (day["id"],)).fetchall() if day else []
    return web.json_response({
        "date": date, "status": day["status"] if day else None,
        "items": [{"name": i["exercise_name"], "done": i["completed_reps"], "target": i["target_reps"],
                   "unit": "s" if i["unit"] == "sec" else i["unit"], "status": i["status"]} for i in items],
    })


def _asset_version() -> str:
    """Changes whenever app.js or app.css changes, so phones never keep stale files
    (Telegram's in-app browser caches aggressively)."""
    return str(int(max((STATIC_DIR / name).stat().st_mtime for name in ("app.js", "app.css"))))


async def handle_index(request: web.Request) -> web.Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{VERSION}}", _asset_version())
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "healthy"})


def create_webapp(bot=None, bot_data: Optional[dict] = None, bot_token: Optional[str] = None) -> web.Application:
    # Large enough for a resized progress photo; JSON actions are tiny.
    app = web.Application(middlewares=[auth_middleware], client_max_size=body_service.MAX_PHOTO_BYTES + 1024)
    app[BOT_KEY] = bot
    app[BOT_DATA_KEY] = bot_data if bot_data is not None else {}
    app[TOKEN_KEY] = bot_token or config.BOT_TOKEN
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/state", handle_state)
    app.router.add_post("/api/action", handle_action)
    app.router.add_post("/api/photo", handle_photo_upload)
    app.router.add_get("/api/photo/{photo_id}", handle_photo)
    app.router.add_get("/api/day", handle_day)
    app.router.add_static("/static/", STATIC_DIR, append_version=False)
    return app


async def start_webapp_server(bot, bot_data: dict, host: str = config.WEBAPP_HOST,
                              port: int = config.WEBAPP_PORT) -> web.AppRunner:
    """Starts the app server inside the bot's event loop."""
    runner = web.AppRunner(create_webapp(bot, bot_data))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("Mini App server on http://%s:%s (public: %s)", host, port, config.WEBAPP_URL)
    return runner
