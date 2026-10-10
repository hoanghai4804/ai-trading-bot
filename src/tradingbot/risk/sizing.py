"""Pure entry-pricing and sizing rules, shared by live runs and the backtest.

stop = ref - atr_multiple * ATR, rounded DOWN to the cent (Alpaca rejects sub-cent
stops). Sizing uses the rounded stop, so the 1% risk rule still holds exactly.

qty = floor(min(risk_pct * equity / (ref - stop), max_position_pct * equity / ref, cash / ref))
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

# Guards float noise such as 0.01 * 100000 / 4 = 249.99999999999997.
_EPS = 1e-9


def compute_stop(ref_price: float, atr_value: float, atr_multiple: float) -> float:
    raw = float(ref_price - atr_multiple * atr_value)  # float(): repr(np.float64) is not a number
    return float(Decimal(repr(raw)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def gap_pct(ref_price: float, last_close: float) -> float:
    """Signed move of the reference price vs the last close used for the signal."""
    return ref_price / last_close - 1


@dataclass(frozen=True)
class PriceCheck:
    ok: bool
    code: str = ""
    detail: str = ""


def check_entry_price(ref_price: float, last_close: float, stop_price: float, max_gap_pct: float) -> PriceCheck:
    """Reject entries whose price moved too far since the signal close, or whose stop makes no sense."""
    gap = gap_pct(ref_price, last_close)
    if abs(gap) > max_gap_pct + _EPS:
        return PriceCheck(False, "gap_too_large",
                          f"ref {ref_price:.2f} vs last close {last_close:.2f} = {gap:+.2%} (max +/-{max_gap_pct:.0%})")
    if stop_price <= 0 or stop_price >= ref_price:
        return PriceCheck(False, "invalid_stop", f"stop {stop_price:.2f} not in (0, ref {ref_price:.2f})")
    return PriceCheck(True)


@dataclass(frozen=True)
class PositionSize:
    qty: int
    limited_by: str  # "risk" | "position_cap" | "cash"
    risk_qty: int
    cap_qty: int
    cash_qty: int


def _floor(x: float) -> int:
    return max(math.floor(x + _EPS), 0)


def position_size(
    equity: float,
    ref_price: float,
    stop_price: float,
    cash_available: float,
    risk_per_trade_pct: float,
    max_position_pct: float,
) -> PositionSize:
    """Whole shares; the binding limit is reported (ties prefer risk, then position cap)."""
    if equity <= 0 or ref_price <= 0 or stop_price >= ref_price:
        raise ValueError("need equity > 0, ref > 0 and stop < ref")
    risk_qty = _floor(risk_per_trade_pct * equity / (ref_price - stop_price))
    cap_qty = _floor(max_position_pct * equity / ref_price)
    cash_qty = _floor(max(cash_available, 0.0) / ref_price)
    qty, limited_by = min((risk_qty, "risk"), (cap_qty, "position_cap"), (cash_qty, "cash"),
                          key=lambda t: t[0])
    return PositionSize(qty=qty, limited_by=limited_by, risk_qty=risk_qty, cap_qty=cap_qty, cash_qty=cash_qty)
