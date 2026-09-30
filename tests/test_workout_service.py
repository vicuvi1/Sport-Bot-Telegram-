import pytest
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

from database import init_db, get_connection, set_setting
from services.workout_service import (
    get_or_create_daily_workout,
    update_workout_item,
    get_exercises,
    add_exercise,
    update_exercise,
    delete_exercise,
    toggle_exercise_active
)

@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_workout.db"
    init_db(db_file)
    return db_file

def test_default_exercises_seeded(test_db):
    exercises = get_exercises(db_path=test_db)
    assert len(exercises) == 4
    names = {ex["name"] for ex in exercises}
    assert "Push-ups" in names
    assert "Squats" in names
    assert "Sit-ups" in names
    assert "Plank" in names

def test_daily_workout_creation(test_db):
    workout = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    assert workout["date"] == "2026-10-01"
    assert workout["status"] == "pending"
    assert len(workout["items"]) == 4

    # Ensure idempotency: re-fetching does not create duplicate rows
    workout_again = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    assert workout_again["id"] == workout["id"]
    assert len(workout_again["items"]) == 4

def test_exercise_completion_and_delta(test_db):
    workout = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    item = workout["items"][0]
    item_id = item["id"]
    target = item["target_reps"]

    # Add 10 reps
    updated, _ = update_workout_item(item_id, delta_reps=10, db_path=test_db)
    assert updated["completed_reps"] == 10
    assert updated["status"] == "pending"

    # Subtract 5 reps
    updated, _ = update_workout_item(item_id, delta_reps=-5, db_path=test_db)
    assert updated["completed_reps"] == 5

    # Direct completion with exact number (e.g. entered 55 when target is 50)
    updated, _ = update_workout_item(item_id, completed_reps=55, db_path=test_db)
    assert updated["completed_reps"] == 55
    assert updated["status"] == "completed"

def test_daily_workout_completes_when_all_done(test_db):
    workout = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    
    # Complete all items
    for item in workout["items"]:
        update_workout_item(item["id"], completed_reps=item["target_reps"], db_path=test_db)

    # Check daily workout status
    refreshed = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    assert refreshed["status"] == "completed"

def test_exercise_skip(test_db):
    workout = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    item = workout["items"][0]
    
    updated, _ = update_workout_item(item["id"], status="skipped", db_path=test_db)
    assert updated["status"] == "skipped"

def test_exercise_crud(test_db):
    # Add
    new_id = add_exercise("Lunges", 40, unit="reps", db_path=test_db)
    assert new_id > 0

    # Toggle active
    state = toggle_exercise_active(new_id, db_path=test_db)
    assert state is False

    # Update
    update_exercise(new_id, target_reps=45, db_path=test_db)
    active_list = get_exercises(active_only=True, db_path=test_db)
    assert not any(e["name"] == "Lunges" for e in active_list)

    # Delete
    deleted = delete_exercise(new_id, db_path=test_db)
    assert deleted is True
