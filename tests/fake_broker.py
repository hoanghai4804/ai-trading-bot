"""In-memory stand-in for BrokerClient (no network). Behaviour switches simulate Alpaca outcomes."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace

from tradingbot.broker.alpaca_client import TERMINAL_STATUSES, EquityPoint, OrderSnapshot, PositionSnapshot

NOW = datetime(2026, 10, 12, 15, 7, tzinfo=timezone.utc)  # Monday run, 11:07 New York


@dataclass
class Rec:
    id: str
    client_order_id: str
    symbol: str
    side: str
    type: str
    status: str
    qty: float
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    stop_price: float | None = None
    order_class: str = "simple"
    submitted_at: datetime | None = NOW
    filled_at: datetime | None = None
    parent: str | None = None


class FakeBroker:
    def __init__(self, positions=None, equity=100_000.0, cash=100_000.0, is_open=True, history=None,
                 prices=None):
        self.positions: dict[str, float] = dict(positions or {})
        self.equity, self.cash, self.is_open = equity, cash, is_open
        self.history = history or []          # list[EquityPoint]
        self.prices = prices or {}            # fill prices
        self.records: dict[str, Rec] = {}
        self.calls: list[tuple] = []          # every method that changes the account
        self.on_buy = "fill"                  # fill | pending | reject | raise
        self.buy_stop_leg = True
        self.on_sell = "fill"                 # fill | pending | reject | raise
        self.on_cancel = "ok"                 # ok | ignore | raise | fills (stop triggers while canceling)
        self.on_stop = "ok"                   # ok | raise
        self.trading_blocked = False
        self.time = 0.0
        self._ids = itertools.count(1)

    # --- clock for the executor's polling --------------------------------------------
    def sleep(self, seconds):
        self.time += seconds

    def monotonic(self):
        return self.time

    # --- helpers for tests ---------------------------------------------------------------
    def add(self, **kw) -> Rec:
        kw.setdefault("id", f"o{next(self._ids)}")
        kw.setdefault("client_order_id", f"manual-{kw['id']}")
        rec = Rec(**kw)
        self.records[rec.id] = rec
        return rec

    def add_stop(self, symbol, qty, stop, status="new") -> Rec:
        return self.add(symbol=symbol, side="sell", type="stop", status=status, qty=qty, stop_price=stop)

    def snap(self, rec: Rec, nested=True) -> OrderSnapshot:
        legs = tuple(self.snap(r) for r in self.records.values() if r.parent == rec.id) if nested else ()
        return OrderSnapshot(rec.id, rec.client_order_id, rec.symbol, rec.side, rec.type, rec.order_class,
                             rec.status, rec.qty, rec.filled_qty, rec.filled_avg_price, rec.stop_price,
                             rec.submitted_at, rec.filled_at, legs)

    def _cid_taken(self, cid):
        if any(r.client_order_id == cid for r in self.records.values()):
            raise RuntimeError(f"client_order_id must be unique: {cid}")

    def _fill(self, rec: Rec, sign: int):
        rec.status, rec.filled_qty = "filled", rec.qty
        rec.filled_avg_price = self.prices.get(rec.symbol, 100.0)
        rec.filled_at = NOW
        self.positions[rec.symbol] = self.positions.get(rec.symbol, 0) + sign * rec.qty
        if self.positions[rec.symbol] == 0:
            del self.positions[rec.symbol]

    # --- read ----------------------------------------------------------------------------
    def get_account(self):
        return SimpleNamespace(equity=self.equity, cash=self.cash, trading_blocked=self.trading_blocked)

    def get_clock(self):
        return SimpleNamespace(is_open=self.is_open, next_open=NOW, timestamp=NOW)

    def get_positions(self):
        return [PositionSnapshot(s, q, 100.0, 100.0, 100.0 * q, 0.0, 0.0) for s, q in self.positions.items()]

    def get_daily_equity_history(self, period="1A"):
        return list(self.history)

    def get_orders(self, status="all", after=None, nested=True, symbols=None, limit=500):
        recs = [r for r in self.records.values() if not (nested and r.parent)]
        if status == "open":
            recs = [r for r in recs if r.status not in TERMINAL_STATUSES]
        if after is not None:
            recs = [r for r in recs if r.submitted_at is None or r.submitted_at > after]
        if symbols:
            recs = [r for r in recs if r.symbol in symbols]
        return [self.snap(r, nested) for r in recs]

    def get_order(self, order_id):
        return self.snap(self.records[order_id])

    def get_order_by_client_id(self, cid):
        return next((self.snap(r) for r in self.records.values() if r.client_order_id == cid), None)

    # --- write ---------------------------------------------------------------------------
    def submit_oto_buy(self, symbol, qty, stop_price, client_order_id):
        self.calls.append(("buy", symbol, qty, stop_price, client_order_id))
        if self.on_buy == "raise":
            raise RuntimeError("insufficient buying power")
        self._cid_taken(client_order_id)
        rec = self.add(symbol=symbol, side="buy", type="market", status="new", qty=qty, order_class="oto",
                       client_order_id=client_order_id)
        if self.on_buy == "reject":
            rec.status = "rejected"
            return self.snap(rec)
        if self.buy_stop_leg:
            self.add(symbol=symbol, side="sell", type="stop", status="held", qty=qty, stop_price=stop_price,
                     order_class="oto", parent=rec.id, client_order_id=f"leg-{rec.id}")
        if self.on_buy == "fill":
            self._fill(rec, +1)
            for leg in self.records.values():
                if leg.parent == rec.id:
                    leg.status = "new"
        return self.snap(rec)

    def submit_market_sell(self, symbol, qty, client_order_id):
        self.calls.append(("sell", symbol, qty, client_order_id))
        if self.on_sell == "raise":
            raise RuntimeError("api error")
        self._cid_taken(client_order_id)
        rec = self.add(symbol=symbol, side="sell", type="market", status="new", qty=qty,
                       client_order_id=client_order_id)
        stops_open = any(r.symbol == symbol and r.type == "stop" and r.status not in TERMINAL_STATUSES
                         for r in self.records.values())
        if self.on_sell == "reject" or stops_open:   # Alpaca: qty held by the stop is not available
            rec.status = "rejected"
        elif self.on_sell == "fill":
            self._fill(rec, -1)
        return self.snap(rec)

    def submit_stop_sell(self, symbol, qty, stop_price, client_order_id):
        self.calls.append(("stop", symbol, qty, stop_price, client_order_id))
        if self.on_stop == "raise":
            raise RuntimeError("api error")
        self._cid_taken(client_order_id)
        return self.snap(self.add(symbol=symbol, side="sell", type="stop", status="new", qty=qty,
                                  stop_price=stop_price, client_order_id=client_order_id))

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        rec = self.records[order_id]
        if self.on_cancel == "raise":
            raise RuntimeError("api error")
        if self.on_cancel == "ok":
            rec.status = "canceled"
        elif self.on_cancel == "fills":
            self._fill(rec, -1)

    @property
    def writes(self):
        return [c[0] for c in self.calls]


def equity_points(values: dict[str, float]) -> list[EquityPoint]:
    return [EquityPoint(date.fromisoformat(d), v) for d, v in values.items()]
