# Kế hoạch: AI trading bot trên Alpaca PAPER (giai đoạn 1)

## Context
Thư mục `D:\Ai-trading-bot` hiện chỉ có `.env` (chứa `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`), chưa có git.
Mục tiêu: hệ thống chạy 1 lần/tuần (thứ Hai), Python ra mọi quyết định tính toán, Claude chỉ đọc
bản tóm tắt gọn + tìm tin + viết báo cáo tiếng Việt. Giai đoạn 1 chỉ kiểm chứng hệ thống chạy ổn định.

## Các quyết định đã chốt
| Chủ đề | Quyết định |
|---|---|
| Python | 3.11, venv `.venv` (`py -3.11 -m venv .venv`), pytest |
| Watchlist | AAPL, MSFT, NVDA, AMZN, GOOGL, META, JPM, XOM, JNJ, PG, SPY |
| Tín hiệu mua | MA20 cắt lên MA50 trong 5 phiên gần nhất **và** hiện MA20 > MA50 |
| Tín hiệu bán | Đang cầm và MA20 < MA50 → bán market thứ Hai, hủy stop cũ |
| Giá tham chiếu | Giá giao dịch mới nhất lúc gửi lệnh (backtest: giá open phiên vào lệnh) |
| Stop loss | Cố định = giá tham chiếu − 2×ATR(14) (ATR tính đến đóng cửa thứ Sáu), đặt 1 lần, không trailing |
| Sizing | qty = floor(min(1%·equity / (ref − stop), 5%·equity / ref)), không vượt cash (không đòn bẩy) |
| Kiểm tra giá | Bỏ lệnh + ghi lý do nếu \|ref / close thứ Sáu − 1\| > `max_gap_pct` (config, 3%) hoặc stop ≥ ref; backtest dùng cùng hàm |
| Lệnh | Market + stop GTC gắn sẵn (OTO) qua alpaca-py |
| Vốn | Toàn bộ equity tài khoản paper |
| Tương quan | Corr lợi suất ngày 60 phiên > 0.8 với vị thế đang có hoặc lệnh vừa duyệt cùng tuần → chặn |
| Dữ liệu | Alpaca historical daily bars (adjustment=all), cache parquet local, feed cấu hình được (SIP, fallback IEX) |
| Chi phí backtest | Trượt giá 5 bps/chiều + phí SEC/TAF khi bán; stop bị gap → khớp ở giá open |
| Walk-forward | Train 3 năm / test 6 tháng, lưới fast {10,20,30} × slow {50,100}, chọn theo Sharpe; báo cáo song song MA20/50 cố định |
| Lịch chạy | Cấu hình trong `config.yaml` (xem mục Tần suất); mặc định chỉ thứ Hai, full report |
| Ngày lễ | Python kiểm tra `clock`; sàn đóng → vẫn tính toán + báo cáo, không gửi lệnh |
| Git | Repo GitHub PRIVATE `ai-trading-bot` (tạo bằng gh ngay sau giai đoạn 1, remote `origin`, nhánh `main`); push sau mỗi giai đoạn đã commit |
| Thông báo | Discord webhook (`DISCORD_WEBHOOK_URL`), Python gửi, lỗi gửi không làm hỏng lần chạy |

## Tần suất chạy cấu hình được (không viết cứng)
```yaml
schedule:
  timezone: America/New_York
  runs:                       # thêm dòng = chạy thêm ngày, không sửa code
    - {weekday: MON, report: full}
    # - {weekday: WED, report: brief}
```
- Routine cloud cron **chỉ thứ Hai, 15:07 UTC** (`7 15 * * 1`; = 11:07 EDT / 10:07 EST, luôn sau giờ mở cửa).
  Không chạy các ngày khác để tiết kiệm hạn mức Pro. `should-run` + `schedule` vẫn giữ trong config làm lớp bảo vệ
  và để mở rộng sau: thêm ngày = sửa config + thêm ngày vào cron (ghi rõ trong prompt).
