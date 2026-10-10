"""Discord notifier: length limit, never raises, URL never logged, retries. No network (fake post)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from tradingbot.config import load_config
from tradingbot.notify.discord import MAX_CONTENT, DiscordNotifier, truncate
from tradingbot.reporting.summary import discord_message

CFG = load_config()
URL = "https://discord.com/api/webhooks/123456789/SECRET-TOKEN-abc"


class FakePost:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)   # int status, dict(status, json), or Exception
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        out = self.outcomes.pop(0) if self.outcomes else 204
        if isinstance(out, Exception):
            raise out
        if isinstance(out, dict):
            return SimpleNamespace(status_code=out["status"], json=lambda: out.get("json", {}), headers={},
                                   text=json.dumps(out.get("json", {})))
        return SimpleNamespace(status_code=out, json=lambda: {}, headers={}, text="")


def make(tmp_path, *outcomes, url=URL, enabled=True):
    post = FakePost(*outcomes)
    sleeps = []
    cfg = CFG.notify.model_copy(update={"discord_enabled": enabled})
    n = DiscordNotifier(url, cfg, log_path=tmp_path / "logs" / "notify.log", post=post, sleep=sleeps.append)
    return n, post, sleeps


def log_text(tmp_path) -> str:
    path = tmp_path / "logs" / "notify.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_message_is_cut_below_2000_chars(tmp_path):
    n, post, _ = make(tmp_path, 204)
    assert n.send("x" * 5000)
    content = post.calls[0][1]["json"]["content"]
    assert len(content) < 2000 and len(content) == MAX_CONTENT and content.endswith("…")
    assert truncate("short") == "short"


def test_mentions_are_disabled(tmp_path):
    n, post, _ = make(tmp_path, 204)
    n.send("@everyone hello")
    assert post.calls[0][1]["json"]["allowed_mentions"] == {"parse": []}


@pytest.mark.parametrize("error", [requests.ConnectionError(f"Max retries exceeded with url: {URL}"),
                                   requests.Timeout(f"read timeout {URL}"), RuntimeError("boom " + URL)])
def test_network_errors_never_raise_and_url_is_never_logged(tmp_path, error, capsys):
    n, post, sleeps = make(tmp_path, error, error, error)
    assert n.send("hello") is False
    assert len(post.calls) == 1 + CFG.notify.max_retries and sleeps == [1.0, 2.0]
    text = log_text(tmp_path) + capsys.readouterr().err
    assert "SECRET-TOKEN" not in text and "api/webhooks" not in text and "<webhook>" in text


def test_http_error_body_with_url_is_redacted(tmp_path):
    n, _, _ = make(tmp_path, {"status": 400, "json": {"message": f"bad request for {URL}"}})
    assert n.send("hello") is False
    assert "SECRET-TOKEN" not in log_text(tmp_path)


def test_429_waits_retry_after_then_succeeds(tmp_path):
    n, post, sleeps = make(tmp_path, {"status": 429, "json": {"retry_after": 2.5}}, 204)
    assert n.send("hello") is True
    assert len(post.calls) == 2 and sleeps == [2.5]


def test_429_retry_after_is_capped(tmp_path):
    n, _, sleeps = make(tmp_path, {"status": 429, "json": {"retry_after": 9999}}, 204)
    assert n.send("hello") and sleeps == [30.0]


def test_5xx_is_retried_4xx_is_not(tmp_path):
    n, post, _ = make(tmp_path, 502, 204)
    assert n.send("a") and len(post.calls) == 2
    n, post, _ = make(tmp_path, 404, 204)
    assert n.send("a") is False and len(post.calls) == 1


def test_gives_up_after_max_retries(tmp_path):
    n, post, _ = make(tmp_path, 500, 500, 500, 500)
    assert n.send("a") is False and len(post.calls) == 1 + CFG.notify.max_retries


@pytest.mark.parametrize("url,enabled", [(None, True), ("", True), (URL, False)])
def test_disabled_or_missing_url_sends_nothing(tmp_path, url, enabled):
    n, post, _ = make(tmp_path, url=url, enabled=enabled)
    assert n.send("hello") is False and post.calls == []
    assert "skipped" in log_text(tmp_path)


def test_attachment_is_sent_as_multipart(tmp_path):
    report = tmp_path / "2026-10-12.md"
    report.write_text("# Báo cáo\n", encoding="utf-8")
    n, post, _ = make(tmp_path, 200)
    assert n.send("summary", report)
    kw = post.calls[0][1]
    name, data, ctype = kw["files"]["files[0]"]
    assert (name, data, ctype) == ("2026-10-12.md", report.read_bytes(), "text/markdown")
    assert json.loads(kw["data"]["payload_json"])["content"] == "summary"


def test_missing_attachment_still_sends_text(tmp_path):
    n, post, _ = make(tmp_path, 204)
    assert n.send("summary", tmp_path / "nope.md")
    assert "json" in post.calls[0][1]


def test_alert_prints_to_stderr_and_sends(tmp_path, capsys):
    n, post, _ = make(tmp_path, 204)
    n.tag = "[DRY-RUN] "
    n.alert("critical", "drawdown breaker tripped")
    assert "ALERT[critical] drawdown breaker tripped" in capsys.readouterr().err
    assert post.calls[0][1]["json"]["content"] == "🚨 CRITICAL [DRY-RUN] drawdown breaker tripped"


def test_alert_never_raises_even_if_logging_fails(tmp_path):
    n, _, _ = make(tmp_path, RuntimeError("down"), RuntimeError("down"), RuntimeError("down"))
    n.log_path = tmp_path / "a-file"
    n.log_path.write_text("x")
    n.log_path = n.log_path / "notify.log"      # parent is a file: every log write fails
    n.alert("warning", "x")                      # must not raise


def test_discord_message_from_a_huge_summary_is_sent_below_2000(tmp_path):
    summary = {"today": "2026-10-12", "mode": "EXECUTE", "status": "ok",
               "account": {"equity": 101234.5, "cash": 50000, "positions": 3},
               "benchmark": {"symbol": "SPY"},
               "performance": {"since_last_run": {"from": "2026-10-05", "return": 0.01, "benchmark_return": 0.02},
                               "since_start": None},
               "orders": [{"side": "buy", "symbol": f"S{i}", "qty": 10, "status": "filled"} for i in range(50)],
               "blocked": [{"symbol": f"B{i}", "code": "correlation"} for i in range(50)],
               "execution_quality": {"all_time": {"market": {"n": 4, "mean_bps": 3.2}}, "backtest_assumed_bps": 5},
               "shadow_check": {"match": False, "detail": "MISMATCH live only: buy AAPL"},
               "alerts": ["x" * 500] * 30}
    msg = discord_message(summary, "2026-10-12.md")
    assert "Từ lần chạy trước: +1.00% (SPY +2.00%)" in msg and "Shadow check" in msg
    n, post, _ = make(tmp_path, 204)
    assert n.send(msg)
    assert len(post.calls[0][1]["json"]["content"]) < 2000


def test_discord_message_for_aborted_run():
    msg = discord_message({"today": "2026-10-12", "mode": "EXECUTE", "status": "aborted",
                           "reason": "SafetyError: System is HALTED", "alerts": ["SafetyError: System is HALTED"]})
    assert "bị dừng" in msg and "HALTED" in msg
