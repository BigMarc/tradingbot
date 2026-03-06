# Hyperliquid Autonomous Trading Bot

Autonomous crypto trading system for Hyperliquid Perpetual Futures with AI-powered decision making (Claude Sonnet 4.6), real-time WebSocket data, and full risk management.

## Architecture

```
Layer 1: Data Collector (0 token cost)    - WebSocket feeds, OHLCV candles, funding rates
Layer 2: Signal Engine  (0 token cost)    - Technical indicators + 0-100 signal scoring
Layer 3: AI Brain       (~5-15 calls/day) - Claude Sonnet 4.6 trade decisions
```

### Project Structure

```
config/          - Settings, strategy.yaml, blacklist
core/            - Exchange, market data, indicators, signals, AI, positions, risk, orders
services/        - Data collector, trader, optimizer, Telegram bot, health monitor
storage/         - SQLite database with WAL mode
utils/           - Logging (loguru), helpers
systemd/         - Service file + install script
tests/           - Unit tests for indicators, signals, risk management
```

## Quick Start (Linux/Ubuntu VPS)

```bash
# 1. Clone and configure
git clone <repo> ~/hyperliquid-bot && cd ~/hyperliquid-bot
cp .env.example .env   # or edit .env directly
nano .env              # Fill in your credentials

# 2. Install with script
chmod +x systemd/install.sh
sudo bash systemd/install.sh

# 3. Or install manually
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .

# 4. Run (testnet first!)
NETWORK=testnet python main.py

# 5. As systemd service
sudo systemctl start trading-bot
sudo journalctl -u trading-bot -f
```

## Running on Windows

Windows does not support `uvloop` (Linux-only). The bot handles this gracefully and falls back to the default asyncio event loop.

### Option A: Native Windows (Python 3.12+)

```powershell
# 1. Install Python 3.12+ from python.org (check "Add to PATH")

# 2. Clone the repo
git clone <repo> C:\hyperliquid-bot
cd C:\hyperliquid-bot

# 3. Create virtual environment
python -m venv .venv
.venv\Scripts\activate

# 4. Install dependencies
pip install -e .
# Note: coincurve may need Visual C++ Build Tools:
# https://visualstudio.microsoft.com/visual-cpp-build-tools/
# Select "Desktop development with C++"

# 5. Configure
copy .env.example .env
notepad .env
# Fill in: HYPERLIQUID_WALLET_ADDRESS, HYPERLIQUID_API_PRIVATE_KEY,
#          ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# Set: NETWORK=testnet

# 6. Run
python main.py

# 7. To run in background (PowerShell)
Start-Process -NoNewWindow python -ArgumentList "main.py" -RedirectStandardOutput bot.log -RedirectStandardError bot_err.log
```

### Option B: WSL2 (Recommended for Windows)

```powershell
# 1. Enable WSL2
wsl --install -d Ubuntu-24.04

# 2. Inside WSL, follow the Linux instructions above
```

### Option C: Docker

```dockerfile
# Create Dockerfile in project root:
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*
COPY . .
RUN pip install -e .
CMD ["python", "main.py"]
```

```powershell
docker build -t trading-bot .
docker run -d --name trading-bot --env-file .env trading-bot
docker logs -f trading-bot
```

### Windows-Specific Notes

- `uvloop` is skipped automatically on Windows (falls back to asyncio)
- `coincurve` requires Visual C++ Build Tools on Windows
- Log files go to `logs/` directory relative to the project root
- Use `Ctrl+C` to stop gracefully (SIGINT handler works on Windows)

## Configuration

### .env (Secrets - never commit!)

```env
HYPERLIQUID_WALLET_ADDRESS=0x...
HYPERLIQUID_API_PRIVATE_KEY=0x...
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=-100...
NETWORK=testnet
INITIAL_BANKROLL=1000
```

### config/strategy.yaml (Tunable Parameters)

All strategy parameters are in `strategy.yaml` and can be changed without code modifications.
The Optimizer service reviews and adjusts these every 4 hours.