- **Giới hạn theo tuần lịch** (ISO week, giờ New York), tính từ **lịch sử lệnh trên Alpaca**, không theo số lần chạy:
  - Số vị thế mới/tuần = số lệnh BUY đã khớp hoặc đang chờ của bot (nhận diện qua tiền tố `client_order_id`) trong tuần hiện tại.
  - Lỗ tuần trước = equity cuối tuần lịch trước so với cuối tuần trước đó, từ portfolio history daily của Alpaca.
- **Cửa sổ tín hiệu**: `cross_lookback_sessions` (mặc định 5) trong config; không mua lại mã đã bị đóng vị thế sau
  ngày giao cắt (tránh vào lại sau khi dính stop với cùng một tín hiệu cũ).
- **Báo cáo 2 chế độ**: `full` (8 mục, có tìm tin) và `brief` (kết quả, lệnh, lệnh bị chặn, cảnh báo — không WebSearch).
  Chế độ lấy từ `schedule.runs`, ghi vào `summary.json` để prompt biết làm gì.

## Idempotency (chạy 2 lần liền không đặt lệnh trùng)
- `client_order_id` tất định: `tb-<YYYY-MM-DD>-<SYMBOL>-<buy|sell>`; Alpaca từ chối id trùng, và executor tự kiểm
  trước: đã có vị thế / lệnh mở / lệnh cùng id hôm nay → bỏ qua, ghi `skipped: already_submitted`.
- Lệnh bán: bỏ qua nếu đã có lệnh SELL đang chờ hoặc vị thế đã đóng.
- File khóa phiên `state/run.lock` (PID + thời điểm, tự hết hạn sau 30 phút) chống 2 lần chạy đồng thời.
- Ghi `state/last_run.json` sau khi xong; equity log upsert theo ngày (không nhân đôi dòng).
- Test: chạy `run` 2 lần với broker mock → lần 2 gửi 0 lệnh.

## Trạng thái bền vững trên cloud
- **Commit + push lên nhánh chính sau mỗi lần chạy** (không nằm trong `.gitignore`): `state/HALT.lock`,
  `state/equity_history.csv`, `state/last_run.json`, `memory/`, `reports/`, `runs/`.
- **Không commit**: `state/run.lock`, `data/cache/`, `logs/`, `.env`, `.venv`.
- Ghi rõ trong CLAUDE.md và `prompts/weekly_run.md`; prompt bắt đầu bằng `git pull` và kết thúc bằng commit + push,
  kể cả khi lần chạy lỗi hoặc bị HALT (để trạng thái không bị mất).

## Quy trình bán (an toàn)
Hủy lệnh stop → bán market → nếu bán thất bại (bị từ chối/lỗi API/không khớp sau thời gian chờ): đặt lại stop GTC
cũ cho số lượng còn lại + `alert()` Discord + ghi vào decisions. Test với broker mock: bán OK, bán lỗi → stop được
đặt lại + alert được gọi, đặt lại stop cũng lỗi → alert mức nghiêm trọng.

## Discord (Python gửi, không phải Claude)
- `src/tradingbot/notify/discord.py`, URL từ `DISCORD_WEBHOOK_URL`, không bao giờ log URL (che khi in lỗi).
- `notify report --path <file.md>`: tin nhắn tóm tắt < 2000 ký tự dựng từ `summary.json` (giá trị danh mục,
  lời/lỗ tuần & từ đầu so với SPY, lệnh mới, lệnh bị chặn, cảnh báo) + đính kèm file báo cáo .md (multipart).
  Nếu Claude chưa viết được báo cáo → đính kèm `summary.md` thay thế.
- `alert()` gửi ngay khi: exception trong `run` (bọc ở `cli.py`), circuit breaker kích hoạt, HALT.lock được tạo
  hoặc phát hiện, guard paper chặn.
- Mọi lỗi gửi (timeout 10s, retry 2 lần, HTTP 429 tôn trọng `retry_after`) chỉ ghi `logs/notify.log`, không raise;
  báo cáo luôn được lưu trong `reports/` trước khi gửi.
- `python -m tradingbot notify-test`: gửi một tin nhắn thử.
- Test: mock HTTP — cắt độ dài < 2000, lỗi mạng không raise, URL không xuất hiện trong log.

