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
