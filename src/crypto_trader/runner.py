"""Hourly strategy evaluation and scheduling."""

from io import StringIO
import logging
import sys
import time
import pandas as pd

from crypto_trader.data import fetch_hourly
from crypto_trader.notifications import send_email_notification
from crypto_trader.strategies.sma_distance import sma_distance

SYMBOL = "SOL-USD"
logger = logging.getLogger(__name__)


def run_trading_signal():
    data = fetch_hourly()
    prices = pd.read_json(StringIO(data[SYMBOL]), orient="table")
    signals = sma_distance(prices, window=20, scale=10)
    current_signal = signals.iloc[-1]
    if pd.isna(current_signal):
        logger.warning("Current signal is NaN, skipping email notification")
        return
    logger.info(f"Current signal: {current_signal}")
    send_email_notification(current_signal, SYMBOL)


def run_forever():
    logger.info("Crypto trader starting")
    while True:
        try:
            run_trading_signal()
        except KeyboardInterrupt:
            logger.info("Crypto trader stopped by user")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Error occurred: {e}")
        time.sleep(3600)
