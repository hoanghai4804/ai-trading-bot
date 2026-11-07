<!--
Template báo cáo FULL (thứ Hai). Claude điền từ runs/<date>/summary.md + summary.json + memory/.
Mọi con số lấy từ summary (Python đã tính), KHÔNG tự tính lại. Tối đa 4 lượt WebSearch cho
cả báo cáo lẫn điểm cảm xúc. Không bịa nguồn: chỉ dùng URL có trong kết quả tìm kiếm.
Lưu tại reports/weekly/<YYYY-MM-DD>.md, rồi chạy `python -m tradingbot notify report`
(báo cáo được gửi lên Discord thành các thẻ, đọc thẳng trên điện thoại).
Xóa mọi chú thích HTML khi viết.

CÁCH VIẾT (bắt buộc) — người đọc là người mới, đọc trên điện thoại:
- Tiếng Việt đời thường, câu ngắn, mỗi gạch đầu dòng 1 ý. Mỗi phần tối đa ~8 dòng.
- KHÔNG có ký hiệu code: không dấu `backtick`, không dạng key=value (orders_sent=0, qty=14,
  halt=False), không True/False, không tên biến/mã lý do tiếng Anh (gap_too_large, no_news, as_of,
  block_new...), không mã lệnh (tb-2026-...). Dùng bảng dịch bên dưới.
- KHÔNG dùng bảng (|...|) và không dùng tiêu đề ### : Discord không hiển thị được. Dùng danh sách.
- In đậm mã cổ phiếu và con số quan trọng: **GOOGL**, **14 cổ phiếu**, **+0.39%**.
- Tiền ghi kèm "USD": 99,749.60 USD. Giữ nguyên con số như Python in ra.
- Ngày viết dạng 02/11/2026.

Bảng dịch mã lý do bị chặn (ghi phần tiếng Việt, không ghi mã):
  gap_too_large -> giá đã nhảy quá 3% so với giá đóng cửa gần nhất (tránh mua đuổi)
  correlation -> biến động quá giống một mã đang giữ (tránh dồn rủi ro)
  correlation_unknown -> chưa đủ dữ liệu để kiểm tra độ tương quan
  weekly_limit -> đã đủ 3 vị thế mới trong tuần
  already_held / duplicate -> đang giữ mã này rồi
  no_reference_price -> không có giá mới (dưới 5 phút) để đặt lệnh
  no_raw_close / no_data / atr_unavailable -> thiếu dữ liệu giá
  invalid_stop -> mức cắt lỗ tính ra không hợp lệ
  insufficient_cash / size_zero -> không đủ tiền mặt
  drawdown_halt -> tài khoản giảm từ 10% so với đỉnh, bot dừng giao dịch
  weekly_loss_breaker -> tuần trước lỗ từ 5%, tạm không mở vị thế mới
  stop_breached -> giá đã xuống dưới mức cắt lỗ, bot bán ra
Từ khác: stop -> "mức cắt lỗ"; equity -> "tổng tài sản"; drawdown -> "mức sụt giảm từ đỉnh";
circuit breaker -> "cầu dao an toàn"; shadow check -> "kiểm tra chéo với backtest";
slippage/bps -> "chênh lệch giá khớp" (1 bps = 0.01%).
-->
# 📊 Báo cáo tuần <DD/MM/YYYY>

<!-- Nếu có cảnh báo của bước 7 trong prompt, đặt dòng "> ⚠️ **CẢNH BÁO:** ..." ngay đây. -->
<!-- Một dòng chế độ, chọn theo dòng đầu của summary.md:
     MODE: EXECUTE -> "🟢 **Chế độ thật trên tài khoản giả lập (paper):** lệnh được gửi lên Alpaca, không dùng tiền thật."
     MODE: DRY-RUN -> "🟡 **Chạy thử (DRY-RUN):** bot chỉ tính toán, không gửi lệnh nào." -->

## 📌 Tóm tắt
<!-- 2-4 gạch đầu dòng: tuần này bot đã làm gì (mua/bán gì, hay không làm gì và vì sao),
     có gì cần bạn chú ý không. -->

## 💼 Tài khoản và hiệu suất
<!-- Tổng tài sản, tiền mặt, số mã đang giữ.
     Lời/lỗ từ lần chạy trước và từ đầu, so với SPY (quỹ đại diện thị trường Mỹ), ví dụ:
     "- Từ đầu (19/10/2026): **-0.25%**, trong khi SPY **-0.77%** → bot đang tốt hơn thị trường."
     Mỗi mã đang giữ một dòng:
     "- **GOOGL**: 14 cổ phiếu, giá mua 345.97 USD, đang lời **+0.39 USD (+0.01%)**, mức cắt lỗ 328.11 USD"
     Không giữ mã nào -> "Hiện không giữ cổ phiếu nào, 100% là tiền mặt." -->

## 🧾 Lệnh tuần này
<!-- Mỗi lệnh một dòng: mua/bán, mã, số cổ phiếu, giá, mức cắt lỗ, lý do bằng lời, ví dụ:
     "- ✅ Mua **GOOGL** 14 cổ phiếu, giá 345.93 USD, mức cắt lỗ 328.11 USD — đường trung bình 20 phiên vừa cắt lên đường 50 phiên."
     Có lệnh cắt lỗ tự khớp giữa tuần -> ghi rõ. Không có lệnh -> "Không có lệnh nào." -->

## 🚫 Lệnh bị chặn
<!-- Mỗi lệnh một dòng, lý do theo bảng dịch, kèm con số nếu có, ví dụ:
     "- **AMZN** (mua): giá đã nhảy **+5.32%** so với giá đóng cửa gần nhất (giới hạn 3%), nên bot không mua đuổi."
     Không có -> "Không có lệnh nào bị chặn." -->

## 👀 Cần theo dõi
<!-- Tín hiệu tuần này, mã đang giữ gần mức cắt lỗ, mã sắp có tín hiệu (nếu summary có). Ngắn gọn. -->

## 📰 Tin tức và sự kiện
<!-- Dòng đầu (in nghiêng): "_Tin tức chỉ để tham khảo, không ảnh hưởng quyết định mua bán của bot._"
     Tối đa 4 lượt WebSearch (dùng chung với điểm cảm xúc). Mỗi tin 1-2 dòng + nguồn dạng link:
     [Tên nguồn](URL thật). Lịch sự kiện tuần tới: họp Fed (FOMC), chỉ số lạm phát (CPI),
     báo cáo lợi nhuận của các mã đang giữ.
     Điểm cảm xúc: một dòng tóm tắt bằng lời, ví dụ "Cảm xúc tin tức: tích cực với **NVDA**, tiêu cực
     với **XOM**; các mã khác không có tin đáng tin cậy." (không liệt kê score/confidence). -->

## 🛡️ An toàn và giám sát
<!-- 3-4 dòng bằng lời:
     - Cầu dao an toàn: bình thường / đã kích hoạt (tuần trước lời/lỗ X%, sụt giảm từ đỉnh Y%; ngưỡng 5% và 10%).
     - Cảnh báo: không có / liệt kê bằng lời.
     - Chênh lệch giá khớp: trung bình +1.2 bps (0.012%), backtest giả định 5 bps → tốt.
     - Kiểm tra chéo với backtest: khớp ✅ / lệch ⚠️ (giải thích, KHÔNG tự sửa code). -->

## 🗓️ Tuần tới
<!-- Lần chạy tới (thứ Hai DD/MM/YYYY, 22:07 giờ Việt Nam). Bạn cần làm gì (thường là không).
     Bài học mới (nếu có) -> cũng ghi vào memory/lessons.md. -->
