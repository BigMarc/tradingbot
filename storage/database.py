from __future__ import annotations

import aiosqlite
import orjson
import time
from pathlib import Path
from utils.logger import logger

DB_PATH = Path(__file__).resolve().parent.parent / "trading_bot.db"

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS price_ticks (
    timestamp REAL NOT NULL,
    token TEXT NOT NULL,
    bid REAL,
    ask REAL,
    mid REAL NOT NULL,
    volume REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ticks_token_ts ON price_ticks(token, timestamp);

CREATE TABLE IF NOT EXISTS candles (
    timestamp REAL NOT NULL,
    token TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL DEFAULT 0,
    PRIMARY KEY (token, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_token_tf ON candles(token, timeframe, timestamp);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    token TEXT NOT NULL,
    direction TEXT NOT NULL,
    score REAL NOT NULL,
    indicators_json TEXT,
    ai_decision_json TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    leverage REAL NOT NULL,
    size_usd REAL NOT NULL,
    pnl_usd REAL DEFAULT 0,
    pnl_pct REAL DEFAULT 0,
    fees REAL DEFAULT 0,
    entry_time REAL NOT NULL,
    exit_time REAL,
    exit_reason TEXT,
    ai_reasoning TEXT,
    slippage_pct REAL DEFAULT 0,
    status TEXT DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    timestamp REAL NOT NULL,
    bankroll REAL NOT NULL,
    unrealized_pnl REAL DEFAULT 0,
    open_positions_json TEXT
);

CREATE TABLE IF NOT EXISTS optimizer_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    stats_json TEXT,
    changes_json TEXT,
    ai_response TEXT
);

CREATE TABLE IF NOT EXISTS token_stats (
    token TEXT PRIMARY KEY,
    total_trades INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    blacklisted INTEGER DEFAULT 0,
    last_traded REAL
);

CREATE TABLE IF NOT EXISTS api_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    service TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS funding_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    token TEXT NOT NULL,
    rate REAL NOT NULL,
    annualized_pct REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funding_token_time ON funding_rates(token, timestamp);
"""

CLEANUP_SQL = """
DELETE FROM price_ticks WHERE timestamp < ?;
DELETE FROM candles WHERE timeframe = '1m' AND timestamp < ?;
"""


class Database:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(CREATE_TABLES_SQL)
        await self._db.commit()
        logger.info("Database initialized at {}", self.db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    # ── Price Ticks ──────────────────────────────────────────

    async def insert_tick(self, token: str, bid: float | None, ask: float | None, mid: float, volume: float = 0) -> None:
        await self.db.execute(
            "INSERT INTO price_ticks (timestamp, token, bid, ask, mid, volume) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), token, bid, ask, mid, volume),
        )
        await self.db.commit()

    async def insert_ticks_batch(self, ticks: list[tuple]) -> None:
        await self.db.executemany(
            "INSERT INTO price_ticks (timestamp, token, bid, ask, mid, volume) VALUES (?, ?, ?, ?, ?, ?)",
            ticks,
        )
        await self.db.commit()

    # ── Candles ──────────────────────────────────────────────

    async def upsert_candle(self, token: str, timeframe: str, ts: float, o: float, h: float, l: float, c: float, v: float) -> None:
        await self.db.execute(
            """INSERT INTO candles (timestamp, token, timeframe, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(token, timeframe, timestamp) DO UPDATE SET
                 high = MAX(candles.high, excluded.high),
                 low = MIN(candles.low, excluded.low),
                 close = excluded.close,
                 volume = excluded.volume""",
            (ts, token, timeframe, o, h, l, c, v),
        )
        await self.db.commit()

    async def get_candles(self, token: str, timeframe: str, limit: int = 100) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM candles WHERE token = ? AND timeframe = ? ORDER BY timestamp DESC LIMIT ?",
            (token, timeframe, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Signals ──────────────────────────────────────────────

    async def insert_signal(self, token: str, direction: str, score: float, indicators: dict, ai_decision: dict | None = None) -> int:
        cursor = await self.db.execute(
            "INSERT INTO signals (timestamp, token, direction, score, indicators_json, ai_decision_json) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), token, direction, score, orjson.dumps(indicators).decode(), orjson.dumps(ai_decision).decode() if ai_decision else None),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore

    # ── Trades ───────────────────────────────────────────────

    async def insert_trade(self, token: str, direction: str, entry_price: float, leverage: float, size_usd: float, ai_reasoning: str = "", slippage_pct: float = 0.0) -> int:
        cursor = await self.db.execute(
            """INSERT INTO trades (token, direction, entry_price, leverage, size_usd, entry_time, ai_reasoning, slippage_pct, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (token, direction, entry_price, leverage, size_usd, time.time(), ai_reasoning, slippage_pct),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore

    async def close_trade(self, trade_id: int, exit_price: float, pnl_usd: float, pnl_pct: float, fees: float, exit_reason: str) -> None:
        await self.db.execute(
            """UPDATE trades SET exit_price = ?, pnl_usd = ?, pnl_pct = ?, fees = ?,
               exit_time = ?, exit_reason = ?, status = 'closed' WHERE id = ?""",
            (exit_price, pnl_usd, pnl_pct, fees, time.time(), exit_reason, trade_id),
        )
        await self.db.commit()

    async def get_open_trades(self) -> list[dict]:
        cursor = await self.db.execute("SELECT * FROM trades WHERE status = 'open'")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_recent_trades(self, limit: int = 10) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_today_trades(self) -> list[dict]:
        day_start = time.time() - (time.time() % 86400)
        cursor = await self.db.execute(
            "SELECT * FROM trades WHERE status = 'closed' AND exit_time >= ? ORDER BY exit_time DESC",
            (day_start,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_trades_since(self, since: float) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM trades WHERE status = 'closed' AND exit_time >= ? ORDER BY exit_time ASC",
            (since,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Portfolio Snapshots ──────────────────────────────────

    async def insert_snapshot(self, bankroll: float, unrealized_pnl: float, open_positions: list[dict]) -> None:
        await self.db.execute(
            "INSERT INTO portfolio_snapshots (timestamp, bankroll, unrealized_pnl, open_positions_json) VALUES (?, ?, ?, ?)",
            (time.time(), bankroll, unrealized_pnl, orjson.dumps(open_positions).decode()),
        )
        await self.db.commit()

    async def get_peak_bankroll(self) -> float:
        cursor = await self.db.execute("SELECT MAX(bankroll) as peak FROM portfolio_snapshots")
        row = await cursor.fetchone()
        return row["peak"] if row and row["peak"] else 0.0

    # ── Optimizer Log ────────────────────────────────────────

    async def insert_optimizer_log(self, stats: dict, changes: dict | None, ai_response: str) -> None:
        await self.db.execute(
            "INSERT INTO optimizer_log (timestamp, stats_json, changes_json, ai_response) VALUES (?, ?, ?, ?)",
            (time.time(), orjson.dumps(stats).decode(), orjson.dumps(changes).decode() if changes else None, ai_response),
        )
        await self.db.commit()

    # ── Token Stats ──────────────────────────────────────────

    async def update_token_stats(self, token: str, total_trades: int, win_rate: float, avg_pnl: float) -> None:
        await self.db.execute(
            """INSERT INTO token_stats (token, total_trades, win_rate, avg_pnl, last_traded)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(token) DO UPDATE SET
                 total_trades = excluded.total_trades,
                 win_rate = excluded.win_rate,
                 avg_pnl = excluded.avg_pnl,
                 last_traded = excluded.last_traded""",
            (token, total_trades, win_rate, avg_pnl, time.time()),
        )
        await self.db.commit()

    # ── API Costs ────────────────────────────────────────────

    async def log_api_cost(self, service: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        await self.db.execute(
            "INSERT INTO api_costs (timestamp, service, model, input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), service, model, input_tokens, output_tokens, cost_usd),
        )
        await self.db.commit()

    async def get_today_api_costs(self) -> float:
        day_start = time.time() - (time.time() % 86400)
        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM api_costs WHERE timestamp >= ?",
            (day_start,),
        )
        row = await cursor.fetchone()
        return row["total"]  # type: ignore

    async def get_today_trading_fees(self) -> float:
        day_start = time.time() - (time.time() % 86400)
        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(fees), 0) as total FROM trades WHERE exit_time >= ? AND status = 'closed'",
            (day_start,),
        )
        row = await cursor.fetchone()
        return row["total"]  # type: ignore

    # ── Funding Rates ────────────────────────────────────────

    async def insert_funding_rate(self, token: str, rate: float, annualized_pct: float) -> None:
        await self.db.execute(
            "INSERT INTO funding_rates (timestamp, token, rate, annualized_pct) VALUES (?, ?, ?, ?)",
            (time.time(), token, rate, annualized_pct),
        )
        await self.db.commit()

    async def insert_funding_rates_batch(self, rates: list[tuple]) -> None:
        await self.db.executemany(
            "INSERT INTO funding_rates (timestamp, token, rate, annualized_pct) VALUES (?, ?, ?, ?)",
            rates,
        )
        await self.db.commit()

    async def get_latest_funding_rate(self, token: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM funding_rates WHERE token = ? ORDER BY timestamp DESC LIMIT 1",
            (token,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ── Cleanup ──────────────────────────────────────────────

    async def cleanup_old_data(self) -> None:
        now = time.time()
        tick_cutoff = now - 48 * 3600      # 48h
        candle_cutoff = now - 7 * 86400    # 7 days
        funding_cutoff = now - 7 * 86400  # 7 days
        await self.db.execute("DELETE FROM price_ticks WHERE timestamp < ?", (tick_cutoff,))
        await self.db.execute("DELETE FROM candles WHERE timeframe = '1m' AND timestamp < ?", (candle_cutoff,))
        await self.db.execute("DELETE FROM funding_rates WHERE timestamp < ?", (funding_cutoff,))
        await self.db.commit()
        logger.debug("Cleaned up old price ticks, 1m candles, and funding rates")
