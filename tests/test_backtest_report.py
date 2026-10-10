"""End-to-end report on the real-data fixture with a shortened schedule (no network)."""

from datetime import date
from pathlib import Path

import pandas as pd
from test_backtest_parity import BARS, CFG

from tradingbot.backtest.report import build, write_reports
from tradingbot.config import DEFAULT_CONFIG_PATH

SHORT = CFG.model_copy(update={"backtest": CFG.backtest.model_copy(update={
    "start": date(2025, 4, 1), "warmup_months": 5, "train_years": 1, "test_months": 1,
    "fast_grid": [10, 20], "slow_grid": [50]})})


def test_report_files_and_sections(tmp_path: Path):
    config_before = DEFAULT_CONFIG_PATH.read_bytes()
    bundle = build(SHORT, BARS, {"first_bar": "2025-03-20", "last_bar": "2026-04-27", "feed": "sip"}, seeds=2)
    written = write_reports(bundle, BARS, tmp_path)
    names = {p.name for p in written}
    assert names == {"comparison.csv", "yearly.csv", "walk_forward_params.csv", "breaker_events.csv",
                     "blocked_reasons.csv", "trades.csv", "trades_walk_forward.csv", "sensitivity.csv",
                     "equity.csv", "equity.svg", "report.md"}
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6.", "## 7.", "## 8.", "## 9.",
                    "Survivorship bias"):
        assert heading in md
    comp = pd.read_csv(tmp_path / "comparison.csv")
    assert {"MA20/50", "walk-forward", "SPY buy & hold", "SMA200 SPY", "random x2 (median)"} <= set(comp["name"])
    assert {"total_return", "annual_return", "sharpe", "max_drawdown", "exposure", "trades", "win_rate",
            "avg_holding_days", "costs"} <= set(comp.columns)
    sens = pd.read_csv(tmp_path / "sensitivity.csv")
    assert len(sens) == 6 and sens["is_config"].sum() == 1
    trades = pd.read_csv(tmp_path / "trades.csv")
    assert {"entry_date", "exit_date", "entry_price", "exit_price", "qty", "stop", "exit_reason", "pnl"} <= set(
        trades.columns)
    assert (tmp_path / "equity.svg").read_text(encoding="utf-8").startswith("<svg")
    assert DEFAULT_CONFIG_PATH.read_bytes() == config_before     # sensitivity never edits config


def test_equity_svg_is_well_formed_xml():
    import xml.etree.ElementTree as ET

    from tradingbot.backtest.report import equity_svg

    s = pd.Series([1.0, 1.1, 1.2], index=pd.to_datetime(["2024-12-30", "2025-06-30", "2026-01-05"]))
    ET.fromstring(equity_svg([("SPY buy & hold", s, "#eb6834"), ("A<B", s * 2, "#2a78d6")], "x & y"))
