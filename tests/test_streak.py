import config
import pytest
from pathlib import Path
from datetime import datetime

from database import init_db, get_connection
from services.workout_service import calculate_streaks

@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_streak.db"
    init_db(db_file)
    return db_file

def insert_workout(db_file: Path, date_str: str, status: str):
    now_iso = datetime.now().isoformat()
    with get_connection(db_file) as conn:
        conn.execute(
            "INSERT INTO daily_workouts (user_id, date, status, created_at) VALUES (?, ?, ?, ?);",
            (config.USER_ID, date_str, status, now_iso)
        )
        conn.commit()

def test_streak_consecutive_completed(test_db):
    # 3 days in a row completed
    insert_workout(test_db, "2026-10-01", "completed")
    insert_workout(test_db, "2026-10-02", "completed")
    insert_workout(test_db, "2026-10-03", "completed")

    current, best = calculate_streaks(today_str="2026-10-03", db_path=test_db)
    assert current == 3
    assert best == 3

def test_streak_with_rest_days(test_db):
    # Mon (completed), Tue (completed), Wed (rest), Thu (completed)
    insert_workout(test_db, "2026-10-01", "completed")
    insert_workout(test_db, "2026-10-02", "completed")
    insert_workout(test_db, "2026-10-03", "rest")       # Rest day: must NOT break streak!
    insert_workout(test_db, "2026-10-04", "completed")

    current, best = calculate_streaks(today_str="2026-10-04", db_path=test_db)
    assert current == 3  # 3 completed workout days
    assert best == 3

def test_streak_broken_by_missed_day(test_db):
    # 2 completed, 1 pending/missed in the past, then 1 completed
    insert_workout(test_db, "2026-10-01", "completed")
    insert_workout(test_db, "2026-10-02", "completed")
    insert_workout(test_db, "2026-10-03", "pending")  # Missed in past
    insert_workout(test_db, "2026-10-04", "completed")

    current, best = calculate_streaks(today_str="2026-10-04", db_path=test_db)
    assert current == 1
    assert best == 2

def test_today_in_progress_does_not_break_streak(test_db):
    # Yesterday completed, today is still pending
    insert_workout(test_db, "2026-10-01", "completed")
    insert_workout(test_db, "2026-10-02", "completed")
    insert_workout(test_db, "2026-10-03", "pending") # Today

    current, best = calculate_streaks(today_str="2026-10-03", db_path=test_db)
    assert current == 2
    assert best == 2
