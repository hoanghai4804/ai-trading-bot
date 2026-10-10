import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tradingbot.config import load_config
from tradingbot.indicators.technical import atr, return_correlation
from tradingbot.risk.circuit_breakers import BreakerStatus
from tradingbot.risk.manager import AccountState, RiskManager
from tradingbot.risk.sizing import compute_stop
from tradingbot.strategies.base import Signal

RISK = load_config().risk
WATCH = ["AAA", "BBB", "CCC", "DDD", "TWIN"]
N = 120
IDX = pd.date_range("2026-04-01", periods=N, freq="B")
AS_OF = IDX[-1].date()


def _frame(close, spread=0.01):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({"open": close, "high": close * (1 + spread), "low": close * (1 - spread),
                         "close": close, "volume": 1e6}, index=IDX[-len(close):])


def _walk(seed, vol=0.01):
    rng = np.random.default_rng(seed)
    return 100 * np.cumprod(1 + rng.normal(0, vol, N))


BASE = {s: _walk(i) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"])}
BARS = {s: _frame(c) for s, c in BASE.items()}
BARS["TWIN"] = _frame(BASE["AAA"] * 2)  # same returns as AAA -> corr 1.0
REF = {s: float(b["close"].iloc[-1]) for s, b in BARS.items()}

OK = BreakerStatus(halt=False, block_new_entries=False, reasons=(), previous_week_return=0.0,
                   drawdown=0.0, peak_equity=100_000, current_equity=100_000)
WEEKLY = BreakerStatus(halt=False, block_new_entries=True, reasons=("previous calendar week return -6%",),
                       previous_week_return=-0.06, drawdown=0.06, peak_equity=100_000, current_equity=94_000)
HALT = BreakerStatus(halt=True, block_new_entries=True, reasons=("drawdown 11% >= 10%",),
                     previous_week_return=-0.02, drawdown=0.11, peak_equity=100_000, current_equity=89_000)


def buy(symbol):
    return Signal(symbol=symbol, action="buy", as_of=AS_OF, reason="cross", cross_date=AS_OF, sessions_since_cross=0)


def exit_(symbol):
    return Signal(symbol=symbol, action="exit", as_of=AS_OF, reason="MA20 < MA50")


def account(positions=None, cash=100_000.0, equity=100_000.0, week=0):
    return AccountState(equity=equity, cash=cash, positions=positions or {}, new_positions_this_week=week)


def review(signals, acct=None, bars=BARS, ref=REF, breakers=OK, as_of=AS_OF):
    return RiskManager(RISK, WATCH).review(signals, as_of, acct or account(), bars, ref, breakers)


def codes(decision):
    return {b.symbol: b.code for b in decision.blocked}


# --- approved buys -------------------------------------------------------------------

def test_buy_is_sized_with_atr_stop_and_respects_both_limits():
    d = review([buy("AAA")])
    [o] = d.approved
    expected_stop = compute_stop(REF["AAA"], atr(BARS["AAA"], 14).iloc[-1], 2.0)
    assert (o.side, o.stop_price, o.ref_price) == ("buy", expected_stop, REF["AAA"])
    assert o.risk_amount <= 0.01 * 100_000 + 1e-6
    assert o.notional <= 0.05 * 100_000 + 1e-6
    assert o.qty == int(o.qty) >= 1
    assert o.limited_by == "position_cap"  # 1% spread -> small ATR -> 5% cap binds
    assert d.blocked == []


def test_high_volatility_buy_is_limited_by_risk():
    bars = {"AAA": _frame(BASE["AAA"], spread=0.08)}
    [o] = review([buy("AAA")], bars=bars).approved
    assert o.limited_by == "risk"
    assert o.risk_amount <= 1_000 + 1e-6 and o.notional < 5_000


# --- exits ---------------------------------------------------------------------------

def test_exit_sells_full_position_even_under_weekly_breaker():
    d = review([exit_("AAA"), buy("BBB")], acct=account({"AAA": 12.5}), breakers=WEEKLY)
    [o] = d.approved
    assert (o.symbol, o.side, o.qty) == ("AAA", "sell", 12.5)
    assert codes(d) == {"BBB": "weekly_loss_breaker"}


def test_exit_of_unheld_symbol_is_blocked_no_short():
    d = review([exit_("AAA")])
    assert d.approved == [] and codes(d) == {"AAA": "not_held"}


def test_short_position_is_refused():
    with pytest.raises(ValueError, match="short"):
        account({"AAA": -5})


# --- breakers ------------------------------------------------------------------------

def test_drawdown_halt_liquidates_everything_and_blocks_buys():
    d = review([exit_("AAA"), buy("CCC")], acct=account({"AAA": 10, "BBB": 7}), breakers=HALT)
    assert d.halt
    assert [(o.symbol, o.qty) for o in d.approved] == [("AAA", 10), ("BBB", 7)]
    assert next(o for o in d.approved if o.symbol == "BBB").reason == "drawdown breaker: liquidate"
    assert codes(d) == {"CCC": "drawdown_halt"}


# --- simple vetoes -------------------------------------------------------------------

def test_already_held_unknown_symbol_and_duplicate():
    d = review([buy("AAA"), buy("ZZZ"), buy("BBB"), buy("BBB")], acct=account({"AAA": 3}))
    assert [o.symbol for o in d.approved] == ["BBB"]
    assert [(b.symbol, b.code) for b in d.blocked] == [
        ("AAA", "already_held"), ("ZZZ", "not_in_watchlist"), ("BBB", "duplicate")]


@pytest.mark.parametrize("already,approved", [(0, 3), (2, 1), (3, 0)])
def test_weekly_limit_counts_order_history_plus_this_run(already, approved):
    d = review([buy("AAA"), buy("BBB"), buy("CCC"), buy("DDD")], acct=account(week=already))
    assert [o.symbol for o in d.approved] == ["AAA", "BBB", "CCC"][:approved]
    assert [b.code for b in d.blocked] == ["weekly_limit"] * (4 - approved)


def test_missing_reference_price_and_data():
    ref = {**REF, "AAA": float("nan")}
    bars = {k: v for k, v in BARS.items() if k != "BBB"}
    d = review([buy("AAA"), buy("BBB")], bars=bars, ref={k: v for k, v in ref.items() if k != "CCC"})
    assert codes(d) == {"AAA": "no_reference_price", "BBB": "no_data"}


def test_atr_unavailable_with_short_history():
    bars = {**BARS, "AAA": BARS["AAA"].iloc[-10:]}
    assert codes(review([buy("AAA")], bars=bars)) == {"AAA": "atr_unavailable"}


@pytest.mark.parametrize("move,code", [(1.031, "gap_too_large"), (0.965, "gap_too_large")])
def test_gap_vs_last_close(move, code):
    ref = {**REF, "AAA": REF["AAA"] * move}
    assert codes(review([buy("AAA")], ref=ref)) == {"AAA": code}


def test_stop_below_zero_is_invalid():
    bars = {**BARS, "AAA": _frame(BASE["AAA"], spread=0.6)}  # ATR ~ 120% of price
    assert codes(review([buy("AAA")], bars=bars)) == {"AAA": "invalid_stop"}


# --- correlation ---------------------------------------------------------------------

def test_correlated_with_held_position_is_blocked():
    d = review([buy("TWIN")], acct=account({"AAA": 10}))
    [b] = d.blocked
    assert b.code == "correlation" and "AAA" in b.detail and "1.00" in b.detail


def test_correlated_with_buy_approved_earlier_in_run_is_blocked():
    d = review([buy("AAA"), buy("TWIN"), buy("BBB")])
    assert [o.symbol for o in d.approved] == ["AAA", "BBB"]
    assert codes(d) == {"TWIN": "correlation"}


def test_position_being_sold_now_is_not_a_correlation_partner():
    d = review([exit_("AAA"), buy("TWIN")], acct=account({"AAA": 10}))
    assert sorted((o.symbol, o.side) for o in d.approved) == [("AAA", "sell"), ("TWIN", "buy")]


def _blend(weight, seed=99):
    """Closes whose daily returns are weight * AAA's returns + noise: sample corr with AAA ~= weight."""
    r_a = np.diff(BASE["AAA"]) / BASE["AAA"][:-1]
    noise = np.random.default_rng(seed).normal(0, r_a.std(), len(r_a))
    r = weight * r_a + np.sqrt(1 - weight ** 2) * noise
    return _frame(100 * np.concatenate([[1.0], np.cumprod(1 + r)]))


@pytest.mark.parametrize("weight,low,high,blocked", [
    (0.9, 0.80, 0.99, True),    # between threshold 0.8 and 1.0 -> must be blocked
    (0.7, 0.55, 0.80, False),   # below threshold -> allowed
])
def test_correlation_threshold_is_the_configured_value(weight, low, high, blocked):
    bars = {**BARS, "BBB": _blend(weight)}
    corr = return_correlation(pd.DataFrame({"AAA": bars["AAA"]["close"], "BBB": bars["BBB"]["close"]}),
                              RISK.correlation.lookback_days).loc["AAA", "BBB"]
    assert low < corr < high  # guards the fixture itself
    d = review([buy("BBB")], acct=account({"AAA": 10}), bars=bars,
               ref={**REF, "BBB": float(bars["BBB"]["close"].iloc[-1])})
    assert (codes(d) == {"BBB": "correlation"}) is blocked
    assert bool(d.approved) is not blocked


def test_unknown_correlation_blocks():
    bars = {**BARS, "BBB": BARS["BBB"].iloc[-30:]}  # held, but only 29 returns < 60
    assert codes(review([buy("AAA")], acct=account({"BBB": 5}), bars=bars)) == {"AAA": "correlation_unknown"}
    bars = {k: v for k, v in BARS.items() if k != "BBB"}  # held, no bars at all
    assert codes(review([buy("AAA")], acct=account({"BBB": 5}), bars=bars)) == {"AAA": "correlation_unknown"}


# --- cash ----------------------------------------------------------------------------

def test_cash_is_shared_across_buys_and_keeps_buffer():
    cash = 7_000.0
    d = review([buy("AAA"), buy("BBB"), buy("CCC")], acct=account(cash=cash))
    spent = sum(o.notional for o in d.approved)
    assert spent <= cash * (1 - RISK.cash_buffer_pct)
    assert d.approved[0].limited_by == "position_cap"
    assert d.approved[1].limited_by == "cash"
    assert codes(d) == {"CCC": "insufficient_cash"}


def test_size_zero_when_one_share_exceeds_position_cap():
    d = review([buy("AAA")], acct=account(equity=1_000, cash=1_000))  # 5% of 1000 = 50 < price ~100
    assert codes(d) == {"AAA": "size_zero"}


# --- combined / properties -----------------------------------------------------------

def test_every_blocked_order_has_code_and_detail_and_decision_is_json():
    signals = [exit_("CCC"), buy("AAA"), buy("TWIN"), buy("ZZZ"), buy("BBB"), buy("DDD")]
    d = review(signals, acct=account({"AAA": 1}, week=2))
    assert d.blocked and all(b.code and b.detail for b in d.blocked)
    assert codes(d) == {"CCC": "not_held", "AAA": "already_held", "TWIN": "correlation",
                        "ZZZ": "not_in_watchlist", "DDD": "weekly_limit"}
    assert [o.symbol for o in d.approved] == ["BBB"]
    payload = json.loads(json.dumps(d.to_dict()))
    assert payload["as_of"] == AS_OF.isoformat() and len(payload["blocked"]) == 5


def test_no_lookahead_future_bars_do_not_change_decision():
    as_of = IDX[-21].date()
    past = {s: b.loc[:pd.Timestamp(as_of)] for s, b in BARS.items()}
    ref = {s: float(b["close"].iloc[-1]) for s, b in past.items()}
    signals = [buy("AAA"), buy("TWIN"), buy("BBB")]
    full_d = review(signals, bars=BARS, ref=ref, as_of=as_of)
    past_d = review(signals, bars=past, ref=ref, as_of=as_of)
    assert full_d == past_d and full_d.approved
