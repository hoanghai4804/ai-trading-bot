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
| 5 | Risk manager + circuit breakers | done | 2c02a78 |
| 6 | Execution (dry-run, OTO, safe sell, idempotency) | done | eacb9c2 |
| 6b | Mode line, fresh/raw ref price, stop registry, signal window | done | 7f33780 |
| 6c | Sell at market when the original stop is breached (stop_breached) | done | 9289d31 |
| 7 | Backtest (walk-forward, costs, 3 benchmarks) | done | 32d84a7 |
| 8 | Memory, reports, Discord + A (sentiment log) + B (execution quality, shadow check) | done | (this commit) |
| 9 | Scheduled-run prompt + cloud routine guide + README | done | (this commit) |

## Next step (resume here)
- Phase 9 committed 2026-10-10 (393 tests pass). `test-order --execute` has NOT run yet (market closed).
- Remaining steps, in order:
  1. **Monday 2026-10-12 after 15:30 Paris (market open)**: `test-order --execute` once on the local machine
     (expect `RESULT PASS`), then `run` (dry-run) once. Record the result here.
  2. Cloud setup per `docs/ROUTINE_SETUP.md`: GitHub App on the repo, environment `tradingbot` (Custom network,
     Alpaca network secret + placeholder env vars, DISCORD_WEBHOOK_URL, BASH_DEFAULT_TIMEOUT_MS, setup script),
     routine (Sonnet, Monday 15:07 UTC, no connectors). TRADINGBOT_MODE NOT set.
  3. Run now with `force-dry-run-full`: check transcript (`account=PA...`, no 403, no secret read), the
     commit on GitHub, the Discord message. If the network secret fails (401/403): fallback env vars.
     First real check of `cloud_setup.sh` on Linux.
  4. Scheduled cloud DRY-RUNs for at least 1-2 weeks without error.
  5. Only if step 1 passed AND step 4 is clean: set `TRADINGBOT_MODE=execute`.
  6. Then: strategy research (MA20/50 has no edge vs the random benchmark).
- Workflow reminder: tests pass -> stop and summarize in Vietnamese -> wait for approval -> commit -> push.

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

### Phase 5 — Risk manager + circuit breakers
- `risk/sizing.py` (pure): stop = ref − 2×ATR rounded down to the cent; gap check |ref/last close − 1| ≤ 3%
  (inclusive) and 0 < stop < ref; whole-share size = min(risk 1%, cap 5%, cash), reports the binding limit.
- `risk/circuit_breakers.py`: previous calendar week return (NY dates, Mon–Sun, last value per week; unknown →
  does not block); drawdown vs peak of broker history + local log + current equity (≥ 10% → halt);
  float tolerance so exact thresholds behave as specified; `trip_halt` writes HALT.lock.
- `risk/manager.py`: `RiskManager.review(signals, as_of, account, bars, ref_prices, breakers) -> RiskDecision`.
  Sells always allowed when held (all positions sold on halt); buys pass 9 ordered checks, each veto has a code
  + detail; correlation partners = held positions not sold now + buys approved earlier in the run;
  cash = cash × (1 − cash_buffer_pct) − approved notional (sell proceeds not counted). `to_dict()` is JSON-ready.
- New config key `risk.cash_buffer_pct: 0.01`.
- Real-data replay (2026-04-24 signals, 100k paper account): AAPL/SPY/NVDA approved, GOOGL blocked by weekly limit.
  Note: with a 2×ATR stop the 5% position cap always binds first → real risk per trade ≈ 0.1–0.3% of equity.
- Bugs caught by tests: empty equity history (new account) crashed date comparisons; numpy floats broke
  stop rounding.
- Mutation check (code changed one rule at a time, tests untouched, then restored): 5%→50%, 1%→10%, 3→30/week,
  drawdown 10%→50%, weekly loss 5%→50%, HALT checks (assert_not_halted / buy block / liquidation) removed,
  sell-when-not-held allowed → all caught. Corr 0.8→0.99 initially survived (fixtures only had corr 1.0 or ~0)
  → added tests with corr ~0.9 (blocked) and ~0.7 (allowed); now caught.
- 133 tests pass.

### Before phase 6 — extra risk tests + mutation re-run
- New tests for rules guarded by a single test: risk 1%/trade (exact qty at two equity levels; budget is 1% of
  equity per trade, not of remaining cash), halt blocks buys (halt code wins over every other check; halt flag
  alone is enough), halt liquidates (all positions incl. fractional and non-watchlist, no exit signal needed;
  each symbol once, strategy reason kept).
