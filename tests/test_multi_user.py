"""Multi-user foundation: migration of a single-user database, isolation
between crew members, ownership checks, and per-update user binding."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import main as app_main
import scheduler as sched
from database import (
    NOBODY,
    add_user,
    as_user,
    bind_user,
    current_user_id,
    get_connection,
    get_setting,
    get_users,
    init_db,
    remove_user,
    set_setting,
)
from handlers.start import is_authorized
from services.fitness_test_service import get_month_results, save_result
from services.workout_service import (
    calculate_streaks,
    complete_all_exercises_for_workout,
    delete_exercise,
    get_exercises,
    get_or_create_daily_workout,
    record_feedback,
    start_pause,
    update_workout_item,
)

BROTHER = 222000333

# The schema as it was in production before multi-user (commit 0f7c3e3).
OLD_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    target_reps INTEGER NOT NULL, unit TEXT NOT NULL DEFAULT 'reps', is_active INTEGER NOT NULL DEFAULT 1,
    days_of_week TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6', created_at TEXT NOT NULL);
CREATE TABLE daily_workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL, completed_at TEXT, feedback TEXT, quick INTEGER NOT NULL DEFAULT 0,
    comeback_stage INTEGER NOT NULL DEFAULT 0);
CREATE TABLE workout_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, daily_workout_id INTEGER NOT NULL, exercise_id INTEGER,
    exercise_name TEXT NOT NULL, target_reps INTEGER NOT NULL, completed_reps INTEGER NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT 'reps', status TEXT NOT NULL DEFAULT 'pending', updated_at TEXT,
    full_target_reps INTEGER,
    FOREIGN KEY (daily_workout_id) REFERENCES daily_workouts(id) ON DELETE CASCADE,
    FOREIGN KEY (exercise_id) REFERENCES exercises(id) ON DELETE SET NULL);
CREATE TABLE scheduled_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, fire_at TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '', seconds INTEGER NOT NULL DEFAULT 0, chat_id INTEGER NOT NULL);
CREATE TABLE pauses (id INTEGER PRIMARY KEY AUTOINCREMENT, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE fitness_results (month TEXT NOT NULL, test_key TEXT NOT NULL, value INTEGER,
    recorded_at TEXT NOT NULL, PRIMARY KEY (month, test_key));

INSERT INTO settings VALUES ('workout_time', '06:30'), ('timezone', 'Europe/Chisinau'),
    ('notifications_enabled', '1'), ('partner_name', 'Ana'), ('easy_feedback_streak', '1');
INSERT INTO exercises (id, name, target_reps, unit, created_at) VALUES
    (1, 'Push-ups', 55, 'reps', 'x'), (2, 'Squats', 50, 'reps', 'x'), (3, 'Plank', 60, 'sec', 'x');
INSERT INTO daily_workouts (id, date, status, created_at, completed_at, feedback) VALUES
    (10, '2026-10-01', 'completed', 'x', '2026-10-01T07:40:00', 'easy'),
    (11, '2026-10-02', 'completed', 'x', '2026-10-02T08:10:00', NULL),
    (12, '2026-10-03', 'pending', 'x', NULL, NULL);
INSERT INTO workout_items (id, daily_workout_id, exercise_id, exercise_name, target_reps, completed_reps, status) VALUES
    (100, 10, 1, 'Push-ups', 55, 55, 'completed'), (101, 10, 2, 'Squats', 50, 50, 'completed'),
    (102, 11, 1, 'Push-ups', 55, 60, 'completed'), (103, 12, 1, 'Push-ups', 55, 10, 'pending');
INSERT INTO pauses (start_date, end_date, created_at) VALUES ('2026-09-20', '2026-09-22', 'x');
INSERT INTO fitness_results VALUES ('2026-09', 'pushups', 31, 'x'), ('2026-10', 'pushups', 35, 'x');
"""


@pytest.fixture
def old_db(tmp_path):
    path = tmp_path / "old_workout.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.close()
    return path


