"""Load normalized Coinbase hourly candles from the project SQLite database."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
import sqlite3

import pandas as pd

from crypto_trader.data.coinbase_candles import (
    DEFAULT_DATABASE_PATH,
    normalize_products,
)

HOURLY_INTERVAL_SECONDS = 3_600
HOURLY_INTERVAL_MS = HOURLY_INTERVAL_SECONDS * 1_000
DEFAULT_RESEARCH_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD")
_VALUE_COLUMNS = ("open", "high", "low", "close", "volume", "ingested_at_ms")


class HourlyDataError(RuntimeError):
    """Raised when the stored hourly dataset is missing or malformed."""


def _utc_milliseconds(value: object | None, *, name: str) -> int | None:
    """Convert a timestamp-like boundary to UTC Unix milliseconds."""

    if value is None:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a valid timestamp") from error
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return int(timestamp.timestamp() * 1_000)


@dataclass(slots=True)
class CoinbaseHourlyData:
    """Validated long-form hourly candles with convenient research views.

    ``candles`` has a UTC ``time``/``product`` MultiIndex and lowercase OHLCV
    columns. Use :meth:`product` for one asset or :meth:`close_prices` for an
    aligned multi-asset matrix suitable for the backtest engine.
    """

    candles: pd.DataFrame
    products: tuple[str, ...]

    def product(self, product_id: str) -> pd.DataFrame:
        """Return one product's candles indexed by UTC opening time."""

        product = normalize_products((product_id,))[0]
        if product not in self.products:
            raise KeyError(f"{product} is not loaded; choose from {self.products}")
        return self.candles.xs(product, level="product").copy()

    def close_prices(self, *, complete_index: bool = True) -> pd.DataFrame:
        """Return hourly close prices with one column per requested product.

        With ``complete_index=True`` (the default), missing source hours remain
        explicit ``NaN`` rows. This prevents a two-hour move across a source
        outage from being mistaken for a normal one-hour return.
        """

        closes = self.candles["close"].unstack("product")
        closes = closes.reindex(columns=self.products).sort_index()
        if complete_index and not closes.empty:
            full_index = pd.date_range(
                closes.index.min(),
                closes.index.max(),
                freq="1h",
                tz="UTC",
                name="time",
            )
            closes = closes.reindex(full_index)
        return closes

    def coverage(self) -> pd.DataFrame:
        """Summarize rows, UTC boundaries, and missing source hours by product."""

        rows: list[dict[str, object]] = []
        for product in self.products:
            frame = self.product(product)
            first = frame.index.min()
            last = frame.index.max()
            expected = int((last - first) / pd.Timedelta(hours=1)) + 1
            rows.append(
                {
                    "product": product,
                    "rows": len(frame),
                    "first_open": first,
                    "last_open": last,
                    "missing_hours": expected - len(frame),
                }
            )
        return pd.DataFrame(rows).set_index("product")


def load_coinbase_hourly(
    database: sqlite3.Connection | str | PathLike[str] = DEFAULT_DATABASE_PATH,
    *,
    products: Sequence[str] = DEFAULT_RESEARCH_PRODUCTS,
    start: object | None = None,
    end: object | None = None,
) -> CoinbaseHourlyData:
    """Load Coinbase hourly candles from SQLite without modifying the database.

    ``start`` and ``end`` are inclusive. Naive timestamp values are interpreted
    as UTC; timezone-aware values are converted to UTC. Every requested product
    must have at least one row in the selected window.
    """

    normalized_products = normalize_products(products)
    start_ms = _utc_milliseconds(start, name="start")
    end_ms = _utc_milliseconds(end, name="end")
    if start_ms is not None and end_ms is not None and end_ms < start_ms:
        raise ValueError("end cannot be before start")

    owns_connection = not isinstance(database, sqlite3.Connection)
    if owns_connection:
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Coinbase candle database not found: {path}")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    else:
        connection = database

    placeholders = ", ".join("?" for _ in normalized_products)
    conditions = [
        "exchange = ?",
        "interval_seconds = ?",
        f"symbol IN ({placeholders})",
    ]
    parameters: list[object] = [
        "coinbase",
        HOURLY_INTERVAL_SECONDS,
        *normalized_products,
    ]
    if start_ms is not None:
        conditions.append("open_time_ms >= ?")
        parameters.append(start_ms)
    if end_ms is not None:
        conditions.append("open_time_ms <= ?")
        parameters.append(end_ms)

    query = f"""
        SELECT symbol AS product, open_time_ms, open, high, low, close, volume,
               ingested_at_ms
        FROM candles
        WHERE {" AND ".join(conditions)}
        ORDER BY open_time_ms, symbol
    """
    try:
        frame = pd.read_sql_query(query, connection, params=parameters)
    except (pd.errors.DatabaseError, sqlite3.Error) as error:
        raise HourlyDataError(
            f"Could not load Coinbase hourly candles: {error}"
        ) from error
    finally:
        if owns_connection:
            connection.close()

    if frame.empty:
        raise HourlyDataError("No Coinbase hourly candles matched the request")
    present = set(frame["product"])
    missing_products = [
        product for product in normalized_products if product not in present
    ]
    if missing_products:
        raise HourlyDataError(
            f"No hourly candles found for: {', '.join(missing_products)}"
        )
    if (frame["open_time_ms"] % HOURLY_INTERVAL_MS != 0).any():
        raise HourlyDataError("Stored Coinbase hourly candles are not UTC-hour aligned")
    if frame.duplicated(subset=["product", "open_time_ms"]).any():
        raise HourlyDataError("Stored Coinbase hourly candles contain duplicates")

    frame["time"] = pd.to_datetime(frame.pop("open_time_ms"), unit="ms", utc=True)
    frame = frame.set_index(["time", "product"])[list(_VALUE_COLUMNS)].sort_index()
    return CoinbaseHourlyData(candles=frame, products=normalized_products)
