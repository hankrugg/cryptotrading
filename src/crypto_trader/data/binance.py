"""Collect completed one-minute candles from Binance public market data."""

import argparse
from collections.abc import Sequence
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from crypto_trader.data.candles import Candle, create_candles_table, upsert_candle

logger = logging.getLogger(__name__)

BINANCE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
BINANCE_MARKET_DATA_URL = "https://data-api.binance.vision"
INTERVAL_SECONDS = 60
INTERVAL_MS = INTERVAL_SECONDS * 1_000
MAX_KLINES_PER_REQUEST = 1_000
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "database" / "trading.db"


class BinanceMarketDataError(RuntimeError):
    """Raised when Binance market data cannot be fetched or decoded."""


def _request_json(
    path: str,
    *,
    params: dict[str, object] | None = None,
    base_url: str = BINANCE_MARKET_DATA_URL,
    timeout: float = 10.0,
) -> Any:
    """Request one public Binance endpoint and decode its JSON response."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    query = f"?{urlencode(params)}" if params else ""
    url = f"{base_url.rstrip('/')}{path}{query}"
    request = Request(url, headers={"User-Agent": "crypto-trader-school/0.1"})

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as error:
        try:
            detail = error.read().decode("utf-8", errors="replace")[:500]
        except OSError:
            detail = ""
        message = detail or str(error.reason)
        raise BinanceMarketDataError(
            f"Binance returned HTTP {error.code}: {message}"
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise BinanceMarketDataError(f"Binance request failed: {error}") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BinanceMarketDataError("Binance returned invalid JSON") from error

    if isinstance(payload, dict) and "code" in payload:
        code = payload.get("code", "unknown")
        message = payload.get("msg", "unknown Binance API error")
        raise BinanceMarketDataError(f"Binance API error ({code}): {message}")
    return payload


def fetch_binance_server_time_ms(
    *,
    base_url: str = BINANCE_MARKET_DATA_URL,
    timeout: float = 10.0,
) -> int:
    """Return Binance server time in UTC Unix milliseconds."""
    payload = _request_json("/api/v3/time", base_url=base_url, timeout=timeout)
    if not isinstance(payload, dict) or isinstance(payload.get("serverTime"), bool):
        raise BinanceMarketDataError("Binance returned an invalid server-time response")
    try:
        server_time_ms = int(payload["serverTime"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise BinanceMarketDataError(
            "Binance returned an invalid server-time response"
        ) from error
    if server_time_ms < 0:
        raise BinanceMarketDataError("Binance server time cannot be negative")
    return server_time_ms


def fetch_completed_minute_candles(
    symbol: str,
    *,
    start_time_ms: int | None = None,
    limit: int = MAX_KLINES_PER_REQUEST,
    base_url: str = BINANCE_MARKET_DATA_URL,
    exchange: str = "binance",
    server_time_ms: int | None = None,
    timeout: float = 10.0,
) -> list[Candle]:
    """Fetch completed one-minute klines for one Binance symbol."""
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol cannot be empty")
    if start_time_ms is not None and start_time_ms < 0:
        raise ValueError("start_time_ms cannot be negative")
    if not 1 <= limit <= MAX_KLINES_PER_REQUEST:
        raise ValueError(f"limit must be between 1 and {MAX_KLINES_PER_REQUEST}")

    observed_at_ms = (
        server_time_ms
        if server_time_ms is not None
        else fetch_binance_server_time_ms(base_url=base_url, timeout=timeout)
    )
    if observed_at_ms < 0:
        raise ValueError("server_time_ms cannot be negative")

    # Binance identifies klines by opening time. Ending at the prior minute keeps
    # the still-forming candle out of the normal response; the row filter below
    # is an additional guard.
    last_complete_close_ms = (observed_at_ms // INTERVAL_MS) * INTERVAL_MS - 1
    params: dict[str, object] = {
        "symbol": normalized_symbol,
        "interval": "1m",
        "endTime": last_complete_close_ms,
        "limit": limit,
    }
    if start_time_ms is not None:
        params["startTime"] = start_time_ms

    payload = _request_json(
        "/api/v3/klines",
        params=params,
        base_url=base_url,
        timeout=timeout,
    )
    if not isinstance(payload, list):
        raise BinanceMarketDataError(
            f"Binance returned an unexpected kline response for {normalized_symbol}"
        )

    candles: list[Candle] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 7:
            raise BinanceMarketDataError(
                f"Binance returned a malformed kline for {normalized_symbol}"
            )
        try:
            close_time_ms = int(row[6])
            if close_time_ms > last_complete_close_ms:
                continue
            candle = Candle(
                exchange=exchange,
                symbol=normalized_symbol,
                interval_seconds=INTERVAL_SECONDS,
                open_time_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                ingested_at_ms=observed_at_ms,
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise BinanceMarketDataError(
                f"Binance returned invalid kline values for {normalized_symbol}"
            ) from error
        candles.append(candle)

    candles.sort(key=lambda candle: candle.open_time_ms)
    return candles


def open_candle_database(path: Path = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    """Open the candle database with settings suitable for concurrent readers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    create_candles_table(connection)
    connection.commit()
    return connection


