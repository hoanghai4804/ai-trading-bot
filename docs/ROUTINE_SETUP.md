# Hướng dẫn cài routine cloud (Claude Code) cho bot giao dịch

Đã đối chiếu với tài liệu Claude Code ngày 2026-10-10:
[Routines](https://code.claude.com/docs/en/routines),
[Cloud environments](https://code.claude.com/docs/en/cloud-environments),
[Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web).
Routines đang ở **research preview**: tên nút, giới hạn và hành vi có thể đổi. Nếu giao diện khác mô tả
bên dưới, làm theo tài liệu mới nhất và cập nhật file này.

Tổng quan: mỗi thứ Hai, routine mở một phiên Claude Code mới trên cloud (model Sonnet), clone repo
`ai-trading-bot`, làm đúng `prompts/weekly_run.md` (should-run → run → báo cáo → Discord → memory →
commit + push lên `main`). Python ra mọi quyết định; chế độ giao dịch do biến `TRADINGBOT_MODE` quyết định.

---

## 0. Trước khi bắt đầu
- Gói **Pro** (routines có trên Pro/Max/Team/Enterprise). Đăng nhập claude.ai bằng tài khoản dùng cho Claude Code.
- Phase 9 đã được commit và push lên `main` (routine clone từ GitHub, không phải từ máy bạn).
- `test-order --execute` chưa bắt buộc để cài routine (routine chạy DRY-RUN), nhưng là điều kiện để bật
  EXECUTE (mục 5.3).
- `git ls-files | grep -x .env` phải rỗng (`.env` không bao giờ lên GitHub).

## 1. Kết nối GitHub (repo private)
Routine cần quyền clone và push repo private. Chọn **một** trong hai cách:

**Cách A — Claude GitHub App (khuyên dùng):**
1. Mở <https://claude.ai/code>, làm theo phần onboarding GitHub, hoặc cài app tại <https://github.com/apps/claude>.
2. Ở bước chọn repository, chọn **Only select repositories** → `ai-trading-bot` (chỉ repo này).
3. Quyền push đi qua proxy GitHub của Anthropic; token GitHub không bao giờ nằm trong máy ảo.

**Cách B — `/web-setup`:** trong Claude Code ở máy bạn (đã `gh auth login`), gõ `/web-setup`: token `gh`
được gửi lên tài khoản Claude. Cách này cho phép truy cập mọi repo mà token `gh` truy cập được (rộng hơn cách A).

Lưu ý:
- Commit của routine mang tên tài khoản GitHub của bạn.
- Routine được phép push thẳng lên `main` vì prompt yêu cầu (mặc định Claude push lên nhánh `claude/...`).
  Nếu sau này bạn bật branch protection cho `main`, phải cho phép tài khoản này push, nếu không bước
  commit/push sẽ thất bại (prompt sẽ alert "git push failed").
- Nếu kết nối GitHub hết hạn, routine bỏ qua các lần chạy tối đa 72 giờ rồi tự tắt; kết nối lại rồi bật lại.

## 2. Tạo cloud environment
Mở <https://claude.ai/code> → biểu tượng đám mây (tên environment, ví dụ **Default**) → **Add environment**
(hoặc sửa một environment bằng biểu tượng bánh răng). Đặt tên ví dụ `tradingbot`.

### 2.1 Network access
Chọn **Custom**, tích **Also include default list of common package managers** (cần cho PyPI và để `uv`
tải Python 3.11 từ GitHub), và điền **Allowed domains**:
```
paper-api.alpaca.markets
data.alpaca.markets
discord.com
```
- `paper-api.alpaca.markets`: tài khoản/lệnh PAPER. `data.alpaca.markets`: dữ liệu giá (bars, latest trade).
- `discord.com`: webhook + file đính kèm.
- **Không** thêm `api.alpaca.markets` (máy chủ giao dịch THẬT): thêm một lớp an toàn ở mức mạng, bên cạnh
  guard paper trong code.

### 2.2 Khóa Alpaca — network secret (cách tài liệu khuyên cho Pro/Max)
Biến môi trường của environment "ai dùng environment cũng đọc được"; với gói Pro, tài liệu khuyên lưu khóa API
dưới dạng **network secret**: proxy của Anthropic gắn khóa vào request **sau khi** request rời máy ảo, nên khóa
không bao giờ nằm trong phiên (Claude không thể đọc nó).

1. Lưu environment trước (network secret chỉ thêm được vào environment đã tồn tại), mở lại bằng **Edit**.
2. Mục **Network secrets** → **Add secret**:
   - **Name**: `Alpaca paper`
   - **Allowed websites**: `paper-api.alpaca.markets` và `data.alpaca.markets` (KHÔNG thêm `api.alpaca.markets`)
   - **Credential type**: giữ kiểu header; **Custom headers**: hai dòng, **xóa Prefix** (Alpaca nhận giá trị trần):
     - Name `APCA-API-KEY-ID` → Value = API key paper
     - Name `APCA-API-SECRET-KEY` → Value = secret key paper
   - **Connect**. Không xem lại được giá trị sau khi lưu; muốn đổi thì xóa và thêm lại.
3. Trong **Environment variables**, đặt giá trị giữ chỗ (không phải bí mật) để code biết là đã có khóa:
   ```
   ALPACA_API_KEY=proxy-injected
   ALPACA_SECRET_KEY=proxy-injected
   ```
   alpaca-py gửi header với giá trị giữ chỗ, proxy thay bằng khóa thật.
4. **Kiểm tra bắt buộc** (bước 5.1 bên dưới): lần Run now đầu tiên phải in được `account=PA...`. Nếu lỗi
   401/403 (form không cho hai header, hoặc proxy không thay header có sẵn) → dùng **phương án dự phòng**:
   xóa secret, đặt khóa paper thật vào **Environment variables** (`ALPACA_API_KEY=...`, `ALPACA_SECRET_KEY=...`).
   Rủi ro chấp nhận được vì đây là khóa PAPER (không có tiền thật) và chỉ bạn dùng environment này;
   đổi khóa trên Alpaca nếu nghi bị lộ.

> Mình chưa thể tự kiểm chứng hai điểm: form có cho nhiều dòng header không, và proxy có ghi đè header đã có
> không. Tài liệu chỉ mô tả một dòng header kiểu `Authorization: Bearer`. Vì vậy mới có bước kiểm tra 4.

### 2.3 Discord webhook — biến môi trường
Token của webhook nằm **trong đường dẫn URL** (`/api/webhooks/<id>/<token>`), mà network secret chỉ gắn vào
header → không dùng network secret được. Theo tài liệu, khóa mà proxy không gắn được thì để trong biến môi trường:
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```
Rủi ro: ai lộ URL có thể gửi tin vào kênh đó (không đọc được gì, không chạm tới Alpaca). Code không bao giờ log
URL (`logs/notify.log` che URL); prompt cấm đọc/in biến này. Nếu lộ: xóa webhook trong Discord, tạo cái mới.

### 2.4 Các biến khác
```
BASH_DEFAULT_TIMEOUT_MS=600000
```
(lệnh `run` có thể chờ khớp lệnh vài phút; mặc định Bash chỉ chờ 2 phút).
**KHÔNG đặt `TRADINGBOT_MODE` lúc đầu** → mọi lần chạy là DRY-RUN, báo cáo có cảnh báo ở đầu.

Toàn bộ khối **Environment variables** lúc đầu (định dạng `.env`):
```
ALPACA_API_KEY=proxy-injected
ALPACA_SECRET_KEY=proxy-injected
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
BASH_DEFAULT_TIMEOUT_MS=600000
```

### 2.5 Setup script
Ô **Setup script** (chạy bằng root trên Ubuntu 24.04 trước khi Claude khởi động; kết quả được cache ~7 ngày):
```bash
#!/bin/bash
# Python 3.11 for the trading bot (the image ships another Python 3.x). Never fails the session.
uv python install 3.11 || true
```
Phần cài thư viện nằm trong repo (`scripts/cloud_setup.sh`, prompt gọi ở bước 2): tạo `.venv` Python 3.11,
cài `requirements.txt` (đã ghim phiên bản) + package. Làm vậy vì setup script không chắc chạy trong thư mục
repo, và requirements đổi thì lần chạy sau tự cài lại.

## 3. Tạo routine
<https://claude.ai/code/routines> → **New routine** (hoặc Desktop app → Code → Routines → New routine → **Cloud**).
- **Name**: `Trading bot weekly (paper)`
- **Instructions/prompt**: dán **toàn bộ** nội dung `prompts/weekly_run.md`.
  Mỗi lần sửa file này trong repo, nhớ dán lại vào routine.
- **Model** (ô chọn trong khung prompt): **Sonnet**.
- **Repositories**: `ai-trading-bot`.
- **Environment**: `tradingbot` (mục 2).
- **Trigger**: **Schedule** → Weekly, thứ Hai. Giờ nhập theo **múi giờ máy bạn** và được giữ theo giờ địa
  phương. Mục tiêu 15:07 UTC = **17:07 giờ Paris (mùa hè)**, 16:07 (mùa đông). Muốn đúng cron
  `7 15 * * 1`: chọn preset gần nhất rồi trong CLI gõ `/schedule update` và đặt cron; kiểm tra giờ
  "next run" hiển thị. Chỉ cần chạy sau giờ mở cửa (13:30 UTC mùa hè Mỹ / 14:30 UTC mùa đông) — cả hai
  lựa chọn đều ổn. Tránh đúng phút :00 (có thể trễ vài phút).
- **Connectors**: **xóa hết** (Gmail, Drive, Calendar, Docs...). Routine chạy không hỏi quyền; connector nào
  còn thì Claude dùng được mọi tool của nó. Bot không cần connector nào.
- **Create**.

## 4. Ý nghĩa các chế độ
| `TRADINGBOT_MODE` | Kết quả |
|---|---|
| không đặt / rỗng / giá trị khác | DRY-RUN: tính toán + báo cáo, **không gửi lệnh**; cảnh báo ở đầu báo cáo |
| `execute` | EXECUTE: gửi lệnh lên tài khoản **PAPER** (code vẫn chặn mọi endpoint live) |

`should-run` (Python) quyết định RUN/SKIP/HALT và in đúng câu lệnh; Claude chỉ chạy câu lệnh đó.
Ngày không có trong `schedule.runs` → SKIP (không ghi gì). Đã có một lần EXECUTE thành công trong ngày → SKIP.

## 5. Quy trình bật dần
### 5.1 Run now ở DRY-RUN (bất kỳ ngày nào)
Trang chi tiết routine → **Run now**. Ngoài thứ Hai, điền ô text: `force-dry-run` (báo cáo ngắn, không
WebSearch) hoặc `force-dry-run-full` (báo cáo đầy đủ + WebSearch + điểm cảm xúc). Text khác bị bỏ qua.
`--force` bị Python từ chối khi `TRADINGBOT_MODE=execute`.

Kiểm tra:
1. **Transcript** (bấm vào lần chạy): màu xanh chỉ có nghĩa phiên không lỗi hạ tầng, KHÔNG có nghĩa nhiệm vụ
   thành công → đọc transcript: `env-check` đủ `set`, `should-run` ra `RUN mode=DRY-RUN`, lệnh `run` in
   `account=PA...`/MODE DRY-RUN, không có lỗi `403 host_not_allowed`, không có lệnh đọc `.env`/biến bí mật.
2. **GitHub**: một commit `weekly run <date> (DRY-RUN, RUN)` trên `main`, chỉ đụng `runs/ reports/ memory/
   state/ data/sentiment/`.
3. **Discord**: tin nhắn `[DRY-RUN]`/`DRY-RUN` + file báo cáo đính kèm.
4. Báo cáo `reports/weekly/<date>.md` có dòng `⚠️ CẢNH BÁO: TRADINGBOT_MODE chưa đặt...` ở đầu.
5. Mục **Hạn mức** (mục 7): ghi lại mức đã dùng.

### 5.2 Chạy DRY-RUN theo lịch 1–2 tuần
Để routine tự chạy thứ Hai. Mỗi tuần xem Discord + commit. Kiểm tra shadow check OK, không có cảnh báo lạ.

### 5.3 Bật EXECUTE — chỉ khi ĐỦ CẢ HAI điều kiện
1. `test-order --execute` đã chạy **thành công** trên máy bạn khi sàn mở (dòng cuối `RESULT PASS`):
   kiểm chứng thật lần đầu đường gửi lệnh OTO (mua + stop) và đóng lệnh trên Alpaca paper.
2. Routine đã chạy **DRY-RUN trên cloud theo lịch ít nhất 1–2 tuần, không lỗi** (transcript sạch, có commit
   trên GitHub, có tin Discord, shadow check OK, không cảnh báo lạ).

Thiếu một trong hai → KHÔNG đặt `TRADINGBOT_MODE`.
Đủ cả hai: environment `tradingbot` → **Edit** → thêm `TRADINGBOT_MODE=execute` → **Save changes**. Áp dụng
từ phiên mới tiếp theo. Báo cáo kế tiếp phải có dòng đầu `MODE: EXECUTE`, không còn cảnh báo DRY-RUN.

## 6. Tạm dừng, dừng khẩn cấp, HALT.lock
- **Tạm dừng lịch**: trang routine → công tắc on/off ở đầu trang. Cấu hình được giữ nguyên.
- **Về DRY-RUN ngay** (vẫn chạy, không gửi lệnh): xóa biến `TRADINGBOT_MODE` khỏi environment.
- **Giữa tuần** chỉ có stop GTC trên Alpaca bảo vệ vị thế; cần can thiệp gấp thì vào dashboard Alpaca paper.
- **HALT.lock** (drawdown ≥ 10% → bot đã bán hết, ghi `state/HALT.lock`, commit lên GitHub):
  mọi lần chạy sau dừng ngay: `should-run` in `HALT`, gửi alert critical, viết báo cáo ngắn, commit.
  Để chạy lại:
  1. Đọc lý do: `cat state/HALT.lock` (lý do, thời điểm, số liệu) và báo cáo tuần đó.
  2. Kiểm tra trên Alpaca: không còn vị thế/lệnh lạ.
  3. Quyết định tiếp tục → ở máy bạn: `git pull`, `git rm state/HALT.lock`,
     `git commit -m "Resume after HALT review"`, `git push`. Routine **không bao giờ** tự xóa file này.
- **Lỗi push** / **setup lỗi**: xem transcript; setup lỗi để lại `runs/<date>/setup_failed.md` trong commit.

## 7. Xem hạn mức đã dùng
- <https://claude.ai/settings/usage>: mức dùng gói (routine tính chung với chat và Claude Code).
- Mỗi lần chạy là một phiên trong danh sách phiên (claude.ai/code); trang routine liệt kê các lần chạy.
- Giới hạn riêng: 100 lần chạy theo lịch/giờ/tài khoản; Run now/API 30 lần/giờ/routine. Bot chỉ cần 1 lần/tuần.
- Hết hạn mức gói lúc đến giờ chạy → lần chạy bị từ chối (trừ khi bật usage credits) → tuần đó không giao
  dịch; stop GTC vẫn bảo vệ vị thế. Nên tránh dùng hết hạn mức vào sáng thứ Hai.
- Số liệu buổi diễn tập local (2026-10-10, Sonnet, báo cáo full, 2 lượt WebSearch, không lệnh): 20 lượt,
  61 giây, ~422k token input (390k đọc từ cache + 32k ghi cache), 5,6k token output; tương đương ~0,25 USD
  theo giá API (con số tham khảo: gói Pro tính theo hạn mức, không tính tiền theo token).
  Trên cloud sẽ hơi nhiều hơn (cài môi trường, git). Xem mức thực tế sau mỗi lần chạy ở trang usage.

## 8. Khi đổi lịch hoặc thêm ngày chạy
Thêm dòng vào `schedule.runs` trong `config/config.yaml` (ví dụ `{weekday: WED, report: brief}`), commit,
**và** thêm trigger schedule cho ngày đó trong routine. Thiếu một trong hai: `should-run` sẽ SKIP hoặc routine
không chạy.
