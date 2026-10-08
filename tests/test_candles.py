"""Tests for the normalized SQLite candle model."""

import sqlite3
import unittest

from crypto_trader.data import Candle, create_candles_table, fetch_hourly, upsert_candle


class CandleModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        create_candles_table(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    @staticmethod
    def candle(**changes) -> Candle:
        values = {
            "exchange": "coinbase",
            "symbol": "BTC-USD",
            "interval_seconds": 60,
            "open_time_ms": 1_799_193_600_000,
            "open": 60_000.0,
            "high": 60_100.0,
            "low": 59_900.0,
            "close": 60_050.0,
            "volume": 12.5,
            "ingested_at_ms": 1_799_193_661_000,
        }
        values.update(changes)
        return Candle(**values)

    def test_existing_market_data_import_is_preserved(self) -> None:
        self.assertTrue(callable(fetch_hourly))

    def test_insert_and_normalize_candle(self) -> None:
        candle = self.candle(exchange=" Coinbase ", symbol=" btc-usd ")
        candle_id = upsert_candle(self.connection, candle)

        row = self.connection.execute(
            "SELECT exchange, symbol, interval_seconds, close FROM candles WHERE id = ?",
            (candle_id,),
        ).fetchone()
        self.assertEqual(row, ("coinbase", "BTC-USD", 60, 60_050.0))

    def test_duplicate_natural_key_updates_one_row(self) -> None:
        first_id = upsert_candle(self.connection, self.candle())
        second_id = upsert_candle(
            self.connection,
            self.candle(close=60_075.0, volume=15.0, ingested_at_ms=1_799_193_662_000),
        )

        count, close, volume = self.connection.execute(
            "SELECT count(*), close, volume FROM candles"
        ).fetchone()
        self.assertEqual(first_id, second_id)
        self.assertEqual((count, close, volume), (1, 60_075.0, 15.0))

    def test_same_candle_time_can_exist_on_multiple_exchanges(self) -> None:
        upsert_candle(self.connection, self.candle())
        upsert_candle(
            self.connection,
            self.candle(exchange="binance_us", symbol="BTCUSDT"),
        )

        count = self.connection.execute("SELECT count(*) FROM candles").fetchone()[0]
        self.assertEqual(count, 2)

    def test_invalid_price_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.candle(high=59_950.0)

    def test_database_constraints_reject_negative_volume(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO candles (
                    exchange, symbol, interval_seconds, open_time_ms,
                    open, high, low, close, volume, ingested_at_ms
                ) VALUES ('coinbase', 'BTC-USD', 60, 1, 10, 11, 9, 10, -1, 2)
                """
            )


if __name__ == "__main__":
    unittest.main()
