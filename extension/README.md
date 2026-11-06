# 🍪 TAIO Cookie Exporter (Chrome / Edge Extension)

Tiện ích mở rộng trình duyệt (Manifest V3) nhẹ, an toàn và bảo mật 100% giúp xuất Cookies chuẩn định dạng **Netscape HTTP Cookie File** để sử dụng cho `yt-dlp`, `curl`, và `TAIO Studio`.

---

## 🌟 Tính năng nổi bật
* **100% Riêng tư & Offline**: Không gửi bất kỳ dữ liệu nào ra máy chủ bên ngoài, toàn bộ quá trình trích xuất diễn ra trực tiếp trong trình duyệt của bạn.
* **Chuẩn định dạng Netscape**: Tương thích hoàn toàn với `yt-dlp` (`--cookies cookies.txt`) để vượt qua cơ chế chống bot của YouTube trên Cloud (Render/VPS).
* **Xuất linh hoạt**: Hỗ trợ xuất cookies cho trang web đang mở (YouTube, Facebook, TikTok, Instagram...) với 1 cú click (Tải file hoặc Copy vào Clipboard).
* **Mã nguồn mở**: Bạn hoàn toàn có thể kiểm tra, tùy biến hoặc bổ sung thêm tính năng theo ý muốn.

---

## 🚀 Hướng dẫn cài đặt lên Chrome / Edge / Brave

1. **Mở trang quản lý tiện ích**:
   * Trên Chrome / Brave: Truy cập `chrome://extensions/`
   * Trên Microsoft Edge: Truy cập `edge://extensions/`
2. **Bật chế độ dành cho nhà phát triển (Developer mode)**:
   * Gạt công tắc **"Developer mode"** ở góc trên cùng bên phải sang **BẬT (ON)**.
3. **Nạp tiện ích vào trình duyệt**:
   * Bấm nút **"Load unpacked"** (Tải tiện ích đã giải nén).
   * Chọn thư mục `extension/` trong dự án này.
4. **Ghim tiện ích**:
   * Bấm biểu tượng mảnh ghép (Extensions) trên thanh công cụ và ghim **TAIO Cookie Exporter** để sử dụng nhanh.

---

## 💡 Cách dùng để nạp Cookies cho Bot YouTube

1. Mở tab [youtube.com](https://www.youtube.com) trên trình duyệt và đăng nhập tài khoản.
2. Bấm vào icon **TAIO Cookie Exporter** trên thanh công cụ.
3. Bấm **"Tải file cookies.txt"**.
4. Gửi trực tiếp file `cookies.txt` vừa tải vào khung chat với Telegram Bot (hoặc dán vào biến `YOUTUBE_COOKIES` trên Render).
5. Xong! Bot sẽ tải được mọi video và Shorts từ YouTube mà không bị chặn.
