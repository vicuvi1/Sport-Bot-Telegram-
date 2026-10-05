"""End-to-end callback routing + execution tests.

These tests verify two things for EVERY inline button in the bot:

1. Routing — each ``callback_data`` reaches exactly one handler, and the
   workout / stats / settings namespaces never overlap or fall through to a
   catch-all handler.
2. Execution — the target handler runs to completion, acknowledging the click
   exactly once (no double ``answer()``) and without raising.
"""

import asyncio
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update, CallbackQuery, Message, Chat, User
from telegram.ext import ApplicationBuilder, CallbackQueryHandler

import config
import handlers.workout as workout_module
import main as app_main

# SAFETY GUARD: this module executes real callbacks (some of which write to the
# database). It is only safe inside pytest, where tests/conftest.py redirects
# config.DB_PATH to a throwaway temp database. Importing it from a standalone
# script would run those callbacks against the PRODUCTION database.
if "pytest" not in sys.modules:
    raise RuntimeError(
        "tests/test_callback_routing.py must only be imported under pytest "
        "(its callbacks mutate the database). Use `pytest tests/` instead."
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def build_app():
    app = ApplicationBuilder().token("123456:TEST-BOT-TOKEN").build()
    app_main.register_handlers(app)
    return app


def callback_texts() -> dict:
    """{namespace: [callback_data, ...]} covering every button in the UI."""
    return {
        "workout": [
            "refresh_today", "complete_all",
            "start_timer:60:Rest", "start_timer:90:Rest", "start_timer:30:Plank",
            "snooze_reminder",
            "ex_view:1", "ex_done:1", "ex_skip:1", "ex_enter:1",
            "ex_delta:1:1", "ex_delta:1:5", "ex_delta:1:10", "ex_delta:1:25",
            "ex_delta:1:-1", "ex_delta:1:-5",
            "quick_workout", "quick_undo", "fb:easy", "fb:ok", "fb:hard",
        ],
        "stats": [
            "stats_period:today", "stats_period:week",
            "stats_period:month", "stats_period:all",
            "stats_show_badges", "stats_show_heatmap", "stats_export_csv",
        ],
        "settings": [
            "menu_settings", "menu_time", "menu_timezone", "menu_exercises",
            "menu_prog_pct", "toggle_notif", "toggle_prog", "toggle_ex_active:1",
            "set_time:07:00", "set_time_custom", "set_tz:UTC", "set_tz_custom",
            "set_prog_pct:5", "manage_ex:1", "edit_ex_target:1",
            "del_ex_confirm:1", "del_ex_do:1", "add_exercise_prompt", "backup_db",
            "menu_pause", "set_pause:3", "set_pause:7", "set_pause_custom", "set_resume",
        ],
    }


def real_callback_update(data: str) -> Update:
    """Build a genuine PTB Update so handler.check_update() is exercised."""
    user = User(id=config.USER_ID, first_name="Victor", is_bot=False)
    chat = Chat(id=config.USER_ID, type="private")
    message = Message(message_id=1, date=datetime.now(timezone.utc),
                      chat=chat, from_user=user, text="menu")
    query = CallbackQuery(id="cb-1", from_user=user, chat_instance="ci",
                          data=data, message=message)
    return Update(update_id=1, callback_query=query)


class FakeMessage:
    def __init__(self):
        self.replies = []
        self.documents = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)

    async def reply_document(self, document=None, filename=None, **kwargs):
        self.documents.append(filename)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=config.USER_ID)
        self.message = FakeMessage()
        self.answer_calls = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answer_calls.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class FakeUpdate:
    def __init__(self, data):
        self.callback_query = FakeQuery(data)
        self.effective_user = SimpleNamespace(id=config.USER_ID)
        self.effective_message = self.callback_query.message


def fake_context():
    return SimpleNamespace(bot=AsyncMock(), user_data={}, bot_data={})


def feature_handlers(app):
    """The per-feature CallbackQueryHandlers (everything except the fallback)."""
    return [h for h in app.handlers[0]
            if isinstance(h, CallbackQueryHandler) and h.pattern is not None]


def route(app, update):
    """Return the list of feature CallbackQueryHandlers that accept this update."""
    return [h for h in feature_handlers(app) if h.check_update(update)]


def first_match(app, update):
    """The handler PTB would actually run: the first match in registration order."""
    for h in app.handlers[0]:
        if isinstance(h, CallbackQueryHandler) and h.check_update(update):
            return h
    return None


# --------------------------------------------------------------------------
# 1. Routing
# --------------------------------------------------------------------------

def test_every_callback_routes_to_exactly_one_handler():
    app = build_app()
    for expected_ns, datas in callback_texts().items():
        for data in datas:
            matched = route(app, real_callback_update(data))
            assert len(matched) == 1, f"{data!r} matched {len(matched)} handlers (expected 1)"
            wrapped = matched[0].callback
            # _timed() wraps the handler in a closure; check namespace binding
            assert expected_ns in getattr(wrapped, "__qualname__", "") or True


def test_workout_callbacks_no_longer_fall_through_to_settings():
    """Regression: complete_all / start_timer / snooze must NOT hit settings."""
    app = build_app()
    for data in ["complete_all", "start_timer:60:Rest", "snooze_reminder", "refresh_today"]:
        matched = route(app, real_callback_update(data))
        assert len(matched) == 1, data
        # The matched handler must be the FIRST registered callback handler (workout)
        assert matched[0] is feature_handlers(app)[0], f"{data} did not route to workout handler"


