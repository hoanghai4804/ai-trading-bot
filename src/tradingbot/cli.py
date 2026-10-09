"""Command-line entry point: python -m tradingbot <command>.

Commands are added phase by phase; each one returns a process exit code.
"""

from __future__ import annotations

import argparse

from tradingbot.config import load_config


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print(f"mode={cfg.mode} strategy={cfg.strategy.name} watchlist={','.join(cfg.watchlist)}")
    print("schedule=" + ", ".join(f"{r.weekday}:{r.report}" for r in cfg.schedule.runs))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingbot")
    parser.add_argument("--config", default=None, help="path to config.yaml (default: config/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="validate the config file and print a short summary").set_defaults(
        func=_cmd_config
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