Key sections:
- `signal` - Score thresholds, indicator weights, funding rate modifier
- `trading` - Position sizing, trailing stops, take profits, cooldowns
- `risk` - Drawdown shield tiers, overtrading limits, chop market detection
- `ai` - Model selection, temperature, timeout

## Risk Management

| Feature | Description |
|---------|-------------|
| **Drawdown Shield** | Progressive size reduction: -3% daily = 50%, -5% = 25%, -7% = pause |
| **Max Leverage** | Hard cap at 5x, enforced at code level |
| **Max Position Size** | Hard cap at 5% of bankroll per trade |
| **Total Drawdown** | Bot stops at -20% from peak (manual restart required) |
| **Min Bankroll** | Bot stops below $100 |
| **Overtrading Guard** | Pauses after 6+ trades/hour |
| **Chop Detection** | Pauses when 60%+ of last 10 trades are stop losses |
| **Cooldowns** | 5min after close, 15min after stop loss, 60min after 3 consecutive losses |
| **Zombie Protection** | Force-closes stale trades after max hold time |

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Portfolio status + open positions |
| `/trades` | Last 10 trades with PnL |
| `/stats` | 24h performance statistics |
| `/pause` | Pause trading (positions still managed) |
| `/resume` | Resume trading |
| `/close_all` | Close all positions immediately |
| `/params` | Show current strategy parameters |
| `/balance` | Wallet balance + API costs |

## VPS Setup (Production Deployment)

### Choosing a VPS Provider

Any Linux VPS with 1 vCPU, 2GB RAM, and 20GB SSD is sufficient. Recommended providers:

| Provider | Plan | Cost | Notes |
|----------|------|------|-------|
| Hetzner | CX22 | ~€4/month | EU data centers, excellent value |
| DigitalOcean | Basic Droplet | $6/month | Simple UI, good docs |
| Vultr | Cloud Compute | $6/month | Many locations worldwide |
| Contabo | VPS S | ~€5/month | Most RAM for the price |
| Oracle Cloud | Free Tier | $0 | ARM instances, always-free tier |

Choose a data center **geographically close to Hyperliquid's servers** for lower latency (US East or Europe).

### Step-by-Step VPS Setup

#### 1. Initial Server Setup

```bash
# SSH into your new VPS
ssh root@YOUR_VPS_IP

# Create a non-root user (never run the bot as root)
adduser trading
usermod -aG sudo trading

# Setup SSH key auth (from your local machine)
ssh-copy-id trading@YOUR_VPS_IP

# Disable password auth (optional but recommended)
sudo nano /etc/ssh/sshd_config
# Set: PasswordAuthentication no
sudo systemctl restart sshd

# Setup basic firewall
sudo ufw allow OpenSSH
sudo ufw enable
```

#### 2. Install System Dependencies

```bash
# Login as trading user
ssh trading@YOUR_VPS_IP

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.12 and build tools
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.12 python3.12-venv python3.12-dev build-essential git sqlite3 curl

# Enable NTP time sync (critical for trading)
sudo timedatectl set-ntp true
timedatectl status  # Verify "NTP synchronized: yes"
```

#### 3. Clone and Configure the Bot

```bash
# Clone the repository
git clone <repo> ~/hyperliquid-bot
cd ~/hyperliquid-bot

# Create virtual environment
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[linux]"  # Includes uvloop for Linux

# Create .env file
cp .env.example .env
nano .env
```

Fill in your `.env`:

```env
HYPERLIQUID_WALLET_ADDRESS=0xYourWalletAddress
HYPERLIQUID_API_PRIVATE_KEY=0xYourPrivateKey
ANTHROPIC_API_KEY=sk-ant-YourAnthropicKey
TELEGRAM_BOT_TOKEN=123456789:ABCDefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_CHAT_ID=-1001234567890
NETWORK=testnet
INITIAL_BANKROLL=1000
CONFIRM_MAINNET=
```

#### 4. Test Connectivity

