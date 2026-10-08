"""Tests for Binance one-minute candle collection."""

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
    collect_once,
    fetch_completed_minute_candles,
    open_candle_database,
)
from crypto_trader.data.candles import Candle, create_candles_table


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


def kline(open_time_ms: int, close_time_ms: int, close: str = "101.5") -> list:
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
        payload = {"code": -1121, "msg": "Invalid symbol."}
        mocked_urlopen.return_value = FakeResponse(json.dumps(payload).encode())

        with self.assertRaisesRegex(BinanceMarketDataError, "Invalid symbol"):
            fetch_completed_minute_candles("NOTREAL", server_time_ms=1_800_000_120_000)


class BinanceCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        create_candles_table(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    @patch("crypto_trader.data.binance.fetch_binance_server_time_ms")
    @patch("crypto_trader.data.binance.fetch_completed_minute_candles")
    def test_collects_all_configured_symbols(
        self, mocked_fetch, mocked_server_time
    ) -> None:
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


if __name__ == "__main__":
    unittest.main()
