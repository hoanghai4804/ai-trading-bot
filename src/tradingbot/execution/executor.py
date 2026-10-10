"""Turns an approved RiskDecision into paper orders (or, in dry-run, into a plan only).

Order of work:
 1. Drawdown halt: write state/HALT.lock FIRST, then cancel open non-stop orders, then sell.
 2. Market closed: nothing is sent (the plan is still reported).
 3. Sells, each with the safe flow: cancel the stop -> market sell -> if the sell fails,
    is rejected or does not fill in time, put the old GTC stop back for the remaining
    shares and alert. If re-placing the stop fails too, a critical alert is raised.
 4. Buys: market + GTC stop attached (OTO), so the stop exists as soon as the buy fills.
 5. Stop reconciliation (state/stops.json holds each position's ORIGINAL stop): a held position
    without an open stop gets exactly its original stop back (+ alert); a position with no
    recorded stop is a critical alert. Stops are never moved. Closed positions leave the file.

Idempotency: client_order_id is deterministic (`<prefix>-<date>-<SYMBOL>-<buy|sell>`), and
before sending anything the executor checks for an existing order with that id, an open
order for the symbol, or (buys) an existing position; any of these -> skipped.

Dry-run reads the broker (positions, orders) but never calls a method that changes it.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from tradingbot.broker.alpaca_client import OrderSnapshot, PositionSnapshot
from tradingbot.config import ExecutionConfig
from tradingbot.execution.orders import client_order_id, is_stop_sell, open_stop_sells
from tradingbot.execution.stop_registry import StopEntry, load_stops, save_stops
from tradingbot.risk.circuit_breakers import trip_halt
from tradingbot.risk.manager import ApprovedOrder, RiskDecision
from tradingbot.safety import STATE_DIR

log = logging.getLogger(__name__)

Level = Literal["warning", "critical"]
Alert = Callable[[Level, str], None]


def stderr_alert(level: Level, message: str) -> None:
    """Default alert sink until Discord is wired in (phase 8)."""
    print(f"ALERT[{level}] {message}", file=sys.stderr)
    log.warning("alert %s: %s", level, message)


class Broker(Protocol):
    def get_positions(self) -> list[PositionSnapshot]: ...
    def get_orders(self, status: str = "all", after: datetime | None = None, nested: bool = True,
                   symbols: Sequence[str] | None = None, limit: int = 500) -> list[OrderSnapshot]: ...
    def get_order(self, order_id: str) -> OrderSnapshot: ...
    def get_order_by_client_id(self, client_order_id: str) -> OrderSnapshot | None: ...
    def submit_oto_buy(self, symbol: str, qty: int, stop_price: float, client_order_id: str) -> OrderSnapshot: ...
    def submit_market_sell(self, symbol: str, qty: float, client_order_id: str) -> OrderSnapshot: ...
    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float,
                         client_order_id: str) -> OrderSnapshot: ...
    def cancel_order(self, order_id: str) -> None: ...


@dataclass(frozen=True)
class OrderResult:
    symbol: str
    side: str      # buy | sell | stop | cancel | halt
    status: str    # dry_run | filled | submitted | skipped | failed | restored | canceled | written
    detail: str = ""
    client_order_id: str | None = None
    order_id: str | None = None
    qty: float | None = None
    fill_price: float | None = None
    stop_price: float | None = None


@dataclass
class ExecutionReport:
    dry_run: bool
    market_open: bool
    results: list[OrderResult] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    @property
    def orders_sent(self) -> int:
        return sum(1 for r in self.results if r.status in ("filled", "submitted", "restored")
                   or (r.status == "failed" and r.order_id))

    def to_dict(self) -> dict[str, Any]:
        return {"dry_run": self.dry_run, "market_open": self.market_open, "orders_sent": self.orders_sent,
                "results": [asdict(r) for r in self.results], "alerts": list(self.alerts)}


class Executor:
    def __init__(
        self,
        broker: Broker,
        cfg: ExecutionConfig,
        today: date,
        dry_run: bool = True,
        alert: Alert = stderr_alert,
        state_dir: Path = STATE_DIR,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.broker = broker
        self.cfg = cfg
        self.today = today
        self.dry_run = dry_run
        self._alert_sink = alert
        self.state_dir = state_dir
        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now
        self.report = ExecutionReport(dry_run=dry_run, market_open=False)
        self.stops: dict[str, StopEntry] = {}

    # --- public --------------------------------------------------------------------

    def execute(self, decision: RiskDecision, market_open: bool) -> ExecutionReport:
        self.report = ExecutionReport(dry_run=self.dry_run, market_open=market_open)
        self.stops = load_stops(self.state_dir)
        sells = [o for o in decision.approved if o.side == "sell"]
        buys = [o for o in decision.approved if o.side == "buy"]
        if decision.halt:
            self._write_halt(decision)
        if not market_open:
            if decision.halt and sells and not self.dry_run:
                self._alert("critical", "market closed: positions were NOT liquidated; HALT.lock blocks later "
                                        "runs, so liquidate manually (stops still protect the positions)")
            for o in sells + buys:
                self._add(OrderResult(o.symbol, o.side, "skipped", "market closed: no order sent",
                                      self._cid(o.symbol, o.side), qty=o.qty, stop_price=o.stop_price))
            self._reconcile_stops(can_send=False)
            return self.report
        if decision.halt:
            self._cancel_open_non_stop_orders()
        for o in sells:
            self.safe_sell(o.symbol, self._cid(o.symbol, "sell"))
        for o in buys:
            self._buy(o)
        self._reconcile_stops(can_send=True)
        return self.report

    def safe_sell(self, symbol: str, cid: str) -> OrderResult:
        """Cancel the stop(s), sell the whole position at market, restore the stop on failure."""
        existing = self.broker.get_order_by_client_id(cid)
        if existing is not None:
            return self._add(OrderResult(symbol, "sell", "skipped", f"already_submitted ({existing.status})",
                                         cid, existing.id))
        qty = self._position_qty(symbol)
        if qty <= 0:
            return self._add(OrderResult(symbol, "sell", "skipped", "position_closed: nothing to sell", cid))
        open_orders = self._open_orders(symbol)
        if any(o.side == "sell" and not is_stop_sell(o) for o in open_orders):
            return self._add(OrderResult(symbol, "sell", "skipped", "already_submitted (open sell order)", cid))
        stops = [o for o in open_orders if is_stop_sell(o)]
        entry = self.stops.get(symbol)
        old_stop = entry.stop_price if entry else max((o.stop_price for o in stops if o.stop_price), default=None)
        if self.dry_run:
            return self._add(OrderResult(symbol, "sell", "dry_run",
                                         f"would cancel {len(stops)} stop(s), then market sell", cid, qty=qty,
                                         stop_price=old_stop))

        for s in stops:
            try:
                self.broker.cancel_order(s.id)
            except Exception as exc:  # noqa: BLE001 - broker errors are reported, never crash the run
                log.warning("cancel %s failed: %s", s.id, exc)
        states = [self.wait_terminal(s.id, self.cfg.cancel_timeout_sec) for s in stops]
        if any(st.is_open for st in states):
            self._alert("warning", f"{symbol}: stop cancel not confirmed in {self.cfg.cancel_timeout_sec}s; "
                                   "sell skipped, the stop still protects the position")
            return self._add(OrderResult(symbol, "sell", "failed", "stop cancel not confirmed", cid, qty=qty,
                                         stop_price=old_stop))
        qty = self._position_qty(symbol)
        if qty <= 0:  # a stop filled while we were canceling it: the position is already closed
            return self._add(OrderResult(symbol, "sell", "skipped", "position_closed: stop filled before the sell",
                                         cid))

        try:
            sell = self.broker.submit_market_sell(symbol, qty, cid)
        except Exception as exc:  # noqa: BLE001
            return self._sell_failed(symbol, cid, None, qty, old_stop, f"submit error: {exc}")
        final = self.wait_terminal(sell.id, self.cfg.sell_fill_timeout_sec)
        if final.status == "filled":
            return self._add(OrderResult(symbol, "sell", "filled", "", cid, final.id, final.filled_qty,
                                         final.filled_avg_price))
        if final.is_open:
            try:
                self.broker.cancel_order(final.id)
                final = self.wait_terminal(final.id, self.cfg.cancel_timeout_sec)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel of unfilled sell %s failed: %s", final.id, exc)
            if final.is_open:
                self._alert("critical", f"{symbol}: market sell {final.id} still open after timeout and could "
                                        "not be canceled; stop NOT restored (would oversell). Check manually.")
                return self._add(OrderResult(symbol, "sell", "failed", "sell open, cancel not confirmed", cid,
                                             final.id, qty, stop_price=old_stop))
        return self._sell_failed(symbol, cid, final.id, qty, old_stop,
                                 f"sell {final.status} (filled {final.filled_qty:g}/{qty:g})")

    # --- internals -----------------------------------------------------------------

    def _sell_failed(self, symbol: str, cid: str, order_id: str | None, qty: float, old_stop: float | None,
                     why: str) -> OrderResult:
        result = self._add(OrderResult(symbol, "sell", "failed", why, cid, order_id, qty, stop_price=old_stop))
        remaining = self._position_qty(symbol)
        if remaining <= 0:
            self._alert("warning", f"{symbol}: {why}, but the position is closed")
            return result
        if old_stop is None:
            self._alert("critical", f"{symbol}: {why}; no previous stop to restore, {remaining:g} shares "
                                    "are UNPROTECTED")
            return result
        # Time suffix: a stop restored by an earlier run the same day must not block this one.
        stop_cid = client_order_id(self.cfg.order_id_prefix, self.today, symbol, "stop",
                                   suffix=self._now().strftime("%H%M%S"))
        try:
            restored = self.broker.submit_stop_sell(symbol, remaining, old_stop, stop_cid)
        except Exception as exc:  # noqa: BLE001
            self._alert("critical", f"{symbol}: {why}; restoring the stop at {old_stop:.2f} FAILED ({exc}); "
                                    f"{remaining:g} shares are UNPROTECTED")
            return result
        self._add(OrderResult(symbol, "stop", "restored", f"after failed sell: {why}", stop_cid, restored.id,
                              remaining, stop_price=old_stop))
        self._alert("warning", f"{symbol}: {why}; stop restored at {old_stop:.2f} for {remaining:g} shares")
        return result

    def _buy(self, o: ApprovedOrder) -> OrderResult:
        cid = self._cid(o.symbol, "buy")
        assert o.stop_price is not None, "approved buys always carry a stop"
        if self._position_qty(o.symbol) > 0:
            return self._add(OrderResult(o.symbol, "buy", "skipped", "already_held", cid))
        existing = self.broker.get_order_by_client_id(cid)
        if existing is not None:
            return self._add(OrderResult(o.symbol, "buy", "skipped", f"already_submitted ({existing.status})",
                                         cid, existing.id))
        if any(x.side == "buy" for x in self._open_orders(o.symbol)):
            return self._add(OrderResult(o.symbol, "buy", "skipped", "already_submitted (open buy order)", cid))
        qty = int(o.qty)
        if self.dry_run:
            return self._add(OrderResult(o.symbol, "buy", "dry_run", f"would send OTO market buy, ref {o.ref_price}",
                                         cid, qty=qty, stop_price=o.stop_price))
        try:
            order = self.broker.submit_oto_buy(o.symbol, qty, o.stop_price, cid)
        except Exception as exc:  # noqa: BLE001
            self._alert("warning", f"{o.symbol}: buy not sent ({exc})")
            return self._add(OrderResult(o.symbol, "buy", "failed", f"submit error: {exc}", cid, qty=qty,
                                         stop_price=o.stop_price))
        self.stops[o.symbol] = StopEntry(o.stop_price, self.today, cid)
        final = self.wait_terminal(order.id, self.cfg.buy_fill_timeout_sec)
        if final.status == "filled":
            if not any(is_stop_sell(leg) for leg in final.legs):
                self._alert("critical", f"{o.symbol}: buy filled but no stop leg found; position UNPROTECTED")
            return self._add(OrderResult(o.symbol, "buy", "filled", "", cid, final.id, final.filled_qty,
                                         final.filled_avg_price, o.stop_price))
        if final.is_open:  # stays working; the attached stop is created when it fills
            return self._add(OrderResult(o.symbol, "buy", "submitted", f"not filled yet ({final.status})", cid,
                                         final.id, qty, stop_price=o.stop_price))
        self._alert("warning", f"{o.symbol}: buy {final.status}")
        return self._add(OrderResult(o.symbol, "buy", "failed", f"buy {final.status}", cid, final.id, qty,
                                     stop_price=o.stop_price))

    def _write_halt(self, decision: RiskDecision) -> None:
        reason = "; ".join(decision.breakers.reasons)
        if self.dry_run:
            self._add(OrderResult("*", "halt", "dry_run", f"would write HALT.lock: {reason}"))
            return
        path = trip_halt(decision.breakers, self.state_dir)
        self._add(OrderResult("*", "halt", "written", f"{path.name}: {reason}"))
        self._alert("critical", f"drawdown breaker tripped, HALT.lock written, liquidating: {reason}")

    def _cancel_open_non_stop_orders(self) -> None:
        """On halt: pending buys etc. go away; stops are canceled by the safe sell of each position."""
        for o in self.broker.get_orders(status="open", nested=False):
            if is_stop_sell(o) or not o.is_open:
                continue
            if self.dry_run:
                self._add(OrderResult(o.symbol, "cancel", "dry_run", f"would cancel open {o.side} {o.type}",
                                      o.client_order_id, o.id))
                continue
            try:
                self.broker.cancel_order(o.id)
                self._add(OrderResult(o.symbol, "cancel", "canceled", f"open {o.side} {o.type}",
                                      o.client_order_id, o.id))
            except Exception as exc:  # noqa: BLE001
                self._alert("warning", f"{o.symbol}: cancel of open order {o.id} failed ({exc})")

    def _reconcile_stops(self, can_send: bool) -> None:
        """Every held position keeps an open stop at its ORIGINAL price; the registry follows positions."""
        open_orders = self.broker.get_orders(status="open", nested=False)
        held = {p.symbol: p.qty for p in self.broker.get_positions() if p.qty > 0}
        for symbol, qty in sorted(held.items()):
            entry = self.stops.get(symbol)
            live = open_stop_sells(open_orders, symbol)
            if live:
                moved = [s.stop_price for s in live if entry and s.stop_price != entry.stop_price]
                if moved:
                    self._alert("warning", f"{symbol}: open stop at {moved} differs from the original stop "
                                           f"{entry.stop_price:.2f}; left unchanged")
                continue
            if entry is None:
                self._alert("critical", f"{symbol}: {qty:g} shares held without an open stop and no original "
                                        "stop recorded; UNPROTECTED")
                continue
            self._replace_stop(symbol, qty, entry, can_send)
        pending_buys = {o.symbol for o in open_orders if o.side == "buy"}
        for symbol in sorted(set(self.stops) - set(held) - pending_buys):
            del self.stops[symbol]   # position closed (sold or stopped out)
        if not self.dry_run:
            save_stops(self.stops, self.state_dir)

    def _replace_stop(self, symbol: str, qty: float, entry: StopEntry, can_send: bool) -> None:
        why = f"no open stop; original stop {entry.stop_price:.2f} (set {entry.entry_date})"
        if self.dry_run or not can_send:
            status = "dry_run" if self.dry_run else "skipped"
            self._add(OrderResult(symbol, "stop", status, f"{why}: would re-place it", qty=qty,
                                  stop_price=entry.stop_price))
            self._alert("warning", f"{symbol}: {why}; NOT re-placed in this run "
                                   f"({'dry-run' if self.dry_run else 'market closed'})")
            return
        cid = client_order_id(self.cfg.order_id_prefix, self.today, symbol, "stop",
                              suffix=self._now().strftime("%H%M%S"))
        try:
            order = self.broker.submit_stop_sell(symbol, qty, entry.stop_price, cid)
        except Exception as exc:  # noqa: BLE001
            self._alert("critical", f"{symbol}: {why}; re-placing it FAILED ({exc}); {qty:g} shares UNPROTECTED")
            return
        self._add(OrderResult(symbol, "stop", "restored", why, cid, order.id, qty, stop_price=entry.stop_price))
        self._alert("warning", f"{symbol}: {why}; re-placed at the original price for {qty:g} shares")

    def wait_terminal(self, order_id: str, timeout: float) -> OrderSnapshot:
        deadline = self._monotonic() + timeout
        order = self.broker.get_order(order_id)
        while order.is_open and self._monotonic() < deadline:
            self._sleep(self.cfg.poll_interval_sec)
            order = self.broker.get_order(order_id)
        return order

    def _open_orders(self, symbol: str) -> list[OrderSnapshot]:
        return [o for o in self.broker.get_orders(status="open", nested=False, symbols=[symbol])
                if o.symbol == symbol and o.is_open]

    def _position_qty(self, symbol: str) -> float:
        return sum(p.qty for p in self.broker.get_positions() if p.symbol == symbol)

    def _cid(self, symbol: str, kind: Literal["buy", "sell"]) -> str:
        return client_order_id(self.cfg.order_id_prefix, self.today, symbol, kind)

    def _add(self, result: OrderResult) -> OrderResult:
        self.report.results.append(result)
        return result

    def _alert(self, level: Level, message: str) -> None:
        self.report.alerts.append(f"[{level}] {message}")
        try:
            self._alert_sink(level, message)
        except Exception as exc:  # noqa: BLE001 - alerting must never break execution
            log.warning("alert sink failed: %s", exc)
