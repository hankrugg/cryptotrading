"""Reusable market-data and backtesting tools for strategy research."""

from crypto_trader.research.backtest import (
    BacktestError,
    BacktestResult,
    TradingCosts,
    run_backtest,
)
from crypto_trader.research.data import (
    CoinbaseHourlyData,
    HourlyDataError,
    load_coinbase_hourly,
)

__all__ = [
    "BacktestError",
    "BacktestResult",
    "CoinbaseHourlyData",
    "HourlyDataError",
    "TradingCosts",
    "load_coinbase_hourly",
    "run_backtest",
]
