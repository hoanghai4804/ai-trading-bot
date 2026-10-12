from functools import partial

from fake_broker import NOW, FakeBroker

from tradingbot.config import load_config
from tradingbot.data.market_data import LatestTrade
from tradingbot.execution.executor import Executor
from tradingbot.execution.orders import is_bot_order, new_positions_this_week
from tradingbot.execution.test_order import check_order_id, run_test_order

CFG = load_config()


def prices(symbols):
    return {s: LatestTrade(571.23, NOW) for s in symbols}


def run(fb, dry_run=False):
    return run_test_order(CFG, fb, prices, now=NOW, dry_run=dry_run, alert=lambda *a: None,
                          executor_factory=partial(Executor, sleep=fb.sleep, monotonic=fb.monotonic))


def steps(report):
    return {name: ok for name, ok, _ in report.steps}


def test_full_cycle_buys_checks_stop_and_closes():
    fb = FakeBroker(prices={"SPY": 571.30})
    report = run(fb)
    assert report.ok, report.steps
    buy, cancel, sell = fb.calls
    assert buy[:4] == ("buy", "SPY", 1, 542.66)                 # 571.23 * 0.95 = 542.6685 -> rounded down
    assert buy[4] == "tbtest-20261012-150700-SPY-buy"
    assert cancel[0] == "cancel" and sell[:3] == ("sell", "SPY", 1)
    assert fb.positions == {} and fb.get_orders(status="open") == []
    sell_detail = next(d for name, _, d in report.steps if name.startswith("safe sell"))
    sell_order = next(o for o in fb.get_orders() if o.side == "sell" and o.type == "market")
    assert sell_detail.startswith(f"order {sell_order.id}, filled 1")


def test_test_orders_are_not_bot_orders():
    fb = FakeBroker()
    run(fb)
    assert not any(is_bot_order(o, "tb") for o in fb.get_orders())
    assert new_positions_this_week(fb.get_orders(), "tb", NOW.date()) == 0
    assert not check_order_id("tb", NOW, "SPY", "buy").startswith("tb-")


def test_dry_run_and_closed_market_send_nothing():
    fb = FakeBroker(is_open=False)
    dry = run(fb, dry_run=True)
    assert dry.ok and fb.calls == [] and "would buy 1 SPY" in dry.steps[-1][2]
    real = run(fb)
    assert not real.ok and steps(real) == {"market open": False} and fb.calls == []


def test_refuses_when_symbol_already_held_or_has_open_orders():
    fb = FakeBroker(positions={"SPY": 3})
    assert not run(fb).ok and fb.calls == []
    fb = FakeBroker()
    fb.add_stop("SPY", 3, 500)
    assert not run(fb).ok and fb.calls == []


def test_missing_stop_leg_fails_and_position_is_still_closed():
    fb = FakeBroker()
    fb.buy_stop_leg = False
    report = run(fb)
    assert not report.ok
    assert steps(report)["stop leg attached"] is False and steps(report)["stop open on Alpaca"] is False
    assert fb.positions == {}


def test_unfilled_buy_is_canceled():
    fb = FakeBroker()
    fb.on_buy = "pending"
    report = run(fb)
    assert not report.ok and steps(report)["buy filled"] is False
    assert fb.writes == ["buy", "cancel"]
