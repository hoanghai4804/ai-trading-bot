<!--
Template báo cáo BRIEF (lần chạy phụ hoặc chạy tay ngoài lịch). KHÔNG WebSearch, KHÔNG điểm cảm xúc.
Mọi con số lấy từ runs/<date>/summary.md + summary.json. Lưu tại reports/weekly/<YYYY-MM-DD>.md,
rồi chạy `python -m tradingbot notify report`. Xóa mọi chú thích HTML khi viết.

CÁCH VIẾT: giống prompts/templates/report_full.md (đọc phần "CÁCH VIẾT" và bảng dịch mã lý do ở đó):
tiếng Việt đời thường, không ký hiệu code, không bảng, không tiêu đề ###, in đậm mã và con số chính.
-->
# 📊 Báo cáo ngắn <DD/MM/YYYY>

<!-- Dòng cảnh báo (nếu có) và một dòng chế độ (🟢 chế độ thật trên paper / 🟡 chạy thử / 🔴 bị dừng). -->

## 📌 Kết quả
<!-- Tổng tài sản, tiền mặt, lời/lỗ từ lần chạy trước và từ đầu so với SPY.
     Lần chạy bị dừng -> lý do bằng lời và bạn cần làm gì. -->

## 🧾 Lệnh
<!-- Mỗi lệnh một dòng (mua/bán, mã, số cổ phiếu, giá, mức cắt lỗ). Không có -> "Không có lệnh nào." -->

## 🚫 Lệnh bị chặn
<!-- Mã + lý do bằng lời (bảng dịch). Không có -> "Không có." -->

## 🛡️ Cảnh báo
<!-- Cảnh báo, kiểm tra chéo với backtest, chênh lệch giá khớp bất thường — bằng lời. Không có -> "Không có." -->
