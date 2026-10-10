"""Log reps by just typing them: "35 push-ups", "push 35", "plank 2 min", "-10 push".

Several at once work too ("30 push 40 squats"). A bare number ("35") asks which
exercise it was for. Runs after every "type a value" prompt, so it never steals
an answer meant for settings or the fitness test.
"""

import re
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from services.workout_service import get_current_date_str, get_or_create_daily_workout, update_workout_item
from views import HTML, build_today_workout_view, esc, unit_label

MAX_AMOUNT = 1000
UNITS = r"(?:s|sec|secs|seconds|m|min|mins|minutes)"
TOKEN = re.compile(rf"([+-]?\d{{1,4}})\s*({UNITS})?(?![a-zA-Z])|([a-zA-Z][a-zA-Z\-]*)", re.IGNORECASE)
BARE_NUMBER = re.compile(r"^\s*([+-]?\d{1,4})\s*$")
FILLER = {"x", "and", "of", "reps", "rep", "did", "done", "i", "more"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _seconds(amount: int, unit: Optional[str]) -> int:
    return amount * 60 if unit and unit.lower().startswith("m") else amount


def find_item(items: List[dict], word: str) -> Optional[dict]:
    """'push', 'pushups', 'push-ups' -> the Push-ups item; None if unclear."""
    w = _norm(word)
    if len(w) < 3:
        return None
    matches = [it for it in items
               if _norm(it["exercise_name"]).startswith(w) or w.startswith(_norm(it["exercise_name"])[:4])
               or w in _norm(it["exercise_name"])]
    return matches[0] if len(matches) == 1 else None


def tokens(text: str) -> List[Tuple[str, object]]:
    """[("num", (amount, unit)), ("word", "push ups"), ...] with consecutive words joined."""
    out: List[Tuple[str, object]] = []
    for m in TOKEN.finditer(text):
        if m.group(1):
            out.append(("num", (int(m.group(1)), m.group(2))))
        elif m.group(3).lower() not in FILLER:
            if out and out[-1][0] == "word":
                out[-1] = ("word", f"{out[-1][1]} {m.group(3)}")
            else:
                out.append(("word", m.group(3)))
    return out


def parse(text: str, items: List[dict]) -> Tuple[List[Tuple[dict, int]], List[str]]:
    """Pairs numbers with exercise names in either order: "35 push", "push 35", "30 push 40 squats".
    Returns ([(item, amount)], [bits it couldn't understand])."""
    toks = tokens(text)
    found, unknown = [], []
    number_first = bool(toks) and toks[0][0] == "num"
    i = 0
    while i < len(toks):
        first = toks[i]
        second = toks[i + 1] if i + 1 < len(toks) else None
        num, word = (first, second) if number_first else (second, first)
        if not second or num[0] != "num" or word[0] != "word":
            unknown.append(str(first[1][0] if first[0] == "num" else first[1]))
            i += 1
            continue
        i += 2
        amount, unit = num[1]
        item = find_item(items, word[1])
        value = (_seconds(amount, unit) if item["unit"] == "sec" else amount) if item else 0
        if not item or value == 0 or abs(value) > MAX_AMOUNT:
            unknown.append(f"{amount} {word[1]}")
            continue
        found.append((item, value))
    return found, unknown


def _log(found: List[Tuple[dict, int]]) -> List[str]:
    lines = []
    for item, value in found:
        updated, _ = update_workout_item(item["id"], delta_reps=value)
        if not updated:
            continue
        unit = "" if updated["unit"] == "reps" else unit_label(updated["unit"])
        mark = "✅" if updated["status"] == "completed" else "➕" if value > 0 else "➖"
        lines.append(f"{mark} {esc(updated['exercise_name'])} {value:+d}{unit} → "
                     f"<b>{updated['completed_reps']}/{updated['target_reps']}{unit}</b>")
    return lines


async def _rest_day_reply(update: Update) -> None:
    await update.message.reply_text("Today is a rest or paused day, so there's nothing to log. 🌿")


async def quick_log_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Logs typed amounts. Returns False when the text isn't a workout log."""
    text = (update.message.text or "").strip()
    if not re.search(r"\d", text):
        return False
    today = get_current_date_str()
    workout = get_or_create_daily_workout(today)
    items = [it for it in workout["items"] if it["status"] != "skipped"]
    bare = BARE_NUMBER.match(text)
    if workout["status"] in ("paused", "rest"):
        if bare or any(kind == "num" for kind, _ in tokens(text)) and any(kind == "word" for kind, _ in tokens(text)):
            await _rest_day_reply(update)
            return True
        return False

    if bare:
        value = int(bare.group(1))
        if value == 0 or abs(value) > MAX_AMOUNT or not items:
            return False
        buttons = [[InlineKeyboardButton(f"{it['exercise_name']} {value:+d}{'' if it['unit'] == 'reps' else 's'}",
                                         callback_data=f"qlog:{it['id']}:{value}")] for it in items]
        await update.message.reply_text(f"Add <b>{value:+d}</b> to which exercise?", parse_mode=HTML,
                                        reply_markup=InlineKeyboardMarkup(buttons))
        return True

    found, unknown = parse(text, items)
    if not found:
        return False
    lines = _log(found)
    if unknown:
        names = ", ".join(it["exercise_name"] for it in items)
        lines.append(f"🤔 Didn't get: {esc(', '.join(unknown))}. Today's exercises: {esc(names)}.")
    view, markup = build_today_workout_view(get_or_create_daily_workout(today), today, intro="\n".join(lines))
    await update.message.reply_text(view, parse_mode=HTML, reply_markup=markup)
    return True


async def quick_log_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """qlog:<item_id>:<amount> from the "Add 35 to which exercise?" buttons."""
    query = update.callback_query
    _, item_id, value = query.data.split(":")
    today = get_current_date_str()
    item = next((it for it in get_or_create_daily_workout(today)["items"] if it["id"] == int(item_id)), None)
    if not item:
        await query.answer("That's not part of today's workout anymore.", show_alert=True)
        return
    await query.answer()
    lines = _log([(item, int(value))])
    view, markup = build_today_workout_view(get_or_create_daily_workout(today), today, intro="\n".join(lines))
    await query.edit_message_text(view, parse_mode=HTML, reply_markup=markup)
