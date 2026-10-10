"""Phase 8 monitoring: equity log, execution quality, shadow check (no network)."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fake_broker import NOW, FakeBroker, equity_points
from test_pipeline import CFG, TODAY, run

import tradingbot.pipeline as pipeline
from tradingbot.broker.alpaca_client import OrderSnapshot
from tradingbot.monitoring.execution_quality import (append_fills, collect_fills, load_order_refs, load_quality,
                                                     slippage_bps, slippage_stats)
from tradingbot.monitoring.shadow import shadow_check
from tradingbot.reporting.equity_log import load_equity_log, performance, upsert_equity
from tradingbot.risk.manager import RiskDecision, RiskManager


# --- equity log -----------------------------------------------------------------------------------

def test_equity_log_upsert_by_day(tmp_path):
    upsert_equity(date(2026, 10, 12), 100_000, 100_000, "SPY", 700.0, tmp_path)
    upsert_equity(date(2026, 10, 12), 100_500, 90_000, "SPY", 701.0, tmp_path)
    upsert_equity(date(2026, 10, 5), 99_000, 99_000, "SPY", None, tmp_path)
    rows = load_equity_log(tmp_path)
    assert [(r["date"], r["equity"], r["benchmark_price"]) for r in rows] == [
        ("2026-10-05", "99000.00", ""), ("2026-10-12", "100500.00", "701.0000")]


def test_performance_since_last_run_and_start(tmp_path):
    for d, eq, px in (("2026-09-28", 100_000, 600.0), ("2026-10-05", 101_000, 610.0)):
        upsert_equity(date.fromisoformat(d), eq, eq, "SPY", px, tmp_path)
    perf = performance(load_equity_log(tmp_path), date(2026, 10, 12), 102_010, 640.5)
    assert perf["since_last_run"]["from"] == "2026-10-05"
    assert perf["since_last_run"]["return"] == pytest.approx(0.01)
    assert perf["since_last_run"]["benchmark_return"] == pytest.approx(0.05)
    assert perf["since_start"]["return"] == pytest.approx(0.0201)
    assert perf["since_start"]["benchmark_return"] == pytest.approx(0.0675)
    assert performance([], TODAY, 1.0, 1.0) == {"since_last_run": None, "since_start": None}


def test_rerun_same_day_does_not_compare_with_itself(tmp_path):
    upsert_equity(TODAY, 100_000, 100_000, "SPY", 700.0, tmp_path)
    assert performance(load_equity_log(tmp_path), TODAY, 101_000, 701.0)["since_last_run"] is None


def test_execute_run_logs_equity_once_per_day_dry_run_never(tmp_path):
    run(FakeBroker(), tmp_path, dry_run=True)
    assert load_equity_log(tmp_path) == []
    fb = FakeBroker()
    run(fb, tmp_path)
    run(fb, tmp_path)
    assert [r["date"] for r in load_equity_log(tmp_path)] == [TODAY.isoformat()]


def test_local_equity_log_is_a_peak_source_for_the_drawdown_breaker(tmp_path):
    upsert_equity(TODAY - timedelta(days=7), 120_000, 120_000, "SPY", 700.0, tmp_path)
    alerts = []
    result = run(FakeBroker(history=equity_points({"2026-10-09": 100_000})), tmp_path, dry_run=True, alerts=alerts)
    assert result.decision.breakers.halt and result.decision.breakers.peak_equity == 120_000


# --- execution quality ----------------------------------------------------------------------------

def order(oid, cid, side, typ, fill, qty=10, stop=None, filled_at=NOW, legs=()):
    return OrderSnapshot(oid, cid, "AAPL", side, typ, "simple", "filled", qty, qty, fill, stop, filled_at,
                         filled_at, tuple(legs))


def test_slippage_sign_is_cost_positive():
    assert slippage_bps("buy", 100.0, 100.05) == pytest.approx(5.0)
    assert slippage_bps("sell", 100.0, 99.95) == pytest.approx(5.0)
    assert slippage_bps("buy", 100.0, 99.9) == pytest.approx(-10.0)


def test_collect_fills_refs_and_filters():
    leg = order("o2", "auto-leg", "sell", "stop", 97.0, stop=98.0)
    orders = [
        order("o1", "tb-2026-10-12-AAPL-buy", "buy", "market", 100.1, legs=[leg]),
        order("o3", "tb-2026-10-12-MSFT-sell", "sell", "market", 49.9),
        order("o4", "manual-1", "buy", "market", 10.0),                       # not a bot order
        order("o5", "tb-2026-10-12-XOM-buy", "buy", "market", 10.0),          # no known reference
        order("o6", "tb-2026-10-05-JNJ-buy", "buy", "market", 10.0, filled_at=NOW - timedelta(days=8)),
    ]
    refs = {"tb-2026-10-12-AAPL-buy": 100.0, "tb-2026-10-12-MSFT-sell": 50.0, "tb-2026-10-05-JNJ-buy": 10.0}
    fills = collect_fills(orders, "tb", NOW - timedelta(days=7), refs)
    got = {(f.order_id, f.ref_source, f.ref_price, round(f.slippage_bps, 1)) for f in fills}
    assert got == {("o1", "decision", 100.0, 10.0), ("o2", "stop", 98.0, 102.0), ("o3", "decision", 50.0, 20.0)}


def test_append_fills_is_idempotent_by_order_id(tmp_path):
    fills = collect_fills([order("o1", "tb-2026-10-12-AAPL-buy", "buy", "market", 100.05)], "tb", None,
                          {"tb-2026-10-12-AAPL-buy": 100.0})
    assert len(append_fills(fills, tmp_path)) == 1
    assert append_fills(fills, tmp_path) == []
    rows = load_quality(tmp_path)
    assert len(rows) == 1 and float(rows[0]["slippage_bps"]) == pytest.approx(5.0)
    stats = slippage_stats(rows)
    assert stats["market"] == {"n": 1, "mean_bps": pytest.approx(5.0)} and stats["stop"]["n"] == 0


def test_order_refs_come_from_execute_runs_only(tmp_path):
    for day, mode, refs in (("2026-10-05", "EXECUTE", {"a": 1.0}), ("2026-10-12", "DRY-RUN", {"b": 2.0})):
        (tmp_path / day).mkdir()
        (tmp_path / day / "decisions.json").write_text(json.dumps({"mode": mode, "order_refs": refs}))
    assert load_order_refs(tmp_path) == {"a": 1.0}


def test_pipeline_records_fills_of_this_run_and_of_earlier_runs(tmp_path):
    fb = FakeBroker(prices={"AAPL": 102.05})
    first = run(fb, tmp_path)
    assert [(f.client_order_id, f.ref_source) for f in first.fills] == [("tb-2026-10-12-AAPL-buy", "decision")]
    assert first.fills[0].slippage_bps == pytest.approx(4.9, abs=0.1)
    assert first.order_refs == {"tb-2026-10-12-AAPL-buy": 102.0}
    assert len(load_quality(tmp_path)) == 1
    run(fb, tmp_path)                                       # rerun: nothing new, no duplicate row
    assert len(load_quality(tmp_path)) == 1


# --- shadow check ---------------------------------------------------------------------------------

def test_shadow_check_matches_on_a_normal_run(tmp_path):
    alerts = []
    result = run(FakeBroker(), tmp_path, alerts=alerts)
    assert result.shadow.match and result.shadow.live == [("AAPL", "buy")]
    assert not any("shadow" in m for _, m in alerts)


def test_shadow_check_detects_a_deliberate_divergence(tmp_path, monkeypatch):
    class BlockAllBuys(RiskManager):
        def review(self, signals, *a, **kw):
            return super().review([s for s in signals if s.action != "buy"], *a, **kw)

    monkeypatch.setattr(pipeline, "RiskManager", BlockAllBuys)
    alerts = []
    result = run(FakeBroker(), tmp_path, dry_run=True, alerts=alerts)
    assert not result.shadow.match
    assert result.shadow.only_shadow == [("AAPL", "buy")] and result.shadow.only_live == []
    assert any(level == "warning" and "shadow check MISMATCH" in m for level, m in alerts)
    assert any("shadow check MISMATCH" in w for w in result.warnings)


def test_shadow_check_detects_an_extra_live_order(tmp_path):
    result = run(FakeBroker(), tmp_path, dry_run=True)
    b = result.decision.breakers
    res = shadow_check(CFG, [("AAPL", "buy"), ("MSFT", "buy")], TODAY, _bars(), {}, {}, None, 100_000, 100_000,
                       {"AAPL": 102.0}, b, 0)
    assert not res.match and res.only_live == [("MSFT", "buy")]
    assert res.describe().startswith("MISMATCH")


def test_shadow_check_error_is_reported_not_raised(tmp_path):
    result = run(FakeBroker(), tmp_path, dry_run=True)
    res = shadow_check(CFG, [], TODAY, _bars(), {}, {}, None, -1.0, 0.0, {}, result.decision.breakers, 0)
    assert not res.match and res.error and res.describe().startswith("ERROR")


def _bars():
    from test_pipeline import BARS
    return BARS
