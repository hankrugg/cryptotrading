"""Tests for local paper-trade execution."""

from datetime import datetime, timezone
import sqlite3
import unittest

from crypto_trader.execution import PaperTradeExecutor


class PaperTradeExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.executor = PaperTradeExecutor(self.connection)
        self.time = datetime(2026, 9, 9, 13, 39, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.connection.close()

    def test_signal_opens_once_and_zero_closes_with_pnl(self) -> None:
        opened = self.executor.apply_signal(
            symbol="COINBASE:SOLUSD",
            signal=1.0,
            price=100.0,
            executed_at=self.time,
        )
        repeated = self.executor.apply_signal(
            symbol="COINBASE:SOLUSD",
            signal=0.5,
            price=105.0,
            executed_at=self.time,
        )
        closed = self.executor.apply_signal(
            symbol="COINBASE:SOLUSD",
            signal=0.0,
            price=110.0,
            executed_at=self.time,
        )

        self.assertEqual(len(opened), 1)
        self.assertEqual(repeated, [])
        self.assertEqual(len(closed), 2)
        rows = self.connection.execute(
            "SELECT trade_type, net_pnl_usd FROM trades ORDER BY id"
        ).fetchall()
        self.assertEqual(rows, [("Entry long", 10.0), ("Exit long", 10.0)])

    def test_opposite_signal_closes_then_opens(self) -> None:
        self.executor.apply_signal(
            symbol="COINBASE:SOLUSD",
            signal=1.0,
            price=100.0,
            executed_at=self.time,
        )
        rows = self.executor.apply_signal(
            symbol="COINBASE:SOLUSD",
            signal=-1.0,
            price=90.0,
            executed_at=self.time,
        )
        self.assertEqual([trade.trade_type for trade in rows], ["Entry long", "Exit long", "Entry short"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM trades").fetchone()[0], 3
        )


if __name__ == "__main__":
    unittest.main()
