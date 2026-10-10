import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load .env file
load_dotenv(BASE_DIR / ".env")

# Telegram credentials and security
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
USER_ID_RAW = os.getenv("TELEGRAM_USER_ID", "0").strip()
try:
    USER_ID = int(USER_ID_RAW)
except ValueError:
    USER_ID = 0

# Default regional & scheduling settings
TIMEZONE = os.getenv("TIMEZONE", "Europe/Chisinau").strip()
WORKOUT_TIME = os.getenv("WORKOUT_TIME", "07:00").strip()

# Optional external dead-man's switch (e.g. https://hc-ping.com/<uuid>).
# The bot pings it every few minutes; if pings stop, that service alerts you.
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "").strip()
if HEALTHCHECK_URL and not HEALTHCHECK_URL.startswith("https://"):
    HEALTHCHECK_URL = ""

# Database & backup paths
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "workout.db"

BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# Telegram Mini App web server settings. The app listens locally; a reverse
# proxy (Caddy) serves it publicly over HTTPS at WEBAPP_URL, which Telegram
# requires. Without an https:// WEBAPP_URL the app stays off.
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "127.0.0.1").strip()
try:
    WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080").strip())
except ValueError:
    WEBAPP_PORT = 8080
WEBAPP_URL = os.getenv("WEBAPP_URL", f"http://localhost:{WEBAPP_PORT}").strip()
WEBAPP_DIR = BASE_DIR / "webapp"

# BOT_MODE=simple (default): a plain workout bot. Today, Progress, History,
# Settings and logging by typing ("35 push-ups"); no Mini App, and the crew,
# duel, wake-up, points, quests and level features stay quiet and hidden.
# BOT_MODE=full turns everything back on (the code is all still here).
BOT_MODE = os.getenv("BOT_MODE", "simple").strip().lower()
FULL_MODE = BOT_MODE == "full"
WEBAPP_ENABLED = FULL_MODE and WEBAPP_URL.startswith("https://")

