import json
from datetime import datetime, timedelta, timezone

import pytest

from tradingbot.safety import (
    RunLock,
    SafetyError,
    assert_not_halted,
    assert_paper_account,
    assert_paper_base_url,
    assert_paper_environment,
    is_live_url,
    mask_account_number,
    read_halt,
    write_halt,
)


@pytest.mark.parametrize(
    "url, live",
    [
        ("https://api.alpaca.markets", True),
        ("https://API.alpaca.markets/v2", True),
        ("https://paper-api.alpaca.markets", False),
        ("https://data.alpaca.markets", False),
    ],
)
def test_is_live_url(url, live):
    assert is_live_url(url) is live


@pytest.mark.parametrize(
    "env",
    [
        {"APCA_API_BASE_URL": "https://api.alpaca.markets"},
        {"ALPACA_BASE_URL": "https://api.alpaca.markets/v2"},
        {"ALPACA_PAPER": "false"},
        {"APCA_PAPER": "0"},
    ],
)
def test_live_environment_is_blocked(env):
    with pytest.raises(SafetyError):
        assert_paper_environment(env)


def test_paper_environment_passes():
    assert_paper_environment({"APCA_API_BASE_URL": "https://paper-api.alpaca.markets", "ALPACA_PAPER": "true"})
    assert_paper_environment({})


class _FakeBaseURL:
    """Mimics alpaca-py's BaseURL enum: str() is the member name, .value is the URL."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return "BaseURL.SOMETHING"


def test_base_url_check_handles_enum_and_string():
    assert_paper_base_url("https://paper-api.alpaca.markets")
    assert_paper_base_url(_FakeBaseURL("https://paper-api.alpaca.markets"))
    with pytest.raises(SafetyError):
        assert_paper_base_url(_FakeBaseURL("https://api.alpaca.markets"))
    with pytest.raises(SafetyError):
        assert_paper_base_url("")


def test_account_number_must_be_paper():
    assert_paper_account("PA1234ABCD")
    for live_like in ("1234ABCD", "XA1234", ""):
        with pytest.raises(SafetyError):
            assert_paper_account(live_like)


def test_mask_account_number_hides_middle():
    masked = mask_account_number("PA3XYZ12VKZ")
    assert masked.startswith("PA") and masked.endswith("2VKZ") and "3XYZ1" not in masked


def test_halt_lock_blocks_until_deleted(tmp_path):
    assert read_halt(tmp_path) is None
    assert_not_halted(tmp_path)
    path = write_halt("drawdown 10.4% >= 10%", {"peak": 100000, "equity": 89600}, tmp_path)
    assert read_halt(tmp_path)["reason"] == "drawdown 10.4% >= 10%"
    with pytest.raises(SafetyError, match="HALTED"):
        assert_not_halted(tmp_path)
    path.unlink()  # the human action that resumes the system
    assert_not_halted(tmp_path)


def test_corrupt_halt_lock_still_halts(tmp_path):
    (tmp_path / "HALT.lock").write_text("not json", encoding="utf-8")
    with pytest.raises(SafetyError):
        assert_not_halted(tmp_path)


def test_run_lock_prevents_concurrent_runs_and_is_released(tmp_path):
    lock = RunLock(ttl=timedelta(minutes=30), state_dir=tmp_path)
    with lock:
        assert lock.path.exists()
        with pytest.raises(SafetyError, match="in progress"):
            with RunLock(ttl=timedelta(minutes=30), state_dir=tmp_path):
                pass
    assert not lock.path.exists()


def test_stale_run_lock_is_taken_over(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    (tmp_path / "run.lock").write_text(json.dumps({"pid": 1, "started_at": old.isoformat()}), encoding="utf-8")
    with RunLock(ttl=timedelta(minutes=30), state_dir=tmp_path):
        pass


def test_env_check_prints_presence_never_values(monkeypatch, capsys):
    import tradingbot.credentials as credentials
    from tradingbot import cli

    monkeypatch.setattr(credentials, "load_env_file", lambda: None)
    monkeypatch.setenv("ALPACA_API_KEY", "SECRET-VALUE-1")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SECRET-VALUE-2")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    assert cli.main(["env-check"]) == 1
    out = capsys.readouterr().out
    assert out.splitlines() == ["ALPACA_API_KEY: set", "ALPACA_SECRET_KEY: set", "DISCORD_WEBHOOK_URL: MISSING"]
    assert "SECRET-VALUE" not in out
