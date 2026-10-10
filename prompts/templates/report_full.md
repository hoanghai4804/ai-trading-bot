<!--
Template báo cáo FULL (thứ Hai). Claude điền từ runs/<date>/summary.md + summary.json + memory/.
Mọi con số lấy từ summary (Python đã tính), KHÔNG tự tính lại. Tối đa 4 lượt WebSearch cho
cả báo cáo lẫn điểm cảm xúc. Không bịa nguồn: chỉ dùng URL có trong kết quả tìm kiếm.
Lưu tại reports/weekly/<YYYY-MM-DD>.md, rồi chạy `python -m tradingbot notify report`.
Xóa mọi chú thích HTML khi viết.
-->
# Báo cáo tuần <YYYY-MM-DD>

> **<MODE line copy nguyên văn dòng đầu của summary.md>**

## 1. Tóm tắt
<!-- 3-5 dòng: chuyện gì đã xảy ra tuần này, lệnh nào được gửi, có cảnh báo nghiêm trọng không. -->

## 2. Danh mục và hiệu suất so với SPY
<!-- Equity, tiền mặt, số vị thế. Lời/lỗ từ lần chạy trước và từ đầu, so với SPY (summary.performance).
     Bảng vị thế: mã, số lượng, giá vốn, lời/lỗ chưa chốt, stop gốc. -->

## 3. Lệnh đã thực hiện
<!-- Mỗi lệnh: mua/bán, mã, số lượng, giá tham chiếu, giá khớp, stop, lý do (tín hiệu / stop_breached / halt).
     Stop đã khớp giữa tuần (stop_fills). Không có lệnh -> ghi rõ "không có lệnh nào". -->

## 4. Lệnh bị chặn và lý do
<!-- Bảng: mã, mã lý do (code), giải thích ngắn bằng tiếng Việt dễ hiểu cho người mới. -->

## 5. Tín hiệu và vị thế cần theo dõi
<!-- Tín hiệu của strategy tuần này; vị thế gần stop; mã có MA20 sắp cắt MA50 (nếu summary có). -->

## 6. Tin tức và sự kiện
<!-- Tối đa 4 lượt WebSearch (dùng chung với điểm cảm xúc). Mỗi tin: 1-2 dòng + nguồn (URL thật).
     Lịch sự kiện tuần tới: FOMC, CPI, báo cáo lợi nhuận của các mã đang giữ.
     Ghi rõ: tin tức CHỈ để tham khảo, KHÔNG ảnh hưởng quyết định giao dịch (Python quyết định).
     Bảng điểm cảm xúc đã ghi (mã, score, confidence, no_news) - xem runs/<date>/sentiment.json. -->

## 7. Rủi ro, cảnh báo và giám sát
<!-- Circuit breakers (lỗ tuần trước, drawdown). Mọi cảnh báo trong summary.alerts.
     Chất lượng khớp lệnh: trượt giá TB so với giả định 5 bps của backtest.
     Shadow check: OK hay MISMATCH (nếu lệch: giải thích khả năng, KHÔNG tự sửa code). -->

## 8. Kế hoạch tuần tới và bài học
<!-- Lần chạy tới, điều cần người dùng làm (nếu có, ví dụ HALT.lock). Bài học mới -> cũng ghi vào memory/lessons.md. -->
