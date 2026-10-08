"""Hourly strategy evaluation and scheduling."""

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
    owns_executor = executor is None
    if owns_executor:
        executor = PaperTradeExecutor.open()
    try:
        data = fetch_hourly()
        prices = pd.read_json(StringIO(data[SYMBOL]), orient="table")
        signals = sma_distance(prices, window=20, scale=10)
        current_signal = signals.iloc[-1]
        if pd.isna(current_signal):
            logger.warning("Current signal is NaN, skipping trade and email")
            return

        latest_price = float(prices.iloc[-1]["Close"])
        latest_index = prices.index[-1]
        latest_time = (
            latest_index.to_pydatetime()
            if hasattr(latest_index, "to_pydatetime")
            else latest_index
        )
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
        send_email_notification(
            current_signal,
            SYMBOL,
            connection=executor.connection,
        )
    finally:
        if owns_executor:
            executor.close()


def run_forever():
    logger.info("Crypto trader starting")
    executor = PaperTradeExecutor.open()
    try:
        while True:
            try:
                run_trading_signal(executor)
            except KeyboardInterrupt:
                logger.info("Crypto trader stopped by user")
                sys.exit(0)
            except Exception as e:
                logger.error(f"Error occurred: {e}")
            time.sleep(3600)
    finally:
        executor.close()
