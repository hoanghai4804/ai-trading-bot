from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from tradingbot.config import StrategyConfig, load_config
from tradingbot.strategies.ma_crossover import MACrossover
from tradingbot.strategies.registry import build_strategy

# Small windows so crosses are easy to place by hand.
PARAMS = {"fast": 3, "slow": 5, "cross_lookback_sessions": 5}
FLAT = 20  # flat bars before the ramp


def _bars(closes, start="2026-01-05"):
    idx = pd.date_range(start, periods=len(closes), freq="B")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1e6}, index=idx)


def _ramp(ramp_bars, step=1.0):
    """FLAT bars at 100 then a linear ramp. MA3 crosses MA5 exactly on the first ramp bar
    (before it both MAs equal 100; on it MA3 = 100 + step/3 > MA5 = 100 + step/5)."""
    return _bars([100.0] * FLAT + [100.0 + step * (i + 1) for i in range(ramp_bars)])


def _last_day(frame):
    return frame.index[-1].date()


def _signals(bars, positions=(), last_exit=None, as_of=None):
    strategy = MACrossover(PARAMS)
    as_of = as_of or max(_last_day(b) for b in bars.values())
    return strategy.generate_signals(bars, as_of, positions, last_exit)


def test_up_cross_on_last_session_is_a_buy():
    bars = _ramp(1)
    [sig] = _signals({"AAA": bars})
    assert (sig.symbol, sig.action, sig.sessions_since_cross) == ("AAA", "buy", 0)
    assert sig.cross_date == _last_day(bars) == sig.as_of
    assert sig.fast_ma > sig.slow_ma


@pytest.mark.parametrize("ramp_bars,expect_buy", [(5, True), (6, False)])
def test_cross_lookback_window(ramp_bars, expect_buy):
    # ramp of 5 bars -> cross 4 sessions ago (inside window of 5); 6 bars -> 5 ago (too old)
    signals = _signals({"AAA": _ramp(ramp_bars)})
    assert bool(signals) is expect_buy
    if expect_buy:
        assert signals[0].sessions_since_cross == 4


def test_down_cross_exits_only_when_held():
    bars = _ramp(3, step=-1.0)
    [sig] = _signals({"AAA": bars}, positions=["AAA"])
    assert sig.action == "exit" and sig.fast_ma < sig.slow_ma
    assert _signals({"AAA": bars}) == []  # not held -> nothing to exit, and no buy


def test_held_symbol_with_fast_above_gets_no_signal():
    assert _signals({"AAA": _ramp(1)}, positions=["AAA"]) == []


def test_no_buy_without_a_visible_cross():
    # Rising from the first bar: MA3 > MA5 as soon as both exist, cross date unknown.
    assert _signals({"AAA": _bars([100.0 + i for i in range(30)])}) == []
    # Flat: MA3 == MA5, not "above".
    assert _signals({"AAA": _bars([100.0] * 30)}) == []


def test_no_reentry_after_closing_after_the_cross():
    bars = _ramp(4)
    cross_date = bars.index[FLAT].date()
    after = bars.index[FLAT + 2].date()
    assert _signals({"AAA": bars}, last_exit={"AAA": after}) == []
    # Closed on/before the cross date -> an older trade, the new cross may be traded.
    assert len(_signals({"AAA": bars}, last_exit={"AAA": cross_date})) == 1
    assert len(_signals({"AAA": bars}, last_exit={"BBB": after})) == 1


def test_insufficient_history_gives_no_signal():
    short = _bars([100.0, 100, 100, 100, 101])  # 5 bars < slow + 1
    assert _signals({"AAA": short}) == []


def test_order_exits_first_then_most_recent_cross_then_alphabetical():
    bars = {
        "ZZZ": _ramp(1),            # cross 0 ago
        "BBB": _ramp(3),            # cross 2 ago
        "AAA": _ramp(3),            # cross 2 ago (tie -> alphabetical)
        "MMM": _ramp(2, step=-1.0),  # held, exit
        "CCC": _ramp(2, step=-1.0),  # held, exit
    }
    out = _signals(bars, positions=["MMM", "CCC"])
    assert [(s.action, s.symbol) for s in out] == [
        ("exit", "CCC"), ("exit", "MMM"), ("buy", "ZZZ"), ("buy", "AAA"), ("buy", "BBB"),
    ]


def test_no_lookahead_rows_after_as_of_are_ignored():
    # Data ends in a down ramp that would cancel the buy; as_of is the cross day itself.
    closes = [100.0] * FLAT + [101.0] + [100.0 - i for i in range(10)]
    bars = _bars(closes)
    as_of = bars.index[FLAT].date()
    [sig] = _signals({"AAA": bars}, as_of=as_of)
    assert sig.action == "buy" and sig.as_of == as_of
    assert _signals({"AAA": bars}, as_of=as_of) == _signals({"AAA": bars.loc[:str(as_of)]}, as_of=as_of)


def test_no_lookahead_appending_future_data_never_changes_past_signals():
    rng = np.random.default_rng(7)
    closes = 100 + rng.normal(0, 1, 160).cumsum()
    full = {"AAA": _bars(closes), "BBB": _bars(closes[::-1].copy())}
    strategy = MACrossover(PARAMS)
    for t in range(10, 120, 3):
        as_of = full["AAA"].index[t].date()
        past = {s: b.iloc[: t + 1] for s, b in full.items()}
        for held in ([], ["AAA"], ["AAA", "BBB"]):
            assert strategy.generate_signals(full, as_of, held) == strategy.generate_signals(past, as_of, held)


def test_as_of_on_weekend_uses_friday_bar():
    bars = _ramp(5)  # 25 business days from a Monday -> ends on a Friday
    friday = _last_day(bars)
    assert friday.weekday() == 4
    sunday = date.fromordinal(friday.toordinal() + 2)
    [sig] = _signals({"AAA": bars}, as_of=sunday)
    assert sig.as_of == friday


def test_registry_builds_default_config_strategy():
    strategy = build_strategy(load_config().strategy)
    assert strategy.name == "ma_crossover"
    assert (strategy.params.fast, strategy.params.slow, strategy.params.cross_lookback_sessions) == (20, 50, 5)
    assert strategy.min_history == 51


def test_registry_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategy(StrategyConfig(name="nope", params={}))


@pytest.mark.parametrize("params", [
    {"fast": 50, "slow": 20, "cross_lookback_sessions": 5},  # fast >= slow
    {"fast": 20, "slow": 50},                                # missing key
    {"fast": 20, "slow": 50, "cross_lookback_sessions": 5, "typo": 1},  # unknown key
    {"fast": 20, "slow": 50, "cross_lookback_sessions": 0},
])
def test_invalid_params_are_rejected(params):
    with pytest.raises(ValidationError):
        build_strategy(StrategyConfig(name="ma_crossover", params=params))
