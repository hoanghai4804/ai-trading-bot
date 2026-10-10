"""No look-ahead in the backtest and in walk-forward selection (real-data fixture, no network)."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from test_backtest_parity import BARS, CFG

from tradingbot.backtest.engine import fixed_strategy, run_backtest
from tradingbot.backtest.walk_forward import (Window, WindowChoice, choose, make_windows, run_walk_forward,
                                              select_parameters)

START, END = date(2025, 9, 2), date(2026, 4, 27)


def _decisions(result, until: date):
    return [(w.session, w.window, [(s.symbol, s.action) for s in w.signals],
             [(o.symbol, o.side, o.qty, o.stop_price) for o in w.decision.approved],
             [(b.symbol, b.code) for b in w.decision.blocked]) for w in result.weeks if w.session <= until]


def _scramble_after(bars, cut: pd.Timestamp, keep_open: bool, seed: int = 0):
    """Replace every value after `cut` (and the cut row except its open, if keep_open) with noise."""
    rng = np.random.default_rng(seed)
    out = {}
    for s, f in bars.items():
        f = f.copy()
        later = f.index > cut
        f.loc[later, ["open", "high", "low", "close"]] *= rng.uniform(0.5, 1.5, (later.sum(), 1))
        if keep_open and cut in f.index:
            f.loc[cut, ["high", "low", "close"]] = [f.loc[cut, "open"] * 1.5, f.loc[cut, "open"] * 0.5,
                                                    f.loc[cut, "open"] * 0.7]
        out[s] = f
    return out


BASE = run_backtest(CFG, BARS, START, END, fixed_strategy(CFG))


def test_fixture_run_trades():
    assert len(BASE.trades) >= 5 and sum(1 for w in BASE.weeks if w.decision.approved) >= 3


@pytest.mark.parametrize("monday", [w.session for w in BASE.weeks if w.decision.approved][:4])
def test_week_decision_unchanged_by_future_data(monday):
    """A Monday's decision only uses data <= the previous day plus that Monday's open."""
    cut = pd.Timestamp(monday)
    scrambled = run_backtest(CFG, _scramble_after(BARS, cut, keep_open=True), START, END, fixed_strategy(CFG))
    assert _decisions(scrambled, monday) == _decisions(BASE, monday)
    before = BASE.equity.index < cut
    assert scrambled.equity[before].equals(BASE.equity[before])
    truncated = run_backtest(CFG, {s: f.loc[:cut] for s, f in BARS.items()}, START, monday, fixed_strategy(CFG))
    assert _decisions(truncated, monday) == _decisions(BASE, monday)


def test_changing_the_previous_friday_changes_the_decision_inputs():
    """Sanity check of the test above: data <= as_of IS used (otherwise the test would prove nothing)."""
    monday = next(w.session for w in BASE.weeks if w.decision.approved)
    cut = pd.Timestamp(monday - timedelta(days=10))
    scrambled = run_backtest(CFG, _scramble_after(BARS, cut, keep_open=False), START, END, fixed_strategy(CFG))
    assert _decisions(scrambled, monday) != _decisions(BASE, monday)


# --- walk-forward ------------------------------------------------------------------------------------

WF_CFG = CFG.model_copy(update={"backtest": CFG.backtest.model_copy(update={"fast_grid": [10, 20],
                                                                              "slow_grid": [50]})})
TRAIN = Window(date(2025, 8, 1), date(2025, 12, 31), date(2026, 1, 1), date(2026, 4, 27))


def test_parameter_choice_uses_only_train_data():
    [base] = select_parameters(WF_CFG, BARS, [TRAIN])
    cut = pd.Timestamp(TRAIN.train_end)
    [other] = select_parameters(WF_CFG, _scramble_after(BARS, cut, keep_open=False, seed=3), [TRAIN])
    assert other.scores == base.scores and other.params == base.params
    assert set(base.scores) == {(10, 50), (20, 50)}
    # while the train window itself matters
    [moved] = select_parameters(WF_CFG, _scramble_after(BARS, pd.Timestamp("2025-10-01"), keep_open=False), [TRAIN])
    assert moved.scores != base.scores


def test_choose_best_sharpe_nan_worst_ties_keep_grid_order():
    order = [(10, 50), (20, 50), (30, 50)]
    assert choose({(10, 50): 0.5, (20, 50): 0.9, (30, 50): 0.9}, order) == (20, 50)
    assert choose({(10, 50): float("nan"), (20, 50): -1.0, (30, 50): float("nan")}, order) == (20, 50)


def test_make_windows_roll_and_clip_to_data_end():
    ws = make_windows(date(2016, 1, 1), date(2020, 2, 10), train_years=3, test_months=6)
    assert [(w.train_start, w.train_end, w.test_start, w.test_end) for w in ws] == [
        (date(2016, 1, 1), date(2018, 12, 31), date(2019, 1, 1), date(2019, 6, 30)),
        (date(2016, 7, 1), date(2019, 6, 30), date(2019, 7, 1), date(2019, 12, 31)),
        (date(2017, 1, 1), date(2019, 12, 31), date(2020, 1, 1), date(2020, 2, 10)),
    ]


def test_walk_forward_switches_parameters_at_window_boundaries():
    w1 = Window(date(2025, 6, 1), date(2025, 8, 31), date(2025, 9, 1), date(2025, 12, 31))
    w2 = Window(date(2025, 9, 1), date(2025, 12, 31), date(2026, 1, 1), date(2026, 4, 27))
    r = run_walk_forward(CFG, BARS, [WindowChoice(w1, (10, 50), {}), WindowChoice(w2, (30, 50), {})])
    reasons = {(w.session < w2.test_start, s.reason.split(" ")[0]) for w in r.weeks for s in w.signals
               if s.action == "buy"}
    assert reasons <= {(True, "MA10"), (False, "MA30")}
    assert {flag for flag, _ in reasons} == {True, False}
