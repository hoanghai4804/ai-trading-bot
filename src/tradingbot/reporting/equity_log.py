"""Local equity log state/equity_history.csv: one row per New York date (upsert), EXECUTE runs only.

Columns: date, equity, cash, benchmark_symbol, benchmark_price. The benchmark price is
the latest RAW trade at run time (the last close when no fresh trade), so the benchmark
comparison is a price return (dividends excluded, ~1-2%/year for SPY).
It is also an extra peak source for the drawdown breaker (with Alpaca's portfolio history).
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from tradingbot.safety import STATE_DIR

EQUITY_FILE = "equity_history.csv"
COLUMNS = ["date", "equity", "cash", "benchmark_symbol", "benchmark_price"]


def load_equity_log(state_dir: Path = STATE_DIR) -> list[dict[str, str]]:
    path = state_dir / EQUITY_FILE
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return sorted(csv.DictReader(f), key=lambda r: r["date"])


def upsert_equity(day: date, equity: float, cash: float, benchmark_symbol: str, benchmark_price: float | None,
                  state_dir: Path = STATE_DIR) -> Path:
    """Insert or replace the row of `day` (a rerun on the same day never adds a second row)."""
    rows = {r["date"]: r for r in load_equity_log(state_dir)}
    rows[day.isoformat()] = {"date": day.isoformat(), "equity": f"{equity:.2f}", "cash": f"{cash:.2f}",
                             "benchmark_symbol": benchmark_symbol,
                             "benchmark_price": "" if benchmark_price is None else f"{benchmark_price:.4f}"}
    path = state_dir / EQUITY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows[d] for d in sorted(rows))
    return path


def equity_log_series(rows: Sequence[dict[str, str]]) -> pd.Series:
    return pd.Series([float(r["equity"]) for r in rows],
                     index=pd.DatetimeIndex([pd.Timestamp(r["date"]) for r in rows]), dtype=float)


def _ret(now: float | None, then: str | None) -> float | None:
    if now is None or not then:
        return None
    base = float(then)
    return now / base - 1 if base > 0 else None


def performance(rows: Sequence[dict[str, str]], today: date, equity: float,
                benchmark_price: float | None) -> dict[str, Any]:
    """Return since the previous logged run and since the first logged run (rows dated before today)."""
    past = [r for r in rows if r["date"] < today.isoformat()]
    out: dict[str, Any] = {"since_last_run": None, "since_start": None}
    for key, row in (("since_last_run", past[-1] if past else None), ("since_start", past[0] if past else None)):
        if row is not None:
            out[key] = {"from": row["date"], "return": _ret(equity, row["equity"]),
                        "benchmark_return": _ret(benchmark_price, row.get("benchmark_price"))}
    return out
