import logging
import sqlite3
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Dict, Iterator, List
import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------
# The bot serves a small crew (the owner from TELEGRAM_USER_ID plus invited
# members). Every per-user query filters on current_user_id(). It is bound
# once per Telegram update (see main.bind_update_user) and explicitly inside
# scheduled jobs (as_user). When nothing is bound it is the owner, which keeps
# single-user behaviour (and older tests) unchanged.

_current_user: ContextVar[Optional[int]] = ContextVar("current_user", default=None)

# Bound for updates from people who aren't in the crew: matches no data.
NOBODY = -1


def current_user_id() -> int:
    uid = _current_user.get()
    return config.USER_ID if uid is None else uid


def bind_user(user_id: Optional[int]) -> None:
    """Binds the current user for the rest of this task (one Telegram update)."""
    _current_user.set(user_id)


@contextmanager
def as_user(user_id: int) -> Iterator[None]:
    """Temporarily acts as `user_id` (scheduled jobs, notifying the other brother)."""
    token = _current_user.set(user_id)
    try:
        yield
    finally:
        _current_user.reset(token)


# Settings that belong to one person. Everything else in `settings` is global
# (bot-wide state such as the accountability partner of the owner).
USER_SETTING_KEYS = frozenset({
    "workout_time", "timezone", "notifications_enabled", "auto_progression_enabled",
    "progression_percentage", "progression_consecutive_workouts", "workout_days",
    "easy_feedback_streak", "test_pullups",
    # crew features
    "wake_time", "wake_enabled", "roast_level", "crew_feed",
    # Mini App
    "ui_anime", "onboarded", "seen_rank", "gear_hair", "gear_band", "gear_aura",
})


def default_user_settings() -> Dict[str, str]:
    return {
        "workout_time": config.WORKOUT_TIME or "07:00",
        "timezone": config.TIMEZONE or "Europe/Chisinau",
        "notifications_enabled": "1",
        "auto_progression_enabled": "0",
        "progression_percentage": "5",
        "progression_consecutive_workouts": "3",
        "workout_days": "0,1,2,3,4,5,6",  # Mon-Sun active by default
        "easy_feedback_streak": "0",      # consecutive "too easy" answers
        "wake_enabled": "0",
        "wake_time": "07:00",
        "roast_level": "savage",
        "crew_feed": "1",
        "ui_anime": "1",
    }


DEFAULT_EXERCISES = [
    ("Push-ups", 50, "reps"),
    ("Squats", 50, "reps"),
    ("Sit-ups", 30, "reps"),
    ("Plank", 60, "sec"),
]


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Returns a SQLite connection with Row factory and foreign keys enabled."""
    target_path = db_path or config.DB_PATH
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _columns(cursor, table: str) -> set:
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table});").fetchall()}


def _table_exists(cursor, table: str) -> bool:
    return cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;", (table,)).fetchone() is not None


def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, ddl: str) -> None:
    """Adds `column` to `table` if it doesn't exist yet."""
    if column not in _columns(cursor, table):
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")


SCHEMA = {
    "exercises": """
        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            target_reps INTEGER NOT NULL,
            unit TEXT NOT NULL DEFAULT 'reps',
            is_active INTEGER NOT NULL DEFAULT 1,
            days_of_week TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
            created_at TEXT NOT NULL,
            UNIQUE (user_id, name)
        );""",
    "daily_workouts": """
        CREATE TABLE IF NOT EXISTS daily_workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            feedback TEXT,
            quick INTEGER NOT NULL DEFAULT 0,
            comeback_stage INTEGER NOT NULL DEFAULT 0,
            UNIQUE (user_id, date)
        );""",
    "workout_items": """
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
            full_target_reps INTEGER,
            FOREIGN KEY (daily_workout_id) REFERENCES daily_workouts(id) ON DELETE CASCADE,
            FOREIGN KEY (exercise_id) REFERENCES exercises(id) ON DELETE SET NULL
        );""",
    "fitness_results": """
        CREATE TABLE IF NOT EXISTS fitness_results (
            user_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            test_key TEXT NOT NULL,
            value INTEGER,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (user_id, month, test_key)
        );""",
    "pauses": """
        CREATE TABLE IF NOT EXISTS pauses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );""",
}

# Tables whose uniqueness had to change from "per bot" to "per user".
# SQLite can't alter constraints, so these are rebuilt (keeping their ids, so
# workout_items foreign keys stay valid). Value: columns copied over.
_REBUILT_FOR_USERS = {
    "exercises": ["id", "name", "target_reps", "unit", "is_active", "days_of_week", "created_at"],
    "daily_workouts": ["id", "date", "status", "created_at", "completed_at", "feedback", "quick", "comeback_stage"],
    "fitness_results": ["month", "test_key", "value", "recorded_at"],
}


