import pytest
from pathlib import Path

from database import init_db, get_connection
from services.workout_service import (
    render_progress_bar,
    get_or_create_daily_workout,
    complete_all_exercises_for_workout,
    generate_workout_heatmap,
    get_user_badges,
    export_workouts_csv
)

@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_features.db"
    init_db(db_file)
    return db_file

def test_render_progress_bar():
    # 0%
    bar_0 = render_progress_bar(0, 50, length=8)
    assert "[░░░░░░░░] 0%" in bar_0

    # 50%
    bar_50 = render_progress_bar(25, 50, length=8)
    assert "[████░░░░] 50%" in bar_50

    # 100%
    bar_100 = render_progress_bar(50, 50, length=8)
    assert "[████████] 100% ✅" in bar_100

def test_complete_all_exercises(test_db):
    workout = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    updated, progs = complete_all_exercises_for_workout(workout["id"], db_path=test_db)

    assert updated["status"] == "completed"
    assert len(updated["items"]) == 4
    for it in updated["items"]:
        assert it["status"] == "completed"
        assert it["completed_reps"] == it["target_reps"]

def test_generate_workout_heatmap(test_db):
    # Create a completed workout
    w = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    complete_all_exercises_for_workout(w["id"], db_path=test_db)

    heatmap = generate_workout_heatmap(weeks_count=4, today_str="2026-10-01", db_path=test_db)
    assert "Workout Heatmap" in heatmap
    assert "M   T   W   T   F   S   S" in heatmap
    assert "🟩 done" in heatmap

def test_user_badges_earned_and_progress(test_db):
    # Before workout: First Step is not unlocked
    badges_before = get_user_badges(today_str="2026-10-01", db_path=test_db)
    first_step_before = next(b for b in badges_before if b["name"] == "First Step")
    assert first_step_before["unlocked"] is False

    # Complete a workout
    w = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    complete_all_exercises_for_workout(w["id"], db_path=test_db)

    # After workout: First Step unlocked!
    badges_after = get_user_badges(today_str="2026-10-01", db_path=test_db)
    first_step_after = next(b for b in badges_after if b["name"] == "First Step")
    assert first_step_after["unlocked"] is True

def test_export_workouts_csv(test_db, tmp_path):
    w = get_or_create_daily_workout("2026-10-01", db_path=test_db)
    complete_all_exercises_for_workout(w["id"], db_path=test_db)

    export_path = export_workouts_csv(db_path=test_db, export_dir=tmp_path)
    assert export_path.exists()
    content = export_path.read_text(encoding="utf-8")
    assert "Date,Workout Status,Completed At" in content
    assert "2026-10-01,completed" in content
    assert "Push-ups" in content
