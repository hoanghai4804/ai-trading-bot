"""Load and validate config/config.yaml.

Every tunable parameter lives in that single file. Models are strict
(unknown keys rejected) and frozen, so a typo fails loudly at startup
instead of silently falling back to a default.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

Weekday = Literal["MON", "TUE", "WED", "THU", "FRI"]
Fraction = Field(gt=0, lt=1)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyConfig(_Strict):
    name: str
    params: dict[str, Any] = {}


class CorrelationConfig(_Strict):
    lookback_days: int = Field(ge=20)
    max_corr: float = Field(gt=0, le=1)


class CircuitBreakerConfig(_Strict):
    weekly_loss_pct: float = Fraction
    max_drawdown_pct: float = Fraction


class RiskConfig(_Strict):
    max_position_pct: float = Fraction
    risk_per_trade_pct: float = Fraction
    max_new_positions_per_week: int = Field(ge=0)
    atr_period: int = Field(ge=2)
    atr_stop_multiple: float = Field(gt=0)
    max_gap_pct: float = Fraction
    cash_buffer_pct: float = Field(ge=0, lt=1)
    correlation: CorrelationConfig
    circuit_breakers: CircuitBreakerConfig

    @model_validator(mode="after")
    def _risk_below_position_cap(self) -> RiskConfig:
        if self.risk_per_trade_pct > self.max_position_pct:
            raise ValueError("risk_per_trade_pct must not exceed max_position_pct")
        return self


class ExecutionConfig(_Strict):
    order_id_prefix: str = Field(pattern=r"^[a-z]{2,8}$")
    sell_fill_timeout_sec: int = Field(ge=1)
    buy_fill_timeout_sec: int = Field(ge=0)
    cancel_timeout_sec: int = Field(ge=1)
    poll_interval_sec: float = Field(gt=0)
    run_lock_ttl_min: int = Field(ge=1)
    test_order_symbol: str
    test_order_stop_pct: float = Fraction


class DataConfig(_Strict):
    feed: Literal["sip", "iex"]
    fallback_feed: Literal["sip", "iex"]
    latest_price_feed: Literal["sip", "iex"]
    history_days: int = Field(ge=120)
    cache_dir: str


class CostsConfig(_Strict):
    slippage_bps: float = Field(ge=0)
    sec_fee_rate: float = Field(ge=0)
    taf_fee_per_share: float = Field(ge=0)
    taf_fee_max: float = Field(ge=0)


class BacktestConfig(_Strict):
    start: date
    initial_capital: float = Field(gt=0)
    train_years: int = Field(ge=1)
    test_months: int = Field(ge=1)
    fast_grid: list[int] = Field(min_length=1)
    slow_grid: list[int] = Field(min_length=1)
    random_seeds: int = Field(ge=1)

    @model_validator(mode="after")
    def _grid_has_valid_pair(self) -> BacktestConfig:
        if not any(f < s for f in self.fast_grid for s in self.slow_grid):
            raise ValueError("fast_grid/slow_grid must contain at least one pair with fast < slow")
        return self


class ScheduledRun(_Strict):
    weekday: Weekday
    report: Literal["full", "brief"]


class ScheduleConfig(_Strict):
    timezone: str
    runs: list[ScheduledRun] = Field(min_length=1)

    @field_validator("runs")
    @classmethod
    def _unique_weekdays(cls, runs: list[ScheduledRun]) -> list[ScheduledRun]:
        days = [r.weekday for r in runs]
        if len(days) != len(set(days)):
            raise ValueError("schedule.runs has duplicate weekdays")
        if not any(r.report == "full" for r in runs):
            raise ValueError("schedule.runs needs at least one 'full' report run")
        return runs


class NotifyConfig(_Strict):
    discord_enabled: bool
    timeout_sec: float = Field(gt=0)
    max_retries: int = Field(ge=0)


class MemoryConfig(_Strict):
    keep_weeks_detailed: int = Field(ge=1)


class Config(_Strict):
    mode: Literal["paper"]  # anything else (e.g. "live") fails validation
    watchlist: list[str] = Field(min_length=1)
    benchmark_symbol: str
    strategy: StrategyConfig
    risk: RiskConfig
    execution: ExecutionConfig
    data: DataConfig
    costs: CostsConfig
    backtest: BacktestConfig
    schedule: ScheduleConfig
    notify: NotifyConfig
    memory: MemoryConfig

    @field_validator("watchlist")
    @classmethod
    def _normalize_watchlist(cls, symbols: list[str]) -> list[str]:
        normalized = [s.strip().upper() for s in symbols]
        if len(normalized) != len(set(normalized)):
            raise ValueError("watchlist has duplicate symbols")
        return normalized

    @field_validator("benchmark_symbol")
    @classmethod
    def _normalize_benchmark(cls, symbol: str) -> str:
        return symbol.strip().upper()


def load_config(path: str | Path | None = None) -> Config:
    """Read and validate the YAML config. Raises on any invalid or unknown key."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with config_path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
