"""Collect completed one-minute candles from Binance public market data.

The collector uses Binance's public REST API, so it does not need an API key
and cannot place orders. It asks Binance for the server clock, requests only
closed one-minute klines, validates that the response is contiguous, and
upserts the result into SQLite.
"""

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

# These are the default symbols; command-line options can collect a subset.
BINANCE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
# This public market-data host does not require trading credentials.
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
    # All public API calls flow through this function so timeout, HTTP, network,
    # JSON, and Binance-specific errors have one consistent exception type.
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    query = f"?{urlencode(params)}" if params else ""
    url = f"{base_url.rstrip('/')}{path}{query}"
    request = Request(url, headers={"User-Agent": "crypto-trader-school/0.1"})

    try:
        # urlopen performs a read-only HTTP request. The User-Agent identifies
        # this client to the public endpoint.
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

    # Binance sometimes returns HTTP 200 with an error object; handle that as
    # an application error instead of passing a misleading payload onward.
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
    # Using Binance's clock avoids trusting a Raspberry Pi whose clock may be
    # slightly wrong when deciding whether the current minute has closed.
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
    end_time_ms: int | None = None,
    limit: int = MAX_KLINES_PER_REQUEST,
    base_url: str = BINANCE_MARKET_DATA_URL,
    exchange: str = "binance",
    server_time_ms: int | None = None,
    timeout: float = 10.0,
) -> list[Candle]:
    """Fetch completed one-minute klines for one Binance symbol.

    ``start_time_ms`` and ``end_time_ms`` are inclusive millisecond bounds for
    one historical page. The normal collector leaves ``end_time_ms`` unset so
    the upper bound is the newest completed minute according to Binance's
    server clock.
    """
    # Normalize and validate caller input before constructing the API query.
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol cannot be empty")
    if start_time_ms is not None and start_time_ms < 0:
        raise ValueError("start_time_ms cannot be negative")
    if end_time_ms is not None and end_time_ms < 0:
        raise ValueError("end_time_ms cannot be negative")
    if (
        start_time_ms is not None
        and end_time_ms is not None
        and end_time_ms < start_time_ms
    ):
        raise ValueError("end_time_ms cannot be before start_time_ms")
    if not 1 <= limit <= MAX_KLINES_PER_REQUEST:
        raise ValueError(f"limit must be between 1 and {MAX_KLINES_PER_REQUEST}")

    # A caller can pass one server timestamp for a batch; otherwise fetch it
    # here. This keeps all symbols in one collection cycle on the same boundary.
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
    if end_time_ms is not None:
        # A historical backfill may request a window ending before the current
        # minute. Never let that request include a currently forming candle.
        last_complete_close_ms = min(last_complete_close_ms, end_time_ms)
    if start_time_ms is not None and start_time_ms > last_complete_close_ms:
        return []
    params: dict[str, object] = {
        "symbol": normalized_symbol,
        "interval": "1m",
        "endTime": last_complete_close_ms,
        "limit": limit,
    }
    if start_time_ms is not None:
        params["startTime"] = start_time_ms

    # Request at most 1000 rows because that is Binance's endpoint limit.
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
        # A kline must contain at least its open time and close time fields;
        # Candle performs the detailed numeric validation below.
        if not isinstance(row, list) or len(row) < 7:
            raise BinanceMarketDataError(
                f"Binance returned a malformed kline for {normalized_symbol}"
            )
        try:
            close_time_ms = int(row[6])
            if close_time_ms > last_complete_close_ms:
                # Keep this second guard even though endTime should already
                # exclude the open candle.
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

    # Sorting makes pagination and continuity checks deterministic even if the
    # API response order changes.
    candles.sort(key=lambda candle: candle.open_time_ms)
    return candles


