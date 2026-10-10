"""Original stop price of every bot position, kept in state/stops.json (committed to git).

The stop is set once at entry (ref - 2 x ATR) and NEVER moved. The registry lets a run
re-place exactly that stop when the live one is gone (Alpaca cancels GTC orders after
90 days, or someone canceled it by hand). Entries are added when a buy is sent and
dropped once the position is closed and no buy is pending.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from tradingbot.safety import STATE_DIR

STOPS_FILE = "stops.json"


@dataclass(frozen=True)
class StopEntry:
    stop_price: float
    entry_date: date
    buy_order_id: str   # client_order_id of the entry order


def stops_path(state_dir: Path = STATE_DIR) -> Path:
    return state_dir / STOPS_FILE


def load_stops(state_dir: Path = STATE_DIR) -> dict[str, StopEntry]:
    path = stops_path(state_dir)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))   # a corrupt file must fail loudly, not lose stops
    return {s: StopEntry(float(e["stop_price"]), date.fromisoformat(e["entry_date"]), str(e["buy_order_id"]))
            for s, e in raw.items()}


def save_stops(stops: dict[str, StopEntry], state_dir: Path = STATE_DIR) -> Path:
    path = stops_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {s: {**asdict(e), "entry_date": e.entry_date.isoformat()} for s, e in sorted(stops.items())}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path
