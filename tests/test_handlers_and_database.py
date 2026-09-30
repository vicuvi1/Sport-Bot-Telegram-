import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from telegram import Update, User, Message, Chat

import config
from database import init_db, get_setting, set_setting, create_backup
from handlers.start import is_authorized, get_main_menu_keyboard
from handlers.workout import build_today_workout_view, build_exercise_detail_view
from handlers.settings import build_settings_menu
from services.workout_service import get_or_create_daily_workout

@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_app.db"
    init_db(db_file)
    return db_file

def test_is_authorized():
    # Matching authorized user
    update_auth = MagicMock(spec=Update)
    user_auth = MagicMock(spec=User)
    user_auth.id = config.USER_ID
    update_auth.effective_user = user_auth

    assert is_authorized(update_auth) is True

    # Unknown user
    update_unauth = MagicMock(spec=Update)
    user_unauth = MagicMock(spec=User)
    user_unauth.id = 999999999
    update_unauth.effective_user = user_unauth

    assert is_authorized(update_unauth) is False

def test_main_menu_keyboard():
    markup = get_main_menu_keyboard()
    button_texts = [btn.text for row in markup.keyboard for btn in row]
    assert "🏋️ Today's Workout" in button_texts
    assert "📊 Progress" in button_texts
    assert "📅 History" in button_texts
    assert "⚙️ Settings" in button_texts

def test_today_workout_view(test_db):
    workout = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    text, markup = build_today_workout_view(workout, "2026-10-01")
    assert "Today's Workout" in text
    assert len(markup.inline_keyboard) > 0

def test_exercise_detail_view(test_db):
    workout = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    item = workout["items"][0]
    text, markup = build_exercise_detail_view(item)
    assert item["exercise_name"] in text
    assert "*Goal:*" in text
    assert str(item["target_reps"]) in text

def test_settings_menu_rendering(test_db):
    text, markup = build_settings_menu()
    assert "Bot Settings & Configuration" in text
    assert any("Notifications" in btn.text for row in markup.inline_keyboard for btn in row)

def test_database_backup(test_db, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_file = create_backup(db_path=test_db, backup_dir=backup_dir)
    assert backup_file.exists()
    assert backup_file.stat().st_size > 0
