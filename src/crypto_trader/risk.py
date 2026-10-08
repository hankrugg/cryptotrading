"""Minute-by-minute portfolio risk metrics for the paper-trading account.

The monitor reads open positions from ``trades``, marks them with the newest
one-minute ``candles``, calculates the requested policy metrics, and stores one
``risk_snapshots`` row per minute. It reports violations; it does not itself
hedge or close a position.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import argparse
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Iterable

from crypto_trader.data import create_candles_table, create_trades_table

logger = logging.getLogger(__name__)

# These are the instruments treated as the approved liquid crypto universe.
LIQUID_CRYPTO_BASES = frozenset({"BTC", "ETH", "SOL", "HYPE", "DOGE"})
# Policy thresholds. The comparisons below use >=, so exactly 2x/5x/20%/10%
# or 25% is considered a violation.
MAX_SPOT_LEVERAGE = 2.0
MAX_FUTURES_LEVERAGE = 5.0
MAX_OTHER_EXPOSURE = 0.20
MAX_DELTA_EXPOSURE = 2.0
MAX_DAILY_DRAWDOWN = 0.10
MAX_DRAWDOWN = 0.25
DAY_MS = 86_400_000
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "database" / "trading.db"


def base_symbol(symbol: str) -> str:
    """Normalize common TradingView/exchange symbols to an asset base."""
    # Strip a venue prefix (COINBASE:) and quote suffixes so SOLUSD, SOLUSDT,
    # and COINBASE:SOLUSD all compare as the base asset SOL.
    value = symbol.strip().upper().split(":")[-1].replace("-", "")
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if value.endswith(quote) and len(value) > len(quote):
            return value[: -len(quote)]
    return value


@dataclass(frozen=True, slots=True)
class RiskPosition:
    """A marked open position used for risk calculations."""

    symbol: str
    side: str
    quantity: float
    entry_price: float
    current_price: float
    market_type: str = "spot"
    leverage: float = 1.0
    delta: float = 1.0
    asset_class: str = "crypto"
    tradable: bool = True
    can_exit_within_day: bool = True
    mark_available: bool = True

    @property
    def base(self) -> str:
        # All liquid/other exposure tests operate on the normalized base asset.
        return base_symbol(self.symbol)

    @property
    def notional_usd(self) -> float:
        # Notional ignores direction because gross exposure counts both long
        # and short positions as positive size.
        return abs(self.quantity * self.current_price)

    @property
    def signed_delta_usd(self) -> float:
        # Delta preserves direction so offsetting longs and shorts can net out.
        direction = 1 if self.side.lower() == "long" else -1
        return direction * self.quantity * self.current_price * self.delta

    @property
    def unrealized_pnl_usd(self) -> float:
        # Longs gain when price rises; shorts gain when price falls.
        direction = 1 if self.side.lower() == "long" else -1
        return direction * (self.current_price - self.entry_price) * self.quantity


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """One persisted portfolio risk measurement."""

    captured_at_ms: int
    equity_usd: float
    gross_exposure_usd: float
    delta_exposure_usd: float
    daily_pnl_usd: float
    daily_drawdown_pct: float
    max_drawdown_pct: float
    liquid_exposure_pct: float
    other_exposure_pct: float
    max_spot_leverage: float
    max_futures_leverage: float
    status: str
    violations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def can_open_new_positions(self) -> bool:
        # This is a reporting decision only; the current executor does not call
        # this property to block a strategy trade automatically.
        return self.status == "ok"


def evaluate_risk(
    positions: Iterable[RiskPosition],
    *,
    equity_usd: float,
    day_start_equity_usd: float,
    peak_equity_usd: float,
    captured_at_ms: int | None = None,
) -> RiskSnapshot:
    """Calculate policy metrics from currently marked positions."""
    # Equity anchors all percentage limits. Refusing nonpositive anchors avoids
    # division by zero and nonsensical drawdown percentages.
    if equity_usd <= 0 or day_start_equity_usd <= 0 or peak_equity_usd <= 0:
        raise ValueError("equity values must be positive")

    captured_at_ms = (
        time.time_ns() // 1_000_000 if captured_at_ms is None else captured_at_ms
    )
    positions = tuple(positions)
    # Compute the core measurements once, then evaluate each policy rule.
    gross = sum(position.notional_usd for position in positions)
    liquid = sum(
        position.notional_usd
        for position in positions
        if position.base in LIQUID_CRYPTO_BASES
    )
    other = gross - liquid
    delta = abs(sum(position.signed_delta_usd for position in positions))
    daily_pnl = equity_usd - day_start_equity_usd
    daily_drawdown = max(0.0, -daily_pnl / day_start_equity_usd)
    max_drawdown = max(0.0, (peak_equity_usd - equity_usd) / peak_equity_usd)
    max_spot = max(
        (position.leverage for position in positions if position.market_type == "spot"),
        default=0.0,
    )
    max_futures = max(
        (
            position.leverage
            for position in positions
            if position.market_type == "futures"
        ),
        default=0.0,
    )

    # Keep machine-readable violation names so emails/logs can list exactly
    # which rule caused a non-ok status.
    violations: list[str] = []
    if any(position.asset_class.lower() != "crypto" for position in positions):
        violations.append("non_crypto_instrument")
    if any(not position.tradable or not position.can_exit_within_day for position in positions):
        violations.append("instrument_not_exit_within_one_day")
    if any(
        position.market_type == "spot" and position.leverage >= MAX_SPOT_LEVERAGE
        for position in positions
    ):
        violations.append("spot_leverage_at_or_above_2x")
    if any(
        position.market_type == "futures" and position.leverage >= MAX_FUTURES_LEVERAGE
        for position in positions
    ):
        violations.append("futures_leverage_at_or_above_5x")
    if other / equity_usd >= MAX_OTHER_EXPOSURE:
        violations.append("other_instrument_exposure_at_or_above_20_percent")
    if equity_usd and delta / equity_usd >= MAX_DELTA_EXPOSURE:
        violations.append("delta_exposure_at_or_above_2x")
    if any(not position.mark_available for position in positions):
        violations.append("missing_minute_mark")
    if daily_drawdown >= MAX_DAILY_DRAWDOWN:
        violations.append("daily_drawdown_at_or_above_10_percent")
    if max_drawdown >= MAX_DRAWDOWN:
        violations.append("max_drawdown_at_or_above_25_percent")

    # Maximum drawdown takes precedence over the ordinary hedge/exit status.
    status = "halt_until_month_end" if max_drawdown >= MAX_DRAWDOWN else (
        "hedge_or_exit" if violations else "ok"
    )
    return RiskSnapshot(
        captured_at_ms=captured_at_ms,
        equity_usd=equity_usd,
        gross_exposure_usd=gross,
        delta_exposure_usd=delta,
        daily_pnl_usd=daily_pnl,
        daily_drawdown_pct=daily_drawdown,
        max_drawdown_pct=max_drawdown,
        liquid_exposure_pct=liquid / equity_usd,
        other_exposure_pct=other / equity_usd,
        max_spot_leverage=max_spot,
        max_futures_leverage=max_futures,
        status=status,
        violations=tuple(violations),
    )


RISK_SNAPSHOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_snapshots (
    -- Internal SQLite row identifier.
    id INTEGER PRIMARY KEY,
    -- A snapshot is identified by the UTC capture millisecond.
    captured_at_ms INTEGER NOT NULL UNIQUE CHECK (captured_at_ms >= 0),
    -- Account and exposure measurements at that instant.
    equity_usd REAL NOT NULL CHECK (equity_usd > 0),
    gross_exposure_usd REAL NOT NULL CHECK (gross_exposure_usd >= 0),
    delta_exposure_usd REAL NOT NULL CHECK (delta_exposure_usd >= 0),
    daily_pnl_usd REAL NOT NULL,
    daily_drawdown_pct REAL NOT NULL CHECK (daily_drawdown_pct >= 0),
    max_drawdown_pct REAL NOT NULL CHECK (max_drawdown_pct >= 0),
    liquid_exposure_pct REAL NOT NULL CHECK (liquid_exposure_pct >= 0),
    other_exposure_pct REAL NOT NULL CHECK (other_exposure_pct >= 0),
    max_spot_leverage REAL NOT NULL CHECK (max_spot_leverage >= 0),
    max_futures_leverage REAL NOT NULL CHECK (max_futures_leverage >= 0),
    -- Human-readable policy result plus a JSON list of individual violations.
    status TEXT NOT NULL CHECK (status IN ('ok', 'hedge_or_exit', 'halt_until_month_end')),
    violations_json TEXT NOT NULL
) STRICT;
"""


