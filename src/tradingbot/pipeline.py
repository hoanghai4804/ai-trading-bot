"""One weekly run: broker snapshot -> bars -> signals -> risk review -> execution.

Everything external is injected (broker, bar loaders, latest prices), so the whole run
is tested with fakes. The HALT.lock / paper guards / run lock are applied by the caller.

Buy-signal window: sessions since the as_of of the last run that could trade (EXECUTE
with the market open, from state/last_run.json), clamped to
[strategy cross_lookback_sessions, schedule.max_signal_window_sessions]. A run that falls
on a market holiday therefore does not make the next run miss that week's crosses.
state/last_run.json is only written by EXECUTE runs.

After execution (phase 8): the shadow check replays the decision through the backtest
path (mismatch -> warning + alert), and bot fills since the previous EXECUTE run are
compared with their reference price (execution quality). EXECUTE runs also upsert the
local equity log (an extra peak source for the drawdown breaker) and append new fills
to state/execution_quality.csv.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import json

import pandas as pd

from tradingbot.config import Config, StrategyConfig
from tradingbot.data.market_data import LatestTrade, fresh_prices
from tradingbot.execution.executor import Alert, Broker, ExecutionReport, Executor, stderr_alert
from tradingbot.execution.orders import (StopFill, client_order_id, last_exits, new_positions_this_week,
                                         stop_fills_since)
from tradingbot.monitoring.execution_quality import FillRecord, append_fills, collect_fills
from tradingbot.monitoring.shadow import ShadowResult, order_keys, shadow_check
from tradingbot.reporting.equity_log import equity_log_series, load_equity_log, performance, upsert_equity
from tradingbot.risk.circuit_breakers import BreakerStatus, equity_series, evaluate_circuit_breakers, week_start
from tradingbot.risk.manager import AccountState, RiskDecision, RiskManager
from tradingbot.safety import STATE_DIR
from tradingbot.strategies.base import Signal
from tradingbot.strategies.registry import build_strategy

BarsLoader = Callable[[list[str], date, date], Mapping[str, pd.DataFrame]]   # (symbols, start, as_of)
PriceLoader = Callable[[Iterable[str]], Mapping[str, LatestTrade]]
RawClosesLoader = Callable[[list[str], date], Mapping[str, tuple[date, float]]]   # (symbols, as_of)
LAST_RUN_FILE = "last_run.json"

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
    signal_window: int = 0
    warnings: list[str] = field(default_factory=list)
    benchmark_symbol: str = ""
    benchmark_price: float | None = None
    position_details: list[Any] = field(default_factory=list)    # PositionSnapshot
    order_refs: dict[str, float] = field(default_factory=dict)    # client_order_id -> reference price
    shadow: ShadowResult | None = None
    fills: list[FillRecord] = field(default_factory=list)         # bot fills since the previous EXECUTE run
    performance: dict[str, Any] = field(default_factory=dict)
    positions_after: list[Any] = field(default_factory=list)      # PositionSnapshot, re-read after execution
    cash_after: float | None = None

    @property
    def mode(self) -> str:
        return "DRY-RUN" if self.dry_run else "EXECUTE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "signal_window_sessions": self.signal_window,
            "today": self.today.isoformat(), "as_of": self.as_of.isoformat(), "dry_run": self.dry_run,
            "market_open": self.market_open, "equity": self.equity, "cash": self.cash,
            "positions": self.positions, "new_positions_this_week": self.new_positions_this_week,
            "stop_fills": [{**asdict(f), "day": f.day.isoformat()} for f in self.stop_fills],
            "signals": [{**asdict(s), "as_of": s.as_of.isoformat(),
                         "cross_date": s.cross_date.isoformat() if s.cross_date else None} for s in self.signals],
            "decision": self.decision.to_dict(), "execution": self.execution.to_dict(),
            "warnings": list(self.warnings),
            "benchmark": {"symbol": self.benchmark_symbol, "price": self.benchmark_price},
            "performance": self.performance, "order_refs": self.order_refs,
            "shadow_check": self.shadow.to_dict() if self.shadow else None,
            "fills": [asdict(f) for f in self.fills],
        }


def run_weekly(
    cfg: Config,
    broker: WeeklyBroker,
    load_bars: BarsLoader,
    latest_prices: PriceLoader,
    load_raw_closes: RawClosesLoader,
    now: datetime,
    today: date,
    dry_run: bool = True,
    alert: Alert = stderr_alert,
    state_dir: Path = STATE_DIR,
    executor_factory: Callable[..., Executor] = Executor,
    order_refs_history: Mapping[str, float] | None = None,
) -> RunResult:
    """`now` is UTC (order-history window), `today` the New York date of the run.

    order_refs_history: client_order_id -> reference price of earlier runs (execution quality).
    """
    as_of = today - timedelta(days=1)   # last completed day; bars never include today
    account = broker.get_account()
    clock = broker.get_clock()
    position_details = [p for p in broker.get_positions() if p.qty != 0]
    positions = {p.symbol: p.qty for p in position_details}
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
    last_run = load_last_run(state_dir)
    window = signal_window(bars, as_of, _date_or_none(last_run.get("last_trading_as_of")),
                           int(cfg.strategy.params.get("cross_lookback_sessions", 1)),
                           cfg.schedule.max_signal_window_sessions)
    strategy = build_strategy(strategy_with_window(cfg.strategy, window))
    signals = strategy.generate_signals(bars, as_of, positions=list(positions), last_exit=exits)

    buy_symbols = sorted({s.symbol for s in signals if s.action == "buy"})
    wanted = set(buy_symbols) | set(positions)
    bench = cfg.benchmark_symbol
    all_fresh, all_notes = fresh_prices(latest_prices(wanted | {bench}), wanted | {bench}, now,
                                        timedelta(minutes=cfg.data.max_price_age_min))
    ref_prices = {s: p for s, p in all_fresh.items() if s in wanted}
    ref_notes = {s: n for s, n in all_notes.items() if s in wanted}
    bench_price = all_fresh.get(bench)
    if bench_price is None and bench in bars and not bars[bench].empty:
        bench_price = float(bars[bench]["close"].iloc[-1])
    raw_closes = dict(load_raw_closes(buy_symbols, as_of)) if buy_symbols else {}
    equity_rows = load_equity_log(state_dir)
    breakers = _breakers(cfg, broker, account.equity, today, equity_rows)
    acct = AccountState(equity=account.equity, cash=account.cash, positions=positions,
                        new_positions_this_week=week_count)
    decision = RiskManager(cfg.risk, cfg.watchlist).review(signals, as_of, acct, bars, ref_prices, breakers,
                                                           raw_closes=raw_closes, ref_notes=ref_notes)
    if breakers.halt or breakers.block_new_entries:
        alert("critical" if breakers.halt else "warning", "circuit breaker: " + "; ".join(breakers.reasons))

    planned = order_keys(decision.approved)     # before the executor appends stop_breached sells
    order_refs = {client_order_id(prefix, today, o.symbol, o.side): (o.ref_price if o.side == "buy"
                                                                     else ref_prices.get(o.symbol))
                  for o in decision.approved}
    order_refs = {k: float(v) for k, v in order_refs.items() if v}

    executor = executor_factory(broker, cfg.execution, today, dry_run=dry_run, alert=alert, state_dir=state_dir)
    report = executor.execute(decision, market_open, fresh_prices=ref_prices)

    shadow = shadow_check(cfg, planned, today, bars, positions, exits,
                          _date_or_none(last_run.get("last_trading_as_of")), account.equity, account.cash,
                          ref_prices, breakers, week_count)
    if not shadow.match:
        warnings.append(f"shadow check {shadow.describe()}")
        alert("warning", f"shadow check {shadow.describe()}")

    positions_after, cash_after = position_details, account.cash
    try:
        positions_after = [p for p in broker.get_positions() if p.qty != 0]
        cash_after = broker.get_account().cash
    except Exception as exc:  # noqa: BLE001 - reporting only
        warnings.append(f"positions not re-read after execution: {type(exc).__name__}: {exc}")

    since = datetime.fromisoformat(last_run["run_at"]) if last_run.get("run_at") else None
    fills: list[FillRecord] = []
    try:
        after = since - timedelta(days=1) if since else now - timedelta(days=ORDER_HISTORY_DAYS)
        fills = collect_fills(broker.get_orders(status="all", after=after, nested=True), prefix, since,
                              {**(order_refs_history or {}), **order_refs})
    except Exception as exc:  # noqa: BLE001 - monitoring must not break the run
        warnings.append(f"execution quality not collected: {type(exc).__name__}: {exc}")

    result = RunResult(today=today, as_of=as_of, dry_run=dry_run, market_open=market_open, equity=account.equity,
                       cash=account.cash, positions=positions, new_positions_this_week=week_count,
                       stop_fills=stop_fills, signals=signals, decision=decision, execution=report,
                       signal_window=window, warnings=warnings, benchmark_symbol=bench,
                       benchmark_price=bench_price, position_details=position_details, order_refs=order_refs,
                       shadow=shadow, fills=fills, positions_after=positions_after, cash_after=cash_after,
                       performance=performance(equity_rows, today, account.equity, bench_price))
    if not dry_run:
        save_last_run(result, now, last_run, state_dir)
        upsert_equity(today, account.equity, account.cash, bench, bench_price, state_dir)
        append_fills(fills, state_dir)
    return result


def signal_window(bars: Mapping[str, pd.DataFrame], as_of: date, last_trading_as_of: date | None,
                  minimum: int, maximum: int) -> int:
    """Sessions after the last trading run's as_of up to `as_of`, clamped to [minimum, maximum]."""
    if last_trading_as_of is None:
        return minimum
    sessions = pd.DatetimeIndex([])
    for frame in bars.values():
        sessions = sessions.union(frame.index)
    n = int(((sessions > pd.Timestamp(last_trading_as_of)) & (sessions <= pd.Timestamp(as_of))).sum())
    return max(minimum, min(n, maximum))


