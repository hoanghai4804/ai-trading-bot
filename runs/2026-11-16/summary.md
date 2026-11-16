MODE: EXECUTE - orders were sent to the Alpaca PAPER account
report: full (scheduled=True)
performance: since last run (2026-11-09) +0.00% vs SPY -0.49% | since start (2026-10-19) -0.25% vs SPY -3.06%
position JNJ    qty=17 avg=287.62 pl=-1.19 (-0.02%) stop=273.90
run today=2026-11-16 as_of=2026-11-15 market_open=True signal_window=5 sessions
equity=99,749.60 cash=99,749.60 positions=- new_positions_this_week=0
breakers: halt=False block_new=False prev_week=+0.00% drawdown=0.25%
signals=1: buy JNJ
approved buy  JNJ    qty=17 ref=287.53 stop=273.90 (position_cap) | MA20 crossed above MA50 3 session(s) ago
exec filled    buy  JNJ    qty=17 stop=273.90 id=tb-2026-11-16-JNJ-buy 
orders_sent=1
execution quality: 1 new fill(s); market fills all-time +2.2 bps over 2 (backtest assumes 5 bps); stop fills 1
shadow check: OK (live == backtest path: buy JNJ)
next: write reports/weekly/2026-11-16.md from prompts/templates/report_full.md; sentiment -> runs/2026-11-16/sentiment.json then `record-sentiment`
