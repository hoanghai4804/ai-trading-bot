from datetime import date
from functools import partial

import numpy as np
import pytest
import pandas as pd
from fake_broker import NOW, FakeBroker, equity_points

from tradingbot.config import load_config
from tradingbot.data.market_data import LatestTrade
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
    return {s: LatestTrade(float(BARS[s]["close"].iloc[-1]), NOW) for s in symbols}


def raw_closes(symbols, as_of):
    return {s: (BARS[s].index[-1].date(), float(BARS[s]["close"].iloc[-1])) for s in symbols}


def run(fb, tmp_path, dry_run=False, alerts=None):
    sink = (lambda level, msg: alerts.append((level, msg))) if alerts is not None else (lambda *a: None)
    return run_weekly(CFG, fb, load_bars, prices, raw_closes, now=NOW, today=TODAY, dry_run=dry_run, alert=sink,
                      state_dir=tmp_path, executor_factory=partial(Executor, sleep=fb.sleep, monotonic=fb.monotonic))


def test_fixture_produces_one_buy_signal(tmp_path):
    fb = FakeBroker()
    result = run(fb, tmp_path, dry_run=True)
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


# --- phase 6b ---------------------------------------------------------------------------

from tradingbot.data.market_data import LatestTrade as _LT  # noqa: E402
from tradingbot.execution.stop_registry import load_stops  # noqa: E402
from tradingbot.pipeline import load_last_run, signal_window  # noqa: E402
from tradingbot.reporting.run_text import format_run  # noqa: E402


def test_first_line_states_the_mode(tmp_path):
    dry = run(FakeBroker(), tmp_path, dry_run=True)
    real = run(FakeBroker(), tmp_path)
    assert format_run(dry)[0].startswith("MODE: DRY-RUN") and "no order" in format_run(dry)[0]
    assert format_run(real)[0].startswith("MODE: EXECUTE") and "PAPER" in format_run(real)[0]
    assert (dry.to_dict()["mode"], real.to_dict()["mode"]) == ("DRY-RUN", "EXECUTE")
    assert list(dry.to_dict())[0] == "mode"


def test_cli_run_prints_mode_first(monkeypatch, capsys, tmp_path):
    from tradingbot import cli

    result = run(FakeBroker(), tmp_path, dry_run=True)
    monkeypatch.setattr(cli, "assert_not_halted", lambda: None)
    monkeypatch.setattr(cli, "_connect", lambda args: (CFG, FakeBroker()))
    monkeypatch.setattr(cli, "RunLock", lambda ttl: __import__("contextlib").nullcontext())
    monkeypatch.setattr(cli, "MarketData", lambda **kw: type("M", (), {"get_raw_closes": None})())
    monkeypatch.setattr(cli, "default_alpaca_source", lambda: None)
    monkeypatch.setattr(cli, "LatestPrices", lambda feed: type("P", (), {"get": None})())
    monkeypatch.setattr(cli, "run_weekly", lambda *a, **kw: result)
    monkeypatch.setattr(cli, "_notifier", lambda cfg, args, tag="": type("N", (), {"alert": print})())
    import tradingbot.reporting.summary as summary_mod
    monkeypatch.setattr(summary_mod, "RUNS_DIR", tmp_path / "runs")
    assert cli.main(["run"]) == 0
    assert (tmp_path / "runs" / TODAY.isoformat() / "summary.md").exists()
    assert capsys.readouterr().out.splitlines()[0].startswith("MODE: DRY-RUN")


@pytest.mark.parametrize("age", [pd.Timedelta(minutes=10), pd.Timedelta(days=3)])
def test_stale_reference_price_blocks_the_buy(tmp_path, age):
    def old_prices(symbols):
        return {s: _LT(float(BARS[s]["close"].iloc[-1]), NOW - age) for s in symbols}

    fb = FakeBroker()
    result = run_weekly(CFG, fb, load_bars, old_prices, raw_closes, now=NOW, today=TODAY, dry_run=False,
                        alert=lambda *a: None, state_dir=tmp_path,
                        executor_factory=partial(Executor, sleep=fb.sleep, monotonic=fb.monotonic))
    [b] = result.decision.blocked
    assert (b.symbol, b.code) == ("AAPL", "no_reference_price") and "min old" in b.detail
    assert fb.calls == []


def test_expired_stop_is_replaced_at_original_price_on_next_run(tmp_path):
    fb = FakeBroker()
    run(fb, tmp_path)                                         # buys AAPL with its OTO stop
    original = load_stops(tmp_path)["AAPL"].stop_price
    [leg] = [r for r in fb.records.values() if r.parent]
    leg.status = "expired"                                    # GTC expired after 90 days
    alerts = []
    second = run(fb, tmp_path, alerts=alerts)
    assert [(c[0], c[1], c[3]) for c in fb.calls[1:]] == [("stop", "AAPL", original)]
    assert any("re-placed at the original price" in m for _, m in alerts)
    assert [r.status for r in second.execution.results] == ["restored"]


