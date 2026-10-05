"""Shared pytest fixtures for the workout bot test-suite.

The handler render helpers (e.g. ``build_today_workout_view`` and
``build_settings_menu``) read application settings through the module-level
``config.DB_PATH`` rather than accepting a ``db_path`` argument. Under pytest
that global DB does not exist, so those helpers raised ``sqlite3.OperationalError:
no such table: settings``.

This autouse fixture points the application's global database at a fresh,
initialized temporary SQLite file for every test, keeping the suite hermetic
(it never touches the real production database in ``/opt/workout-bot/data``).
"""

import pytest

import config
from database import init_db


# Fixed fake Telegram user id used as the authorized user in every test.
TEST_USER_ID = 123456789


@pytest.fixture(autouse=True)
def isolated_global_db(tmp_path, monkeypatch):
    """Redirect the app's global DB / data / backup paths to a temp dir."""
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    db_file = data_dir / "workout.db"

    data_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(config, "DB_PATH", db_file)

    # Tests must not depend on the developer's local .env: without a configured
    # TELEGRAM_USER_ID, is_authorized() rejects every update and tests fail.
    monkeypatch.setattr(config, "USER_ID", TEST_USER_ID)

    init_db(db_file)
    yield db_file
