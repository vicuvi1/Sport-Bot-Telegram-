"""Tests for startup configuration checks in main.py."""

from pathlib import Path

import pytest

import main


@pytest.mark.parametrize("token", ["", "   ", None, "PUT_YOUR_NEW_TOKEN_HERE", "your_bot_token_from_botfather"])
def test_placeholder_or_missing_token_is_not_configured(token):
    assert main.token_is_configured(token) is False


def test_real_looking_token_is_configured():
    assert main.token_is_configured("123456:ABC-fake-token-for-tests") is True


def test_env_example_token_is_a_known_placeholder():
    """.env.example must ship a value that main.py recognises as 'not set'."""
    env_example = Path(main.config.BASE_DIR) / ".env.example"
    values = dict(
        line.split("=", 1)
        for line in env_example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    assert main.token_is_configured(values["TELEGRAM_BOT_TOKEN"]) is False


def test_requirements_file_is_utf8():
    """requirements.txt must be plain UTF-8 so pip works on every platform."""
    raw = (Path(main.config.BASE_DIR) / "requirements.txt").read_bytes()
    assert not raw.startswith((b"\xff\xfe", b"\xfe\xff")), "requirements.txt is UTF-16"
    assert b"\x00" not in raw
    assert "python-telegram-bot" in raw.decode("utf-8")