def create_risk_snapshots_table(connection: sqlite3.Connection) -> None:
    # Both the hourly email path and the minute monitor can safely call this.
    connection.execute(RISK_SNAPSHOTS_SCHEMA)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_risk_snapshots_captured_at "
        "ON risk_snapshots (captured_at_ms)"
    )


def record_risk_snapshot(
    connection: sqlite3.Connection,
    snapshot: RiskSnapshot,
) -> int:
    """Insert or refresh a snapshot at the same timestamp."""
    # Re-running a snapshot with the same capture time updates its measurements
    # instead of creating a duplicate minute.
    create_risk_snapshots_table(connection)
    connection.execute(
        """
        INSERT INTO risk_snapshots (
            captured_at_ms, equity_usd, gross_exposure_usd, delta_exposure_usd,
            daily_pnl_usd, daily_drawdown_pct, max_drawdown_pct,
            liquid_exposure_pct, other_exposure_pct, max_spot_leverage,
            max_futures_leverage, status, violations_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (captured_at_ms) DO UPDATE SET
            equity_usd = excluded.equity_usd,
            gross_exposure_usd = excluded.gross_exposure_usd,
            delta_exposure_usd = excluded.delta_exposure_usd,
            daily_pnl_usd = excluded.daily_pnl_usd,
            daily_drawdown_pct = excluded.daily_drawdown_pct,
            max_drawdown_pct = excluded.max_drawdown_pct,
            liquid_exposure_pct = excluded.liquid_exposure_pct,
            other_exposure_pct = excluded.other_exposure_pct,
            max_spot_leverage = excluded.max_spot_leverage,
            max_futures_leverage = excluded.max_futures_leverage,
            status = excluded.status,
            violations_json = excluded.violations_json
        """,
        (
            snapshot.captured_at_ms,
            snapshot.equity_usd,
            snapshot.gross_exposure_usd,
            snapshot.delta_exposure_usd,
            snapshot.daily_pnl_usd,
            snapshot.daily_drawdown_pct,
            snapshot.max_drawdown_pct,
            snapshot.liquid_exposure_pct,
            snapshot.other_exposure_pct,
            snapshot.max_spot_leverage,
            snapshot.max_futures_leverage,
            snapshot.status,
            json.dumps(snapshot.violations),
        ),
    )
    # Unlike the trade helper, this function explicitly commits because the
    # risk service may be the only writer in a given minute.
    connection.commit()
    row = connection.execute(
        "SELECT id FROM risk_snapshots WHERE captured_at_ms = ?",
        (snapshot.captured_at_ms,),
    ).fetchone()
    if row is None:
        raise RuntimeError("risk snapshot was not stored")
    return int(row[0])