- Mutation re-run (12 mutants on these 3 rules): all caught. 4 of them were caught ONLY by the new tests:
  halt checked after watchlist/held, halt relying on block_new_entries, liquidating watchlist symbols only,
  liquidation overwriting the exit reason.

### Phase 6 — Execution
- `broker/alpaca_client.py`: `OrderSnapshot` (legs nested), get_orders / get_order / get_order_by_client_id
  (404 -> None), `submit_oto_buy` (market, GTC, stop_loss leg), `submit_market_sell` (DAY), `submit_stop_sell`
  (GTC), `cancel_order`. `data/market_data.py`: `LatestPrices` (latest trade, feed `data.latest_price_feed`=iex).
- `execution/orders.py` (pure): ids `tb-<date>-<SYM>-<buy|sell>` (restored stops `-stop-HHMMSS`);
  `new_positions_this_week` = distinct symbols of bot BUY orders submitted this NY calendar week, any status
  except rejected/canceled/expired (those count only if shares filled); `last_exits` (bot market sells + OTO stop
  legs, attributed via the parent id); `stop_fills_since` (mid-week stop reconciliation); `open_stop_sells`.
- `execution/executor.py`: halt -> HALT.lock written first, open non-stop orders canceled, then sells; market
  closed -> nothing sent; safe sell = cancel stop(s) -> wait for confirmation (else no sell) -> market sell ->
  wait fill; on reject / API error / timeout: cancel the sell, restore the old stop for the remaining shares +
  alert; restore failure or no previous stop -> critical alert. Buys: idempotency checks (held, same id,
  open buy) then OTO; filled buy without stop leg -> critical alert. Final check: held positions without an
  open stop -> alert. Dry-run reads only (tested: zero write calls).
- `pipeline.py` `run_weekly`: account/clock/positions/order history (35 days) -> bars (watchlist + held, cut
  at yesterday) -> signals (with last_exit) -> latest prices -> breakers -> RiskManager -> Executor.
- CLI: `run` (dry-run unless `--execute`), `test-order` (manual only, dry-run unless `--execute`; refuses if
  SPY is held or has open orders; ids `tbtest-...` so never counted as bot orders).
- New config keys: execution.buy_fill_timeout_sec, cancel_timeout_sec, poll_interval_sec, test_order_symbol,
  test_order_stop_pct; data.latest_price_feed.
- Real checks on 2026-10-10 (Saturday, market closed, dry-run only): `check`, `run` (0 signals as of 10-09),
  `test-order` (plan only). Replay of week 2026-04-27 with real data: same decisions as the phase-5 replay;
  with today's prices AAPL/SPY/NVDA are blocked by the gap check (+9% to +24%).
- Mutation check on execution rules (13 mutants: dry-run guards, market-closed guard, HALT.lock write,
  idempotency, stop cancel/restore, weekly count statuses, NY week, test-order prefix/closed market): all caught.
- 191 tests pass.
- Open issues:
  - The OTO request shape is only unit-tested; first real acceptance check = test-order on Monday.
  - Alpaca cancels GTC orders after 90 days: stops of positions held longer disappear. -> fixed in 6b.
  - Halt while the market is closed: HALT.lock is written but nothing is sold (critical alert, manual action).
  - Alerts go to stderr until Discord (phase 8). decisions.json / summary / local equity log are phase 8
    (last_run.json came in 6b).

### Phase 6b — hardening of execution
1. Mode: `reporting/run_text.format_run` (used by `run`; phase 8 summary builds on it) and `test-order` print
   `MODE: DRY-RUN ...` / `MODE: EXECUTE ...` as the FIRST line; `RunResult.to_dict()` starts with `mode`.
2. Reference price: `LatestPrices` returns price + timestamp; `fresh_prices` drops trades older than
   `data.max_price_age_min` (5) -> `no_reference_price` with the age in the detail (exactly 5 min still ok).
   Gap check uses the last RAW close (`MarketData.get_raw_closes`, adjustment=raw, not cached); missing raw
   close or one from another session -> new veto `no_raw_close`. The backtest passes no raw closes (uses bars).
   test-order requires a fresh price with `--execute` (dry-run accepts a stale one and says so).
3. `execution/stop_registry.py` -> `state/stops.json` (committed): original stop recorded when a buy is sent.
   Every run (end of execution): held position without an open stop -> re-place exactly the original stop
   (+ warning); open stop at another price -> warning, left unchanged; no recorded stop -> critical alert, no
   stop invented; dry-run or market closed -> nothing sent (+ warning). A failed sell restores the ORIGINAL
   stop. Entries of closed positions are dropped (kept while a buy is pending). Dry-run never writes the file.
