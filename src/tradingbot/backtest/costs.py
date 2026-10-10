"""Backtest trading costs (config `costs`).

- Slippage: `slippage_bps` per side, against us (buy higher, sell lower).
- Sells only: SEC fee = rate x notional, FINRA TAF = per share capped at `taf_fee_max`;
  each rounded UP to the cent, as brokers pass them through.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tradingbot.config import CostsConfig


def _cents_up(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


@dataclass(frozen=True)
class Fill:
    price: float      # per-share fill price after slippage
    fees: float       # regulatory fees (sells only)
    slippage: float   # cost of slippage in money (qty x |fill - base|)

    @property
    def cost(self) -> float:
        return self.fees + self.slippage


class CostModel:
    def __init__(self, cfg: CostsConfig) -> None:
        self.cfg = cfg

    @property
    def slip(self) -> float:
        return self.cfg.slippage_bps / 10_000

    def buy(self, base_price: float, qty: float) -> Fill:
        price = base_price * (1 + self.slip)
        return Fill(price=price, fees=0.0, slippage=qty * (price - base_price))

    def sell(self, base_price: float, qty: float) -> Fill:
        price = base_price * (1 - self.slip)
        return Fill(price=price, fees=self.sell_fees(price, qty), slippage=qty * (base_price - price))

    def sell_fees(self, price: float, qty: float) -> float:
        if qty <= 0:
            return 0.0
        sec = _cents_up(self.cfg.sec_fee_rate * price * qty)
        taf = min(_cents_up(self.cfg.taf_fee_per_share * qty), self.cfg.taf_fee_max)
        return sec + taf
