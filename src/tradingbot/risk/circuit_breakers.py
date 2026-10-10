"""Account-level circuit breakers, checked once per run (not between runs).

- Weekly loss: the previous calendar week (Mon-Sun, New York dates) lost more than
  `weekly_loss_pct` -> no new positions this week. Exits are still allowed.
- Drawdown: equity is `max_drawdown_pct` or more below its peak -> close everything,
  write state/HALT.lock, and every later run stops until a human deletes the lock.

Equity histories are daily end-of-day series indexed by date. The weekly return uses
the broker's history (authoritative); the peak is the max over every source given,
including the current equity.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tradingbot.config import CircuitBreakerConfig
from tradingbot.safety import STATE_DIR, write_halt

# 1 - 90_000 / 100_000 = 0.09999999999999998: compare with a tolerance so the exact
# threshold counts as reached (drawdown) / not exceeded (weekly loss).
_EPS = 1e-9


def equity_series(points: Iterable[Any] | Mapping[date, float]) -> pd.Series:
    """Build a daily equity series from {date: equity} or objects with `.day` / `.equity`."""
    items = points.items() if isinstance(points, Mapping) else ((p.day, p.equity) for p in points)
    data = {pd.Timestamp(d): float(e) for d, e in items}
    # Explicit DatetimeIndex: an empty series (new account) must still compare with dates.
    return pd.Series(list(data.values()), index=pd.DatetimeIndex(list(data.keys())), dtype=float).sort_index()


def week_start(day: date) -> date:
    """Monday of the calendar (ISO) week containing `day`."""
    return day - timedelta(days=day.weekday())


def previous_week_return(history: pd.Series, today: date) -> float | None:
    """Return of the last full calendar week before `today`'s week, or None if unknown.

    End of a week = last equity value dated on/before that week's Sunday.
    Unknown when the account has no value before the previous week started
    (e.g. funded during it) or no value inside the previous week.
    """
    prev_end = pd.Timestamp(week_start(today) - timedelta(days=1))
    base_end = prev_end - pd.Timedelta(days=7)
    base = history[history.index <= base_end]
    prev = history[(history.index > base_end) & (history.index <= prev_end)]
    if base.empty or prev.empty or base.iloc[-1] <= 0:
        return None
    return float(prev.iloc[-1] / base.iloc[-1] - 1)


@dataclass(frozen=True)
class BreakerStatus:
    halt: bool                # drawdown breaker: liquidate + HALT.lock
    block_new_entries: bool   # true whenever halt is true
    reasons: tuple[str, ...]
    previous_week_return: float | None
    drawdown: float
    peak_equity: float
    current_equity: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_circuit_breakers(
    current_equity: float,
    today: date,
    cfg: CircuitBreakerConfig,
    broker_history: pd.Series,
    extra_histories: Iterable[pd.Series] = (),
) -> BreakerStatus:
    if current_equity <= 0:
        raise ValueError("current equity must be > 0")
    peak = current_equity
    for series in (broker_history, *extra_histories):
        if not series.empty:
            peak = max(peak, float(series.max()))
    drawdown = 1 - current_equity / peak
    week_ret = previous_week_return(broker_history, today)

    reasons = []
    halt = drawdown >= cfg.max_drawdown_pct - _EPS
    if halt:
        reasons.append(f"drawdown {drawdown:.2%} >= {cfg.max_drawdown_pct:.0%} "
                       f"(equity {current_equity:,.2f}, peak {peak:,.2f})")
    weekly = week_ret is not None and week_ret < -cfg.weekly_loss_pct - _EPS
    if weekly:
        reasons.append(f"previous calendar week return {week_ret:.2%} < -{cfg.weekly_loss_pct:.0%}")
    return BreakerStatus(
        halt=halt,
        block_new_entries=halt or weekly,
        reasons=tuple(reasons),
        previous_week_return=week_ret,
        drawdown=drawdown,
        peak_equity=peak,
        current_equity=current_equity,
    )


def trip_halt(status: BreakerStatus, state_dir: Path = STATE_DIR) -> Path:
    """Write state/HALT.lock for a tripped drawdown breaker."""
    if not status.halt:
        raise ValueError("drawdown breaker is not tripped")
    return write_halt(status.reasons[0], status.to_dict(), state_dir)
