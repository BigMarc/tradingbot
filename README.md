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
git clone https://github.com/BigMarc/tradingbot && cd ~/hyperliquid-bot
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
| **Drawdown Shield** | Progressive size reduction: -3% daily = 100%, -5% = 50%, -7% = 25%, -8% = pause |
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

## Testnet First

Always start on testnet:

1. Set `NETWORK=testnet` in `.env`
2. Get testnet USDC: https://app.hyperliquid-testnet.xyz/drip
3. Run the bot and verify trades execute correctly
4. Switch to `NETWORK=mainnet` when confident

## Cost Estimates

- **Anthropic API**: ~$1-5/day (5-15 AI calls/day via Sonnet 4.6)
- **Trading Fees**: Maker 0.01%, Taker 0.035% (tracked per trade)
- **VPS**: ~$5-10/month (1 vCPU, 2GB RAM sufficient)
