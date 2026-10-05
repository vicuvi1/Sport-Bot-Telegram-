"""Unattended-operation helpers: error reports to Telegram and an external heartbeat.

- TelegramErrorReporter forwards ERROR log records to the user's chat, rate
  limited, so problems are noticed without reading server logs.
- ping_healthcheck() pings an external dead-man's switch (e.g. healthchecks.io).
  If the pings stop because the server, network or bot died, that service
  alerts the user. A process can't report its own death, so this is the only
  way to catch a dead server.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class Health:
    """Process-wide health counters shown in /status and the weekly summary."""
    started_at: Optional[datetime] = None
    errors_total: int = 0
    errors_since_summary: int = 0
    last_heartbeat_ok: Optional[bool] = None
    last_heartbeat_at: Optional[datetime] = None
    last_backup_ok: Optional[bool] = None
    last_backup_message: str = ""


health = Health()


# Records carrying this attribute are never forwarded (prevents report loops).
SKIP_REPORT = {"skip_report": True}


class TelegramErrorReporter(logging.Handler):
    """Logging handler that sends ERROR records to the user via the bot.

    The same error is reported at most once per `cooldown` seconds, and at
    most `max_per_hour` reports are sent in total, so a failure loop can't
    flood the chat.
    """

    def __init__(self, bot, chat_id: int, loop: asyncio.AbstractEventLoop,
                 cooldown: float = 3600, max_per_hour: int = 10):
        super().__init__(level=logging.ERROR)
        self.bot = bot
        self.chat_id = chat_id
        self.loop = loop
        self.cooldown = cooldown
        self.max_per_hour = max_per_hour
        self._last_sent: Dict[str, float] = {}
        self._suppressed: Dict[str, int] = {}
        self._sent_times: list = []

    def _key(self, record: logging.LogRecord) -> str:
        return f"{record.name}:{record.getMessage()[:80]}"

    def should_send(self, record: logging.LogRecord, now: Optional[float] = None) -> bool:
        """Applies the per-error cooldown and the hourly cap."""
        now = time.monotonic() if now is None else now
        key = self._key(record)
        last = self._last_sent.get(key)
        self._sent_times = [t for t in self._sent_times if now - t < 3600]
        if (last is not None and now - last < self.cooldown) or len(self._sent_times) >= self.max_per_hour:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False
        self._last_sent[key] = now
        self._sent_times.append(now)
        return True

    def format_report(self, record: logging.LogRecord) -> str:
        key = self._key(record)
        suppressed = self._suppressed.pop(key, 0)
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            message += f"\n{type(exc).__name__}: {exc}"
        text = f"🚨 Bot error ({record.name})\n\n{message[:900]}"
        if suppressed:
            text += f"\n\n(+{suppressed} similar errors not reported)"
        text += "\n\nDetails: journalctl -u workout-bot"
        return text

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR or getattr(record, "skip_report", False):
            return
        health.errors_total += 1
        health.errors_since_summary += 1
        try:
            if not self.should_send(record):
                return
            text = self.format_report(record)
            # emit() may run outside the event loop (e.g. APScheduler threads).
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._send(text)))
        except Exception:
            # A logging handler must never raise.
            pass

    async def _send(self, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception as e:
            logger.warning("Could not send error report: %s", e, extra=SKIP_REPORT)


def install_error_reporter(bot, chat_id: int, loop: asyncio.AbstractEventLoop) -> Optional[TelegramErrorReporter]:
    """Attaches the reporter to the root logger (once). Returns it, or None without a chat."""
    if not chat_id:
        return None
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, TelegramErrorReporter):
            root.removeHandler(h)
    reporter = TelegramErrorReporter(bot, chat_id, loop)
    root.addHandler(reporter)
    return reporter


async def ping_healthcheck(url: str, bot=None) -> bool:
    """Pings the dead-man's switch. Reports failure if Telegram is unreachable.

    healthchecks.io convention: GET <url> means "alive", GET <url>/fail means
    "alive but broken". Network problems here are only logged as warnings: if
    the internet is down, no Telegram error report could be delivered anyway,
    and the missing ping is exactly what triggers the external alert.
    """
    ok = True
    if bot is not None:
        try:
            await bot.get_me()
        except Exception as e:
            ok = False
            logger.warning("Heartbeat: Telegram API check failed: %s", e)

    target = url if ok else url.rstrip("/") + "/fail"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(target)
    except Exception as e:
        logger.warning("Heartbeat ping failed: %s", e)
        ok = False

    health.last_heartbeat_ok = ok
    health.last_heartbeat_at = datetime.now(timezone.utc)
    return ok