```bash
cd ~/hyperliquid-bot
source .venv/bin/activate

# Test exchange connection
python -c "
import asyncio
from core.exchange import Exchange
async def test():
    e = Exchange()
    await e.connect()
    b = await e.fetch_balance()
    print(f'Connected! Balance: {b.get(\"total\", {}).get(\"USDC\", 0)} USDC')
asyncio.run(test())
"

# Test Telegram
python -c "
from services.telegram_bot import TelegramBot
import asyncio
async def test():
    t = TelegramBot()
    await t.send_message('Bot test message from VPS!')
asyncio.run(test())
"
```

#### 5. Install as systemd Service

```bash
# Use the install script (handles everything)
cd ~/hyperliquid-bot
sudo bash systemd/install.sh

# Or install manually:
sudo cp systemd/trading-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
```

#### 6. Start the Bot

```bash
# Start on testnet first
sudo systemctl start trading-bot

# View logs in real-time
sudo journalctl -u trading-bot -f

# Check status
sudo systemctl status trading-bot
```

#### 7. Switch to Mainnet

After verifying the bot works correctly on testnet:

```bash
# Edit .env
nano ~/hyperliquid-bot/.env
# Set: NETWORK=mainnet
# Set: CONFIRM_MAINNET=true  (required safety check)

# Restart
sudo systemctl restart trading-bot
```

### Monitoring & Maintenance

#### Log Management

```bash
# View recent logs
sudo journalctl -u trading-bot --since "1 hour ago"

# View errors only
sudo journalctl -u trading-bot -p err

# Log rotation is handled by journald automatically
```

#### Database Backups

The install script sets up automatic daily backups at 3:00 AM UTC, keeping the last 7 days. Backups are stored in `~/hyperliquid-bot/storage/backups/`.

```bash
# Create backup directory if not exists
mkdir -p ~/hyperliquid-bot/storage/backups

# Manual backup
cp ~/hyperliquid-bot/trading_bot.db ~/hyperliquid-bot/storage/backups/trading_bot_$(date +%Y%m%d_%H%M).db

# Verify cron backup is set
crontab -l
```

#### Updating the Bot

```bash
cd ~/hyperliquid-bot
git pull origin main
source .venv/bin/activate
pip install -e ".[linux]"
sudo systemctl restart trading-bot
```

#### Health Checks

The bot has built-in health monitoring:
- **Exchange maintenance detection**: Pauses trading during exchange outages
- **WebSocket reconnection**: Automatic reconnect with exponential backoff
- **Clock drift check**: Alerts if system time drifts >2 seconds
- **Task watchdog**: Restarts crashed service tasks automatically
- **Telegram alerts**: Instant notifications for errors and risk events

#### Security Hardening

```bash
# Restrict .env file permissions
chmod 600 ~/hyperliquid-bot/.env

# Ensure only trading user can access the bot directory
chmod 700 ~/hyperliquid-bot

# Setup automatic security updates
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

# API key rotation reminder:
# Hyperliquid API keys expire after 180 days
# The bot logs a reminder on every startup
# Set a calendar reminder to rotate keys before expiry
```

#### Systemd Service Commands

```bash
sudo systemctl start trading-bot     # Start
sudo systemctl stop trading-bot      # Stop (positions stay open)
sudo systemctl restart trading-bot   # Restart
sudo systemctl status trading-bot    # Status
sudo systemctl enable trading-bot    # Enable auto-start on boot
sudo systemctl disable trading-bot   # Disable auto-start
```

## Testnet First

Always start on testnet:

1. Set `NETWORK=testnet` in `.env`
2. Get testnet USDC: https://app.hyperliquid-testnet.xyz/drip
3. Run the bot and verify trades execute correctly
4. Switch to `NETWORK=mainnet` when confident (requires `CONFIRM_MAINNET=true`)

## Cost Estimates

- **Anthropic API**: ~$1-5/day (5-15 AI calls/day via Sonnet 4.6)
- **Trading Fees**: Maker 0.01%, Taker 0.035% (tracked per trade)
- **VPS**: ~$5-10/month (1 vCPU, 2GB RAM sufficient)
