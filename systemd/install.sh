#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/home/trading/hyperliquid-bot"
SERVICE_NAME="trading-bot"
USER_NAME="trading"

echo "=== Hyperliquid Trading Bot Installer ==="

# 1. Install system dependencies
echo "[1/9] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq build-essential python3.12 python3.12-venv python3.12-dev curl git sqlite3

# 2. Create user if it doesn't exist
echo "[2/9] Setting up user..."
if ! id "$USER_NAME" &>/dev/null; then
    sudo useradd -m -s /bin/bash "$USER_NAME"
    echo "Created user: $USER_NAME"
fi

# 3. Install uv
echo "[3/9] Installing uv package manager..."
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# 4. Copy project files
echo "[4/9] Setting up project..."
if [ "$(pwd)" != "$INSTALL_DIR" ]; then
    sudo mkdir -p "$INSTALL_DIR"
    sudo cp -r . "$INSTALL_DIR/"
    sudo chown -R "$USER_NAME:$USER_NAME" "$INSTALL_DIR"
fi

# 5. Install Python dependencies
echo "[5/9] Installing Python dependencies..."
cd "$INSTALL_DIR"
sudo -u "$USER_NAME" bash -c "cd $INSTALL_DIR && python3.12 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e ."

# 6. Initialize database
echo "[6/9] Initializing database..."
sudo -u "$USER_NAME" bash -c "cd $INSTALL_DIR && .venv/bin/python -c 'import asyncio; from storage.database import Database; asyncio.run(Database().connect())'"
echo "Database initialized."

# 7. Enable NTP time synchronization
echo "[7/9] Enabling NTP time sync..."
if command -v timedatectl &>/dev/null; then
    sudo timedatectl set-ntp true
    echo "NTP synchronization enabled"
else
    echo "timedatectl not found, skipping NTP setup (ensure time is synchronized manually)"
fi

# 8. Install systemd service
echo "[8/9] Installing systemd service..."
sudo cp "$INSTALL_DIR/systemd/trading-bot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# 9. Setup daily database backup cron
echo "[9/9] Setting up daily DB backup cron..."
CRON_CMD="0 3 * * * cp $INSTALL_DIR/trading_bot.db $INSTALL_DIR/storage/backups/trading_bot_\$(date +\\%Y\\%m\\%d).db && find $INSTALL_DIR/storage/backups/ -name 'trading_bot_*.db' -mtime +7 -delete"
(sudo -u "$USER_NAME" crontab -l 2>/dev/null | grep -v "trading_bot.*backup"; echo "$CRON_CMD") | sudo -u "$USER_NAME" crontab -
echo "Daily backup cron installed (3:00 AM, keep 7 days)"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $INSTALL_DIR/.env with your credentials"
echo "  2. Test connectivity: sudo -u $USER_NAME $INSTALL_DIR/.venv/bin/python -c \\"
echo "     \"import asyncio; from core.exchange import Exchange; e = Exchange(); asyncio.run(e.connect()); print('Connected!')\""
echo "  3. Start the bot: sudo systemctl start $SERVICE_NAME"
echo "  4. View logs: sudo journalctl -u $SERVICE_NAME -f"
echo ""
