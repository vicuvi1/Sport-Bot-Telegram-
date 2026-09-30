import logging
from pathlib import Path
from typing import Optional
from aiohttp import web

import config
from services.workout_service import (
    get_or_create_daily_workout,
    update_workout_item,
    complete_all_exercises_for_workout,
    calculate_streaks,
    get_current_date_str
)

logger = logging.getLogger(__name__)

async def handle_index(request: web.Request) -> web.FileResponse:
    """Serves the single-page Telegram Mini App interface."""
    index_path = config.WEBAPP_DIR / "index.html"
    return web.FileResponse(index_path)

async def handle_get_workout(request: web.Request) -> web.Response:
    """Returns today's active workout details and streak in JSON."""
    db_path = request.app.get("db_path")
    today_str = get_current_date_str(db_path=db_path)
    workout = get_or_create_daily_workout(today_str, db_path=db_path)
    streak, best = calculate_streaks(today_str=today_str, db_path=db_path)
    workout["streak"] = streak
    workout["best_streak"] = best
    return web.json_response(workout)

async def handle_update_item(request: web.Request) -> web.Response:
    """Updates completion reps or delta for an exercise item."""
    try:
        db_path = request.app.get("db_path")
        data = await request.json()
        item_id = int(data.get("item_id"))
        completed_reps = data.get("completed_reps")
        delta_reps = data.get("delta_reps")
        status = data.get("status")

        updated_item, prog = update_workout_item(
            item_id=item_id,
            completed_reps=completed_reps,
            delta_reps=delta_reps,
            status=status,
            db_path=db_path
        )
        return web.json_response({
            "status": "ok",
            "item": updated_item,
            "progression": prog
        })
    except Exception as e:
        logger.error(f"Error updating item via webapp API: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=400)

async def handle_complete_all(request: web.Request) -> web.Response:
    """Marks all exercises complete for today."""
    try:
        db_path = request.app.get("db_path")
        today_str = get_current_date_str(db_path=db_path)
        workout = get_or_create_daily_workout(today_str, db_path=db_path)
        updated, progs = complete_all_exercises_for_workout(workout["id"], db_path=db_path)
        return web.json_response({
            "status": "ok",
            "workout": updated,
            "progressions": progs
        })
    except Exception as e:
        logger.error(f"Error completing all via webapp API: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=400)

async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "healthy"})

def create_webapp(db_path: Optional[Path] = None) -> web.Application:
    """Builds and returns the aiohttp web application."""
    app = web.Application()
    if db_path:
        app["db_path"] = db_path
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/workout", handle_get_workout)
    app.router.add_post("/api/workout/item", handle_update_item)
    app.router.add_post("/api/workout/complete_all", handle_complete_all)
    app.router.add_get("/health", handle_health)
    return app

async def start_webapp_server(
    host: str = config.WEBAPP_HOST,
    port: int = config.WEBAPP_PORT
) -> web.AppRunner:
    """Starts the aiohttp server asynchronously within the existing event loop."""
    app = create_webapp()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"📱 Telegram Mini App server running at http://{host}:{port}")
    return runner

if __name__ == "__main__":
    web.run_app(create_webapp(), host=config.WEBAPP_HOST, port=config.WEBAPP_PORT)
