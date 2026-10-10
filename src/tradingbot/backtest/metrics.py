"""Performance metrics on a daily equity series (pure functions).

- total return = last / first - 1 (first = equity at the close of the first session;
  the base for benchmarks is the starting capital, so pass the series with that point)
- annualized return = (1 + total) ** (252 / n_returns) - 1   (trading-day convention)
- Sharpe = mean(daily returns) / std(daily returns, ddof=1) * sqrt(252), risk-free rate 0
- max drawdown = max over t of 1 - equity[t] / max(equity[..t])
- exposure = mean of invested value / equity at each close
Trade stats use closed trades only (positions still open at the end are reported apart).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def total_return(equity: pd.Series) -> float:
    return float(equity.iloc[-1] / equity.iloc[0] - 1)


def annualized_return(equity: pd.Series) -> float:
    n = len(equity) - 1
    if n <= 0:
        return 0.0
    return float((1 + total_return(equity)) ** (TRADING_DAYS / n) - 1)


def sharpe_ratio(equity: pd.Series) -> float:
    """NaN when returns have no variance (e.g. always in cash)."""
    r = equity.pct_change().dropna()
    if len(r) < 2:
        return float("nan")
    sd = float(r.std(ddof=1))
    if not sd > 1e-12:
        return float("nan")
    return float(r.mean() / sd * math.sqrt(TRADING_DAYS))


def drawdown_series(equity: pd.Series) -> pd.Series:
    return 1 - equity / equity.cummax()


def max_drawdown(equity: pd.Series) -> float:
    return float(drawdown_series(equity).max())


@dataclass(frozen=True)
class Metrics:
    total_return: float
    annual_return: float
    sharpe: float
    max_drawdown: float
    exposure: float
    trades: int               # closed round trips
    win_rate: float           # share of closed trades with pnl > 0 (NaN if none)
    avg_holding_days: float   # calendar days, closed trades
    costs: float              # slippage + fees paid (all trades)
    open_trades: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_metrics(equity: pd.Series, exposure: pd.Series | None = None, trades: Sequence[Any] = (),
                    costs: float | None = None) -> Metrics:
    """`trades`: objects with pnl, holding_days, costs, is_open (backtest.engine.Trade)."""
    closed = [t for t in trades if not t.is_open]
    wins = [t for t in closed if t.pnl > 0]
    return Metrics(
        total_return=total_return(equity),
        annual_return=annualized_return(equity),
        sharpe=sharpe_ratio(equity),
        max_drawdown=max_drawdown(equity),
        exposure=float(exposure.mean()) if exposure is not None and len(exposure) else float("nan"),
        trades=len(closed),
        win_rate=len(wins) / len(closed) if closed else float("nan"),
        avg_holding_days=float(np.mean([t.holding_days for t in closed])) if closed else float("nan"),
        costs=float(costs if costs is not None else sum(t.costs for t in trades)),
        open_trades=len(trades) - len(closed),
    )


def yearly_returns(equity: pd.Series, start_value: float | None = None) -> pd.Series:
    """Calendar-year returns; the first year starts from `start_value` (default: first mark)."""
    year_end = equity.groupby(equity.index.year).last()
    prev = year_end.shift(1)
    prev.iloc[0] = start_value if start_value is not None else equity.iloc[0]
    return year_end / prev - 1
