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

# Database & backup paths
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "workout.db"

BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
