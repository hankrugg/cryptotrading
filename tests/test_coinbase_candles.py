"""Tests for Coinbase live and historical candle collection."""

from io import BytesIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from crypto_trader.data.candles import Candle, create_candles_table
from crypto_trader.data.coinbase_candles import (
    COINBASE_PRODUCTS,
    CoinbaseCandleError,
    _calendar_years_ago_ms,
    backfill_historical_candles,
    collect_once,
    fetch_completed_candles,
    fetch_coinbase_server_time_ms,
    fetch_completed_minute_candles,
    normalize_products,
    open_candle_database,
)


class FakeResponse(BytesIO):
    """Small context-manager stand-in for urllib responses."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


def candle(open_time_ms: int, close: str = "101.5") -> dict[str, str]:
    return {
        "start": str(open_time_ms // 1_000),
        "low": "99.0",
        "high": "102.0",
        "open": "100.0",
        "close": close,
        "volume": "12.5",
    }


class CoinbaseFetchTests(unittest.TestCase):
    def test_normalize_products_validates_and_deduplicates(self) -> None:
        self.assertEqual(
            normalize_products((" btc-usd ", "ETH-USD", "BTC-USD")),
            ("BTC-USD", "ETH-USD"),
        )
        with self.assertRaisesRegex(ValueError, "BTC-USD"):
            normalize_products(("BTCUSD",))

    @patch("crypto_trader.data.coinbase_candles.urlopen")
    def test_reads_coinbase_server_time(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = FakeResponse(
            json.dumps(
                {
                    "iso": "2027-01-15T08:02:00Z",
                    "epochSeconds": "1800000120",
                    "epochMillis": "1800000120000",
                }
            ).encode()
        )

        self.assertEqual(fetch_coinbase_server_time_ms(), 1_800_000_120_000)
        request = mocked_urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/api/v3/brokerage/time"))

    @patch("crypto_trader.data.coinbase_candles.time.sleep")
    @patch("crypto_trader.data.coinbase_candles.urlopen")
    def test_retries_a_temporary_network_failure(
        self, mocked_urlopen, mocked_sleep
    ) -> None:
        mocked_urlopen.side_effect = [
            URLError("temporary failure"),
            FakeResponse(json.dumps({"epochMillis": "1800000120000"}).encode()),
        ]

        self.assertEqual(fetch_coinbase_server_time_ms(), 1_800_000_120_000)
        self.assertEqual(mocked_urlopen.call_count, 2)
        mocked_sleep.assert_called_once_with(1.0)

    @patch("crypto_trader.data.coinbase_candles.urlopen")
    def test_fetches_only_requested_completed_candles(self, mocked_urlopen) -> None:
        now_ms = 1_800_000_120_000
        payload = {
            # Coinbase may return newest first. The current 08:02 candle is
            # deliberately included in the fake response and must be ignored.
            "candles": [
                candle(1_800_000_120_000, close="102.0"),
                candle(1_800_000_060_000, close="101.75"),
                candle(1_800_000_000_000),
            ]
        }
        mocked_urlopen.return_value = FakeResponse(json.dumps(payload).encode())

        candles = fetch_completed_minute_candles(
            " btc-usd ",
            start_time_ms=1_800_000_000_000,
            end_time_ms=1_800_000_060_000,
            limit=2,
            server_time_ms=now_ms,
        )

        self.assertEqual(
            [item.open_time_ms for item in candles],
            [1_800_000_000_000, 1_800_000_060_000],
        )
        self.assertEqual(candles[-1].close, 101.75)
        self.assertEqual(candles[-1].symbol, "BTC-USD")
        self.assertEqual(candles[-1].exchange, "coinbase")
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("/market/products/BTC-USD/candles?", request.full_url)
        self.assertIn("granularity=ONE_MINUTE", request.full_url)
        self.assertIn("limit=2", request.full_url)
        self.assertIn("end=1800000060", request.full_url)

    @patch("crypto_trader.data.coinbase_candles.urlopen")
    def test_fetches_completed_hourly_candles(self, mocked_urlopen) -> None:
        hour_ms = 3_600_000
        server_time_ms = 1_800_003_600_000
        first_open_ms = 1_799_996_400_000
        mocked_urlopen.return_value = FakeResponse(
            json.dumps(
                {
                    "candles": [
                        candle(first_open_ms),
                        candle(first_open_ms + hour_ms),
                    ]
                }
            ).encode()
        )

        candles = fetch_completed_candles(
            "BTC-USD",
            granularity="ONE_HOUR",
            start_time_ms=first_open_ms,
            end_time_ms=first_open_ms + hour_ms,
            limit=2,
            server_time_ms=server_time_ms,
        )

        self.assertEqual(len(candles), 2)
        self.assertTrue(all(item.interval_seconds == 3_600 for item in candles))
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("granularity=ONE_HOUR", request.full_url)

    @patch("crypto_trader.data.coinbase_candles.urlopen")
    def test_api_error_response_is_reported(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = FakeResponse(
            json.dumps(
                {
                    "error": "INVALID_ARGUMENT",
                    "code": 3,
                    "message": "Unknown product",
                }
            ).encode()
        )

        with self.assertRaisesRegex(CoinbaseCandleError, "Unknown product"):
            fetch_completed_minute_candles(
                "NOT-REAL",
                server_time_ms=1_800_000_120_000,
            )


class CoinbaseCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        create_candles_table(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    @patch("crypto_trader.data.coinbase_candles.fetch_coinbase_server_time_ms")
    @patch("crypto_trader.data.coinbase_candles.fetch_completed_candles")
    def test_collects_all_configured_products(
        self, mocked_fetch, mocked_server_time
    ) -> None:
        mocked_server_time.return_value = 1_800_000_120_000

        def candle_for_product(product_id, **kwargs):
            return [
                Candle(
                    exchange="coinbase",
                    symbol=product_id,
                    interval_seconds=60,
                    open_time_ms=1_800_000_060_000,
                    open=100,
                    high=102,
                    low=99,
                    close=101,
                    volume=10,
                    ingested_at_ms=1_800_000_120_000,
                )
            ]

        mocked_fetch.side_effect = candle_for_product
        stored = collect_once(self.connection, request_limit=1)

        rows = self.connection.execute(
            "SELECT exchange, symbol FROM candles ORDER BY symbol"
        ).fetchall()
        self.assertEqual(stored, len(COINBASE_PRODUCTS))
        self.assertEqual(
            rows,
            [("coinbase", product) for product in sorted(COINBASE_PRODUCTS)],
        )

    def test_database_file_is_created_with_candle_table(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "trading.db"
            connection = open_candle_database(path)
            try:
                table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'candles'"
                ).fetchone()
            finally:
                connection.close()
        self.assertEqual(table, ("candles",))

    @patch("crypto_trader.data.coinbase_candles.fetch_coinbase_server_time_ms")
    @patch("crypto_trader.data.coinbase_candles.fetch_completed_candles")
    def test_historical_backfill_writes_requested_window(
        self, mocked_fetch, mocked_server_time
    ) -> None:
        mocked_server_time.return_value = 1_800_000_120_000

        def historical_page(product_id, **kwargs):
            start = kwargs["start_time_ms"]
            return [
                Candle(
                    exchange="coinbase",
                    symbol=product_id,
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
                    exchange="coinbase",
                    symbol=product_id,
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
            products=("BTC-USD",),
            minutes=2,
            request_limit=10,
            request_delay=0,
        )

        self.assertEqual(stored, 2)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM candles").fetchone()[0],
            2,
        )
        self.assertEqual(mocked_fetch.call_count, 1)

        stored_again = backfill_historical_candles(
            self.connection,
            products=("BTC-USD",),
            minutes=2,
            request_limit=10,
            request_delay=0,
        )
        self.assertEqual(stored_again, 0)
        self.assertEqual(mocked_fetch.call_count, 1)

    @patch("crypto_trader.data.coinbase_candles.fetch_coinbase_server_time_ms")
    @patch("crypto_trader.data.coinbase_candles.fetch_completed_candles")
    def test_historical_backfill_warns_and_keeps_source_gap(
        self, mocked_fetch, mocked_server_time
    ) -> None:
        mocked_server_time.return_value = 1_800_000_120_000
        mocked_fetch.return_value = [
            Candle(
                exchange="coinbase",
                symbol="BTC-USD",
                interval_seconds=60,
                open_time_ms=1_800_000_000_000,
                open=100,
                high=102,
                low=99,
                close=101,
                volume=10,
                ingested_at_ms=1_800_000_120_000,
            )
        ]

        with self.assertLogs(
            "crypto_trader.data.coinbase_candles", level="WARNING"
        ) as logs:
            stored = backfill_historical_candles(
                self.connection,
                products=("BTC-USD",),
                minutes=2,
                request_limit=10,
                request_delay=0,
            )

        self.assertEqual(stored, 1)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM candles").fetchone()[0],
            1,
        )
        self.assertIn("omitted 1 ONE_MINUTE candle", "\n".join(logs.output))
        self.assertIn("rather than inventing OHLCV data", "\n".join(logs.output))

    def test_calendar_year_window_preserves_month_day_and_hour(self) -> None:
        # 2026-10-09 19:00:00 UTC aligned to a one-hour candle.
        last_complete_open_ms = 1_791_572_400_000
        first_open_ms = _calendar_years_ago_ms(
            last_complete_open_ms,
            years=5,
            interval_ms=3_600_000,
        )

        self.assertEqual(first_open_ms, 1_633_806_000_000)


if __name__ == "__main__":
    unittest.main()
