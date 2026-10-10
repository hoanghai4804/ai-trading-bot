"""News-sentiment log: DATA COLLECTION ONLY, never used by any trading decision.

Purpose: after 1-2 years, test whether weekly news sentiment predicts next week's return.
In a full-report run Claude writes runs/<date>/sentiment.json from the SAME web searches
used for the report (at most 4); `record-sentiment --file` validates it strictly and upserts
it into data/sentiment/sentiment_log.csv (committed), keyed by (date, symbol), with the
RAW close of the last session at recording time. Forward returns are NOT computed here.

Schema (unknown keys rejected, numbers must be JSON numbers):
{
  "date": "2026-10-12",            # run date (New York)
  "web_searches": 3,               # searches used for report + sentiment, 0..4
  "items": [                       # exactly one per watchlist symbol
    {"symbol": "AAPL", "score": 0.3, "confidence": 0.6, "reason": "one line",
     "no_news": false, "n_sources": 2, "sources": ["https://...", "https://..."]}
  ]
}
Rules: score in [-1, 1], confidence in [0, 1]; reason = one line, 1-200 chars;
no_news=true -> score 0, confidence 0, n_sources 0, no source; otherwise n_sources >= 1,
n_sources == len(sources), sources are distinct http(s) URLs taken from the search results
(the prompt forbids inventing a source; the schema can only check the format).
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from tradingbot.config import PROJECT_ROOT

SENTIMENT_LOG = PROJECT_ROOT / "data" / "sentiment" / "sentiment_log.csv"
MAX_WEB_SEARCHES = 4
COLUMNS = ["date", "symbol", "score", "confidence", "no_news", "n_sources", "reason", "sources",
           "ref_close", "ref_close_date", "recorded_at"]

Url = Annotated[str, StringConstraints(pattern=r"^https?://[^\s|]+$", max_length=500)]
StrictUnit = Annotated[float, Field(strict=True)]


class SentimentError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SentimentItem(_Strict):
    symbol: Annotated[str, StringConstraints(pattern=r"^[A-Z.]{1,10}$")]
    score: StrictUnit = Field(ge=-1, le=1)
    confidence: StrictUnit = Field(ge=0, le=1)
    reason: Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")]
    no_news: bool
    n_sources: int = Field(ge=0)
    sources: list[Url]

    @model_validator(mode="after")
    def _consistent(self) -> SentimentItem:
        if self.no_news:
            if self.score != 0 or self.confidence != 0 or self.n_sources != 0 or self.sources:
                raise ValueError(f"{self.symbol}: no_news requires score 0, confidence 0 and no source")
        else:
            if self.n_sources < 1:
                raise ValueError(f"{self.symbol}: a score needs at least one source (else set no_news)")
            if self.n_sources != len(self.sources):
                raise ValueError(f"{self.symbol}: n_sources={self.n_sources} but {len(self.sources)} URL(s)")
            if len(set(self.sources)) != len(self.sources):
                raise ValueError(f"{self.symbol}: duplicate source URL")
        return self


class SentimentFile(_Strict):
    date: date
    web_searches: int = Field(ge=0, le=MAX_WEB_SEARCHES)
    items: list[SentimentItem] = Field(min_length=1)


def parse_sentiment(raw: str | bytes, watchlist: Sequence[str]) -> SentimentFile:
    """Validate the JSON text; raises SentimentError with every problem listed."""
    try:
        data = SentimentFile.model_validate_json(raw)
    except ValidationError as exc:
        raise SentimentError(f"invalid sentiment file: {exc}") from None
    symbols = [i.symbol for i in data.items]
    dupes = sorted({s for s in symbols if symbols.count(s) > 1})
    missing = sorted(set(watchlist) - set(symbols))
    extra = sorted(set(symbols) - set(watchlist))
    problems = [f"duplicate symbol(s) {dupes}" if dupes else "", f"missing watchlist symbol(s) {missing}" if missing
                else "", f"symbol(s) not in watchlist {extra}" if extra else ""]
    problems = [p for p in problems if p]
    if problems:
        raise SentimentError("invalid sentiment file: " + "; ".join(problems))
    return data


def load_log(path: Path = SENTIMENT_LOG) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


RawCloses = Callable[[list[str], date], Mapping[str, tuple[date, float]]]   # (symbols, as_of)


def record_sentiment(data: SentimentFile, raw_closes: RawCloses, path: Path = SENTIMENT_LOG,
                     now: datetime | None = None) -> dict[str, int]:
    """Upsert one row per (date, symbol). Returns {'added': n, 'replaced': m}."""
    from datetime import timedelta

    closes = raw_closes([i.symbol for i in data.items], data.date - timedelta(days=1))
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    rows = {(r["date"], r["symbol"]): r for r in load_log(path)}
    counts = {"added": 0, "replaced": 0}
    for item in data.items:
        key = (data.date.isoformat(), item.symbol)
        counts["replaced" if key in rows else "added"] += 1
        close = closes.get(item.symbol)
        rows[key] = {"date": key[0], "symbol": item.symbol, "score": f"{item.score:g}",
                     "confidence": f"{item.confidence:g}", "no_news": str(item.no_news).lower(),
                     "n_sources": str(item.n_sources), "reason": item.reason, "sources": " | ".join(item.sources),
                     "ref_close": f"{close[1]:.4f}" if close else "",
                     "ref_close_date": close[0].isoformat() if close else "", "recorded_at": stamp}
    _write(path, (rows[k] for k in sorted(rows)))
    return counts


def _write(path: Path, rows: Iterable[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_file(path: Path, watchlist: Sequence[str]) -> SentimentFile:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SentimentError(f"cannot read {path}: {exc}") from None
    return parse_sentiment(raw, watchlist)


def dumps_example(watchlist: Sequence[str], day: date) -> str:
    """A valid all-no_news file (used by tests and as a starting point for the prompt)."""
    items = [{"symbol": s, "score": 0, "confidence": 0, "reason": "no relevant news found", "no_news": True,
              "n_sources": 0, "sources": []} for s in watchlist]
    return json.dumps({"date": day.isoformat(), "web_searches": 0, "items": items}, indent=2)
