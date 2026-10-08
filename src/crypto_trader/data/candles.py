"""SQLite model for normalized exchange candles.

The Binance collector turns each completed one-minute API row into a
``Candle`` and upserts it into this table. Times are UTC Unix milliseconds so
the database does not depend on the Raspberry Pi's local timezone.
"""

from dataclasses import dataclass, field
import math
import sqlite3
import time


CANDLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    -- Internal row identifier. The exchange/time columns below identify a
    -- market candle; this ID is only a convenient SQLite primary key.
    id INTEGER PRIMARY KEY,
    -- Normalized venue and symbol names make queries consistent.
    exchange TEXT NOT NULL
        CHECK (length(trim(exchange)) > 0 AND exchange = lower(exchange)),
    symbol TEXT NOT NULL
        CHECK (length(trim(symbol)) > 0 AND symbol = upper(symbol)),
    -- interval_seconds=60 identifies the one-minute collector data.
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    -- The candle's opening time, expressed as UTC Unix milliseconds.
    open_time_ms INTEGER NOT NULL CHECK (open_time_ms >= 0),
    -- Standard OHLCV market data.
    open REAL NOT NULL CHECK (open > 0),
    high REAL NOT NULL CHECK (high > 0),
    low REAL NOT NULL CHECK (low > 0),
    close REAL NOT NULL CHECK (close > 0),
    volume REAL NOT NULL CHECK (volume >= 0),
    -- Time when this row was written locally, not when the candle occurred.
    ingested_at_ms INTEGER NOT NULL CHECK (ingested_at_ms >= 0),
    -- These checks reject impossible candles before they enter the database.
    CHECK (high >= low),
    CHECK (high >= open AND high >= close),
    CHECK (low <= open AND low <= close),
    -- Re-fetching a candle updates it instead of creating a duplicate row.
    UNIQUE (exchange, symbol, interval_seconds, open_time_ms)
) STRICT;
"""

CANDLES_TIME_INDEX = """
CREATE INDEX IF NOT EXISTS idx_candles_interval_open_time
ON candles (interval_seconds, open_time_ms);
"""


@dataclass(frozen=True, slots=True)
class Candle:
    """A completed OHLCV candle normalized to UTC Unix milliseconds.

    The dataclass repeats the database checks so malformed API data is rejected
    before an INSERT is attempted.
    """

    exchange: str
    symbol: str
    interval_seconds: int
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    ingested_at_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)
    id: int | None = None

    def __post_init__(self) -> None:
        # Normalize names once at the boundary: exchange is lowercase and
        # symbols are uppercase everywhere after construction.
        exchange = self.exchange.strip().lower()
        symbol = self.symbol.strip().upper()
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "symbol", symbol)

        # Validate identifiers and timestamps first.
        if not exchange:
            raise ValueError("exchange cannot be empty")
        if not symbol:
            raise ValueError("symbol cannot be empty")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.open_time_ms < 0:
            raise ValueError("open_time_ms cannot be negative")
        if self.ingested_at_ms < 0:
            raise ValueError("ingested_at_ms cannot be negative")

        # Reject NaN/infinite prices because they make later PnL and risk math
        # meaningless, then enforce the basic OHLC ordering rules.
        prices = (self.open, self.high, self.low, self.close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            raise ValueError("OHLC prices must be finite and positive")
        if not math.isfinite(self.volume) or self.volume < 0:
            raise ValueError("volume must be finite and non-negative")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be at least the open, close, and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be at most the open, close, and high")


def create_candles_table(connection: sqlite3.Connection) -> None:
    """Create the candles table and its cross-symbol time index."""
    # IF NOT EXISTS makes this safe to call from every collector startup.
    connection.execute(CANDLES_SCHEMA)
    # This index supports queries that ask for the newest candle across symbols
    # or intervals, which the risk monitor does frequently.
    connection.execute(CANDLES_TIME_INDEX)


def upsert_candle(connection: sqlite3.Connection, candle: Candle) -> int:
    """Insert a candle or refresh the same venue candle after a backfill."""
    # The composite UNIQUE key is the conflict target. If Binance revises a
    # candle during a backfill, only its OHLCV values and ingest time change.
    connection.execute(
        """
        INSERT INTO candles (
            exchange,
            symbol,
            interval_seconds,
            open_time_ms,
            open,
            high,
            low,
            close,
            volume,
            ingested_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (exchange, symbol, interval_seconds, open_time_ms)
        DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            ingested_at_ms = excluded.ingested_at_ms
        """,
        (
            candle.exchange,
            candle.symbol,
            candle.interval_seconds,
            candle.open_time_ms,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            candle.ingested_at_ms,
        ),
    )
    # SQLite's INSERT statement above does not return the ID on all supported
    # versions, so read it back using the same natural key.
    row = connection.execute(
        """
        SELECT id
        FROM candles
        WHERE exchange = ?
          AND symbol = ?
          AND interval_seconds = ?
          AND open_time_ms = ?
        """,
        (
            candle.exchange,
            candle.symbol,
            candle.interval_seconds,
            candle.open_time_ms,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("candle upsert succeeded without returning a stored row")
    return int(row[0])
