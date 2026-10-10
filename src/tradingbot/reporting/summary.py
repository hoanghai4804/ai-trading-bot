"""Run outputs: runs/<date>/summary.md (~40 lines, the ONLY file Claude reads from the
computation), summary.json (compact, also feeds the Discord message), decisions.json
(everything: signals, approved/blocked with reasons, execution, shadow check, fills).

The first line of summary.md is always the MODE line (reporting/run_text.py).
EXECUTE runs also rewrite memory/portfolio.md (positions, original stops, equity).
A run that aborts before deciding (HALT.lock, paper guard, error) still writes a short
summary with status "aborted", so the weekly prompt always has something to report.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from tradingbot.config import PROJECT_ROOT, Config
from tradingbot.execution.stop_registry import load_stops
from tradingbot.monitoring.execution_quality import load_quality, slippage_stats
from tradingbot.pipeline import RunResult
from tradingbot.reporting.run_text import format_run, mode_line
from tradingbot.safety import STATE_DIR
from tradingbot.scheduling import report_mode, scheduled_report

RUNS_DIR = PROJECT_ROOT / "runs"
MEMORY_DIR = PROJECT_ROOT / "memory"
MAX_WEB_SEARCHES = 4


def run_dir(today: date, runs_dir: Path | None = None) -> Path:
    return (runs_dir or RUNS_DIR) / today.isoformat()   # resolved at call time (tests patch RUNS_DIR)


def _pct(v: float | None) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.2%}"


def _bps(v: float) -> str:
    return "n/a" if math.isnan(v) else f"{v:+.1f} bps"


def _perf_line(perf: Mapping[str, Any], bench: str) -> str:
    parts = []
    for key, label in (("since_last_run", "since last run"), ("since_start", "since start")):
        p = perf.get(key)
        parts.append(f"{label} n/a (first logged run)" if not p else
                     f"{label} ({p['from']}) {_pct(p['return'])} vs {bench} {_pct(p['benchmark_return'])}")
    return "performance: " + " | ".join(parts)


def _clean(obj: Any) -> Any:
    """JSON-safe: NaN -> None, dates -> ISO strings."""
    if isinstance(obj, float):
        return None if math.isnan(obj) else obj
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def quality_section(result: RunResult, cfg: Config, state_dir: Path) -> dict[str, Any]:
    rows = load_quality(state_dir)
    known = {r["order_id"] for r in rows}
    pending = [asdict(f) for f in result.fills if f.order_id not in known]   # dry-run: not written
    return {"new_fills": len(result.fills), "this_run": slippage_stats(asdict(f) for f in result.fills),
            "all_time": slippage_stats([*rows, *pending]), "backtest_assumed_bps": cfg.costs.slippage_bps}


def build_summary(result: RunResult, cfg: Config, state_dir: Path = STATE_DIR) -> dict[str, Any]:
    stops = load_stops(state_dir)
    rmode = report_mode(cfg, result.today)
    alerts = [*result.warnings, *result.execution.alerts]
    return _clean({
        "mode": result.mode,
        "status": "ok",
        "report_mode": rmode,
        "scheduled": scheduled_report(cfg, result.today) is not None,
        "today": result.today, "as_of": result.as_of, "market_open": result.market_open,
        "signal_window_sessions": result.signal_window,
        "account": {"equity": result.equity, "cash_before": result.cash,
                    "cash": result.cash_after if result.cash_after is not None else result.cash,
                    "positions": len(result.positions_after),
                    "new_positions_this_week": result.new_positions_this_week},
        "benchmark": {"symbol": result.benchmark_symbol, "price": result.benchmark_price},
        "performance": result.performance,
        "breakers": result.decision.breakers.to_dict(),
        "positions": [{"symbol": p.symbol, "qty": p.qty, "avg_entry": p.avg_entry_price,
                       "price": p.current_price, "unrealized_pl": p.unrealized_pl,
                       "unrealized_plpc": p.unrealized_plpc,
                       "stop": stops[p.symbol].stop_price if p.symbol in stops else None}
                      for p in sorted(result.positions_after, key=lambda p: p.symbol)],   # after execution
        "signals": [{"action": s.action, "symbol": s.symbol} for s in result.signals],
        "approved": [{"side": o.side, "symbol": o.symbol, "qty": o.qty, "ref_price": o.ref_price,
                      "stop_price": o.stop_price, "reason": o.reason} for o in result.decision.approved],
        "blocked": [{"side": b.side, "symbol": b.symbol, "code": b.code, "detail": b.detail}
                    for b in result.decision.blocked],
        "orders": [{"side": r.side, "symbol": r.symbol, "status": r.status, "qty": r.qty,
                    "fill_price": r.fill_price, "stop_price": r.stop_price} for r in result.execution.results],
        "orders_sent": result.execution.orders_sent,
        "stop_fills": [{"symbol": f.symbol, "qty": f.qty, "price": f.price, "day": f.day}
                       for f in result.stop_fills],
        "execution_quality": quality_section(result, cfg, state_dir),
        "shadow_check": {"match": result.shadow.match if result.shadow else None,
                         "detail": result.shadow.describe() if result.shadow else "not run"},
        "alerts": alerts,
        "next_steps": next_steps(cfg, result.today, rmode),
    })


def next_steps(cfg: Config, today: date, rmode: str) -> dict[str, Any]:
    steps: dict[str, Any] = {"report_path": f"reports/weekly/{today.isoformat()}.md",
                             "template": f"prompts/templates/report_{rmode}.md"}
    if rmode == "full":
        steps.update({"sentiment_file": f"runs/{today.isoformat()}/sentiment.json",
                      "sentiment_symbols": list(cfg.watchlist), "max_web_searches": MAX_WEB_SEARCHES})
    return steps


def summary_lines(result: RunResult, summary: Mapping[str, Any], max_lines: int = 45) -> list[str]:
    run_lines = format_run(result)          # first line = MODE
    lines = [run_lines[0], f"report: {summary['report_mode']} (scheduled={summary['scheduled']})"]
    lines.append(_perf_line(summary["performance"], result.benchmark_symbol))
    for p in summary["positions"]:
        stop = f"{p['stop']:.2f}" if p["stop"] is not None else "NONE"
        lines.append(f"position {p['symbol']:6} qty={p['qty']:g} avg={p['avg_entry']:.2f} "
                     f"pl={p['unrealized_pl']:+,.2f} ({_pct(p['unrealized_plpc'])}) stop={stop}")
    lines.extend(ln for ln in run_lines[1:] if not ln.startswith("WARNING "))
    warnings = [ln for ln in run_lines[1:] if ln.startswith("WARNING ")]
    q = summary["execution_quality"]
    steps = summary["next_steps"]
    tail = [f"execution quality: {q['new_fills']} new fill(s); market fills all-time "
            f"{_bps(q['all_time']['market']['mean_bps'] if q['all_time']['market']['n'] else float('nan'))} "
            f"over {q['all_time']['market']['n']} (backtest assumes {q['backtest_assumed_bps']:g} bps); "
            f"stop fills {q['all_time']['stop']['n']}",
            f"shadow check: {summary['shadow_check']['detail']}",
            f"next: write {steps['report_path']} from {steps['template']}"
            + (f"; sentiment -> {steps['sentiment_file']} then `record-sentiment`" if "sentiment_file" in steps
               else "")]
    # Over the cap: warnings keep at least half the budget, the middle of the body is cut first;
    # the MODE line and the tail are always kept. Everything is in decisions.json / summary.json.
    budget = max_lines - 1 - len(tail)
    if len(lines) - 1 + len(warnings) > budget:
        keep_w = min(len(warnings), max(budget // 2, budget - (len(lines) - 1)))
        if keep_w < len(warnings):
            warnings = warnings[:keep_w - 1] + [f"WARNING ... {len(warnings) - keep_w + 1} more, see summary.json"]
        keep_b = budget - len(warnings)
        if len(lines) - 1 > keep_b:
            cut = len(lines) - 1 - (keep_b - 1)
            lines = lines[:1 + keep_b - 1] + [f"... {cut} line(s) cut, see decisions.json"]
    return [*lines, *tail, *warnings]


def write_run_outputs(result: RunResult, cfg: Config, runs_dir: Path | None = None, state_dir: Path = STATE_DIR,
                      memory_dir: Path = MEMORY_DIR) -> Path:
    out = run_dir(result.today, runs_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = build_summary(result, cfg, state_dir)
    _write(out / "summary.md", "\n".join(summary_lines(result, summary)) + "\n")
    _write(out / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    _write(out / "decisions.json", json.dumps(_clean(result.to_dict()), indent=2, ensure_ascii=False,
                                              default=str) + "\n")
    if not result.dry_run:
        write_portfolio(result, summary, memory_dir)
    return out


def write_abort_summary(today: date, dry_run: bool, reason: str, cfg: Config | None = None,
                        runs_dir: Path | None = None) -> Path:
    out = run_dir(today, runs_dir)
    out.mkdir(parents=True, exist_ok=True)
    rmode = report_mode(cfg, today) if cfg else "brief"
    summary = {"mode": "DRY-RUN" if dry_run else "EXECUTE", "status": "aborted", "reason": reason,
               "report_mode": rmode, "today": today.isoformat(), "alerts": [reason],
               "next_steps": {"report_path": f"reports/weekly/{today.isoformat()}.md",
                              "template": "prompts/templates/report_brief.md"}}
    _write(out / "summary.md", f"{mode_line(dry_run)}\nRUN ABORTED: {reason}\nNo decision was made, no order sent.\n")
    _write(out / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return out


def write_portfolio(result: RunResult, summary: Mapping[str, Any], memory_dir: Path = MEMORY_DIR) -> Path:
    lines = ["# Portfolio", "",
             f"Generated by the run of {result.today} (as_of {result.as_of}, {result.mode}). Do not edit by hand.",
             "", f"- Equity (start of run): {result.equity:,.2f} | cash after orders: "
             f"{summary['account']['cash']:,.2f} | positions: {len(summary['positions'])}",
             "- " + _perf_line(summary["performance"], result.benchmark_symbol), ""]
    if summary["positions"]:
        lines += ["| Symbol | Qty | Avg entry | Price | Unrealized P/L | Original stop |", "|---|---|---|---|---|---|"]
        for p in summary["positions"]:
            stop = f"{p['stop']:.2f}" if p["stop"] is not None else "NONE"
            lines.append(f"| {p['symbol']} | {p['qty']:g} | {p['avg_entry']:.2f} | {p['price']:.2f} | "
                         f"{p['unrealized_pl']:+,.2f} ({_pct(p['unrealized_plpc'])}) | {stop} |")
    else:
        lines.append("No open position.")
    path = memory_dir / "portfolio.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, "\n".join(lines) + "\n")
    return path


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def discord_message(summary: Mapping[str, Any], report_name: str | None = None) -> str:
    """Short Vietnamese message (< 2000 chars after truncation) built from summary.json."""
    lines = [f"**Trading bot {summary.get('today')}** - {summary.get('mode')}"]
    if summary.get("status") == "aborted":
        lines.append(f"🚨 Lần chạy bị dừng: {summary.get('reason')}")
    else:
        acct = summary.get("account", {})
        bench = summary.get("benchmark", {}).get("symbol", "SPY")
        lines.append(f"Danh mục: {acct.get('equity', 0):,.2f} $ | tiền mặt {acct.get('cash', 0):,.2f} $ | "
                     f"{acct.get('positions', 0)} vị thế")
        perf = summary.get("performance") or {}
        for key, label in (("since_last_run", "Từ lần chạy trước"), ("since_start", "Từ đầu")):
            p = perf.get(key)
            if p:
                lines.append(f"{label}: {_pct(p.get('return'))} (SPY {_pct(p.get('benchmark_return'))})"
                             .replace("SPY", bench))
        orders = [o for o in summary.get("orders", []) if o.get("side") in ("buy", "sell")]
        lines.append("Lệnh: " + (", ".join(f"{o['side']} {o['symbol']} {o.get('qty') or ''} ({o['status']})"
                                          .replace(" ()", "") for o in orders) or "không có"))
        blocked = summary.get("blocked", [])
        if blocked:
            lines.append("Bị chặn: " + ", ".join(f"{b['symbol']} ({b['code']})" for b in blocked))
        q = summary.get("execution_quality", {}).get("all_time", {}).get("market", {})
        if q.get("n"):
            lines.append(f"Trượt giá TB: {q['mean_bps']:+.1f} bps / {q['n']} lệnh (backtest giả định "
                         f"{summary['execution_quality']['backtest_assumed_bps']:g} bps)")
        shadow = summary.get("shadow_check", {})
        if shadow.get("match") is False:
            lines.append(f"⚠️ Shadow check: {shadow.get('detail')}")
    alerts = summary.get("alerts", [])
    if alerts:
        lines.append(f"Cảnh báo ({len(alerts)}):")
        lines.extend(f"- {a[:300]}" for a in alerts[:8])
    if report_name:
        lines.append(f"Báo cáo đầy đủ: {report_name} (đính kèm)")
    return "\n".join(lines)


def load_summary(today: date, runs_dir: Path | None = None) -> dict[str, Any] | None:
    path = run_dir(today, runs_dir) / "summary.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def latest_run_date(runs_dir: Path | None = None) -> date | None:
    runs_dir = runs_dir or RUNS_DIR
    dates: Sequence[str] = sorted(p.parent.name for p in runs_dir.glob("*/summary.json")) if runs_dir.exists() else []
    return date.fromisoformat(dates[-1]) if dates else None