def latest_risk_snapshot(
    connection: sqlite3.Connection,
    *,
    before_ms: int | None = None,
) -> RiskSnapshot | None:
    """Read the most recent stored snapshot, optionally before a timestamp."""
    # Emails use this to attach the newest available risk state to an hourly
    # signal, even if the two services do not run at exactly the same instant.
    create_risk_snapshots_table(connection)
    query = """
        SELECT captured_at_ms, equity_usd, gross_exposure_usd, delta_exposure_usd,
               daily_pnl_usd, daily_drawdown_pct, max_drawdown_pct,
               liquid_exposure_pct, other_exposure_pct, max_spot_leverage,
               max_futures_leverage, status, violations_json
        FROM risk_snapshots
    """
    params: tuple[object, ...] = ()
    if before_ms is not None:
        query += " WHERE captured_at_ms < ?"
        params = (before_ms,)
    query += " ORDER BY captured_at_ms DESC LIMIT 1"
    row = connection.execute(query, params).fetchone()
    if row is None:
        return None
    return RiskSnapshot(
        captured_at_ms=row[0],
        equity_usd=row[1],
        gross_exposure_usd=row[2],
        delta_exposure_usd=row[3],
        daily_pnl_usd=row[4],
        daily_drawdown_pct=row[5],
        max_drawdown_pct=row[6],
        liquid_exposure_pct=row[7],
        other_exposure_pct=row[8],
        max_spot_leverage=row[9],
        max_futures_leverage=row[10],
        status=row[11],
        violations=tuple(json.loads(row[12])),
    )