4. Signal window: `state/last_run.json` (EXECUTE runs only) keeps `last_trading_as_of` (advances only when the
   market was open). Window = sessions after it up to as_of, clamped to [cross_lookback_sessions=5,
   `schedule.max_signal_window_sessions`=15]. Test: Monday 2026-10-05 holiday -> the 10-12 run uses 10 sessions
   and catches the 10-01 cross; with a normal Monday the window is 5 and the cross is (correctly) not re-proposed.
- Mutation check: 24 mutants over the 4 points; 3 survived at first (max age in hours instead of minutes,
  pipeline not passing raw closes, dry-run saving the stop file) -> tests added -> 24/24 caught.
- Real dry-run 2026-10-10 (market closed): mode line OK; SPY latest trade 836 min old flagged; raw and adjusted
  closes of 10-09 identical (no corporate action since).
- Open: if the price is already below the original stop, re-placing the stop will be rejected by Alpaca ->
  critical alert only (no automatic market sell). -> resolved in 6c.

### Phase 6c — stop_breached (user decision 2026-10-10)
- In stop reconciliation: held position with NO open stop and a FRESH reference price (passed
  `data.max_price_age_min`) at or below its original stop -> sell at market through the existing safe-sell
  flow; an `ApprovedOrder` with reason `stop_breached: ...` is appended to the decision, plus an alert
  (warning if filled, critical if the sell failed; a failed sell tries to restore the original stop).
- Only with `--execute` and the market open. Dry-run / market closed -> nothing sent, critical alert.
  Stale or missing price -> no sell; the original stop is re-placed as usual (a rejection = critical alert).
  A position whose stop is still open is left to that stop.
- Fix found by the new tests: the stop registry pruned closed positions using the position list read BEFORE
  the stop_breached sells -> now re-read after them.
- Mutation check: 9 mutants (check off, `<` vs `<=`, stale prices reaching the executor, dry-run / closed
  market guards, bypassing safe sell, reason not recorded, alert level, stale pruning) -> all caught.
- 232 tests pass.

### Phase 7 — Backtest
- `backtest/engine.py`: replays the Monday run session by session with the SAME Strategy, `signal_window`,
  `evaluate_circuit_breakers` and `RiskManager` as live. Order per session: stop gapped at the open -> sold at the
  OPEN (`stop_gap`); Monday run (as_of = d-1, bars cut like live, ref = Monday open, gap check vs bar close);
  intraday stop (low <= stop) -> sold at the stop, entry day included; mark at the close. Holiday Monday = no run
  that week, next window widens like live. Monday-only runs -> weekly count is always 0 before the run.
- HALT in the backtest: the risk manager sells everything at the open and the event is recorded. Main line
  (`halt_mode="continue"`) assumes a human reviewed and deleted HALT.lock: trading resumes the next week and the
  drawdown peak restarts at the halt session (new `peak_since` argument of `evaluate_circuit_breakers`, live runs
  leave it None). Variant `halt_mode="stop"` (reported as `MA20/50 (HALT = stop)`) stays in cash to the end.
- `costs.py`: 5 bps slippage per side, SEC + TAF (capped) on sells, rounded up to the cent.
- `walk_forward.py`: train 3 y / test 6 m, grid fast {10,20,30} x slow {50,100}, best train Sharpe (NaN worst,
  ties = grid order); train runs only get bars cut at train_end; out-of-sample = one continuous run.
- `benchmarks.py`: SPY buy & hold, SMA200 on SPY (Monday, 100%/0%), RandomStrategy in the same engine (same risk
  rules, stops, breakers, costs) calibrated to MA20/50 signal frequency, 50 seeds -> median + p5/p95.
- `report.py` + CLI `backtest [--end --seeds --workers --out]` -> `reports/backtest/` (report.md in Vietnamese,
  CSVs, equity.svg). Config: `backtest.warmup_months: 12` (full period starts 2017-01-01). No config change from
  the sensitivity table (stop 2/2.5/3 x ATR x cap 5%/10% is report-only).
- Parity test: week 2026-04-27 on real SIP bars (fixture) -> backtest decision == live `run_weekly` (dry-run):
  AAPL 18 @ 265.62 stop 253.88, SPY 7 @ 709.58 stop 692.38, NVDA 23 @ 209.17 stop 198.29; GOOGL blocked
  `weekly_limit`.
