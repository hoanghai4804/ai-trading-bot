# AI trading bot — weekly MA crossover on Alpaca PAPER

*Tiếng Việt: [README.vi.md](README.vi.md)*

A small but complete automated trading system, built to learn how real trading infrastructure is made
safe: a deterministic Python core makes every decision, an independent risk manager can veto any order,
and an LLM agent (Claude Code, running as a scheduled cloud routine) only orchestrates the run, reads a
compact summary, searches the news and writes a weekly report in Vietnamese.

**Paper trading only.** No real money, by design and by code (see [Safety layers](#safety-layers)).
The strategy is deliberately simple; the backtest shows it has **no edge** over a random-entry benchmark
(see [Results](#backtest-results)). The point of the project is the engineering around it.

## What it does
Every Monday (15:07 UTC, after the US open) a fresh Claude Code cloud session:
1. pulls the repo, installs a pinned Python 3.11 environment, and asks Python whether to run
   (`should-run`: RUN / SKIP / HALT, mode and exact command);
2. runs the weekly pipeline: daily bars up to last Friday → MA20/MA50 signals → risk manager →
   orders on the Alpaca **paper** account (market buy + attached GTC stop, safe market sells);
3. reads `runs/<date>/summary.md` (~40 lines, the only computed file the LLM reads), searches the news
   (≤ 4 searches), logs a per-symbol news-sentiment score (data only, never used for trading);
4. writes the report, sends it to Discord, updates its memory files, and commits + pushes the state.

Example output: [`docs/examples/weekly_report_example.md`](docs/examples/weekly_report_example.md) (a
rehearsal DRY-RUN on a day the market was closed, so no signal and no order).

## Architecture
```
                         Claude Code routine (cloud, Sonnet) — prompts/weekly_run.md
                         orchestration, news, report, memory; never decides a trade
                                            │ CLI
 ┌──────────────────────────────────────────▼───────────────────────────────────────────┐
 │ python -m tradingbot  (src/tradingbot)                                               │
 │                                                                                      │
 │  scheduling ── should-run ──► pipeline.run_weekly(as_of = last session)              │
 │                                   │                                                  │
 │  data/market_data (Alpaca bars, as_of cut, cache) ──► indicators (SMA, ATR, corr)    │
 │                                   ▼                                                  │
 │  strategies/ma_crossover ──► Signals ──► risk/manager (independent, can veto) ──►    │
 │       sizing, gap check, correlation, weekly limit, circuit breakers, HALT.lock      │
 │                                   ▼                                                  │
 │  execution/executor (dry-run | paper): OTO buy+stop, safe sell, stop registry        │
 │                                   ▼                                                  │
 │  reporting (summary.md/json, decisions.json) · monitoring (slippage, shadow check)   │
 │  notify/discord (never raises) · memory/compact · sentiment/record (data only)       │
 │                                                                                      │
 │  backtest/engine ── same Strategy + RiskManager + weekly_decision as live            │
 │       walk_forward · benchmarks (SPY B&H, SMA200, random ×50) · costs · metrics      │
 └──────────────────────────────────────────────────────────────────────────────────────┘
 State committed to git after every run: state/ (HALT.lock, stops, equity log, last run,
 execution quality), memory/, reports/, runs/, data/sentiment/.
```

### Trading rules (all parameters in `config/config.yaml`, strict pydantic schema)
- Universe: AAPL MSFT NVDA AMZN GOOGL META JPM XOM JNJ PG SPY (multi-sector), long-only.
- Buy: MA20 crossed above MA50 within the signal window (≥ 5 sessions, widened after a missed run) and is
  still above. Exit: MA20 < MA50.
- Reference price = latest trade at order time (rejected if older than 5 min); skip if it moved > 3% from
  the last raw close. Stop = ref − 2 × ATR(14), fixed, never moved; re-placed exactly if it disappears.
- Size = min(1% equity at risk, 5% equity notional), capped by available cash.
- Max 3 new positions per calendar week; a buy correlated > 0.8 (60-day returns) with a held or
  just-approved position is blocked.
- Circuit breakers: previous week −5% → no new positions; drawdown ≥ 10% from peak → sell everything,
  write `state/HALT.lock`, all later runs stop until a human removes it.

## Project structure
```
ai-trading-bot/
├── CLAUDE.md                     # rules for Claude Code (paper only, no secrets, layout, key decisions)
├── README.md / README.vi.md      # this file (English / Vietnamese)
├── pyproject.toml                # package definition (src/ layout, `python -m tradingbot`)
├── requirements.txt              # pinned dependencies
├── .env.example                  # names of the required env vars (the real .env is never committed)
├── .gitignore / .gitattributes   # ignored files (.env, cache, logs, venv) / LF line endings
│
├── config/
│   └── config.yaml               # ALL parameters: watchlist, strategy, risk, costs, backtest, schedule, notify
│
├── src/tradingbot/
│   ├── __init__.py / __main__.py # package marker / entry point of `python -m tradingbot`
│   ├── cli.py                    # every CLI command (check, data, signals, should-run, run, backtest, notify...)
│   ├── config.py                 # strict pydantic schema of config.yaml (mode must be paper, unknown keys rejected)
│   ├── credentials.py            # reads API keys from env vars; repr hides the secrets
│   ├── safety.py                 # paper guard (live endpoint / non-PA account -> abort), HALT.lock, run.lock
│   ├── scheduling.py             # should-run: RUN / SKIP / HALT, DRY-RUN vs EXECUTE, signal window
│   ├── pipeline.py               # run_weekly: account -> bars -> signals -> prices -> breakers -> risk -> executor
│   ├── broker/
│   │   └── alpaca_client.py      # thin Alpaca wrapper: account, clock, positions, orders, OTO buy, sells, cancel
│   ├── data/
│   │   └── market_data.py        # daily bars cut at as_of (+ parquet cache), raw closes, latest trade prices
│   ├── indicators/
│   │   └── technical.py          # SMA, true range, ATR (Wilder), daily returns, return correlation
│   ├── strategies/
│   │   ├── base.py               # Signal dataclass + Strategy interface
│   │   ├── ma_crossover.py       # MA20/MA50 crossover: buy on a recent cross up, exit when MA20 < MA50
│   │   └── registry.py           # strategy name in config -> class
│   ├── risk/
│   │   ├── sizing.py             # stop = ref - 2xATR, gap check, position size (1% risk, 5% cap, cash)
│   │   ├── circuit_breakers.py   # previous-week loss, drawdown from peak, HALT trip
│   │   └── manager.py            # RiskManager: independent review, veto with reason codes
│   ├── execution/
│   │   ├── orders.py             # deterministic order ids, weekly position count, last exits, open stops
│   │   ├── executor.py           # dry-run | paper: OTO buy + stop, safe sell, halt liquidation, stop checks
│   │   ├── stop_registry.py      # state/stops.json: original stop of each position, re-placed if missing
│   │   └── test_order.py         # MANUAL end-to-end order check (1 SPY + stop, then close); never weekly
│   ├── reporting/
│   │   ├── run_text.py           # console output of a run (first line = MODE)
│   │   ├── summary.py            # runs/<date>/summary.md, summary.json, decisions.json, memory/portfolio.md
│   │   └── equity_log.py         # state/equity_history.csv (equity vs SPY, extra drawdown peak source)
│   ├── monitoring/
│   │   ├── execution_quality.py  # fill price vs reference -> slippage in bps (state/execution_quality.csv)
│   │   └── shadow.py             # replays each live decision through the backtest path, alerts on mismatch
│   ├── notify/
│   │   ├── discord.py            # Discord webhook: reports + alerts, never raises, URL never logged
│   │   └── discord_report.py     # weekly report .md -> Discord embeds (readable on the phone, no download)
│   ├── memory/
│   │   └── compact.py            # keeps 4 weeks of journal, moves older entries to journal_archive.md
│   ├── sentiment/
│   │   └── record.py             # validates + stores news sentiment (data only, never used for trading)
│   └── backtest/
│       ├── engine.py             # weekly replay with the SAME strategy/risk code as live (weekly_decision)
│       ├── costs.py              # slippage (5 bps) + SEC/TAF fees
│       ├── metrics.py            # return, Sharpe, max drawdown, exposure, trade stats
│       ├── walk_forward.py       # train 3 y / test 6 m parameter search, out-of-sample run
│       ├── benchmarks.py         # SPY buy & hold, SMA200 on SPY, random entries x50
│       └── report.py             # reports/backtest/ (report.md, CSVs, equity.svg)
│
├── tests/                        # pytest, no network (HTTP blocked in conftest.py)
│   ├── conftest.py               # shared fixtures, network block
│   ├── fake_broker.py            # in-memory broker for executor/pipeline tests
│   ├── fixtures/                 # real bars of week 2026-04-27 (parity test)
│   └── test_*.py                 # one file per module (config, risk, executor, backtest parity, look-ahead...)
│
├── prompts/
│   ├── weekly_run.md             # prompt of the scheduled cloud routine (steps + hard rules)
│   └── templates/                # report_full.md / report_brief.md (weekly report templates, Vietnamese)
├── scripts/
│   └── cloud_setup.sh            # idempotent venv setup for the cloud routine; last line PYTHON=<path>
├── docs/
│   ├── PLAN.md                   # approved project plan
│   ├── PROGRESS.md               # phase status, log, open issues (resume point)
│   ├── ROUTINE_SETUP.md          # how to set up the Claude Code cloud routine
│   └── examples/                 # example weekly report
│
├── memory/                       # LLM memory: strategy.md, lessons.md, journal.md, portfolio.md (Python-written)
├── reports/
│   ├── backtest/                 # backtest report, CSV tables, equity curve
│   └── weekly/<date>.md          # weekly report written by the routine
├── runs/<date>/                  # per run: summary.md (the only file the LLM reads), summary.json,
│                                 #   decisions.json, sentiment.json
├── state/                        # persistent state: HALT.lock, stops.json, equity_history.csv,
│                                 #   last_run.json, execution_quality.csv (run.lock is never committed)
└── data/
    ├── sentiment/sentiment_log.csv  # news sentiment history (data only)
    └── cache/                    # bar cache (git-ignored)
```

## Safety layers
| Layer | Mechanism |
|---|---|
| Paper only | `TradingClient(paper=True)`; abort on any live endpoint in env/config, non-`PA` account number, `mode != paper`; the cloud network allowlist doesn't include the live trading host |
| Dry-run by default | `run` sends nothing without `--execute`; the routine adds it only when `TRADINGBOT_MODE=execute`, otherwise the report starts with a warning |
| LLM can't trade | The agent runs the exact command printed by `should-run`; prompt forbids editing code/config, and the run is reproducible from `decisions.json` |
| Independent risk manager | Every veto has a reason code, logged in `decisions.json`; exits are always allowed |
| No look-ahead | Every function takes `as_of`; data cut `<= as_of`; tests append future data and check that past signals don't change |
| Idempotency | Deterministic `client_order_id`, open-order/position checks, run lock; running twice sends nothing new |
| Safe sell | Cancel stop → confirm → market sell → on failure restore the original stop + alert |
| Monitoring | Fill slippage vs the 5 bps backtest assumption; shadow check replays each live decision through the backtest path and alerts on mismatch |
| Secrets | Never printed or logged; `env-check` reports set/missing only; on the cloud the Alpaca keys are meant to be network secrets, injected by a proxy outside the VM |
| Alerts | Every abort/failure → stderr + Discord, plus an "aborted" summary so the run is always reported |

## Backtest results
Weekly replay with the **same** strategy, risk manager and sizing code as live; entry at Monday's open,
5 bps slippage per side + SEC/TAF fees, stops gapped at the open fill at the open. Data: Alpaca SIP
adjusted daily bars 2016-01-04 → 2026-10-09; $100,000 start.

**Full period 2017-01-01 → 2026-10-09**

| Strategy | Annual return | Sharpe | Max DD | Exposure | Trades |
|---|---|---|---|---|---|
| MA20/50 (this bot) | +3.5% | 0.76 | 9.5% | 23% | 231 |
| SPY buy & hold | +15.3% | 0.89 | 33.8% | 100% | – |
| SMA200 on SPY | +11.2% | 0.95 | 17.6% | 83% | 13 |
| Random entries ×50, same risk rules (median [p5; p95]) | +5.3% [+3.5; +6.5] | 1.08 [0.79; 1.29] | 8.1% | 23% | 232 |

**Out-of-sample 2019 → 2026 (walk-forward: train 3 y / test 6 m, grid fast {10,20,30} × slow {50,100}):**
walk-forward +2.5%/y, Sharpe 0.66; fixed MA20/50 +3.1%/y, Sharpe 0.69; random median Sharpe 1.09.

**Conclusion.** The MA20/50 signal does **not** beat random entries under the same risk rules (≈ 4th
percentile of the random runs on return and Sharpe over the full period, below every random run
out-of-sample), the walk-forward winner changes from window to window (no stable parameter), and holding
SPY at the same 23% exposure would have returned +42.4% vs +39.2%. The low drawdown comes from the risk
rules and the low exposure, not from the signal. Full report: [`reports/backtest/report.md`](reports/backtest/report.md).

## Running it
Requires Python 3.11 and an Alpaca **paper** account (free). Windows paths shown; on Linux use `.venv/bin/python`.
```bash
py -3.11 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt && .venv/Scripts/python -m pip install -e . --no-deps
cp .env.example .env            # ALPACA_API_KEY, ALPACA_SECRET_KEY, DISCORD_WEBHOOK_URL (never committed)
.venv/Scripts/python -m pytest  # ~390 tests, no network
.venv/Scripts/python -m tradingbot env-check   # set / MISSING, never values
.venv/Scripts/python -m tradingbot check       # paper account + market status
.venv/Scripts/python -m tradingbot signals     # strategy proposals only
.venv/Scripts/python -m tradingbot run         # weekly run, DRY-RUN (add --execute for paper orders)
.venv/Scripts/python -m tradingbot backtest    # reports/backtest/
```
Scheduled cloud run: [`docs/ROUTINE_SETUP.md`](docs/ROUTINE_SETUP.md) (Claude Code routine, environment,
network secrets, gradual rollout dry-run → execute). Prompt: [`prompts/weekly_run.md`](prompts/weekly_run.md).

## Testing approach
- pytest, no network (HTTP blocked in `conftest.py`); fake broker for executor/pipeline tests.
- Parity test: one real week replayed through the backtest equals the live dry-run decision.
- Mutation checks after each phase: rules changed one at a time (e.g. 5% → 50%, `<` → `<=`, guard
  removed) to confirm a test fails; surviving mutants led to new tests (more than 100 mutants in total).

## Limitations
- The strategy has no demonstrated edge (see above); this is a learning/engineering project.
- Survivorship bias: today's large-cap watchlist applied to 2016–2026; data only from 2016.
- Backtest fills at Monday's open, live fills ~11:07 New York; adjusted vs raw prices around ex-dates.
- Risk is checked once a week: between runs only the GTC stops protect positions; a halt while the market
  is closed cannot sell until the next run. Alpaca cancels GTC orders after 90 days (the bot re-places
  the original stop each run).
- IEX real-time trades only (free data plan) for the reference price; cash earns 0% in the backtest.
- The cloud routine depends on a research-preview feature and on the subscription's usage limits.

## Possible next steps
- Evaluate the logged news sentiment against next-week returns after 1–2 years of data.
- Strategy research with the existing harness (always vs the random benchmark): regime filter
  (forward-only HMM), volatility-scaled sizing (GARCH), mean reversion on non-trending symbols (ADF).
- Survivorship-free universe, intraday fills in the backtest, transaction-cost model from real fills.

## Built with
Python 3.11, alpaca-py, pandas, numpy, pydantic, pytest · Claude Code (development and scheduled cloud routine).
