# CLAUDE.md — AI trading bot (Alpaca PAPER only)

Weekly MA20/MA50 crossover bot. **Python makes every computed decision** (indicators, signals,
sizing, risk checks); Claude only reads compact summaries, searches news and writes the report.

**At the start of every new session, read `docs/PLAN.md` (approved plan) and `docs/PROGRESS.md`
(current phase, open issues) before doing anything else. Update `docs/PROGRESS.md` after each phase.**

## Hard rules
- PAPER ONLY. `config.mode` must be `paper`; code aborts on any live endpoint/config. Never real money.
- Never print, log or ask for keys. Secrets come from env vars (`.env` locally, never committed).
- Long-only stocks/ETFs. No leverage, no short, no options.
- Risk manager is independent of strategies and can veto any order; every blocked order is logged with a reason.
- No look-ahead: every function takes `as_of`; data is cut at `<= as_of`.
- All parameters live in `config/config.yaml` (strict schema in `src/tradingbot/config.py`).
- Talk to the user in Vietnamese; code, names and comments in English.

## Layout
- `config/config.yaml` — watchlist, strategy, risk, costs, backtest, schedule, notify
- `src/tradingbot/` — package (`python -m tradingbot <command>`)
- `tests/` — pytest, no network calls
- `memory/` — strategy, portfolio, journal, lessons (short; 4 weeks detailed, older summarized)
- `prompts/weekly_run.md` — prompt for the scheduled run (fresh session)

## Persistent state (cloud runs)
Committed and pushed to `main` after every run: `state/HALT.lock`, `state/equity_history.csv`,
`state/last_run.json`, `memory/`, `reports/`, `runs/`.
Never committed: `state/run.lock`, `data/cache/`, `logs/`, `.env`, `.venv`.

## Dev commands
```
py -3.11 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt && .venv/Scripts/python -m pip install -e . --no-deps
.venv/Scripts/python -m pytest
.venv/Scripts/python -m tradingbot config      # validate config
```

## Key decisions
- Watchlist: AAPL MSFT NVDA AMZN GOOGL META JPM XOM JNJ PG SPY (multi-sector so the correlation check matters).
- Buy: MA20 crossed above MA50 within last 5 sessions and still above. Exit: MA20 < MA50.
- Reference price = latest trade at order time (backtest: open). Stop = ref − 2×ATR(14), fixed.
  Skip if |ref/Friday close − 1| > 3% or stop ≥ ref.
- Size = min(1% equity / (ref − stop), 5% equity / ref), capped by cash.
- Weekly limits use calendar weeks (New York) from Alpaca order history, not run counts.
- Runs: cloud cron Monday 15:07 UTC only; `schedule.runs` in config is the guard/extension point.
- Drawdown breaker only checked at run time; between runs only GTC stops protect positions.

## Phase status
Tracked in `docs/PROGRESS.md` (single source; do not duplicate here).