def rows(db, sql, params=()):
    with get_connection(db) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def test_migration_moves_all_history_to_the_owner(old_db):
    init_db(old_db)
    owner = config.USER_ID

    workouts = rows(old_db, "SELECT id, user_id, date, status, feedback FROM daily_workouts ORDER BY id")
    assert [(w["id"], w["user_id"], w["date"]) for w in workouts] == [
        (10, owner, "2026-10-01"), (11, owner, "2026-10-02"), (12, owner, "2026-10-03")]
    assert workouts[0]["feedback"] == "easy"
    assert {e["user_id"] for e in rows(old_db, "SELECT user_id FROM exercises")} == {owner}
    assert len(rows(old_db, "SELECT id FROM exercises")) == 3  # owner had exercises: no defaults added
    assert rows(old_db, "SELECT user_id FROM pauses") == [{"user_id": owner}]
    assert {(r["user_id"], r["month"]) for r in rows(old_db, "SELECT user_id, month FROM fitness_results")} == {
        (owner, "2026-09"), (owner, "2026-10")}
    # Items untouched and still linked.
    assert len(rows(old_db, "SELECT id FROM workout_items")) == 4
    assert rows(old_db, "PRAGMA foreign_key_check") == []


def test_migration_keeps_settings_and_global_state(old_db):
    init_db(old_db)
    owner_settings = {r["key"]: r["value"] for r in rows(
        old_db, "SELECT key, value FROM user_settings WHERE user_id = ?", (config.USER_ID,))}
    assert owner_settings["workout_time"] == "06:30"
    assert owner_settings["easy_feedback_streak"] == "1"
    assert owner_settings["roast_level"] == "savage"  # new defaults filled in
    assert rows(old_db, "SELECT value FROM settings WHERE key = 'partner_name'") == [{"value": "Ana"}]
    assert rows(old_db, "SELECT user_id, role FROM users") == [{"user_id": config.USER_ID, "role": "owner"}]


def test_migration_saves_a_copy_first_and_runs_once(old_db):
    init_db(old_db)
    copies = list(config.BACKUP_DIR.glob("pre_multiuser_*.db"))
    assert len(copies) == 1
    # The copy is the untouched old database.
    assert rows(copies[0], "SELECT COUNT(*) AS n FROM daily_workouts") == [{"n": 3}]
    init_db(old_db)
    assert len(list(config.BACKUP_DIR.glob("pre_multiuser_*.db"))) == 1


