# Notebooks

Notebooks are for explanation, charts, and final backtest presentation. Reusable data loading, strategy, risk, and metric logic belongs under `src/crypto_trader/` so notebook results can be reproduced and tested.

Suggested sequence:

1. `01_data_quality.ipynb`
2. `02_sma_backtest.ipynb`
3. `03_portfolio_backtest.ipynb`
4. `04_risk_report.ipynb`

`Database_Backfill_And_Inspection.ipynb` is an operational notebook rather
than a backtest. It runs a one-time Binance historical candle backfill and
shows database coverage, recent rows, and the SQLite schema.

`Strategy_Comparison_Backtest.ipynb` compares several target-weight strategies
with the same train/test and turnover accounting used by the existing
backtests.

`MultiAsset_Portfolio_Backtest.ipynb` reads the Binance minute-candle database
and evaluates a cross-sectional, volatility-weighted BTC/ETH/SOL/XRP/DOGE
portfolio with exposure diagnostics.
