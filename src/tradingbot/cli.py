"""Command-line entry point: python -m tradingbot <command>.

Commands are added phase by phase; each one returns a process exit code.
"""

from __future__ import annotations

import argparse
import sys

from tradingbot.broker.alpaca_client import BrokerClient
from tradingbot.config import load_config
from tradingbot.credentials import MissingCredentialsError, get_alpaca_credentials
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
