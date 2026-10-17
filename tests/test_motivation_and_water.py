from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from database import current_user_id, get_setting, init_db, set_setting
import config
from services import quote_service as qs
from services import water_service as ws
from services import workout_service as wks
from handlers import quicklog, settings
from views import HTML


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_workout.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "USER_ID", 1900611848)
    init_db(db_file)
    from database import bind_user
    bind_user(config.USER_ID)
    return db_file


def test_quote_personas_and_cues():
    for persona in ("beast", "stoic", "system", "athlete"):
        quote = qs.get_morning_quote(persona)
        assert len(quote) > 10
        for event in ("first_blood", "halfway", "overdelivered", "victory"):
            hype = qs.get_in_workout_quote(event, persona)
            assert len(hype) > 5

    cues = qs.get_form_cues("push-ups")
    assert cues and len(cues) >= 2
    assert any("elbows" in c.lower() for c in cues)

    pacing = qs.get_smart_pacing("push-ups", 105)
    assert pacing and "3 sets" in pacing and "90s rest" in pacing


def test_water_personalized_target_and_logging():
    target = ws.calculate_personalized_target(75.0)
    assert target == 2700

    status = ws.get_today_water()
    assert status["total_ml"] == 0
    assert status["target_ml"] == 2700
    assert not status["is_completed"]

    ws.log_water(500)
    status2 = ws.get_today_water()
    assert status2["total_ml"] == 500
    assert status2["percentage"] == 19
    assert status2["glasses"] == 2.0

    ws.log_water(2200)
    status3 = ws.get_today_water()
    assert status3["total_ml"] == 2700
    assert status3["is_completed"]

    text, markup = ws.build_water_view(status3)
    assert "Hydration Tracker" in text
    assert "Daily Hydration Goal Smashed" in text
    assert any(b.callback_data == "water_log:250" for row in markup.inline_keyboard for b in row)


@pytest.mark.anyio
async def test_flexible_water_commands():
    update = SimpleNamespace(
        message=SimpleNamespace(
            text="water 500",
            reply_text=AsyncMock()
        )
    )
    context = SimpleNamespace()
    handled = await quicklog.handle_flexible_commands(update, context)
    assert handled
    update.message.reply_text.assert_called_once()
    assert ws.get_today_water()["total_ml"] == 500

    # Test "water" view command
    update_view = SimpleNamespace(
        message=SimpleNamespace(
            text="water",
            reply_text=AsyncMock()
        )
    )
    handled_view = await quicklog.handle_flexible_commands(update_view, context)
    assert handled_view
    update_view.message.reply_text.assert_called_once()


@pytest.mark.anyio
async def test_flexible_pause_and_resume():
    update_sick = SimpleNamespace(
        message=SimpleNamespace(
            text="sick today",
            reply_text=AsyncMock()
        )
    )
    context = SimpleNamespace()
    assert await quicklog.handle_flexible_commands(update_sick, context)
    assert wks.get_active_pause() is not None

    update_resume = SimpleNamespace(
        message=SimpleNamespace(
            text="resume",
            reply_text=AsyncMock()
        )
    )
    assert await quicklog.handle_flexible_commands(update_resume, context)
    assert wks.get_active_pause() is None


@pytest.mark.anyio
async def test_flexible_target_adjustments():
    today = wks.get_current_date_str()
    workout = wks.get_or_create_daily_workout(today)
    push_item = next(it for it in workout["items"] if "push" in it["exercise_name"].lower())

    # Today's target only
    update_today = SimpleNamespace(
        message=SimpleNamespace(
            text=f"today pushups 85",
            reply_text=AsyncMock()
        )
    )
    context = SimpleNamespace()
    assert await quicklog.handle_flexible_commands(update_today, context)

    workout_after = wks.get_or_create_daily_workout(today)
    push_after = next(it for it in workout_after["items"] if "push" in it["exercise_name"].lower())
    assert push_after["target_reps"] == 85

    # Permanent catalog target
    update_perm = SimpleNamespace(
        message=SimpleNamespace(
            text=f"target pushups 65",
            reply_text=AsyncMock()
        )
    )
    assert await quicklog.handle_flexible_commands(update_perm, context)
    ex = wks.get_exercise_by_id(push_after["exercise_id"])
    assert ex["target_reps"] == 65


@pytest.mark.anyio
async def test_flexible_add_and_swap_exercise():
    today = wks.get_current_date_str()
    update_add = SimpleNamespace(
        message=SimpleNamespace(
            text="add dips 25",
            reply_text=AsyncMock()
        )
    )
    context = SimpleNamespace()
    assert await quicklog.handle_flexible_commands(update_add, context)

    workout = wks.get_or_create_daily_workout(today)
    dips = next((it for it in workout["items"] if "dips" in it["exercise_name"].lower()), None)
    assert dips is not None
    assert dips["target_reps"] == 25

    # Swap push-ups for chin-ups
    update_swap = SimpleNamespace(
        message=SimpleNamespace(
            text="swap push-ups for chin-ups",
            reply_text=AsyncMock()
        )
    )
    assert await quicklog.handle_flexible_commands(update_swap, context)
    workout_swapped = wks.get_or_create_daily_workout(today)
    assert any("chin" in it["exercise_name"].lower() for it in workout_swapped["items"])


@pytest.mark.anyio
async def test_settings_persona_and_water():
    text, markup = settings.build_settings_menu()
    assert "Coach" in text
    assert "Hydration" in text
    assert any("menu_persona" in b.callback_data for row in markup.inline_keyboard for b in row)
    assert any("menu_water_settings" in b.callback_data for row in markup.inline_keyboard for b in row)

    # Test persona menu and selection
    p_text, p_markup = settings.build_persona_menu()
    assert "Beast Mode" in p_text

    query = SimpleNamespace(
        data="set_persona:stoic",
        answer=AsyncMock(),
        edit_message_text=AsyncMock()
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=config.USER_ID))
    context = SimpleNamespace(user_data={}, bot_data={})
    await settings.settings_callback_handler(update, context)
    assert get_setting("coach_persona") == "stoic"

    # Test water time text setting
    context.user_data["awaiting_setting"] = "morning_water_time"
    update_time = SimpleNamespace(
        message=SimpleNamespace(
            text="06:30",
            reply_text=AsyncMock()
        ),
        effective_user=SimpleNamespace(id=config.USER_ID)
    )
    assert await settings.settings_text_input_handler(update_time, context)
    assert get_setting("morning_water_time") == "06:30"
