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

# Telegram Mini App web server settings
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0").strip()
try:
    WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080").strip())
except ValueError:
    WEBAPP_PORT = 8080
WEBAPP_URL = os.getenv("WEBAPP_URL", f"http://localhost:{WEBAPP_PORT}").strip()
WEBAPP_DIR = BASE_DIR / "webapp"

