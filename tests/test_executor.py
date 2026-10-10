from datetime import date

import pytest
from fake_broker import FakeBroker

from tradingbot.config import load_config
from tradingbot.execution.executor import Executor
from tradingbot.risk.circuit_breakers import BreakerStatus
from tradingbot.risk.manager import ApprovedOrder, RiskDecision
from tradingbot.safety import read_halt

CFG = load_config().execution
TODAY = date(2026, 10, 12)
OK = BreakerStatus(halt=False, block_new_entries=False, reasons=(), previous_week_return=0.0,
                   drawdown=0.0, peak_equity=100_000, current_equity=100_000)
HALT = BreakerStatus(halt=True, block_new_entries=True, reasons=("drawdown 11.00% >= 10%",),
                     previous_week_return=-0.02, drawdown=0.11, peak_equity=100_000, current_equity=89_000)


def buy(symbol, qty=10, stop=95.0):
    return ApprovedOrder(symbol, "buy", qty, "cross", ref_price=100.0, stop_price=stop, notional=qty * 100.0)


def sell(symbol, qty):
    return ApprovedOrder(symbol, "sell", qty, "MA20 < MA50")


def decision(*orders, breakers=OK):
    return RiskDecision(as_of=date(2026, 10, 9), breakers=breakers, approved=list(orders))


def run(fb, *orders, dry_run=False, market_open=True, breakers=OK, state_dir=None, alerts=None):
    sink = (lambda level, msg: alerts.append((level, msg))) if alerts is not None else (lambda *a: None)
    ex = Executor(fb, CFG, TODAY, dry_run=dry_run, alert=sink, state_dir=state_dir, sleep=fb.sleep,
                  monotonic=fb.monotonic)
    return ex.execute(decision(*orders, breakers=breakers), market_open)


def statuses(report):
    return [(r.side, r.symbol, r.status) for r in report.results]


# --- dry-run / market closed ---------------------------------------------------------

def test_dry_run_reads_but_never_writes(tmp_path):
    fb = FakeBroker(positions={"MSFT": 5})
    fb.add_stop("MSFT", 5, 90)
    report = run(fb, sell("MSFT", 5), buy("AAPL"), dry_run=True, breakers=HALT, state_dir=tmp_path)
    assert fb.calls == []
    assert read_halt(tmp_path) is None          # dry-run does not write HALT.lock either
    assert statuses(report) == [("halt", "*", "dry_run"), ("sell", "MSFT", "dry_run"), ("buy", "AAPL", "dry_run")]
    assert report.orders_sent == 0
    sell_r = report.results[1]
    assert (sell_r.client_order_id, sell_r.qty, sell_r.stop_price) == ("tb-2026-10-12-MSFT-sell", 5, 90)


def test_market_closed_sends_nothing():
    fb = FakeBroker(positions={"MSFT": 5}, is_open=False)
    report = run(fb, sell("MSFT", 5), buy("AAPL"), market_open=False)
    assert fb.calls == []
    assert [r.status for r in report.results] == ["skipped", "skipped"]
    assert all("market closed" in r.detail for r in report.results)


# --- buys ----------------------------------------------------------------------------

def test_buy_sends_oto_with_stop_and_checks_the_leg():
    fb = FakeBroker()
    alerts = []
    report = run(fb, buy("AAPL", qty=12, stop=95.37), alerts=alerts)
    assert fb.calls == [("buy", "AAPL", 12, 95.37, "tb-2026-10-12-AAPL-buy")]
    assert statuses(report) == [("buy", "AAPL", "filled")]
    assert alerts == [] and fb.positions == {"AAPL": 12}


def test_filled_buy_without_stop_leg_raises_critical_alert():
    fb = FakeBroker()
    fb.buy_stop_leg = False
    alerts = []
    run(fb, buy("AAPL"), alerts=alerts)
    assert [lvl for lvl, _ in alerts] == ["critical", "warning"]   # no leg + _check_stops
    assert "no stop leg" in alerts[0][1]


@pytest.mark.parametrize("outcome,status", [("reject", "failed"), ("raise", "failed"), ("pending", "submitted")])
def test_buy_outcomes(outcome, status):
    fb = FakeBroker()
    fb.on_buy = outcome
    alerts = []
    report = run(fb, buy("AAPL"), alerts=alerts)
    assert statuses(report) == [("buy", "AAPL", status)]
    assert bool(alerts) is (status == "failed")


def test_second_run_sends_no_duplicate_orders():
    fb = FakeBroker(positions={"MSFT": 5})
    fb.add_stop("MSFT", 5, 90)
    fb.on_buy = "pending"
    first = run(fb, sell("MSFT", 5), buy("AAPL"), buy("NVDA"))
    assert first.orders_sent == 3
    n = len(fb.calls)
    second = run(fb, sell("MSFT", 5), buy("AAPL"), buy("NVDA"))
    assert len(fb.calls) == n and second.orders_sent == 0
    assert all(r.status == "skipped" and "already_submitted" in r.detail for r in second.results)


def test_buy_skipped_when_held_or_open_buy_exists():
    fb = FakeBroker(positions={"AAPL": 3})
    fb.add(symbol="NVDA", side="buy", type="limit", status="new", qty=2)   # placed by hand
    report = run(fb, buy("AAPL"), buy("NVDA"))
    assert fb.writes == []
    assert [r.detail for r in report.results] == ["already_held", "already_submitted (open buy order)"]


# --- safe sell -----------------------------------------------------------------------

