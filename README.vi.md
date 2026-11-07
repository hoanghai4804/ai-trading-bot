# AI trading bot — giao dịch giao cắt MA hằng tuần trên Alpaca PAPER

*English version: [README.md](README.md)*

Một hệ thống giao dịch tự động nhỏ nhưng đầy đủ, được xây dựng để học cách hạ tầng giao dịch thật được làm
cho an toàn: một lõi Python tất định (deterministic) đưa ra mọi quyết định, một bộ quản lý rủi ro độc lập có
quyền chặn (veto) bất kỳ lệnh nào, còn tác tử LLM (Claude Code, chạy như một routine định kỳ trên cloud) chỉ
điều phối lượt chạy, đọc bản tóm tắt ngắn, tìm tin tức và viết báo cáo hằng tuần bằng tiếng Việt.

**Chỉ giao dịch giấy (paper trading).** Không dùng tiền thật — cả về thiết kế lẫn trong code (xem
[Các lớp an toàn](#các-lớp-an-toàn)). Chiến lược được cố ý giữ đơn giản; backtest cho thấy nó **không có lợi
thế (edge)** so với chiến lược vào lệnh ngẫu nhiên (xem [Kết quả backtest](#kết-quả-backtest)). Mục đích của
dự án là phần kỹ thuật xung quanh chiến lược.

## Bot làm gì
Mỗi thứ Hai (15:07 UTC, sau khi thị trường Mỹ mở cửa), một phiên Claude Code mới trên cloud sẽ:
1. kéo repo về, cài môi trường Python 3.11 với phiên bản thư viện cố định, rồi hỏi Python có nên chạy không
   (`should-run`: RUN / SKIP / HALT, chế độ chạy và lệnh chính xác cần dùng);
2. chạy pipeline hằng tuần: nến ngày đến hết thứ Sáu tuần trước → tín hiệu MA20/MA50 → quản lý rủi ro →
   đặt lệnh trên tài khoản Alpaca **paper** (lệnh mua market kèm stop GTC, bán market an toàn);
3. đọc `runs/<date>/summary.md` (~40 dòng, là file tính toán DUY NHẤT mà LLM đọc), tìm tin tức (≤ 4 lượt
   tìm), ghi điểm cảm xúc tin tức (sentiment) cho từng mã (chỉ là dữ liệu, không bao giờ dùng để giao dịch);
4. viết báo cáo, gửi lên Discord, cập nhật các file bộ nhớ, rồi commit + push trạng thái lên git.

Ví dụ đầu ra: [`docs/examples/weekly_report_example.md`](docs/examples/weekly_report_example.md) (một lần
DRY-RUN tập dượt vào ngày thị trường đóng cửa, nên không có tín hiệu và không có lệnh).

## Kiến trúc
```
                         Claude Code routine (cloud, Sonnet) — prompts/weekly_run.md
                         điều phối, tin tức, báo cáo, bộ nhớ; KHÔNG BAO GIỜ quyết định giao dịch
                                            │ CLI
 ┌──────────────────────────────────────────▼───────────────────────────────────────────┐
 │ python -m tradingbot  (src/tradingbot)                                               │
 │                                                                                      │
 │  scheduling ── should-run ──► pipeline.run_weekly(as_of = phiên gần nhất)            │
 │                                   │                                                  │
 │  data/market_data (nến Alpaca, cắt tại as_of, cache) ──► indicators (SMA, ATR, corr) │
 │                                   ▼                                                  │
 │  strategies/ma_crossover ──► Signals ──► risk/manager (độc lập, có quyền chặn) ──►   │
 │       khối lượng, kiểm tra gap, tương quan, giới hạn tuần, cầu dao, HALT.lock        │
 │                                   ▼                                                  │
 │  execution/executor (dry-run | paper): mua OTO + stop, bán an toàn, sổ lưu stop      │
 │                                   ▼                                                  │
 │  reporting (summary.md/json, decisions.json) · monitoring (trượt giá, shadow check)  │
 │  notify/discord (không bao giờ ném lỗi) · memory/compact · sentiment (chỉ dữ liệu)   │
 │                                                                                      │
 │  backtest/engine ── dùng CHUNG Strategy + RiskManager + weekly_decision với live     │
 │       walk_forward · benchmarks (SPY B&H, SMA200, ngẫu nhiên ×50) · chi phí · chỉ số │
 └──────────────────────────────────────────────────────────────────────────────────────┘
 Trạng thái được commit lên git sau mỗi lần chạy: state/ (HALT.lock, stops, nhật ký equity,
 lần chạy cuối, chất lượng khớp lệnh), memory/, reports/, runs/, data/sentiment/.
```

### Quy tắc giao dịch (mọi tham số nằm trong `config/config.yaml`, schema pydantic chặt chẽ)
- Danh mục theo dõi: AAPL MSFT NVDA AMZN GOOGL META JPM XOM JNJ PG SPY (nhiều ngành), chỉ mua (long-only).
- **Mua:** đường MA20 (trung bình 20 phiên) cắt lên trên MA50 (trung bình 50 phiên) trong cửa sổ tín hiệu
  (≥ 5 phiên, được nới rộng nếu lỡ một lượt chạy) và vẫn đang nằm trên. **Thoát:** MA20 < MA50.
- Giá tham chiếu = giao dịch gần nhất lúc đặt lệnh (bị loại nếu cũ hơn 5 phút); bỏ qua nếu giá đã lệch > 3%
  so với giá đóng cửa thô (chưa điều chỉnh) gần nhất. Stop = giá tham chiếu − 2 × ATR(14), cố định, không bao
  giờ dời; nếu stop biến mất thì đặt lại đúng mức cũ.
- Khối lượng = min(rủi ro 1% vốn, giá trị tối đa 5% vốn), giới hạn bởi tiền mặt sẵn có.
- Tối đa 3 vị thế mới mỗi tuần lịch; lệnh mua có tương quan > 0,8 (lợi suất 60 ngày) với một vị thế đang giữ
  hoặc vừa được duyệt sẽ bị chặn.
- Cầu dao (circuit breaker): tuần trước lỗ −5% → không mở vị thế mới; sụt giảm (drawdown) ≥ 10% từ đỉnh →
  bán toàn bộ, ghi `state/HALT.lock`, mọi lượt chạy sau đều dừng cho đến khi con người xoá file này.

## Cấu trúc dự án
```
ai-trading-bot/
├── CLAUDE.md                     # quy tắc cho Claude Code (chỉ paper, không lộ key, bố cục, quyết định chính)
├── README.md / README.vi.md      # file giới thiệu (tiếng Anh / tiếng Việt)
├── pyproject.toml                # định nghĩa package (bố cục src/, chạy bằng `python -m tradingbot`)
├── requirements.txt              # thư viện với phiên bản cố định
├── .env.example                  # tên các biến môi trường cần có (file .env thật không bao giờ commit)
├── .gitignore / .gitattributes   # file bị git bỏ qua (.env, cache, logs, venv) / xuống dòng kiểu LF
│
├── config/
│   └── config.yaml               # TOÀN BỘ tham số: danh mục, chiến lược, rủi ro, chi phí, backtest, lịch, thông báo
│
├── src/tradingbot/
│   ├── __init__.py / __main__.py # đánh dấu package / điểm vào của `python -m tradingbot`
│   ├── cli.py                    # mọi lệnh CLI (check, data, signals, should-run, run, backtest, notify...)
│   ├── config.py                 # schema pydantic chặt cho config.yaml (mode phải là paper, key lạ bị từ chối)
│   ├── credentials.py            # đọc API key từ biến môi trường; khi in object thì key bị ẩn
│   ├── safety.py                 # chốt chặn paper (endpoint live / tài khoản không phải PA -> dừng), HALT.lock, run.lock
│   ├── scheduling.py             # should-run: RUN / SKIP / HALT, DRY-RUN hay EXECUTE, cửa sổ tín hiệu
│   ├── pipeline.py               # run_weekly: tài khoản -> nến -> tín hiệu -> giá -> cầu dao -> rủi ro -> thực thi
│   ├── broker/
│   │   └── alpaca_client.py      # lớp bọc mỏng Alpaca: tài khoản, giờ thị trường, vị thế, lệnh, mua OTO, bán, huỷ
│   ├── data/
│   │   └── market_data.py        # nến ngày cắt tại as_of (+ cache parquet), giá đóng cửa thô, giá giao dịch mới nhất
│   ├── indicators/
│   │   └── technical.py          # SMA, true range, ATR (Wilder), lợi suất ngày, tương quan lợi suất
│   ├── strategies/
│   │   ├── base.py               # dataclass Signal + giao diện Strategy
│   │   ├── ma_crossover.py       # giao cắt MA20/MA50: mua khi vừa cắt lên, thoát khi MA20 < MA50
│   │   └── registry.py           # tên chiến lược trong config -> class tương ứng
│   ├── risk/
│   │   ├── sizing.py             # stop = giá tham chiếu - 2xATR, kiểm tra gap, khối lượng (rủi ro 1%, trần 5%, tiền mặt)
│   │   ├── circuit_breakers.py   # cầu dao: lỗ tuần trước, drawdown từ đỉnh, kích hoạt HALT
│   │   └── manager.py            # RiskManager: duyệt độc lập, chặn lệnh kèm mã lý do
│   ├── execution/
│   │   ├── orders.py             # mã lệnh tất định, đếm vị thế mới trong tuần, lần thoát gần nhất, stop đang mở
│   │   ├── executor.py           # dry-run | paper: mua OTO + stop, bán an toàn, bán hết khi HALT, kiểm tra stop
│   │   ├── stop_registry.py      # state/stops.json: stop gốc của từng vị thế, đặt lại nếu bị mất
│   │   └── test_order.py         # kiểm tra lệnh THỦ CÔNG từ đầu đến cuối (1 SPY + stop rồi đóng); không chạy hằng tuần
│   ├── reporting/
│   │   ├── run_text.py           # nội dung in ra màn hình của một lượt chạy (dòng đầu = MODE)
│   │   ├── summary.py            # runs/<date>/summary.md, summary.json, decisions.json, memory/portfolio.md
│   │   └── equity_log.py         # state/equity_history.csv (equity so với SPY, thêm nguồn đỉnh cho drawdown)
│   ├── monitoring/
│   │   ├── execution_quality.py  # giá khớp so với giá tham chiếu -> trượt giá bps (state/execution_quality.csv)
│   │   └── shadow.py             # chạy lại mỗi quyết định live qua đường backtest, cảnh báo nếu khác nhau
│   ├── notify/
│   │   ├── discord.py            # webhook Discord: báo cáo + cảnh báo, không bao giờ ném lỗi, không log URL
│   │   └── discord_report.py     # báo cáo .md -> các thẻ (embed) Discord, đọc thẳng trên điện thoại, không cần tải
│   ├── memory/
│   │   └── compact.py            # giữ 4 tuần nhật ký, chuyển phần cũ hơn sang journal_archive.md
│   ├── sentiment/
│   │   └── record.py             # kiểm tra + lưu sentiment tin tức (chỉ dữ liệu, không dùng để giao dịch)
│   └── backtest/
│       ├── engine.py             # chạy lại theo tuần với CÙNG code chiến lược/rủi ro như live (weekly_decision)
│       ├── costs.py              # trượt giá (5 bps) + phí SEC/TAF
│       ├── metrics.py            # lợi nhuận, Sharpe, max drawdown, thời gian có vị thế, thống kê giao dịch
│       ├── walk_forward.py       # dò tham số train 3 năm / test 6 tháng, chạy ngoài mẫu
│       ├── benchmarks.py         # mua và giữ SPY, SMA200 trên SPY, vào lệnh ngẫu nhiên x50
│       └── report.py             # reports/backtest/ (report.md, các file CSV, equity.svg)
│
├── tests/                        # pytest, không gọi mạng (HTTP bị chặn trong conftest.py)
│   ├── conftest.py               # fixture dùng chung, chặn mạng
│   ├── fake_broker.py            # broker giả trong bộ nhớ cho test executor/pipeline
│   ├── fixtures/                 # nến thật của tuần 2026-04-27 (test đối chiếu parity)
│   └── test_*.py                 # mỗi module một file test (config, rủi ro, executor, parity backtest, look-ahead...)
│
├── prompts/
│   ├── weekly_run.md             # prompt của routine định kỳ trên cloud (các bước + quy tắc cứng)
│   └── templates/                # report_full.md / report_brief.md (mẫu báo cáo tuần, tiếng Việt)
├── scripts/
│   └── cloud_setup.sh            # cài venv cho routine cloud (chạy lại không sao); dòng cuối PYTHON=<path>
├── docs/
│   ├── PLAN.md                   # kế hoạch dự án đã được duyệt
│   ├── PROGRESS.md               # trạng thái các phase, nhật ký, vấn đề còn mở (điểm tiếp tục công việc)
│   ├── ROUTINE_SETUP.md          # hướng dẫn cài routine Claude Code trên cloud
│   └── examples/                 # báo cáo tuần mẫu
│
├── memory/                       # bộ nhớ của LLM: strategy.md, lessons.md, journal.md, portfolio.md (do Python ghi)
├── reports/
│   ├── backtest/                 # báo cáo backtest, bảng CSV, đường equity
│   └── weekly/<date>.md          # báo cáo tuần do routine viết
├── runs/<date>/                  # mỗi lượt chạy: summary.md (file duy nhất LLM đọc), summary.json,
│                                 #   decisions.json, sentiment.json
├── state/                        # trạng thái lâu dài: HALT.lock, stops.json, equity_history.csv,
│                                 #   last_run.json, execution_quality.csv (run.lock không bao giờ commit)
└── data/
    ├── sentiment/sentiment_log.csv  # lịch sử sentiment tin tức (chỉ dữ liệu)
    └── cache/                    # cache nến (git bỏ qua)
```

## Các lớp an toàn
| Lớp | Cơ chế |
|---|---|
| Chỉ paper | `TradingClient(paper=True)`; dừng ngay nếu gặp endpoint live trong env/config, số tài khoản không bắt đầu bằng `PA`, hoặc `mode != paper`; danh sách mạng cho phép trên cloud không chứa host giao dịch thật |
| Mặc định DRY-RUN | `run` không gửi gì nếu thiếu `--execute`; routine chỉ thêm cờ này khi `TRADINGBOT_MODE=execute`, nếu không báo cáo mở đầu bằng cảnh báo |
| LLM không thể giao dịch | Tác tử chỉ chạy đúng lệnh mà `should-run` in ra; prompt cấm sửa code/config, và lượt chạy có thể tái lập từ `decisions.json` |
| Quản lý rủi ro độc lập | Mỗi lần chặn đều có mã lý do, ghi trong `decisions.json`; lệnh thoát luôn được phép |
| Không nhìn trước tương lai (look-ahead) | Mọi hàm nhận `as_of`; dữ liệu cắt `<= as_of`; test thêm dữ liệu tương lai và kiểm tra tín hiệu quá khứ không đổi |
| Idempotency (chạy lại không trùng lệnh) | `client_order_id` tất định, kiểm tra lệnh mở/vị thế, khoá lượt chạy; chạy hai lần không gửi thêm lệnh nào |
| Bán an toàn | Huỷ stop → xác nhận → bán market → nếu lỗi thì đặt lại stop gốc + cảnh báo |
| Giám sát | Trượt giá khớp lệnh so với giả định 5 bps của backtest; shadow check chạy lại mỗi quyết định live qua đường backtest và cảnh báo nếu khác |
| Bí mật (secrets) | Không bao giờ in hay ghi log; `env-check` chỉ báo set/MISSING; trên cloud, key Alpaca được thiết kế là network secret, do proxy bên ngoài VM chèn vào |
| Cảnh báo | Mọi lần dừng/lỗi → stderr + Discord, kèm một bản tóm tắt "aborted" để lượt chạy luôn được báo cáo |

## Kết quả backtest
Chạy lại theo tuần với **cùng** code chiến lược, quản lý rủi ro và tính khối lượng như live; vào lệnh ở giá mở
cửa thứ Hai, trượt giá 5 bps mỗi chiều + phí SEC/TAF, stop bị gap thì khớp ở giá mở cửa. Dữ liệu: nến ngày
Alpaca SIP đã điều chỉnh 2016-01-04 → 2026-10-09; vốn ban đầu $100.000.

**Toàn giai đoạn 2017-01-01 → 2026-10-09**

| Chiến lược | Lợi nhuận/năm | Sharpe | Max DD | Thời gian có vị thế | Số giao dịch |
|---|---|---|---|---|---|
| MA20/50 (bot này) | +3,5% | 0,76 | 9,5% | 23% | 231 |
| Mua và giữ SPY | +15,3% | 0,89 | 33,8% | 100% | – |
| SMA200 trên SPY | +11,2% | 0,95 | 17,6% | 83% | 13 |
| Vào lệnh ngẫu nhiên ×50, cùng quy tắc rủi ro (trung vị [p5; p95]) | +5,3% [+3,5; +6,5] | 1,08 [0,79; 1,29] | 8,1% | 23% | 232 |

**Ngoài mẫu (out-of-sample) 2019 → 2026 (walk-forward: train 3 năm / test 6 tháng, lưới fast {10,20,30} ×
slow {50,100}):** walk-forward +2,5%/năm, Sharpe 0,66; MA20/50 cố định +3,1%/năm, Sharpe 0,69; ngẫu nhiên
trung vị Sharpe 1,09.

**Kết luận.** Tín hiệu MA20/50 **không** thắng được vào lệnh ngẫu nhiên với cùng quy tắc rủi ro (≈ bách phân
vị thứ 4 của các lần chạy ngẫu nhiên về lợi nhuận và Sharpe trên toàn giai đoạn, thấp hơn mọi lần chạy ngẫu
nhiên ở giai đoạn ngoài mẫu), bộ tham số thắng trong walk-forward thay đổi theo từng cửa sổ (không có tham số
ổn định), và giữ SPY với cùng mức 23% thời gian có vị thế sẽ đạt +42,4% so với +39,2%. Drawdown thấp đến từ
quy tắc rủi ro và mức độ có vị thế thấp, không phải từ tín hiệu. Báo cáo đầy đủ:
[`reports/backtest/report.md`](reports/backtest/report.md).

## Cách chạy
Cần Python 3.11 và một tài khoản Alpaca **paper** (miễn phí). Đường dẫn bên dưới là của Windows; trên Linux
dùng `.venv/bin/python`.
```bash
py -3.11 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt && .venv/Scripts/python -m pip install -e . --no-deps
cp .env.example .env            # ALPACA_API_KEY, ALPACA_SECRET_KEY, DISCORD_WEBHOOK_URL (không bao giờ commit)
.venv/Scripts/python -m pytest  # ~390 test, không gọi mạng
.venv/Scripts/python -m tradingbot env-check   # set / MISSING, không bao giờ in giá trị
.venv/Scripts/python -m tradingbot check       # tài khoản paper + trạng thái thị trường
.venv/Scripts/python -m tradingbot signals     # chỉ đề xuất của chiến lược
.venv/Scripts/python -m tradingbot run         # lượt chạy tuần, DRY-RUN (thêm --execute để đặt lệnh paper)
.venv/Scripts/python -m tradingbot backtest    # reports/backtest/
```
Chạy định kỳ trên cloud: [`docs/ROUTINE_SETUP.md`](docs/ROUTINE_SETUP.md) (routine Claude Code, môi trường,
network secret, triển khai dần DRY-RUN → EXECUTE). Prompt: [`prompts/weekly_run.md`](prompts/weekly_run.md).

## Cách kiểm thử
- pytest, không gọi mạng (HTTP bị chặn trong `conftest.py`); broker giả cho test executor/pipeline.
- Test đối chiếu (parity): một tuần thật chạy qua backtest cho ra đúng quyết định của lượt dry-run live.
- Kiểm tra đột biến (mutation) sau mỗi phase: đổi từng quy tắc một (vd. 5% → 50%, `<` → `<=`, bỏ một lớp
  bảo vệ) để chắc chắn có test bị fail; đột biến nào sống sót thì viết thêm test (tổng cộng hơn 100 đột biến).

## Hạn chế
- Chiến lược chưa chứng minh được lợi thế (xem trên); đây là dự án học tập/kỹ thuật.
- Thiên lệch sống sót (survivorship bias): áp danh mục cổ phiếu lớn của hôm nay cho 2016–2026; dữ liệu chỉ có
  từ 2016.
- Backtest khớp ở giá mở cửa thứ Hai, còn live khớp khoảng 11:07 giờ New York; giá điều chỉnh và giá thô lệch
  nhau quanh ngày giao dịch không hưởng quyền.
- Rủi ro chỉ được kiểm tra mỗi tuần một lần: giữa các lượt chạy chỉ có stop GTC bảo vệ vị thế; nếu HALT xảy ra
  khi thị trường đóng cửa thì phải đợi lượt chạy sau mới bán được. Alpaca huỷ lệnh GTC sau 90 ngày (bot đặt lại
  stop gốc mỗi lượt chạy).
- Giá tham chiếu chỉ dùng giao dịch real-time của IEX (gói dữ liệu miễn phí); tiền mặt không sinh lãi trong
  backtest.
- Routine trên cloud phụ thuộc vào một tính năng đang ở dạng research preview và vào hạn mức sử dụng của gói
  đăng ký.

## Hướng phát triển tiếp
- Đánh giá điểm sentiment tin tức đã ghi so với lợi nhuận tuần kế tiếp sau 1–2 năm dữ liệu.
- Nghiên cứu chiến lược bằng bộ khung hiện có (luôn so với benchmark ngẫu nhiên): bộ lọc chế độ thị trường
  (HMM chỉ nhìn về trước), khối lượng theo biến động (GARCH), hồi quy về trung bình trên các mã không có xu
  hướng (kiểm định ADF).
- Danh mục không bị thiên lệch sống sót, khớp lệnh trong ngày trong backtest, mô hình chi phí giao dịch từ dữ
  liệu khớp lệnh thật.

## Công nghệ sử dụng
Python 3.11, alpaca-py, pandas, numpy, pydantic, pytest · Claude Code (phát triển và routine định kỳ trên
cloud).
