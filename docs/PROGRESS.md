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
| 7 | Backtest (walk-forward, costs, 3 benchmarks) | todo | |
| 8 | Memory, reports, Discord | todo | |
| 9 | Scheduled-run prompt + cloud routine guide | todo | |

## Next step (resume here)
- Session 2026-10-10 (Saturday): extra risk tests (2203f5b) + phase 6 (eacb9c2) committed and pushed. Next: phase 6b.
- **Monday 2026-10-12 after 15:30 Paris (market open)**: run `python -m tradingbot test-order --execute` once
  (1 SPY share, OTO stop, verify, close). It is the first real check that Alpaca accepts our OTO request
  (market + GTC + stop_loss). Then `run` (dry-run) once with the market open.
- Then: **phase 7 — Backtest**.
- Phase 7 requirement (user): the 5% cap binds -> at most ~55% of equity invested. The backtest report must show
  average invested fraction (exposure) and compare on Sharpe and max drawdown, not only absolute return.
  Decide on loosening the 5% cap only after phase 7.
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
  - Alpaca cancels GTC orders after 90 days: stops of positions held longer disappear. Currently only alerted
    ("held without an open stop"); auto re-placing is a later improvement.
  - Halt while the market is closed: HALT.lock is written but nothing is sold (critical alert, manual action).
  - Alerts go to stderr until Discord (phase 8). decisions.json / summary / last_run.json / local equity log
    are phase 8.
