#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/home/trading/hyperliquid-bot"
SERVICE_NAME="trading-bot"
USER_NAME="trading"

echo "=== Hyperliquid Trading Bot Installer ==="

# 1. Install system dependencies
echo "[1/7] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq build-essential python3.12 python3.12-venv python3.12-dev curl git sqlite3

# 2. Create user if it doesn't exist
echo "[2/7] Setting up user..."
if ! id "$USER_NAME" &>/dev/null; then
    sudo useradd -m -s /bin/bash "$USER_NAME"
    echo "Created user: $USER_NAME"
fi

# 3. Install uv
echo "[3/7] Installing uv package manager..."
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# 4. Copy project files
echo "[4/7] Setting up project..."
if [ "$(pwd)" != "$INSTALL_DIR" ]; then
    sudo mkdir -p "$INSTALL_DIR"
    sudo cp -r . "$INSTALL_DIR/"
    sudo chown -R "$USER_NAME:$USER_NAME" "$INSTALL_DIR"
fi

# 5. Install Python dependencies
echo "[5/7] Installing Python dependencies..."
cd "$INSTALL_DIR"
sudo -u "$USER_NAME" bash -c "cd $INSTALL_DIR && python3.12 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e ."

# 6. Initialize database
echo "[6/7] Initializing database..."
sudo -u "$USER_NAME" bash -c "cd $INSTALL_DIR && .venv/bin/python -c 'import asyncio; from storage.database import Database; asyncio.run(Database().connect())'"
echo "Database initialized."

# 7. Install systemd service
echo "[7/7] Installing systemd service..."
sudo cp "$INSTALL_DIR/systemd/trading-bot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

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
