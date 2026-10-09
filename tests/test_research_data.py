"""Tests for the reusable Coinbase hourly research-data loader."""

import sqlite3
import unittest

from crypto_trader.data.candles import Candle, create_candles_table, upsert_candle
from crypto_trader.research.data import HourlyDataError, load_coinbase_hourly


class CoinbaseHourlyLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        create_candles_table(self.connection)
        first_hour = 1_800_000_000_000
        for product in ("BTC-USD", "ETH-USD"):
            for offset in (0, 1, 2):
                if product == "BTC-USD" and offset == 1:
                    continue
                self._insert(
                    exchange="coinbase",
                    product=product,
                    interval_seconds=3_600,
                    open_time_ms=first_hour + offset * 3_600_000,
                    close=100 + offset,
                )
        # These rows prove that exchange and interval filters remain isolated.
        self._insert(
            exchange="coinbase",
            product="BTC-USD",
            interval_seconds=60,
            open_time_ms=first_hour,
            close=999,
        )
        self._insert(
            exchange="binance",
            product="BTC-USD",
            interval_seconds=3_600,
            open_time_ms=first_hour,
            close=888,
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def _insert(
        self,
        *,
        exchange: str,
        product: str,
        interval_seconds: int,
        open_time_ms: int,
        close: float,
    ) -> None:
        upsert_candle(
            self.connection,
            Candle(
                exchange=exchange,
                symbol=product,
                interval_seconds=interval_seconds,
                open_time_ms=open_time_ms,
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=10,
                ingested_at_ms=open_time_ms + interval_seconds * 1_000,
            ),
        )

    def test_loads_only_requested_coinbase_hourly_rows(self) -> None:
        dataset = load_coinbase_hourly(
            self.connection,
            products=("BTC-USD", "ETH-USD"),
        )

        self.assertEqual(dataset.products, ("BTC-USD", "ETH-USD"))
        self.assertEqual(len(dataset.candles), 5)
        self.assertEqual(dataset.product("BTC-USD")["close"].tolist(), [100, 102])
        self.assertEqual(dataset.coverage().loc["BTC-USD", "missing_hours"], 1)

    def test_close_matrix_preserves_missing_source_hour(self) -> None:
        closes = load_coinbase_hourly(
            self.connection,
            products=("BTC-USD", "ETH-USD"),
        ).close_prices()

        self.assertEqual(len(closes), 3)
        self.assertTrue(closes.index.tz is not None)
        self.assertTrue(closes["BTC-USD"].isna().iloc[1])
        self.assertEqual(closes["ETH-USD"].tolist(), [100, 101, 102])

    def test_time_boundaries_are_inclusive_and_interpreted_as_utc(self) -> None:
        dataset = load_coinbase_hourly(
            self.connection,
            products=("ETH-USD",),
            start="2027-01-15 09:00:00",
            end="2027-01-15 09:00:00+00:00",
        )

        self.assertEqual(len(dataset.candles), 1)
        self.assertEqual(dataset.product("ETH-USD")["close"].iloc[0], 101)

    def test_missing_requested_product_is_reported(self) -> None:
        with self.assertRaisesRegex(HourlyDataError, "DOGE-USD"):
            load_coinbase_hourly(
                self.connection,
                products=("BTC-USD", "DOGE-USD"),
            )


if __name__ == "__main__":
    unittest.main()
