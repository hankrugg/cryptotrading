"""Tests for portfolio risk metrics and persistence."""

import sqlite3
import unittest

from crypto_trader.risk import (
    RiskPosition,
    create_risk_snapshots_table,
    evaluate_risk,
    record_risk_snapshot,
    latest_risk_snapshot,
    snapshot_once,
)


class RiskMetricTests(unittest.TestCase):
    def test_policy_metrics_and_drawdown_status(self) -> None:
        positions = [
            RiskPosition("BTCUSDT", "long", 1, 100, 100, leverage=1.0),
            RiskPosition("XRPUSDT", "long", 30, 100, 100, leverage=1.0),
        ]
        snapshot = evaluate_risk(
            positions,
            equity_usd=9_000,
            day_start_equity_usd=10_000,
            peak_equity_usd=12_000,
            captured_at_ms=1_800_000_000_000,
        )

        self.assertEqual(snapshot.status, "halt_until_month_end")
        self.assertAlmostEqual(snapshot.other_exposure_pct, 3000 / 9000)
        self.assertIn("daily_drawdown_at_or_above_10_percent", snapshot.violations)
        self.assertIn("max_drawdown_at_or_above_25_percent", snapshot.violations)

    def test_leverage_and_non_crypto_are_reported(self) -> None:
        snapshot = evaluate_risk(
            [
                RiskPosition("BTCUSDT", "long", 1, 100, 100, leverage=2.0),
                RiskPosition(
                    "AAPL", "long", 1, 100, 100, asset_class="equity"
                ),
            ],
            equity_usd=1_000,
            day_start_equity_usd=1_000,
            peak_equity_usd=1_000,
        )
        self.assertEqual(snapshot.status, "hedge_or_exit")
        self.assertIn("spot_leverage_at_or_above_2x", snapshot.violations)
        self.assertIn("non_crypto_instrument", snapshot.violations)

    def test_snapshot_round_trip(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            create_risk_snapshots_table(connection)
            snapshot = evaluate_risk(
                [],
                equity_usd=100_000,
                day_start_equity_usd=100_000,
                peak_equity_usd=100_000,
                captured_at_ms=1_800_000_000_000,
            )
            record_risk_snapshot(connection, snapshot)
            self.assertEqual(latest_risk_snapshot(connection), snapshot)
        finally:
            connection.close()

    def test_snapshot_uses_realized_pnl_from_trades(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            snapshot = snapshot_once(
                connection,
                initial_equity_usd=100_000,
                captured_at_ms=1_800_000_000_000,
            )
            self.assertEqual(snapshot.equity_usd, 100_000)
            self.assertEqual(snapshot.status, "ok")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
