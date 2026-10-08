"""SQLite model for TradingView-shaped paper-trading rows.

The table is a durable local ledger. An entry and its later exit are separate
rows with the same ``trade_number``; the exit row carries the realized PnL and
the entry row is updated to carry the same final totals for CSV compatibility.
"""

from dataclasses import dataclass, field
import csv
from io import StringIO
import math
import sqlite3
import time
from collections.abc import Mapping


TRADINGVIEW_TRADE_HEADERS = (
    "Symbol",
    "Trade number",
    "Type",
    "Date and time",
    "Order ID",
    "Signal",
    "Price",
    "Size (qty)",
    "Size (value)",
    "Net PnL USD",
    "Return %",
    "Commission USD",
    "Cumulative PnL USD",
    "Cumulative PnL %",
)

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    -- Internal SQLite row identifier.
    id INTEGER PRIMARY KEY,
    -- These columns mirror TradingView's exported columns.
    symbol TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
    trade_number INTEGER NOT NULL CHECK (trade_number > 0),
    trade_type TEXT NOT NULL CHECK (length(trim(trade_type)) > 0),
    executed_at TEXT NOT NULL CHECK (length(trim(executed_at)) > 0),
    order_id TEXT NOT NULL CHECK (length(trim(order_id)) > 0),
    signal TEXT NOT NULL CHECK (length(trim(signal)) > 0),
    -- Numeric fields are kept as REAL so PnL and sizing can be calculated.
    price REAL NOT NULL CHECK (price > 0),
    size_qty REAL NOT NULL CHECK (size_qty > 0),
    size_value REAL NOT NULL CHECK (size_value >= 0),
    net_pnl_usd REAL NOT NULL,
    return_pct REAL NOT NULL,
    commission_usd REAL NOT NULL CHECK (commission_usd >= 0),
    cumulative_pnl_usd REAL NOT NULL,
    cumulative_pnl_pct REAL NOT NULL,
    -- The import timestamp records when this local row was written.
    imported_at_ms INTEGER NOT NULL CHECK (imported_at_ms >= 0),
    -- This is the stable identity used when importing/updating a CSV row.
    UNIQUE (symbol, trade_number, trade_type, order_id)
) STRICT;
"""


@dataclass(frozen=True, slots=True)
class Trade:
    """One entry or exit row from a TradingView paper-trading export."""

    symbol: str
    trade_number: int
    trade_type: str
    executed_at: str
    order_id: str
    signal: str
    price: float
    size_qty: float
    size_value: float
    net_pnl_usd: float
    return_pct: float
    commission_usd: float
    cumulative_pnl_usd: float
    cumulative_pnl_pct: float
    imported_at_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)
    id: int | None = None

    def __post_init__(self) -> None:
        # Strip whitespace and normalize symbols at the application boundary so
        # values coming from CSV files and strategy code behave the same way.
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "trade_type", self.trade_type.strip())
        object.__setattr__(self, "executed_at", self.executed_at.strip())
        object.__setattr__(self, "order_id", self.order_id.strip())
        object.__setattr__(self, "signal", self.signal.strip())

        # Validate required text fields and the positive trade number first.
        if not self.symbol or not self.trade_type or not self.executed_at:
            raise ValueError("symbol, trade_type, and executed_at are required")
        if not self.order_id or not self.signal:
            raise ValueError("order_id and signal are required")
        if self.trade_number <= 0:
            raise ValueError("trade_number must be positive")
        if self.imported_at_ms < 0:
            raise ValueError("imported_at_ms cannot be negative")

        # Every numeric value must be finite; otherwise a NaN could poison all
        # later cumulative PnL calculations.
        numeric_values = (
            self.price,
            self.size_qty,
            self.size_value,
            self.net_pnl_usd,
            self.return_pct,
            self.commission_usd,
            self.cumulative_pnl_usd,
            self.cumulative_pnl_pct,
        )
        if any(not math.isfinite(value) for value in numeric_values):
            raise ValueError("trade numeric values must be finite")
        if self.price <= 0 or self.size_qty <= 0:
            raise ValueError("price and size_qty must be positive")
        if self.size_value < 0 or self.commission_usd < 0:
            raise ValueError("size_value and commission_usd cannot be negative")

    @classmethod
    def from_csv_row(
        cls,
        row: Mapping[str, str],
        *,
        imported_at_ms: int | None = None,
    ) -> "Trade":
        """Build a trade from one row using TradingView's exported headers."""

        def number(header: str) -> float:
            # Keep conversion errors tied to the specific TradingView column so
            # a bad CSV row is easy to diagnose.
            try:
                return float(row[header].strip())
            except (KeyError, AttributeError, TypeError, ValueError) as error:
                raise ValueError(f"invalid {header} value") from error

        # Constructing Trade performs the remaining normalization and checks.
        try:
            trade = cls(
                symbol=row["Symbol"],
                trade_number=int(row["Trade number"]),
                trade_type=row["Type"],
                executed_at=row["Date and time"],
                order_id=row["Order ID"],
                signal=row["Signal"],
                price=number("Price"),
                size_qty=number("Size (qty)"),
                size_value=number("Size (value)"),
                net_pnl_usd=number("Net PnL USD"),
                return_pct=number("Return %"),
                commission_usd=number("Commission USD"),
                cumulative_pnl_usd=number("Cumulative PnL USD"),
                cumulative_pnl_pct=number("Cumulative PnL %"),
                **({} if imported_at_ms is None else {"imported_at_ms": imported_at_ms}),
            )
        except KeyError as error:
            raise ValueError(f"missing TradingView column: {error.args[0]}") from error
        except (TypeError, ValueError) as error:
            if isinstance(error, ValueError) and str(error).startswith("invalid "):
                raise
            raise ValueError("invalid TradingView trade row") from error
        return trade