def test_migrated_database_still_enforces_constraints(old_db):
    init_db(old_db)
    with get_connection(old_db) as conn:
        # Cascade still works: deleting a day deletes its items.
        conn.execute("DELETE FROM daily_workouts WHERE id = 10;")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM workout_items WHERE daily_workout_id = 10;").fetchone()[0] == 0
        # One day per user, but another user may use the same date.
        conn.execute("INSERT INTO daily_workouts (user_id, date, status, created_at) VALUES (?, '2026-10-02', 'pending', 'x');",
                     (BROTHER,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO daily_workouts (user_id, date, status, created_at) VALUES (?, '2026-10-02', 'pending', 'x');",
                         (config.USER_ID,))
        # New ids continue after the migrated ones.
        cur = conn.execute("INSERT INTO daily_workouts (user_id, date, status, created_at) VALUES (?, '2026-11-01', 'pending', 'x');",
                           (config.USER_ID,))
        assert cur.lastrowid > 12


def test_migrated_history_works_through_the_app(old_db, monkeypatch):
    init_db(old_db)
    monkeypatch.setattr(config, "DB_PATH", old_db)
    assert calculate_streaks(today_str="2026-10-03") == (2, 2)
    assert get_or_create_daily_workout("2026-10-03")["items"][0]["completed_reps"] == 10


# --------------------------------------------------------------------------
# Isolation between crew members
# --------------------------------------------------------------------------

def test_new_member_gets_own_defaults():
    add_user(BROTHER, "Andrei")
    with as_user(BROTHER):
        exercises = get_exercises()
        assert [e["name"] for e in exercises] == ["Push-ups", "Squats", "Sit-ups", "Plank"]
        assert get_setting("roast_level") == "savage"
    owner_ids = {e["id"] for e in get_exercises()}
    assert owner_ids.isdisjoint({e["id"] for e in exercises})
    assert [u["user_id"] for u in get_users()] == [config.USER_ID, BROTHER]


def test_workouts_streaks_and_settings_are_separate():
    add_user(BROTHER, "Andrei")
    owner_day = get_or_create_daily_workout("2026-10-05")
    complete_all_exercises_for_workout(owner_day["id"])
    set_setting("workout_time", "06:00")

    with as_user(BROTHER):
        brother_day = get_or_create_daily_workout("2026-10-05")
        assert brother_day["id"] != owner_day["id"]
        assert brother_day["status"] == "pending"
        assert calculate_streaks(today_str="2026-10-06") == (0, 0)
        assert get_setting("workout_time") == "07:00"
        set_setting("workout_time", "08:15")
        start_pause(3, today_str="2026-10-06")
        save_result("2026-10", "pushups", 40)

    assert get_setting("workout_time") == "06:00"
    assert calculate_streaks(today_str="2026-10-06") == (1, 1)
    assert get_or_create_daily_workout("2026-10-06")["status"] == "pending"  # the pause was the brother's
    assert get_month_results("2026-10") == {}


def test_members_cannot_touch_each_others_data():
    add_user(BROTHER, "Andrei")
    owner_day = get_or_create_daily_workout("2026-10-05")
    owner_item = owner_day["items"][0]
    owner_exercise = get_exercises()[0]

    with as_user(BROTHER):
        assert update_workout_item(owner_item["id"], completed_reps=999) == (None, None)
        with pytest.raises(PermissionError):
            complete_all_exercises_for_workout(owner_day["id"])
        assert record_feedback(owner_day["id"], "hard")["recorded"] is False
        assert delete_exercise(owner_exercise["id"]) is False

    refreshed = get_or_create_daily_workout("2026-10-05")
    assert refreshed["items"][0]["completed_reps"] == 0
    assert refreshed["status"] == "pending"
    assert len(get_exercises()) == 4


def test_remove_user_deletes_only_their_data():
    add_user(BROTHER, "Andrei")
    with as_user(BROTHER):
        get_or_create_daily_workout("2026-10-05")
    get_or_create_daily_workout("2026-10-05")
    remove_user(BROTHER)
    assert [u["user_id"] for u in get_users()] == [config.USER_ID]
    assert rows(None, "SELECT DISTINCT user_id FROM daily_workouts") == [{"user_id": config.USER_ID}]
    with pytest.raises(ValueError):
        remove_user(config.USER_ID)


# --------------------------------------------------------------------------
# Who is the current user?
# --------------------------------------------------------------------------

def fake_update(user_id):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, first_name="X"))


def test_is_authorized_accepts_members_and_binds_them():
    add_user(BROTHER, "Andrei")

    async def check():
        assert is_authorized(fake_update(BROTHER)) is True
        assert current_user_id() == BROTHER
        assert is_authorized(fake_update(999)) is False
    asyncio.run(check())


def test_update_binding_gives_strangers_no_data():
    add_user(BROTHER, "Andrei")

    async def check():
        await app_main.bind_update_user(fake_update(BROTHER), None)
        assert current_user_id() == BROTHER
        await app_main.bind_update_user(fake_update(999), None)
        assert current_user_id() == NOBODY
        assert get_exercises() == []
    asyncio.run(check())


def test_each_member_gets_their_own_jobs_and_messages():
    add_user(BROTHER, "Andrei")
    with as_user(BROTHER):
        set_setting("workout_time", "08:15")
    scheduler = AsyncIOScheduler()
    bot = AsyncMock()
    for user in get_users():
        sched.reschedule_user_jobs(scheduler, bot, user["user_id"])

    owner_job = scheduler.get_job(f"daily_morning_workout:{config.USER_ID}")
    brother_job = scheduler.get_job(f"daily_morning_workout:{BROTHER}")
    assert owner_job and brother_job
    assert str(brother_job.trigger).count("hour='8'") == 1

    asyncio.run(sched.run_as(BROTHER, sched.send_daily_workout_notification, bot))
    assert bot.send_message.await_args.kwargs["chat_id"] == BROTHER
