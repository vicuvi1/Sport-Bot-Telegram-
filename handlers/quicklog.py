"""Log reps by just typing them: "35 push-ups", "push 35", "plank 2 min", "-10 push".

Several at once work too ("30 push 40 squats"). A bare number ("35") asks which
exercise it was for. Runs after every "type a value" prompt, so it never steals
an answer meant for settings or the fitness test.
"""

import re
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_setting
from services.quote_service import get_in_workout_quote
from services.water_service import build_water_view, get_today_water, log_water
from services.workout_service import (
    add_today_workout_item,
    get_current_date_str,
    get_exercises,
    get_or_create_daily_workout,
    resume_from_pause,
    start_pause,
    swap_today_workout_item,
    update_exercise,
    update_today_item_target,
    update_workout_item,
)
from views import HTML, build_today_workout_view, esc, nice_date, unit_label

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


def _calculate_hype(
    found: List[Tuple[dict, int]],
    total_before: int,
    target_before: int,
    was_pending: bool,
    today: str,
) -> Optional[str]:
    workout_after = get_or_create_daily_workout(today)
    total_after = sum(it["completed_reps"] for it in workout_after["items"])
    target_after = sum(it["target_reps"] for it in workout_after["items"])
    is_completed = workout_after["status"] == "completed"

    persona = get_setting("coach_persona", "beast")
    found_ids = {f[0]["id"] for f in found}
    overdelivered = any(it["completed_reps"] > it["target_reps"] for it in workout_after["items"] if it["id"] in found_ids)

    if is_completed and was_pending:
        return get_in_workout_quote("victory", persona)
    if overdelivered:
        return get_in_workout_quote("overdelivered", persona)
    if target_before > 0 and (total_before / target_before < 0.5) and (target_after > 0 and total_after / target_after >= 0.5):
        return get_in_workout_quote("halfway", persona)
    if total_before == 0 and total_after > 0:
        return get_in_workout_quote("first_blood", persona)
    return None


async def _rest_day_reply(update: Update) -> None:
    await update.message.reply_text("Today is a rest or paused day, so there's nothing to log. 🌿")


