"""Private body tracking: weight, waist and progress photos.

Never shared with the crew. Photos are stored as files under
data/photos/<user_id>/ (not in the database), and only their owner can load them.
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from database import current_user_id, get_connection
from services.workout_service import get_current_date_str

WEIGHT_RANGE = (25.0, 350.0)   # kg
WAIST_RANGE = (40.0, 250.0)    # cm
MAX_PHOTO_BYTES = 6 * 1024 * 1024
PHOTO_TYPES = {b"\xff\xd8\xff": "jpg", b"\x89PNG\r\n\x1a\n": "png"}


def photos_dir(user_id: Optional[int] = None) -> Path:
    return config.DATA_DIR / "photos" / str(current_user_id() if user_id is None else user_id)


def log_body(weight: Optional[float] = None, waist: Optional[float] = None,
             date_str: Optional[str] = None) -> Optional[str]:
    """Saves today's weight and/or waist. Returns an error message, or None when saved."""
    if weight is None and waist is None:
        return "Enter a weight or a waist measurement."
    if weight is not None and not WEIGHT_RANGE[0] <= weight <= WEIGHT_RANGE[1]:
        return f"Weight should be between {WEIGHT_RANGE[0]:.0f} and {WEIGHT_RANGE[1]:.0f} kg."
    if waist is not None and not WAIST_RANGE[0] <= waist <= WAIST_RANGE[1]:
        return f"Waist should be between {WAIST_RANGE[0]:.0f} and {WAIST_RANGE[1]:.0f} cm."
    date_str = date_str or get_current_date_str()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO body_logs (user_id, date, weight, waist) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, date) DO UPDATE SET weight = COALESCE(excluded.weight, weight), "
            "waist = COALESCE(excluded.waist, waist);",
            (current_user_id(), date_str, weight, waist))
        conn.commit()
    return None


def body_summary(today_str: Optional[str] = None, weeks: int = 12) -> Dict[str, Any]:
    """Weekly average weight (smooths out daily water swings), waist entries, changes."""
    today = datetime.strptime(today_str or get_current_date_str(), "%Y-%m-%d").date()
    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(weeks=weeks - 1)
    with get_connection() as conn:
        rows = conn.execute("SELECT date, weight, waist FROM body_logs WHERE user_id = ? ORDER BY date;",
                            (current_user_id(),)).fetchall()
    weekly: Dict[str, List[float]] = {}
    for r in rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        if r["weight"] is not None and d >= start:
            week = (d - timedelta(days=d.weekday())).isoformat()
            weekly.setdefault(week, []).append(r["weight"])
    weights = [r["weight"] for r in rows if r["weight"] is not None]
    waists = [(r["date"], r["waist"]) for r in rows if r["waist"] is not None]
    return {
        "weekly": [{"week": w, "avg": round(sum(v) / len(v), 1)} for w, v in sorted(weekly.items())],
        "latest_weight": weights[-1] if weights else None,
        "weight_change": round(weights[-1] - weights[0], 1) if len(weights) > 1 else None,
        "waist": [{"date": d, "value": v} for d, v in waists[-8:]],
        "waist_change": round(waists[-1][1] - waists[0][1], 1) if len(waists) > 1 else None,
        "logged_today": any(r["date"] == today.isoformat() for r in rows),
    }


def save_photo(data: bytes, date_str: Optional[str] = None) -> Dict[str, Any]:
    """Stores a JPEG/PNG progress photo. Returns {"ok", "message", "id"}."""
    if not data or len(data) > MAX_PHOTO_BYTES:
        return {"ok": False, "message": "Photo must be smaller than 6 MB."}
    ext = next((e for magic, e in PHOTO_TYPES.items() if data.startswith(magic)), None)
    if not ext:
        return {"ok": False, "message": "Only JPEG or PNG photos."}
    folder = photos_dir()
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"   # random name: never derived from user input
    (folder / filename).write_bytes(data)
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO progress_photos (user_id, date, filename, created_at) VALUES (?, ?, ?, ?);",
                           (current_user_id(), date_str or get_current_date_str(), filename,
                            datetime.now(timezone.utc).isoformat()))
        conn.commit()
    return {"ok": True, "message": "Photo saved. Only you can see it.", "id": cur.lastrowid}


def list_photos() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, date FROM progress_photos WHERE user_id = ? ORDER BY date, id;",
                            (current_user_id(),)).fetchall()
    return [{"id": r["id"], "date": r["date"]} for r in rows]


def photo_file(photo_id: int) -> Optional[Path]:
    """The file for one of the current user's photos (None for anyone else's)."""
    with get_connection() as conn:
        row = conn.execute("SELECT filename FROM progress_photos WHERE id = ? AND user_id = ?;",
                           (photo_id, current_user_id())).fetchone()
    if not row:
        return None
    path = photos_dir() / row["filename"]
    return path if path.is_file() else None


def delete_photo(photo_id: int) -> bool:
    path = photo_file(photo_id)
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM progress_photos WHERE id = ? AND user_id = ?;", (photo_id, current_user_id()))
        conn.commit()
    if path:
        path.unlink(missing_ok=True)
    return cur.rowcount > 0
