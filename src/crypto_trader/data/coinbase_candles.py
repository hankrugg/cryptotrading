"""Collect completed candles from Coinbase public market data.

The collector uses the unauthenticated Advanced Trade public REST API.  It
reads Coinbase's server clock, requests only closed candles, reports any
buckets omitted by Coinbase, and upserts the returned rows into the
project's SQLite database without inventing replacement prices.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from crypto_trader.data.candles import Candle, create_candles_table, upsert_candle

logger = logging.getLogger(__name__)

COINBASE_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")
COINBASE_API_URL = "https://api.coinbase.com"
INTERVAL_SECONDS = 60
INTERVAL_MS = INTERVAL_SECONDS * 1_000
GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 5 * 60,
    "FIFTEEN_MINUTE": 15 * 60,
    "THIRTY_MINUTE": 30 * 60,
    "ONE_HOUR": 60 * 60,
    "TWO_HOUR": 2 * 60 * 60,
    "FOUR_HOUR": 4 * 60 * 60,
    "SIX_HOUR": 6 * 60 * 60,
    "ONE_DAY": 24 * 60 * 60,
}
MAX_CANDLES_PER_REQUEST = 350
DEFAULT_MAX_RETRIES = 4
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "database" / "trading.db"
_PRODUCT_ID_PATTERN = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")


class CoinbaseCandleError(RuntimeError):
    """Raised when Coinbase candle data cannot be fetched or validated."""


def normalize_products(products: Sequence[str]) -> tuple[str, ...]:
    """Normalize, validate, and de-duplicate Coinbase product IDs."""

    if not products:
        raise ValueError("at least one product is required")
    normalized = tuple(dict.fromkeys(product.strip().upper() for product in products))
    if any(not _PRODUCT_ID_PATTERN.fullmatch(product) for product in normalized):
        raise ValueError("products must look like BTC-USD")
    return normalized


def normalize_granularity(granularity: str) -> tuple[str, int]:
    """Return a Coinbase granularity name and its length in seconds."""

    normalized = granularity.strip().upper()
    try:
        return normalized, GRANULARITY_SECONDS[normalized]
    except KeyError as error:
        choices = ", ".join(GRANULARITY_SECONDS)
        raise ValueError(f"granularity must be one of: {choices}") from error


def _request_json(
    path: str,
    *,
    params: dict[str, object] | None = None,
    base_url: str = COINBASE_API_URL,
    timeout: float = 10.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Any:
    """Request JSON, retrying rate limits and temporary Coinbase failures."""

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if max_retries < 0:
        raise ValueError("max_retries cannot be negative")

    query = f"?{urlencode(params)}" if params else ""
    url = f"{base_url.rstrip('/')}{path}{query}"
    request = Request(url, headers={"User-Agent": "crypto-trader-school/0.1"})
    for attempt in range(max_retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            break
        except HTTPError as error:
            retryable = error.code in RETRYABLE_HTTP_STATUS_CODES
            if retryable and attempt < max_retries:
                retry_after = (
                    error.headers.get("Retry-After") if error.headers else None
                )
                try:
                    delay = (
                        float(retry_after) if retry_after is not None else 2**attempt
                    )
                except ValueError:
                    delay = 2**attempt
                delay = min(30.0, max(0.0, delay))
                logger.warning(
                    "Coinbase returned HTTP %d; retrying in %.1f seconds (%d/%d)",
                    error.code,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(delay)
                continue
            try:
                detail = error.read().decode("utf-8", errors="replace")[:500]
            except OSError:
                detail = ""
            message = detail or str(error.reason)
            raise CoinbaseCandleError(
                f"Coinbase returned HTTP {error.code}: {message}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            if attempt < max_retries:
                delay = min(30.0, float(2**attempt))
                logger.warning(
                    "Coinbase request failed; retrying in %.1f seconds (%d/%d): %s",
                    delay,
                    attempt + 1,
                    max_retries,
                    error,
                )
                time.sleep(delay)
                continue
            raise CoinbaseCandleError(f"Coinbase request failed: {error}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CoinbaseCandleError("Coinbase returned invalid JSON") from error

    if isinstance(payload, dict) and "error" in payload:
        code = payload.get("code", "unknown")
        message = payload.get("message") or payload.get("error")
        raise CoinbaseCandleError(f"Coinbase API error ({code}): {message}")
    return payload


def fetch_coinbase_server_time_ms(
    *,
    base_url: str = COINBASE_API_URL,
    timeout: float = 10.0,
) -> int:
    """Return Coinbase server time as UTC Unix milliseconds."""

    payload = _request_json(
        "/api/v3/brokerage/time",
        base_url=base_url,
        timeout=timeout,
    )
    if not isinstance(payload, dict) or isinstance(payload.get("epochMillis"), bool):
        raise CoinbaseCandleError("Coinbase returned an invalid server-time response")
    try:
        server_time_ms = int(payload["epochMillis"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise CoinbaseCandleError(
            "Coinbase returned an invalid server-time response"
        ) from error
    if server_time_ms < 0:
        raise CoinbaseCandleError("Coinbase server time cannot be negative")
    return server_time_ms


def _last_complete_open_time_ms(server_time_ms: int, interval_ms: int) -> int:
    if server_time_ms < 0:
        raise ValueError("server_time_ms cannot be negative")
    if interval_ms <= 0:
        raise ValueError("interval_ms must be positive")
    return ((server_time_ms // interval_ms) - 1) * interval_ms


def fetch_completed_candles(
    product_id: str,
    *,
    granularity: str = "ONE_MINUTE",
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = MAX_CANDLES_PER_REQUEST,
    base_url: str = COINBASE_API_URL,
    exchange: str = "coinbase",
    server_time_ms: int | None = None,
    timeout: float = 10.0,
) -> list[Candle]:
    """Fetch one page of completed candles at a supported granularity."""

    normalized_granularity, interval_seconds = normalize_granularity(granularity)
    interval_ms = interval_seconds * 1_000
    normalized_product = normalize_products((product_id,))[0]
    if not 1 <= limit <= MAX_CANDLES_PER_REQUEST:
        raise ValueError(f"limit must be between 1 and {MAX_CANDLES_PER_REQUEST}")
    for name, value in (("start_time_ms", start_time_ms), ("end_time_ms", end_time_ms)):
        if value is not None and value < 0:
            raise ValueError(f"{name} cannot be negative")
        if value is not None and value % interval_ms:
            raise ValueError(f"{name} must align to the candle interval")
    if start_time_ms is not None and end_time_ms is not None:
        if end_time_ms < start_time_ms:
            raise ValueError("end_time_ms cannot be before start_time_ms")

    observed_at_ms = (
        server_time_ms
        if server_time_ms is not None
        else fetch_coinbase_server_time_ms(base_url=base_url, timeout=timeout)
    )
    last_complete_open_ms = _last_complete_open_time_ms(observed_at_ms, interval_ms)
    if last_complete_open_ms < 0:
        return []

    requested_end_ms = min(
        last_complete_open_ms,
        end_time_ms if end_time_ms is not None else last_complete_open_ms,
    )
    if start_time_ms is None:
        requested_start_ms = max(
            0,
            requested_end_ms - (limit - 1) * interval_ms,
        )
    else:
        requested_start_ms = start_time_ms
    if requested_start_ms > requested_end_ms:
        return []

    requested_count = ((requested_end_ms - requested_start_ms) // interval_ms) + 1
    if requested_count > limit:
        raise ValueError("requested candle window is larger than limit")

    # Coinbase includes the candle whose opening time equals ``end``. Passing
    # the next boundary could push the oldest requested row out of a full page.
    params = {
        "start": requested_start_ms // 1_000,
        "end": requested_end_ms // 1_000,
        "granularity": normalized_granularity,
        "limit": requested_count,
    }
    encoded_product = quote(normalized_product, safe="-")
    payload = _request_json(
        f"/api/v3/brokerage/market/products/{encoded_product}/candles",
        params=params,
        base_url=base_url,
        timeout=timeout,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise CoinbaseCandleError(
            f"Coinbase returned an unexpected candle response for {normalized_product}"
        )

    candles_by_open_time: dict[int, Candle] = {}
    for row in payload["candles"]:
        if not isinstance(row, dict):
            raise CoinbaseCandleError(
                f"Coinbase returned a malformed candle for {normalized_product}"
            )
        try:
            open_time_ms = int(row["start"]) * 1_000
            if not requested_start_ms <= open_time_ms <= requested_end_ms:
                continue
            candle = Candle(
                exchange=exchange,
                symbol=normalized_product,
                interval_seconds=interval_seconds,
                open_time_ms=open_time_ms,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                ingested_at_ms=observed_at_ms,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise CoinbaseCandleError(
                f"Coinbase returned invalid candle values for {normalized_product}"
            ) from error
        if open_time_ms in candles_by_open_time:
            raise CoinbaseCandleError(
                f"Coinbase returned duplicate candles for {normalized_product}"
            )
        candles_by_open_time[open_time_ms] = candle

    return [candles_by_open_time[key] for key in sorted(candles_by_open_time)]


def fetch_completed_minute_candles(
    product_id: str,
    *,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = MAX_CANDLES_PER_REQUEST,
    base_url: str = COINBASE_API_URL,
    exchange: str = "coinbase",
    server_time_ms: int | None = None,
    timeout: float = 10.0,
) -> list[Candle]:
    """Fetch one page of completed one-minute candles.

    ``start_time_ms`` and ``end_time_ms`` are inclusive candle-opening times.
    If neither is supplied, the newest ``limit`` completed candles are
    requested.  Returned rows are always sorted oldest to newest.
    """

    return fetch_completed_candles(
        product_id,
        granularity="ONE_MINUTE",
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        limit=limit,
        base_url=base_url,
        exchange=exchange,
        server_time_ms=server_time_ms,
        timeout=timeout,
    )


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
    product_id: str,
    interval_seconds: int,
) -> int | None:
    row = connection.execute(
        """
        SELECT MAX(open_time_ms)
        FROM candles
        WHERE exchange = ? AND symbol = ? AND interval_seconds = ?
        """,
        (exchange.strip().lower(), product_id, interval_seconds),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _missing_open_times(
    candles: Sequence[Candle],
    *,
    expected_start_ms: int,
    expected_end_ms: int,
    interval_ms: int,
) -> list[int]:
    """Return requested candle openings that Coinbase did not provide.

    Coinbase's historical endpoint occasionally omits an otherwise valid
    candle bucket. A missing source row is not equivalent to a zero-volume
    candle, so the collector records the gap as a warning instead of creating
    synthetic OHLCV data.
    """

    actual = {candle.open_time_ms for candle in candles}
    return [
        open_time_ms
        for open_time_ms in range(
            expected_start_ms,
            expected_end_ms + 1,
            interval_ms,
        )
        if open_time_ms not in actual
    ]


def _store_window(
    connection: sqlite3.Connection,
    product_id: str,
    *,
    start_time_ms: int,
    end_time_ms: int,
    request_limit: int,
    base_url: str,
    exchange: str,
    server_time_ms: int,
    timeout: float,
    granularity: str,
) -> int:
    _, interval_seconds = normalize_granularity(granularity)
    interval_ms = interval_seconds * 1_000
    candles = fetch_completed_candles(
        product_id,
        granularity=granularity,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        limit=request_limit,
        base_url=base_url,
        exchange=exchange,
        server_time_ms=server_time_ms,
        timeout=timeout,
    )
    missing = _missing_open_times(
        candles,
        expected_start_ms=start_time_ms,
        expected_end_ms=end_time_ms,
        interval_ms=interval_ms,
    )
    if missing:
        logger.warning(
            "Coinbase omitted %d %s candle(s) for %s; first missing "
            "open_time_ms=%d. Keeping the gap rather than inventing OHLCV data.",
            len(missing),
            granularity,
            product_id,
            missing[0],
        )
    with connection:
        for candle in candles:
            upsert_candle(connection, candle)
    return len(candles)


def _collect_product(
    connection: sqlite3.Connection,
    product_id: str,
    *,
    request_limit: int,
    base_url: str,
    exchange: str,
    server_time_ms: int,
    timeout: float,
    granularity: str,
) -> int:
    """Store every missing completed candle for one Coinbase product."""

    _, interval_seconds = normalize_granularity(granularity)
    interval_ms = interval_seconds * 1_000
    last_complete_open_ms = _last_complete_open_time_ms(server_time_ms, interval_ms)
    latest_stored_ms = _latest_open_time_ms(
        connection,
        exchange=exchange,
        product_id=product_id,
        interval_seconds=interval_seconds,
    )
    if latest_stored_ms is None:
        cursor_ms = max(
            0,
            last_complete_open_ms - (request_limit - 1) * interval_ms,
        )
    else:
        cursor_ms = latest_stored_ms + interval_ms
    if cursor_ms > last_complete_open_ms:
        return 0

    stored = 0
    while cursor_ms <= last_complete_open_ms:
        page_end_ms = min(
            cursor_ms + (request_limit - 1) * interval_ms,
            last_complete_open_ms,
        )
        stored += _store_window(
            connection,
            product_id,
            start_time_ms=cursor_ms,
            end_time_ms=page_end_ms,
            request_limit=request_limit,
            base_url=base_url,
            exchange=exchange,
            server_time_ms=server_time_ms,
            timeout=timeout,
            granularity=granularity,
        )
        cursor_ms = page_end_ms + interval_ms
    return stored


def _calendar_years_ago_ms(
    last_complete_open_ms: int,
    years: int,
    interval_ms: int,
) -> int:
    """Return an interval-aligned UTC opening time a number of years earlier."""

    if years <= 0:
        raise ValueError("years must be positive")
    last_complete = datetime.fromtimestamp(
        last_complete_open_ms / 1_000,
        tz=timezone.utc,
    )
    try:
        first = last_complete.replace(year=last_complete.year - years)
    except ValueError:
        # A February 29 end date maps to February 28 in a non-leap start year.
        first = last_complete.replace(year=last_complete.year - years, day=28)
    return int(first.timestamp() * 1_000) // interval_ms * interval_ms


def _window_is_complete(
    connection: sqlite3.Connection,
    *,
    exchange: str,
    product_id: str,
    interval_seconds: int,
    start_time_ms: int,
    end_time_ms: int,
) -> bool:
    """Return true when every expected candle in a page is already stored."""

    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM candles
        WHERE exchange = ?
          AND symbol = ?
          AND interval_seconds = ?
          AND open_time_ms BETWEEN ? AND ?
        """,
        (
            exchange.strip().lower(),
            product_id,
            interval_seconds,
            start_time_ms,
            end_time_ms,
        ),
    ).fetchone()
    expected = ((end_time_ms - start_time_ms) // (interval_seconds * 1_000)) + 1
    return row is not None and int(row[0]) == expected


def backfill_historical_candles(
    connection: sqlite3.Connection,
    *,
    products: Sequence[str],
    minutes: int | None = None,
    years: int | None = None,
    granularity: str = "ONE_MINUTE",
    request_limit: int = MAX_CANDLES_PER_REQUEST,
    base_url: str = COINBASE_API_URL,
    exchange: str = "coinbase",
    timeout: float = 10.0,
    request_delay: float = 0.1,
) -> int:
    """Fetch a historical window and upsert it into SQLite.

    Supply exactly one of ``minutes`` or ``years``. Complete pages already in
    SQLite are skipped, so an interrupted multi-year backfill resumes quickly.
    """

    normalized_products = normalize_products(products)
    normalized_granularity, interval_seconds = normalize_granularity(granularity)
    interval_ms = interval_seconds * 1_000
    if (minutes is None) == (years is None):
        raise ValueError("provide exactly one of minutes or years")
    if minutes is not None and minutes <= 0:
        raise ValueError("minutes must be positive")
    if years is not None and years <= 0:
        raise ValueError("years must be positive")
    if request_delay < 0:
        raise ValueError("request_delay cannot be negative")
    if not 1 <= request_limit <= MAX_CANDLES_PER_REQUEST:
        raise ValueError(
            f"request_limit must be between 1 and {MAX_CANDLES_PER_REQUEST}"
        )

    server_time_ms = fetch_coinbase_server_time_ms(
        base_url=base_url,
        timeout=timeout,
    )
    last_complete_open_ms = _last_complete_open_time_ms(server_time_ms, interval_ms)
    if years is not None:
        first_open_ms = _calendar_years_ago_ms(
            last_complete_open_ms,
            years,
            interval_ms,
        )
    else:
        assert minutes is not None
        requested_buckets = max(1, math.ceil(minutes * 60 / interval_seconds))
        first_open_ms = max(
            0,
            last_complete_open_ms - (requested_buckets - 1) * interval_ms,
        )

    total_buckets = ((last_complete_open_ms - first_open_ms) // interval_ms) + 1
    total_pages = math.ceil(total_buckets / request_limit)

    stored = 0
    failures: list[str] = []
    for product_id in normalized_products:
        cursor_ms = first_open_ms
        page_number = 0
        try:
            while cursor_ms <= last_complete_open_ms:
                page_number += 1
                page_end_ms = min(
                    cursor_ms + (request_limit - 1) * interval_ms,
                    last_complete_open_ms,
                )
                if not _window_is_complete(
                    connection,
                    exchange=exchange,
                    product_id=product_id,
                    interval_seconds=interval_seconds,
                    start_time_ms=cursor_ms,
                    end_time_ms=page_end_ms,
                ):
                    stored += _store_window(
                        connection,
                        product_id,
                        start_time_ms=cursor_ms,
                        end_time_ms=page_end_ms,
                        request_limit=request_limit,
                        base_url=base_url,
                        exchange=exchange,
                        server_time_ms=server_time_ms,
                        timeout=timeout,
                        granularity=normalized_granularity,
                    )
                    if request_delay:
                        time.sleep(request_delay)
                if (
                    page_number == 1
                    or page_number % 10 == 0
                    or page_number == total_pages
                ):
                    logger.info(
                        "Backfill progress for %s: page %d/%d",
                        product_id,
                        page_number,
                        total_pages,
                    )
                cursor_ms = page_end_ms + interval_ms
        except CoinbaseCandleError as error:
            failures.append(product_id)
            logger.error("%s", error)
            continue
        logger.info(
            "Backfilled %s candles for %s from %d through %d",
            normalized_granularity,
            product_id,
            first_open_ms,
            last_complete_open_ms,
        )

    if failures:
        raise CoinbaseCandleError(
            f"Coinbase historical backfill failed for: {', '.join(failures)}"
        )
    return stored


def collect_once(
    connection: sqlite3.Connection,
    *,
    products: Sequence[str] = COINBASE_PRODUCTS,
    request_limit: int = MAX_CANDLES_PER_REQUEST,
    base_url: str = COINBASE_API_URL,
    exchange: str = "coinbase",
    timeout: float = 10.0,
    granularity: str = "ONE_MINUTE",
) -> int:
    """Fetch and store all currently missing completed Coinbase candles."""

    normalized_products = normalize_products(products)
    if not 1 <= request_limit <= MAX_CANDLES_PER_REQUEST:
        raise ValueError(
            f"request_limit must be between 1 and {MAX_CANDLES_PER_REQUEST}"
        )

    server_time_ms = fetch_coinbase_server_time_ms(
        base_url=base_url,
        timeout=timeout,
    )
    stored = 0
    failures: list[str] = []
    for product_id in normalized_products:
        try:
            product_count = _collect_product(
                connection,
                product_id,
                request_limit=request_limit,
                base_url=base_url,
                exchange=exchange,
                server_time_ms=server_time_ms,
                timeout=timeout,
                granularity=granularity,
            )
        except CoinbaseCandleError as error:
            failures.append(product_id)
            logger.error("%s", error)
            continue
        stored += product_count
        logger.info("Stored %d new candles for %s", product_count, product_id)

    if failures:
        raise CoinbaseCandleError(
            f"Coinbase candle collection failed for: {', '.join(failures)}"
        )
    return stored


def _sleep_until_next_interval(delay_seconds: float, interval_seconds: int) -> None:
    """Wait until shortly after the next candle boundary."""

    now = time.time()
    next_boundary = ((int(now) // interval_seconds) + 1) * interval_seconds
    time.sleep(max(0.0, next_boundary + delay_seconds - now))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect completed Coinbase candles into SQLite."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help=f"SQLite database path (default: {DEFAULT_DATABASE_PATH})",
    )
    parser.add_argument(
        "--products",
        "--symbols",
        dest="products",
        nargs="+",
        default=list(COINBASE_PRODUCTS),
        help="Coinbase product IDs to collect, such as BTC-USD",
    )
    parser.add_argument(
        "--granularity",
        choices=tuple(GRANULARITY_SECONDS),
        default="ONE_MINUTE",
        help="Coinbase candle granularity (default: ONE_MINUTE)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("COINBASE_API_URL", COINBASE_API_URL),
        help="Coinbase Advanced Trade public REST base URL",
    )
    parser.add_argument(
        "--exchange",
        default="coinbase",
        help="Exchange name stored with each candle",
    )
    parser.add_argument(
        "--request-limit",
        type=int,
        default=MAX_CANDLES_PER_REQUEST,
        help="API page size and first-run lookback in candles (1-350)",
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
        help="Seconds after each candle boundary to poll",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.1,
        help="Pause between historical API pages in seconds",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Collect once and exit instead of polling continuously",
    )
    backfill_group = parser.add_mutually_exclusive_group()
    backfill_group.add_argument(
        "--backfill-days",
        type=int,
        help="Backfill this many days of completed candles, then exit",
    )
    backfill_group.add_argument(
        "--backfill-years",
        type=int,
        help="Backfill this many calendar years of completed candles, then exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the Coinbase candle collector command."""

    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not 1 <= args.request_limit <= MAX_CANDLES_PER_REQUEST:
        raise SystemExit(
            f"--request-limit must be between 1 and {MAX_CANDLES_PER_REQUEST}"
        )
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if args.poll_delay < 0:
        raise SystemExit("--poll-delay cannot be negative")
    if args.request_delay < 0:
        raise SystemExit("--request-delay cannot be negative")
    if args.backfill_days is not None and args.backfill_days <= 0:
        raise SystemExit("--backfill-days must be positive")
    if args.backfill_years is not None and args.backfill_years <= 0:
        raise SystemExit("--backfill-years must be positive")
    if not args.exchange.strip():
        raise SystemExit("--exchange cannot be empty")
    try:
        products = normalize_products(args.products)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    connection = open_candle_database(args.database)
    try:
        if args.backfill_days is not None or args.backfill_years is not None:
            try:
                stored = backfill_historical_candles(
                    connection,
                    products=products,
                    minutes=(
                        args.backfill_days * 24 * 60
                        if args.backfill_days is not None
                        else None
                    ),
                    years=args.backfill_years,
                    granularity=args.granularity,
                    request_limit=args.request_limit,
                    base_url=args.base_url,
                    exchange=args.exchange,
                    timeout=args.timeout,
                    request_delay=args.request_delay,
                )
            except CoinbaseCandleError:
                logger.exception("Coinbase historical backfill failed")
                return 1
            logger.info("Historical backfill complete: processed %d rows", stored)
            return 0

        while True:
            try:
                collect_once(
                    connection,
                    products=products,
                    request_limit=args.request_limit,
                    base_url=args.base_url,
                    exchange=args.exchange,
                    timeout=args.timeout,
                    granularity=args.granularity,
                )
            except CoinbaseCandleError:
                logger.exception("Coinbase candle collection failed")
                if args.once:
                    return 1
            if args.once:
                return 0
            interval_seconds = GRANULARITY_SECONDS[args.granularity]
            _sleep_until_next_interval(args.poll_delay, interval_seconds)
    except KeyboardInterrupt:
        logger.info("Coinbase candle collector stopped")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
