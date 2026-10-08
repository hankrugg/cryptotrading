# Crypto paper trading

Hourly strategy signals for manual TradingView paper trading. Requires Python 3.11+.

## Run

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
crypto-trader
```

`python -m crypto_trader` and `python -m crypto_trader.main` also work.
Set `GMAIL_APP_PASSWORD` in the repository-root `.env` file.
The application immediately fetches completed Yahoo hourly candles, evaluates
the SOL-USD SMA-distance target (window 20, scale 10), and emails the allocation.
Each hourly email now includes the latest minute risk snapshot and attaches the
complete trade log as a CSV file. It waits 3600 seconds after each iteration
before repeating.

Logs go to the console and `logs/crypto-trader.log`, with rotation. Override
the destination with `CRYPTO_TRADER_LOG_FILE` and verbosity with `LOG_LEVEL`.
Run from the repository root to keep relative log paths consistent.

## Structure

```text
data/
    database/            Local SQLite database and its working files (ignored by Git)
src/crypto_trader/
    __main__.py          Module entry point
    main.py              Environment and application startup
    runner.py            Hourly strategy evaluation and scheduling
    data/                Yahoo, Binance, and trade-record data models
    notifications.py     Email formatting and delivery
    logging_config.py    Console and rotating-file logging
    strategies/
        sma_distance.py  Reusable target-weight strategy
notebooks/
    SMA_Backtest.ipynb
    MeanReversion_Backtest.ipynb
```

The notebooks retain their experimental strategies, backtests, and metrics.
Experiment configuration, shared backtest/metrics modules, and Flask trade
entry are planned, not implemented. Orders are entered manually in TradingView;
this program produces signals and records local paper fills.

## TradingView trade records

The `trades` table mirrors the columns in a TradingView paper-trading CSV,
including entry/exit rows, order IDs, prices, quantities, PnL, returns,
commissions, and cumulative totals. `Trade.from_csv_row()` converts a CSV row
into the model, and `upsert_trade()` makes repeated imports safe. The table is
created with `create_trades_table()` in the same `trading.db` database used for
market candles.

The hourly runner now uses `PaperTradeExecutor` to record simulated fills at
the latest strategy candle close. It uses the SMA-distance signal as a target
portfolio weight from -1 (fully short) to +1 (fully long), matching the
backtest. Position quantity is calculated as `abs(weight) * equity / price`;
when the weight changes, the paper position is rebalanced. This is local paper
execution for the trade log; it does not submit an order to TradingView or an
exchange. Set `PAPER_INITIAL_EQUITY_USD` in `.env` to the same starting balance
used by `crypto-risk`.

## Minute risk monitoring

`crypto-risk` records one risk snapshot per minute in the `risk_snapshots`
table. It checks that positions are crypto, leverage stays below 2x for spot
and 5x for futures, non-liquid exposure stays below 20% of equity, total
delta stays below 2x equity, and drawdown stays below the 10% daily and 25%
maximum limits. A snapshot is marked `hedge_or_exit` at a policy violation and
`halt_until_month_end` at the maximum drawdown limit.

Set the actual starting paper-account balance and run it as a separate service:

```bash
crypto-risk --initial-equity 100000
```

The monitor reports and records risk status; it does not submit a hedge or
close an order automatically. That action should be wired to the paper
executor after the desired policy response is confirmed.

## Binance minute candles

The `crypto-candles` command stores completed one-minute candles for
`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, and `DOGEUSDT` in
`data/database/trading.db`. It uses Binance's public market-data API, so no
API key is required. The first run backfills up to 1,000 minutes per symbol;
later runs resume from the latest stored candle and fill any gap.

Run one collection cycle while testing:

```bash
crypto-candles --once
```

Run continuously on the Raspberry Pi:

```bash
crypto-candles
```

Use `--database /path/to/trading.db` to choose another SQLite file,
`--symbols BTCUSDT ETHUSDT` to collect a subset, or `--base-url` to select a
Binance-compatible public endpoint such as Binance.US. The collector stores
only closed candles and is safe to restart; its unique key prevents duplicate
rows.

## Database storage

Use `data/database/trading.db` for the SQLite database, relative to the
repository root. The collector creates the `candles` table on first run.

Git tracks only the directory's `.gitkeep` placeholder. Database files,
SQLite journal/WAL files, and backups placed here are ignored. Each checkout
(including the Pi) keeps its own data; Git updates do not transfer trades.
Back up the database separately using SQLite's backup facilities once it is
in use.

## Git workflow

Review and commit the structural refactor:

```bash
git status
git diff
git add README.md src/crypto_trader notebooks/SMA_Backtest.ipynb
git diff --cached
git commit -m "Refactor signal runner into focused modules"
git push
```

To update the Pi, stop the running application, then from its repository root:

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install -e .
```

Restart with the existing launch method. If Git reports local changes or
divergent history, resolve those before updating; do not discard local work.
Existing ignore rules exclude `.env`, `.venv`, logs, and generated data from
new commits. No credentials or runtime files are needed in the refactor commit.
