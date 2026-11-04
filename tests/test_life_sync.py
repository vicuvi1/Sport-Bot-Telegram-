"""Water and workouts shared with Life System (services/life_sync.py)."""
import pytest

import config
from database import bind_user, init_db
from services import life_sync
from services import water_service as ws


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    db_file = tmp_path / "w.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "USER_ID", 42)
    init_db(db_file)
    bind_user(42)
    monkeypatch.setenv("LIFE_API_URL", "https://life.example")
    monkeypatch.setenv("LIFE_SYNC_KEY", "k")
    # Background pushes run inline in tests.
    monkeypatch.setattr(life_sync, "_background", lambda payload: life_sync._call(payload) if life_sync.enabled() else None)


def fake_post(calls, water_ml=1950):
    state = {"water": {"ml": water_ml, "target": 2700, "glass": 250}, "exercises": []}

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        if json["op"] == "water":
            state["water"]["ml"] = max(0, state["water"]["ml"] + json["ml"])
        return FakeResponse({"ok": True, "state": state})

    return post


def test_today_water_comes_from_life_system(monkeypatch):
    calls = []
    monkeypatch.setattr(life_sync.httpx, "post", fake_post(calls))
    status = ws.get_today_water()
    assert status["total_ml"] == 1950 and status["target_ml"] == 2700 and status["shared"]
    text, _ = ws.build_water_view(status)
    assert "1,950" in text and "Shared with Life System" in text
    assert calls[0][0] == "https://life.example/api/cron?job=sport"
    assert calls[0][2] == {"Authorization": "Bearer k"}


def test_logging_water_adds_it_in_life_system(monkeypatch):
    calls = []
    monkeypatch.setattr(life_sync.httpx, "post", fake_post(calls))
    status = ws.log_water(250)
    assert status["total_ml"] == 2200
    assert calls[-1][1] == {"op": "water", "ml": 250}


def test_falls_back_to_local_when_life_is_down(monkeypatch):
    def down(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(life_sync.httpx, "post", down)
    status = ws.log_water(500)
    assert status["total_ml"] == 500 and not status.get("shared")


def test_only_the_owner_syncs_and_only_when_set_up(monkeypatch):
    calls = []
    monkeypatch.setattr(life_sync.httpx, "post", fake_post(calls))
    bind_user(7)  # a crew member
    assert not life_sync.enabled()
    ws.log_water(250)
    bind_user(42)
    monkeypatch.delenv("LIFE_SYNC_KEY")
    assert not life_sync.enabled()
    assert calls == []


def test_reps_and_finished_workouts_are_pushed(monkeypatch):
    calls = []
    monkeypatch.setattr(life_sync.httpx, "post", fake_post(calls))
    life_sync.push_reps("Push-ups", 10)
    life_sync.push_workout_done()
    life_sync.push_reps("Push-ups", 0)
    assert [c[1] for c in calls] == [{"op": "reps", "name": "Push-ups", "amount": 10}, {"op": "workout", "title": "Workout (Sporty)", "source": "sporty"}]


def test_finished_workout_goes_with_its_day_and_reps(monkeypatch):
    from database import get_connection

    calls = []
    monkeypatch.setattr(life_sync.httpx, "post", fake_post(calls))
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO daily_workouts (user_id, date, status, created_at) VALUES (42, '2026-11-01', 'completed', '2026-11-01T10:00:00');")
        wid = cur.lastrowid
        cur.execute("INSERT OR IGNORE INTO exercises (user_id, name, target_reps, unit, created_at) VALUES (42, 'Push-ups', 100, 'reps', '2026-11-01T10:00:00');")
        ex = cur.execute("SELECT id FROM exercises WHERE user_id = 42 AND name = 'Push-ups';").fetchone()[0]
        cur.execute(
            "INSERT INTO workout_items (daily_workout_id, exercise_id, exercise_name, target_reps, completed_reps, status) VALUES (?, ?, 'Push-ups', 100, 100, 'completed');",
            (wid, ex),
        )
        life_sync.push_workout_done(cur, wid)
    sent = calls[-1][1]
    assert sent["op"] == "workout" and sent["day"] == "2026-11-01" and sent["source"] == "sporty"
    assert sent["items"] == [{"exerciseId": "push-ups", "name": "Push-ups", "amount": 100, "unit": "reps"}]
    assert "100 Push-ups" in sent["title"]


def test_history_sends_each_completed_day(monkeypatch):
    from datetime import date, timedelta

    from database import get_connection

    calls = []
    monkeypatch.setattr(life_sync.httpx, "post", fake_post(calls))
    with get_connection() as conn:
        cur = conn.cursor()
        for i, st in [(1, "completed"), (2, "completed"), (3, "pending"), (30, "completed")]:
            cur.execute("INSERT INTO daily_workouts (user_id, date, status, created_at) VALUES (42, ?, ?, '2026-11-01T10:00:00');", ((date.today() - timedelta(days=i)).isoformat(), st))
        conn.commit()
    assert life_sync.push_history(14) == 2
    assert [c[1]["day"] for c in calls] == [(date.today() - timedelta(days=2)).isoformat(), (date.today() - timedelta(days=1)).isoformat()]
