"""Telegram Mini App authentication.

Telegram passes the app a signed `initData` string (who opened it, when).
The app sends it with every API call ("Authorization: tma <initData>"), and
we verify the signature with the bot token, as documented at
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
Only a valid, recent signature from a crew member gets through.
"""

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl

# How old a signed session may be. Telegram signs when the app is opened;
# a day covers leaving the app open all day without reopening it.
MAX_AGE_SECONDS = 24 * 3600


class AuthError(Exception):
    pass


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign_init_data(fields: Dict[str, str], bot_token: str) -> str:
    """Builds a signed initData string (used by tests; Telegram does this for real)."""
    from urllib.parse import urlencode
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    digest = hmac.new(_secret_key(bot_token), check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def verify_init_data(init_data: str, bot_token: str, now: Optional[float] = None) -> Dict[str, Any]:
    """Returns the Telegram user ({"id", "first_name", ...}) or raises AuthError."""
    if not init_data or not bot_token:
        raise AuthError("missing init data")
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = fields.pop("hash", "")
    if not received:
        raise AuthError("missing hash")

    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    expected = hmac.new(_secret_key(bot_token), check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("bad signature")

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        raise AuthError("bad auth_date")
    now = time.time() if now is None else now
    if not auth_date or now - auth_date > MAX_AGE_SECONDS:
        raise AuthError("session expired")

    try:
        user = json.loads(fields.get("user", ""))
        int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise AuthError("no user")
    return user
