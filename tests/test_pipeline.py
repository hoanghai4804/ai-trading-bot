from datetime import date
from functools import partial

import numpy as np
import pytest
import pandas as pd
from fake_broker import NOW, FakeBroker, equity_points

from tradingbot.config import load_config
from tradingbot.execution.executor import Executor
from tradingbot.pipeline import run_weekly
from tradingbot.safety import read_halt

CFG = load_config()
TODAY = date(2026, 10, 12)          # Monday
IDX = pd.bdate_range(end="2026-10-09", periods=150)


def _frame(close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": 1e6}, index=IDX)


def _cross_on_last_day():
    """Alternating 100.5 / 99.5 (MA20 == MA50 exactly), then a jump: MA20 crosses above on the last session."""
    close = np.where(np.arange(len(IDX)) % 2 == 0, 100.5, 99.5)
    close[-1] = 102.0
    return close


def _bars():
    rng = np.random.default_rng(7)
    bars = {s: _frame(80 * np.cumprod(1 - 0.001 + rng.normal(0, 0.01, len(IDX)))) for s in CFG.watchlist}
    bars["AAPL"] = _frame(_cross_on_last_day())
    return bars


BARS = _bars()


def load_bars(symbols, start, as_of):
    return {s: BARS[s].loc[:pd.Timestamp(as_of)] for s in symbols}


def prices(symbols):
    return {s: float(BARS[s]["close"].iloc[-1]) for s in symbols}


def run(fb, tmp_path, dry_run=False, alerts=None):
    sink = (lambda level, msg: alerts.append((level, msg))) if alerts is not None else (lambda *a: None)
    return run_weekly(CFG, fb, load_bars, prices, now=NOW, today=TODAY, dry_run=dry_run, alert=sink,
                      state_dir=tmp_path, executor_factory=partial(Executor, sleep=fb.sleep, monotonic=fb.monotonic))


def test_fixture_produces_one_buy_signal():
    fb = FakeBroker()
    result = run(fb, None, dry_run=True)
    assert [(s.action, s.symbol) for s in result.signals] == [("buy", "AAPL")]
    assert result.as_of == date(2026, 10, 11)


@pytest.mark.parametrize("on_buy,first_status", [("fill", "filled"), ("pending", "submitted")])
def test_running_twice_sends_orders_only_once(tmp_path, on_buy, first_status):
    fb = FakeBroker()
    fb.on_buy = on_buy
    first = run(fb, tmp_path)
    assert [(r.side, r.symbol, r.status) for r in first.execution.results] == [("buy", "AAPL", first_status)]
    [(_, symbol, qty, stop, cid)] = fb.calls
    assert (symbol, cid) == ("AAPL", "tb-2026-10-12-AAPL-buy") and stop < 102.0
    second = run(fb, tmp_path)
    assert len(fb.calls) == 1 and second.execution.orders_sent == 0
    assert second.new_positions_this_week == 1
    if on_buy == "fill":   # held now -> the strategy no longer proposes it
        assert [s.symbol for s in second.signals] == []
    else:                  # still pending -> approved again, but the executor finds the same order id
        assert [r.detail for r in second.execution.results] == ["already_submitted (new)"]


def test_dry_run_and_closed_market_send_nothing(tmp_path):
    fb = FakeBroker()
    assert run(fb, tmp_path, dry_run=True).execution.results[0].status == "dry_run"
    fb.is_open = False
    assert run(fb, tmp_path).execution.results[0].status == "skipped"
    assert fb.calls == []


def test_weekly_limit_comes_from_order_history(tmp_path):
    fb = FakeBroker()
    for s in ("MSFT", "NVDA", "JPM"):
        fb.add(symbol=s, side="buy", type="market", status="filled", qty=1, filled_qty=1,
               client_order_id=f"tb-2026-10-12-{s}-buy")
    fb.add(symbol="XOM", side="buy", type="market", status="rejected", qty=1,
           client_order_id="tb-2026-10-12-XOM-buy")                     # rejected: not counted
    result = run(fb, tmp_path)
    assert result.new_positions_this_week == 3
    assert {b.symbol: b.code for b in result.decision.blocked} == {"AAPL": "weekly_limit"}
    assert fb.calls == []


def test_drawdown_halt_writes_lock_and_liquidates(tmp_path):
    fb = FakeBroker(positions={"MSFT": 4}, equity=88_000,
                    history=equity_points({"2026-09-01": 100_000, "2026-10-09": 88_000}))
    fb.add_stop("MSFT", 4, 70)
    alerts = []
    result = run(fb, tmp_path, alerts=alerts)
    assert result.decision.halt and read_halt(tmp_path) is not None
    assert fb.positions == {} and "buy" not in fb.writes
    assert {b.symbol: b.code for b in result.decision.blocked} == {"AAPL": "drawdown_halt"}
    assert any(level == "critical" for level, _ in alerts)