def _latest_open_time_ms(
    connection: sqlite3.Connection,
    *,
    exchange: str,
    symbol: str,
) -> int | None:
    row = connection.execute(
        """
        SELECT MAX(open_time_ms)
        FROM candles
        WHERE exchange = ? AND symbol = ? AND interval_seconds = ?
        """,
        (exchange.strip().lower(), symbol.strip().upper(), INTERVAL_SECONDS),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _collect_symbol(
    connection: sqlite3.Connection,
    symbol: str,
    *,
    request_limit: int,
    base_url: str,
    exchange: str,
    server_time_ms: int,
    timeout: float,
) -> int:
    """Store every missing completed candle for one symbol."""
    last_complete_open_ms = (
        (server_time_ms // INTERVAL_MS) - 1
    ) * INTERVAL_MS
    last_open_time_ms = _latest_open_time_ms(
        connection,
        exchange=exchange,
        symbol=symbol,
    )
    start_time_ms = (
        None if last_open_time_ms is None else last_open_time_ms + INTERVAL_MS
    )
    if start_time_ms is not None and start_time_ms > last_complete_open_ms:
        return 0

    stored = 0

    while True:
        candles = fetch_completed_minute_candles(
            symbol,
            start_time_ms=start_time_ms,
            limit=request_limit,
            base_url=base_url,
            exchange=exchange,
            server_time_ms=server_time_ms,
            timeout=timeout,
        )
        if not candles:
            raise BinanceMarketDataError(
                f"Binance returned no candles while filling {symbol}"
            )

        if start_time_ms is not None:
            if candles[0].open_time_ms != start_time_ms:
                raise BinanceMarketDataError(
                    f"Binance returned a gap before {symbol} candle "
                    f"{start_time_ms}"
                )

        for previous, current in zip(candles, candles[1:]):
            if current.open_time_ms != previous.open_time_ms + INTERVAL_MS:
                raise BinanceMarketDataError(
                    f"Binance returned non-contiguous candles for {symbol}"
                )

        newest_open_time_ms = candles[-1].open_time_ms
        if newest_open_time_ms > last_complete_open_ms:
            raise BinanceMarketDataError(
                f"Binance returned an unfinished candle for {symbol}"
            )

        with connection:
            for candle in candles:
                upsert_candle(connection, candle)
        stored += len(candles)

        if newest_open_time_ms == last_complete_open_ms:
            break

        next_start_time_ms = newest_open_time_ms + INTERVAL_MS
        if start_time_ms is not None and next_start_time_ms <= start_time_ms:
            raise BinanceMarketDataError(
                f"Binance did not advance the candle cursor for {symbol}"
            )
        start_time_ms = next_start_time_ms

    return stored


def collect_once(
    connection: sqlite3.Connection,
    *,
    symbols: Sequence[str] = BINANCE_SYMBOLS,
    request_limit: int = MAX_KLINES_PER_REQUEST,
    base_url: str = BINANCE_MARKET_DATA_URL,
    exchange: str = "binance",
    timeout: float = 10.0,
) -> int:
    """Fetch and store all currently missing completed candles."""
    if not symbols:
        raise ValueError("at least one symbol is required")
    if not 1 <= request_limit <= MAX_KLINES_PER_REQUEST:
        raise ValueError(
            f"request_limit must be between 1 and {MAX_KLINES_PER_REQUEST}"
        )

    normalized_symbols = tuple(symbol.strip().upper() for symbol in symbols)
    if any(not symbol for symbol in normalized_symbols):
        raise ValueError("symbols cannot be empty")

    server_time_ms = fetch_binance_server_time_ms(
        base_url=base_url,
        timeout=timeout,
    )
    stored = 0
    failures: list[str] = []
    for symbol in normalized_symbols:
        try:
            symbol_count = _collect_symbol(
                connection,
                symbol,
                request_limit=request_limit,
                base_url=base_url,
                exchange=exchange,
                server_time_ms=server_time_ms,
                timeout=timeout,
            )
        except BinanceMarketDataError as error:
            failures.append(symbol)
            logger.error("%s", error)
            continue

        stored += symbol_count
        logger.info("Stored %d new candles for %s", symbol_count, symbol)

    if failures:
        failed_symbols = ", ".join(failures)
        raise BinanceMarketDataError(
            f"Binance collection failed for: {failed_symbols}"
        )
    return stored


def _sleep_until_next_minute(delay_seconds: float) -> None:
    """Wait until shortly after the next UTC minute boundary."""
    now = time.time()
    next_minute = ((int(now) // 60) + 1) * 60
    time.sleep(max(0.0, next_minute + delay_seconds - now))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect completed Binance one-minute candles into SQLite."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help=f"SQLite database path (default: {DEFAULT_DATABASE_PATH})",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=list(BINANCE_SYMBOLS),
        help="Binance symbols to collect",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("BINANCE_MARKET_DATA_URL", BINANCE_MARKET_DATA_URL),
        help="Binance-compatible public REST base URL",
    )
    parser.add_argument(
        "--exchange",
        default="binance",
        help="Exchange name stored with each candle",
    )
    parser.add_argument(
        "--request-limit",
        type=int,
        default=MAX_KLINES_PER_REQUEST,
        help="API page size and first-run lookback in minutes (1-1000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--poll-delay",
        type=float,
        default=2.0,
        help="Seconds after each minute boundary to poll",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Collect once and exit instead of polling continuously",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the Binance candle collector command."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not 1 <= args.request_limit <= MAX_KLINES_PER_REQUEST:
        raise SystemExit(
            f"--request-limit must be between 1 and {MAX_KLINES_PER_REQUEST}"
        )
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if args.poll_delay < 0:
        raise SystemExit("--poll-delay cannot be negative")
    if not args.exchange.strip():
        raise SystemExit("--exchange cannot be empty")

    symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in args.symbols))
    if any(not symbol for symbol in symbols):
        raise SystemExit("--symbols cannot contain an empty value")

    connection = open_candle_database(args.database)
    try:
        while True:
            try:
                collect_once(
                    connection,
                    symbols=symbols,
                    request_limit=args.request_limit,
                    base_url=args.base_url,
                    exchange=args.exchange,
                    timeout=args.timeout,
                )
            except BinanceMarketDataError:
                logger.exception("Binance candle collection failed")
                if args.once:
                    return 1

            if args.once:
                return 0
            _sleep_until_next_minute(args.poll_delay)
    except KeyboardInterrupt:
        logger.info("Binance candle collector stopped")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
