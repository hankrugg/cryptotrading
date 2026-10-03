"""Simple long-only SMA crossover for one instrument."""

import math
import pandas as pd


class SMACrossover:
    def __init__(self, data: pd.DataFrame, fast=3, slow=8):
        self.data = data
        self.fast = fast
        self.slow = slow
        self.signals = pd.DataFrame()
        self.signals["signal"] = 0.0

    def calculate_sma(self):
        """Calculate the fast and slow simple moving averages."""
        self.signals["fast_sma"] = self.data["Close"].rolling(window=self.fast, min_periods=1).mean()
        self.signals["slow_sma"] = self.data["Close"].rolling(window=self.slow, min_periods=1).mean()
        return self.signals

    def generate_signals(self):
        """Generate buy/sell signals based on the SMA crossover."""
        self.signals["signal"] = 0.0
        self.signals.loc[
            self.signals["fast_sma"] > self.signals["slow_sma"], "signal"] = 1.0
        self.signals.loc[
            self.signals["fast_sma"] < self.signals["slow_sma"], "signal"] = -1.0
        return self.signals
