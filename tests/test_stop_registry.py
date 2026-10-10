import json
from datetime import date

import pytest
from fake_broker import FakeBroker

from tradingbot.config import load_config
from tradingbot.execution.executor import Executor
from tradingbot.execution.stop_registry import StopEntry, load_stops, save_stops, stops_path
from tradingbot.risk.circuit_breakers import BreakerStatus
from tradingbot.risk.manager import ApprovedOrder, RiskDecision

CFG = load_config().execution
TODAY = date(2026, 10, 12)
OK = BreakerStatus(halt=False, block_new_entries=False, reasons=(), previous_week_return=0.0,
                   drawdown=0.0, peak_equity=100_000, current_equity=100_000)
MSFT = StopEntry(stop_price=90.37, entry_date=date(2026, 6, 1), buy_order_id="tb-2026-06-01-MSFT-buy")


def run(fb, state_dir, *orders, dry_run=False, market_open=True, alerts=None):
    sink = (lambda level, msg: alerts.append((level, msg))) if alerts is not None else (lambda *a: None)
    ex = Executor(fb, CFG, TODAY, dry_run=dry_run, alert=sink, state_dir=state_dir, sleep=fb.sleep,
                  monotonic=fb.monotonic)
    return ex.execute(RiskDecision(as_of=date(2026, 10, 9), breakers=OK, approved=list(orders)), market_open)


def test_save_and_load_roundtrip_and_corrupt_file_fails(tmp_path):
    save_stops({"MSFT": MSFT}, tmp_path)
    assert load_stops(tmp_path) == {"MSFT": MSFT}
    assert load_stops(tmp_path / "empty") == {}
    stops_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):   # never silently forget stops
        load_stops(tmp_path)


def test_buy_records_its_original_stop(tmp_path):
    fb = FakeBroker()
    run(fb, tmp_path, ApprovedOrder("AAPL", "buy", 10, "cross", ref_price=100.0, stop_price=95.37))
    assert load_stops(tmp_path) == {"AAPL": StopEntry(95.37, TODAY, "tb-2026-10-12-AAPL-buy")}


def test_missing_stop_is_replaced_at_the_original_price(tmp_path):
    save_stops({"MSFT": MSFT}, tmp_path)
    fb = FakeBroker(positions={"MSFT": 6})
    fb.add_stop("MSFT", 6, 90.37, status="expired")      # Alpaca canceled the GTC order after 90 days
    alerts = []
    report = run(fb, tmp_path, alerts=alerts)
    [call] = fb.calls
    assert call[:4] == ("stop", "MSFT", 6, 90.37) and call[4].startswith("tb-2026-10-12-MSFT-stop-")
    assert [(r.side, r.status, r.stop_price) for r in report.results] == [("stop", "restored", 90.37)]
    assert alerts and alerts[0][0] == "warning" and "re-placed at the original price" in alerts[0][1]
    assert load_stops(tmp_path) == {"MSFT": MSFT}          # registry unchanged: the stop is never moved


def test_existing_stop_at_another_price_is_reported_not_moved(tmp_path):
    save_stops({"MSFT": MSFT}, tmp_path)
    fb = FakeBroker(positions={"MSFT": 6})
    fb.add_stop("MSFT", 6, 95.00)
    alerts = []
    run(fb, tmp_path, alerts=alerts)
    assert fb.calls == []
    assert alerts == [("warning", "MSFT: open stop at [95.0] differs from the original stop 90.37; left unchanged")]


@pytest.mark.parametrize("dry_run,market_open,status", [(True, True, "dry_run"), (False, False, "skipped")])
def test_no_stop_order_in_dry_run_or_closed_market(tmp_path, dry_run, market_open, status):
    save_stops({"MSFT": MSFT}, tmp_path)
    before = stops_path(tmp_path).read_text(encoding="utf-8")
    fb = FakeBroker(positions={"MSFT": 6}, is_open=market_open)
    alerts = []
    report = run(fb, tmp_path, dry_run=dry_run, market_open=market_open, alerts=alerts)
    assert fb.calls == [] and [r.status for r in report.results] == [status]
    assert "NOT re-placed" in alerts[0][1]
    assert stops_path(tmp_path).read_text(encoding="utf-8") == before


def test_closed_positions_leave_the_registry_but_pending_buys_stay(tmp_path):
    save_stops({"MSFT": MSFT, "JNJ": MSFT, "AAPL": MSFT}, tmp_path)
    fb = FakeBroker(positions={"MSFT": 6})
    fb.add_stop("MSFT", 6, 90.37)
    fb.add(symbol="AAPL", side="buy", type="market", status="new", qty=3, client_order_id="tb-x-AAPL-buy")
    run(fb, tmp_path)                                     # JNJ was stopped out mid-week
    assert set(load_stops(tmp_path)) == {"MSFT", "AAPL"}


def test_restore_after_failed_sell_uses_the_original_stop(tmp_path):
    save_stops({"MSFT": MSFT}, tmp_path)
    fb = FakeBroker(positions={"MSFT": 6})
    fb.add_stop("MSFT", 6, 95.00)                         # live stop differs (moved by hand)
    fb.on_sell = "reject"
    run(fb, tmp_path, ApprovedOrder("MSFT", "sell", 6, "MA20 < MA50"))
    assert [c[3] for c in fb.calls if c[0] == "stop"] == [90.37]


def test_replace_failure_is_critical(tmp_path):
    save_stops({"MSFT": MSFT}, tmp_path)
    fb = FakeBroker(positions={"MSFT": 6})
    fb.on_stop = "raise"
    alerts = []
    run(fb, tmp_path, alerts=alerts)
    assert alerts[0][0] == "critical" and "UNPROTECTED" in alerts[0][1]


def test_dry_run_never_touches_the_registry_file(tmp_path):
    save_stops({"JNJ": MSFT}, tmp_path)                    # JNJ closed: a real run would drop it
    run(FakeBroker(), tmp_path, dry_run=True)
    assert load_stops(tmp_path) == {"JNJ": MSFT}
    other = tmp_path / "fresh"
    run(FakeBroker(), other, ApprovedOrder("AAPL", "buy", 1, "x", ref_price=100.0, stop_price=95.0), dry_run=True)
    assert not stops_path(other).exists()