## Nguyên tắc thiết kế xuyên suốt
- **An toàn paper**: `TradingClient(paper=True)` hard-code; dừng ngay nếu biến môi trường/URL chứa endpoint live
  (`api.alpaca.markets` không có `paper`), hoặc `account_number` không bắt đầu bằng `PA`, hoặc config có `mode: live`.
  Không bao giờ log key; `.env` đọc bằng python-dotenv chỉ khi file tồn tại (cloud dùng env vars).
- **Chống look-ahead**: mọi hàm nhận `as_of`; dữ liệu cắt `<= as_of` (thứ Sáu đóng cửa trước đó). Có test
  "thêm dữ liệu tương lai không làm đổi tín hiệu quá khứ". Backtest vào lệnh ở giá open phiên sau tín hiệu.
- **Một đường code**: backtest và live dùng chung `Strategy` + `RiskManager` → kết quả backtest phản ánh đúng logic live.
- **Risk manager độc lập**: nhận đề xuất từ strategy, có quyền phủ quyết; lệnh thoát (giảm rủi ro) luôn được phép.
  Mọi lệnh bị chặn ghi vào `decisions.json` kèm lý do.
- **Circuit breakers**: tuần lịch trước lỗ > 5% (từ portfolio history Alpaca) → không mở mới cả tuần này.
  Drawdown ≥ 10% từ đỉnh (đỉnh = max của equity log và portfolio history daily của Alpaca) → đóng hết, hủy lệnh,
  ghi `state/HALT.lock` (lý do, thời điểm, số liệu); mọi lần chạy sau dừng ngay khi thấy file này.
  Giới hạn: DD chỉ kiểm tra lúc chạy hằng tuần, giữa tuần chỉ stop loss bảo vệ (ghi rõ trong CLAUDE.md).
- **Ưu tiên khi > 3 tín hiệu**: giao cắt gần nhất trước, hòa thì theo thứ tự alphabet (tất định, test được).
- **Tiết kiệm token**: Python xuất `runs/<date>/summary.md` (~40 dòng) là thứ duy nhất Claude cần đọc từ phần tính toán.

## Cấu trúc thư mục
```
Ai-trading-bot/
├── CLAUDE.md                  # <150 dòng: cốt lõi + đường dẫn
├── README.md                  # cho CV: kiến trúc, cách chạy, kết quả backtest
├── .gitignore  .env.example   # ignore .env, .venv, data/cache, logs, __pycache__; example liệt kê 3 tên biến, không giá trị
├── pyproject.toml  requirements.txt
├── config/config.yaml         # MỌI tham số: watchlist, strategy, risk, costs, backtest, schedule
├── src/tradingbot/
│   ├── cli.py                 # python -m tradingbot {check|run|backtest|compact} [--dry-run]
│   ├── config.py              # load + validate (pydantic)
│   ├── safety.py              # paper guard, HALT.lock
│   ├── broker/alpaca_client.py      # account, positions, orders, clock, portfolio history
│   ├── data/market_data.py          # fetch + cache bars, cắt theo as_of
│   ├── indicators/technical.py      # SMA, ATR, returns, correlation (hàm thuần)
│   ├── strategies/base.py           # Strategy ABC + Signal dataclass
│   ├── strategies/ma_crossover.py
│   ├── strategies/registry.py       # tên trong config → class
│   ├── risk/sizing.py  risk/circuit_breakers.py  risk/manager.py
│   ├── execution/executor.py        # dry-run in lệnh / paper gửi OTO; đối soát lệnh stop giữa tuần
│   ├── backtest/engine.py walk_forward.py benchmarks.py metrics.py costs.py
│   ├── reporting/summary.py         # summary.md + decisions.json
│   ├── memory/compact.py            # giữ 4 tuần chi tiết, cũ hơn chuyển sang archive
│   ├── scheduling.py                # should-run, chế độ full/brief, tuần lịch (ISO, NY)
│   └── notify/discord.py            # tóm tắt + đính kèm, alert, test; không bao giờ raise
├── tests/                     # test_safety, test_indicators, test_strategy, test_risk, test_executor,
│                              # test_backtest, test_no_lookahead, test_memory (dùng dữ liệu giả, không gọi mạng)
├── memory/  strategy.md (có mục "Ý tưởng giai đoạn sau": HMM forward-only, GARCH, ADF, mean reversion, pairs)
│            portfolio.md journal.md lessons.md (+ journal_archive.md, không đọc mặc định)
├── state/   equity_history.csv last_run.json [HALT.lock]
├── runs/<YYYY-MM-DD>/summary.md decisions.json
├── reports/weekly/<YYYY-MM-DD>.md   reports/backtest/
└── prompts/weekly_run.md      # chạy được trong phiên Claude Code mới hoàn toàn
```

