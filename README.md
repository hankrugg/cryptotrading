# Crypto paper trading

Python 3.11+, standard library only. Run from the project folder:

```console
python3 src/crypto_trader/main.py
```

One process hosts the trade-entry page and runs the hourly workflow:
collect completed Binance candles → SMA signals → portfolio/risk report → email.
It runs immediately, then at five minutes past every UTC hour. Ctrl+C stops it.

Open the web link printed at startup (use the Pi's IP from another device).
Record your actual TradingView fills there; use the same fill ID when retrying.
The page is intended for your trusted local network.

## Settings

- `configs/data.toml`: symbols, hourly feed, storage location.
- `configs/strategies/sma_crossover.toml`: 3/8-period crossover.
- `configs/risk.toml`: starting cash, assignment limits, 10% position target.
- `src/crypto_trader/crypto-trader.env`: private local email settings, ignored by Git.

The Pi service uses its private environment file instead.
See [Pi setup](deploy/raspberry-pi/README.md) and
[email and fill instructions](deploy/raspberry-pi/EMAIL.md).

## Code map

- `main.py`: commands and hourly workflow.
- `data/`: fetch, validate, archive, and store candles.
- `strategies/`: independent strategies; reusable by notebooks.
- `signals/`: evaluate and save decisions without duplicates.
- `portfolio/`: manual fills, cash and positions.
- `risk/`: valuation, assignment rules, drawdown history and trade sizes.
- `notifications/`: report formatting and SMTP delivery.
- `service/web.py`: trade-entry form.
- `scheduling/hourly.py`: Python clock loop.
- `common.py`: shared database setup and decimal serialization.
- `notebooks/`: exploration and backtest presentation.
- `tests/`: offline checks, including a temporary local web server.

## Useful commands

```console
python3 src/crypto_trader/main.py run-once
python3 src/crypto_trader/main.py status
python3 src/crypto_trader/main.py report
PYTHONPATH=src python3 -m unittest discover -s tests -t . -q
```

`run-once` includes email; `report` previews without sending.
The commands `collect`, `signal`, and `record-fill` remain available.

SQLite stores candles, fills, risk snapshots and delivery history. Existing
data survives restarts. Back up the configured data directory. No actual
orders are placed: execute paper trades in TradingView and report fills here.

The current portfolio supports cash-funded long Spot USDT pairs. Report fills
before the next hourly snapshot; historical reconciliation is not implemented.
Suggested sizes use the last completed close and recorded holdings. Hourly
drawdown checks and volume-based exit estimates cannot guarantee intrahour
limits or execution prices. A dedicated historical backtest engine is still
future work.
