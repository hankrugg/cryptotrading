"""Tests for causal fee-aware target-weight backtesting."""

import unittest

import pandas as pd

from crypto_trader.research.backtest import (
    BacktestError,
    TradingCosts,
    run_backtest,
)


class TradingCostsTests(unittest.TestCase):
    def test_blends_maker_taker_and_slippage_rates(self) -> None:
        costs = TradingCosts(
            maker_fee_rate=0.005,
            taker_fee_rate=0.009,
            maker_fraction=0.75,
            slippage_rate=0.0005,
        )

        self.assertAlmostEqual(costs.blended_fee_rate, 0.006)
        self.assertAlmostEqual(costs.total_rate, 0.0065)

    def test_rejects_invalid_cost_assumptions(self) -> None:
        with self.assertRaises(ValueError):
            TradingCosts(0.005, 0.009, maker_fraction=1.1)


class BacktestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = pd.date_range("2026-01-01", periods=4, freq="1h", tz="UTC")
        self.maker_costs = TradingCosts(
            maker_fee_rate=0.01,
            taker_fee_rate=0.02,
            maker_fraction=1.0,
        )

    def test_delays_signal_and_charges_entry_and_final_exit(self) -> None:
        prices = pd.Series([100.0, 110.0, 121.0, 121.0], index=self.index, name="BTC")
        targets = pd.Series([1.0, 1.0, 0.0, 0.0], index=self.index, name="BTC")

        result = run_backtest(prices, targets, costs=self.maker_costs)

        # The t=0 signal earns the t=0 -> t=1 return, never the earlier return.
        self.assertEqual(result.target_weights["BTC"].tolist(), [1.0, 1.0, 0.0])
        self.assertEqual(result.held_weights["BTC"].tolist(), [1.0, 1.0, 0.0])
        self.assertEqual(result.turnover["BTC"].tolist(), [1.0, 0.0, 1.0])
        self.assertAlmostEqual(result.gross_returns.iloc[0], 0.10)
        self.assertAlmostEqual(result.net_returns.iloc[0], 0.09)
        self.assertAlmostEqual(result.net_returns.iloc[-1], -0.01)
        self.assertAlmostEqual(result.net_equity.iloc[-1], 1.18701)

    def test_reversal_charges_two_units_of_turnover(self) -> None:
        prices = pd.Series([100.0, 100.0, 100.0, 100.0], index=self.index, name="BTC")
        targets = pd.Series([1.0, -1.0, -1.0, 0.0], index=self.index, name="BTC")

        result = run_backtest(
            prices,
            targets,
            costs=self.maker_costs,
            liquidate_at_end=False,
        )

        self.assertEqual(result.turnover["BTC"].tolist(), [1.0, 2.0, 0.0])
        self.assertEqual(result.fee_returns.tolist(), [0.01, 0.02, 0.0])

    def test_multi_asset_portfolio_enforces_gross_exposure(self) -> None:
        prices = pd.DataFrame(
            {"BTC": [100, 101, 102, 103], "ETH": [100, 99, 98, 97]},
            index=self.index,
            dtype=float,
        )
        invalid_targets = pd.DataFrame(
            {"BTC": [0.75] * 4, "ETH": [0.75] * 4},
            index=self.index,
        )

        with self.assertRaisesRegex(BacktestError, "gross exposure"):
            run_backtest(prices, invalid_targets, costs=self.maker_costs)

    def test_turnover_rebalances_from_drifted_asset_weights(self) -> None:
        prices = pd.DataFrame(
            {"BTC": [100, 110, 110, 110], "ETH": [100, 100, 100, 100]},
            index=self.index,
            dtype=float,
        )
        targets = pd.DataFrame(
            {"BTC": [0.5] * 4, "ETH": [0.5] * 4},
            index=self.index,
        )

        result = run_backtest(
            prices,
            targets,
            costs=self.maker_costs,
            liquidate_at_end=False,
        )

        self.assertAlmostEqual(result.turnover.iloc[0].sum(), 1.0)
        self.assertAlmostEqual(result.turnover.iloc[1].sum(), 1 / 21)

    def test_rebalance_threshold_leaves_small_weight_drift_in_place(self) -> None:
        prices = pd.DataFrame(
            {"BTC": [100, 110, 110, 110], "ETH": [100, 100, 100, 100]},
            index=self.index,
            dtype=float,
        )
        targets = pd.DataFrame(
            {"BTC": [0.5] * 4, "ETH": [0.5] * 4},
            index=self.index,
        )

        result = run_backtest(
            prices,
            targets,
            costs=self.maker_costs,
            rebalance_threshold=0.05,
            liquidate_at_end=False,
        )

        self.assertAlmostEqual(result.turnover.iloc[1].sum(), 0.0)
        self.assertAlmostEqual(result.held_weights.iloc[1]["BTC"], 11 / 21)
        self.assertAlmostEqual(result.held_weights.iloc[1]["ETH"], 10 / 21)

    def test_data_gap_liquidates_and_restarts_from_cash(self) -> None:
        index = self.index.append(
            pd.DatetimeIndex([self.index[-1] + pd.Timedelta(hours=2)])
        )
        prices = pd.Series([100, 100, 100, 100, 100], index=index, name="BTC")
        targets = pd.Series([1.0] * 5, index=index, name="BTC")

        result = run_backtest(
            prices,
            targets,
            costs=self.maker_costs,
            liquidate_at_end=False,
        )

        # The final row is not contiguous, so the prior segment exits and the
        # new segment enters from cash rather than carrying across the gap.
        self.assertEqual(result.turnover["BTC"].tolist(), [1.0, 0.0, 1.0, 1.0])

    def test_missing_hour_is_not_scored_as_a_normal_return(self) -> None:
        prices = pd.Series(
            [100.0, float("nan"), 120.0, 132.0], index=self.index, name="BTC"
        )
        targets = pd.Series([1.0, 1.0, 1.0, 1.0], index=self.index, name="BTC")

        result = run_backtest(prices, targets, costs=self.maker_costs)

        self.assertEqual(result.net_returns.index.tolist(), [self.index[3]])
        self.assertAlmostEqual(result.gross_returns.iloc[0], 0.10)

    def test_summary_reports_cost_and_risk_statistics(self) -> None:
        prices = pd.Series([100.0, 110.0, 121.0, 121.0], index=self.index, name="BTC")
        targets = pd.Series([1.0, 1.0, 0.0, 0.0], index=self.index, name="BTC")

        summary = run_backtest(prices, targets, costs=self.maker_costs).summary()

        self.assertEqual(summary["observations"], 3)
        self.assertAlmostEqual(summary["total_turnover"], 2.0)
        self.assertAlmostEqual(summary["approximate_round_trips"], 1.0)
        self.assertAlmostEqual(summary["fee_return_charged"], 0.02)
        self.assertEqual(summary["rebalance_threshold"], 0.0)
        self.assertTrue(summary["liquidated_at_end"])


if __name__ == "__main__":
    unittest.main()