- Results (SIP adjusted bars 2016-01-04 -> 2026-10-09; full period 2017-01-01 -> 2026-10-09):
  MA20/50 +3.5%/y, Sharpe 0.76, max DD 9.5%, exposure 23%, 231 trades, win 38%, hold 64 d.
  SPY B&H +15.3%/y, Sharpe 0.89, DD 33.8%. SMA200 SPY +11.2%/y, Sharpe 0.95, DD 17.6%.
  Random x50 median +5.3%/y, Sharpe 1.08 [p5 0.79; p95 1.29]. MA20/50 sits at the ~4th percentile of the random
  seeds on return and Sharpe in the full period, below every seed out-of-sample (2019+). SPY at the same 23%
  exposure: +42.4% vs MA20/50 +39.2%. Walk-forward (OOS) +2.5%/y, Sharpe 0.66 vs fixed MA20/50 +3.1%/y, 0.69;
  chosen pair changes often (30/50 x6, 10/100 x5, 10/50 x3, 30/100 x2 over 16 windows) -> no stable edge.
  Blocked buys (full, MA20/50): correlation 28, weekly_limit 5, gap_too_large 4, drawdown_halt 0.
- **Conclusion (user decision 2026-10-10): the MA20/50 signal does not beat the random-entry benchmark (same risk
  rules). Improving the strategy is work for after phase 9. The 5% position cap stays; config unchanged.**
- Mutation check (18 mutants, code changed, tests untouched, restored): entry at Friday close / Monday close,
  strategy seeing Monday's bar, no slippage / no fees / no costs / fees not debited, breakers disabled (empty
  history, thresholds 100%), halt=stop keeps trading, peak not reset, gapped stop filled at the stop, open-gap
  check removed, walk-forward trained on the test window / extended into it / picking the worst Sharpe -> all
  caught. 2 survived: random benchmark without risk rules, random benchmark without a usable stop -> added
  `test_random_job_applies_the_configured_risk_rules` -> 18/18 caught.
- Limits (in report.md section 9): survivorship bias of today's watchlist, data only from 2016, adjusted prices,
  Monday open vs ~11:07 live fill, no intraday order, GTC 90-day cancel not simulated, cash earns 0%.
- 278 tests pass.

### Phase 8 — Memory, reports, Discord (+ A sentiment log, B monitoring)
- `notify/discord.py`: webhook from DISCORD_WEBHOOK_URL; content cut to 1990 chars, mentions disabled; timeout,
  `max_retries` (5xx / network: backoff, 429: `retry_after` capped 30 s, other 4xx: no retry); never raises;
  `logs/notify.log` with the URL redacted (exact URL + any discord webhook pattern). `alert()` = stderr + Discord.
  CLI: `notify report [--path --date]` (message from summary.json + report .md attached; falls back to
  runs/<date>/summary.md), `notify-test`. `run --no-notify` keeps alerts on stderr only.
- Alerts wired: executor + pipeline alerts -> notifier (DRY-RUN tagged); `run` aborted by a SafetyError (HALT.lock
  found, paper guard) or any exception -> critical alert + an "aborted" summary (exception still re-raised).
- `reporting/summary.py`: runs/<date>/summary.md (<= 45 lines, line 1 = MODE from run_text; when long, the middle
  is cut first, the shadow/quality/next lines are always kept and warnings keep at least half the budget),
  summary.json (account, performance vs SPY, positions AFTER execution with original stops, approved/blocked,
  orders, execution quality, shadow check, alerts, next_steps incl. report template + sentiment file),
  decisions.json (full RunResult incl. order_refs). EXECUTE runs rewrite memory/portfolio.md.
  `scheduling.py`: report mode from `schedule.runs` (unscheduled manual run -> brief).
- Templates (Vietnamese): `prompts/templates/report_full.md` (8 sections) and `report_brief.md` (4 sections).
- `memory/`: journal.md (+ `compact`: entries before the Monday of the week 3 weeks back -> journal_archive.md,
  one summary line kept; idempotent, crash-safe), lessons.md, portfolio.md (Python-written), strategy.md updated.
- Equity log `state/equity_history.csv` (EXECUTE only, upsert per NY date, benchmark = SPY latest raw trade):
  performance since last run / since start vs SPY (price return), and extra peak source for the drawdown breaker.
- A: `sentiment/record.py` + `record-sentiment --file`: strict schema (pydantic strict, extra keys rejected,
  exact watchlist, score [-1,1], confidence [0,1], one-line reason, no_news => 0/0/no source, n_sources ==
  distinct http(s) URLs, web_searches <= 4) -> data/sentiment/sentiment_log.csv upsert by (date, symbol) with the
  raw close of the last session. Test: no trading module mentions "sentiment". Forward returns: later.
