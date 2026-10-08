"""Tests for local paper-trade execution and PnL accounting."""

from datetime import datetime, timezone
import sqlite3
import unittest

from crypto_trader.execution import PaperTradeExecutor


class PaperTradeExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        # Each test starts with a clean $100 paper account in memory.
        self.connection = sqlite3.connect(":memory:")
        self.executor = PaperTradeExecutor(
            self.connection,
            initial_equity_usd=100.0,
        )
        self.time = datetime(2026, 9, 9, 13, 39, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.connection.close()

    def test_signal_opens_once_and_zero_closes_with_pnl(self) -> None:
        # Same target is a no-op; zero target closes and realizes the gain.
        opened = self.executor.apply_signal(
            symbol="COINBASE:SOLUSD",
            signal=1.0,
            price=100.0,
            executed_at=self.time,
        )
        repeated = self.executor.apply_signal(
            symbol="COINBASE:SOLUSD",
            signal=1.0,
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
        # Reversing direction creates an exit for the old side and an entry for
        # the new side in one apply_signal call.
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

    def test_signal_weight_change_rebalances_quantity(self) -> None:
        # Moving from 50% to 100% target weight closes/reopens at new size.
        self.executor.apply_signal(
            symbol="COINBASE:SOLUSD",
            signal=0.5,
            price=100.0,
            executed_at=self.time,
        )
        self.executor.apply_signal(
            symbol="COINBASE:SOLUSD",
            signal=1.0,
            price=100.0,
            executed_at=self.time,
        )
        quantities = self.connection.execute(
            "SELECT trade_type, size_qty FROM trades ORDER BY id"
        ).fetchall()
        self.assertEqual(
            quantities,
            [("Entry long", 0.5), ("Exit long", 0.5), ("Entry long", 1.0)],
        )


if __name__ == "__main__":
    unittest.main()
