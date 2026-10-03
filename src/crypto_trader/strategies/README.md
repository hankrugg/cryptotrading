# Strategies

Each strategy gets its own directory and will eventually contain:

- a short design document;
- one Python implementation shared by notebooks, historical backtests, and scheduled signal generation;
- default parameters;
- strategy-specific tests.

Strategies produce proposed position changes. They do not send email, update the paper portfolio, or bypass the shared risk engine.

The backtest and scheduled runner must provide a strategy with completed bars in the same schema and use the same configuration. This prevents a separate "live" strategy from drifting away from the version that was tested.
