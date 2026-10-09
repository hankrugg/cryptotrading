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
    data/                Yahoo, Coinbase, and trade-record data models
        coinbase_candles.py Public live and historical candle collector
        coinbase_ticks.py  Continuous public trade/Level 2 collector
        rotating_writer.py Durable gzip-CSV rotation and book checkpoints
        upload.py          Independent rclone backup command
    notifications.py     Email formatting and delivery
    logging_config.py    Console and rotating-file logging
    research/
        data.py          Validated Coinbase hourly-data loader
        backtest.py      Causal fee-aware target-weight backtester
    strategies/
        sma_distance.py  Reusable target-weight strategy
notebooks/
    SMA_Backtest.ipynb
    MeanReversion_Backtest.ipynb
    Strategy_Comparison_Backtest.ipynb
    MultiAsset_Portfolio_Backtest.ipynb
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

## Coinbase candles

The `crypto-candles` command stores completed candles for
`BTC-USD`, `ETH-USD`, `SOL-USD`, `XRP-USD`, and `DOGE-USD` in
`data/database/trading.db`. It uses Coinbase Advanced Trade's public candle and
server-time endpoints, so no API key is required. One-minute collection remains
the default for the live service. Later runs resume from the latest stored
candle. Coinbase occasionally omits individual historical buckets. The
collector warns about those source gaps and continues without manufacturing a
zero-volume candle or a fake price.

Run one collection cycle while testing:

```bash
crypto-candles --once
```

Run continuously on the Raspberry Pi:

```bash
crypto-candles
```

For the medium-horizon strategy, backfill five calendar years of hourly candles
for BTC, ETH, SOL, and DOGE. The command retries temporary failures, paces API
requests, reports page progress, and skips complete pages when resumed:

```bash
crypto-candles --backfill-years 5 --granularity ONE_HOUR \
  --products BTC-USD ETH-USD SOL-USD DOGE-USD
```

The same operation can be run interactively in
`notebooks/Coinbase_Candle_Backfill_And_Inspection.ipynb`, which also displays
row counts, time coverage, recent candles, and the SQLite schema.

Use `--database /path/to/trading.db` to choose another SQLite file,
`--products BTC-USD ETH-USD` to collect a subset, or `--backfill-days` for a
shorter fixed window. `--symbols` remains an alias for `--products` during the
transition. The collector stores only closed candles and is safe to restart;
its unique key includes the interval and prevents duplicate rows. Hourly and
minute candles can therefore coexist in the same database.

## Reusable strategy research

The research layer loads only Coinbase hourly candles and leaves missing source
hours explicit. It accepts either the database path or an existing SQLite
connection. The backtest engine shifts every target by one bar to prevent
lookahead and requires an explicit maker/taker cost assumption.

```python
import pandas as pd

from crypto_trader.research import TradingCosts, load_coinbase_hourly, run_backtest

data = load_coinbase_hourly()
closes = data.close_prices()

# Replace this constant allocation with a strategy-generated target matrix.
targets = pd.DataFrame(0.25, index=closes.index, columns=closes.columns)

costs = TradingCosts(
    maker_fee_rate=0.005,
    taker_fee_rate=0.009,
    maker_fraction=1.0,
    slippage_rate=0.0002,
)
result = run_backtest(
    closes,
    targets,
    costs=costs,
    initial_equity=100_000,
    rebalance_threshold=0.05,
)
result.summary()
```

Costs are charged on actual executed turnover relative to the portfolio's
drifted weights. Reversing from a full long to a full short therefore creates
two units of turnover. Here, `rebalance_threshold=0.05` skips a rebalance unless
the sum of the required absolute weight changes exceeds 5%; this prevents tiny
hourly adjustments from creating unrealistic fee drag. By default, the final
open position is liquidated so net performance includes its exit cost. The
engine reports gross returns, exchange fees, slippage, net returns, turnover,
equity, drawdown, and an approximate round-trip count separately.

For a notebook/development environment, install the optional tools with:

```bash
python -m pip install -e ".[research,dev]"
```

## Database storage

Use `data/database/trading.db` for the SQLite database, relative to the
repository root. The collector creates the `candles` table on first run.

Git tracks only the directory's `.gitkeep` placeholder. Database files,
SQLite journal/WAL files, and backups placed here are ignored. Each checkout
(including the Pi) keeps its own data; Git updates do not transfer trades.
Back up the database separately using SQLite's backup facilities once it is
in use.

## Continuous Coinbase tick collection

The notebook example is intentionally small and keeps its rows in memory. The
continuous collector streams public Coinbase `market_trades`, `level2`, and
`heartbeats` messages for all five research products directly to compressed
files:

```bash
crypto-coinbase-ticks
```

The default products are `BTC-USD`, `ETH-USD`, `SOL-USD`, `DOGE-USD`, and
`XRP-USD`. No Coinbase credentials are required because the collector uses
only public market-data channels. Run a short, cleanly terminated smoke test
before installing the service:

