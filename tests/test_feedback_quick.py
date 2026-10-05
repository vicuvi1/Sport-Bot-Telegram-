"""Tests for difficulty feedback, the comeback ramp and quick ("short on time") workouts."""

from database import get_setting, set_setting
from handlers.workout import build_today_workout_view, feedback_notice
from services.workout_service import (
    calculate_streaks,
    check_and_apply_progression,
    get_exercises,
    get_or_create_daily_workout,
    record_feedback,
    start_pause,
    start_quick_workout,
    undo_quick_workout,
    update_workout_item,
)

# Default exercises seeded by init_db: Push-ups 50, Squats 50, Sit-ups 30, Plank 60s.
DEFAULT_TARGETS = [50, 50, 30, 60]


def targets(workout):
    return [it["target_reps"] for it in workout["items"]]


def complete(date_str):
    workout = get_or_create_daily_workout(date_str)
    for item in workout["items"]:
        update_workout_item(item["id"], completed_reps=item["target_reps"])
    return get_or_create_daily_workout(date_str)


def exercise_targets():
    return {ex["name"]: ex["target_reps"] for ex in get_exercises()}


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --------------------------------------------------------------------------
# Comeback ramp
# --------------------------------------------------------------------------

def test_first_ever_workout_is_not_a_comeback():
    workout = get_or_create_daily_workout("2026-10-01")
    assert workout["comeback_stage"] == 0 and targets(workout) == DEFAULT_TARGETS


def test_comeback_ramps_70_85_100_after_four_missed_days():
    complete("2026-10-01")
    day1 = get_or_create_daily_workout("2026-10-06")  # missed 02, 03, 04, 05
    assert day1["comeback_stage"] == 1
    assert targets(day1) == [35, 35, 21, 42]

    complete("2026-10-06")
    day2 = get_or_create_daily_workout("2026-10-07")
    assert day2["comeback_stage"] == 2
    assert targets(day2) == [43, 43, 26, 51]

    complete("2026-10-07")
    day3 = get_or_create_daily_workout("2026-10-08")
    assert day3["comeback_stage"] == 0 and targets(day3) == DEFAULT_TARGETS


def test_three_missed_days_is_not_a_comeback():
    complete("2026-10-01")
    assert get_or_create_daily_workout("2026-10-05")["comeback_stage"] == 0


def test_scheduled_rest_days_do_not_count_as_missed():
    set_setting("workout_days", "0,2,4")  # Mon / Wed / Fri
    complete("2026-10-02")  # Friday
    # Next planned day is Monday 10-05; Sat/Sun aren't workout days.
    assert get_or_create_daily_workout("2026-10-05")["comeback_stage"] == 0


def test_vacation_pause_still_triggers_comeback():
    complete("2026-10-01")
    start_pause(4, today_str="2026-10-02")  # 02..05 paused
    assert get_or_create_daily_workout("2026-10-06")["comeback_stage"] == 1


def test_comeback_view_explains_reduced_targets():
    complete("2026-10-01")
    workout = get_or_create_daily_workout("2026-10-06")
    text, _ = build_today_workout_view(workout, "2026-10-06")
    assert "Comeback day" in text and "70%" in text


# --------------------------------------------------------------------------
# Quick workout ("short on time")
# --------------------------------------------------------------------------

def test_quick_workout_halves_pending_targets():
    workout = get_or_create_daily_workout("2026-10-10")
    update_workout_item(workout["items"][0]["id"], completed_reps=30)  # Push-ups 30/50
    update_workout_item(workout["items"][1]["id"], status="skipped")   # Squats skipped

    quick = start_quick_workout(workout["id"])
    assert quick["quick"] == 1
    pushups, squats, situps, plank = quick["items"]
    assert (pushups["target_reps"], pushups["status"]) == (25, "completed")  # 30 ≥ 25
    assert (squats["target_reps"], squats["status"]) == (50, "skipped")      # untouched
    assert situps["target_reps"] == 15 and situps["full_target_reps"] == 30
    assert plank["target_reps"] == 30


def test_quick_workout_counts_for_streak():
    workout = get_or_create_daily_workout("2026-10-10")
    start_quick_workout(workout["id"])
    done = complete("2026-10-10")
    assert done["status"] == "completed"
    assert calculate_streaks(today_str="2026-10-10")[0] == 1


def test_undo_quick_restores_full_targets():
    workout = get_or_create_daily_workout("2026-10-10")
    update_workout_item(workout["items"][0]["id"], completed_reps=30)
    start_quick_workout(workout["id"])

    restored = undo_quick_workout(workout["id"])
    assert restored["quick"] == 0
    assert targets(restored) == DEFAULT_TARGETS
    assert restored["items"][0]["status"] == "pending"  # 30/50 again
    assert all(it["full_target_reps"] is None for it in restored["items"])


def test_quick_only_applies_to_pending_full_workouts():
    done = complete("2026-10-10")
    assert start_quick_workout(done["id"]) is None
    assert undo_quick_workout(done["id"]) is None

    workout = get_or_create_daily_workout("2026-10-11")
    start_quick_workout(workout["id"])
    assert start_quick_workout(workout["id"]) is None  # can't halve twice


def test_quick_days_do_not_trigger_auto_progression():
    set_setting("auto_progression_enabled", "1")
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        workout = get_or_create_daily_workout(day)
        start_quick_workout(workout["id"])
        complete(day)
    push_ups_id = get_exercises()[0]["id"]
    assert check_and_apply_progression(push_ups_id) is None
    assert exercise_targets()["Push-ups"] == 50


