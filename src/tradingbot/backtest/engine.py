"""Weekly backtest engine: replays the live Monday run, session by session.

Shared with live runs (no backtest-only copy of any rule): the Strategy classes, the
signal-window rule (`pipeline.signal_window`), `evaluate_circuit_breakers`, and
`RiskManager.review` (sizing, stop, gap check, correlation, weekly limit, cash buffer).

Calendar: sessions = union of the bars' dates. The run happens on Monday sessions only
(the cloud cron runs on Mondays). A Monday holiday means no trading that week; the next
run's buy-signal window then widens exactly like live (sessions since the as_of of the
last run that traded, clamped to [cross_lookback_sessions, max_signal_window_sessions]).

Each session d, in this order:
 1. Stops gapped at the open: open <= stop -> sold at the OPEN (`stop_gap`).
 2. Monday run: as_of = d - 1 day, bars cut to [as_of - data.history_days, as_of] (the
    slice live runs load) -> strategy -> circuit breakers (daily equity marks of the last
    year before d, like Alpaca's 1A portfolio history, + equity at d's open) -> risk
    manager with d's OPEN as reference price and no raw closes (the gap check uses the
    bars' close). Approved sells, then buys, fill at the open with costs. The stop is
    the risk manager's (ref = open) and is never moved.
 3. Intraday stops: low <= stop -> sold at the STOP price (`stop`), entry day included.
 4. Equity marked at the close (last known close if a symbol has no bar that day).

Drawdown halt: the risk manager sells everything and the event is recorded. With
halt_mode="continue" trading resumes the next week and the drawdown peak restarts at
the halt session (as if a human reviewed and deleted HALT.lock); with "stop" the
account stays in cash until the end.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import numpy as np
import pandas as pd

from tradingbot.backtest.costs import CostModel
from tradingbot.config import Config, RiskConfig
from tradingbot.pipeline import signal_window
from tradingbot.risk.circuit_breakers import evaluate_circuit_breakers
from tradingbot.risk.manager import AccountState, RiskDecision, RiskManager
from tradingbot.strategies.base import Signal, Strategy

# Live runs read the broker's daily portfolio history with period "1A".
BROKER_HISTORY_DAYS = 365

StrategyFactory = Callable[[date, int], Strategy]   # (run session, signal window) -> strategy
HaltMode = Literal["continue", "stop"]


@dataclass
class Trade:
    symbol: str
    entry_date: date
    ref_price: float          # open of the entry session (risk manager reference)
    entry_price: float        # fill incl. slippage
    qty: int
    stop: float
    entry_reason: str
    entry_slippage: float
    exit_date: date | None = None
    exit_price: float | None = None   # fill incl. slippage; last close for open trades
    exit_reason: str = "open"
    exit_fees: float = 0.0
    exit_slippage: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.exit_reason == "open"

    @property
    def costs(self) -> float:
        return self.entry_slippage + self.exit_slippage + self.exit_fees

    @property
    def pnl(self) -> float:
        return self.qty * ((self.exit_price or self.entry_price) - self.entry_price) - self.exit_fees

    @property
    def return_pct(self) -> float:
        return self.pnl / (self.qty * self.entry_price)

    @property
    def holding_days(self) -> int:
        return ((self.exit_date or self.entry_date) - self.entry_date).days


@dataclass(frozen=True)
class WeekRecord:
    session: date
    as_of: date
    window: int
    signals: list[Signal]
    decision: RiskDecision


@dataclass(frozen=True)
class BreakerEvent:
    session: date
    kind: str                 # drawdown_halt | weekly_loss
    equity: float
    peak: float
    drawdown: float
    previous_week_return: float | None
    positions_sold: tuple[str, ...]
    detail: str


@dataclass
class BacktestResult:
    name: str
    equity: pd.Series         # daily close marks; first point = starting capital the session before
    exposure: pd.Series       # invested value / equity at the close
    trades: list[Trade]
    weeks: list[WeekRecord]
    events: list[BreakerEvent]
    skipped_mondays: list[date] = field(default_factory=list)   # holiday Mondays (no run)
    notes: list[str] = field(default_factory=list)

    @property
    def costs_paid(self) -> float:
        return sum(t.costs for t in self.trades)

    def blocked_counts(self) -> Counter[str]:
        return Counter(b.code for w in self.weeks for b in w.decision.blocked)


class _Market:
    """Per-symbol OHLC arrays aligned on the session calendar (NaN where a symbol has no bar)."""

    def __init__(self, bars: Mapping[str, pd.DataFrame], sessions: pd.DatetimeIndex) -> None:
        self.sessions = sessions
        self.open, self.low, self.close = {}, {}, {}
        for s, frame in bars.items():
            f = frame.reindex(sessions)
            self.open[s] = f["open"].to_numpy(float)
            self.low[s] = f["low"].to_numpy(float)
            self.close[s] = f["close"].to_numpy(float)


def trading_sessions(bars: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex([])
    for frame in bars.values():
        index = index.union(frame.index)
    return index.sort_values()


def run_backtest(
    cfg: Config,
    bars: Mapping[str, pd.DataFrame],
    start: date,
    end: date,
    strategy_for: StrategyFactory,
    risk: RiskConfig | None = None,
    initial_capital: float | None = None,
    halt_mode: HaltMode = "continue",
    name: str = "strategy",
) -> BacktestResult:
    """Trade from the first session >= start to the last session <= end.

    `bars` may hold history before `start` (indicator warm-up). Decisions on a Monday d
    only use rows dated <= d - 1 day plus d's open; d's low and close are only used for
    stops and the end-of-day mark, after the decision.
    """
    risk = risk or cfg.risk
    costs = CostModel(cfg.costs)
    manager = RiskManager(risk, cfg.watchlist)
    all_sessions = trading_sessions(bars)
    market = _Market(bars, all_sessions)
    lo, hi = all_sessions.searchsorted(pd.Timestamp(start)), all_sessions.searchsorted(pd.Timestamp(end), "right")
    if lo >= hi:
        raise ValueError(f"no session between {start} and {end}")
    min_window = int(cfg.strategy.params.get("cross_lookback_sessions", 1))
    history_days = timedelta(days=cfg.data.history_days)

    cash = float(initial_capital if initial_capital is not None else cfg.backtest.initial_capital)
    positions: dict[str, Trade] = {}
    trades: list[Trade] = []
    last_exit: dict[str, date] = {}
    last_close: dict[str, float] = {}
    marks_dates: list[pd.Timestamp] = []
    marks: list[float] = []
    exposure: list[float] = []
    weeks: list[WeekRecord] = []
    events: list[BreakerEvent] = []
    skipped: list[date] = []
    notes: list[str] = []
    last_trading_as_of: date | None = None
    peak_since: date | None = None
    halted = False

    def close_position(sym: str, base_price: float, day: date, reason: str) -> None:
        nonlocal cash
        trade = positions.pop(sym)
        fill = costs.sell(base_price, trade.qty)
        cash += trade.qty * fill.price - fill.fees
        trade.exit_date, trade.exit_price, trade.exit_reason = day, fill.price, reason
        trade.exit_fees, trade.exit_slippage = fill.fees, fill.slippage
        last_exit[sym] = day

    previous_session: date | None = None
    for i in range(lo, hi):
        ts = all_sessions[i]
        day = ts.date()
        # Monday holiday between the previous session and this one: no run that week.
        if previous_session is not None:
            gap_monday = previous_session + timedelta(days=(7 - previous_session.weekday()) % 7 or 7)
            if gap_monday < day and gap_monday.weekday() == 0:
                skipped.append(gap_monday)
        previous_session = day

        # 1. stops gapped at the open
        for sym in sorted(positions):
            o = market.open[sym][i]
            if np.isfinite(o) and o <= positions[sym].stop:
                close_position(sym, o, day, "stop_gap")

        # 2. Monday run
        if ts.weekday() == 0 and not halted:
            as_of = day - timedelta(days=1)
            symbols = list(dict.fromkeys([*cfg.watchlist, *sorted(positions)]))
            window_start = pd.Timestamp(as_of - history_days)
            window_bars = {s: bars[s].loc[window_start:pd.Timestamp(as_of)] for s in symbols if s in bars}
            window = signal_window(window_bars, as_of, last_trading_as_of, min_window,
                                   cfg.schedule.max_signal_window_sessions)
            strategy = strategy_for(day, window)
            signals = strategy.generate_signals(window_bars, as_of, positions=list(positions), last_exit=last_exit)

            opens = {s: float(market.open[s][i]) for s in symbols if np.isfinite(market.open[s][i])}
            equity_now = cash + sum(t.qty * opens.get(s, last_close.get(s, t.entry_price))
                                    for s, t in positions.items())
            history = pd.Series(marks, index=pd.DatetimeIndex(marks_dates), dtype=float)
            history = history[history.index > pd.Timestamp(day - timedelta(days=BROKER_HISTORY_DAYS))]
            breakers = evaluate_circuit_breakers(equity_now, day, risk.circuit_breakers, history,
                                                 peak_since=peak_since)
            wanted = {s.symbol for s in signals if s.action == "buy"} | set(positions)
            account = AccountState(equity=equity_now, cash=cash,
                                   positions={s: float(t.qty) for s, t in positions.items()},
                                   new_positions_this_week=0)   # Monday-only runs: no earlier buy this week
            decision = manager.review(signals, as_of, account, window_bars,
                                      {s: p for s, p in opens.items() if s in wanted}, breakers)
            weeks.append(WeekRecord(day, as_of, window, signals, decision))

            sold = []
            for order in (o for o in decision.approved if o.side == "sell"):
                if order.symbol not in opens:
                    notes.append(f"{day}: no open price for {order.symbol}, sell postponed")
                    continue
                close_position(order.symbol, opens[order.symbol], day, "drawdown_halt" if breakers.halt else "signal")
                sold.append(order.symbol)
            for order in (o for o in decision.approved if o.side == "buy"):
                fill = costs.buy(order.ref_price, order.qty)
                cash -= order.qty * fill.price
                trade = Trade(order.symbol, day, order.ref_price, fill.price, int(order.qty), order.stop_price,
                              order.reason, fill.slippage)
                positions[order.symbol] = trade
                trades.append(trade)
            if cash < -1e-6:
                raise AssertionError(f"{day}: negative cash {cash:.2f} (no leverage allowed)")

            if breakers.block_new_entries:
                kind = "drawdown_halt" if breakers.halt else "weekly_loss"
                events.append(BreakerEvent(day, kind, breakers.current_equity, breakers.peak_equity,
                                           breakers.drawdown, breakers.previous_week_return, tuple(sold),
                                           "; ".join(breakers.reasons)))
            if breakers.halt:
                if halt_mode == "stop":
                    halted = True
                peak_since = day
            last_trading_as_of = as_of

        # 3. intraday stops (entry day included)
        for sym in sorted(positions):
            low = market.low[sym][i]
            if np.isfinite(low) and low <= positions[sym].stop:
                close_position(sym, positions[sym].stop, day, "stop")

        # 4. mark at the close
        for sym in positions:
            c = market.close[sym][i]
            if np.isfinite(c):
                last_close[sym] = float(c)
        invested = sum(t.qty * last_close.get(s, t.entry_price) for s, t in positions.items())
        marks_dates.append(ts)
        marks.append(cash + invested)
        exposure.append(invested / (cash + invested))

    # Base point: the starting capital at the previous session's close (metrics measure from it).
    base_ts = all_sessions[lo - 1] if lo > 0 else all_sessions[lo] - pd.Timedelta(days=1)
    marks_dates.insert(0, base_ts)
    marks.insert(0, float(initial_capital if initial_capital is not None else cfg.backtest.initial_capital))
    exposure.insert(0, 0.0)
    last_day = all_sessions[hi - 1].date()
    for sym, trade in positions.items():
        trade.exit_date, trade.exit_price = last_day, last_close.get(sym, trade.entry_price)
    index = pd.DatetimeIndex(marks_dates)
    return BacktestResult(name=name, equity=pd.Series(marks, index=index, name=name),
                          exposure=pd.Series(exposure, index=index, name=name), trades=trades, weeks=weeks,
                          events=events, skipped_mondays=skipped, notes=notes)


def fixed_strategy(cfg: Config) -> StrategyFactory:
    """The configured strategy with the live signal-window rule."""
    from tradingbot.pipeline import strategy_with_window
    from tradingbot.strategies.registry import build_strategy

    return lambda session, window: build_strategy(strategy_with_window(cfg.strategy, window))


def symbols_needed(cfg: Config) -> Sequence[str]:
    return list(dict.fromkeys([*cfg.watchlist, cfg.benchmark_symbol]))
