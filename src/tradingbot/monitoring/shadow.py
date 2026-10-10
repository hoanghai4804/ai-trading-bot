"""Shadow check: replay the live run's decision through the BACKTEST code path.

Same as_of, bars, account state (equity, cash, positions, new positions this week), exits,
signal-window state, fresh reference prices and circuit-breaker status; the decision is
rebuilt by `backtest.engine.weekly_decision` with the configured strategy. The approved
(symbol, side) lists must match the live decision taken BEFORE execution (executor-added
stop_breached sells are not part of it).

Known, legitimate source of difference: the live gap check uses the RAW close of the last
session, the backtest the bars' (adjusted) close; they differ only when a dividend/split
goes ex between as_of and the run. A mismatch is reported (summary + alert), never acted on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import pandas as pd

from tradingbot.config import Config
from tradingbot.risk.circuit_breakers import BreakerStatus
from tradingbot.risk.manager import ApprovedOrder, RiskManager

OrderKey = tuple[str, str]   # (symbol, side)


@dataclass(frozen=True)
class ShadowResult:
    match: bool
    live: list[OrderKey]
    shadow: list[OrderKey]
    error: str | None = None

    @property
    def only_live(self) -> list[OrderKey]:
        return [k for k in self.live if k not in self.shadow]

    @property
    def only_shadow(self) -> list[OrderKey]:
        return [k for k in self.shadow if k not in self.live]

    def describe(self) -> str:
        if self.error:
            return f"ERROR ({self.error})"
        if self.match:
            return "OK (live == backtest path: " + (_fmt(self.live) or "no order") + ")"
        return f"MISMATCH live only: {_fmt(self.only_live) or '-'}; backtest only: {_fmt(self.only_shadow) or '-'}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "only_live": self.only_live, "only_shadow": self.only_shadow}


def _fmt(keys: list[OrderKey]) -> str:
    return ", ".join(f"{side} {symbol}" for symbol, side in keys)


def order_keys(orders: list[ApprovedOrder]) -> list[OrderKey]:
    return sorted((o.symbol, o.side) for o in orders)


def shadow_check(
    cfg: Config,
    live_orders: list[OrderKey],
    today: date,
    bars: Mapping[str, pd.DataFrame],
    positions: Mapping[str, float],
    last_exit: Mapping[str, date],
    last_trading_as_of: date | None,
    equity: float,
    cash: float,
    ref_prices: Mapping[str, float],
    breakers: BreakerStatus,
    new_positions_this_week: int,
) -> ShadowResult:
    """Never raises: an error in the replay is reported as such (and is a mismatch)."""
    from tradingbot.backtest.engine import fixed_strategy, weekly_decision   # engine imports pipeline

    live = sorted(live_orders)
    try:
        week = weekly_decision(cfg, RiskManager(cfg.risk, cfg.watchlist), fixed_strategy(cfg), today, bars,
                               positions, last_exit, last_trading_as_of, equity, cash, ref_prices, breakers,
                               new_positions_this_week)
    except Exception as exc:  # noqa: BLE001 - monitoring must not break the run
        return ShadowResult(False, live, [], error=f"{type(exc).__name__}: {exc}")
    shadow = order_keys(week.decision.approved)
    return ShadowResult(live == shadow, live, shadow)
