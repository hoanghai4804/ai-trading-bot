"""News sentiment log: strict schema, idempotent upsert, reference close, never used for trading."""

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from tradingbot.config import PROJECT_ROOT, load_config
from tradingbot.sentiment.record import SentimentError, dumps_example, load_log, parse_sentiment, record_sentiment

CFG = load_config()
DAY = date(2026, 10, 12)


def valid() -> dict:
    data = json.loads(dumps_example(CFG.watchlist, DAY))
    data["web_searches"] = 3
    data["items"][0] = {"symbol": "AAPL", "score": 0.4, "confidence": 0.7, "reason": "Strong iPhone demand, nhu cầu tốt",
                        "no_news": False, "n_sources": 2,
                        "sources": ["https://example.com/a", "https://news.example.org/b?x=1"]}
    return data


def raw_closes(symbols, as_of):
    assert as_of == date(2026, 10, 11)
    return {s: (date(2026, 10, 9), 100.0 + i) for i, s in enumerate(symbols)}


def test_valid_file_is_accepted():
    data = parse_sentiment(json.dumps(valid()), CFG.watchlist)
    assert len(data.items) == len(CFG.watchlist) and data.items[0].score == 0.4


def _mutate(path, value):
    data = valid()
    target = data
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return data


@pytest.mark.parametrize("path,value", [
    (("items", 0, "score"), 1.5),
    (("items", 0, "score"), -1.01),
    (("items", 0, "score"), "0.4"),             # strings are not numbers
    (("items", 0, "score"), True),              # bools are not numbers
    (("items", 0, "confidence"), 1.2),
    (("items", 0, "confidence"), -0.1),
    (("items", 0, "reason"), ""),
    (("items", 0, "reason"), "two\nlines"),
    (("items", 0, "reason"), "x" * 201),
    (("items", 0, "n_sources"), 3),             # != number of URLs
    (("items", 0, "sources"), ["https://example.com/a", "https://example.com/a"]),
    (("items", 0, "sources"), ["not a url", "https://example.com/b"]),
    (("items", 0, "sources"), ["ftp://example.com/a", "https://example.com/b"]),
    (("items", 0, "no_news"), True),            # no_news with a score and sources
    (("items", 1, "score"), 0.2),               # no_news item with a non-zero score
    (("items", 1, "confidence"), 0.5),          # no_news item with confidence
    (("items", 0, "extra"), "x"),               # unknown key
    (("items", 0, "symbol"), KeyError),
    (("web_searches",), 5),                     # more than 4 searches
    (("date",), "12/10/2026"),
    (("items",), []),
])
def test_invalid_files_are_rejected(path, value):
    with pytest.raises(SentimentError):
        parse_sentiment(json.dumps(_mutate(path, value)), CFG.watchlist)


def test_score_without_source_is_rejected():
    data = valid()
    data["items"][0].update({"n_sources": 0, "sources": []})
    with pytest.raises(SentimentError, match="at least one source"):
        parse_sentiment(json.dumps(data), CFG.watchlist)


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate"])
def test_items_must_match_the_watchlist_exactly(change):
    data = valid()
    if change == "missing":
        data["items"].pop()
    elif change == "extra":
        data["items"].append({**data["items"][-1], "symbol": "TSLA"})
    else:
        data["items"][-1] = {**data["items"][-2]}
    with pytest.raises(SentimentError):
        parse_sentiment(json.dumps(data), CFG.watchlist)


def test_not_json_is_rejected():
    with pytest.raises(SentimentError):
        parse_sentiment("{not json", CFG.watchlist)


def test_record_writes_one_row_per_symbol_with_reference_close(tmp_path):
    log = tmp_path / "sentiment_log.csv"
    counts = record_sentiment(parse_sentiment(json.dumps(valid()), CFG.watchlist), raw_closes, log)
    assert counts == {"added": len(CFG.watchlist), "replaced": 0}
    rows = load_log(log)
    aapl = next(r for r in rows if r["symbol"] == "AAPL")
    assert (aapl["score"], aapl["confidence"], aapl["no_news"], aapl["n_sources"]) == ("0.4", "0.7", "false", "2")
    assert aapl["sources"] == "https://example.com/a | https://news.example.org/b?x=1"
    assert aapl["reason"] == "Strong iPhone demand, nhu cầu tốt"       # comma + Vietnamese kept by csv quoting
    assert (aapl["ref_close"], aapl["ref_close_date"]) == ("100.0000", "2026-10-09")
    msft = next(r for r in rows if r["symbol"] == "MSFT")
    assert (msft["no_news"], msft["score"], msft["sources"]) == ("true", "0", "")


def test_recording_twice_does_not_duplicate(tmp_path):
    log = tmp_path / "sentiment_log.csv"
    data = parse_sentiment(json.dumps(valid()), CFG.watchlist)
    record_sentiment(data, raw_closes, log)
    second = record_sentiment(data, raw_closes, log)
    assert second == {"added": 0, "replaced": len(CFG.watchlist)}
    rows = load_log(log)
    assert len(rows) == len(CFG.watchlist)
    assert len({(r["date"], r["symbol"]) for r in rows}) == len(rows)


def test_another_week_appends_and_keeps_history(tmp_path):
    log = tmp_path / "sentiment_log.csv"
    record_sentiment(parse_sentiment(json.dumps(valid()), CFG.watchlist), raw_closes, log)
    later = valid()
    later["date"] = "2026-10-19"
    record_sentiment(parse_sentiment(json.dumps(later), CFG.watchlist),
                     lambda s, a: {x: (date(2026, 10, 16), 1.0) for x in s}, log)
    rows = load_log(log)
    assert len(rows) == 2 * len(CFG.watchlist)
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    with log.open(encoding="utf-8", newline="") as f:
        assert next(csv.reader(f))[:3] == ["date", "symbol", "score"]


def test_sentiment_is_never_used_by_trading_code():
    src = PROJECT_ROOT / "src" / "tradingbot"
    trading = [*src.glob("strategies/*.py"), *src.glob("risk/*.py"), *src.glob("execution/*.py"),
               src / "pipeline.py", *src.glob("backtest/*.py")]
    offenders = [p.name for p in trading if "sentiment" in p.read_text(encoding="utf-8")]
    assert offenders == []
