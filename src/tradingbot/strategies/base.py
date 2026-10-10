"""Strategy interface shared by live runs and the backtest.

A strategy only proposes; the risk manager sizes and may veto every proposal.
Strategies never see the broker, cash or equity.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd

Action = Literal["buy", "exit"]


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: Action
    as_of: date               # last session whose data was used
    reason: str
    cross_date: date | None = None          # session of the MA cross behind the signal
    sessions_since_cross: int | None = None  # 0 = the cross happened on the as_of session
    close: float | None = None
    fast_ma: float | None = None
    slow_ma: float | None = None


class Strategy(ABC):
    name: str

    @abstractmethod
    def generate_signals(
        self,
        bars: Mapping[str, pd.DataFrame],
        as_of: date,
        positions: Collection[str],
        last_exit: Mapping[str, date] | None = None,
    ) -> list[Signal]:
        """Return proposals using only rows dated <= `as_of`.

        bars: {symbol: OHLCV frame indexed by date}.
        positions: symbols currently held.
        last_exit: {symbol: date the bot last closed a position in it}.
        Order: exits first (alphabetical), then buys by priority.
        """

    @property
    @abstractmethod
    def min_history(self) -> int:
        """Bars needed before the strategy can emit any signal."""
