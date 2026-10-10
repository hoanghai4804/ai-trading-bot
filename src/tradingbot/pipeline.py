"""One weekly run: broker snapshot -> bars -> signals -> risk review -> execution.

Everything external is injected (broker, bar loader, latest prices), so the whole run
is tested with fakes. The HALT.lock / paper guards / run lock are applied by the caller.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from tradingbot.config import Config
from tradingbot.execution.executor import Alert, Broker, ExecutionReport, Executor, stderr_alert
from tradingbot.execution.orders import StopFill, last_exits, new_positions_this_week, stop_fills_since
from tradingbot.risk.circuit_breakers import BreakerStatus, equity_series, evaluate_circuit_breakers, week_start
from tradingbot.risk.manager import AccountState, RiskDecision, RiskManager
from tradingbot.safety import STATE_DIR
from tradingbot.strategies.base import Signal
from tradingbot.strategies.registry import build_strategy

BarsLoader = Callable[[list[str], date, date], Mapping[str, pd.DataFrame]]   # (symbols, start, as_of)
PriceLoader = Callable[[Iterable[str]], Mapping[str, float]]

# Order history window: covers this week's buys, last week's stop fills and recent exits
# for the no-reentry rule (a buy signal's cross is at most a few sessions old).
ORDER_HISTORY_DAYS = 35


class WeeklyBroker(Broker, Protocol):
    """Read methods the run needs on top of the executor's."""

    def get_account(self) -> Any: ...
    def get_clock(self) -> Any: ...
    def get_daily_equity_history(self, period: str = "1A") -> list[Any]: ...


@dataclass
class RunResult:
    today: date
    as_of: date
    dry_run: bool
    market_open: bool
    equity: float
    cash: float
    positions: dict[str, float]
    new_positions_this_week: int
    stop_fills: list[StopFill]
    signals: list[Signal]
    decision: RiskDecision
    execution: ExecutionReport
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "today": self.today.isoformat(), "as_of": self.as_of.isoformat(), "dry_run": self.dry_run,
            "market_open": self.market_open, "equity": self.equity, "cash": self.cash,
            "positions": self.positions, "new_positions_this_week": self.new_positions_this_week,
            "stop_fills": [{**asdict(f), "day": f.day.isoformat()} for f in self.stop_fills],
            "signals": [{**asdict(s), "as_of": s.as_of.isoformat(),
                         "cross_date": s.cross_date.isoformat() if s.cross_date else None} for s in self.signals],
            "decision": self.decision.to_dict(), "execution": self.execution.to_dict(),
            "warnings": list(self.warnings),
        }


def run_weekly(
    cfg: Config,
    broker: WeeklyBroker,
    load_bars: BarsLoader,
    latest_prices: PriceLoader,
    now: datetime,
    today: date,
    dry_run: bool = True,
    alert: Alert = stderr_alert,
    state_dir: Path = STATE_DIR,
    executor_factory: Callable[..., Executor] = Executor,
) -> RunResult:
    """`now` is UTC (order-history window), `today` the New York date of the run."""
    as_of = today - timedelta(days=1)   # last completed day; bars never include today
    account = broker.get_account()
    clock = broker.get_clock()
    positions = {p.symbol: p.qty for p in broker.get_positions() if p.qty != 0}
    warnings = []
    if account.trading_blocked:
        warnings.append("account trading_blocked: no order sent")
    market_open = bool(clock.is_open) and not account.trading_blocked

    history = broker.get_orders(status="all", after=now - timedelta(days=ORDER_HISTORY_DAYS), nested=True)
    prefix = cfg.execution.order_id_prefix
    week_count = new_positions_this_week(history, prefix, today)
    exits = last_exits(history, prefix)
    stop_fills = stop_fills_since(history, prefix, week_start(today) - timedelta(days=7))

    symbols = list(dict.fromkeys([*cfg.watchlist, *sorted(positions)]))
    bars = load_bars(symbols, as_of - timedelta(days=cfg.data.history_days), as_of)
    strategy = build_strategy(cfg.strategy)
    signals = strategy.generate_signals(bars, as_of, positions=list(positions), last_exit=exits)

    wanted = {s.symbol for s in signals if s.action == "buy"} | set(positions)
    ref_prices = dict(latest_prices(wanted))
    breakers = _breakers(cfg, broker, account.equity, today)
    acct = AccountState(equity=account.equity, cash=account.cash, positions=positions,
                        new_positions_this_week=week_count)
    decision = RiskManager(cfg.risk, cfg.watchlist).review(signals, as_of, acct, bars, ref_prices, breakers)
    if breakers.halt or breakers.block_new_entries:
        alert("critical" if breakers.halt else "warning", "circuit breaker: " + "; ".join(breakers.reasons))

    executor = executor_factory(broker, cfg.execution, today, dry_run=dry_run, alert=alert, state_dir=state_dir)
    report = executor.execute(decision, market_open)
    return RunResult(today=today, as_of=as_of, dry_run=dry_run, market_open=market_open, equity=account.equity,
                     cash=account.cash, positions=positions, new_positions_this_week=week_count,
                     stop_fills=stop_fills, signals=signals, decision=decision, execution=report,
                     warnings=warnings)


def _breakers(cfg: Config, broker: WeeklyBroker, equity: float, today: date) -> BreakerStatus:
    # The local equity log (extra peak source) is added in phase 8.
    history = equity_series(broker.get_daily_equity_history())
    return evaluate_circuit_breakers(equity, today, cfg.risk.circuit_breakers, history)
