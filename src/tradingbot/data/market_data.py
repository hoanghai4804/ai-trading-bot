"""Daily OHLCV bars: fetch from Alpaca, cache locally, cut at `as_of`.

Look-ahead rules enforced here:
- Bars are indexed by the New York trading date (Alpaca daily timestamps are
  midnight New York time, i.e. 04:00 or 05:00 UTC).
- `get_bars(..., as_of=d)` never returns a row dated after `d`.
- The default `as_of` is the last session strictly before today (New York),
  so a partially formed bar for today can never leak into a decision.

Cache rule: prices are split/dividend *adjusted*, and every new dividend
rescales the whole history. Appending new bars to an old cache would mix two
adjustment bases, so a cache file is only reused on the day it was fetched;
otherwise the full range is fetched again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from tradingbot.config import PROJECT_ROOT, DataConfig

log = logging.getLogger(__name__)

EXCHANGE_TZ = ZoneInfo("America/New_York")
OHLCV = ["open", "high", "low", "close", "volume"]
# Free data plans may not query SIP data newer than 15 minutes.
_RECENT_DATA_MARGIN = timedelta(minutes=16)


class BarsSource(Protocol):
    def fetch(self, symbols: list[str], start: date, end: date, feed: str) -> dict[str, pd.DataFrame]:
        """Return {symbol: OHLCV frame indexed by New York date}, inclusive of start..end."""


class FeedPermissionError(RuntimeError):
    """The account's data plan does not allow the requested feed."""


