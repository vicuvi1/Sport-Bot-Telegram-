"""Every HTML screen must be valid Telegram HTML.

Telegram rejects the whole message on an unknown tag, an unclosed tag or an
unescaped "<" / "&", so a formatting slip would silently break a screen. This
renders each screen in its interesting states (including hostile exercise
names) and checks the markup the way Telegram's parser would.
"""

from html.parser import HTMLParser

import pytest

import config
from database import get_connection, set_setting
from handlers.settings import build_settings_menu
from handlers.start import build_home_text
from handlers.stats import build_badges_text, build_history_text
from services import partner_service as ps
from services.stats_service import format_stats_message, get_stats_for_period
from services.summary_service import build_weekly_summary
from services.workout_service import (
    add_exercise,
    complete_all_exercises_for_workout,
    generate_workout_heatmap,
    get_current_date_str,
    get_or_create_daily_workout,
    record_feedback,
    start_pause,
    start_quick_workout,
    update_workout_item,
)
from views import build_evening_nudge, build_exercise_detail_view, build_today_workout_view, feedback_notice

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre",
                "a", "blockquote", "tg-spoiler", "span"}


class TelegramHTMLChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"tag <{tag}> not allowed")
        if tag == "blockquote" and attrs not in ([], [("expandable", None)]):
            self.errors.append(f"bad blockquote attrs {attrs}")
        if tag == "blockquote" and "blockquote" in self.stack:
            self.errors.append("nested blockquote")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> (open: {self.stack})")
        else:
            self.stack.pop()

    def handle_entityref(self, name):
        if name not in ("lt", "gt", "amp", "quot"):
            self.errors.append(f"unsupported entity &{name};")


def assert_valid_html(text):
    # A bare "&" (not an entity) or a "<" that isn't a tag is what breaks Telegram.
    import re
    for m in re.finditer(r"&(?!(lt|gt|amp|quot);)", text):
        raise AssertionError(f"unescaped & at {m.start()}: {text[max(0, m.start() - 20):m.start() + 20]!r}")
    checker = TelegramHTMLChecker()
    checker.feed(text)
    checker.close()
    assert not checker.errors, checker.errors
    assert not checker.stack, f"unclosed tags {checker.stack}"
    assert len(text) <= 4096, f"message too long ({len(text)})"


@pytest.fixture
def hostile_names():
    """Exercise names that would break unescaped HTML."""
    add_exercise("Push & <Pull>", 20)
    add_exercise("Dips > Rows", 10, "sec")


def today_states(today):
    workout = get_or_create_daily_workout(today)
    yield "pending", workout
    update_workout_item(workout["items"][0]["id"], completed_reps=5)
    update_workout_item(workout["items"][1]["id"], status="skipped")
    yield "partial", get_or_create_daily_workout(today)
    yield "quick", start_quick_workout(workout["id"])
    complete_all_exercises_for_workout(workout["id"])
    yield "completed quick", get_or_create_daily_workout(today)


def test_today_screens_are_valid(hostile_names):
    today = get_current_date_str()
    for state, workout in today_states(today):
        text, _ = build_today_workout_view(workout, today, intro="☀️ <b>Good morning!</b>")
        assert_valid_html(text), state
        for item in workout["items"]:
            assert_valid_html(build_exercise_detail_view(item)[0])
    assert_valid_html(build_evening_nudge(get_or_create_daily_workout(today), today)[0])


def test_completed_feedback_rest_and_paused_screens_are_valid(hostile_names):
    done = get_or_create_daily_workout("2026-10-01")
    complete_all_exercises_for_workout(done["id"])
    done = get_or_create_daily_workout("2026-10-01")
    assert_valid_html(build_today_workout_view(done, "2026-10-01")[0])
    result = record_feedback(done["id"], "hard")
    assert_valid_html(feedback_notice(result))
    assert_valid_html(build_today_workout_view(get_or_create_daily_workout("2026-10-01"), "2026-10-01")[0])

    set_setting("workout_days", "0")  # only Mondays: 2026-10-07 is a Wednesday
    assert_valid_html(build_today_workout_view(get_or_create_daily_workout("2026-10-07"), "2026-10-07")[0])

    start_pause(3, today_str="2026-10-12")
    assert_valid_html(build_today_workout_view(get_or_create_daily_workout("2026-10-12"), "2026-10-12")[0])


def test_home_settings_stats_history_summary_are_valid(hostile_names):
    today = get_current_date_str()
    workout = get_or_create_daily_workout(today)
    complete_all_exercises_for_workout(workout["id"])
    code = ps.create_invite()
    ps.accept_invite(code, 42, "Ana & <Co>", config.USER_ID)

    assert_valid_html(build_home_text("Vic <tor> & co"))
    assert_valid_html(build_settings_menu()[0])
    for period in ("today", "week", "month", "all"):
        assert_valid_html(format_stats_message(get_stats_for_period(period)))
    assert_valid_html(build_badges_text())
    assert_valid_html(build_history_text())
    assert_valid_html(generate_workout_heatmap())
    assert_valid_html(build_weekly_summary(health_line="🤝 Shared with Ana &amp; co.\n🩺 Bot health: ok"))


def test_empty_database_screens_are_valid():
    with get_connection() as conn:
        conn.execute("DELETE FROM exercises;")
        conn.commit()
    today = get_current_date_str()
    assert_valid_html(build_home_text(""))
    assert_valid_html(build_history_text())
    assert_valid_html(format_stats_message(get_stats_for_period("week")))
    assert_valid_html(build_today_workout_view(get_or_create_daily_workout(today), today)[0])


def test_checker_catches_broken_markup():
    for bad in ("<b>open", "a & b", "<div>x</div>", "<b><i>x</b></i>", "x &nbsp; y"):
        with pytest.raises(AssertionError):
            assert_valid_html(bad)
