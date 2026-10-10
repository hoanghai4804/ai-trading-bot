"""Walk-forward parameter selection for the MA crossover.

Test windows of `test_months` start at backtest.start + train_years and roll until the
data ends. For each window the (fast, slow) pair with the best Sharpe on the TRAIN
window [test_start - train_years, test_start) is chosen; ties keep the grid order. The
train backtests only receive bars cut at the day before the test window starts, so the
choice cannot see test data. The out-of-sample run is ONE continuous backtest whose
strategy parameters switch at each window boundary (positions carry over).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from tradingbot.backtest.engine import BacktestResult, StrategyFactory, run_backtest
from tradingbot.backtest.metrics import sharpe_ratio
from tradingbot.config import Config
from tradingbot.data.market_data import cut_as_of
from tradingbot.pipeline import strategy_with_window
from tradingbot.strategies.registry import build_strategy

Params = tuple[int, int]
MapFn = Callable[[Callable[[Any], Any], Iterable[Any]], Iterable[Any]]


@dataclass(frozen=True)
class Window:
    train_start: date
    train_end: date      # inclusive (day before test_start)
    test_start: date
    test_end: date       # inclusive


@dataclass(frozen=True)
class WindowChoice:
    window: Window
    params: Params
    scores: dict[Params, float]    # train Sharpe of every grid pair


def _add_months(d: date, months: int) -> date:
    return (pd.Timestamp(d) + pd.DateOffset(months=months)).date()


def make_windows(start: date, data_end: date, train_years: int, test_months: int) -> list[Window]:
    windows = []
    test_start = _add_months(start, 12 * train_years)
    while test_start <= data_end:
        nxt = _add_months(test_start, test_months)
        windows.append(Window(_add_months(test_start, -12 * train_years), test_start - timedelta(days=1),
                              test_start, min(nxt - timedelta(days=1), data_end)))
        test_start = nxt
    return windows


def grid(cfg: Config) -> list[Params]:
    return [(f, s) for f in cfg.backtest.fast_grid for s in cfg.backtest.slow_grid if f < s]


def params_strategy(cfg: Config, params: Params) -> StrategyFactory:
    fast, slow = params

    def factory(session: date, window: int):
        base = cfg.strategy.model_copy(update={"params": {**cfg.strategy.params, "fast": fast, "slow": slow}})
        return build_strategy(strategy_with_window(base, window))

    return factory


def _train_score(job: tuple[Config, Mapping[str, pd.DataFrame], Window, Params]) -> float:
    cfg, bars, window, params = job
    logging.getLogger("tradingbot.strategies").setLevel(logging.ERROR)   # warm-up weeks are expected
    cut = {s: cut_as_of(f, window.train_end) for s, f in bars.items()}
    result = run_backtest(cfg, cut, window.train_start, window.train_end, params_strategy(cfg, params),
                          name=f"train {params}")
    return sharpe_ratio(result.equity)


def choose(scores: Mapping[Params, float], order: list[Params]) -> Params:
    """Highest Sharpe; NaN counts as worst; ties keep grid order."""
    def key(p: Params) -> tuple[float, int]:
        v = scores[p]
        return (v if v == v else float("-inf"), -order.index(p))
    return max(order, key=key)


def select_parameters(cfg: Config, bars: Mapping[str, pd.DataFrame], windows: list[Window],
                      map_fn: MapFn = map) -> list[WindowChoice]:
    pairs = grid(cfg)
    jobs = [(cfg, bars, w, p) for w in windows for p in pairs]
    values = list(map_fn(_train_score, jobs))
    choices = []
    for k, w in enumerate(windows):
        scores = dict(zip(pairs, values[k * len(pairs):(k + 1) * len(pairs)]))
        choices.append(WindowChoice(w, choose(scores, pairs), scores))
    return choices


def run_walk_forward(cfg: Config, bars: Mapping[str, pd.DataFrame], choices: list[WindowChoice],
                     name: str = "walk_forward") -> BacktestResult:
    def factory(session: date, window: int):
        for c in choices:
            if c.window.test_start <= session <= c.window.test_end:
                return params_strategy(cfg, c.params)(session, window)
        raise ValueError(f"{session} is outside every walk-forward test window")

    return run_backtest(cfg, bars, choices[0].window.test_start, choices[-1].window.test_end, factory, name=name)
