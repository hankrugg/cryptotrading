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
It waits 3600 seconds after each iteration before repeating.

Logs go to the console and `logs/crypto-trader.log`, with rotation. Override
the destination with `CRYPTO_TRADER_LOG_FILE` and verbosity with `LOG_LEVEL`.
Run from the repository root to keep relative log paths consistent.

## Structure

```text
src/crypto_trader/
    __main__.py          Module entry point
    main.py              Environment and application startup
    runner.py            Hourly strategy evaluation and scheduling
    data.py              Yahoo hourly price loading
    notifications.py     Email formatting and delivery
    logging_config.py    Console and rotating-file logging
    strategies/
        sma_distance.py  Reusable target-weight strategy
notebooks/
    SMA_Backtest.ipynb
    MeanReversion_Backtest.ipynb
```

The notebooks retain their experimental strategies, backtests, and metrics.
Experiment configuration, shared backtest/metrics modules, SQLite portfolio
tracking, and Flask trade entry are planned, not implemented. Orders are
entered manually in TradingView; this program only produces signals.

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
