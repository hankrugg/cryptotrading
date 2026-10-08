"""Hourly strategy evaluation and scheduling.

The runner does not place a live TradingView order. It fetches the latest
completed Yahoo candle, calculates the strategy target, records a local paper
fill in SQLite, and emails the result.
"""

from io import StringIO
import logging
import sys
import time
import pandas as pd

from crypto_trader.data import fetch_hourly
from crypto_trader.execution import PaperTradeExecutor
from crypto_trader.notifications import send_email_notification
from crypto_trader.strategies.sma_distance import sma_distance

SYMBOL = "SOL-USD"
TRADINGVIEW_SYMBOL = "COINBASE:SOLUSD"
logger = logging.getLogger(__name__)


def run_trading_signal(executor: PaperTradeExecutor | None = None):
    # Tests can provide an executor with an in-memory database. The normal
    # service passes one shared executor so the SQLite connection stays open
    # across hourly iterations.
    owns_executor = executor is None
    if owns_executor:
        executor = PaperTradeExecutor.open()
    try:
        # fetch_hourly removes the current unfinished candle before returning.
        data = fetch_hourly()

        # The strategy currently trades only SOL. The other Yahoo symbols are
        # fetched for the shared data helper but are not used by this runner.
        prices = pd.read_json(StringIO(data[SYMBOL]), orient="table")
        signals = sma_distance(prices, window=20, scale=10)
        current_signal = signals.iloc[-1]
        if pd.isna(current_signal):
            # There are not yet 20 completed bars, so no meaningful SMA exists.
            logger.warning("Current signal is NaN, skipping trade and email")
            return

        # The simulated fill uses the last completed candle's close. This is
        # the candle timestamp, not the wall-clock time when the email arrives.
        latest_price = float(prices.iloc[-1]["Close"])
        latest_index = prices.index[-1]
        latest_time = (
            latest_index.to_pydatetime()
            if hasattr(latest_index, "to_pydatetime")
            else latest_index
        )
        # apply_signal closes/reopens the paper position only when the target
        # direction or target weight changes; repeating a target is a no-op.
        trades = executor.apply_signal(
            symbol=TRADINGVIEW_SYMBOL,
            signal=float(current_signal),
            price=latest_price,
            executed_at=latest_time,
        )
        logger.info(
            "Signal %.4f at %.2f produced %d paper trade row(s)",
            current_signal,
            latest_price,
            len(trades),
        )
        logger.info("Current signal: %s", current_signal)
        # The email contains the target, the latest saved risk snapshot, and a
        # CSV export of every trade currently in the shared database.
        send_email_notification(
            current_signal,
            SYMBOL,
            connection=executor.connection,
        )
    finally:
        if owns_executor:
            executor.close()


def run_forever():
    # One connection is reused for the lifetime of the service. This keeps the
    # trade history in the same database across all hourly evaluations.
    logger.info("Crypto trader starting")
    executor = PaperTradeExecutor.open()
    try:
        while True:
            try:
                # The first evaluation happens immediately when the process
                # starts; the sleep occurs after the evaluation completes.
                run_trading_signal(executor)
            except KeyboardInterrupt:
                logger.info("Crypto trader stopped by user")
                sys.exit(0)
            except Exception as e:
                logger.error(f"Error occurred: {e}")
            # This is a 3600-second delay from the end of the previous run; it
            # is not synchronized to the top of the next clock hour.
            time.sleep(3600)
    finally:
        executor.close()
