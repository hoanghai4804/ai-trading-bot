# Progress

Update this file at the end of every phase (status, what was done, open issues).
Plan: [docs/PLAN.md](PLAN.md).

| # | Phase | Status | Commit |
|---|---|---|---|
| 0 | git init + .gitignore | done | 8684c32 |
| 1 | Project skeleton, strict config, tests | done | 7c524f7 |
| 2 | Alpaca connection + paper guard | done | e7f7550 |
| 3 | Data & indicators | done | 4dd1754 |
| 4 | Strategy (MA crossover) | done | f16bc77 |
| 5 | Risk manager + circuit breakers | todo | |
| 6 | Execution (dry-run, OTO, safe sell, idempotency) | todo | |
| 7 | Backtest (walk-forward, costs, 3 benchmarks) | todo | |
| 8 | Memory, reports, Discord | todo | |
| 9 | Scheduled-run prompt + cloud routine guide | todo | |

## Next step (resume here)
- Last session ended 2026-10-10 after phase 4 was committed and pushed.
- Then: **phase 5 — Risk manager + circuit breakers** (docs/PLAN.md §Các giai đoạn, item 5).
- Workflow reminder: tests pass → stop and summarize in Vietnamese → wait for approval → commit → push.

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

### Phase 4 — Strategy
- `strategies/base.py`: `Signal` dataclass + `Strategy.generate_signals(bars, as_of, positions, last_exit)`;
  output order = exits (alphabetical), then buys by most recent cross, ties alphabetical.
- `strategies/ma_crossover.py`: strict params (fast < slow, lookback >= 1). Cross at t = fast[t] > slow[t] and
  fast[t-1] <= slow[t-1]; buy if cross is 0..lookback-1 sessions before as_of, still above, not held, and no
  bot exit after the cross date. Exit if held and fast < slow. Data cut at as_of inside the strategy.
- `strategies/registry.py`: config name → class; `config` command now validates strategy params.
- CLI `signals [--as-of] [--held SYM,...]` (no risk checks, no orders). Weekly scan over the last year on real
  data looks sane (0–4 buy signals per week).
- Note: in a 4-session holiday week a cross from the previous Friday is still inside the 5-session window,
  so it can be proposed twice (risk manager / held check handle it).
- 74 tests pass.
