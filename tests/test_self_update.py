"""Self-update: checks GitHub, fast-forwards, obeys the Life System button."""

import subprocess
from types import SimpleNamespace

from services import self_update


def _cp(out="", code=0, err=""):
    return subprocess.CompletedProcess([], code, stdout=out, stderr=err)


def test_check_reports_new_changes(monkeypatch):
    def git(*args, timeout=60):
        if args[0] == "fetch":
            return _cp()
        if args[0] == "rev-list":
            return _cp("2\n")
        if args[0] == "rev-parse":
            return _cp("abc1234\n")
        if args[0] == "log":
            return _cp("Fix water\nNew card\n")
        return _cp()

    monkeypatch.setattr(self_update, "_git", git)
    assert self_update.check() == {"behind": 2, "latest": "abc1234", "changes": ["Fix water", "New card"]}


def test_check_reports_fetch_error(monkeypatch):
    monkeypatch.setattr(self_update, "_git", lambda *a, **k: _cp(code=1, err="no network"))
    assert self_update.check() == {"error": "no network"}


def test_apply_does_nothing_when_up_to_date(monkeypatch):
    calls = []
    monkeypatch.setattr(self_update, "version", lambda: "aaa")
    monkeypatch.setattr(self_update, "check", lambda: {"behind": 0, "latest": "aaa", "changes": []})
    monkeypatch.setattr(self_update, "_git", lambda *a, **k: calls.append(a) or _cp())
    assert self_update.apply() == {"ok": True, "updated": False, "version": "aaa"}
    assert not any(a[0] == "merge" for a in calls)


def test_apply_fast_forwards_only(monkeypatch):
    calls = []
    versions = iter(["aaa", "bbb"])
    monkeypatch.setattr(self_update, "version", lambda: next(versions))
    monkeypatch.setattr(self_update, "check", lambda: {"behind": 1, "latest": "bbb", "changes": ["New"]})

    def git(*args, timeout=60):
        calls.append(args)
        return _cp("same\n")

    monkeypatch.setattr(self_update, "_git", git)
    r = self_update.apply()
    assert r == {"ok": True, "updated": True, "from": "aaa", "version": "bbb", "changes": ["New"]}
    assert ("merge", "--ff-only", f"origin/{self_update.BRANCH}") in calls


def test_apply_reports_a_failed_merge(monkeypatch):
    monkeypatch.setattr(self_update, "version", lambda: "aaa")
    monkeypatch.setattr(self_update, "check", lambda: {"behind": 1, "latest": "bbb", "changes": []})
    monkeypatch.setattr(self_update, "_git", lambda *a, **k: _cp(code=1, err="local changes") if a[0] == "merge" else _cp("x"))
    r = self_update.apply()
    assert r["ok"] is False and "local changes" in r["error"]


def test_update_and_restart_tells_life_system_and_restarts(monkeypatch):
    from services import life_sync

    sent, synced, restarted = [], [], []
    monkeypatch.setattr(self_update, "apply", lambda: {"ok": True, "updated": True, "from": "aaa", "version": "bbb", "changes": ["New"]})
    monkeypatch.setattr(self_update, "_send_owner", lambda text, button=False: sent.append(text))
    monkeypatch.setattr(self_update, "restart_soon", lambda delay_s=2.0: restarted.append(True))
    monkeypatch.setattr(life_sync, "call_raw", lambda p: synced.append(p))
    self_update.update_and_restart("test")
    assert synced[0]["op"] == "updated" and synced[0]["version"] == "bbb"
    assert "aaa → bbb" in sent[0] and restarted == [True]


def test_no_restart_when_nothing_new(monkeypatch):
    from services import life_sync

    restarted = []
    monkeypatch.setattr(self_update, "apply", lambda: {"ok": True, "updated": False, "version": "aaa"})
    monkeypatch.setattr(self_update, "restart_soon", lambda delay_s=2.0: restarted.append(True))
    monkeypatch.setattr(life_sync, "call_raw", lambda p: None)
    self_update.update_and_restart()
    assert restarted == []