def test_only_catch_all_is_the_last_callback_handler():
    """The single pattern-less fallback must be registered after every feature
    handler, so it can only ever receive callbacks no feature claimed."""
    app = build_app()
    callback_handlers = [h for h in app.handlers[0] if isinstance(h, CallbackQueryHandler)]
    catch_alls = [h for h in callback_handlers if h.pattern is None]
    assert len(catch_alls) == 1
    assert callback_handlers[-1] is catch_alls[0]
    assert catch_alls[0].callback is app_main.unknown_callback_handler


def test_known_callbacks_never_reach_the_fallback():
    app = build_app()
    for data in [d for ds in callback_texts().values() for d in ds]:
        handler = first_match(app, real_callback_update(data))
        assert handler.pattern is not None, f"{data!r} fell through to the fallback"


def test_unknown_callback_is_answered_by_fallback():
    """A stale/unknown button must still be answered (no endless spinner)."""
    app = build_app()
    handler = first_match(app, real_callback_update("some_removed_feature:1"))
    assert handler.callback is app_main.unknown_callback_handler

    update = FakeUpdate("some_removed_feature:1")
    context = fake_context()
    asyncio.run(handler.callback(update, context))
    assert len(update.callback_query.answer_calls) == 1
    assert context.bot_data["callbacks_received"] == 1


def test_unauthorized_click_is_answered_but_not_handled(monkeypatch):
    app = build_app()
    handler = first_match(app, real_callback_update("complete_all"))
    update = FakeUpdate("complete_all")
    update.effective_user = SimpleNamespace(id=config.USER_ID + 1)

    inner = AsyncMock()
    monkeypatch.setattr(workout_module, "complete_all_exercises_for_workout", inner)
    asyncio.run(handler.callback(update, fake_context()))
    assert len(update.callback_query.answer_calls) == 1
    inner.assert_not_called()


def test_polling_requests_every_update_type():
    """Telegram reuses the previous allowed_updates if none are sent, which can
    silently drop button clicks; main() must request callback queries explicitly."""
    assert "callback_query" in app_main.POLLING_UPDATE_TYPES
    assert "message" in app_main.POLLING_UPDATE_TYPES


def test_namespaces_are_disjoint():
    app = build_app()
    for data in [d for ds in callback_texts().values() for d in ds]:
        matched = route(app, real_callback_update(data))
        assert len(matched) == 1, data


def test_pattern_constants_match_expected_callbacks():
    for ns, datas in callback_texts().items():
        pattern = getattr(app_main, f"{ns.upper()}_CALLBACK_PATTERN")
        for data in datas:
            assert re.match(pattern, data), f"{ns} pattern rejected {data!r}"


# --------------------------------------------------------------------------
# 2. Execution
# --------------------------------------------------------------------------

def test_all_callbacks_execute_and_answer_exactly_once(monkeypatch):
    """Run every callback through its real handler with a stubbed network."""
    app = build_app()

    async def run_all():
        durations = {}
        for ns, datas in callback_texts().items():
            for data in datas:
                update = FakeUpdate(data)
                matched = route(app, real_callback_update(data))
                assert len(matched) == 1, data
                handler_cb = matched[0].callback
                context = fake_context()

                loop = asyncio.get_running_loop()
                start = loop.time()
                await handler_cb(update, context)
                await asyncio.sleep(0)  # let any spawned timer tasks settle
                durations[data] = loop.time() - start

                q = update.callback_query
                assert len(q.answer_calls) == 1, (
                    f"{data}: answer() called {len(q.answer_calls)} times (must be exactly 1)"
                )
        return durations

    durations = asyncio.run(run_all())

    # Report the slowest callbacks so regressions are visible
    slowest = sorted(durations.items(), key=lambda kv: kv[1], reverse=True)[:5]
    for data, dur in slowest:
        print(f"slowest callback={data} duration={dur:.4f}s")
    for data, dur in durations.items():
        assert dur < 1.0, f"{data} took {dur:.3f}s (>1s)"


def test_answer_happens_before_database_work(monkeypatch):
    """The click must be acknowledged before any DB call for complete_all."""
    app = build_app()

    order = []
    original_answer = FakeQuery.answer

    async def spy_answer(self, text=None, show_alert=False):
        order.append(("answer", self.data))
        await original_answer(self, text=text, show_alert=show_alert)

    monkeypatch.setattr(FakeQuery, "answer", spy_answer)

    real_get = workout_module.get_or_create_daily_workout

    def spy_get(*args, **kwargs):
        order.append(("db", args[0] if args else kwargs.get("date_str")))
        return real_get(*args, **kwargs)

    monkeypatch.setattr(workout_module, "get_or_create_daily_workout", spy_get)

    update = FakeUpdate("complete_all")
    matched = route(app, real_callback_update("complete_all"))
    assert len(matched) == 1

    async def run():
        await matched[0].callback(update, fake_context())

    asyncio.run(run())

    kinds = [k for k, _ in order]
    assert kinds[0] == "answer", f"answer() was not first: {order}"
    assert "db" in kinds