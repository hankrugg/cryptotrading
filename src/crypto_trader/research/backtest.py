"""Causal, fee-aware backtesting for hourly target-weight strategies."""

from __future__ import annotations

from dataclasses import dataclass
import math
import sys

import numpy as np
import pandas as pd

HOURS_PER_YEAR = 365.25 * 24


class BacktestError(ValueError):
    """Raised when prices or strategy weights cannot form a valid backtest."""


@dataclass(frozen=True, slots=True)
class TradingCosts:
    """One-way execution costs charged on every dollar of turnover.

    Rates are decimals: ``0.005`` is 0.50%. ``maker_fraction`` represents the
    expected share of executed notional receiving the maker rate. The remainder
    receives the taker rate. Slippage is applied to all executed notional.
    """

    maker_fee_rate: float
    taker_fee_rate: float
    maker_fraction: float
    slippage_rate: float = 0.0

    def __post_init__(self) -> None:
        for name in ("maker_fee_rate", "taker_fee_rate", "slippage_rate"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or value >= 1:
                raise ValueError(f"{name} must be finite and between 0 and 1")
        if not math.isfinite(self.maker_fraction) or not 0 <= self.maker_fraction <= 1:
            raise ValueError("maker_fraction must be between 0 and 1")

    @property
    def blended_fee_rate(self) -> float:
        """Expected exchange fee per dollar of one-way turnover."""

        return (
            self.maker_fraction * self.maker_fee_rate
            + (1 - self.maker_fraction) * self.taker_fee_rate
        )

    @property
    def total_rate(self) -> float:
        """Expected fee plus slippage per dollar of one-way turnover."""

        return self.blended_fee_rate + self.slippage_rate


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Aligned strategy states, costs, returns, and equity curves."""

    target_weights: pd.DataFrame
    held_weights: pd.DataFrame
    asset_returns: pd.DataFrame
    turnover: pd.DataFrame
    gross_returns: pd.Series
    fee_returns: pd.Series
    slippage_returns: pd.Series
    net_returns: pd.Series
    gross_equity: pd.Series
    net_equity: pd.Series
    initial_equity: float
    costs: TradingCosts
    rebalance_threshold: float
    liquidated_at_end: bool

    def summary(self) -> pd.Series:
        """Return the principal performance and cost diagnostics."""

        elapsed_years = max(
            (self.net_returns.index[-1] - self.net_returns.index[0]).total_seconds()
            / (365.25 * 24 * 60 * 60),
            1 / HOURS_PER_YEAR,
        )
        ending_multiple = float(self.net_equity.iloc[-1] / self.initial_equity)
        annualized_log_return = math.log(ending_multiple) / elapsed_years
        annualized_return = (
            math.expm1(annualized_log_return)
            if annualized_log_return <= math.log(sys.float_info.max)
            else float("inf")
        )
        volatility = float(self.net_returns.std(ddof=1))
        annualized_volatility = volatility * math.sqrt(HOURS_PER_YEAR)
        sharpe = (
            float(self.net_returns.mean() / volatility * math.sqrt(HOURS_PER_YEAR))
            if volatility > 0
            else float("nan")
        )
        drawdown = 1 - self.net_equity / self.net_equity.cummax()
        total_turnover = float(self.turnover.sum().sum())
        return pd.Series(
            {
                "start": self.net_returns.index[0],
                "end": self.net_returns.index[-1],
                "observations": len(self.net_returns),
                "total_return": ending_multiple - 1,
                "annualized_return": annualized_return,
                "annualized_volatility": annualized_volatility,
                "sharpe_ratio": sharpe,
                "max_drawdown": float(drawdown.max()),
                "total_turnover": total_turnover,
                "approximate_round_trips": total_turnover / 2,
                "fee_return_charged": float(self.fee_returns.sum()),
                "slippage_return_charged": float(self.slippage_returns.sum()),
                "rebalance_threshold": self.rebalance_threshold,
                "liquidated_at_end": self.liquidated_at_end,
                "ending_equity": float(self.net_equity.iloc[-1]),
            },
            name="backtest",
        )


def _as_frame(
    value: pd.Series | pd.DataFrame,
    *,
    name: str,
) -> pd.DataFrame:
    if isinstance(value, pd.Series):
        column = value.name or "asset"
        return value.rename(column).to_frame()
    if isinstance(value, pd.DataFrame):
        return value.copy()
    raise TypeError(f"{name} must be a pandas Series or DataFrame")


def _validate_inputs(
    prices: pd.DataFrame,
    targets: pd.DataFrame,
    *,
    allow_short: bool,
    max_gross_exposure: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not isinstance(prices.index, pd.DatetimeIndex):
        raise BacktestError("prices must use a DatetimeIndex")
    if prices.empty:
        raise BacktestError("prices cannot be empty")
    if not prices.index.is_monotonic_increasing or not prices.index.is_unique:
        raise BacktestError("price timestamps must be unique and increasing")
    if prices.columns.has_duplicates:
        raise BacktestError("price columns must be unique")
    if not targets.index.equals(prices.index):
        raise BacktestError("target weights must have the same index as prices")
    if set(targets.columns) != set(prices.columns):
        raise BacktestError("target weights must have the same assets as prices")
    targets = targets.reindex(columns=prices.columns)
    if max_gross_exposure <= 0 or not math.isfinite(max_gross_exposure):
        raise ValueError("max_gross_exposure must be finite and positive")

    try:
        prices = prices.astype(float)
        targets = targets.astype(float)
    except (TypeError, ValueError) as error:
        raise BacktestError("prices and target weights must be numeric") from error

    price_values = prices.to_numpy()
    finite_prices = price_values[np.isfinite(price_values)]
    if (
        finite_prices.size == 0
        or np.isinf(price_values).any()
        or (finite_prices <= 0).any()
    ):
        raise BacktestError("prices must be finite and positive where present")
    target_values = targets.to_numpy()
    finite_targets = target_values[np.isfinite(target_values)]
    if np.isinf(target_values).any():
        raise BacktestError("target weights must be finite where present")
    if not allow_short and (finite_targets < 0).any():
        raise BacktestError("negative target weights require allow_short=True")
    if (abs(finite_targets) > max_gross_exposure + 1e-12).any():
        raise BacktestError("an individual target exceeds max_gross_exposure")
    complete_targets = targets.dropna(how="any")
    if (complete_targets.abs().sum(axis=1) > max_gross_exposure + 1e-12).any():
        raise BacktestError("target portfolio gross exposure exceeds its limit")
    return prices, targets


def run_backtest(
    close_prices: pd.Series | pd.DataFrame,
    target_weights: pd.Series | pd.DataFrame,
    *,
    costs: TradingCosts,
    initial_equity: float = 1.0,
    allow_short: bool = True,
    max_gross_exposure: float = 1.0,
    rebalance_threshold: float = 0.0,
    liquidate_at_end: bool = True,
) -> BacktestResult:
    """Backtest close-generated target weights without lookahead.

    A target observed at hour ``t`` is first eligible for the return from ``t``
    to ``t+1``. The portfolio trades to that target only when the required total
    absolute weight change exceeds ``rebalance_threshold``; otherwise its
    drifted weights remain in place. Executed turnover incurs one-way maker/
    taker fees and slippage according to ``costs``. The portfolio is optionally
    liquidated on the final scored close so reported net PnL includes an exit
    cost.

    The calculation treats target weights as desired fractions of equity and
    calculates rebalancing turnover against the previous bar's drifted ending
    weights. A break in hourly price coverage closes the prior segment and
    restarts from cash, avoiding an unobserved return across the source gap. It
    does not yet model order-book fills, borrowing, funding, or market impact.
    """

    if not isinstance(costs, TradingCosts):
        raise TypeError("costs must be a TradingCosts instance")
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("initial_equity must be finite and positive")
    if not math.isfinite(rebalance_threshold) or rebalance_threshold < 0:
        raise ValueError("rebalance_threshold must be finite and non-negative")

    prices = _as_frame(close_prices, name="close_prices")
    targets = _as_frame(target_weights, name="target_weights")
    prices, targets = _validate_inputs(
        prices,
        targets,
        allow_short=allow_short,
        max_gross_exposure=max_gross_exposure,
    )

    asset_returns = prices.pct_change(fill_method=None)
    held_weights = targets.shift(1)
    valid = asset_returns.notna().all(axis=1) & held_weights.notna().all(axis=1)
    if not valid.any():
        raise BacktestError("no scorable rows remain after warm-up and missing data")

    scored_returns = asset_returns.loc[valid]
    desired_weights = held_weights.loc[valid]

    # Simulate the held state sequentially. This is necessary because skipping
    # a small rebalance means the next bar begins from drifted actual weights,
    # not from the unchanged desired target. NumPy arrays keep this stateful
    # loop fast enough for repeated multi-year research runs.
    index = desired_weights.index
    columns = prices.columns
    desired_values = desired_weights.to_numpy()
    return_values = scored_returns.to_numpy()
    held_values = np.zeros_like(desired_values)
    ending_values = np.zeros_like(desired_values)
    turnover_values = np.zeros_like(desired_values)
    gross_values = np.zeros(len(index))
    cash_weights = np.zeros(len(columns))
    previous_time: pd.Timestamp | None = None
    previous_ending: np.ndarray | None = None
    for row_number, timestamp in enumerate(index):
        begins_segment = (
            previous_time is None or timestamp - previous_time != pd.Timedelta(hours=1)
        )
        if begins_segment:
            if previous_time is not None and previous_ending is not None:
                turnover_values[row_number - 1] += np.abs(previous_ending)
            pretrade = cash_weights
        else:
            assert previous_ending is not None
            pretrade = previous_ending

        desired = desired_values[row_number]
        required = np.abs(desired - pretrade)
        if float(required.sum()) > rebalance_threshold:
            held = desired
            turnover_values[row_number] = required
        else:
            held = pretrade
        held_values[row_number] = held

        returns = return_values[row_number]
        gross_return = float(np.dot(held, returns))
        if gross_return <= -1:
            raise BacktestError(f"portfolio loses at least 100% at {timestamp}")
        gross_values[row_number] = gross_return
        previous_ending = held * (1 + returns) / (1 + gross_return)
        ending_values[row_number] = previous_ending
        previous_time = timestamp

    if liquidate_at_end:
        turnover_values[-1] += np.abs(ending_values[-1])

    scored_held = pd.DataFrame(held_values, index=index, columns=columns)
    turnover = pd.DataFrame(turnover_values, index=index, columns=columns)
    gross_returns = pd.Series(gross_values, index=index, name="gross_return")

    total_turnover = turnover.sum(axis=1)
    fee_returns = total_turnover * costs.blended_fee_rate
    slippage_returns = total_turnover * costs.slippage_rate
    net_returns = gross_returns - fee_returns - slippage_returns
    if (net_returns <= -1).any():
        timestamp = net_returns.index[net_returns <= -1][0]
        raise BacktestError(f"portfolio loses at least 100% at {timestamp}")

    gross_equity = initial_equity * (1 + gross_returns).cumprod()
    net_equity = initial_equity * (1 + net_returns).cumprod()
    for series, name in (
        (gross_returns, "gross_return"),
        (fee_returns, "fee_return"),
        (slippage_returns, "slippage_return"),
        (net_returns, "net_return"),
        (gross_equity, "gross_equity"),
        (net_equity, "net_equity"),
    ):
        series.name = name

    return BacktestResult(
        target_weights=desired_weights,
        held_weights=scored_held,
        asset_returns=scored_returns,
        turnover=turnover,
        gross_returns=gross_returns,
        fee_returns=fee_returns,
        slippage_returns=slippage_returns,
        net_returns=net_returns,
        gross_equity=gross_equity,
        net_equity=net_equity,
        initial_equity=initial_equity,
        costs=costs,
        rebalance_threshold=rebalance_threshold,
        liquidated_at_end=liquidate_at_end,
    )
