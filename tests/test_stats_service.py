import pytest
from pathlib import Path

from database import init_db
from services.workout_service import get_or_create_daily_workout, update_workout_item
from services.stats_service import get_stats_for_period, format_stats_message

@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_stats.db"
    init_db(db_file)
    return db_file

def test_stats_aggregation(test_db):
    # Setup 2 completed workouts in a week
    # Day 1: 2026-10-01 (Thu)
    w1 = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    for it in w1["items"]:
        update_workout_item(it["id"], completed_reps=it["target_reps"], db_path=test_db)

    # Day 2: 2026-10-02 (Fri)
    w2 = get_or_create_daily_workout("2026-10-02", db_path=test_db)
    for it in w2["items"]:
        # Do double reps for the first exercise
        update_workout_item(it["id"], completed_reps=it["target_reps"] * 2, db_path=test_db)

    stats = get_stats_for_period(period="week", today_str="2026-10-02", db_path=test_db)
    assert stats["completed_workouts"] == 2
    assert stats["scheduled_workouts"] == 2
    assert len(stats["exercises"]) == 4

    # Format message test
    msg = format_stats_message(stats)
    assert "Workouts <b>2/2</b>" in msg
    assert "Push-ups" in msg
