import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Dict, List
import config

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Returns a SQLite connection with Row factory and foreign keys enabled."""
    target_path = db_path or config.DB_PATH
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, ddl: str) -> None:
    """Adds `column` to `table` if it doesn't exist yet."""
    existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table});").fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")

def init_db(db_path: Optional[Path] = None) -> None:
    """Initializes tables, default settings, and default exercises."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # 1. Settings Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

        # 2. Exercises Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS exercises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                target_reps INTEGER NOT NULL,
                unit TEXT NOT NULL DEFAULT 'reps',
                is_active INTEGER NOT NULL DEFAULT 1,
                days_of_week TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
                created_at TEXT NOT NULL
            );
        """)

        # 3. Daily Workouts Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
        """)

        # 4. Daily Workout Items (History & Completion details)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workout_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                daily_workout_id INTEGER NOT NULL,
                exercise_id INTEGER,
                exercise_name TEXT NOT NULL,
                target_reps INTEGER NOT NULL,
                completed_reps INTEGER NOT NULL DEFAULT 0,
                unit TEXT NOT NULL DEFAULT 'reps',
                status TEXT NOT NULL DEFAULT 'pending',
                updated_at TEXT,
                FOREIGN KEY (daily_workout_id) REFERENCES daily_workouts(id) ON DELETE CASCADE,
                FOREIGN KEY (exercise_id) REFERENCES exercises(id) ON DELETE SET NULL
            );
        """)

        # 5. Scheduled one-off alerts (rest timers, snoozed reminders).
        # Persisted so they survive a bot restart; fire_at is a UTC ISO string.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                fire_at TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                seconds INTEGER NOT NULL DEFAULT 0,
                chat_id INTEGER NOT NULL
            );
        """)

        # 6. Vacation / sick pauses. Dates are inclusive YYYY-MM-DD strings.
        # Kept as history so past streaks and stats stay correct.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pauses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

        # 7. Monthly fitness test results. value NULL = test skipped that month.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fitness_results (
                month TEXT NOT NULL,
                test_key TEXT NOT NULL,
                value INTEGER,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (month, test_key)
            );
        """)

        # Columns added after the first release. ALTER TABLE keeps existing
        # databases (and their history) working without a manual migration.
        _ensure_column(cursor, "daily_workouts", "feedback", "TEXT")           # easy / ok / hard
        _ensure_column(cursor, "daily_workouts", "quick", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cursor, "daily_workouts", "comeback_stage", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cursor, "workout_items", "full_target_reps", "INTEGER")  # set during a quick workout

        # Seed default settings if not present
        default_settings = {
            "workout_time": config.WORKOUT_TIME or "07:00",
            "timezone": config.TIMEZONE or "Europe/Chisinau",
            "notifications_enabled": "1",
            "auto_progression_enabled": "0",
            "progression_percentage": "5",
            "progression_consecutive_workouts": "3",
            "workout_days": "0,1,2,3,4,5,6",  # Mon-Sun active by default
            "easy_feedback_streak": "0"       # consecutive "too easy" answers
        }
        for key, val in default_settings.items():
            cursor.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);",
                (key, str(val))
            )

        # Seed default exercises if table is empty
        cursor.execute("SELECT COUNT(*) as cnt FROM exercises;")
        count = cursor.fetchone()["cnt"]
        if count == 0:
            now_iso = datetime.now().isoformat()
            default_exercises = [
                ("Push-ups", 50, "reps", 1, "0,1,2,3,4,5,6", now_iso),
                ("Squats", 50, "reps", 1, "0,1,2,3,4,5,6", now_iso),
                ("Sit-ups", 30, "reps", 1, "0,1,2,3,4,5,6", now_iso),
                ("Plank", 60, "sec", 1, "0,1,2,3,4,5,6", now_iso),
            ]
            cursor.executemany(
                """
                INSERT INTO exercises (name, target_reps, unit, is_active, days_of_week, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                default_exercises
            )

        conn.commit()

def get_setting(key: str, default: Any = None, db_path: Optional[Path] = None) -> Any:
    """Fetches a setting value by key."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?;", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: Any, db_path: Optional[Path] = None) -> None:
    """Sets or updates a setting."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """,
            (key, str(value))
        )
        conn.commit()

