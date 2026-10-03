# SMA crossover

One class takes a pandas DataFrame of completed candles, oldest first, with
Open and Close columns. Windows count rows: 3 and 8 hours for hourly data.

```python
from crypto_trader.strategies.sma_crossover import SMACrossover

strategy = SMACrossover(data, fast=3, slow=8)
result = strategy.backtest(initial_cash=10000, fee=0.001)
print(result[["signal", "trade", "equity", "drawdown"]])
print(strategy.latest_signal())
```

Backtest every row of a fresh Yahoo download, without saving anything:

```python
from io import StringIO
import pandas as pd
from crypto_trader.data import fetch_hourly

for symbol, table in fetch_hourly().items():
    data = pd.read_json(StringIO(table), orient="table")
    result = SMACrossover(data).backtest()
    print(symbol, result["equity"].iloc[-1])
```

BUY on an upward crossover; EXIT on a downward crossover; otherwise HOLD.
Warm-up rows are HOLD. Each backtest starts in cash, buys with all available
cash and sells the full position. Signals execute at the following candle's
open, so the final signal waits for another candle. Fees default to 0.1% per
execution, with no slippage. Final holdings are marked at the last close.
Return and drawdown columns are fractions.

For fresh data, create SMACrossover(updated_data) and call latest_signal().
With the defaults, provide at least nine completed candles. Each symbol's
backtest is independent, not a shared multi-coin portfolio.
