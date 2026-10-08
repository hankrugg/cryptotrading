"""Download completed hourly crypto prices from Yahoo Finance.

This module is used by the hourly strategy runner. It is separate from the
Binance collector: the strategy currently reads Yahoo hourly bars, while the
risk monitor uses the locally stored Binance minute candles for marks.
"""

from datetime import datetime, timezone
import logging

import yfinance as yf

logger = logging.getLogger(__name__)

SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "HYPE32196-USD", "DOGE-USD"]


def fetch_hourly(period="5d"):
    """Fetch hourly OHLCV for the requested period, excluding the unfinished hour."""
    logger.info(f"Fetching hourly data for period: {period}")

    # Yahoo returns a multi-symbol DataFrame with one group of OHLCV columns
    # per ticker. auto_adjust=False keeps the reported close as the raw market
    # close rather than applying stock-style split/dividend adjustments.
    prices = yf.download(
        SYMBOLS, period=period, interval="1h", auto_adjust=False,
        group_by="ticker", progress=False, threads=False,
    )
    # Only use bars whose interval has finished. If the current time is 14:37,
    # the 14:00 bar is still forming, so the latest usable bar is 13:00.
    cutoff = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    prices = prices.loc[prices.index < cutoff]
    result = {}
    for symbol in SYMBOLS:
        # Drop incomplete rows before serializing each symbol independently.
        bars = prices[symbol].dropna(subset=["Open", "High", "Low", "Close"])
        if bars.empty:
            raise RuntimeError(f"Yahoo returned no completed prices for {symbol}")
        # The table-oriented JSON preserves the time index so the runner can
        # recover the candle timestamp when it records a simulated fill.
        result[symbol] = bars.to_json(orient="table", date_format="iso")
    logger.info(f"Fetched hourly data for period: {period}")
    return result