def _needs_user_migration(db_path: Optional[Path]) -> bool:
    conn = get_connection(db_path)
    try:
        return _table_exists(conn, "daily_workouts") and "user_id" not in _columns(conn, "daily_workouts")
    finally:
        conn.close()


def _migrate_to_multi_user(db_path: Optional[Path], owner_id: int) -> None:
    """One-time upgrade of a single-user database: all existing data becomes the owner's.

    A copy of the database is saved first (backups/pre_multiuser_*.db, never
    pruned). Runs in one transaction with foreign keys off, following
    SQLite's "create new table, copy, drop, rename" procedure.
    """
    source = Path(db_path or config.DB_PATH)
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    safety_copy = config.BACKUP_DIR / f"pre_multiuser_{datetime.now():%Y%m%d_%H%M%S}.db"
    src, dst = sqlite3.connect(source), sqlite3.connect(safety_copy)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    logger.info("Migrating database to multi-user; safety copy at %s", safety_copy)

    conn = sqlite3.connect(source, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute("BEGIN;")
        # Old databases may predate these columns; add them so the copy is uniform.
        _ensure_column(conn, "daily_workouts", "feedback", "TEXT")
        _ensure_column(conn, "daily_workouts", "quick", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "daily_workouts", "comeback_stage", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "workout_items", "full_target_reps", "INTEGER")

        for table, cols in _REBUILT_FOR_USERS.items():
            if not _table_exists(conn, table):
                continue
            conn.execute(SCHEMA[table].replace(f"IF NOT EXISTS {table}", f"{table}_new"))
            col_list = ", ".join(cols)
            conn.execute(f"INSERT INTO {table}_new (user_id, {col_list}) SELECT ?, {col_list} FROM {table};",
                         (owner_id,))
            conn.execute(f"DROP TABLE {table};")
            conn.execute(f"ALTER TABLE {table}_new RENAME TO {table};")

        if _table_exists(conn, "pauses") and "user_id" not in _columns(conn, "pauses"):
            conn.execute("ALTER TABLE pauses ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;")
            conn.execute("UPDATE pauses SET user_id = ?;", (owner_id,))

        problems = conn.execute("PRAGMA foreign_key_check;").fetchall()
        if problems:
            raise sqlite3.IntegrityError(f"foreign key problems after migration: {problems[:5]}")
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.close()


def init_db(db_path: Optional[Path] = None) -> None:
    """Creates/upgrades tables and makes sure the owner exists with defaults."""
    if _needs_user_migration(db_path):
        _migrate_to_multi_user(db_path, config.USER_ID)

    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Global key/value settings (bot-wide state).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # The crew: the owner plus invited members.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'member',
                joined_at TEXT NOT NULL
            );
        """)
        # Per-user settings (see USER_SETTING_KEYS).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            );
        """)
        for ddl in SCHEMA.values():
            cursor.execute(ddl)

        # Crew live feed: things worth telling the other members about.
        # Recorded by services, sent by crew_service.deliver_events().
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crew_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0
            );
        """)

        # Wake-up challenge: one check per user per day (see wake_service).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wake_logs (
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                target TEXT NOT NULL,
                challenge INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                answered_at TEXT,
                minutes_late INTEGER,
                PRIMARY KEY (user_id, date)
            );
        """)

        # Crew competition (see compete_service): 1-on-1 challenges and the
        # weekly loser's forfeit.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS challenges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                challenger INTEGER NOT NULL,
                target INTEGER NOT NULL,
                kind TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'offered',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS forfeits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start TEXT NOT NULL,
                winner INTEGER NOT NULL,
                loser INTEGER NOT NULL,
                task TEXT,
                status TEXT NOT NULL DEFAULT 'choosing',
                created_at TEXT NOT NULL,
                done_at TEXT,
                UNIQUE (week_start, loser)
            );
        """)

        # Crew activity log (Duel "fight log", ghost pace) and its reactions.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                local_time TEXT NOT NULL
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_reactions (
                activity_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                PRIMARY KEY (activity_id, user_id),
                FOREIGN KEY (activity_id) REFERENCES activity(id) ON DELETE CASCADE
            );
        """)
        # Body tracking (private: never shown to the crew).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS body_logs (
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                weight REAL,
                waist REAL,
                PRIMARY KEY (user_id, date)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS progress_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                filename TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

        # Scheduled one-off alerts (rest timers, snoozed reminders).
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

        if config.USER_ID:
            _ensure_user_row(cursor, config.USER_ID, "", "owner")
            # Settings a single-user install kept globally become the owner's.
            cursor.execute(
                f"""
                INSERT OR IGNORE INTO user_settings (user_id, key, value)
                SELECT ?, key, value FROM settings
                WHERE key IN ({",".join("?" * len(USER_SETTING_KEYS))});
                """,
                (config.USER_ID, *sorted(USER_SETTING_KEYS))
            )
            _seed_user(cursor, config.USER_ID)

        conn.commit()


