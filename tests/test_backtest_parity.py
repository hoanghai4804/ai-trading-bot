"""Parity: the backtest replays the week of 2026-04-27 exactly like the live pipeline.

Fixture = real Alpaca SIP adjusted daily bars (2025-03-20 .. 2026-04-27) for the watchlist.
Live side: run_weekly (dry-run) with a fake 100k paper account, the 04-27 open as fresh
latest trade and the 04-24 close as raw close. Backtest side: the engine on the same bars
with a fresh 100k account starting 2026-04-27.
"""

from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path

import pandas as pd
from fake_broker import FakeBroker

from tradingbot.backtest.engine import fixed_strategy, run_backtest
from tradingbot.config import load_config
from tradingbot.data.market_data import LatestTrade
from tradingbot.execution.executor import Executor
from tradingbot.pipeline import run_weekly

CFG = load_config()
MONDAY = date(2026, 4, 27)
NOW = datetime(2026, 4, 27, 15, 7, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "bars_2026-04-27.csv.gz"


def _bars() -> dict[str, pd.DataFrame]:
    df = pd.read_csv(FIXTURE, parse_dates=["date"])
    return {s: g.drop(columns="symbol").set_index("date").sort_index() for s, g in df.groupby("symbol")}


BARS = _bars()


def _live(state_dir):
    fb = FakeBroker()
    friday = {s: f.loc[:"2026-04-26"] for s, f in BARS.items()}   # live never sees Monday's bar
    return run_weekly(
        CFG, fb,
        load_bars=lambda symbols, start, as_of: {s: friday[s].loc[pd.Timestamp(start):] for s in symbols},
        latest_prices=lambda symbols: {s: LatestTrade(float(BARS[s].loc["2026-04-27", "open"]), NOW)
                                       for s in symbols},
        load_raw_closes=lambda symbols, as_of: {s: (friday[s].index[-1].date(), float(friday[s]["close"].iloc[-1]))
                                                for s in symbols},
        now=NOW, today=MONDAY, dry_run=True, alert=lambda *a: None, state_dir=state_dir,
        executor_factory=partial(Executor, sleep=fb.sleep, monotonic=fb.monotonic),
    ).decision


def _backtest():
    result = run_backtest(CFG, BARS, MONDAY, MONDAY, fixed_strategy(CFG))
    [week] = result.weeks
    return week.decision, result


def _key(decision):
    return ([(o.symbol, o.side, o.qty, o.ref_price, o.stop_price) for o in decision.approved],
            [(b.symbol, b.code) for b in decision.blocked])


def test_backtest_week_matches_live_pipeline(tmp_path):
    live = _live(tmp_path)
    bt, _ = _backtest()
    assert _key(bt) == _key(live)


def test_week_2026_04_27_decisions():
    decision, result = _backtest()
    assert [(o.symbol, o.side) for o in decision.approved] == [("AAPL", "buy"), ("SPY", "buy"), ("NVDA", "buy")]
    assert [(b.symbol, b.code) for b in decision.blocked] == [("GOOGL", "weekly_limit")]
    # filled at Monday's open plus slippage, with the risk manager's stop
    for trade, order in zip(result.trades, decision.approved):
        assert trade.entry_price == order.ref_price * (1 + CFG.costs.slippage_bps / 10_000)
        assert trade.ref_price == BARS[order.symbol].loc["2026-04-27", "open"]
        assert trade.stop == order.stop_price