def _first_snapshot_on_or_after(
    connection: sqlite3.Connection,
    *,
    start_ms: int,
    before_ms: int,
) -> RiskSnapshot | None:
    # The first snapshot in the current UTC day establishes the baseline for
    # daily PnL/drawdown. If none exists, snapshot_once uses current equity.
    row = connection.execute(
        """
        SELECT captured_at_ms, equity_usd, gross_exposure_usd, delta_exposure_usd,
               daily_pnl_usd, daily_drawdown_pct, max_drawdown_pct,
               liquid_exposure_pct, other_exposure_pct, max_spot_leverage,
               max_futures_leverage, status, violations_json
        FROM risk_snapshots
        WHERE captured_at_ms >= ? AND captured_at_ms < ?
        ORDER BY captured_at_ms ASC LIMIT 1
        """,
        (start_ms, before_ms),
    ).fetchone()
    if row is None:
        return None
    return RiskSnapshot(
        captured_at_ms=row[0],
        equity_usd=row[1],
        gross_exposure_usd=row[2],
        delta_exposure_usd=row[3],
        daily_pnl_usd=row[4],
        daily_drawdown_pct=row[5],
        max_drawdown_pct=row[6],
        liquid_exposure_pct=row[7],
        other_exposure_pct=row[8],
        max_spot_leverage=row[9],
        max_futures_leverage=row[10],
        status=row[11],
        violations=tuple(json.loads(row[12])),
    )


def _sleep_until_next_minute(delay_seconds: float) -> None:
    # Align the monitor to UTC minute boundaries rather than drifting by the
    # amount of time each risk calculation takes.
    now = time.time()
    time.sleep(max(0.0, 60 - (now % 60) + delay_seconds))


def _open_positions(connection: sqlite3.Connection) -> list[RiskPosition]:
    """Load unmatched paper entries and use their entry price as a fallback mark."""
    # A paper position is an Entry row for which no matching Exit row exists.
    # This is intentionally derived from the trade ledger; there is no separate
    # positions table.
    create_trades_table(connection)
    rows = connection.execute(
        """
        SELECT entry.symbol, entry.trade_type, entry.size_qty, entry.price
        FROM trades AS entry
        WHERE entry.trade_type IN ('Entry long', 'Entry short')
          AND NOT EXISTS (
              SELECT 1 FROM trades AS exit
              WHERE exit.symbol = entry.symbol
                AND exit.trade_number = entry.trade_number
                AND exit.trade_type IN ('Exit long', 'Exit short')
          )
        """
    ).fetchall()
    # Entry price is the fallback mark until a matching minute candle is found.
    return [
        RiskPosition(
            symbol=row[0],
            side="long" if row[1].endswith("long") else "short",
            quantity=row[2],
            entry_price=row[3],
            current_price=row[3],
            mark_available=False,
        )
        for row in rows
    ]


def _mark_positions(
    connection: sqlite3.Connection,
    positions: list[RiskPosition],
) -> list[RiskPosition]:
    # There is nothing to mark when the account has no open positions.
    if not positions:
        return positions
    # Rows are newest-first. setdefault keeps the first close for each base,
    # which is the latest available mark for that asset.
    rows = connection.execute(
        """
        SELECT symbol, close
        FROM candles
        WHERE interval_seconds = 60
        ORDER BY open_time_ms DESC
        """
    ).fetchall()
    latest_by_base: dict[str, float] = {}
    for symbol, close in rows:
        latest_by_base.setdefault(base_symbol(symbol), float(close))
    return [
        replace(
            position,
            current_price=latest_by_base.get(position.base, position.current_price),
            mark_available=position.base in latest_by_base,
        )
        for position in positions
    ]


