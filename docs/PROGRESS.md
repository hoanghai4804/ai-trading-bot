# Progress

Update this file at the end of every phase (status, what was done, open issues).
Plan: [docs/PLAN.md](PLAN.md).

| # | Phase | Status | Commit |
|---|---|---|---|
| 0 | git init + .gitignore | done | 8684c32 |
| 1 | Project skeleton, strict config, tests | done | 7c524f7 |
| 2 | Alpaca connection + paper guard | done, waiting for approval | — |
| 3 | Data & indicators | todo | |
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
