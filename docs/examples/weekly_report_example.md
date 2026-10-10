> **Example only — rehearsal, not a real weekly report.** Written on 2026-10-10 (Saturday, market
> CLOSED) by a fresh headless Claude Code session (Sonnet) following `prompts/weekly_run.md` with
> `TRADINGBOT_REHEARSAL=1` and the forced full report (`force-dry-run-full`): DRY-RUN, no order could be
> sent, no git writes. Its run outputs and sentiment rows were deleted afterwards. Section 8 quotes
> `docs/PROGRESS.md`, which the prompt now forbids reading.

# Báo cáo tuần 2026-10-10

> ⚠️ **CẢNH BÁO:** TRADINGBOT_MODE chưa đặt → chạy DRY-RUN, không lệnh nào được gửi.
> ⚠️ **CẢNH BÁO:** Chế độ REHEARSAL (diễn tập): không `git pull`, không commit/push. Chạy thủ công ngoài lịch (forced `force-dry-run-full`, thứ Bảy).

> **MODE: DRY-RUN - simulation, no order was sent**

## 1. Tóm tắt
- Chạy thử DRY-RUN ngày 2026-10-10 (thứ Bảy, thị trường đóng cửa), as_of=2026-10-09, cửa sổ tín hiệu 5 phiên.
- Strategy không có tín hiệu nào (signals=0); không lệnh nào được gửi (orders_sent=0).
- Không có cảnh báo nghiêm trọng.

## 2. Danh mục và hiệu suất so với SPY
- Equity 100,000.00, tiền mặt 100,000.00, 0 vị thế.
- Hiệu suất: chưa có (đây là lần chạy đầu tiên được ghi nhận) — since last run n/a, since start n/a.

## 3. Lệnh đã thực hiện
Không có lệnh nào. Stop khớp giữa tuần: không có.

## 4. Lệnh bị chặn và lý do
Không có lệnh nào bị chặn.

## 5. Tín hiệu và vị thế cần theo dõi
Không có tín hiệu tuần này và không giữ vị thế nào.

## 6. Tin tức và sự kiện
Tin tức và điểm cảm xúc chỉ để tham khảo, KHÔNG ảnh hưởng quyết định giao dịch (Python quyết định). Đã dùng 2/4 lượt WebSearch.
- Lịch FOMC/CPI tuần 12/10: tìm kiếm không xác nhận được ngày cụ thể; nên kiểm tra lịch BLS và Fed.
- JPM và JNJ dự kiến báo cáo lợi nhuận thứ Ba 13/10 trước giờ mở cửa. JPM: EPS kỳ vọng ~5.88, biến động ngụ ý ~±5.3% ([nguồn](https://chartrow.com/quote/jpm/earnings)). JNJ: EPS kỳ vọng ~2.90 ([nguồn](https://chartrow.com/quote/jnj/earnings)). Ngày có thể thay đổi.

| Mã | Score | Confidence | no_news |
|---|---|---|---|
| JPM | 0.0 | 0.2 | không |
| JNJ | 0.0 | 0.2 | không |
| AAPL, MSFT, NVDA, AMZN, GOOGL, META, XOM, PG, SPY | 0.0 | 0.0 | có |

## 7. Rủi ro, cảnh báo và giám sát
- Circuit breakers: halt=False, block_new=False, lỗ tuần trước = unknown, drawdown 0.00%.
- Chất lượng khớp lệnh: 0 lệnh khớp mới; chưa có dữ liệu so với giả định 5 bps.
- Shadow check: OK (live == backtest path: no order).

## 8. Kế hoạch tuần tới và bài học
- Lần chạy theo lịch kế tiếp: thứ Hai 15:07 UTC. Theo PROGRESS: thứ Hai 12/10 sau 15:30 Paris chạy `test-order --execute` một lần (thủ công).
- Không có bài học mới.
