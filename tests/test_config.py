from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tradingbot.cli import main
from tradingbot.config import DEFAULT_CONFIG_PATH, load_config


def _write(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_default_config_is_valid():
    cfg = load_config()
    assert cfg.mode == "paper"
    assert "SPY" in cfg.watchlist and cfg.benchmark_symbol == "SPY"
    assert cfg.risk.max_position_pct == 0.05
    assert cfg.risk.risk_per_trade_pct == 0.01
    assert cfg.risk.max_new_positions_per_week == 3
    assert [r.weekday for r in cfg.schedule.runs] == ["MON"]


def test_live_mode_is_rejected(tmp_path):
    path = _write(tmp_path, lambda r: r.update(mode="live"))
    with pytest.raises(ValidationError):
        load_config(path)


def test_unknown_key_is_rejected(tmp_path):
    path = _write(tmp_path, lambda r: r["risk"].update(max_positon_pct=0.05))  # typo on purpose
    with pytest.raises(ValidationError):
        load_config(path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["risk"].update(max_position_pct=1.5),
        lambda r: r["risk"].update(risk_per_trade_pct=0.10),  # above position cap
        lambda r: r["risk"]["circuit_breakers"].update(max_drawdown_pct=0),
        lambda r: r.update(watchlist=["AAPL", "aapl"]),  # duplicate after normalization
        lambda r: r["schedule"].update(runs=[{"weekday": "MON", "report": "full"}, {"weekday": "MON", "report": "brief"}]),
        lambda r: r["schedule"].update(runs=[{"weekday": "WED", "report": "brief"}]),  # no full run
        lambda r: r["schedule"].update(runs=[{"weekday": "SAT", "report": "full"}]),
        lambda r: r["backtest"].update(fast_grid=[100], slow_grid=[50]),
    ],
)
def test_invalid_values_are_rejected(tmp_path, mutate):
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, mutate))


def test_watchlist_is_normalized(tmp_path):
    cfg = load_config(_write(tmp_path, lambda r: r.update(watchlist=[" aapl", "msft"])))
    assert cfg.watchlist == ["AAPL", "MSFT"]


def test_config_is_immutable():
    cfg = load_config()
    with pytest.raises(ValidationError):
        cfg.mode = "live"


def test_cli_config_command(capsys):
    assert main(["config"]) == 0
    assert "mode=paper" in capsys.readouterr().out
