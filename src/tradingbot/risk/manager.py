"""Independent risk manager: turns strategy proposals into approved or blocked orders.

It never talks to the broker; callers pass a snapshot of the account, the bars,
reference prices and the circuit-breaker status, so live runs and the backtest
share exactly the same rules. Every blocked proposal carries a reason code.

Exits (risk-reducing) are always approved when the symbol is held, even under a
breaker. With the drawdown breaker tripped, every held position is sold.

Buy checks, in order (the first failure is the reported reason):
 1. drawdown_halt / weekly_loss_breaker
 2. not_in_watchlist
 3. already_held / duplicate
 4. weekly_limit       new positions this calendar week (from order history) + approved now
 5. no_reference_price                  missing, invalid or stale latest trade (`ref_notes` says why)
 6. no_data / atr_unavailable
 7. no_raw_close / gap_too_large / invalid_stop
                                         live runs compare the raw trade with the RAW close of the
                                         last session (`raw_closes`); the backtest passes none and
                                         uses the bars' close
 8. correlation / correlation_unknown   vs held positions not being sold now, and buys
                                         approved earlier in this run
 9. insufficient_cash / size_zero
Cash for buys = cash * (1 - cash_buffer_pct) - notional already approved. Proceeds of
sells in the same run are NOT counted (fills are not instant; no leverage ever).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd

from tradingbot.config import RiskConfig
from tradingbot.data.market_data import cut_as_of
from tradingbot.indicators.technical import atr, return_correlation
from tradingbot.risk.circuit_breakers import BreakerStatus
from tradingbot.risk.sizing import check_entry_price, compute_stop, position_size
from tradingbot.strategies.base import Signal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class AccountState:
    equity: float
    cash: float
    positions: Mapping[str, float]   # symbol -> qty held (long only)
    new_positions_this_week: int     # bot BUY orders filled or pending in the current calendar week

    def __post_init__(self) -> None:
        if self.equity <= 0:
            raise ValueError("equity must be > 0")
        if self.new_positions_this_week < 0:
            raise ValueError("new_positions_this_week must be >= 0")
        shorts = sorted(s for s, q in self.positions.items() if q < 0)
        if shorts:
            raise ValueError(f"short position(s) found in a long-only account: {', '.join(shorts)}")

    @property
    def held(self) -> set[str]:
        return {s for s, q in self.positions.items() if q > 0}


@dataclass(frozen=True)
class ApprovedOrder:
    symbol: str
    side: Side
    qty: float
    reason: str
    ref_price: float | None = None
    stop_price: float | None = None
    notional: float | None = None
    risk_amount: float | None = None   # loss if the stop fills exactly at stop_price
    limited_by: str | None = None      # which sizing limit bound the quantity


@dataclass(frozen=True)
class BlockedOrder:
    symbol: str
    side: Side
    code: str
    detail: str


@dataclass(frozen=True)
class RiskDecision:
    as_of: date
    breakers: BreakerStatus
    approved: list[ApprovedOrder] = field(default_factory=list)
    blocked: list[BlockedOrder] = field(default_factory=list)

    @property
    def halt(self) -> bool:
        return self.breakers.halt

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "halt": self.halt,
            "breakers": self.breakers.to_dict(),
            "approved": [asdict(o) for o in self.approved],
            "blocked": [asdict(o) for o in self.blocked],
        }


class RiskManager:
    def __init__(self, risk: RiskConfig, watchlist: Sequence[str]) -> None:
        self.cfg = risk
        self.watchlist = set(watchlist)

    def review(
        self,
        signals: Iterable[Signal],
        as_of: date,
        account: AccountState,
        bars: Mapping[str, pd.DataFrame],
        ref_prices: Mapping[str, float],
        breakers: BreakerStatus,
        raw_closes: Mapping[str, tuple[date, float]] | None = None,
        ref_notes: Mapping[str, str] | None = None,
    ) -> RiskDecision:
        """Apply every rule; signals are processed in the order given (strategy priority).

        raw_closes: {symbol: (session date, unadjusted close)} for the gap check (live runs).
        ref_notes: why a symbol has no reference price (e.g. stale trade), for the veto detail.
        """
        signals = list(signals)
        decision = RiskDecision(as_of=as_of, breakers=breakers)
        self._review_sells(signals, account, ref_prices, decision)
        selling = {o.symbol for o in decision.approved}
        for sig in (s for s in signals if s.action == "buy"):
            blocked = self._review_buy(sig, as_of, account, bars, ref_prices, breakers, selling, decision,
                                       raw_closes, ref_notes or {})
            if blocked is not None:
                decision.blocked.append(blocked)
        return decision

    # --- sells -------------------------------------------------------------------

    def _review_sells(
        self, signals: list[Signal], account: AccountState, ref_prices: Mapping[str, float], decision: RiskDecision
    ) -> None:
        reasons: dict[str, str] = {}
        for sig in (s for s in signals if s.action == "exit"):
            if sig.symbol in reasons:
                decision.blocked.append(BlockedOrder(sig.symbol, "sell", "duplicate", "exit already approved"))
            elif sig.symbol not in account.held:
                decision.blocked.append(BlockedOrder(sig.symbol, "sell", "not_held",
                                                     "nothing to sell (long-only, no short)"))
            else:
                reasons[sig.symbol] = sig.reason
        if decision.breakers.halt:
            for symbol in account.held:
                reasons.setdefault(symbol, "drawdown breaker: liquidate")
        for symbol in sorted(reasons):
            decision.approved.append(ApprovedOrder(symbol=symbol, side="sell", qty=account.positions[symbol],
                                                   reason=reasons[symbol], ref_price=ref_prices.get(symbol)))

    # --- buys --------------------------------------------------------------------

    def _review_buy(
        self,
        sig: Signal,
        as_of: date,
        account: AccountState,
        bars: Mapping[str, pd.DataFrame],
        ref_prices: Mapping[str, float],
        breakers: BreakerStatus,
        selling: set[str],
        decision: RiskDecision,
        raw_closes: Mapping[str, tuple[date, float]] | None,
        ref_notes: Mapping[str, str],
    ) -> BlockedOrder | None:
        cfg = self.cfg
        symbol = sig.symbol

        def block(code: str, detail: str) -> BlockedOrder:
            return BlockedOrder(symbol, "buy", code, detail)

        approved_buys = [o for o in decision.approved if o.side == "buy"]
        if breakers.halt:
            return block("drawdown_halt", "; ".join(breakers.reasons))
        if breakers.block_new_entries:
            return block("weekly_loss_breaker", "; ".join(breakers.reasons))
        if symbol not in self.watchlist:
            return block("not_in_watchlist", f"{symbol} is not in config.watchlist")
        if symbol in account.held:
            return block("already_held", f"position of {account.positions[symbol]:g} shares exists")
        if any(o.symbol == symbol for o in approved_buys):
            return block("duplicate", "buy already approved in this run")
        used = account.new_positions_this_week + len(approved_buys)
        if used >= cfg.max_new_positions_per_week:
            return block("weekly_limit", f"{used} new position(s) this calendar week "
                                         f"(max {cfg.max_new_positions_per_week})")

        ref = ref_prices.get(symbol)
        if ref is None or not np.isfinite(ref) or ref <= 0:
            return block("no_reference_price", ref_notes.get(symbol, "no valid latest price"))
        frame = cut_as_of(bars[symbol], as_of) if symbol in bars else None
        if frame is None or frame.empty:
            return block("no_data", f"no bars up to {as_of}")
        atr_value = float(atr(frame, cfg.atr_period).iloc[-1])
        if not np.isfinite(atr_value) or atr_value <= 0:
            return block("atr_unavailable", f"ATR({cfg.atr_period}) not available at {as_of}")
        last_close = float(frame["close"].iloc[-1])
        if raw_closes is not None:
            last_session = frame.index[-1].date()
            raw = raw_closes.get(symbol)
            if raw is None or raw[0] != last_session or not np.isfinite(raw[1]) or raw[1] <= 0:
                return block("no_raw_close", f"no unadjusted close for the last session {last_session} "
                                             f"(got {raw})")
            last_close = raw[1]
        stop = compute_stop(ref, atr_value, cfg.atr_stop_multiple)
        check = check_entry_price(ref, last_close, stop, cfg.max_gap_pct)
        if not check.ok:
            return block(check.code, check.detail)

        partners = sorted((account.held - selling) | {o.symbol for o in approved_buys})
        corr_block = self._check_correlation(symbol, partners, as_of, bars)
        if corr_block is not None:
            return block(*corr_block)

        cash_available = account.cash * (1 - cfg.cash_buffer_pct) - sum(o.notional or 0 for o in approved_buys)
        size = position_size(account.equity, ref, stop, cash_available, cfg.risk_per_trade_pct, cfg.max_position_pct)
        if size.qty < 1:
            if size.limited_by == "cash":
                return block("insufficient_cash", f"cash available {cash_available:,.2f} < 1 share at {ref:.2f}")
            return block("size_zero", f"size rounds to 0 shares (limited by {size.limited_by})")

        decision.approved.append(ApprovedOrder(
            symbol=symbol, side="buy", qty=size.qty, reason=sig.reason, ref_price=ref, stop_price=stop,
            notional=size.qty * ref, risk_amount=size.qty * (ref - stop), limited_by=size.limited_by,
        ))
        return None

    def _check_correlation(
        self, symbol: str, partners: list[str], as_of: date, bars: Mapping[str, pd.DataFrame]
    ) -> tuple[str, str] | None:
        """Block if the 60-day return correlation with any partner is above max, or unknown."""
        cc = self.cfg.correlation
        for other in partners:
            if other not in bars:
                return "correlation_unknown", f"no bars for {other} to compare with"
            closes = pd.DataFrame({
                symbol: cut_as_of(bars[symbol], as_of)["close"],
                other: cut_as_of(bars[other], as_of)["close"],
            })
            value = return_correlation(closes, cc.lookback_days).loc[symbol, other]
            if not np.isfinite(value):
                return "correlation_unknown", f"fewer than {cc.lookback_days} overlapping returns with {other}"
            if value > cc.max_corr:
                return "correlation", f"{cc.lookback_days}-day return corr with {other} = {value:.2f} > {cc.max_corr}"
        return None