def strategy_with_window(cfg: StrategyConfig, window: int) -> StrategyConfig:
    if "cross_lookback_sessions" not in cfg.params:
        return cfg
    return cfg.model_copy(update={"params": {**cfg.params, "cross_lookback_sessions": window}})


def _date_or_none(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


def load_last_run(state_dir: Path = STATE_DIR) -> dict[str, Any]:
    path = state_dir / LAST_RUN_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_last_run(result: RunResult, now: datetime, previous: Mapping[str, Any],
                  state_dir: Path = STATE_DIR) -> Path:
    """Record an EXECUTE run. `last_trading_as_of` only advances when the market was open."""
    payload = {
        "run_at": now.isoformat(timespec="seconds"),
        "today": result.today.isoformat(),
        "as_of": result.as_of.isoformat(),
        "mode": result.mode,
        "market_open": result.market_open,
        "orders_sent": result.execution.orders_sent,
        "signal_window_sessions": result.signal_window,
        "last_trading_as_of": result.as_of.isoformat() if result.market_open else previous.get("last_trading_as_of"),
    }
    path = state_dir / LAST_RUN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _breakers(cfg: Config, broker: WeeklyBroker, equity: float, today: date,
              equity_rows: list[dict[str, str]]) -> BreakerStatus:
    """Peak = max of Alpaca's daily portfolio history and the local equity log."""
    history = equity_series(broker.get_daily_equity_history())
    return evaluate_circuit_breakers(equity, today, cfg.risk.circuit_breakers, history,
                                     extra_histories=[equity_log_series(equity_rows)])
