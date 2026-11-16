# Journal

Detailed entries for the last 4 weeks, newest first. Older weeks: one line each below; full text in journal_archive.md (do not read by default).

## Older weeks

- 2026-10-19: EXECUTE theo lịch, 1 lệnh mua GOOGL đã khớp, 0 cảnh báo.
- 2026-10-12: DRY-RUN theo lịch (TRADINGBOT_MODE chưa đặt), 0 tín hiệu, 0 lệnh, 0 cảnh báo.
- 2026-10-11: DRY-RUN chạy tay ngoài lịch (force-dry-run), 0 lệnh, 0 cảnh báo.

## 2026-11-16
Tóm tắt: EXECUTE theo lịch, 1 lệnh mua JNJ đã khớp, 0 cảnh báo.
- Tín hiệu mua JNJ (MA20 cắt lên MA50 cách 3 phiên): qty=17, ref=287.53, stop=273.90.
- Không có lệnh bị chặn; breakers đều tắt, drawdown 0.25%.
- Shadow check OK; trượt giá thị trường +2.2 bps tất cả thời gian (backtest giả định 5 bps).
- Equity 99,749.60; +0.00% so với SPY -0.49%; từ đầu -0.25% so với SPY -3.06%.
- Tìm tin 4 lượt: JNJ có tin earnings (lợi nhuận GAAP giảm do chi phí talc, nâng guidance); META/JPM/PG không có nguồn tin cậy.

## 2026-11-09
Tóm tắt: EXECUTE theo lịch, 0 tín hiệu, 0 lệnh, 0 cảnh báo.
- Không có tín hiệu mua; không còn vị thế, 100% tiền mặt.
- Equity 99,749.60; +0.00% so với SPY -1.83%; breakers đều tắt, drawdown 0.25%.
- Shadow check OK; trượt giá thị trường +1.2 bps (backtest giả định 5 bps).
- Tìm tin nghèo nàn (2 lượt), sentiment ghi no_news cho cả 11 mã.

## 2026-11-02
Tóm tắt: EXECUTE theo lịch, 1 tín hiệu (AMZN) bị chặn, 0 lệnh, 0 cảnh báo.
- Mua AMZN bị chặn: gap_too_large (ref 253.14 vs close 240.35 = +5.32%, ngưỡng ±3%).
- Equity 99,749.60; +0.00% so với SPY -0.58%; breakers đều tắt, drawdown 0.25%.
- Shadow check OK; trượt giá thị trường +1.2 bps (backtest giả định 5 bps).
- Tìm tin nghèo nàn (2 lượt), sentiment ghi no_news cho cả 11 mã.

## 2026-10-26
Tóm tắt: EXECUTE theo lịch, 0 tín hiệu, 0 lệnh, 0 cảnh báo.
- Stop GOOGL khớp giữa tuần (14 @ 328.099286, 2026-10-22); hiện không còn vị thế.
- Equity 99,749.60; -0.25% so với SPY -0.19%; breakers đều tắt.
- Shadow check OK; trượt giá lệnh thị trường +1.2 bps (backtest giả định 5 bps).
- Tìm tin nghèo nàn (2 lượt), sentiment ghi no_news cho cả 11 mã.
