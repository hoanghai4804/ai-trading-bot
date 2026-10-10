"""Costs, metrics on series with known answers, benchmarks, reproducibility."""

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import pytest
from test_backtest_engine import CFG as SYN_CFG, M, MONDAYS, make_bars
from test_backtest_parity import BARS, CFG

from tradingbot.backtest.benchmarks import (RandomStrategy, buy_and_hold, calibrate_random, distribution,
                                            sma_timing)
from tradingbot.backtest.costs import CostModel
from tradingbot.backtest.engine import fixed_strategy, run_backtest
from tradingbot.backtest.metrics import (annualized_return, compute_metrics, max_drawdown, sharpe_ratio,
                                         total_return, yearly_returns)
from tradingbot.backtest.report import Period, _random_job, exposure_matched

COSTS = CostModel(CFG.costs)


# --- costs ------------------------------------------------------------------------------------

def test_buy_slippage_against_us():
    fill = COSTS.buy(100.0, 10)
    assert fill.price == pytest.approx(100.05) and fill.fees == 0 and fill.slippage == pytest.approx(0.5)


def test_sell_fees_sec_and_taf_rounded_up_to_cent():
    fill = COSTS.sell(200.0, 100)                       # 199.90 x 100 = 19,990
    assert fill.price == pytest.approx(199.9)
    assert fill.fees == pytest.approx(0.56 + 0.02)      # SEC 0.5557 -> 0.56, TAF 0.0166 -> 0.02
    assert fill.slippage == pytest.approx(10.0)


def test_taf_is_capped():
    assert COSTS.sell_fees(1.0, 1_000_000) == pytest.approx(math.ceil(1_000_000 * 0.0000278 * 100) / 100 + 8.30)


# --- metrics ----------------------------------------------------------------------------------

def _series(values, start="2024-01-01"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


def test_metrics_on_known_series():
    eq = _series([100, 110, 99, 121])
    assert total_return(eq) == pytest.approx(0.21)
    assert max_drawdown(eq) == pytest.approx(0.10)
    r = np.array([0.10, -0.10, 121 / 99 - 1])
    assert sharpe_ratio(eq) == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252))
    assert annualized_return(eq) == pytest.approx(1.21 ** (252 / 3) - 1)


def test_annualized_return_two_years():
    eq = _series(np.linspace(100, 121, 505))           # 504 daily returns = 2 years
    assert annualized_return(eq) == pytest.approx(0.10)


def test_flat_series_has_nan_sharpe_and_zero_drawdown():
    eq = _series([100.0] * 10)
    assert math.isnan(sharpe_ratio(eq)) and max_drawdown(eq) == 0 and total_return(eq) == 0


def test_yearly_returns_chain_from_start_value():
    eq = pd.Series([110.0, 121.0], index=pd.to_datetime(["2024-12-31", "2025-12-31"]))
    assert yearly_returns(eq, 100.0).tolist() == pytest.approx([0.10, 0.10])


@dataclass
class T:
    pnl: float
    holding_days: int
    costs: float
    is_open: bool = False


def test_trade_stats_use_closed_trades_only():
    m = compute_metrics(_series([100, 101]), _series([0.0, 0.5]),
                        [T(10, 5, 1.0), T(-5, 15, 1.0), T(3, 10, 0.5), T(100, 1, 0.5, is_open=True)])
    assert (m.trades, m.open_trades) == (3, 1)
    assert m.win_rate == pytest.approx(2 / 3) and m.avg_holding_days == pytest.approx(10)
    assert m.costs == pytest.approx(3.0) and m.exposure == pytest.approx(0.25)


def test_distribution_median_and_percentiles():
    med, p5, p95 = distribution(list(range(101)) + [float("nan")])
    assert (med, p5, p95) == (50, 5, 95)


# --- benchmarks ---------------------------------------------------------------------------------

