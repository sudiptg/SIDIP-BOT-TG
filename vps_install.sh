#!/usr/bin/env bash
# Bare-metal VPS installer (Ubuntu / Debian) - runs the bot as a systemd service, no Docker needed.
# Usage (from the project folder):  sudo bash deploy/vps_install.sh
set -euo pipefail

APP_DIR=/opt/autorename-bot
[ "$(id -u)" -eq 0 ] || { echo "Please run as root: sudo bash deploy/vps_install.sh"; exit 1; }

apt-get update -y
apt-get install -y python3 python3-venv python3-pip python3-dev gcc libffi-dev ffmpeg aria2 rsync

id autorename >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin autorename
mkdir -p "$APP_DIR"
rsync -a --exclude venv --exclude .git --exclude data --exclude downloads --exclude .env ./ "$APP_DIR/"
[ -f "$APP_DIR/.env" ] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

chown -R autorename:autorename "$APP_DIR"
chmod 600 "$APP_DIR/.env"
cp deploy/autorename-bot.service /etc/systemd/system/autorename-bot.service
systemctl daemon-reload
systemctl enable autorename-bot

echo
echo "Installed. Next steps:"
echo "  1) nano $APP_DIR/.env          (fill API_ID, API_HASH, BOT_TOKEN, OWNER_ID)"
echo "  2) systemctl restart autorename-bot"
echo "  3) journalctl -u autorename-bot -f"