def open_candle_database(path: Path = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    """Open the candle database with settings suitable for concurrent readers."""
    # WAL permits the risk service to read while this collector writes. The
    # timeout/busy_timeout reduce transient lock errors between the services.
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    # Creating the table here makes the CLI self-initializing on a new Pi.
    create_candles_table(connection)
    connection.commit()
    return connection


def _latest_open_time_ms(
    connection: sqlite3.Connection,
    *,
    exchange: str,
    symbol: str,
) -> int | None:
    # The newest stored candle is the resume cursor for this symbol. A missing
    # row means the collector should perform its initial backfill.
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
    # Binance labels a minute by its opening time. The last complete opening
    # time is therefore one interval before the current minute's opening time.
    last_complete_open_ms = (
        (server_time_ms // INTERVAL_MS) - 1
    ) * INTERVAL_MS
    last_open_time_ms = _latest_open_time_ms(
        connection,
        exchange=exchange,
        symbol=symbol,
    )
    # Resume exactly after the newest row already stored; otherwise backfill
    # from Binance's earliest page available to this request.
    start_time_ms = (
        None if last_open_time_ms is None else last_open_time_ms + INTERVAL_MS
    )
    if start_time_ms is not None and start_time_ms > last_complete_open_ms:
        # The database is already caught up for this symbol.
        return 0

    stored = 0

    while True:
        # One page may not cover a long outage, so continue requesting pages
        # until the current completed minute is stored.
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
            # The first returned row must be exactly the requested cursor; a
            # missing row would create a silent hole in the time series.
            if candles[0].open_time_ms != start_time_ms:
                raise BinanceMarketDataError(
                    f"Binance returned a gap before {symbol} candle "
                    f"{start_time_ms}"
                )

        # Verify that each page is a continuous one-minute sequence.
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

        # The page is written atomically. If the process stops midway, SQLite
        # rolls back the incomplete transaction and the next run resumes.
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


def backfill_historical_candles(
    connection: sqlite3.Connection,
    *,
    symbols: Sequence[str],
    minutes: int,
    request_limit: int = MAX_KLINES_PER_REQUEST,
    base_url: str = BINANCE_MARKET_DATA_URL,
    exchange: str = "binance",
    timeout: float = 10.0,
) -> int:
    """Fetch a fixed historical window once and upsert it into SQLite.

    The window ends at the newest completed Binance minute and extends
    ``minutes`` candles backward. This deliberately does not use the normal
    resume cursor, so it can fill history before the oldest existing row. The
    operation is safe to repeat because candle identity is protected by the
    database's unique key.
    """
    if not symbols:
        raise ValueError("at least one symbol is required")
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    if not 1 <= request_limit <= MAX_KLINES_PER_REQUEST:
        raise ValueError(
            f"request_limit must be between 1 and {MAX_KLINES_PER_REQUEST}"
        )

    normalized_symbols = tuple(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols)
    )
    if any(not symbol for symbol in normalized_symbols):
        raise ValueError("symbols cannot be empty")

    # One server-time sample gives every symbol the same historical endpoint.
    server_time_ms = fetch_binance_server_time_ms(
        base_url=base_url,
        timeout=timeout,
    )
    last_complete_open_ms = (
        (server_time_ms // INTERVAL_MS) - 1
    ) * INTERVAL_MS
    first_open_ms = last_complete_open_ms - (minutes - 1) * INTERVAL_MS
    last_close_ms = last_complete_open_ms + INTERVAL_MS - 1

    stored = 0
    failures: list[str] = []
    for symbol in normalized_symbols:
        cursor_ms = first_open_ms
        try:
            while cursor_ms <= last_complete_open_ms:
                # Binance limits one response page, so walk forward until the
                # requested historical window is completely covered.
                candles = fetch_completed_minute_candles(
                    symbol,
                    start_time_ms=cursor_ms,
                    end_time_ms=last_close_ms,
                    limit=request_limit,
                    base_url=base_url,
                    exchange=exchange,
                    server_time_ms=server_time_ms,
                    timeout=timeout,
                )
                if not candles:
                    raise BinanceMarketDataError(
                        f"Binance returned no candles while backfilling {symbol}"
                    )
                if candles[0].open_time_ms != cursor_ms:
                    raise BinanceMarketDataError(
                        f"Binance returned a gap before {symbol} candle {cursor_ms}"
                    )

                for previous, current in zip(candles, candles[1:]):
                    if current.open_time_ms != previous.open_time_ms + INTERVAL_MS:
                        raise BinanceMarketDataError(
                            f"Binance returned non-contiguous candles for {symbol}"
                        )

                newest_open_ms = candles[-1].open_time_ms
                if newest_open_ms > last_complete_open_ms:
                    raise BinanceMarketDataError(
                        f"Binance returned an unfinished candle for {symbol}"
                    )

                # A page is one transaction, so an interrupted backfill never
                # leaves half of that page committed.
                with connection:
                    for candle in candles:
                        upsert_candle(connection, candle)
                stored += len(candles)

                next_cursor_ms = newest_open_ms + INTERVAL_MS
                if next_cursor_ms <= cursor_ms:
                    raise BinanceMarketDataError(
                        f"Binance did not advance the backfill cursor for {symbol}"
                    )
                cursor_ms = next_cursor_ms
        except BinanceMarketDataError as error:
            failures.append(symbol)
            logger.error("%s", error)
            continue
        logger.info(
            "Backfilled %d requested minutes for %s from %d through %d",
            minutes,
            symbol,
            first_open_ms,
            last_complete_open_ms,
        )

    if failures:
        raise BinanceMarketDataError(
            f"Binance historical backfill failed for: {', '.join(failures)}"
        )
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
    # Validate the batch before making network calls so a typo fails quickly.
    if not symbols:
        raise ValueError("at least one symbol is required")
    if not 1 <= request_limit <= MAX_KLINES_PER_REQUEST:
        raise ValueError(
            f"request_limit must be between 1 and {MAX_KLINES_PER_REQUEST}"
        )

    normalized_symbols = tuple(symbol.strip().upper() for symbol in symbols)
    if any(not symbol for symbol in normalized_symbols):
        raise ValueError("symbols cannot be empty")

    # One server-time read defines the completion boundary for every symbol in
    # this cycle, preventing symbols from using different current minutes.
    server_time_ms = fetch_binance_server_time_ms(
        base_url=base_url,
        timeout=timeout,
    )
    stored = 0
    failures: list[str] = []
    for symbol in normalized_symbols:
        # Continue collecting the other symbols after one symbol fails, then
        # report the batch as failed so systemd/logging can surface the issue.
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
    # Polling just after the boundary gives Binance time to finalize the prior
    # candle while keeping the stored series close to real time.
    now = time.time()
    next_minute = ((int(now) // 60) + 1) * 60
    time.sleep(max(0.0, next_minute + delay_seconds - now))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Keeping all CLI parsing here makes ``main`` easy to test and keeps the
    # collection functions usable as a Python library.
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
    parser.add_argument(
        "--backfill-days",
        type=int,
        help=(
            "Fetch this many days of completed one-minute history for the "
            "selected symbols, then exit"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the Binance candle collector command."""
    # This standalone CLI configures its own basic logging because it does not
    # run through the hourly service's main.py entry point.
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
    if args.backfill_days is not None and args.backfill_days <= 0:
        raise SystemExit("--backfill-days must be positive")
    if not args.exchange.strip():
        raise SystemExit("--exchange cannot be empty")

    # dict.fromkeys removes duplicate command-line symbols while preserving the
    # order the user supplied.
    symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in args.symbols))
    if any(not symbol for symbol in symbols):
        raise SystemExit("--symbols cannot contain an empty value")

    connection = open_candle_database(args.database)
    try:
        if args.backfill_days is not None:
            try:
                stored = backfill_historical_candles(
                    connection,
                    symbols=symbols,
                    minutes=args.backfill_days * 24 * 60,
                    request_limit=args.request_limit,
                    base_url=args.base_url,
                    exchange=args.exchange,
                    timeout=args.timeout,
                )
            except BinanceMarketDataError:
                logger.exception("Binance historical backfill failed")
                return 1
            logger.info(
                "Historical backfill complete: processed %d candle rows",
                stored,
            )
            return 0

        while True:
            # A collection error is logged and retried in continuous mode. In
            # --once mode it becomes a nonzero exit code for scripts/systemd.
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
