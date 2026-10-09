"""Crypto strategy research and paper-trading infrastructure.

The package has three independent jobs:

* collect one-minute Coinbase candles;
* calculate an hourly strategy signal and record a local paper trade; and
* calculate minute-by-minute risk snapshots.

Nothing in this package submits a live order to TradingView or an exchange.
"""

__version__ = "0.1.0"
