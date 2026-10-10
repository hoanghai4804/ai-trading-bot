"""Run outputs (summary.md / summary.json / decisions.json / portfolio.md) and CLI wiring (no network)."""

import json
from argparse import Namespace
from datetime import date

import pytest
from fake_broker import FakeBroker
from test_pipeline import CFG, TODAY, run

import tradingbot.cli as cli
import tradingbot.reporting.summary as summary_mod
from tradingbot.reporting.run_text import MODE_LINES
from tradingbot.reporting.summary import discord_message, write_abort_summary, write_run_outputs
from tradingbot.safety import SafetyError


def outputs(tmp_path, dry_run, fb=None):
    fb = fb or FakeBroker()
    result = run(fb, tmp_path / "state", dry_run=dry_run)
    out = write_run_outputs(result, CFG, runs_dir=tmp_path / "runs", state_dir=tmp_path / "state",
                            memory_dir=tmp_path / "memory")
    return result, out


@pytest.mark.parametrize("dry_run,mode", [(True, "DRY-RUN"), (False, "EXECUTE")])
def test_summary_files_first_line_is_mode(tmp_path, dry_run, mode):
    _, out = outputs(tmp_path, dry_run)
    assert out == tmp_path / "runs" / TODAY.isoformat()
    md = (out / "summary.md").read_text(encoding="utf-8").splitlines()
    assert md[0] == MODE_LINES[mode]
    assert len(md) <= 45
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    decisions = json.loads((out / "decisions.json").read_text(encoding="utf-8"))
    assert summary["mode"] == decisions["mode"] == mode
    assert summary["report_mode"] == "full" and summary["scheduled"] is True      # Monday = full
    assert summary["next_steps"]["sentiment_file"] == f"runs/{TODAY}/sentiment.json"
    assert summary["next_steps"]["max_web_searches"] == 4
    assert summary["shadow_check"]["match"] is True
    assert decisions["decision"]["approved"][0]["symbol"] == "AAPL"
    assert "order_refs" in decisions and "shadow_check" in decisions


def test_dry_run_writes_no_state_and_no_portfolio(tmp_path):
    outputs(tmp_path, dry_run=True)
    assert not (tmp_path / "memory" / "portfolio.md").exists()
    state = tmp_path / "state"
    assert not (state / "equity_history.csv").exists() and not (state / "execution_quality.csv").exists()


def test_execute_writes_portfolio_after_execution(tmp_path):
    _, out = outputs(tmp_path, dry_run=False)
    portfolio = (tmp_path / "memory" / "portfolio.md").read_text(encoding="utf-8")
    assert "| AAPL | 49 |" in portfolio                       # bought in this run
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["account"]["positions"] == 1 and summary["positions"][0]["stop"] == pytest.approx(97.92)


def test_blocked_orders_keep_their_reason(tmp_path):
    fb = FakeBroker(cash=100.0)                             # not enough cash for one share
    _, out = outputs(tmp_path, dry_run=True, fb=fb)
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert [(b["symbol"], b["code"]) for b in summary["blocked"]] == [("AAPL", "insufficient_cash")]
    assert "blocked  buy  AAPL   insufficient_cash" in (out / "summary.md").read_text(encoding="utf-8")


def test_long_summary_is_capped_and_keeps_mode_line(tmp_path, monkeypatch):
    result = run(FakeBroker(), tmp_path / "state", dry_run=True)
    result.warnings.extend(f"warning {i}" for i in range(80))
    out = write_run_outputs(result, CFG, runs_dir=tmp_path / "runs", state_dir=tmp_path / "state",
                            memory_dir=tmp_path / "memory")
    md = (out / "summary.md").read_text(encoding="utf-8").splitlines()
    assert md[0] == MODE_LINES["DRY-RUN"] and len(md) <= 45
    shown = [ln for ln in md if ln.startswith("WARNING warning ")]
    more = int(md[-1].removeprefix("WARNING ... ").split()[0])
    assert md[-1].endswith("more, see summary.json") and shown[0] == "WARNING warning 0"
    assert len(shown) + more == 80 and len(shown) >= 15
    assert any(ln.startswith("shadow check:") for ln in md) and any(ln.startswith("next:") for ln in md)
    assert any(ln.startswith("approved buy  AAPL") for ln in md)        # short body kept whole