def _ensure_user_row(cursor, user_id: int, name: str, role: str) -> None:
    cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, name, role, joined_at) VALUES (?, ?, ?, ?);",
        (user_id, name, role, datetime.now().isoformat())
    )


def _seed_user(cursor, user_id: int) -> None:
    """Default settings and starter exercises for a user who has none yet."""
    for key, val in default_user_settings().items():
        cursor.execute("INSERT OR IGNORE INTO user_settings (user_id, key, value) VALUES (?, ?, ?);",
                       (user_id, key, str(val)))
    has_exercises = cursor.execute("SELECT 1 FROM exercises WHERE user_id = ? LIMIT 1;", (user_id,)).fetchone()
    if not has_exercises:
        now_iso = datetime.now().isoformat()
        cursor.executemany(
            "INSERT INTO exercises (user_id, name, target_reps, unit, is_active, days_of_week, created_at) "
            "VALUES (?, ?, ?, ?, 1, '0,1,2,3,4,5,6', ?);",
            [(user_id, name, target, unit, now_iso) for name, target, unit in DEFAULT_EXERCISES]
        )


# ---------------------------------------------------------------------------
# Crew members
# ---------------------------------------------------------------------------

def add_user(user_id: int, name: str, role: str = "member", db_path: Optional[Path] = None) -> None:
    """Adds a crew member (with default settings and starter exercises)."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        _ensure_user_row(cursor, user_id, name, role)
        cursor.execute("UPDATE users SET name = ? WHERE user_id = ? AND ? != '';", (name, user_id, name))
        _seed_user(cursor, user_id)
        conn.commit()


def get_user(user_id: int, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,)).fetchone()
        return dict(row) if row else None


def get_users(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """All crew members, owner first."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY role = 'owner' DESC, joined_at ASC;").fetchall()
        return [dict(r) for r in rows]


def is_member(user_id: Optional[int], db_path: Optional[Path] = None) -> bool:
    return bool(user_id) and get_user(user_id, db_path) is not None


def set_user_name(user_id: int, name: str, db_path: Optional[Path] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute("UPDATE users SET name = ? WHERE user_id = ?;", (name, user_id))
        conn.commit()


def remove_user(user_id: int, db_path: Optional[Path] = None) -> None:
    """Removes a member and all their data (never the owner)."""
    if user_id == config.USER_ID:
        raise ValueError("the owner can't be removed")
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM daily_workouts WHERE user_id = ?;", (user_id,))  # items cascade
        conn.execute("DELETE FROM challenges WHERE challenger = ? OR target = ?;", (user_id, user_id))
        conn.execute("DELETE FROM forfeits WHERE winner = ? OR loser = ?;", (user_id, user_id))
        conn.execute("DELETE FROM activity_reactions WHERE user_id = ? OR activity_id IN "
                     "(SELECT id FROM activity WHERE user_id = ?);", (user_id, user_id))
        for table in ("exercises", "pauses", "fitness_results", "wake_logs", "crew_events", "activity",
                      "body_logs", "progress_photos", "user_settings", "users"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?;", (user_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key: str, default: Any = None, db_path: Optional[Path] = None) -> Any:
    """Fetches a setting: per-user keys for the current user, others globally."""
    with get_connection(db_path) as conn:
        if key in USER_SETTING_KEYS:
            row = conn.execute("SELECT value FROM user_settings WHERE user_id = ? AND key = ?;",
                               (current_user_id(), key)).fetchone()
        else:
            row = conn.execute("SELECT value FROM settings WHERE key = ?;", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: Any, db_path: Optional[Path] = None) -> None:
    """Sets a setting: per-user keys for the current user, others globally."""
    with get_connection(db_path) as conn:
        if key in USER_SETTING_KEYS:
            conn.execute(
                "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value;",
                (current_user_id(), key, str(value))
            )
        else:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                (key, str(value))
            )
        conn.commit()


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
