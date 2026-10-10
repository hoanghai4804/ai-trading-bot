"""should-run: RUN / SKIP / HALT, mode from TRADINGBOT_MODE, exact command (no network)."""

import json
from argparse import Namespace
from datetime import date

import pytest
from test_summary import Recorder

import tradingbot.cli as cli
import tradingbot.reporting.summary as summary_mod
from tradingbot.config import load_config
from tradingbot.scheduling import EXIT_HALT, EXIT_RUN, EXIT_SKIP, decide_run, mode_from_env

CFG = load_config()
MONDAY, WEDNESDAY = date(2026, 10, 12), date(2026, 10, 14)
DONE = {"mode": "EXECUTE", "status": "ok"}


@pytest.mark.parametrize("value", ["execute", "EXECUTE", " Execute "])
def test_only_execute_trades(value):
    assert mode_from_env(value) == (False, ())


@pytest.mark.parametrize("value", [None, "", "  ", "live", "exec", "execute!", "true", "1", "paper"])
def test_anything_else_is_dry_run_with_a_warning(value):
    dry_run, warnings = mode_from_env(value)
    assert dry_run and len(warnings) == 1 and "DRY-RUN" in warnings[0]


def test_unknown_mode_value_is_shown_truncated():
    _, (warning,) = mode_from_env("x" * 50)
    assert "x" * 20 in warning and "x" * 21 not in warning


def test_scheduled_monday_runs_full_with_the_mode_command():
    d = decide_run(CFG, MONDAY, "execute", None, None)
    assert (d.action, d.mode, d.report, d.exit_code) == ("RUN", "EXECUTE", "full", EXIT_RUN)
    assert d.command == "python -m tradingbot run --execute"
    assert d.lines()[0] == "SHOULD-RUN: RUN mode=EXECUTE report=full date=2026-10-12"
    dry = decide_run(CFG, MONDAY, None, None, None)
    assert dry.command == "python -m tradingbot run" and any("WARNING:" in ln for ln in dry.lines())


def test_unscheduled_day_skips():
    d = decide_run(CFG, WEDNESDAY, "execute", None, None)
    assert (d.action, d.exit_code, d.command) == ("SKIP", EXIT_SKIP, None)


@pytest.mark.parametrize("report", ["brief", "full"])
def test_force_runs_an_unscheduled_day_in_dry_run_only(report):
    d = decide_run(CFG, WEDNESDAY, None, None, None, force=report)
    assert (d.action, d.mode, d.report) == ("RUN", "DRY-RUN", report)
    assert d.command == "python -m tradingbot run"
    refused = decide_run(CFG, WEDNESDAY, "execute", None, None, force=report)
    assert refused.action == "SKIP" and "refused" in refused.reason


def test_force_on_a_scheduled_day_keeps_the_scheduled_report():
    assert decide_run(CFG, MONDAY, None, None, None, force="brief").report == "full"


def test_force_value_is_checked():
    with pytest.raises(ValueError):
        decide_run(CFG, WEDNESDAY, None, None, None, force="yes")


def test_halt_wins_over_everything():
    for mode in (None, "execute"):
        for force in (False, True):
            d = decide_run(CFG, MONDAY, mode, {"reason": "drawdown"}, None, force="full" if force else None)
            assert (d.action, d.exit_code, d.command, d.report) == ("HALT", EXIT_HALT, None, "brief")
            assert "drawdown" in d.reason


def test_completed_execute_run_is_not_repeated():
    for mode in (None, "execute"):
        assert decide_run(CFG, MONDAY, mode, None, DONE).action == "SKIP"


@pytest.mark.parametrize("existing", [{"mode": "DRY-RUN", "status": "ok"}, {"mode": "EXECUTE", "status": "aborted"}])
def test_dry_run_or_aborted_summary_does_not_block_a_rerun(existing):
    assert decide_run(CFG, MONDAY, "execute", None, existing).action == "RUN"


# --- CLI -------------------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(cli, "_notifier", lambda cfg, args, tag="": rec)
    monkeypatch.setattr(summary_mod, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(cli, "read_halt", lambda: None)
    monkeypatch.delenv("TRADINGBOT_MODE", raising=False)
    return rec


def ns(today, force=None):
    return Namespace(config=None, today=today.isoformat(), force=force)


def test_cli_should_run_prints_decision_and_exit_code(env, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGBOT_MODE", "execute")
    assert cli._cmd_should_run(ns(MONDAY)) == EXIT_RUN
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "SHOULD-RUN: RUN mode=EXECUTE report=full date=2026-10-12"
    assert "command: python -m tradingbot run --execute" in out
    assert cli._cmd_should_run(ns(WEDNESDAY)) == EXIT_SKIP
    assert env.alerts == []


def test_cli_should_run_skips_after_a_completed_execute_run(env, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGBOT_MODE", "execute")
    day = tmp_path / "runs" / MONDAY.isoformat()
    day.mkdir(parents=True)
    (day / "summary.json").write_text(json.dumps(DONE), encoding="utf-8")
    assert cli._cmd_should_run(ns(MONDAY)) == EXIT_SKIP


def test_cli_should_run_halt_alerts_and_writes_aborted_summary(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_halt", lambda: {"reason": "drawdown 10.4%"})
    assert cli._cmd_should_run(ns(MONDAY)) == EXIT_HALT
    assert capsys.readouterr().out.startswith("SHOULD-RUN: HALT mode=DRY-RUN")
    [(level, message)] = env.alerts
    assert level == "critical" and "drawdown 10.4%" in message
    summary = json.loads((tmp_path / "runs" / MONDAY.isoformat() / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "aborted" and summary["mode"] == "DRY-RUN" and "HALTED" in summary["reason"]


def test_cli_should_run_skip_writes_nothing(env, tmp_path):
    cli._cmd_should_run(ns(WEDNESDAY))
    assert not (tmp_path / "runs").exists()


def test_cli_force_flag_parsing(env, capsys):
    assert cli.main(["should-run", "--today", WEDNESDAY.isoformat(), "--force"]) == EXIT_RUN
    assert "report=brief" in capsys.readouterr().out
    assert cli.main(["should-run", "--today", WEDNESDAY.isoformat(), "--force", "full"]) == EXIT_RUN
    assert "report=full" in capsys.readouterr().out


def test_cli_notify_alert(env):
    assert cli.main(["notify", "alert", "--level", "critical", "--message", "git push failed"]) == 0
    assert env.alerts == [("critical", "git push failed")]
