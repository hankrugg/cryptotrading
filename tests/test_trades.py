"""Tests for CSV parsing, idempotent storage, and CSV export."""

import sqlite3
import unittest

from crypto_trader.data import (
    Trade,
    create_trades_table,
    export_trades_csv,
    upsert_trade,
)


CSV_ROW = {
    # This fixture matches the column names and value shapes in the user's
    # TradingView export.
    "Symbol": "COINBASE:BTCUSD",
    "Trade number": "1",
    "Type": "Exit short",
    "Date and time": "Sep 9, 2026, 13:41",
    "Order ID": "3511717172",
    "Signal": "3511717172",
    "Price": "78697.47",
    "Size (qty)": "1",
    "Size (value)": "78715.29",
    "Net PnL USD": "17.82",
    "Return %": "0.02",
    "Commission USD": "0",
    "Cumulative PnL USD": "17.82",
    "Cumulative PnL %": "0.02",
}


class TradeModelTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep trade tests isolated from the Pi's persistent database.
        self.connection = sqlite3.connect(":memory:")
        create_trades_table(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_builds_from_tradingview_csv_row(self) -> None:
        # CSV text is converted into a validated Trade dataclass.
        trade = Trade.from_csv_row(CSV_ROW, imported_at_ms=1_800_000_000_000)
        self.assertEqual(trade.symbol, "COINBASE:BTCUSD")
        self.assertEqual(trade.trade_number, 1)
        self.assertEqual(trade.net_pnl_usd, 17.82)

    def test_upsert_is_idempotent(self) -> None:
        # Same natural key updates PnL instead of creating a second row.
        trade = Trade.from_csv_row(CSV_ROW, imported_at_ms=1_800_000_000_000)
        first_id = upsert_trade(self.connection, trade)
        second_id = upsert_trade(
            self.connection,
            Trade.from_csv_row({**CSV_ROW, "Net PnL USD": "20"}, imported_at_ms=1_800_000_001_000),
        )
        row = self.connection.execute(
            "SELECT count(*), net_pnl_usd FROM trades"
        ).fetchone()
        self.assertEqual(first_id, second_id)
        self.assertEqual(row, (1, 20.0))

    def test_entry_and_exit_rows_are_distinct(self) -> None:
        # Entry and exit have different order IDs/types and therefore coexist.
        exit_trade = Trade.from_csv_row(CSV_ROW)
        entry_trade = Trade.from_csv_row(
            {**CSV_ROW, "Type": "Entry short", "Order ID": "3511711354"}
        )
        upsert_trade(self.connection, exit_trade)
        upsert_trade(self.connection, entry_trade)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM trades").fetchone()[0], 2
        )

    def test_rejects_negative_commission(self) -> None:
        # Negative fees are invalid even if the CSV parser can read the number.
        with self.assertRaises(ValueError):
            Trade.from_csv_row({**CSV_ROW, "Commission USD": "-1"})

    def test_exports_tradingview_csv_headers_and_rows(self) -> None:
        # The export must be directly usable as a TradingView-shaped log.
        upsert_trade(self.connection, Trade.from_csv_row(CSV_ROW))
        exported = export_trades_csv(self.connection)
        lines = exported.splitlines()
        self.assertEqual(lines[0], "Symbol,Trade number,Type,Date and time,Order ID,Signal,Price,Size (qty),Size (value),Net PnL USD,Return %,Commission USD,Cumulative PnL USD,Cumulative PnL %")
        self.assertIn("COINBASE:BTCUSD,1,Exit short", lines[1])


if __name__ == "__main__":
    unittest.main()
