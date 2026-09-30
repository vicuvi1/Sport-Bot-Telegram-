import pytest
from pathlib import Path
from datetime import datetime

from database import init_db, set_setting, get_connection
from services.workout_service import (
    get_exercises,
    get_or_create_daily_workout,
    update_workout_item,
    check_and_apply_progression
)

@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_progression.db"
    init_db(db_file)
    return db_file

def test_progression_disabled_by_default(test_db):
    # Default is disabled (0)
    exercises = get_exercises(db_path=test_db)
    pushups = next(e for e in exercises if e["name"] == "Push-ups")

    # Complete 3 workouts
    for d in ["2026-10-01", "2026-10-02", "2026-10-03"]:
        w = get_or_create_daily_workout(d, db_path=test_db)
        item = next(it for it in w["items"] if it["exercise_id"] == pushups["id"])
        update_workout_item(item["id"], completed_reps=50, db_path=test_db)

    # Check progression
    prog = check_and_apply_progression(pushups["id"], db_path=test_db)
    assert prog is None

def test_progression_enabled_triggers_increase(test_db):
    set_setting("auto_progression_enabled", "1", db_path=test_db)
    set_setting("progression_percentage", "5", db_path=test_db)
    set_setting("progression_consecutive_workouts", "3", db_path=test_db)

    exercises = get_exercises(db_path=test_db)
    pushups = next(e for e in exercises if e["name"] == "Push-ups")
    initial_target = pushups["target_reps"] # 50

    # Complete 3 workouts consecutively
    last_prog = None
    for d in ["2026-10-01", "2026-10-02", "2026-10-03"]:
        w = get_or_create_daily_workout(d, db_path=test_db)
        item = next(it for it in w["items"] if it["exercise_id"] == pushups["id"])
        _, last_prog = update_workout_item(item["id"], completed_reps=initial_target, db_path=test_db)

    assert last_prog is not None
    assert last_prog["old_target"] == 50
    # 5% of 50 is 2.5 -> ceil is 3 -> new target is 53
    assert last_prog["new_target"] == 53

    # Check exercises table was updated
    updated_exercises = get_exercises(db_path=test_db)
    updated_pushups = next(e for e in updated_exercises if e["name"] == "Push-ups")
    assert updated_pushups["target_reps"] == 53