def test_view_offers_quick_button_then_undo():
    workout = get_or_create_daily_workout("2026-10-10")
    _, markup = build_today_workout_view(workout, "2026-10-10")
    assert "quick_workout" in callbacks(markup)

    quick = start_quick_workout(workout["id"])
    text, markup = build_today_workout_view(quick, "2026-10-10")
    assert "Quick workout" in text
    assert "quick_undo" in callbacks(markup) and "quick_workout" not in callbacks(markup)


# --------------------------------------------------------------------------
# Difficulty feedback
# --------------------------------------------------------------------------

def test_completed_full_workout_asks_for_feedback():
    done = complete("2026-10-10")
    text, markup = build_today_workout_view(done, "2026-10-10")
    assert "How did it feel?" in text
    assert {"fb:easy", "fb:ok", "fb:hard"} <= set(callbacks(markup))


def test_completed_quick_workout_does_not_ask_for_feedback():
    workout = get_or_create_daily_workout("2026-10-10")
    start_quick_workout(workout["id"])
    done = complete("2026-10-10")
    _, markup = build_today_workout_view(done, "2026-10-10")
    assert not [c for c in callbacks(markup) if c.startswith("fb:")]
    assert record_feedback(done["id"], "easy")["recorded"] is False


def test_too_easy_twice_in_a_row_raises_targets():
    first = record_feedback(complete("2026-10-10")["id"], "easy")
    assert first["recorded"] and first["changes"] == [] and first["easy_streak"] == 1
    assert "next time" in feedback_notice(first)

    second = record_feedback(complete("2026-10-11")["id"], "easy")
    assert second["easy_streak"] == 0
    assert exercise_targets() == {"Push-ups": 55, "Squats": 55, "Sit-ups": 33, "Plank": 66}
    assert "Targets raised" in feedback_notice(second) and "Push-ups 50→55" in feedback_notice(second)


def test_just_right_resets_the_easy_count():
    record_feedback(complete("2026-10-10")["id"], "easy")
    record_feedback(complete("2026-10-11")["id"], "ok")
    record_feedback(complete("2026-10-12")["id"], "easy")
    assert exercise_targets()["Push-ups"] == 50
    assert get_setting("easy_feedback_streak") == "1"


def test_too_hard_lowers_targets_immediately():
    result = record_feedback(complete("2026-10-10")["id"], "hard")
    assert exercise_targets() == {"Push-ups": 45, "Squats": 45, "Sit-ups": 27, "Plank": 54}
    assert "Targets lowered" in feedback_notice(result)


def test_feedback_only_once_per_day_and_only_when_completed():
    pending = get_or_create_daily_workout("2026-10-10")
    assert record_feedback(pending["id"], "hard")["recorded"] is False

    done = complete("2026-10-10")
    assert record_feedback(done["id"], "hard")["recorded"] is True
    again = record_feedback(done["id"], "hard")
    assert again["recorded"] is False and "already saved" in feedback_notice(again)
    assert exercise_targets()["Push-ups"] == 45  # lowered once, not twice

    text, markup = build_today_workout_view(get_or_create_daily_workout("2026-10-10"), "2026-10-10")
    assert "You said: 🥵 Too hard" in text
    assert not [c for c in callbacks(markup) if c.startswith("fb:")]


def test_easy_on_a_comeback_day_does_not_count():
    complete("2026-10-01")
    comeback = complete("2026-10-06")
    assert comeback["comeback_stage"] == 1
    result = record_feedback(comeback["id"], "easy")
    assert result["easy_streak"] == 0
    assert "Comeback days" in feedback_notice(result)


def test_targets_never_drop_below_one():
    from database import get_connection
    with get_connection() as conn:
        conn.execute("UPDATE exercises SET target_reps = 1;")
        conn.commit()
    result = record_feedback(complete("2026-10-10")["id"], "hard")
    assert result["changes"] == []
    assert set(exercise_targets().values()) == {1}


# --------------------------------------------------------------------------
# Upgrading an existing (pre-feedback) database
# --------------------------------------------------------------------------

def test_init_db_upgrades_old_database_and_keeps_history(tmp_path):
    import sqlite3
    from database import init_db

    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript("""
        CREATE TABLE daily_workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, completed_at TEXT);
        CREATE TABLE workout_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, daily_workout_id INTEGER NOT NULL,
            exercise_id INTEGER, exercise_name TEXT NOT NULL, target_reps INTEGER NOT NULL,
            completed_reps INTEGER NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT 'reps',
            status TEXT NOT NULL DEFAULT 'pending', updated_at TEXT);
        INSERT INTO daily_workouts (date, status, created_at) VALUES ('2026-09-30', 'completed', 'x');
        INSERT INTO workout_items (daily_workout_id, exercise_name, target_reps, completed_reps, status)
            VALUES (1, 'Push-ups', 50, 50, 'completed');
    """)
    conn.commit()
    conn.close()

    init_db(old_db)
    init_db(old_db)  # running twice (every restart) must be harmless

    conn = sqlite3.connect(old_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM daily_workouts WHERE date = '2026-09-30';").fetchone()
    assert row["status"] == "completed"
    assert row["feedback"] is None and row["quick"] == 0 and row["comeback_stage"] == 0
    item = conn.execute("SELECT * FROM workout_items;").fetchone()
    assert item["completed_reps"] == 50 and item["full_target_reps"] is None
    conn.close()
