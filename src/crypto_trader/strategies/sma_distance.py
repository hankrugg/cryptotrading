"""SMA-distance target weights: -1 short, 0 cash, 1 long.

The returned number is a target allocation, not an order size. The executor
later converts its absolute value into a quantity using account equity and the
current price.
"""


def sma_distance(prices, window=20, scale=10):
    # The moving average uses only the current row and earlier rows. The first
    # ``window - 1`` rows therefore have no signal and become NaN.
    sma = prices["Close"].rolling(window).mean()

    # A close above its SMA produces a positive (long) target; below the SMA
    # produces a negative (short) target. Clipping prevents leverage beyond a
    # full long or full short allocation.
    return ((prices["Close"] / sma - 1) * scale).clip(-1, 1)