def today_new_york(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(EXCHANGE_TZ).date()


def last_completed_day(now: datetime | None = None) -> date:
    """Latest calendar day strictly before today in New York (weekends/holidays simply have no bar)."""
    return today_new_york(now) - timedelta(days=1)


def cut_as_of(bars: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Drop every row dated after `as_of`. The only way data should be sliced for decisions."""
    return bars.loc[bars.index <= pd.Timestamp(as_of)]


def frame_from_alpaca(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Convert alpaca-py's (symbol, timestamp) MultiIndex frame into per-symbol frames by NY date."""
    out: dict[str, pd.DataFrame] = {}
    if df.empty:
        return out
    for symbol, group in df.groupby(level="symbol"):
        g = group.droplevel("symbol")
        ny_dates = pd.DatetimeIndex(g.index).tz_convert(EXCHANGE_TZ).tz_localize(None).normalize()
        frame = g[OHLCV].astype(float).set_axis(ny_dates)
        frame.index.name = "date"
        out[str(symbol)] = frame[~frame.index.duplicated(keep="last")].sort_index()
    return out


class AlpacaBarsSource:
    """Real data source. The client is created lazily so tests never need credentials."""

    def __init__(self, client_factory: Callable[[], object]) -> None:
        self._client_factory = client_factory
        self._client: object | None = None

    def fetch(self, symbols: list[str], start: date, end: date, feed: str) -> dict[str, pd.DataFrame]:
        from alpaca.common.exceptions import APIError
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        if self._client is None:
            self._client = self._client_factory()
        start_dt = datetime.combine(start, time.min, EXCHANGE_TZ)
        end_dt = datetime.combine(end + timedelta(days=1), time.min, EXCHANGE_TZ)
        end_dt = min(end_dt, datetime.now(timezone.utc) - _RECENT_DATA_MARGIN)
        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
            adjustment=Adjustment.ALL,
            feed=DataFeed(feed),
        )
        try:
            bars = self._client.get_stock_bars(request)  # type: ignore[attr-defined]
        except APIError as exc:
            if getattr(exc, "status_code", None) == 403 or "subscription" in str(exc).lower():
                raise FeedPermissionError(f"feed '{feed}' not permitted: {exc}") from exc
            raise
        return frame_from_alpaca(bars.df)


def _data_client_factory() -> object:
    from alpaca.data.historical import StockHistoricalDataClient

    from tradingbot.credentials import get_alpaca_credentials

    creds = get_alpaca_credentials()
    return StockHistoricalDataClient(creds.api_key, creds.secret_key)


def default_alpaca_source() -> AlpacaBarsSource:
    return AlpacaBarsSource(_data_client_factory)


class LatestPrices:
    """Reference prices = latest trade per symbol (taken at order time, never used for signals)."""

    def __init__(self, feed: str, client_factory: Callable[[], object] = _data_client_factory) -> None:
        self.feed = feed
        self._client_factory = client_factory
        self._client: object | None = None

    def get(self, symbols: Iterable[str]) -> dict[str, float]:
        """{symbol: price}; symbols without a valid trade are left out (callers treat them as unknown)."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        symbols = sorted(set(symbols))
        if not symbols:
            return {}
        if self._client is None:
            self._client = self._client_factory()
        trades = self._client.get_stock_latest_trade(  # type: ignore[attr-defined]
            StockLatestTradeRequest(symbol_or_symbols=symbols, feed=DataFeed(self.feed)))
        return {s: float(t.price) for s, t in trades.items() if t is not None and t.price and t.price > 0}


@dataclass
class MarketData:
    """Cached access to adjusted daily bars."""

    source: BarsSource
    config: DataConfig
    cache_root: Path = PROJECT_ROOT
    today: Callable[[], date] = today_new_york

    @property
    def cache_dir(self) -> Path:
        return self.cache_root / self.config.cache_dir

    def _cache_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol}.parquet"

    def _read_cache(self, symbol: str, start: date, end: date) -> pd.DataFrame | None:
        path = self._cache_path(symbol)
        if not path.exists():
            return None
        try:
            frame = pd.read_parquet(path)
        except Exception:  # corrupt cache is just a cache miss
            log.warning("unreadable cache for %s, refetching", symbol)
            return None
        meta = frame.attrs
        if meta.get("fetched_on") != self.today().isoformat():
            return None
        if date.fromisoformat(meta["start"]) > start or date.fromisoformat(meta["end"]) < end:
            return None
        return frame

    def _write_cache(self, symbol: str, frame: pd.DataFrame, start: date, end: date, feed: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        frame = frame.copy()
        frame.attrs = {"fetched_on": self.today().isoformat(), "start": start.isoformat(),
                       "end": end.isoformat(), "feed": feed}
        frame.to_parquet(self._cache_path(symbol))

    def _fetch(self, symbols: list[str], start: date, end: date) -> tuple[dict[str, pd.DataFrame], str]:
        try:
            return self.source.fetch(symbols, start, end, self.config.feed), self.config.feed
        except FeedPermissionError:
            fallback = self.config.fallback_feed
            log.warning("feed %s not permitted, falling back to %s", self.config.feed, fallback)
            return self.source.fetch(symbols, start, end, fallback), fallback

    def get_bars(
        self, symbols: Iterable[str], start: date, as_of: date | None = None
    ) -> dict[str, pd.DataFrame]:
        """Adjusted daily bars for each symbol from `start` to `as_of` (inclusive, never later)."""
        as_of = as_of or self.today() - timedelta(days=1)
        if as_of >= self.today():
            raise ValueError("as_of must be a completed day (before today in New York)")
        symbols = list(dict.fromkeys(symbols))
        result: dict[str, pd.DataFrame] = {}
        missing = []
        for symbol in symbols:
            cached = self._read_cache(symbol, start, as_of)
            if cached is None:
                missing.append(symbol)
            else:
                result[symbol] = cached
        if missing:
            fetched, feed = self._fetch(missing, start, as_of)
            for symbol in missing:
                frame = fetched.get(symbol)
                if frame is None or frame.empty:
                    raise ValueError(f"no bars returned for {symbol}")
                self._write_cache(symbol, frame, start, as_of, feed)
                result[symbol] = frame
        return {s: cut_as_of(result[s], as_of).loc[pd.Timestamp(start):] for s in symbols}