## Các giai đoạn (mỗi giai đoạn: test pass → DỪNG, tóm tắt tiếng Việt, chờ duyệt → commit → push)
1. **Khung project**: `git init`, `.gitignore` (kiểm tra `.env` bị ignore *trước* commit đầu), venv, pyproject,
   requirements, config.yaml + loader, CLAUDE.md. Test: load/validate config.
2. **Kết nối Alpaca**: `safety.py`, `alpaca_client.py`, lệnh `check` (in equity, trạng thái sàn, không in key).
   Test: guard chặn cấu hình live (mock), lock file chặn chạy.
3. **Dữ liệu & chỉ báo**: fetch + cache bars, SMA/ATR/corr. Test: giá trị chỉ báo trên dữ liệu tự tạo, cắt as_of.
4. **Chiến lược**: interface `Strategy.generate_signals(bars, as_of, positions) -> list[Signal]`, MA crossover, registry.
   Test: các ca cắt lên/xuống, cắt >5 phiên trước, no-lookahead.
5. **Risk manager**: sizing, 5%/1%, cash, 3 lệnh/tuần, tương quan, long-only, circuit breakers, HALT.lock.
   Test từng luật + ca kết hợp, mọi lệnh bị chặn có lý do.
6. **Thực thi**: dry-run (mặc định) và paper; lấy giá tham chiếu + kiểm tra lệch giá; OTO market+stop;
   quy trình bán an toàn (hủy stop → bán → lỗi thì đặt lại stop + alert);
   đối soát lệnh stop đã khớp giữa tuần; bỏ qua gửi lệnh khi sàn đóng. Test với client mock; chạy dry-run thật 1 lần.
7. **Backtest**: engine theo lịch tuần, chi phí, walk-forward, 3 benchmark (SPY B&H, SMA200 trên SPY,
   ngẫu nhiên cùng luật rủi ro × N seed → trung vị/phân phối), metrics. Xuất `reports/backtest/`.
   Test: chi phí, gap qua stop, metrics trên chuỗi biết trước đáp án.
8. **Bộ nhớ, báo cáo & Discord**: `summary.md`/`summary.json`/`decisions.json`, template full (8 mục) và brief,
   `compact` (4 tuần), equity log, `notify` + `notify-test` + alert. Test như mục Discord.
9. **Prompt chạy định kỳ**: `prompts/weekly_run.md` (`should-run` → kiểm tra HALT → cài deps → `run` → đọc summary
   + memory → nếu full: ≤4 lượt WebSearch tin tức/sự kiện → viết báo cáo → `notify report` → cập nhật memory →
   `compact` → commit/push),
   hướng dẫn nối repo private (đã có từ giai đoạn 1) với routine cloud + env vars.

## Verification
- `pytest -q` pass ở mỗi giai đoạn (test không gọi mạng).
- `python -m tradingbot check` xác nhận kết nối paper (account `PA...`).
- `python -m tradingbot run --dry-run` in đầy đủ lệnh dự kiến + lệnh bị chặn, không gửi gì; chạy 2 lần liền → không lệnh trùng.
- `python -m tradingbot notify-test` → tin nhắn thử hiện trên Discord điện thoại.
- `python -m tradingbot backtest` sinh báo cáo so với 3 benchmark.
- Chạy thử toàn bộ prompt hằng tuần một lần thủ công ở chế độ dry-run trước khi bật routine.
