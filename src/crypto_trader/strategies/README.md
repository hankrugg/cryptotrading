# Strategies

Each small strategy lives in a named Python module. Strategies calculate
target weights; scheduling, notifications, and logging live outside them.

```python
from crypto_trader.strategies.sma_distance import sma_distance

weights = sma_distance(prices, window=20, scale=10)
```

Supply a chronological pandas DataFrame with a `Close` column. The result
is an aligned Series between -1 (short) and +1 (long), with NaN during warm-up.
This strategy measures distance from one moving average; it is not a
two-average crossover strategy.

Other experiments currently live in the notebooks. Shared configuration and
multi-strategy scheduling have not yet been implemented.
