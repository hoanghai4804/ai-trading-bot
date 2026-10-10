"""Execution quality: fill price vs reference price of every bot fill -> state/execution_quality.csv.

Reference price per order:
- market buy / market sell: the reference price the run used (latest trade at decision
  time), kept in runs/<date>/decisions.json under `order_refs` (client_order_id -> price);
- stop sell (OTO leg or re-placed stop): its stop price (slippage = how far below the stop it filled).
Orders without a known reference (manual orders, test-order) are left out.

Slippage in bps, positive = cost: buy (fill / ref - 1) x 1e4, sell (1 - fill / ref) x 1e4.
The backtest assumes `costs.slippage_bps` (5) per side for market fills; stop fills are
reported separately (the backtest fills them at the stop, or at the open on a gap).
The CSV is append-only and keyed by Alpaca order id, so overlapping windows never duplicate rows.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from tradingbot.broker.alpaca_client import OrderSnapshot
from tradingbot.execution.orders import bot_orders_with_legs, is_stop_sell, ny_date
from tradingbot.safety import STATE_DIR

QUALITY_FILE = "execution_quality.csv"


@dataclass(frozen=True)
class FillRecord:
    fill_date: str
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    type: str
    qty: float
    ref_price: float
    ref_source: str        # decision | stop
    fill_price: float
    slippage_bps: float


COLUMNS = [f.name for f in fields(FillRecord)]


def slippage_bps(side: str, ref: float, fill: float) -> float:
    raw = (fill / ref - 1) if side == "buy" else (1 - fill / ref)
    return round(raw * 10_000, 2)


def collect_fills(orders: Iterable[OrderSnapshot], prefix: str, since: datetime | None,
                  refs: Mapping[str, float]) -> list[FillRecord]:
    """Bot fills (orders and their legs) with filled_at > since and a known reference price."""
    out = []
    for o in bot_orders_with_legs(orders, prefix):
        if o.filled_qty <= 0 or not o.filled_avg_price or o.filled_at is None:
            continue
        if since is not None and o.filled_at <= since:
            continue
        if is_stop_sell(o) and o.stop_price:
            ref, source = float(o.stop_price), "stop"
        elif o.client_order_id in refs:
            ref, source = float(refs[o.client_order_id]), "decision"
        else:
            continue
        out.append(FillRecord(str(ny_date(o.filled_at)), o.id, o.client_order_id, o.symbol, o.side, o.type,
                              float(o.filled_qty), ref, source, float(o.filled_avg_price),
                              slippage_bps(o.side, ref, float(o.filled_avg_price))))
    return sorted(out, key=lambda r: (r.fill_date, r.symbol, r.order_id))


def load_quality(state_dir: Path = STATE_DIR) -> list[dict[str, str]]:
    path = state_dir / QUALITY_FILE
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_fills(records: Iterable[FillRecord], state_dir: Path = STATE_DIR) -> list[FillRecord]:
    """Append fills not recorded yet (by order id). Returns the rows actually added."""
    path = state_dir / QUALITY_FILE
    known = {r["order_id"] for r in load_quality(state_dir)}
    new = [r for r in records if r.order_id not in known]
    if not new:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        if write_header:
            writer.writeheader()
        for r in new:
            writer.writerow(asdict(r))
    return new


def slippage_stats(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """{'market': {'n', 'mean_bps'}, 'stop': {...}} over CSV rows or FillRecord dicts."""
    groups: dict[str, list[float]] = {"market": [], "stop": []}
    for r in rows:
        groups["stop" if r["ref_source"] == "stop" else "market"].append(float(r["slippage_bps"]))
    return {k: {"n": len(v), "mean_bps": (sum(v) / len(v)) if v else float("nan")} for k, v in groups.items()}


def load_order_refs(runs_dir: Path) -> dict[str, float]:
    """client_order_id -> reference price, from every runs/<date>/decisions.json (EXECUTE runs)."""
    refs: dict[str, float] = {}
    if not runs_dir.exists():
        return refs
    for path in sorted(runs_dir.glob("*/decisions.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("mode") == "EXECUTE":
            refs.update({k: float(v) for k, v in (data.get("order_refs") or {}).items() if v})
    return refs