```bash
crypto-coinbase-ticks --products BTC-USD SOL-USD --run-seconds 30
```

Data is partitioned by product and UTC date below `data/raw/coinbase/`. A
separate trade and Level 2 gzip-CSV file is finalized each hour. While a file
is open it ends in `.partial`; only a clean rotation or shutdown removes that
suffix. Every completed file has a JSON sidecar manifest containing its row
count, sequence range, compressed size, connection identifier, and SHA-256
digest.

```text
data/raw/coinbase/
    BTC-USD/
        2026-10-09/
            coinbase_BTC-USD_level2_20261009T140000Z_<connection>.csv.gz
            coinbase_BTC-USD_level2_20261009T140000Z_<connection>.csv.gz.manifest.json
            coinbase_BTC-USD_trades_20261009T140000Z_<connection>.csv.gz
```

The receive timestamp is captured before JSON decoding, book maintenance, and
disk writing. Level 2 rows retain exchange event time, Coinbase message time,
and local receipt time. Every reconnection has a new `connection_id`, requires
a fresh exchange snapshot, and starts new output files. A connection-wide
sequence gap also forces reconnection instead of continuing with a potentially
invalid book.

At an hourly boundary, the collector writes the current book into the new file
before the first update. These rows are explicitly identified by
`event_type=checkpoint` and `record_source=local_checkpoint`; they are not
Coinbase events. Treat the first checkpoint group like a snapshot when replaying
one hourly partition. Uninterrupted files that begin with Coinbase data instead
start with `event_type=snapshot` and `record_source=exchange`.

Install the collector on Raspberry Pi OS after cloning the repository:

```bash
./deploy/install-coinbase-collector-service.sh
sudo journalctl -u coinbase-tick-collector -f
```

The service starts at boot, restarts after network failures, handles SIGTERM as
a clean shutdown, and uses an advisory lock to prevent two collector instances
from writing simultaneously. A sudden power loss can leave `.partial` files;
they are retained for inspection and are never uploaded automatically.

## Google Drive tick-data backup

Google Drive backup is a separate process so an internet or Drive failure
cannot stop local collection. Install `rclone`, create a Google Drive remote
named `gdrive`, and test it as the same Linux user that will run the service:

```bash
rclone config
rclone lsd gdrive:
crypto-upload-ticks --remote gdrive:coinbase-data/raw
```

Rclone's shared Google client ID is being retired during 2026, so follow its
[Google Drive setup instructions](https://rclone.org/drive/) and create a
personal OAuth client ID for unattended use.

After the manual upload works, install the fifteen-minute systemd timer:

```bash
./deploy/install-data-upload-timer.sh gdrive:coinbase-data/raw
systemctl list-timers coinbase-data-upload.timer
```

The command-line uploader uses non-destructive `rclone copy` by default. The
installed Pi timer deliberately runs it with `--move` to conserve limited SD
card space. It includes only completed gzip files and manifests, refuses to
overwrite a different remote object, and removes an eligible local file only
after rclone successfully transfers and checks it. Active `.partial` files and
files from failed uploads remain local for the next retry. Consequently, the
Pi normally holds only the active hourly partitions and a small upload backlog,
but free space should still be monitored in case Drive is unavailable.

Useful service commands:

```bash
sudo systemctl status coinbase-tick-collector
sudo systemctl restart coinbase-tick-collector
sudo systemctl stop coinbase-tick-collector
sudo systemctl start coinbase-data-upload.service
sudo journalctl -u coinbase-data-upload --since today
```

## Future production considerations

The current Coinbase Level 2 work is suitable for collection, reconstruction,
and initial research. Revisit these timing details before using it as a live
trading system:

- Record `received_at_ns` immediately after receiving the raw WebSocket
  message, before JSON decoding or book calculations.
- Preserve separate `book_updated_at_ns`, `features_ready_at_ns`, and (when
  applicable) `order_sent_at_ns` timestamps. Do not overwrite the original
  receipt time or combine the stages into one timestamp.
- Measure actual processing-latency distributions under realistic load,
  including median, p95, p99, and worst-case delays. Avoid assuming that a
  fixed delay such as 5 ms accurately represents busy periods or backlogs.
- Use `features_ready_at_ns` or a later actionable timestamp in backtests so a
  strategy cannot act before its inputs would have been calculated. Add order
  submission and exchange latency when modeling fills.
- Do not add historical notebook replay time to market receipt timestamps.
  Offline replay reconstructs old data; a production process incrementally
  updates an already-live book and should measure that live processing time
  separately.

## Git workflow

Review and commit the structural refactor:

```bash
git status
git diff
git add README.md notebooks/README.md pyproject.toml deploy src/crypto_trader tests
git diff --cached
git commit -m "Add continuous Coinbase tick collector"
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