# Signal window ------------------------------------------------------------------------

SESSIONS = {"X": pd.DataFrame({"close": 1.0}, index=pd.bdate_range("2026-09-01", "2026-10-09"))}


@pytest.mark.parametrize("last,expected", [
    (None, 5),                    # no previous run -> configured minimum
    (date(2026, 10, 9), 5),       # same as_of (rerun) -> minimum
    (date(2026, 10, 2), 5),       # last week's run -> 5 sessions
    (date(2026, 9, 25), 10),      # last week's Monday was a holiday -> 10 sessions
    (date(2026, 8, 1), 15),       # long gap -> capped
])
def test_signal_window(last, expected):
    assert signal_window(SESSIONS, date(2026, 10, 11), last, 5, 15) == expected


def _holiday_bars():
    """AAPL crosses up on Thu 2026-10-01: 6 sessions before the as_of of the 10-12 run."""
    bars = _bars()
    close = np.where(np.arange(len(IDX)) % 2 == 0, 100.5, 99.5)
    close[IDX >= pd.Timestamp("2026-10-01")] += 2.0
    bars["AAPL"] = _frame(close)
    return bars


def _run_on(fb, tmp_path, today, bars, is_open):
    fb.is_open = is_open
    return run_weekly(CFG, fb, lambda s, st, a: {x: bars[x].loc[:pd.Timestamp(a)] for x in s},
                      lambda syms: {s: _LT(float(bars[s]["close"].iloc[-1]), NOW) for s in syms},
                      lambda syms, a: {s: (bars[s].loc[:pd.Timestamp(a)].index[-1].date(),
                                           float(bars[s].loc[:pd.Timestamp(a)]["close"].iloc[-1])) for s in syms},
                      now=NOW, today=today, dry_run=False, alert=lambda *a: None, state_dir=tmp_path,
                      executor_factory=partial(Executor, sleep=fb.sleep, monotonic=fb.monotonic))


@pytest.mark.parametrize("monday_oct5_open,window,buys", [(False, 10, ["AAPL"]), (True, 5, [])])
def test_week_after_a_holiday_does_not_miss_the_cross(tmp_path, monday_oct5_open, window, buys):
    bars = _holiday_bars()
    fb = FakeBroker()
    _run_on(fb, tmp_path, date(2026, 9, 28), bars, is_open=True)
    _run_on(fb, tmp_path, date(2026, 10, 5), bars, is_open=monday_oct5_open)   # holiday or normal Monday
    if monday_oct5_open:
        assert load_last_run(tmp_path)["last_trading_as_of"] == "2026-10-04"
    else:
        assert load_last_run(tmp_path)["last_trading_as_of"] == "2026-09-27"   # closed run does not count
    result = _run_on(fb, tmp_path, date(2026, 10, 12), bars, is_open=True)
    assert result.signal_window == window
    assert [s.symbol for s in result.signals if s.action == "buy"] == buys


def test_dry_run_never_writes_last_run(tmp_path):
    run(FakeBroker(), tmp_path, dry_run=True)
    assert load_last_run(tmp_path) == {}


def test_gap_is_checked_against_the_raw_close(tmp_path):
    def raw_4pct_lower(symbols, as_of):   # e.g. adjusted history rescaled by a split/dividend
        return {s: (d, c / 1.04) for s, (d, c) in raw_closes(symbols, as_of).items()}

    fb = FakeBroker()
    result = run_weekly(CFG, fb, load_bars, prices, raw_4pct_lower, now=NOW, today=TODAY, dry_run=False,
                        alert=lambda *a: None, state_dir=tmp_path,
                        executor_factory=partial(Executor, sleep=fb.sleep, monotonic=fb.monotonic))
    assert {b.symbol: b.code for b in result.decision.blocked} == {"AAPL": "gap_too_large"}
    assert fb.calls == []


@pytest.mark.parametrize("age,sold", [(pd.Timedelta(minutes=1), True), (pd.Timedelta(minutes=10), False)])
def test_breached_original_stop_needs_a_fresh_price(tmp_path, age, sold):
    from tradingbot.execution.stop_registry import StopEntry, save_stops

    # AAPL held (MA20 > MA50: no exit signal); original stop far above its ~102 price.
    save_stops({"AAPL": StopEntry(500.0, date(2026, 6, 1), "tb-2026-06-01-AAPL-buy")}, tmp_path)
    fb = FakeBroker(positions={"AAPL": 3})

    def priced(symbols):
        return {s: _LT(float(BARS[s]["close"].iloc[-1]), NOW - age) for s in symbols}

    result = run_weekly(CFG, fb, load_bars, priced, raw_closes, now=NOW, today=TODAY, dry_run=False,
                        alert=lambda *a: None, state_dir=tmp_path,
                        executor_factory=partial(Executor, sleep=fb.sleep, monotonic=fb.monotonic))
    assert ("sell" in fb.writes) is sold
    assert any(o.reason.startswith("stop_breached") for o in result.decision.approved) is sold
