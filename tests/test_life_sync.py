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
    assert [c[1] for c in calls] == [{"op": "reps", "name": "Push-ups", "amount": 10}, {"op": "workout", "title": "Workout (Sporty)"}]
