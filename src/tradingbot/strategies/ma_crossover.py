"""Moving-average crossover (long-only).

Buy:  MA(fast) crossed above MA(slow) within the last `cross_lookback_sessions`
      sessions (cross on the as_of session counts as 0 sessions ago) and is still
      above, the symbol is not held, and the bot has not closed a position in it
      after the cross date (no second entry on the same old cross, e.g. after a stop).
Exit: symbol held and MA(fast) < MA(slow).

A cross at session t means fast[t] > slow[t] and fast[t-1] <= slow[t-1].
Buys are ordered most recent cross first, ties alphabetical (deterministic).
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingbot.data.market_data import cut_as_of
from tradingbot.indicators.technical import sma
from tradingbot.strategies.base import Signal, Strategy

log = logging.getLogger(__name__)


class MACrossoverParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fast: int = Field(ge=2)
    slow: int = Field(ge=3)
    cross_lookback_sessions: int = Field(ge=1)

    @model_validator(mode="after")
    def _fast_below_slow(self) -> MACrossoverParams:
        if self.fast >= self.slow:
            raise ValueError("fast must be < slow")
        return self


class MACrossover(Strategy):
    name = "ma_crossover"

    def __init__(self, params: MACrossoverParams | Mapping[str, Any]) -> None:
        self.params = params if isinstance(params, MACrossoverParams) else MACrossoverParams.model_validate(params)

    @property
    def min_history(self) -> int:
        return self.params.slow + 1  # one bar before the first slow MA to detect a cross

    def generate_signals(
        self,
        bars: Mapping[str, pd.DataFrame],
        as_of: date,
        positions: Collection[str],
        last_exit: Mapping[str, date] | None = None,
    ) -> list[Signal]:
        held = set(positions)
        last_exit = last_exit or {}
        exits: list[Signal] = []
        buys: list[Signal] = []
        for symbol in sorted(bars):
            frame = cut_as_of(bars[symbol], as_of)
            if len(frame) < self.min_history:
                log.warning("%s: %d bars < %d needed, no signal", symbol, len(frame), self.min_history)
                continue
            signal = self._evaluate(symbol, frame, symbol in held, last_exit.get(symbol))
            if signal is not None:
                (exits if signal.action == "exit" else buys).append(signal)
        buys.sort(key=lambda s: (s.sessions_since_cross, s.symbol))
        return exits + buys

    def _evaluate(self, symbol: str, frame: pd.DataFrame, held: bool, exited_on: date | None) -> Signal | None:
        p = self.params
        close = frame["close"]
        fast = sma(close, p.fast).to_numpy()
        slow = sma(close, p.slow).to_numpy()
        signal_date = frame.index[-1].date()
        common = dict(symbol=symbol, as_of=signal_date, close=float(close.iloc[-1]),
                      fast_ma=float(fast[-1]), slow_ma=float(slow[-1]))

        if held:
            if fast[-1] < slow[-1]:
                return Signal(action="exit", reason=f"MA{p.fast} < MA{p.slow}", **common)
            return None
        if not fast[-1] > slow[-1]:
            return None

        # Most recent up-cross; since fast > slow now, it starts the current "above" stretch.
        above = fast > slow
        valid = ~np.isnan(fast) & ~np.isnan(slow)
        crosses = np.flatnonzero(above[1:] & ~above[:-1] & valid[1:] & valid[:-1]) + 1
        if crosses.size == 0:
            return None  # above since the first comparable bar: cross date unknown
        cross_idx = int(crosses[-1])
        sessions_since = len(frame) - 1 - cross_idx
        if sessions_since >= p.cross_lookback_sessions:
            return None
        cross_date = frame.index[cross_idx].date()
        if exited_on is not None and exited_on > cross_date:
            log.info("%s: closed on %s after cross %s, no re-entry", symbol, exited_on, cross_date)
            return None
        return Signal(action="buy", reason=f"MA{p.fast} crossed above MA{p.slow} {sessions_since} session(s) ago",
                      cross_date=cross_date, sessions_since_cross=sessions_since, **common)
