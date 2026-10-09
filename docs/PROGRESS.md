# Progress

Update this file at the end of every phase (status, what was done, open issues).
Plan: [docs/PLAN.md](PLAN.md).

| # | Phase | Status | Commit |
|---|---|---|---|
| 0 | git init + .gitignore | done | 8684c32 |
| 1 | Project skeleton, strict config, tests | done | 7c524f7 |
| 2 | Alpaca connection + paper guard | done | e7f7550 |
| 3 | Data & indicators | done | 4dd1754 |
| 4 | Strategy (MA crossover) | todo | |
| 5 | Risk manager + circuit breakers | todo | |
| 6 | Execution (dry-run, OTO, safe sell, idempotency) | todo | |
| 7 | Backtest (walk-forward, costs, 3 benchmarks) | todo | |
| 8 | Memory, reports, Discord | todo | |
| 9 | Scheduled-run prompt + cloud routine guide | todo | |

## Log
### Phase 1 — Project skeleton
- Python 3.11 venv, pinned `requirements.txt`, editable package `src/tradingbot`.
- `config/config.yaml` + strict pydantic schema (`mode` must be `paper`, unknown keys rejected, frozen).
- CLI `python -m tradingbot config`; 14 tests pass.
- `CLAUDE.md`, `memory/strategy.md` (later-phase ideas), `.gitattributes` (LF), `.env.example`.
- Open: SEC/TAF fee rates in config should be re-checked periodically (backtest only).

### Plan change — GitHub repo created early
- Private repo `ai-trading-bot` created with gh after phase 1; push after every approved phase.

### Phase 2 — Alpaca connection + paper guard
- `safety.py`: live-endpoint env check, client base-URL check, `PA` account check, HALT.lock, run.lock (TTL).
- `credentials.py`: keys from env (`.env` only if present, never overrides), repr hides secrets.
- `broker/alpaca_client.py`: account / clock / positions / daily equity history as plain dataclasses.
- CLI `check` verified against the real paper account (PA…2VKZ, equity 100,000, market open).
- Lessons: alpaca-py stores the base URL as a `BaseURL` enum (check `.value`, not `str()`);
  portfolio history reports 0 equity for days before funding → dropped.
- 38 tests pass.

### Phase 3 — Data & indicators
- `data/market_data.py`: Alpaca adjusted daily bars (SIP from 2016; IEX fallback only has data from 2020-07),
  indexed by New York date, `get_bars(..., as_of)` never returns rows after `as_of`, default `as_of` = yesterday (NY).
- Cache `data/cache/<SYMBOL>.parquet` (git-ignored) reused only on the day it was fetched: adjusted prices are
  rescaled by each new dividend/split, so appending to an old cache would mix adjustment bases.
- `indicators/technical.py`: SMA, true range, ATR (Wilder, seeded with SMA), daily returns, return correlation
  (NaN when overlap < lookback → callers must treat as unknown).
- CLI `data [--as-of]` checked on real data (as of 2026-10-08: highest 60-day corr AMZN/GOOGL 0.69 < 0.8).
- 56 tests pass.