- B1: `monitoring/execution_quality.py`: bot fills since the previous EXECUTE run (orders + legs) vs reference
  (market: price used by the decision, kept in decisions.json `order_refs`; stop: its stop price) ->
  state/execution_quality.csv (append, dedupe by order id); summary shows mean market slippage vs 5 bps.
- B2: `monitoring/shadow.py`: after each run the decision is replayed through `backtest.engine.weekly_decision`
  (extracted from the engine, which now uses it too) with the same inputs; approved (symbol, side) compared with
  the live decision taken before execution. Mismatch -> warning in summary + alert. Known legit difference: raw
  vs adjusted close in the gap check around an ex-date.
- tests/conftest.py blocks all HTTP in tests: an old CLI test had started to open a real (read-only) Alpaca
  connection and write the real runs/ after `run` changed; fixed (run goes through `_connect`, test isolated).
- Mutation check: 32 mutants (Discord 6, sentiment 6, compact 3, equity log 3, shadow 3, quality 4, summary 4,
  CLI 3). 1 survived at first (archive duplicated after an interrupted compact) -> test added -> 32/32 caught.
- Real `notify-test` (2026-10-10): message received on the user's phone.
- Rule added (user, 2026-10-10): NEVER read `.env` in any way (I had used awk/grep/wc on it to diagnose the
  missing webhook: names and value lengths only, but still a breach). New `env-check` command prints only
  set/MISSING after loading the environment; rule written in CLAUDE.md hard rules.
- Incident check after the real-connection test: git status/diff -> state/ empty and untracked (no
  equity_history / stops / last_run / execution_quality written), memory/ only intended changes, no runs/ left.
- 364 tests pass.

### Phase 9 — Scheduled-run prompt, cloud routine guide, README
- `scheduling.decide_run` + CLI `should-run [--force [brief|full]] [--today]`: first line
  `SHOULD-RUN: RUN|SKIP|HALT mode=... report=... date=...`, then `reason:`, `command:` (the exact `run`
  command, `--execute` only when `TRADINGBOT_MODE` is exactly `execute`, case-insensitive), `WARNING:` lines
  (mode not set / other value -> DRY-RUN). Exit 0/3/4. HALT wins over everything and sends a critical alert +
  writes an aborted summary; a completed EXECUTE run today -> SKIP; unscheduled day -> SKIP unless `--force`
  in DRY-RUN (refused in EXECUTE). CLI `notify alert --level --message` for the prompt's abnormal cases.
- `scripts/cloud_setup.sh`: idempotent .venv (Python 3.11 via uv on the cloud, py launcher locally),
  reinstalls when requirements change; last line `PYTHON=<path>`.
- `prompts/weekly_run.md`: hard rules (no secret/.env, write only memory/ reports/ runs/ state/
  data/sentiment/, only the command printed by should-run, alert + stop on anything abnormal), steps 0-9,
  payload `force-dry-run` / `force-dry-run-full` for Run now, `TRADINGBOT_REHEARSAL=1` = no pull/commit/push,
  commit + push to main always (allowed paths only, .env check, rebase once on rejection).
- Rehearsal 2026-10-10 (fresh headless `claude -p --model sonnet`, REHEARSAL, forced full, DRY-RUN, market
  closed): all steps OK; report + journal + 11 sentiment rows + Discord message. 20 turns, 61 s, ~422k input
  tokens (mostly cache reads), 5.6k output, ~0.25 USD at API prices. Fixed in the prompt afterwards: the
  session read docs/PROGRESS.md (CLAUDE.md startup rule) and quoted it in the report, read source code,
  wrote a temp script in /tmp -> explicit rules added. Known: a forced full run's summary.md still says
  `report: brief` (the prompt follows should-run).
- `docs/ROUTINE_SETUP.md` (checked against the docs on 2026-10-10): GitHub App, environment (Custom network:
  Alpaca paper + data + discord.com, no live host), Alpaca keys as a network secret with placeholder env
  vars (UNVERIFIED: two custom headers + header override -> mandatory check, fallback env vars), Discord
  webhook as env var (token in the URL path: a network secret cannot carry it), setup script, routine,
  gradual rollout, pause / HALT / usage.
- `README.md` for the CV (English). Rehearsal report kept as `docs/examples/weekly_report_example.md`;
  the rehearsal's runs/ folder, journal entry and the 11 sentiment rows were deleted (not real data).
- Correction (user, 2026-10-10): `test-order --execute` has NOT run yet; it is a precondition for EXECUTE.
- Open: Linux path of `cloud_setup.sh` and the network-secret behaviour are only testable with the first
  cloud Run now.
