"""Engine mechanics on synthetic bars with a scripted strategy (real RiskManager, costs, breakers)."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest.engine import run_backtest
from tradingbot.config import load_config
from tradingbot.indicators.technical import atr
from tradingbot.risk.sizing import compute_stop
from tradingbot.strategies.base import Signal, Strategy

BASE_CFG = load_config()
SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE"]
CFG = BASE_CFG.model_copy(update={"watchlist": SYMS})
SLIP = CFG.costs.slippage_bps / 10_000
IDX = pd.bdate_range("2025-01-06", periods=160)          # starts on a Monday
MONDAYS = [d.date() for d in IDX if d.weekday() == 0]
M = MONDAYS[20]                                          # first test Monday (> 60 sessions of history)


def _walk(seed: int, n: int = len(IDX), vol: float = 0.01) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100 * np.cumprod(1 + rng.normal(0, vol, n))


def make_bars(spread: float = 0.01) -> dict[str, pd.DataFrame]:
    out = {}
    for k, s in enumerate(SYMS):
        close = _walk(k + 1)
        open_ = np.r_[close[0], close[:-1]]
        out[s] = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + spread),
                               "low": np.minimum(open_, close) * (1 - spread), "close": close,
                               "volume": 1e6}, index=IDX)
    return out


class Scripted(Strategy):
    name = "scripted"
    min_history = 1

    def __init__(self, buys=(), exits=()):
        self.buys, self.exits = list(buys), list(exits)

    def generate_signals(self, bars, as_of, positions, last_exit=None):
        out = [Signal(s, "exit", as_of, "test exit") for s in self.exits if s in positions]
        return out + [Signal(s, "buy", as_of, "test buy", cross_date=as_of, sessions_since_cross=0)
                      for s in self.buys if s not in positions]


def scripted(plan):
    """plan: {monday: (buys, exits)}; `every` key applies to all other Mondays."""
    return lambda session, window: Scripted(*plan.get(session, plan.get("every", ((), ()))))


def ts(d) -> pd.Timestamp:
    return pd.Timestamp(d)


def nxt(d: date, n: int = 1) -> date:
    return IDX[IDX.get_loc(ts(d)) + n].date()


# --- entry -------------------------------------------------------------------------------------

def test_entry_fills_at_monday_open_plus_slippage_with_live_stop():
    bars = make_bars()
    friday_close = bars["AAA"].loc[ts(M) - pd.Timedelta(days=3), "close"]
    bars["AAA"].loc[ts(M), "open"] = friday_close * 1.02    # Monday open differs from Friday close
    r = run_backtest(CFG, bars, M, M, scripted({M: (["AAA"], [])}))
    [t] = r.trades
    open_ = bars["AAA"].loc[ts(M), "open"]
    assert t.ref_price == open_
    assert t.entry_price == pytest.approx(open_ * (1 + SLIP), rel=1e-12)
    window = bars["AAA"].loc[ts(M - timedelta(days=CFG.data.history_days)):ts(M - timedelta(days=1))]
    assert t.stop == compute_stop(open_, float(atr(window, CFG.risk.atr_period).iloc[-1]),
                                  CFG.risk.atr_stop_multiple)
    [week] = r.weeks
    assert week.as_of == M - timedelta(days=1)


def test_position_cap_and_cash_buffer_respected():
    r = run_backtest(CFG, make_bars(), M, MONDAYS[24], scripted({"every": (SYMS, [])}))
    for t in r.trades:
        eq = r.equity.loc[: ts(t.entry_date) - pd.Timedelta(days=1)].iloc[-1]
        assert t.qty * t.ref_price <= CFG.risk.max_position_pct * eq * 1.01   # equity at open ~ prior close
    assert (r.exposure <= 1).all()


# --- stops ------------------------------------------------------------------------------------

def _entered(bars, sym="AAA"):
    r = run_backtest(CFG, bars, M, M, scripted({M: ([sym], [])}))
    return r.trades[0]


def test_stop_gap_fills_at_open_not_at_stop():
    bars = make_bars()
    t0 = _entered(bars)
    d = nxt(M)
    gap_open = t0.stop * 0.95
    bars["AAA"].loc[ts(d), ["open", "low"]] = [gap_open, gap_open * 0.99]
    r = run_backtest(CFG, bars, M, d, scripted({M: (["AAA"], [])}))
    [t] = r.trades
    assert (t.exit_date, t.exit_reason) == (d, "stop_gap")
    assert t.exit_price == pytest.approx(gap_open * (1 - SLIP), rel=1e-12)
    assert t.exit_price < t.stop


def test_intraday_stop_fills_at_stop_price():
    bars = make_bars()
    t0 = _entered(bars)
    d = nxt(M, 2)
    bars["AAA"].loc[ts(d), ["open", "low"]] = [t0.stop * 1.02, t0.stop * 0.97]
    r = run_backtest(CFG, bars, M, d, scripted({M: (["AAA"], [])}))
    [t] = r.trades
    assert (t.exit_date, t.exit_reason) == (d, "stop")
    assert t.exit_price == pytest.approx(t0.stop * (1 - SLIP), rel=1e-12)


def test_stop_is_never_moved():
    bars = make_bars()
    t0 = _entered(bars)
    r = run_backtest(CFG, bars, M, MONDAYS[23], scripted({M: (["AAA"], [])}))
    assert r.trades[0].stop == t0.stop


# --- costs ------------------------------------------------------------------------------------

def test_round_trip_cash_equals_hand_computation():
    bars = make_bars(spread=0.001)               # tight range: no stop hit
    m2 = MONDAYS[21]
    r = run_backtest(CFG, bars, M, m2, scripted({M: (["AAA"], []), m2: ([], ["AAA"])}))
    [t] = r.trades
    buy = bars["AAA"].loc[ts(M), "open"] * (1 + SLIP)
    sell = bars["AAA"].loc[ts(m2), "open"] * (1 - SLIP)
    notional = sell * t.qty
    fees = np.ceil(round(CFG.costs.sec_fee_rate * notional * 100, 6)) / 100 + \
        min(np.ceil(round(CFG.costs.taf_fee_per_share * t.qty * 100, 6)) / 100, CFG.costs.taf_fee_max)
    expected = CFG.backtest.initial_capital - t.qty * buy + t.qty * sell - fees
    assert t.exit_reason == "signal" and t.exit_fees == pytest.approx(fees) and fees > 0
    assert r.equity.iloc[-1] == pytest.approx(expected, abs=1e-6)
    assert t.costs == pytest.approx(t.qty * bars["AAA"].loc[ts(M), "open"] * SLIP + t.qty *
                                    bars["AAA"].loc[ts(m2), "open"] * SLIP + fees)


def test_costs_lower_the_result_by_exactly_the_costs_paid():
    bars = make_bars(spread=0.001)
    m2 = MONDAYS[21]
    plan = scripted({M: (["AAA"], []), m2: ([], ["AAA"])})
    free = CFG.model_copy(update={"costs": CFG.costs.model_copy(update={
        "slippage_bps": 0, "sec_fee_rate": 0, "taf_fee_per_share": 0})})
    with_costs = run_backtest(CFG, bars, M, m2, plan)
    without = run_backtest(free, bars, M, m2, plan)
    assert with_costs.costs_paid > 0 and without.costs_paid == 0
    assert with_costs.trades[0].qty == without.trades[0].qty
    assert without.equity.iloc[-1] - with_costs.equity.iloc[-1] == pytest.approx(with_costs.costs_paid, abs=1e-6)


# --- calendar -----------------------------------------------------------------------------------

def test_holiday_monday_skips_the_week_and_widens_the_next_window():
    holiday = MONDAYS[22]
    bars = {s: f.drop(index=ts(holiday)) for s, f in make_bars().items()}
    r = run_backtest(CFG, bars, M, MONDAYS[24], scripted({}))
    sessions = [w.session for w in r.weeks]
    assert holiday not in sessions and r.skipped_mondays == [holiday]
    assert sessions == [MONDAYS[20], MONDAYS[21], MONDAYS[23], MONDAYS[24]]
    windows = {w.session: w.window for w in r.weeks}
    assert windows[MONDAYS[21]] == 5
    assert windows[MONDAYS[23]] == 9          # Mon-Fri of last traded week + Tue-Fri of the holiday week
    assert windows[MONDAYS[24]] == 5


# --- risk rules through the engine ---------------------------------------------------------------

def test_weekly_limit_and_correlation_applied():
    bars = make_bars()
    bars["EEE"] = bars["AAA"] * 2                     # same returns as AAA -> corr 1.0
    r = run_backtest(CFG, bars, M, M, scripted({M: (["AAA", "EEE", "BBB", "CCC", "DDD"], [])}))
    [week] = r.weeks
    assert [o.symbol for o in week.decision.approved] == ["AAA", "BBB", "CCC"]
    assert [(b.symbol, b.code) for b in week.decision.blocked] == [("EEE", "correlation"), ("DDD", "weekly_limit")]
    assert r.blocked_counts() == {"correlation": 1, "weekly_limit": 1}


def _crash_setup(drop: float):
    """Three 30% positions entered at M (wide ATR so stops sit far away), then a steady
    decline of `drop` during the week after (5 sessions), then flat; buys proposed every Monday."""
    cfg = CFG.model_copy(update={"risk": CFG.risk.model_copy(update={
        "max_position_pct": 0.3, "risk_per_trade_pct": 0.3})})
    bars = make_bars(spread=0.08)
    start = MONDAYS[18]
    i0, i1 = IDX.get_loc(ts(M)), IDX.get_loc(ts(MONDAYS[21]))
    for s in ("AAA", "BBB", "CCC"):
        f = bars[s]
        base = f["close"].iloc[i0]
        n_rest = len(IDX) - i1 - 5
        path = np.r_[np.full(i1 - i0, base), np.linspace(base, base * (1 - drop), 5), np.full(n_rest, base * (1 - drop))]
        f.iloc[i0:, f.columns.get_loc("close")] = path
        f.iloc[i0:, f.columns.get_loc("open")] = np.r_[base, path[:-1]]
        f.iloc[i0:, f.columns.get_loc("high")] = np.maximum(f["open"], f["close"]).iloc[i0:] * 1.001
        f.iloc[i0:, f.columns.get_loc("low")] = np.minimum(f["open"], f["close"]).iloc[i0:] * 0.999
        bars[s] = f
    bars["DDD"].iloc[i0:] = bars["DDD"].iloc[i0:].assign(high=lambda f: f[["open", "close"]].max(axis=1) * 1.001,
                                                          low=lambda f: f[["open", "close"]].min(axis=1) * 0.999)
    plan = scripted({start: ([], []), MONDAYS[19]: ([], []), M: (["AAA", "BBB", "CCC"], []),
                     "every": (["DDD", "EEE"], [])})
    return cfg, bars, start, plan


def test_drawdown_halt_liquidates_records_event_and_continues():
    cfg, bars, start, plan = _crash_setup(drop=0.25)
    r = run_backtest(cfg, bars, start, MONDAYS[26], plan)
    halts = [e for e in r.events if e.kind == "drawdown_halt"]
    assert halts, "drawdown breaker never tripped"
    e = halts[0]
    assert e.drawdown >= cfg.risk.circuit_breakers.max_drawdown_pct
    assert set(e.positions_sold) >= {"AAA", "BBB", "CCC"}
    sold = [t for t in r.trades if t.exit_reason == "drawdown_halt"]
    assert {t.symbol for t in sold} >= {"AAA", "BBB", "CCC"} and all(t.exit_date == e.session for t in sold)
    halt_week = next(w for w in r.weeks if w.session == e.session)
    buy_blocks = [b.code for b in halt_week.decision.blocked if b.side == "buy"]
    assert buy_blocks and set(buy_blocks) == {"drawdown_halt"}
    assert not [o for o in halt_week.decision.approved if o.side == "buy"]
    # continue mode: weeks after the halt still run, with the peak reset at the halt session
    later = [w for w in r.weeks if w.session > e.session]
    assert later and not later[0].decision.breakers.halt
    assert later[0].decision.breakers.peak_equity <= e.equity * 1.05


def test_drawdown_halt_stop_mode_stays_in_cash():
    cfg, bars, start, plan = _crash_setup(drop=0.25)
    r = run_backtest(cfg, bars, start, MONDAYS[26], plan, halt_mode="stop")
    e = next(e for e in r.events if e.kind == "drawdown_halt")
    assert all(w.session <= e.session for w in r.weeks)
    assert (r.exposure.loc[ts(e.session):] == 0).all()


def test_weekly_loss_breaker_blocks_buys_but_keeps_positions():
    cfg, bars, start, plan = _crash_setup(drop=0.09)
    r = run_backtest(cfg, bars, start, MONDAYS[23], plan)
    weekly = [e for e in r.events if e.kind == "weekly_loss"]
    assert weekly and not [e for e in r.events if e.kind == "drawdown_halt"]
    e = weekly[0]
    assert e.previous_week_return < -cfg.risk.circuit_breakers.weekly_loss_pct
    week = next(w for w in r.weeks if w.session == e.session)
    assert [(b.symbol, b.code) for b in week.decision.blocked] == [("EEE", "weekly_loss_breaker")]
    assert e.positions_sold == () and not week.decision.approved
    assert {t.symbol for t in r.trades if t.is_open} >= {"AAA", "BBB", "CCC", "DDD"}


def test_no_negative_cash_ever():
    r = run_backtest(CFG, make_bars(), MONDAYS[12], MONDAYS[30], scripted({"every": (SYMS, [])}))
    invested = r.exposure * r.equity
    assert ((r.equity - invested) >= -1e-6).all()


def test_stop_checked_on_the_entry_day():
    bars = make_bars()
    t0 = _entered(bars)
    bars["AAA"].loc[ts(M), "low"] = t0.stop * 0.99
    r = run_backtest(CFG, bars, M, nxt(M), scripted({M: (["AAA"], [])}))
    [t] = r.trades
    assert (t.entry_date, t.exit_date, t.exit_reason) == (M, M, "stop")


class Spy(Scripted):
    seen: list = []

    def generate_signals(self, bars, as_of, positions, last_exit=None):
        Spy.seen.append((as_of, max(f.index[-1] for f in bars.values())))
        return super().generate_signals(bars, as_of, positions, last_exit)


def test_strategy_never_receives_rows_after_as_of():
    Spy.seen = []
    run_backtest(CFG, make_bars(), M, MONDAYS[24], lambda session, window: Spy(["AAA"]))
    assert len(Spy.seen) == 5
    assert all(last <= pd.Timestamp(as_of) for as_of, last in Spy.seen)