def test_safe_sell_cancels_stop_first_then_sells():
    fb = FakeBroker(positions={"MSFT": 5})
    stop = fb.add_stop("MSFT", 5, 90)
    report = run(fb, sell("MSFT", 5))
    assert fb.calls == [("cancel", stop.id), ("sell", "MSFT", 5, "tb-2026-10-12-MSFT-sell")]
    assert statuses(report) == [("sell", "MSFT", "filled")] and "MSFT" not in fb.positions


def test_sell_uses_current_position_qty():
    fb = FakeBroker(positions={"MSFT": 7})     # e.g. decision computed before a partial fill
    fb.add_stop("MSFT", 7, 90)
    run(fb, sell("MSFT", 5))
    assert ("sell", "MSFT", 7, "tb-2026-10-12-MSFT-sell") in fb.calls


@pytest.mark.parametrize("outcome", ["reject", "raise", "pending"])
def test_failed_sell_restores_old_stop_and_alerts(outcome):
    fb = FakeBroker(positions={"MSFT": 5})
    fb.add_stop("MSFT", 5, 90.25)
    fb.on_sell = outcome
    alerts = []
    report = run(fb, sell("MSFT", 5), alerts=alerts)
    restores = [c for c in fb.calls if c[0] == "stop"]
    assert len(restores) == 1 and restores[0][1:4] == ("MSFT", 5, 90.25)
    assert restores[0][4].startswith("tb-2026-10-12-MSFT-stop-")
    assert [r.status for r in report.results] == ["failed", "restored"]
    assert alerts and alerts[0][0] == "warning" and "restored" in alerts[0][1]
    if outcome == "pending":   # unfilled sell is canceled before the stop is put back
        assert fb.writes == ["cancel", "sell", "cancel", "stop"]


def test_restore_failure_is_critical():
    fb = FakeBroker(positions={"MSFT": 5})
    fb.add_stop("MSFT", 5, 90)
    fb.on_sell, fb.on_stop = "reject", "raise"
    alerts = []
    run(fb, sell("MSFT", 5), alerts=alerts)
    assert alerts[0][0] == "critical" and "UNPROTECTED" in alerts[0][1]


def test_failed_sell_without_previous_stop_is_critical():
    fb = FakeBroker(positions={"MSFT": 5})
    fb.on_sell = "reject"
    alerts = []
    run(fb, sell("MSFT", 5), alerts=alerts)
    assert "stop" not in fb.writes
    assert alerts[0][0] == "critical" and "no previous stop" in alerts[0][1]


def test_unconfirmed_stop_cancel_means_no_sell():
    fb = FakeBroker(positions={"MSFT": 5})
    fb.add_stop("MSFT", 5, 90)
    fb.on_cancel = "ignore"
    alerts = []
    report = run(fb, sell("MSFT", 5), alerts=alerts)
    assert fb.writes == ["cancel"]                       # never sells while the stop may still fire
    assert report.results[0].status == "failed" and alerts


def test_stop_filling_during_cancel_closes_the_position_without_selling():
    fb = FakeBroker(positions={"MSFT": 5})
    fb.add_stop("MSFT", 5, 90)
    fb.on_cancel = "fills"
    report = run(fb, sell("MSFT", 5))
    assert fb.writes == ["cancel"]
    assert report.results[0].detail.startswith("position_closed")


def test_sell_skipped_when_position_already_closed():
    fb = FakeBroker()
    report = run(fb, sell("MSFT", 5))
    assert fb.calls == [] and report.results[0].detail.startswith("position_closed")


# --- halt ----------------------------------------------------------------------------

def test_halt_writes_lock_before_canceling_and_selling(tmp_path):
    fb = FakeBroker(positions={"MSFT": 5, "AAPL": 2})
    fb.add_stop("MSFT", 5, 90)
    fb.add_stop("AAPL", 2, 150)
    pending_buy = fb.add(symbol="NVDA", side="buy", type="market", status="new", qty=3,
                         client_order_id="tb-2026-10-12-NVDA-buy")
    seen_lock = []
    original_cancel = fb.cancel_order

    def cancel_spy(order_id):
        seen_lock.append(read_halt(tmp_path) is not None)
        original_cancel(order_id)

    fb.cancel_order = cancel_spy
    alerts = []
    report = run(fb, sell("AAPL", 2), sell("MSFT", 5), breakers=HALT, state_dir=tmp_path, alerts=alerts)
    assert seen_lock and all(seen_lock)                   # the lock existed before the first cancel
    assert fb.records[pending_buy.id].status == "canceled"
    assert fb.positions == {}
    assert statuses(report)[0] == ("halt", "*", "written")
    assert any(lvl == "critical" and "HALT.lock" in msg for lvl, msg in alerts)


def test_halt_with_market_closed_writes_lock_and_warns_positions_not_sold(tmp_path):
    fb = FakeBroker(positions={"MSFT": 5}, is_open=False)
    alerts = []
    run(fb, sell("MSFT", 5), breakers=HALT, market_open=False, state_dir=tmp_path, alerts=alerts)
    assert read_halt(tmp_path) is not None and fb.calls == []
    assert any("NOT liquidated" in msg for _, msg in alerts)


def test_position_without_open_stop_is_alerted():
    fb = FakeBroker(positions={"JNJ": 4})
    alerts = []
    run(fb, alerts=alerts)
    assert alerts == [("warning", "JNJ: 4 shares held without an open stop order")]
