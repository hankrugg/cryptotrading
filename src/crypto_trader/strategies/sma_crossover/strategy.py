"""SMA-distance target weights: -1 short, 0 cash, 1 long."""


def sma_distance(prices, window=20, scale=10):
    sma = prices["Close"].rolling(window).mean()
    return ((prices["Close"] / sma - 1) * scale).clip(-1, 1)
