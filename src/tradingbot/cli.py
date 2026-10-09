"""Command-line entry point: python -m tradingbot <command>.

Commands are added phase by phase; each one returns a process exit code.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

from tradingbot.broker.alpaca_client import BrokerClient
from tradingbot.config import load_config
from tradingbot.credentials import MissingCredentialsError, get_alpaca_credentials
from tradingbot.data.market_data import MarketData, default_alpaca_source
from tradingbot.indicators.technical import atr, return_correlation, sma
from tradingbot.safety import SafetyError, read_halt


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
