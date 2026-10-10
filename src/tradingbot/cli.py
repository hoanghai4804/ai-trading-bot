"""Command-line entry point: python -m tradingbot <command>.

Commands are added phase by phase; each one returns a process exit code.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from tradingbot.broker.alpaca_client import BrokerClient
from tradingbot.config import Config, load_config
from tradingbot.credentials import MissingCredentialsError, get_alpaca_credentials
from tradingbot.data.market_data import LatestPrices, MarketData, default_alpaca_source, today_new_york
from tradingbot.execution.test_order import run_test_order
from tradingbot.indicators.technical import atr, return_correlation, sma
from tradingbot.notify.discord import DiscordNotifier, notifier_from_env
from tradingbot.pipeline import run_weekly
from tradingbot.reporting.run_text import format_run, mode_line
from tradingbot.safety import RunLock, SafetyError, assert_not_halted, read_halt
from tradingbot.strategies.registry import build_strategy


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    build_strategy(cfg.strategy)  # validates strategy name + params
    print(f"mode={cfg.mode} strategy={cfg.strategy.name} watchlist={','.join(cfg.watchlist)}")
    print("schedule=" + ", ".join(f"{r.weekday}:{r.report}" for r in cfg.schedule.runs))
    return 0


def _cmd_env_check(args: argparse.Namespace) -> int:
    """Print whether each required env var is set. Never prints a value."""
    from tradingbot.credentials import env_presence

    presence = env_presence()
    for name, present in presence.items():
        print(f"{name}: {'set' if present else 'MISSING'}")
    return 0 if all(presence.values()) else 1


def _cmd_check(args: argparse.Namespace) -> int:
    """Connect to the paper account and print a short health summary (never prints keys)."""
    load_config(args.config)
    halt = read_halt()
    try:
        broker = BrokerClient.connect(get_alpaca_credentials())
        account = broker.get_account()
        clock = broker.get_clock()
        positions = broker.get_positions()
    except (SafetyError, MissingCredentialsError) as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2
    print(f"account={account.account_number_masked} status={account.status} currency={account.currency}")
    print(f"equity={account.equity:,.2f} cash={account.cash:,.2f} trading_blocked={account.trading_blocked}")
    print(f"positions={len(positions)}" + (" (" + ", ".join(p.symbol for p in positions) + ")" if positions else ""))
    market = "OPEN" if clock.is_open else f"CLOSED (next open {clock.next_open.isoformat()})"
    print(f"market={market}")
    print("halt=" + (f"YES ({halt.get('reason')})" if halt else "no"))
    return 0


def _cmd_data(args: argparse.Namespace) -> int:
    """Fetch watchlist bars up to as_of and print indicator values (sanity check, no decisions)."""
    cfg = load_config(args.config)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    md = MarketData(source=default_alpaca_source(), config=cfg.data)
    start = (as_of or md.today()) - timedelta(days=cfg.data.history_days)
    bars = md.get_bars(cfg.watchlist, start=start, as_of=as_of)
    fast, slow = cfg.strategy.params.get("fast", 20), cfg.strategy.params.get("slow", 50)
    print(f"{'symbol':6} {'date':10} {'close':>9} {'sma'+str(fast):>9} {'sma'+str(slow):>9} {'atr':>7} {'atr%':>5}")
    for symbol, b in bars.items():
        close = b["close"]
        a = atr(b, cfg.risk.atr_period).iloc[-1]
        print(f"{symbol:6} {b.index[-1].date()} {close.iloc[-1]:9.2f} {sma(close, fast).iloc[-1]:9.2f} "
              f"{sma(close, slow).iloc[-1]:9.2f} {a:7.2f} {a / close.iloc[-1]:5.1%}")
    closes = pd.DataFrame({s: b["close"] for s, b in bars.items()})
    corr = return_correlation(closes, cfg.risk.correlation.lookback_days)
    upper = np.triu(np.ones(corr.shape, dtype=bool), k=1)  # each pair once, no diagonal
    pairs = corr.where(upper).stack().sort_values(ascending=False)
    print("top correlations:", ", ".join(f"{a}/{b}={v:.2f}" for (a, b), v in pairs.head(5).items()))
    return 0


def _cmd_signals(args: argparse.Namespace) -> int:
    """Print strategy proposals up to as_of (no risk checks, no orders)."""
    cfg = load_config(args.config)
    strategy = build_strategy(cfg.strategy)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    md = MarketData(source=default_alpaca_source(), config=cfg.data)
    as_of = as_of or md.today() - timedelta(days=1)
    bars = md.get_bars(cfg.watchlist, start=as_of - timedelta(days=cfg.data.history_days), as_of=as_of)
    held = [s.strip().upper() for s in args.held.split(",") if s.strip()] if args.held else []
    signals = strategy.generate_signals(bars, as_of, positions=held)
    print(f"strategy={strategy.name} as_of={as_of} held={','.join(held) or '-'} signals={len(signals)}")
    for s in signals:
        print(f"{s.action:4} {s.symbol:6} close={s.close:.2f} fast={s.fast_ma:.2f} slow={s.slow_ma:.2f} | {s.reason}")
    return 0


def _connect(args: argparse.Namespace) -> tuple[Config, BrokerClient]:
    cfg = load_config(args.config)
    return cfg, BrokerClient.connect(get_alpaca_credentials())


def _notifier(cfg: Config, args: argparse.Namespace, tag: str = "") -> DiscordNotifier:
    if getattr(args, "no_notify", False):
        return DiscordNotifier(None, cfg.notify, tag=tag)   # alerts still go to stderr
    return notifier_from_env(cfg.notify, tag=tag)


def _cmd_run(args: argparse.Namespace) -> int:
    """Weekly run. Dry-run unless --execute: then approved orders are sent to the PAPER account.

    Alerts go to stderr + Discord. Outputs: runs/<date>/summary.md, summary.json, decisions.json
    (also when the run aborts: a short "aborted" summary).
    """
    from tradingbot.monitoring.execution_quality import load_order_refs
    from tradingbot.reporting.summary import RUNS_DIR, write_abort_summary, write_run_outputs

    cfg = load_config(args.config)
    dry_run = not args.execute
    notifier = _notifier(cfg, args, tag="[DRY-RUN] " if dry_run else "")
    today = today_new_york()
    try:
        assert_not_halted()
        _, broker = _connect(args)
        with RunLock(ttl=timedelta(minutes=cfg.execution.run_lock_ttl_min)):
            md = MarketData(source=default_alpaca_source(), config=cfg.data)
            result = run_weekly(
                cfg, broker,
                load_bars=lambda symbols, start, as_of: md.get_bars(symbols, start=start, as_of=as_of),
                latest_prices=LatestPrices(cfg.data.latest_price_feed).get,
                load_raw_closes=md.get_raw_closes,
                now=datetime.now(timezone.utc), today=today, dry_run=dry_run, alert=notifier.alert,
                order_refs_history=load_order_refs(RUNS_DIR),
            )
    except (SafetyError, MissingCredentialsError) as exc:
        print(mode_line(dry_run))
        print(f"ABORT: {exc}", file=sys.stderr)
        notifier.alert("critical", f"run aborted: {exc}")
        write_abort_summary(today, dry_run, f"{type(exc).__name__}: {exc}", cfg)
        return 2
    except Exception as exc:
        print(mode_line(dry_run))
        notifier.alert("critical", f"run failed: {type(exc).__name__}: {exc}")
        write_abort_summary(today, dry_run, f"error {type(exc).__name__}: {exc}", cfg)
        raise
    out = write_run_outputs(result, cfg)
    print("\n".join(format_run(result)))
    print(f"outputs: {out}")
    return 0


def _cmd_should_run(args: argparse.Namespace) -> int:
    """RUN / SKIP / HALT for today + the exact `run` command (mode from TRADINGBOT_MODE).

    Exit 0 = RUN, 3 = SKIP, 4 = HALT. On HALT: critical alert + an "aborted" summary for today, so the
    weekly prompt only has to write the brief report, notify and commit.
    """
    import os

    from tradingbot.reporting.summary import load_summary, write_abort_summary
    from tradingbot.scheduling import MODE_ENV_VAR, decide_run

    cfg = load_config(args.config)
    today = date.fromisoformat(args.today) if args.today else today_new_york()
    halt = read_halt()
    decision = decide_run(cfg, today, os.environ.get(MODE_ENV_VAR), halt, load_summary(today), force=args.force)
    print("\n".join(decision.lines()))
    if decision.action == "HALT":
        notifier = _notifier(cfg, args, tag="[DRY-RUN] " if decision.dry_run else "")
        notifier.alert("critical", f"weekly run not started: {decision.reason}")
        out = write_abort_summary(today, decision.dry_run, f"HALTED: {decision.reason}", cfg)
        print(f"outputs: {out}")
    return decision.exit_code


def _cmd_notify_alert(args: argparse.Namespace) -> int:
    """One alert (stderr + Discord) for situations the weekly prompt cannot handle itself."""
    cfg = load_config(args.config)
    _notifier(cfg, args).alert(args.level, args.message[:1500])
    return 0


def _cmd_notify_report(args: argparse.Namespace) -> int:
    """Send the run summary (< 2000 chars) + the report file to Discord. Never raises."""
    from tradingbot.config import PROJECT_ROOT
    from tradingbot.reporting.summary import discord_message, latest_run_date, load_summary, run_dir

    cfg = load_config(args.config)
    day = date.fromisoformat(args.date) if args.date else latest_run_date()
    summary = load_summary(day) if day else None
    if summary is None:
        print("no runs/<date>/summary.json found: nothing to send", file=sys.stderr)
        return 1
    path = Path(args.path) if args.path else PROJECT_ROOT / "reports" / "weekly" / f"{day.isoformat()}.md"
    if not path.exists():
        print(f"report {path} not found: attaching summary.md instead", file=sys.stderr)
        path = run_dir(day) / "summary.md"
    ok = _notifier(cfg, args).send(discord_message(summary, path.name), path)
    print(f"notify report {day}: " + ("sent" if ok else "NOT sent (see logs/notify.log)"))
    return 0 if ok else 1


def _cmd_notify_test(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ok = _notifier(cfg, args).send(f"✅ tradingbot notify-test {stamp}: Discord hoạt động bình thường.")
    print("notify-test: " + ("sent" if ok else "NOT sent (see logs/notify.log)"))
    return 0 if ok else 1


def _cmd_compact(args: argparse.Namespace) -> int:
    from tradingbot.memory.compact import compact
    from tradingbot.reporting.summary import MEMORY_DIR

    cfg = load_config(args.config)
    today = date.fromisoformat(args.today) if args.today else today_new_york()
    counts = compact(MEMORY_DIR, today, cfg.memory.keep_weeks_detailed)
    print(f"compact: kept {counts['kept']} detailed entr(y/ies), archived {counts['archived']}")
    return 0


def _cmd_record_sentiment(args: argparse.Namespace) -> int:
    """Validate a sentiment JSON and upsert it into data/sentiment/sentiment_log.csv (data only)."""
    from tradingbot.sentiment.record import SentimentError, load_file, record_sentiment

    cfg = load_config(args.config)
    try:
        data = load_file(Path(args.file), cfg.watchlist)
    except SentimentError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 1
    md = MarketData(source=default_alpaca_source(), config=cfg.data)
    counts = record_sentiment(data, md.get_raw_closes)
    print(f"record-sentiment {data.date}: added {counts['added']}, replaced {counts['replaced']}")
    return 0


def _cmd_test_order(args: argparse.Namespace) -> int:
    """Manual order-path check (1 share, OTO stop, then close). Never used by the weekly run."""
    try:
        assert_not_halted()
        cfg, broker = _connect(args)
        with RunLock(ttl=timedelta(minutes=cfg.execution.run_lock_ttl_min)):
            report = run_test_order(cfg, broker, LatestPrices(cfg.data.latest_price_feed).get,
                                    now=datetime.now(timezone.utc), dry_run=not args.execute)
    except (SafetyError, MissingCredentialsError) as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2
    print(mode_line(report.dry_run))
    for step, ok, detail in report.steps:
        print(f"{'PASS' if ok else 'FAIL'} {step}: {detail}")
    print("RESULT " + ("PASS" if report.ok else "FAIL"))
    return 0 if report.ok else 1


def _cmd_backtest(args: argparse.Namespace) -> int:
    """Weekly backtest + walk-forward + benchmarks -> reports/backtest/ (data only, no account access)."""
    from concurrent.futures import ProcessPoolExecutor
    from pathlib import Path

    from tradingbot.backtest.report import build, write_reports
    from tradingbot.config import PROJECT_ROOT

    cfg = load_config(args.config)
    md = MarketData(source=default_alpaca_source(), config=cfg.data)
    end = date.fromisoformat(args.end) if args.end else md.today() - timedelta(days=1)
    bars = md.get_bars(list(dict.fromkeys([*cfg.watchlist, cfg.benchmark_symbol])), start=cfg.backtest.start,
                       as_of=end)
    cache = pd.read_parquet(md.cache_dir / f"{cfg.benchmark_symbol}.parquet")
    info = {"first_bar": min(b.index[0] for b in bars.values()).date(),
            "last_bar": max(b.index[-1] for b in bars.values()).date(),
            "feed": cache.attrs.get("feed", cfg.data.feed)}
    print(f"backtest data {info['first_bar']} -> {info['last_bar']} feed={info['feed']} symbols={len(bars)}")
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        bundle = build(cfg, bars, info, map_fn=pool.map, seeds=args.seeds)
    out = Path(args.out) if args.out else PROJECT_ROOT / "reports" / "backtest"
    for path in write_reports(bundle, bars, out):
        print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingbot")
    parser.add_argument("--config", default=None, help="path to config.yaml (default: config/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="validate the config file and print a short summary").set_defaults(
        func=_cmd_config
    )
    sub.add_parser("env-check", help="required env vars: set / MISSING (never prints values)").set_defaults(
        func=_cmd_env_check
    )
    sub.add_parser("check", help="connect to Alpaca paper and print account/market status").set_defaults(
        func=_cmd_check
    )
    data = sub.add_parser("data", help="fetch watchlist bars and print indicators up to as_of")
    data.add_argument("--as-of", default=None, help="YYYY-MM-DD, must be before today (default: yesterday)")
    data.set_defaults(func=_cmd_data)
    signals = sub.add_parser("signals", help="print strategy signals up to as_of (no risk checks, no orders)")
    signals.add_argument("--as-of", default=None, help="YYYY-MM-DD, must be before today (default: yesterday)")
    signals.add_argument("--held", default="", help="comma-separated symbols to treat as held (exit checks)")
    signals.set_defaults(func=_cmd_signals)
    should_run = sub.add_parser("should-run", help="RUN / SKIP / HALT for today + the exact run command "
                                                   "(mode from TRADINGBOT_MODE; exit 0 / 3 / 4)")
    should_run.add_argument("--force", nargs="?", const="brief", choices=("brief", "full"), default=None,
                            help="run on an unscheduled day with this report (default brief; DRY-RUN only)")
    should_run.add_argument("--today", default=None, help="YYYY-MM-DD (default: today in New York)")
    should_run.set_defaults(func=_cmd_should_run)
    run = sub.add_parser("run", help="weekly run: signals -> risk -> orders (dry-run unless --execute)")
    run.add_argument("--execute", action="store_true", help="send approved orders to the PAPER account")
    run.add_argument("--no-notify", action="store_true", help="alerts to stderr only (no Discord)")
    run.set_defaults(func=_cmd_run)
    notify = sub.add_parser("notify", help="Discord notifications")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    report = notify_sub.add_parser("report", help="send the run summary + report file (.md) to Discord")
    report.add_argument("--path", default=None, help="report file (default: reports/weekly/<date>.md, "
                                                      "else runs/<date>/summary.md)")
    report.add_argument("--date", default=None, help="run date YYYY-MM-DD (default: latest run)")
    report.set_defaults(func=_cmd_notify_report)
    alert = notify_sub.add_parser("alert", help="send one alert (stderr + Discord)")
    alert.add_argument("--level", choices=("info", "warning", "critical"), default="warning")
    alert.add_argument("--message", required=True)
    alert.set_defaults(func=_cmd_notify_alert)
    sub.add_parser("notify-test", help="send a test message to Discord").set_defaults(func=_cmd_notify_test)
    compact = sub.add_parser("compact", help="keep the last N weeks of memory/journal.md, archive older ones")
    compact.add_argument("--today", default=None, help="YYYY-MM-DD (default: today in New York)")
    compact.set_defaults(func=_cmd_compact)
    sentiment = sub.add_parser("record-sentiment", help="validate a news-sentiment JSON and log it (data only)")
    sentiment.add_argument("--file", required=True, help="path to the sentiment JSON (runs/<date>/sentiment.json)")
    sentiment.set_defaults(func=_cmd_record_sentiment)
    test_order = sub.add_parser("test-order", help="MANUAL: buy 1 test share with OTO stop, verify, close "
                                                   "(dry-run unless --execute; never in the weekly run)")
    test_order.add_argument("--execute", action="store_true", help="really send the test orders (market open)")
    test_order.set_defaults(func=_cmd_test_order)
    backtest = sub.add_parser("backtest", help="weekly backtest, walk-forward, benchmarks -> reports/backtest/")
    backtest.add_argument("--end", default=None, help="last session YYYY-MM-DD (default: yesterday)")
    backtest.add_argument("--seeds", type=int, default=None, help="random benchmark seeds (default: config)")
    backtest.add_argument("--workers", type=int, default=None, help="worker processes (default: CPU count)")
    backtest.add_argument("--out", default=None, help="output directory (default: reports/backtest)")
    backtest.set_defaults(func=_cmd_backtest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
