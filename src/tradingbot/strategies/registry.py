"""Map `strategy.name` in config to a Strategy class."""

from __future__ import annotations

from tradingbot.config import StrategyConfig
from tradingbot.strategies.base import Strategy
from tradingbot.strategies.ma_crossover import MACrossover

STRATEGIES: dict[str, type[Strategy]] = {
    MACrossover.name: MACrossover,
}


def build_strategy(cfg: StrategyConfig) -> Strategy:
    """Instantiate the configured strategy; raises on unknown name or invalid params."""
    try:
        cls = STRATEGIES[cfg.name]
    except KeyError:
        raise ValueError(f"unknown strategy '{cfg.name}' (known: {', '.join(sorted(STRATEGIES))})") from None
    return cls(cfg.params)  # type: ignore[call-arg]
