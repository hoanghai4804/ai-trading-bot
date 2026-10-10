from datetime import date, datetime, timedelta, timezone

import pytest
from fake_broker import NOW, FakeBroker

from tradingbot.execution.orders import (
    client_order_id,
    last_exits,
    new_positions_this_week,
    open_stop_sells,
    stop_fills_since,
)

TODAY = date(2026, 10, 12)   # Monday; calendar week = Oct 12-18 (New York)
LAST_WEEK = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
# Sunday 23:30 New York is Monday 03:30 UTC: still the PREVIOUS calendar week in New York.
SUNDAY_NIGHT_NY = datetime(2026, 10, 12, 3, 30, tzinfo=timezone.utc)


def test_client_order_id_is_deterministic():
    assert client_order_id("tb", TODAY, "AAPL", "buy") == "tb-2026-10-12-AAPL-buy"
    assert client_order_id("tb", TODAY, "AAPL", "stop", suffix="150700") == "tb-2026-10-12-AAPL-stop-150700"


def _buy(fb, symbol, status, filled_qty=0.0, cid=None, submitted_at=NOW, side="buy"):
    fb.add(symbol=symbol, side=side, type="market", status=status, qty=10, filled_qty=filled_qty,
           client_order_id=cid or f"tb-2026-10-12-{symbol}-{side}", submitted_at=submitted_at)


@pytest.mark.parametrize("status,filled_qty,counted", [
    ("filled", 10, True),
    ("partially_filled", 4, True),
    ("new", 0, True),
    ("accepted", 0, True),
    ("pending_new", 0, True),
    ("held", 0, True),
    ("rejected", 0, False),
    ("canceled", 0, False),
    ("expired", 0, False),
    ("canceled", 3, True),     # canceled after a partial fill: shares were bought
])
def test_weekly_count_by_order_status(status, filled_qty, counted):
    fb = FakeBroker()
    _buy(fb, "AAPL", status, filled_qty)
    assert new_positions_this_week(fb.get_orders(), "tb", TODAY) == int(counted)


def test_weekly_count_ignores_other_weeks_sells_foreign_ids_and_counts_symbols_once():
    fb = FakeBroker()
    _buy(fb, "AAPL", "filled", 10)
    _buy(fb, "AAPL", "filled", 10, cid="tb-2026-10-12-AAPL-buy-2")   # same symbol twice -> 1
    _buy(fb, "MSFT", "filled", 10, submitted_at=LAST_WEEK)
    _buy(fb, "NVDA", "filled", 10, submitted_at=SUNDAY_NIGHT_NY)
    _buy(fb, "SPY", "filled", 1, cid="tbtest-20261012-150700-SPY-buy")  # manual test-order
    _buy(fb, "JPM", "filled", 10, cid="manual-1")                       # placed by hand
    _buy(fb, "XOM", "filled", 10, side="sell")
    assert new_positions_this_week(fb.get_orders(), "tb", TODAY) == 1
    assert new_positions_this_week(fb.get_orders(), "tb", TODAY + timedelta(days=6)) == 1  # Sunday
    assert new_positions_this_week(fb.get_orders(), "tb", TODAY + timedelta(days=7)) == 0  # next week


def test_last_exits_from_market_sells_and_oto_stop_legs():
    fb = FakeBroker()
    fb.add(symbol="AAPL", side="sell", type="market", status="filled", qty=5, filled_qty=5,
           client_order_id="tb-2026-10-05-AAPL-sell", filled_at=datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    parent = fb.add(symbol="MSFT", side="buy", type="market", status="filled", qty=3, filled_qty=3,
                    order_class="oto", client_order_id="tb-2026-09-28-MSFT-buy")
    fb.add(symbol="MSFT", side="sell", type="stop", status="filled", qty=3, filled_qty=3, stop_price=90,
           filled_avg_price=89.5, parent=parent.id, client_order_id="random-leg-id",
           filled_at=datetime(2026, 10, 8, 18, tzinfo=timezone.utc))
    fb.add(symbol="NVDA", side="sell", type="market", status="filled", qty=1, filled_qty=1,
           client_order_id="manual-x", filled_at=NOW)                       # not a bot order
    fb.add(symbol="JPM", side="sell", type="market", status="canceled", qty=1,
           client_order_id="tb-2026-10-05-JPM-sell")                         # never filled
    orders = fb.get_orders(nested=True)
    assert last_exits(orders, "tb") == {"AAPL": date(2026, 10, 5), "MSFT": date(2026, 10, 8)}
    [fill] = stop_fills_since(orders, "tb", since=date(2026, 10, 5))
    assert (fill.symbol, fill.qty, fill.price, fill.day) == ("MSFT", 3, 89.5, date(2026, 10, 8))
    assert stop_fills_since(orders, "tb", since=date(2026, 10, 9)) == []


def test_open_stop_sells_in_flat_and_nested_listings():
    fb = FakeBroker()
    parent = fb.add(symbol="AAPL", side="buy", type="market", status="filled", qty=3, order_class="oto")
    fb.add(symbol="AAPL", side="sell", type="stop", status="new", qty=3, stop_price=90, parent=parent.id)
    fb.add_stop("AAPL", 3, 80, status="canceled")
    fb.add_stop("MSFT", 2, 50)
    for nested in (True, False):
        stops = open_stop_sells(fb.get_orders(nested=nested), "AAPL")
        assert [s.stop_price for s in stops] == [90]
