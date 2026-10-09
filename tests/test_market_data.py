from datetime import date, datetime, timezone

import pandas as pd
import pytest

from tradingbot.config import load_config
from tradingbot.data.market_data import (
    FeedPermissionError,
    MarketData,
    cut_as_of,
    frame_from_alpaca,
    last_completed_day,
)

DATA_CFG = load_config().data


def make_frame(start: str, end: str) -> pd.DataFrame:
    idx = pd.bdate_range(start, end, name="date")
    close = pd.Series(range(len(idx)), index=idx, dtype=float) + 100
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1e6})


class FakeSource:
    def __init__(self, deny_feeds=()):
        self.calls = []
        self.deny_feeds = set(deny_feeds)

    def fetch(self, symbols, start, end, feed):
        self.calls.append((tuple(symbols), start, end, feed))
        if feed in self.deny_feeds:
            raise FeedPermissionError(feed)
        # Deliberately returns one bar AFTER `end` to prove get_bars cuts it.
        return {s: make_frame(start.isoformat(), (end + pd.Timedelta(days=3)).isoformat()) for s in symbols}


@pytest.fixture
def make_md(tmp_path):
    def _make(source, today=date(2026, 10, 12)):
        return MarketData(source=source, config=DATA_CFG, cache_root=tmp_path, today=lambda: today)
    return _make


def test_get_bars_never_returns_rows_after_as_of(make_md):
    md = make_md(FakeSource())
    bars = md.get_bars(["AAPL", "SPY"], start=date(2026, 1, 1), as_of=date(2026, 10, 9))
    assert set(bars) == {"AAPL", "SPY"}
    for frame in bars.values():
        assert frame.index.max() == pd.Timestamp("2026-10-09")
        assert frame.index.min() >= pd.Timestamp("2026-01-01")


def test_as_of_today_or_later_is_rejected(make_md):
    md = make_md(FakeSource(), today=date(2026, 10, 12))
    with pytest.raises(ValueError, match="completed day"):
        md.get_bars(["AAPL"], start=date(2026, 1, 1), as_of=date(2026, 10, 12))


def test_default_as_of_is_yesterday(make_md):
    md = make_md(FakeSource(), today=date(2026, 10, 12))
    bars = md.get_bars(["AAPL"], start=date(2026, 9, 1))
    assert bars["AAPL"].index.max() <= pd.Timestamp("2026-10-11")


def test_cache_is_reused_same_day_and_refetched_next_day(make_md):
    source = FakeSource()
    make_md(source).get_bars(["AAPL"], start=date(2026, 1, 1), as_of=date(2026, 10, 9))
    make_md(source).get_bars(["AAPL"], start=date(2026, 3, 1), as_of=date(2026, 10, 8))  # subset -> cache hit
    assert len(source.calls) == 1
    make_md(source).get_bars(["AAPL"], start=date(2025, 1, 1), as_of=date(2026, 10, 9))  # wider -> miss
    assert len(source.calls) == 2
    # Next day: adjusted history may have been rescaled by a dividend -> full refetch.
    make_md(source, today=date(2026, 10, 13)).get_bars(["AAPL"], start=date(2025, 1, 1), as_of=date(2026, 10, 9))
    assert len(source.calls) == 3


def test_corrupt_cache_is_a_miss(make_md, tmp_path):
    source = FakeSource()
    make_md(source).get_bars(["AAPL"], start=date(2026, 1, 1), as_of=date(2026, 10, 9))
    (tmp_path / DATA_CFG.cache_dir / "AAPL.parquet").write_bytes(b"garbage")
    make_md(source).get_bars(["AAPL"], start=date(2026, 1, 1), as_of=date(2026, 10, 9))
    assert len(source.calls) == 2


def test_falls_back_to_iex_when_sip_not_permitted(make_md):
    source = FakeSource(deny_feeds={"sip"})
    make_md(source).get_bars(["AAPL"], start=date(2026, 1, 1), as_of=date(2026, 10, 9))
    assert [c[3] for c in source.calls] == ["sip", "iex"]


def test_missing_symbol_raises(make_md):
    class EmptySource(FakeSource):
        def fetch(self, symbols, start, end, feed):
            return {}
    with pytest.raises(ValueError, match="no bars"):
        make_md(EmptySource()).get_bars(["ZZZZ"], start=date(2026, 1, 1), as_of=date(2026, 10, 9))


def test_frame_from_alpaca_uses_new_york_dates_across_dst():
    # 05:00 UTC in winter and 04:00 UTC in summer are both midnight New York.
    idx = pd.MultiIndex.from_tuples(
        [("AAPL", pd.Timestamp("2026-01-05 05:00", tz="UTC")), ("AAPL", pd.Timestamp("2026-07-06 04:00", tz="UTC"))],
        names=["symbol", "timestamp"],
    )
    raw = pd.DataFrame({"open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2], "volume": [1, 2],
                        "trade_count": [1, 1], "vwap": [1, 2]}, index=idx)
    frame = frame_from_alpaca(raw)["AAPL"]
    assert list(frame.index) == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-07-06")]
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]


def test_cut_as_of_and_last_completed_day():
    frame = make_frame("2026-10-05", "2026-10-09")
    assert cut_as_of(frame, date(2026, 10, 7)).index.max() == pd.Timestamp("2026-10-07")
    # 02:00 UTC on Oct 13 is still Oct 12 in New York -> last completed day is Oct 11.
    assert last_completed_day(datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)) == date(2026, 10, 11)


def test_cli_data_command_prints_every_symbol(monkeypatch, tmp_path, capsys):
    from tradingbot import cli

    monkeypatch.setattr(cli, "default_alpaca_source", lambda: FakeSource())
    monkeypatch.setattr(
        cli, "MarketData",
        lambda source, config: MarketData(source, config, cache_root=tmp_path, today=lambda: date(2026, 10, 12)),
    )
    assert cli.main(["data", "--as-of", "2026-10-09"]) == 0
    out = capsys.readouterr().out
    assert all(s in out for s in load_config().watchlist)
    assert "2026-10-12" not in out  # FakeSource returns bars after as_of; they must be cut
