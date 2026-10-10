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
`state/last_run.json`, `state/stops.json`, `memory/`, `reports/`, `runs/`.
Never committed: `state/run.lock`, `data/cache/`, `logs/`, `.env`, `.venv`.

## Git workflow
Remote `origin` = private GitHub repo `ai-trading-bot`, branch `main`. After each phase is approved:
commit → `git push`. Before any push, confirm `.env` is not tracked (`git ls-files | grep -x .env` must be empty).

## Dev commands
```
py -3.11 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt && .venv/Scripts/python -m pip install -e . --no-deps
.venv/Scripts/python -m pytest
.venv/Scripts/python -m tradingbot config      # validate config
.venv/Scripts/python -m tradingbot check       # paper account + market status (exit 2 = safety abort)
.venv/Scripts/python -m tradingbot data        # watchlist indicators up to yesterday (--as-of YYYY-MM-DD)
.venv/Scripts/python -m tradingbot signals     # strategy proposals only, no risk/orders (--as-of, --held)
.venv/Scripts/python -m tradingbot run         # weekly run, DRY-RUN unless --execute (paper orders)
.venv/Scripts/python -m tradingbot test-order  # MANUAL only (never in the weekly run): 1 SPY + OTO stop, verify, close; --execute
```

## Key decisions
- Watchlist: AAPL MSFT NVDA AMZN GOOGL META JPM XOM JNJ PG SPY (multi-sector so the correlation check matters).
- Buy: MA20 crossed above MA50 within last 5 sessions and still above. Exit: MA20 < MA50.
- Reference price = latest IEX trade at order time (backtest: open), older than `data.max_price_age_min`
  (5) → no_reference_price. Stop = ref − 2×ATR(14), fixed. Skip if |ref/last RAW (unadjusted) close − 1| > 3%
  or stop ≥ ref.
- Original stop of each position lives in `state/stops.json`; a held position without an open stop gets
  exactly that stop back (+ alert). Stops are NEVER moved.
- Buy-signal window = sessions since `last_trading_as_of` in `state/last_run.json` (last EXECUTE run with the
  market open), clamped to [5, `schedule.max_signal_window_sessions`=15]; catches crosses after a holiday run.
- `run` / `test-order` are DRY-RUN unless `--execute`; the first output line always states the mode.
  `test-order` is manual only, never part of the weekly run.
- Size = min(1% equity / (ref − stop), 5% equity / ref), capped by cash × (1 − cash_buffer_pct);
  same-run sell proceeds are not counted. Stop rounded down to the cent.
- Risk vetoes have reason codes (see `risk/manager.py` docstring); unknown correlation blocks, unknown
  previous-week return does not.
- Weekly limits use calendar weeks (New York) from Alpaca order history, not run counts.
- Runs: cloud cron Monday 15:07 UTC only; `schedule.runs` in config is the guard/extension point.
- Drawdown breaker only checked at run time; between runs only GTC stops protect positions.

## Phase status
Tracked in `docs/PROGRESS.md` (single source; do not duplicate here).