def create_trades_table(connection: sqlite3.Connection) -> None:
    """Create the normalized paper-trade table and lookup indexes."""
    # The runner and importer both call this, so creation is intentionally
    # idempotent.
    connection.execute(TRADES_SCHEMA)
    # These indexes support the common export and open-position lookups.
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_executed_at ON trades (executed_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_trade_number ON trades (trade_number)"
    )


def upsert_trade(connection: sqlite3.Connection, trade: Trade) -> int:
    """Insert a trade or update the same exported row on re-import."""
    # Re-importing the same TradingView row updates its values rather than
    # duplicating it. A newly generated paper order ID creates a new row.
    connection.execute(
        """
        INSERT INTO trades (
            symbol, trade_number, trade_type, executed_at, order_id, signal,
            price, size_qty, size_value, net_pnl_usd, return_pct,
            commission_usd, cumulative_pnl_usd, cumulative_pnl_pct, imported_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, trade_number, trade_type, order_id)
        DO UPDATE SET
            executed_at = excluded.executed_at,
            signal = excluded.signal,
            price = excluded.price,
            size_qty = excluded.size_qty,
            size_value = excluded.size_value,
            net_pnl_usd = excluded.net_pnl_usd,
            return_pct = excluded.return_pct,
            commission_usd = excluded.commission_usd,
            cumulative_pnl_usd = excluded.cumulative_pnl_usd,
            cumulative_pnl_pct = excluded.cumulative_pnl_pct,
            imported_at_ms = excluded.imported_at_ms
        """,
        (
            trade.symbol,
            trade.trade_number,
            trade.trade_type,
            trade.executed_at,
            trade.order_id,
            trade.signal,
            trade.price,
            trade.size_qty,
            trade.size_value,
            trade.net_pnl_usd,
            trade.return_pct,
            trade.commission_usd,
            trade.cumulative_pnl_usd,
            trade.cumulative_pnl_pct,
            trade.imported_at_ms,
        ),
    )
    # Fetch the internal ID after the upsert so callers can confirm what row was
    # stored without relying on SQLite-specific RETURNING behavior.
    row = connection.execute(
        """
        SELECT id FROM trades
        WHERE symbol = ? AND trade_number = ? AND trade_type = ? AND order_id = ?
        """,
        (trade.symbol, trade.trade_number, trade.trade_type, trade.order_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("trade upsert succeeded without returning a stored row")
    return int(row[0])


def export_trades_csv(connection: sqlite3.Connection) -> str:
    """Return all stored trades in TradingView-compatible CSV column order."""
    # Ensure the table exists even when a caller asks for an export before the
    # first trade has been generated.
    create_trades_table(connection)
    # Exits are ordered before entries for each trade number, matching the
    # format in the user's TradingView export.
    rows = connection.execute(
        """
        SELECT symbol, trade_number, trade_type, executed_at, order_id, signal,
               price, size_qty, size_value, net_pnl_usd, return_pct,
               commission_usd, cumulative_pnl_usd, cumulative_pnl_pct
        FROM trades
        ORDER BY trade_number,
                 CASE WHEN trade_type LIKE 'Exit %' THEN 0 ELSE 1 END,
                 executed_at, id
        """
    ).fetchall()
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(TRADINGVIEW_TRADE_HEADERS)
    writer.writerows(rows)
    return output.getvalue()
