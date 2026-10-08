"""Tests for Binance one-minute candle collection.

These tests replace the network with deterministic fake responses, so they
check parsing and persistence without contacting Binance.
"""

from io import BytesIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crypto_trader.data.binance import (
    BINANCE_SYMBOLS,
    BinanceMarketDataError,
    backfill_historical_candles,
    collect_once,
    fetch_completed_minute_candles,
    open_candle_database,
)
from crypto_trader.data.candles import Candle, create_candles_table


class FakeResponse(BytesIO):
    # urlopen returns a context manager; this fake mirrors the two methods the
    # collector uses when reading a response body.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


def kline(open_time_ms: int, close_time_ms: int, close: str = "101.5") -> list:
    # Construct the subset of Binance's positional kline format used by the
    # collector. Extra fields are retained to resemble a real API row.
    return [
        open_time_ms,
        "100.0",
        "102.0",
        "99.0",
        close,
        "12.5",
        close_time_ms,
        "0",
        10,
        "0",
        "0",
        "0",
    ]


class BinanceFetchTests(unittest.TestCase):
    @patch("crypto_trader.data.binance.urlopen")
    def test_fetches_only_completed_minute_candles(self, mocked_urlopen) -> None:
        # The third row is still forming at ``now_ms`` and must be ignored.
        now_ms = 1_800_000_120_000
        payload = [
            kline(1_800_000_000_000, 1_800_000_059_999),
            kline(1_800_000_060_000, 1_800_000_119_999, close="101.75"),
            kline(1_800_000_120_000, 1_800_000_179_999, close="102.0"),
        ]
        mocked_urlopen.return_value = FakeResponse(json.dumps(payload).encode())

        candles = fetch_completed_minute_candles(
            " btcusdt ", limit=3, server_time_ms=now_ms
        )

        self.assertEqual(
            [c.open_time_ms for c in candles],
            [payload[0][0], payload[1][0]],
        )
        self.assertEqual(candles[-1].close, 101.75)
        self.assertEqual(candles[-1].symbol, "BTCUSDT")
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("symbol=BTCUSDT", request.full_url)
        self.assertIn("interval=1m", request.full_url)

    @patch("crypto_trader.data.binance.urlopen")
    def test_api_error_response_is_reported(self, mocked_urlopen) -> None:
        # Binance may encode an API failure as a JSON error object.
        payload = {"code": -1121, "msg": "Invalid symbol."}
        mocked_urlopen.return_value = FakeResponse(json.dumps(payload).encode())

        with self.assertRaisesRegex(BinanceMarketDataError, "Invalid symbol"):
            fetch_completed_minute_candles("NOTREAL", server_time_ms=1_800_000_120_000)


class BinanceCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Each test gets an isolated in-memory database.
        self.connection = sqlite3.connect(":memory:")
        create_candles_table(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    @patch("crypto_trader.data.binance.fetch_binance_server_time_ms")
    @patch("crypto_trader.data.binance.fetch_completed_minute_candles")
    def test_collects_all_configured_symbols(
        self, mocked_fetch, mocked_server_time
    ) -> None:
        # A successful cycle should write one candle for every default symbol.
        mocked_server_time.return_value = 1_800_000_120_000

        def candle_for_symbol(symbol, **kwargs):
            return [
                Candle(
                    exchange="binance",
                    symbol=symbol,
                    interval_seconds=60,
                    open_time_ms=1_800_000_060_000,
                    open=100,
                    high=102,
                    low=99,
                    close=101,
                    volume=10,
                    ingested_at_ms=1_800_000_060_000,
                )
            ]

        mocked_fetch.side_effect = candle_for_symbol

        stored = collect_once(self.connection)

        rows = self.connection.execute(
            "SELECT symbol FROM candles ORDER BY symbol"
        ).fetchall()
        self.assertEqual(stored, len(BINANCE_SYMBOLS))
        self.assertEqual([row[0] for row in rows], sorted(BINANCE_SYMBOLS))

    def test_database_file_is_created_with_candle_table(self) -> None:
        # The open helper creates missing parent directories and schema.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "trading.db"
            connection = open_candle_database(path)
            try:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'candles'"
                ).fetchone()
            finally:
                connection.close()

            self.assertEqual(table, ("candles",))

    @patch("crypto_trader.data.binance.fetch_binance_server_time_ms")
    @patch("crypto_trader.data.binance.fetch_completed_minute_candles")
    def test_historical_backfill_writes_requested_window(
        self, mocked_fetch, mocked_server_time
    ) -> None:
        # Two minutes ending at the newest completed minute should be fetched
        # even though the normal collector's resume cursor is not involved.
        mocked_server_time.return_value = 1_800_000_120_000

        def historical_page(symbol, **kwargs):
            start = kwargs["start_time_ms"]
            return [
                Candle(
                    exchange="binance",
                    symbol=symbol,
                    interval_seconds=60,
                    open_time_ms=start,
                    open=100,
                    high=102,
                    low=99,
                    close=101,
                    volume=10,
                    ingested_at_ms=1_800_000_120_000,
                ),
                Candle(
                    exchange="binance",
                    symbol=symbol,
                    interval_seconds=60,
                    open_time_ms=start + 60_000,
                    open=101,
                    high=103,
                    low=100,
                    close=102,
                    volume=11,
                    ingested_at_ms=1_800_000_120_000,
                ),
            ]

        mocked_fetch.side_effect = historical_page

        stored = backfill_historical_candles(
            self.connection,
            symbols=("BTCUSDT",),
            minutes=2,
            request_limit=10,
        )

        self.assertEqual(stored, 2)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM candles").fetchone()[0],
            2,
        )
        self.assertEqual(mocked_fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
