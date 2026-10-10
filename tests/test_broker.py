from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tradingbot.broker.alpaca_client import BrokerClient
from tradingbot.credentials import AlpacaCredentials, MissingCredentialsError, get_alpaca_credentials
from tradingbot.safety import SafetyError

PAPER_URL = "https://paper-api.alpaca.markets"
CREDS = AlpacaCredentials(api_key="key-should-not-leak", secret_key="secret-should-not-leak")


class FakeTradingClient:
    def __init__(self, api_key, secret_key, paper, base_url=PAPER_URL, account_number="PA00001234"):
        assert paper is True
        self._base_url = base_url
        self.account_number = account_number

    def get_account(self):
        return SimpleNamespace(
            account_number=self.account_number, status="ACTIVE", currency="USD",
            equity="100000", last_equity="99000", cash="50000", trading_blocked=False,
        )

    def get_clock(self):
        now = datetime(2026, 10, 12, 15, 7, tzinfo=timezone.utc)
        return SimpleNamespace(timestamp=now, is_open=True, next_open=now, next_close=now)

    def get_all_positions(self):
        return [SimpleNamespace(symbol="AAPL", qty="10", avg_entry_price="200", current_price="210",
                                market_value="2100", unrealized_pl="100", unrealized_plpc="0.05")]

    def get_portfolio_history(self, request):
        # 2026-10-06 (unfunded), 2026-10-08, 2026-10-09 (None) at midnight New York time
        return SimpleNamespace(timestamp=[1791259200, 1791432000, 1791518400],
                               equity=[0.0, 100000.0, None])


def factory(**overrides):
    return lambda *a, **kw: FakeTradingClient(*a, **kw, **overrides)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_PAPER", "APCA_PAPER"):
        monkeypatch.delenv(name, raising=False)


def test_connect_and_snapshots():
    broker = BrokerClient.connect(CREDS, client_factory=factory())
    acc = broker.get_account()
    assert acc.equity == 100000.0 and acc.cash == 50000.0
    assert acc.account_number_masked.endswith("1234") and "0000" not in acc.account_number_masked
    assert broker.get_clock().is_open
    assert broker.get_positions()[0].qty == 10.0


def test_equity_history_drops_unfunded_days_and_uses_new_york_dates():
    history = BrokerClient.connect(CREDS, client_factory=factory()).get_daily_equity_history()
    assert [(p.day.isoformat(), p.equity) for p in history] == [("2026-10-08", 100000.0)]


def test_connect_refuses_live_base_url():
    with pytest.raises(SafetyError):
        BrokerClient.connect(CREDS, client_factory=factory(base_url="https://api.alpaca.markets"))


def test_connect_refuses_non_paper_account():
    with pytest.raises(SafetyError):
        BrokerClient.connect(CREDS, client_factory=factory(account_number="123456789"))


def test_connect_refuses_live_env(monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(SafetyError):
        BrokerClient.connect(CREDS, client_factory=factory())


def test_credentials_repr_hides_secrets():
    assert "leak" not in repr(CREDS) and "leak" not in str(CREDS)


def test_missing_credentials(monkeypatch):
    monkeypatch.setattr("tradingbot.credentials.load_env_file", lambda: None)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(MissingCredentialsError, match="ALPACA_API_KEY"):
        get_alpaca_credentials()


def test_cli_check_aborts_with_exit_code_2_on_safety_error(monkeypatch, capsys):
    from tradingbot import cli

    def refuse(_creds):
        raise SafetyError("Account number does not look like a paper account; aborting")

    monkeypatch.setattr(cli, "get_alpaca_credentials", lambda: CREDS)
    monkeypatch.setattr(cli.BrokerClient, "connect", staticmethod(refuse))
    assert cli.main(["check"]) == 2
    err = capsys.readouterr().err
    assert "ABORT" in err and "leak" not in err


# --- orders ----------------------------------------------------------------------------

def _alpaca_order(**kw):
    from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, OrderType

    base = dict(id="uuid-1", client_order_id="tb-2026-10-12-AAPL-buy", symbol="AAPL", side=OrderSide.BUY,
                type=OrderType.MARKET, order_type=OrderType.MARKET, order_class=OrderClass.OTO,
                status=OrderStatus.FILLED, qty="10", filled_qty="10", filled_avg_price="201.5", stop_price=None,
                submitted_at=datetime(2026, 10, 12, 15, 7, tzinfo=timezone.utc), filled_at=None, legs=None)
    base.update(kw)
    return SimpleNamespace(**base)


class OrderClient(FakeTradingClient):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.submitted = []

    def submit_order(self, request):
        self.submitted.append(request)
        return _alpaca_order()

    def get_order_by_client_id(self, cid):
        from alpaca.common.exceptions import APIError

        raise APIError('{"code": 40410000, "message": "order not found"}', SimpleNamespace(response=SimpleNamespace(status_code=404)))

    def get_orders(self, request):
        from alpaca.trading.enums import OrderSide, OrderStatus, OrderType

        leg = _alpaca_order(id="uuid-2", client_order_id="leg", side=OrderSide.SELL, type=OrderType.STOP,
                            status=OrderStatus.NEW, filled_qty="0", filled_avg_price=None, stop_price="190.12")
        return [_alpaca_order(legs=[leg])]


def test_order_snapshot_converts_enums_numbers_and_legs():
    broker = BrokerClient.connect(CREDS, client_factory=lambda *a, **kw: OrderClient(*a, **kw))
    [o] = broker.get_orders(status="all")
    assert (o.side, o.type, o.order_class, o.status, o.qty, o.filled_avg_price) == (
        "buy", "market", "oto", "filled", 10.0, 201.5)
    [leg] = o.legs
    assert (leg.side, leg.type, leg.status, leg.stop_price, leg.is_open) == ("sell", "stop", "new", 190.12, True)
    assert not o.is_open


def test_oto_buy_request_is_market_gtc_with_stop_loss():
    client_holder = []

    def make(*a, **kw):
        client_holder.append(OrderClient(*a, **kw))
        return client_holder[0]

    broker = BrokerClient.connect(CREDS, client_factory=make)
    broker.submit_oto_buy("AAPL", 10, 190.12, "tb-2026-10-12-AAPL-buy")
    broker.submit_market_sell("AAPL", 10, "tb-2026-10-12-AAPL-sell")
    broker.submit_stop_sell("AAPL", 10, 190.12, "tb-2026-10-12-AAPL-stop-1")
    buy, sell, stop = (r.to_request_fields() for r in client_holder[0].submitted)
    assert {k: str(getattr(v, "value", v)) for k, v in buy.items() if k != "stop_loss"} == {
        "symbol": "AAPL", "qty": "10.0", "side": "buy", "type": "market", "time_in_force": "gtc",
        "order_class": "oto", "client_order_id": "tb-2026-10-12-AAPL-buy"}
    assert buy["stop_loss"] == {"stop_price": 190.12}
    assert (sell["type"].value, sell["time_in_force"].value, sell["side"].value) == ("market", "day", "sell")
    assert (stop["type"].value, stop["time_in_force"].value, stop["stop_price"]) == ("stop", "gtc", 190.12)


def test_unknown_client_order_id_returns_none():
    broker = BrokerClient.connect(CREDS, client_factory=lambda *a, **kw: OrderClient(*a, **kw))
    assert broker.get_order_by_client_id("tb-2026-10-12-AAPL-buy") is None
