# 🎬 Flow AI Video Studio (Veo 3.1 / Omni 1.1 Flash)

Công cụ tự động hóa tạo Video **Google Flow AI (`https://flow.google.com/`)** đa tài khoản, đa Proxy IP (1 Acc = 1 IP riêng), mỗi cảnh cố định **8 giây**, hỗ trợ **Ảnh/Video tham chiếu + Mô tả tham chiếu + Prompt** và **Hậu kỳ tự động FFmpeg + Antigravity 2.0 Hook**.

## ✨ Tính năng nổi bật
1. **Đa Tài Khoản & Đa Proxy IP Độc Lập (`LocalProxyBridge`):**
   - Mỗi tài khoản chạy trên 1 Profile Chrome riêng biệt và 1 Proxy IP độc lập.
   - Khi bấm **Mở Chrome Đăng Nhập**, trình duyệt mở hiện trực tiếp (`headless=False`) để đăng nhập và tự động bắt Link Project (`https://flow.google.com/project/...`) trên mọi Tab.
   - Sau khi đã lưu Cookie, hệ thống hỗ trợ **Chạy Ngầm (`--headless=new`)** giúp tiết kiệm RAM/CPU.
2. **Tự Động Phát Hiện Hết Tín Dụng & Đổi Tài Khoản (`CreditExhaustedError`):**
   - Tự động nhận diện khi tài khoản hết tín dụng Google Flow và chuyển cảnh đang chạy sang Tài khoản + IP dự phòng tiếp theo không làm gián đoạn tiến trình.
3. **Đồng Bộ Bảng "Cài Đặt Tác Nhân" (`Agent Settings`):**
   - Tự động bấm biểu tượng Tuỳ chỉnh (`tune` cạnh nút Gửi `→`) dưới thanh nhập lệnh để thiết lập:
     - **Xác nhận trước khi tạo:** `Không bao giờ` (Tự động tạo ngay).
     - **Khung hình Video:** `9:16` (Dọc) hoặc `16:9` (Ngang).
     - **Số lượng Video:** `x1`, `x2`, `x3`, `x4`.
     - **Model Video:** `Omni 1.1 Flash`, `Veo 3.1 - Fast`, `Veo 3.1 - Quality`.
     - Tự động bấm **`Lưu`** trước khi tạo video.
4. **Hậu Kỳ Tự Động 100% Miễn Phí (`post_processor.py` + Antigravity 2.0):**
   - Tự động chuẩn hóa và nối các cảnh 8s thành 1 video hoàn chỉnh.
   - Tự động lồng **Nhạc nền (BGM)** và chèn **Hiệu ứng âm thanh (SFX)** chuyển cảnh từ thư viện `audio_library/{bgm,sfx}`.

## 🚀 Cách chạy trên máy mới (ở nhà)
```bash
git clone <link-repo-cua-anh>
cd Flow-AI-Video-Studio
pip install -r requirements.txt
playwright install chromium
```
Sau đó nhấp đúp vào **`CHAY_TOOL_FLOW_AI.bat`** (hoặc chạy `python app.py`) và mở `http://127.0.0.1:8686`.