def _realized_pnl(connection: sqlite3.Connection) -> float:
    # Only exit rows realize PnL. Open positions contribute unrealized PnL in
    # snapshot_once after they are marked.
    row = connection.execute(
        "SELECT COALESCE(SUM(net_pnl_usd), 0) FROM trades WHERE trade_type LIKE 'Exit %'"
    ).fetchone()
    return float(row[0])


def snapshot_once(
    connection: sqlite3.Connection,
    *,
    initial_equity_usd: float,
    captured_at_ms: int | None = None,
) -> RiskSnapshot:
    """Mark current paper positions, evaluate policy, and persist one snapshot."""
    # captured_at_ms is the observation time, not the trade candle time.
    if initial_equity_usd <= 0:
        raise ValueError("initial_equity_usd must be positive")
    captured_at_ms = (
        time.time_ns() // 1_000_000 if captured_at_ms is None else captured_at_ms
    )
    # Ensure both source tables exist before querying them on a fresh database.
    create_candles_table(connection)
    positions = _mark_positions(connection, _open_positions(connection))
    # Equity is starting cash plus realized PnL plus current unrealized PnL.
    equity = initial_equity_usd + _realized_pnl(connection)
    equity += sum(position.unrealized_pnl_usd for position in positions)

    # Unix-day boundaries are UTC. The first saved snapshot in that day is the
    # baseline for the daily drawdown calculation.
    day_start_ms = (captured_at_ms // DAY_MS) * DAY_MS
    create_risk_snapshots_table(connection)
    day_start = _first_snapshot_on_or_after(
        connection,
        start_ms=day_start_ms,
        before_ms=captured_at_ms,
    )
    day_start_equity = day_start.equity_usd if day_start else equity
    # The historical equity peak is used to calculate maximum drawdown.
    previous_peak = connection.execute(
        "SELECT MAX(equity_usd) FROM risk_snapshots WHERE captured_at_ms < ?",
        (captured_at_ms,),
    ).fetchone()[0]
    peak_equity = max(equity, float(previous_peak) if previous_peak else equity)
    snapshot = evaluate_risk(
        positions,
        equity_usd=equity,
        day_start_equity_usd=day_start_equity,
        peak_equity_usd=peak_equity,
        captured_at_ms=captured_at_ms,
    )
    record_risk_snapshot(connection, snapshot)
    return snapshot


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # The risk service supports one-shot checks for testing and continuous
    # minute polling for systemd.
    parser = argparse.ArgumentParser(
        description="Record minute-by-minute paper portfolio risk metrics."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--initial-equity",
        type=float,
        default=100_000.0,
        help="paper account starting equity in USD",
    )
    parser.add_argument("--poll-delay", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # This command has its own logging setup because it is launched separately
    # from the hourly signal process.
    args = _parse_args(argv)
    if args.initial_equity <= 0 or args.poll_delay < 0:
        raise SystemExit("initial equity must be positive and poll delay non-negative")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    args.database.parent.mkdir(parents=True, exist_ok=True)
    # WAL/busy_timeout allow the hourly executor and candle collector to share
    # this database without immediately failing on a short write lock.
    connection = sqlite3.connect(args.database, timeout=10.0)
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        while True:
            # Each loop writes one point-in-time risk measurement, then aligns
            # the next iteration to the following minute.
            snapshot = snapshot_once(
                connection,
                initial_equity_usd=args.initial_equity,
            )
            logger.info(
                "Risk status=%s equity=%.2f delta=%.2fx daily_drawdown=%.2f%% "
                "max_drawdown=%.2f%% violations=%s",
                snapshot.status,
                snapshot.equity_usd,
                snapshot.delta_exposure_usd / snapshot.equity_usd,
                snapshot.daily_drawdown_pct * 100,
                snapshot.max_drawdown_pct * 100,
                ",".join(snapshot.violations) or "none",
            )
            if args.once:
                return 0
            _sleep_until_next_minute(args.poll_delay)
    except KeyboardInterrupt:
        logger.info("Risk monitor stopped")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
