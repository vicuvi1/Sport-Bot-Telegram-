#!/usr/bin/env bash
# Rebuilds the bot's virtualenv and restarts it.
#
# Run this after an Ubuntu release upgrade (or any change of the system
# python3): the old .venv points at a Python that no longer exists, and the
# bot would fail on its next restart. Data in data/ and backups/ is untouched.
#
#   cd /opt/workout-bot && ./scripts/rebuild_venv.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Python: $(python3 --version)"
python3 -m venv --clear .venv
./.venv/bin/pip install --quiet -r requirements.txt
./.venv/bin/python -m pytest -q
./.venv/bin/python main.py --check
sudo systemctl restart workout-bot
sleep 5
systemctl is-active workout-bot
echo "Done. You should get a 'Bot started' message in Telegram."
