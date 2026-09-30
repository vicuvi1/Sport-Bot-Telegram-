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

        # Seed default settings if not present
        default_settings = {
            "workout_time": config.WORKOUT_TIME or "07:00",
            "timezone": config.TIMEZONE or "Europe/Chisinau",
            "notifications_enabled": "1",
            "auto_progression_enabled": "0",
            "progression_percentage": "5",
            "progression_consecutive_workouts": "3",
            "workout_days": "0,1,2,3,4,5,6"  # Mon-Sun active by default
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
    
    with get_connection(source_path) as src_conn:
        with sqlite3.connect(backup_file) as dst_conn:
            src_conn.backup(dst_conn)
            
    return backup_file
