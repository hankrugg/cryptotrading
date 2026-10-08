"""Local paper-trade execution for strategy target signals."""

from dataclasses import dataclass
from datetime import datetime
import math
import logging
import os
from pathlib import Path
import sqlite3
import uuid

from crypto_trader.data import Trade, create_trades_table, upsert_trade

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "database" / "trading.db"


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """The unmatched entry row representing the current paper position."""

    trade: Trade
    side: str


def _display_time(value: datetime) -> str:
    """Format a candle timestamp like TradingView's CSV export."""
    return value.strftime("%b %-d, %Y, %H:%M")


class PaperTradeExecutor:
    """Turn target-direction changes into local SQLite paper trades.

    This deliberately does not submit orders to an exchange or TradingView.
    It records simulated fills at the strategy's latest candle close.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        initial_equity_usd: float = 100_000.0,
        quantity: float | None = None,
    ):
        if initial_equity_usd <= 0:
            raise ValueError("initial_equity_usd must be positive")
        if quantity is not None and quantity <= 0:
            raise ValueError("quantity must be positive when provided")
        self.connection = connection
        self.initial_equity_usd = initial_equity_usd
        self.fixed_quantity = quantity
        self._weight_synchronized: set[str] = set()
        create_trades_table(connection)
        connection.commit()

    @classmethod
    def open(
        cls,
        path: Path = DEFAULT_DATABASE_PATH,
        *,
        initial_equity_usd: float | None = None,
        quantity: float | None = None,
    ) -> "PaperTradeExecutor":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        if initial_equity_usd is None:
            initial_equity_usd = float(
                os.getenv("PAPER_INITIAL_EQUITY_USD", "100000")
            )
        return cls(
            connection,
            initial_equity_usd=initial_equity_usd,
            quantity=quantity,
        )

    def close(self) -> None:
        self.connection.close()

    def apply_signal(
        self,
        *,
        symbol: str,
        signal: float,
        price: float,
        executed_at: datetime,
    ) -> list[Trade]:
        """Apply a target signal and return rows created or updated.

        Positive signals target long, negative signals target short, and zero
        closes an open position. Repeating a signal in the same direction does
        not create another trade.
        """
        if price <= 0:
            raise ValueError("price must be positive")
        if not executed_at.tzinfo:
            logger.debug("executed_at has no timezone; preserving its value")

        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol cannot be empty")
        target_side = "long" if signal > 0 else "short" if signal < 0 else None
        current = self._current_position(normalized_symbol)
        target_quantity = (
            0.0
            if target_side is None
            else self._target_quantity(current, signal=signal, price=price)
        )
        same_target = False
        if current is not None and current.side == target_side:
            try:
                same_target = math.isclose(
                    float(current.trade.signal),
                    signal,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                )
            except ValueError:
                same_target = False
        needs_initial_sync = (
            same_target
            and normalized_symbol not in self._weight_synchronized
            and not math.isclose(
                current.trade.size_qty if current is not None else 0.0,
                target_quantity,
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
        )
        if same_target and not needs_initial_sync:
            self._weight_synchronized.add(normalized_symbol)
            return []

        created: list[Trade] = []
        if current is not None:
            created.extend(
                self._close_position(
                    current,
                    symbol=normalized_symbol,
                    price=price,
                    signal=signal,
                    executed_at=executed_at,
                )
            )
        if target_side is not None:
            entry = self._create_entry(
                symbol=normalized_symbol,
                side=target_side,
                signal=signal,
                price=price,
                quantity=target_quantity,
                executed_at=executed_at,
            )
            with self.connection:
                upsert_trade(self.connection, entry)
            created.append(entry)
        self._weight_synchronized.add(normalized_symbol)
        return created

    def _current_position(self, symbol: str) -> OpenPosition | None:
        row = self.connection.execute(
            """
            SELECT symbol, trade_number, trade_type, executed_at, order_id, signal,
                   price, size_qty, size_value, net_pnl_usd, return_pct,
                   commission_usd, cumulative_pnl_usd, cumulative_pnl_pct,
                   imported_at_ms, id
            FROM trades AS entry
            WHERE entry.symbol = ?
              AND entry.trade_type IN ('Entry long', 'Entry short')
              AND NOT EXISTS (
                  SELECT 1 FROM trades AS exit
                  WHERE exit.symbol = entry.symbol
                    AND exit.trade_number = entry.trade_number
                    AND exit.trade_type IN ('Exit long', 'Exit short')
              )
            ORDER BY entry.trade_number DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        trade = Trade(
            symbol=row[0],
            trade_number=row[1],
            trade_type=row[2],
            executed_at=row[3],
            order_id=row[4],
            signal=row[5],
            price=row[6],
            size_qty=row[7],
            size_value=row[8],
            net_pnl_usd=row[9],
            return_pct=row[10],
            commission_usd=row[11],
            cumulative_pnl_usd=row[12],
            cumulative_pnl_pct=row[13],
            imported_at_ms=row[14],
            id=row[15],
        )
        return OpenPosition(trade=trade, side="long" if trade.trade_type.endswith("long") else "short")

    def _next_trade_number(self) -> int:
        row = self.connection.execute("SELECT COALESCE(MAX(trade_number), 0) + 1 FROM trades").fetchone()
        return int(row[0])

    def _cumulative_pnl(self) -> float:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(net_pnl_usd), 0) FROM trades WHERE trade_type LIKE 'Exit %'"
        ).fetchone()
        return float(row[0])

    def _target_quantity(
        self,
        current: OpenPosition | None,
        *,
        signal: float,
        price: float,
    ) -> float:
        if self.fixed_quantity is not None:
            return self.fixed_quantity
        equity = self.initial_equity_usd + self._cumulative_pnl()
        if current is not None:
            direction = 1 if current.side == "long" else -1
            equity += direction * (price - current.trade.price) * current.trade.size_qty
        return abs(signal) * equity / price

    def _create_entry(
        self,
        *,
        symbol: str,
        side: str,
        signal: float,
        price: float,
        quantity: float,
        executed_at: datetime,
    ) -> Trade:
        trade_number = self._next_trade_number()
        cumulative = self._cumulative_pnl()
        return Trade(
            symbol=symbol,
            trade_number=trade_number,
            trade_type=f"Entry {side}",
            executed_at=_display_time(executed_at),
            order_id=f"paper-{uuid.uuid4().hex}",
            signal=str(signal),
            price=price,
            size_qty=quantity,
            size_value=price * quantity,
            net_pnl_usd=0.0,
            return_pct=0.0,
            commission_usd=0.0,
            cumulative_pnl_usd=cumulative,
            cumulative_pnl_pct=0.0,
        )

    def _close_position(
        self,
        position: OpenPosition,
        *,
        symbol: str,
        price: float,
        signal: float,
        executed_at: datetime,
    ) -> list[Trade]:
        entry = position.trade
        pnl = (
            (price - entry.price) * entry.size_qty
            if position.side == "long"
            else (entry.price - price) * entry.size_qty
        )
        return_pct = pnl / entry.size_value * 100 if entry.size_value else 0.0
        cumulative = self._cumulative_pnl() + pnl
        exit_trade = Trade(
            symbol=symbol,
            trade_number=entry.trade_number,
            trade_type=f"Exit {position.side}",
            executed_at=_display_time(executed_at),
            order_id=f"paper-{uuid.uuid4().hex}",
            signal=str(signal),
            price=price,
            size_qty=entry.size_qty,
            size_value=price * entry.size_qty,
            net_pnl_usd=pnl,
            return_pct=return_pct,
            commission_usd=0.0,
            cumulative_pnl_usd=cumulative,
            cumulative_pnl_pct=0.0,
        )
        updated_entry = Trade(
            **{
                field: getattr(entry, field)
                for field in (
                    "symbol", "trade_number", "trade_type", "executed_at", "order_id",
                    "signal", "price", "size_qty", "size_value", "commission_usd", "id",
                )
            },
            net_pnl_usd=pnl,
            return_pct=return_pct,
            cumulative_pnl_usd=cumulative,
            cumulative_pnl_pct=0.0,
            imported_at_ms=entry.imported_at_ms,
        )
        with self.connection:
            upsert_trade(self.connection, updated_entry)
            upsert_trade(self.connection, exit_trade)
        return [updated_entry, exit_trade]
