"""Technical indicators as pure functions.

Every value at row t uses only rows <= t (backward-looking windows only),
so appending future data can never change a past value.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average; NaN until `window` observations exist."""
    if window < 1:
        raise ValueError("window must be >= 1")
    return series.rolling(window=window, min_periods=window).mean()


def true_range(bars: pd.DataFrame) -> pd.Series:
    """max(high - low, |high - prev_close|, |low - prev_close|); first row is high - low."""
    prev_close = bars["close"].shift(1)
    ranges = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - prev_close).abs(), (bars["low"] - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1, skipna=True)


def atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range with Wilder's smoothing.

    Seed: ATR[period-1] = mean of the first `period` true ranges.
    Then: ATR[t] = (ATR[t-1] * (period - 1) + TR[t]) / period.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    tr = true_range(bars).to_numpy(dtype=float)
    out = np.full(len(tr), np.nan)
    if len(tr) >= period:
        out[period - 1] = tr[:period].mean()
        for t in range(period, len(tr)):
            out[t] = (out[t - 1] * (period - 1) + tr[t]) / period
    return pd.Series(out, index=bars.index, name=f"atr{period}")


def daily_returns(close: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    """Simple close-to-close returns; first row is NaN."""
    return close.pct_change(fill_method=None)


def return_correlation(closes: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Pairwise correlation of daily returns over the last `lookback` returns.

    `closes` has one column per symbol and must already be cut at `as_of`.
    Pairs with fewer than `lookback` overlapping returns get NaN (treated by
    callers as "unknown", not as "uncorrelated").
    """
    returns = daily_returns(closes).iloc[-lookback:]
    return returns.corr(min_periods=lookback)