def get_all_settings(db_path: Optional[Path] = None) -> Dict[str, str]:
    """Returns a dictionary of all current settings."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings;")
        rows = cursor.fetchall()
        return {r["key"]: r["value"] for r in rows}

def create_backup(db_path: Optional[Path] = None, backup_dir: Optional[Path] = None) -> Path:
    """Creates a timestamped online SQLite backup and returns its path."""
    source_path = db_path or config.DB_PATH
    target_dir = backup_dir or config.BACKUP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = target_dir / f"workout_backup_{timestamp}.db"
    
    src_conn = get_connection(source_path)
    dst_conn = sqlite3.connect(backup_file)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    prune_backups(target_dir)
    return backup_file

def _count_rows(conn: sqlite3.Connection) -> Dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]
        for table in ("daily_workouts", "workout_items", "exercises")
    }

def verify_backup(backup_file: Path, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Restore test: opens the backup as a database and compares it to the live one.

    Returns {"ok": bool, "message": str, "counts": {...}}. A backup is only
    trusted if SQLite's integrity check passes and it holds the same number of
    workouts, items and exercises as the live database.
    """
    try:
        conn = sqlite3.connect(f"{Path(backup_file).resolve().as_uri()}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
            backup_counts = _count_rows(conn)
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"ok": False, "message": f"backup can't be opened: {e}", "counts": {}}

    if integrity != "ok":
        return {"ok": False, "message": f"integrity check failed: {integrity}", "counts": backup_counts}

    live = get_connection(db_path)
    try:
        live_counts = _count_rows(live)
    finally:
        live.close()

    if backup_counts != live_counts:
        return {"ok": False, "counts": backup_counts,
                "message": f"backup has {backup_counts}, live database has {live_counts}"}

    return {"ok": True, "counts": backup_counts,
            "message": (f"{backup_counts['daily_workouts']} days, "
                        f"{backup_counts['workout_items']} exercise logs, "
                        f"{backup_counts['exercises']} exercises")}

# Number of most recent backup files kept on disk; older ones are deleted.
BACKUPS_TO_KEEP = 10

def prune_backups(backup_dir: Optional[Path] = None, keep: int = BACKUPS_TO_KEEP) -> List[Path]:
    """Deletes all but the newest `keep` backup files. Returns the removed paths."""
    target_dir = backup_dir or config.BACKUP_DIR
    # Filenames embed a sortable timestamp, so name order == age order.
    backups = sorted(target_dir.glob("workout_backup_*.db"))
    removed = backups[:-keep] if keep > 0 else backups
    for path in removed:
        path.unlink(missing_ok=True)
    return removed

def get_latest_backup(backup_dir: Optional[Path] = None) -> Optional[Path]:
    """Returns the newest backup file, or None if there are none."""
    target_dir = backup_dir or config.BACKUP_DIR
    backups = sorted(target_dir.glob("workout_backup_*.db"))
    return backups[-1] if backups else None

# ---------------------------------------------------------------------------
# Scheduled alerts (rest timers / snoozed reminders)
# ---------------------------------------------------------------------------

def add_scheduled_alert(kind: str, fire_at: str, label: str, seconds: int, chat_id: int,
                        db_path: Optional[Path] = None) -> int:
    """Stores a one-off alert and returns its id."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO scheduled_alerts (kind, fire_at, label, seconds, chat_id) VALUES (?, ?, ?, ?, ?);",
            (kind, fire_at, label, int(seconds), int(chat_id))
        )
        conn.commit()
        return cursor.lastrowid

def get_scheduled_alert(alert_id: int, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM scheduled_alerts WHERE id = ?;", (alert_id,)).fetchone()
        return dict(row) if row else None

def get_scheduled_alerts(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM scheduled_alerts ORDER BY fire_at;").fetchall()
        return [dict(r) for r in rows]

def delete_scheduled_alert(alert_id: int, db_path: Optional[Path] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM scheduled_alerts WHERE id = ?;", (alert_id,))
        conn.commit()
