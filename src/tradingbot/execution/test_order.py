"""Manual end-to-end check of the order path on the paper account. NEVER part of the weekly run.

Buys 1 share of `execution.test_order_symbol` with an attached stop (OTO), checks that the
buy filled and the stop exists on Alpaca, then closes the position with the same safe-sell
flow the weekly run uses (cancel stop -> market sell).

Its ids start with `<prefix>test-`, not `<prefix>-`, so these orders are never counted as
bot positions (weekly limit) or bot exits (no-reentry rule).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from tradingbot.config import Config
from tradingbot.execution.executor import Alert, Executor, stderr_alert
from tradingbot.execution.orders import is_stop_sell, open_stop_sells
from tradingbot.pipeline import WeeklyBroker


@dataclass
class OrderCheckReport:
    dry_run: bool
    steps: list[tuple[str, bool, str]] = field(default_factory=list)   # (step, ok, detail)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(ok for _, ok, _ in self.steps)

    def add(self, step: str, ok: bool, detail: str = "") -> bool:
        self.steps.append((step, ok, detail))
        return ok


def check_order_id(prefix: str, now: datetime, symbol: str, kind: str) -> str:
    return f"{prefix}test-{now:%Y%m%d-%H%M%S}-{symbol}-{kind}"


def run_test_order(
    cfg: Config,
    broker: WeeklyBroker,
    latest_prices: Callable[[Iterable[str]], Mapping[str, float]],
    now: datetime,
    dry_run: bool = True,
    alert: Alert = stderr_alert,
    executor_factory: Callable[..., Executor] = Executor,
) -> OrderCheckReport:
    ex = cfg.execution
    symbol = ex.test_order_symbol
    report = OrderCheckReport(dry_run=dry_run)

    clock = broker.get_clock()
    if not report.add("market open", bool(clock.is_open) or dry_run,
                      "open" if clock.is_open else f"closed (next open {clock.next_open.isoformat()})"):
        return report
    held = sum(p.qty for p in broker.get_positions() if p.symbol == symbol)
    if not report.add(f"no existing {symbol} position", held == 0, f"held {held:g}"):
        return report
    pending = [o for o in broker.get_orders(status="open", nested=False, symbols=[symbol]) if o.symbol == symbol]
    if not report.add(f"no open {symbol} orders", not pending, f"{len(pending)} open"):
        return report
    ref = latest_prices([symbol]).get(symbol)
    if not report.add("latest price", ref is not None and ref > 0, f"{ref}"):
        return report
    stop = float(Decimal(repr(ref * (1 - ex.test_order_stop_pct))).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    buy_id = check_order_id(ex.order_id_prefix, now, symbol, "buy")
    if dry_run:
        report.add("dry-run plan", True, f"would buy 1 {symbol} (OTO, stop {stop:.2f}, id {buy_id}), "
                                         "check fill + stop, then cancel stop and sell")
        return report

    try:
        order = broker.submit_oto_buy(symbol, 1, stop, buy_id)
    except Exception as exc:  # noqa: BLE001
        report.add("submit OTO buy", False, str(exc))
        return report
    report.add("submit OTO buy", True, f"order {order.id}, stop {stop:.2f}")
    executor = executor_factory(broker, ex, now.date(), dry_run=False, alert=alert)
    final = executor.wait_terminal(order.id, ex.sell_fill_timeout_sec)
    if not report.add("buy filled", final.status == "filled" and final.filled_qty == 1,
                      f"{final.status}, {final.filled_qty:g} @ {final.filled_avg_price}"):
        if final.is_open:
            broker.cancel_order(final.id)
            report.add("unfilled buy canceled", True, final.id)
        return report

    legs = [leg for leg in final.legs if is_stop_sell(leg)]
    report.add("stop leg attached", len(legs) == 1 and legs[0].stop_price == stop,
               ", ".join(f"{leg.type} {leg.status} @ {leg.stop_price}" for leg in legs) or "none")
    open_stops = open_stop_sells(broker.get_orders(status="open", nested=False, symbols=[symbol]), symbol)
    report.add("stop open on Alpaca", len(open_stops) == 1,
               ", ".join(f"{o.id} {o.status} qty {o.qty:g} @ {o.stop_price}" for o in open_stops) or "none")

    sell = executor.safe_sell(symbol, check_order_id(ex.order_id_prefix, now, symbol, "sell"))
    report.add("safe sell (cancel stop -> market sell)", sell.status == "filled",
               f"{sell.status} {sell.detail} {sell.qty} @ {sell.fill_price}".strip())
    left = sum(p.qty for p in broker.get_positions() if p.symbol == symbol)
    still_open = [o for o in broker.get_orders(status="open", nested=False, symbols=[symbol]) if o.symbol == symbol]
    report.add("position closed, no open orders", left == 0 and not still_open,
               f"qty {left:g}, open orders {len(still_open)}")
    for msg in executor.report.alerts:
        report.add("no alert", False, msg)
    return report
