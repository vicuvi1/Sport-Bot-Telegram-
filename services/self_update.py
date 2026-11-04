"""Sporty updates itself from GitHub (no SSH needed after the first time).

- `check()`: fetches the main branch and says how many new changes are waiting.
- `apply()`: pulls them (fast-forward only, so local data is never touched),
  installs requirements if they changed, and then the bot restarts itself in
  place (works with systemd, Docker or a plain `python main.py`).
- `watch_loop()`: runs in the background. Every minute it tells Life System
  which version runs here and asks whether the owner pressed "Update Sporty"
  in the app; every 6 hours it checks GitHub and, if something new is there,
  sends the owner one "Update ready" message with an Update button.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

import config

logger = logging.getLogger(__name__)

BRANCH = os.getenv("SPORTY_UPDATE_BRANCH", "main").strip() or "main"
CHECK_EVERY_S = 6 * 3600
POLL_EVERY_S = 60
_lock = threading.Lock()
_last_notified: Optional[str] = None


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=config.BASE_DIR, capture_output=True, text=True, timeout=timeout)


def version() -> str:
    r = _git("rev-parse", "--short", "HEAD", timeout=10)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"


def check() -> Dict[str, Any]:
    """{'behind': n, 'latest': sha, 'changes': [subjects]} or {'error': ...}."""
    try:
        f = _git("fetch", "--quiet", "origin", BRANCH)
        if f.returncode != 0:
            return {"error": (f.stderr or "git fetch failed").strip()[:200]}
        n = _git("rev-list", "--count", f"HEAD..origin/{BRANCH}", timeout=10)
        behind = int(n.stdout.strip() or 0) if n.returncode == 0 else 0
        latest = _git("rev-parse", "--short", f"origin/{BRANCH}", timeout=10).stdout.strip()
        log = _git("log", "--format=%s", f"HEAD..origin/{BRANCH}", "-n", "8", timeout=10)
        changes: List[str] = [x for x in log.stdout.splitlines() if x.strip()] if log.returncode == 0 else []
        return {"behind": behind, "latest": latest, "changes": changes}
    except Exception as e:
        return {"error": str(e)[:200]}


def apply() -> Dict[str, Any]:
    """Pulls the new version (fast-forward only) and installs requirements if needed."""
    with _lock:
        before = version()
        info = check()
        if info.get("error"):
            return {"ok": False, "error": info["error"], "version": before}
        if not info.get("behind"):
            return {"ok": True, "updated": False, "version": before}
        req_before = _git("rev-parse", "HEAD:requirements.txt", timeout=10).stdout.strip()
        m = _git("merge", "--ff-only", f"origin/{BRANCH}")
        if m.returncode != 0:
            return {"ok": False, "error": ("Could not fast-forward: " + (m.stderr or m.stdout)).strip()[:300], "version": before}
        req_after = _git("rev-parse", "HEAD:requirements.txt", timeout=10).stdout.strip()
        if req_before != req_after:
            p = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=config.BASE_DIR, capture_output=True, text=True, timeout=600)
            if p.returncode != 0:
                logger.warning("pip install after update failed: %s", p.stderr[-400:])
        return {"ok": True, "updated": True, "from": before, "version": version(), "changes": info.get("changes", [])}


def restart_soon(delay_s: float = 2.0) -> None:
    """Restarts this process with the new code (same command, same environment)."""
    def _go() -> None:
        time.sleep(delay_s)
        logger.info("Restarting with the new version…")
        os.execv(sys.executable, [sys.executable, *sys.argv])

    threading.Thread(target=_go, daemon=True).start()


def _send_owner(text: str, button: bool = False) -> None:
    if not (config.BOT_TOKEN and config.USER_ID):
        return
    body: Dict[str, Any] = {"chat_id": config.USER_ID, "text": text, "parse_mode": "HTML"}
    if button:
        body["reply_markup"] = {"inline_keyboard": [[{"text": "⬆️ Update now", "callback_data": "sporty_update"}]]}
    try:
        httpx.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage", json=body, timeout=10)
    except Exception as e:
        logger.warning("Could not send the update message: %s", e)


def update_and_restart(source: str = "") -> Dict[str, Any]:
    """Applies the update, tells Life System and the owner, then restarts."""
    from services import life_sync

    r = apply()
    life_sync.call_raw({"op": "updated", **{k: v for k, v in r.items() if k in ("ok", "updated", "version", "error")}, "changes": r.get("changes", [])[:8]})
    if r.get("ok") and r.get("updated"):
        lines = "\n".join(f"• {c}" for c in r.get("changes", [])[:8])
        _send_owner(f"⬆️ <b>Sporty updated</b> {r.get('from')} → {r.get('version')}{(' (' + source + ')') if source else ''}\n{lines}\nRestarting now…")
        restart_soon()
    elif not r.get("ok"):
        _send_owner(f"⚠️ Sporty update failed: {r.get('error')}")
    return r


def watch_loop() -> None:
    """Background: report the version to Life System, obey its Update button, check GitHub every 6 h."""
    global _last_notified
    from services import life_sync

    last_check = 0.0
    behind = 0
    latest = ""
    while True:
        try:
            if time.time() - last_check > CHECK_EVERY_S:
                last_check = time.time()
                info = check()
                behind = int(info.get("behind") or 0)
                latest = str(info.get("latest") or "")
                if behind and latest and latest != _last_notified:
                    _last_notified = latest
                    changes = "\n".join(f"• {c}" for c in info.get("changes", [])[:6])
                    _send_owner(f"🆕 <b>Sporty update ready</b> ({behind} change{'s' if behind != 1 else ''})\n{changes}", button=True)
            res = life_sync.call_raw({"op": "poll", "version": version(), "behind": behind, "latest": latest})
            if res and res.get("update"):
                update_and_restart("from the Life System app")
        except Exception as e:
            logger.warning("Update watcher: %s", e)
        time.sleep(POLL_EVERY_S)


def start_watcher() -> None:
    threading.Thread(target=watch_loop, daemon=True, name="sporty-updater").start()