async def handle_flexible_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Parses on-the-fly commands: water logging, pauses, targets, adding/swapping movements."""
    text = (update.message.text or "").strip()
    clean = text.lower()
    today = get_current_date_str()

    # 1. Hydration view & logging
    if clean in ("water", "drink", "hydration", "💧"):
        status = get_today_water()
        card_text, markup = build_water_view(status)
        await update.message.reply_text(card_text, reply_markup=markup, parse_mode=HTML)
        return True

    # e.g. "water 250", "drink 500", "water +500", "500ml", "water 2 glasses"
    m_water = (
        re.match(r"^(?:water|drink)\s*([+-]?\d{2,4})\s*(?:ml)?$", text, re.IGNORECASE)
        or re.match(r"^([+-]?\d{2,4})\s*ml$", text, re.IGNORECASE)
        or re.match(r"^(?:water|drink)\s*(\d{1,2})\s*glass(?:es)?$", text, re.IGNORECASE)
    )
    if m_water:
        multiplier = 250 if "glass" in clean else 1
        amount = int(m_water.group(1)) * multiplier
        status = log_water(amount)
        mark = "💧 Added" if amount > 0 else "➖ Subtracted"
        intro = f"{mark} <b>{abs(amount)} ml</b> of water!"
        card_text, markup = build_water_view(status, intro=intro)
        await update.message.reply_text(card_text, reply_markup=markup, parse_mode=HTML)
        return True

    # 2. Quick Vacation / Sick Pause
    if clean in ("sick today", "pause today", "sick", "feeling sick"):
        pause = start_pause(1)
        await update.message.reply_text(
            f"🏖 Rest up and recover! Workouts are paused for 1 day (until <b>{nice_date(pause['end_date'])}</b>).\n"
            "Your streak is safe and frozen. 🌿",
            parse_mode=HTML
        )
        return True

    m_pause = re.match(r"^pause\s+(\d{1,2})(?:\s*days?)?$", text, re.IGNORECASE)
    if m_pause:
        days = int(m_pause.group(1))
        pause = start_pause(days)
        await update.message.reply_text(
            f"🏖 Workouts paused for {days} days (until <b>{nice_date(pause['end_date'])}</b>).\n"
            "No reminders will be sent and your streak is frozen. 🌿",
            parse_mode=HTML
        )
        return True

    if clean in ("resume", "unpause", "resume workout", "resume workouts"):
        resumed = resume_from_pause()
        workout = get_or_create_daily_workout(today)
        intro = "▶️ <b>Welcome back!</b> Pause ended and training resumed." if resumed else "You were not paused."
        view, markup = build_today_workout_view(workout, today, intro=intro)
        await update.message.reply_text(view, reply_markup=markup, parse_mode=HTML)
        return True

    # 3. Permanent Target Change: "target pushups 60", "set target squats 70"
    m_target = re.match(r"^(?:set\s+)?target\s+([a-zA-Z\- ]+?)\s+(\d{1,4})$", text, re.IGNORECASE)
    if m_target:
        ex_name = m_target.group(1).strip()
        new_target = int(m_target.group(2))
        exercises = get_exercises()
        matched_ex = None
        for e in exercises:
            if _norm(ex_name) in _norm(e["name"]) or _norm(e["name"]) in _norm(ex_name):
                matched_ex = e
                break
        if matched_ex:
            update_exercise(matched_ex["id"], target_reps=new_target)
            workout = get_or_create_daily_workout(today)
            today_item = next((it for it in workout["items"] if it["exercise_id"] == matched_ex["id"]), None)
            if today_item:
                update_today_item_target(today_item["id"], new_target)
            intro = f"🎯 <b>Target updated!</b> Baseline for <b>{esc(matched_ex['name'])}</b> is now <b>{new_target} {esc(unit_label(matched_ex['unit']))}</b>."
            view, markup = build_today_workout_view(get_or_create_daily_workout(today), today, intro=intro)
            await update.message.reply_text(view, reply_markup=markup, parse_mode=HTML)
            return True

    # 4. Today's Target Only: "today pushups 80", "today target pushups 80"
    m_today_target = re.match(r"^today\s+(?:target\s+)?([a-zA-Z\- ]+?)\s+(\d{1,4})$", text, re.IGNORECASE)
    if m_today_target:
        ex_name = m_today_target.group(1).strip()
        new_target = int(m_today_target.group(2))
        workout = get_or_create_daily_workout(today)
        item = find_item(workout["items"], ex_name)
        if item:
            update_today_item_target(item["id"], new_target)
            intro = f"🎯 <b>Today's target adjusted!</b> <b>{esc(item['exercise_name'])}</b> set to <b>{new_target} {esc(unit_label(item['unit']))}</b> for today only."
            view, markup = build_today_workout_view(get_or_create_daily_workout(today), today, intro=intro)
            await update.message.reply_text(view, reply_markup=markup, parse_mode=HTML)
            return True

    # 5. Add exercise on the fly: "add dips 20", "today add pull-ups 15"
    m_add = re.match(r"^(?:today\s+)?add\s+([a-zA-Z\- ]+?)\s+(\d{1,4})(?:\s*(reps|s|sec|seconds))?$", text, re.IGNORECASE)
    if m_add:
        ex_name = m_add.group(1).strip()
        target = int(m_add.group(2))
        raw_unit = m_add.group(3) or ("sec" if "plank" in ex_name.lower() else "reps")
        unit = "sec" if raw_unit.startswith("s") else "reps"
        added = add_today_workout_item(ex_name, target, unit, today_str=today)
        intro = f"➕ <b>Added to today's workout!</b> <b>{esc(added['exercise_name'])}</b> ({target} {esc(unit_label(added['unit']))})."
        view, markup = build_today_workout_view(get_or_create_daily_workout(today), today, intro=intro)
        await update.message.reply_text(view, reply_markup=markup, parse_mode=HTML)
        return True

    # 6. Swap exercise: "swap sit-ups for plank", "swap squats with lunges"
    m_swap = re.match(r"^swap\s+([a-zA-Z\- ]+?)\s+(?:for|with)\s+([a-zA-Z\- ]+?)$", text, re.IGNORECASE)
    if m_swap:
        old_name = m_swap.group(1).strip()
        new_name = m_swap.group(2).strip()
        swapped = swap_today_workout_item(old_name, new_name, today_str=today)
        if swapped:
            intro = f"🔄 <b>Swapped!</b> Replaced with <b>{esc(swapped['exercise_name'])}</b> for today."
            view, markup = build_today_workout_view(get_or_create_daily_workout(today), today, intro=intro)
            await update.message.reply_text(view, reply_markup=markup, parse_mode=HTML)
            return True

    return False


async def quick_log_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Logs typed amounts or natural commands. Returns False when text is not handled."""
    text = (update.message.text or "").strip()
    if not text:
        return False

    # 1. Check flexible text commands (water, pauses, targets, add, swap)
    if await handle_flexible_commands(update, context):
        return True

    # 2. Standard quick log requires digits (e.g. 35 push-ups)
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

    total_before = sum(it["completed_reps"] for it in workout["items"])
    target_before = sum(it["target_reps"] for it in workout["items"])
    was_pending = workout["status"] == "pending"

    lines = _log(found)
    hype = _calculate_hype(found, total_before, target_before, was_pending, today)
    if hype:
        lines.append(f"\n<b>{esc(hype)}</b>")

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
    workout = get_or_create_daily_workout(today)
    item = next((it for it in workout["items"] if it["id"] == int(item_id)), None)
    if not item:
        await query.answer("That's not part of today's workout anymore.", show_alert=True)
        return
    await query.answer()

    total_before = sum(it["completed_reps"] for it in workout["items"])
    target_before = sum(it["target_reps"] for it in workout["items"])
    was_pending = workout["status"] == "pending"

    lines = _log([(item, int(value))])
    hype = _calculate_hype([(item, int(value))], total_before, target_before, was_pending, today)
    if hype:
        lines.append(f"\n<b>{esc(hype)}</b>")

    view, markup = build_today_workout_view(get_or_create_daily_workout(today), today, intro="\n".join(lines))
    await query.edit_message_text(view, parse_mode=HTML, reply_markup=markup)