def test_buy_and_hold_matches_hand_computation():
    spy = BARS["SPY"]
    start, end = date(2025, 6, 2), date(2026, 4, 24)
    r = buy_and_hold(CFG, spy, start, end)
    window = spy.loc[pd.Timestamp(start):pd.Timestamp(end)]
    by_hand = window["close"].iloc[-1] / window["open"].iloc[0] - 1
    assert total_return(r.equity) == pytest.approx((1 + by_hand) / (1 + CFG.costs.slippage_bps / 1e4) - 1, rel=1e-12)
    assert abs(total_return(r.equity) - by_hand) < 0.001
    assert r.equity.iloc[0] == CFG.backtest.initial_capital and (r.exposure.iloc[1:] == 1).all()


def test_sma_timing_goes_to_cash_below_the_average():
    idx = pd.bdate_range("2024-01-01", periods=400)
    close = np.r_[np.linspace(100, 200, 300), np.linspace(200, 120, 100)]
    frame = pd.DataFrame({"open": close, "high": close, "low": close, "close": close}, index=idx)
    r = sma_timing(CFG, frame, idx[250].date(), idx[-1].date(), window=200)
    assert r.exposure.iloc[1:30].eq(1).all()           # uptrend: invested
    assert r.exposure.iloc[-5:].eq(0).all()            # well below SMA200: cash
    assert [t.exit_reason for t in r.trades] == ["signal"]


def test_exposure_matched_benchmark():
    bench = buy_and_hold(CFG, BARS["SPY"], date(2025, 6, 2), date(2026, 4, 24))
    half = exposure_matched(bench, 0.5)
    r = bench.equity.pct_change().fillna(0)
    assert half.iloc[-1] == pytest.approx(bench.equity.iloc[0] * float((1 + 0.5 * r).prod()))
    assert exposure_matched(bench, 1.0).iloc[-1] == pytest.approx(bench.equity.iloc[-1])


def test_random_benchmark_same_seed_same_result_different_seed_differs():
    def run(seed):
        return run_backtest(SYN_CFG, make_bars(), M, MONDAYS[30],
                            lambda s, w: RandomStrategy(seed, p_entry=0.3, p_exit=0.2), name=f"r{seed}")
    a, b, c = run(7), run(7), run(8)
    assert a.equity.equals(b.equity)
    assert [(t.symbol, t.entry_date, t.exit_date) for t in a.trades] == \
        [(t.symbol, t.entry_date, t.exit_date) for t in b.trades]
    assert not a.equity.equals(c.equity)


def test_random_strategy_draw_does_not_depend_on_future_rows():
    strat = RandomStrategy(3, 0.5, 0.5)
    as_of = date(2026, 3, 13)
    full = strat.generate_signals(BARS, as_of, positions=["AAPL"])
    cut = strat.generate_signals({s: f.loc[:pd.Timestamp(as_of)] for s, f in BARS.items()}, as_of, positions=["AAPL"])
    assert full == cut


def test_calibrate_random_from_main_run():
    r = run_backtest(CFG, BARS, date(2025, 9, 2), date(2026, 4, 27), fixed_strategy(CFG))
    p_entry, p_exit = calibrate_random(r, len(CFG.watchlist))
    assert 0 < p_entry < 1 and 0 < p_exit <= 1


def test_random_job_applies_the_configured_risk_rules():
    """The report's random benchmark = same engine + same risk config (sizing, stop, weekly limit)."""
    period = Period("t", date(2025, 9, 2), date(2026, 4, 27))
    r = _random_job((CFG, BARS, period, 5, 0.6, 0.1))
    direct = run_backtest(CFG, BARS, period.start, period.end, lambda s, w: RandomStrategy(5, 0.6, 0.1))
    assert r.equity.equals(direct.equity)
    assert r.trades and r.blocked_counts()["weekly_limit"] > 0
    for week in r.weeks:
        equity = week.decision.breakers.current_equity
        for o in (o for o in week.decision.approved if o.side == "buy"):
            assert o.qty * o.ref_price <= CFG.risk.max_position_pct * equity + 1e-6
            assert 0 < o.stop_price < o.ref_price
            assert (o.ref_price - o.stop_price) * o.qty <= CFG.risk.risk_per_trade_pct * equity + 1e-6
