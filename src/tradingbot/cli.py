"""Command-line entry point: python -m tradingbot <command>.

Commands are added phase by phase; each one returns a process exit code.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from tradingbot.broker.alpaca_client import BrokerClient
from tradingbot.config import Config, load_config
from tradingbot.credentials import MissingCredentialsError, get_alpaca_credentials
from tradingbot.data.market_data import LatestPrices, MarketData, default_alpaca_source, today_new_york
from tradingbot.execution.test_order import run_test_order
from tradingbot.indicators.technical import atr, return_correlation, sma
from tradingbot.pipeline import RunResult, run_weekly
from tradingbot.safety import RunLock, SafetyError, assert_not_halted, read_halt
from tradingbot.strategies.registry import build_strategy


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    build_strategy(cfg.strategy)  # validates strategy name + params
    print(f"mode={cfg.mode} strategy={cfg.strategy.name} watchlist={','.join(cfg.watchlist)}")
    print("schedule=" + ", ".join(f"{r.weekday}:{r.report}" for r in cfg.schedule.runs))
    return 0


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


def _print_run(result: RunResult) -> None:
    mode = "DRY-RUN (nothing sent)" if result.dry_run else "EXECUTE (paper orders)"
    print(f"run today={result.today} as_of={result.as_of} mode={mode} market_open={result.market_open}")
    print(f"equity={result.equity:,.2f} cash={result.cash:,.2f} positions="
          + (",".join(f"{s}:{q:g}" for s, q in sorted(result.positions.items())) or "-")
          + f" new_positions_this_week={result.new_positions_this_week}")
    b = result.decision.breakers
    week = "unknown" if b.previous_week_return is None else f"{b.previous_week_return:+.2%}"
    print(f"breakers: halt={b.halt} block_new={b.block_new_entries} prev_week={week} drawdown={b.drawdown:.2%}")
    for f in result.stop_fills:
        print(f"stop filled since last week: {f.symbol} {f.qty:g} @ {f.price} on {f.day}")
    print(f"signals={len(result.signals)}: " + (", ".join(f"{s.action} {s.symbol}" for s in result.signals) or "-"))
    for o in result.decision.approved:
        extra = f" ref={o.ref_price:.2f} stop={o.stop_price:.2f} ({o.limited_by})" if o.side == "buy" else ""
        print(f"approved {o.side:4} {o.symbol:6} qty={o.qty:g}{extra} | {o.reason}")
    for bl in result.decision.blocked:
        print(f"blocked  {bl.side:4} {bl.symbol:6} {bl.code}: {bl.detail}")
    for r in result.execution.results:
        stop = f" stop={r.stop_price:.2f}" if r.stop_price else ""
        qty = f" qty={r.qty:g}" if r.qty is not None else ""
        print(f"exec {r.status:9} {r.side:4} {r.symbol:6}{qty}{stop} id={r.client_order_id or '-'} {r.detail}")
    print(f"orders_sent={result.execution.orders_sent}")
    for w in [*result.warnings, *result.execution.alerts]:
        print(f"WARNING {w}")


def _connect(args: argparse.Namespace) -> tuple[Config, BrokerClient]:
    cfg = load_config(args.config)
    return cfg, BrokerClient.connect(get_alpaca_credentials())


def _cmd_run(args: argparse.Namespace) -> int:
    """Weekly run. Dry-run unless --execute: then approved orders are sent to the PAPER account."""
    try:
        assert_not_halted()
        cfg, broker = _connect(args)
        with RunLock(ttl=timedelta(minutes=cfg.execution.run_lock_ttl_min)):
            md = MarketData(source=default_alpaca_source(), config=cfg.data)
            result = run_weekly(
                cfg, broker,
                load_bars=lambda symbols, start, as_of: md.get_bars(symbols, start=start, as_of=as_of),
                latest_prices=LatestPrices(cfg.data.latest_price_feed).get,
                now=datetime.now(timezone.utc), today=today_new_york(), dry_run=not args.execute,
            )
    except (SafetyError, MissingCredentialsError) as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2
    _print_run(result)
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
    print(f"test-order mode={'DRY-RUN (nothing sent)' if report.dry_run else 'EXECUTE (paper)'}")
    for step, ok, detail in report.steps:
        print(f"{'PASS' if ok else 'FAIL'} {step}: {detail}")
    print("RESULT " + ("PASS" if report.ok else "FAIL"))
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingbot")
    parser.add_argument("--config", default=None, help="path to config.yaml (default: config/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="validate the config file and print a short summary").set_defaults(
        func=_cmd_config
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
    run = sub.add_parser("run", help="weekly run: signals -> risk -> orders (dry-run unless --execute)")
    run.add_argument("--execute", action="store_true", help="send approved orders to the PAPER account")
    run.set_defaults(func=_cmd_run)
    test_order = sub.add_parser("test-order", help="MANUAL: buy 1 test share with OTO stop, verify, close "
                                                   "(dry-run unless --execute; never in the weekly run)")
    test_order.add_argument("--execute", action="store_true", help="really send the test orders (market open)")
    test_order.set_defaults(func=_cmd_test_order)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
