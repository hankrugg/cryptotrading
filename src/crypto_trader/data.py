"""Download completed hourly crypto prices from Yahoo Finance."""

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "HYPE32196-USD", "DOGE-USD"]

def fetch_hourly(period="5d"):
    """Fetch hourly OHLCV for the requested period, excluding the unfinished hour."""
    prices = yf.download(
        SYMBOLS, period=period, interval="1h", auto_adjust=False,
        group_by="ticker", progress=False, threads=False,
    )
    cutoff = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    prices = prices.loc[prices.index < cutoff]
    result = {}
    for symbol in SYMBOLS:
        bars = prices[symbol].dropna(subset=["Open", "High", "Low", "Close"])
        if bars.empty:
            raise RuntimeError(f"Yahoo returned no completed prices for {symbol}")
        result[symbol] = bars.to_json(orient="table", date_format="iso")
    return result
