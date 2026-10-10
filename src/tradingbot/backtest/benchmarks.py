"""Benchmarks for the backtest report.

- Buy and hold SPY: all capital in SPY at the open of the first session (fractional
  shares, buy slippage), held to the end (no sale, marked at the close).
- SMA200 on SPY: on each Monday run (same calendar as the engine, holiday Mondays
  skipped) hold 100% SPY if the as_of close > SMA200(as_of), else 100% cash; trades at
  the open with the same costs. No stop, no risk manager (a classic timing rule).
- Random entries: `RandomStrategy` plugs into the SAME engine (risk manager, stops,
  breakers, weekly limit, correlation, costs); only the choice of entries/exits is
  random, calibrated to the main strategy's activity. Deterministic per seed.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import date, timedelta

import numpy as np
import pandas as pd

from tradingbot.backtest.costs import CostModel
from tradingbot.backtest.engine import BacktestResult, Trade, trading_sessions
from tradingbot.config import Config
from tradingbot.data.market_data import cut_as_of
from tradingbot.indicators.technical import sma
from tradingbot.strategies.base import Signal, Strategy


def _single_asset(cfg: Config, frame: pd.DataFrame, start: date, end: date, name: str,
                  target_on: Callable[[int, pd.Timestamp], int | None], initial_capital: float | None = None) -> BacktestResult:
    """Hold 0% or 100% of equity in one symbol; `target_on(i, ts)` -> 1, 0 or None (no change)."""
    costs = CostModel(cfg.costs)
    sessions = trading_sessions({name: frame})
    lo, hi = sessions.searchsorted(pd.Timestamp(start)), sessions.searchsorted(pd.Timestamp(end), "right")
    if lo >= hi:
        raise ValueError(f"no session between {start} and {end}")
    capital = float(initial_capital if initial_capital is not None else cfg.backtest.initial_capital)
    cash, qty = capital, 0.0
    trades: list[Trade] = []
    open_trade: Trade | None = None
    base_ts = sessions[lo - 1] if lo > 0 else sessions[lo] - pd.Timedelta(days=1)
    dates, marks, expo = [base_ts], [capital], [0.0]
    opens, closes = frame["open"].reindex(sessions).to_numpy(float), frame["close"].reindex(sessions).to_numpy(float)
    for i in range(lo, hi):
        ts = sessions[i]
        target = target_on(i, ts)
        if target == 1 and qty == 0:
            price = costs.buy(opens[i], 1).price
            qty = cash / price
            open_trade = Trade(name, ts.date(), opens[i], price, qty, 0.0, "enter", qty * (price - opens[i]))
            trades.append(open_trade)
            cash = 0.0
        elif target == 0 and qty > 0:
            fill = costs.sell(opens[i], qty)
            cash = qty * fill.price - fill.fees
            open_trade.exit_date, open_trade.exit_price, open_trade.exit_reason = ts.date(), fill.price, "signal"
            open_trade.exit_fees, open_trade.exit_slippage = fill.fees, fill.slippage
            qty, open_trade = 0.0, None
        invested = qty * closes[i]
        dates.append(ts)
        marks.append(cash + invested)
        expo.append(invested / (cash + invested))
    if open_trade is not None:
        open_trade.exit_date, open_trade.exit_price = sessions[hi - 1].date(), closes[hi - 1]
    index = pd.DatetimeIndex(dates)
    return BacktestResult(name=name, equity=pd.Series(marks, index=index, name=name),
                          exposure=pd.Series(expo, index=index, name=name), trades=trades, weeks=[], events=[])


def buy_and_hold(cfg: Config, frame: pd.DataFrame, start: date, end: date, name: str = "buy_hold",
                 initial_capital: float | None = None) -> BacktestResult:
    return _single_asset(cfg, frame, start, end, name, lambda i, ts: 1, initial_capital)


def sma_timing(cfg: Config, frame: pd.DataFrame, start: date, end: date, window: int = 200,
               name: str = "sma200", initial_capital: float | None = None) -> BacktestResult:
    def target(i: int, ts: pd.Timestamp) -> int | None:
        if ts.weekday() != 0:
            return None
        hist = cut_as_of(frame, ts.date() - timedelta(days=1))["close"]
        if len(hist) < window:
            return 0
        return int(hist.iloc[-1] > sma(hist.iloc[-window:], window).iloc[-1])

    return _single_asset(cfg, frame, start, end, name, target, initial_capital)


class RandomStrategy(Strategy):
    """Random proposals; the risk manager, stops and breakers still apply in the engine.

    Each run: every held symbol exits with probability `p_exit`; every other symbol with
    at least `min_bars` bars is proposed with probability `p_entry`, in random order.
    The generator is seeded with (seed, as_of) so a week's draw never depends on the future.
    """

    name = "random"

    def __init__(self, seed: int, p_entry: float, p_exit: float, min_bars: int = 61) -> None:
        self.seed, self.p_entry, self.p_exit, self.min_bars = seed, p_entry, p_exit, min_bars

    @property
    def min_history(self) -> int:
        return self.min_bars

    def generate_signals(self, bars: Mapping[str, pd.DataFrame], as_of: date, positions: Collection[str],
                         last_exit: Mapping[str, date] | None = None) -> list[Signal]:
        rng = np.random.default_rng([self.seed, as_of.toordinal()])
        held = set(positions)
        exits, buys = [], []
        for symbol in sorted(bars):
            frame = cut_as_of(bars[symbol], as_of)
            if len(frame) < self.min_bars:
                continue
            draw = rng.random()
            close = float(frame["close"].iloc[-1])
            if symbol in held:
                if draw < self.p_exit:
                    exits.append(Signal(symbol, "exit", frame.index[-1].date(), "random exit", close=close))
            elif draw < self.p_entry:
                buys.append(Signal(symbol, "buy", frame.index[-1].date(), "random entry",
                                   cross_date=frame.index[-1].date(), sessions_since_cross=0, close=close))
        order = rng.permutation(len(buys))
        return exits + [buys[k] for k in order]


def calibrate_random(result: BacktestResult, n_symbols: int) -> tuple[float, float]:
    """(p_entry, p_exit) matching the main strategy's activity per run.

    p_entry = buy signals / not-held symbol-runs; p_exit = signal exits / held position-runs.
    """
    buy_signals = held_runs = exit_signals = 0
    for week in result.weeks:
        held = sum(1 for t in result.trades
                   if t.entry_date < week.session and (t.is_open or t.exit_date >= week.session))
        held_runs += held
        buy_signals += sum(1 for s in week.signals if s.action == "buy")
        exit_signals += sum(1 for s in week.signals if s.action == "exit")
    runs = len(result.weeks)
    free = max(runs * n_symbols - held_runs, 1)
    return min(buy_signals / free, 1.0), min(exit_signals / max(held_runs, 1), 1.0)


def distribution(values: Sequence[float]) -> tuple[float, float, float]:
    """(median, 5th percentile, 95th percentile), NaNs ignored."""
    arr = np.asarray([v for v in values if np.isfinite(v)], dtype=float)
    if arr.size == 0:
        return float("nan"), float("nan"), float("nan")
    return float(np.median(arr)), float(np.percentile(arr, 5)), float(np.percentile(arr, 95))