def test_long_body_is_cut_in_the_middle_warnings_kept(tmp_path):
    result = run(FakeBroker(), tmp_path / "state", dry_run=True)
    result.signals.extend(result.signals * 0)
    from tradingbot.risk.manager import BlockedOrder
    result.decision.blocked.extend(BlockedOrder(f"S{i}", "buy", "correlation", "x") for i in range(60))
    result.warnings.append("stop missing on XOM")
    out = write_run_outputs(result, CFG, runs_dir=tmp_path / "runs", state_dir=tmp_path / "state",
                            memory_dir=tmp_path / "memory")
    md = (out / "summary.md").read_text(encoding="utf-8").splitlines()
    assert md[0] == MODE_LINES["DRY-RUN"] and len(md) == 45
    assert md[-1] == "WARNING stop missing on XOM" and any("line(s) cut" in ln for ln in md)


def test_unscheduled_day_is_a_brief_report(tmp_path):
    from tradingbot.scheduling import report_mode
    assert report_mode(CFG, date(2026, 10, 14)) == "brief" and report_mode(CFG, TODAY) == "full"


def test_abort_summary(tmp_path):
    out = write_abort_summary(TODAY, False, "SafetyError: System is HALTED", CFG, runs_dir=tmp_path)
    md = (out / "summary.md").read_text(encoding="utf-8").splitlines()
    assert md[0] == MODE_LINES["EXECUTE"] and "HALTED" in md[1]
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "aborted" and "bị dừng" in discord_message(summary)


# --- CLI wiring -------------------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.alerts, self.sent = [], []

    def alert(self, level, message):
        self.alerts.append((level, message))

    def send(self, content, attachment=None):
        self.sent.append((content, attachment))
        return True


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(cli, "_notifier", lambda cfg, args, tag="": rec)
    monkeypatch.setattr(summary_mod, "RUNS_DIR", tmp_path / "runs")
    return rec


def test_cli_run_halted_alerts_and_writes_abort_summary(cli_env, tmp_path, monkeypatch, capsys):
    def halted():
        raise SafetyError("System is HALTED (drawdown)")
    monkeypatch.setattr(cli, "assert_not_halted", halted)
    assert cli._cmd_run(Namespace(config=None, execute=True, no_notify=False)) == 2
    assert capsys.readouterr().out.splitlines()[0] == MODE_LINES["EXECUTE"]
    assert cli_env.alerts == [("critical", "run aborted: System is HALTED (drawdown)")]
    runs = list((tmp_path / "runs").glob("*/summary.json"))
    assert len(runs) == 1 and json.loads(runs[0].read_text(encoding="utf-8"))["status"] == "aborted"


def test_cli_run_unexpected_error_alerts_then_raises(cli_env, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "assert_not_halted", lambda: None)

    def broken(*a, **kw):
        raise RuntimeError("alpaca down")
    monkeypatch.setattr(cli, "_connect", broken)
    with pytest.raises(RuntimeError):
        cli._cmd_run(Namespace(config=None, execute=False, no_notify=False))
    assert cli_env.alerts == [("critical", "run failed: RuntimeError: alpaca down")]


def test_cli_notify_report_falls_back_to_summary_md(cli_env, tmp_path):
    result = run(FakeBroker(), tmp_path / "state", dry_run=True)
    out = write_run_outputs(result, CFG, runs_dir=tmp_path / "runs", state_dir=tmp_path / "state",
                            memory_dir=tmp_path / "memory")
    code = cli._cmd_notify_report(Namespace(config=None, path=str(tmp_path / "missing.md"), date=None))
    assert code == 0
    [(content, attachment)] = cli_env.sent
    assert attachment == out / "summary.md" and content.startswith(f"**Trading bot {TODAY}** - DRY-RUN")


def test_cli_notify_report_attaches_the_report(cli_env, tmp_path):
    result = run(FakeBroker(), tmp_path / "state", dry_run=True)
    write_run_outputs(result, CFG, runs_dir=tmp_path / "runs", state_dir=tmp_path / "state",
                      memory_dir=tmp_path / "memory")
    report = tmp_path / f"{TODAY}.md"
    report.write_text("# Báo cáo\n", encoding="utf-8")
    assert cli._cmd_notify_report(Namespace(config=None, path=str(report), date=TODAY.isoformat())) == 0
    assert cli_env.sent[0][1] == report and f"{TODAY}.md" in cli_env.sent[0][0]
