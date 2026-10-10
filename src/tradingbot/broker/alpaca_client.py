"""Thin wrapper around alpaca-py's TradingClient.

Converts Alpaca models into small plain dataclasses so the rest of the
code (and tests) never depend on alpaca-py types. Paper-only checks run
before and after the connection is made.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrderByIdRequest,
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
)

from tradingbot.credentials import AlpacaCredentials
from tradingbot.safety import (
    assert_paper_account,
    assert_paper_base_url,
    assert_paper_environment,
    mask_account_number,
)

# Alpaca daily portfolio-history timestamps are midnight in exchange time.
EXCHANGE_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class AccountSnapshot:
    account_number_masked: str
    status: str
    currency: str
    equity: float
    last_equity: float  # equity at the previous trading day's close
    cash: float
    trading_blocked: bool


@dataclass(frozen=True)
class ClockSnapshot:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float


@dataclass(frozen=True)
class EquityPoint:
    day: date
    equity: float


# Order statuses after which nothing more can happen to the order.
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced", "stopped", "suspended"})


@dataclass(frozen=True)
class OrderSnapshot:
    id: str
    client_order_id: str
    symbol: str
    side: str                  # "buy" | "sell"
    type: str                  # "market" | "stop" | ...
    order_class: str           # "simple" | "oto" | ...
    status: str
    qty: float
    filled_qty: float
    filled_avg_price: float | None
    stop_price: float | None
    submitted_at: datetime | None
    filled_at: datetime | None
    legs: tuple[OrderSnapshot, ...] = field(default_factory=tuple)

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL_STATUSES


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


def order_snapshot(o: Any) -> OrderSnapshot:
    return OrderSnapshot(
        id=str(o.id),
        client_order_id=str(o.client_order_id),
        symbol=str(o.symbol),
        side=_enum_value(o.side),
        type=_enum_value(o.type if getattr(o, "type", None) is not None else o.order_type),
        order_class=_enum_value(o.order_class or "simple"),
        status=_enum_value(o.status),
        qty=float(o.qty or 0),
        filled_qty=float(o.filled_qty or 0),
        filled_avg_price=_float_or_none(o.filled_avg_price),
        stop_price=_float_or_none(o.stop_price),
        submitted_at=o.submitted_at,
        filled_at=o.filled_at,
        legs=tuple(order_snapshot(leg) for leg in (o.legs or ())),
    )


class BrokerClient:
    """Paper-only broker access. Build it with `BrokerClient.connect(...)`."""

    def __init__(self, trading_client: Any) -> None:
        self._client = trading_client

    @classmethod
    def connect(
        cls,
        credentials: AlpacaCredentials,
        client_factory: Callable[..., Any] = TradingClient,
    ) -> BrokerClient:
        assert_paper_environment()
        # paper=True is hard-coded and url_override is never passed.
        client = client_factory(credentials.api_key, credentials.secret_key, paper=True)
        assert_paper_base_url(getattr(client, "_base_url", ""))
        broker = cls(client)
        broker.get_account()  # verifies the account number is a paper account
        return broker

    def get_account(self) -> AccountSnapshot:
        acc = self._client.get_account()
        assert_paper_account(acc.account_number)
        return AccountSnapshot(
            account_number_masked=mask_account_number(acc.account_number),
            status=_enum_value(acc.status),
            currency=str(acc.currency),
            equity=float(acc.equity),
            last_equity=float(acc.last_equity),
            cash=float(acc.cash),
            trading_blocked=bool(acc.trading_blocked),
        )

    def get_clock(self) -> ClockSnapshot:
        c = self._client.get_clock()
        return ClockSnapshot(
            timestamp=c.timestamp, is_open=bool(c.is_open), next_open=c.next_open, next_close=c.next_close
        )

    def get_positions(self) -> list[PositionSnapshot]:
        return [
            PositionSnapshot(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price),
                market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
                unrealized_plpc=float(p.unrealized_plpc),
            )
            for p in self._client.get_all_positions()
        ]

    def get_daily_equity_history(self, period: str = "1A") -> list[EquityPoint]:
        """Daily end-of-day equity, oldest first. Used for weekly-loss and drawdown breakers."""
        history = self._client.get_portfolio_history(GetPortfolioHistoryRequest(period=period, timeframe="1D"))
        points = []
        for ts, equity in zip(history.timestamp, history.equity, strict=True):
            # Days before the account was funded are reported as 0 (or None); they would
            # break weekly-loss and drawdown math, so they are dropped.
            if equity is None or equity <= 0:
                continue
            day = datetime.fromtimestamp(ts, tz=EXCHANGE_TZ).date()
            points.append(EquityPoint(day=day, equity=float(equity)))
        return points

    # --- orders --------------------------------------------------------------------
    # The only methods that change anything on the account. The executor never calls
    # them in dry-run mode.

    def get_orders(self, status: str = "all", after: datetime | None = None, nested: bool = True,
                   symbols: Sequence[str] | None = None, limit: int = 500) -> list[OrderSnapshot]:
        """Orders (newest first). nested=True rolls OTO/bracket legs under their parent;
        nested=False lists every leg as its own order (used for open orders)."""
        request = GetOrdersRequest(status=QueryOrderStatus(status), after=after, limit=limit, nested=nested,
                                   symbols=list(symbols) if symbols else None)
        return [order_snapshot(o) for o in self._client.get_orders(request)]

    def get_order(self, order_id: str) -> OrderSnapshot:
        return order_snapshot(self._client.get_order_by_id(order_id, GetOrderByIdRequest(nested=True)))

    def get_order_by_client_id(self, client_order_id: str) -> OrderSnapshot | None:
        from alpaca.common.exceptions import APIError

        try:
            return order_snapshot(self._client.get_order_by_client_id(client_order_id))
        except APIError as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise

    def submit_oto_buy(self, symbol: str, qty: int, stop_price: float, client_order_id: str) -> OrderSnapshot:
        """Market buy with an attached GTC stop-loss sell (OTO): the stop is created when the buy fills."""
        request = MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
            order_class=OrderClass.OTO, stop_loss=StopLossRequest(stop_price=stop_price),
            client_order_id=client_order_id,
        )
        return order_snapshot(self._client.submit_order(request))

    def submit_market_sell(self, symbol: str, qty: float, client_order_id: str) -> OrderSnapshot:
        request = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                     client_order_id=client_order_id)
        return order_snapshot(self._client.submit_order(request))

    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float, client_order_id: str) -> OrderSnapshot:
        request = StopOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                                   stop_price=stop_price, client_order_id=client_order_id)
        return order_snapshot(self._client.submit_order(request))

    def cancel_order(self, order_id: str) -> None:
        self._client.cancel_order_by_id(order_id)
