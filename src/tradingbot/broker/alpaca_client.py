"""Thin wrapper around alpaca-py's TradingClient.

Converts Alpaca models into small plain dataclasses so the rest of the
code (and tests) never depend on alpaca-py types. Paper-only checks run
before and after the connection is made.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetPortfolioHistoryRequest

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


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


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
