# Universal Video Downloader (Hỗ trợ Mac & iOS Mobile)

Công cụ hỗ trợ tải video chất lượng cao từ **YouTube**, **Facebook Reels** và **TikTok** (không dính watermark/logo).

---

## 🍎 1. Sử dụng trên Mac (macOS)

### Cách 1: Giao diện Web trực quan (Khuyên dùng)
1. Nhấp đúp (Double-click) vào file **`run_mac.command`**.
   *(File sẽ tự động kiểm tra cài đặt thư viện cần thiết, khởi động máy chủ và mở Safari tại địa chỉ `http://localhost:8000`).*
2. Dán link video (Facebook Reel, TikTok, YouTube) và bấm **Bắt đầu tải video**.
3. Video tải xong sẽ tự động lưu vào thư mục `downloads` và hiển thị trên màn hình.

### Cách 2: Dòng lệnh Terminal (CLI)
Mở Terminal và gõ:
```bash
cd /Users/abc/.gemini/antigravity/scratch/video_downloader
python3 cli.py "LINK_VIDEO_CẦN_TẢI"
```

---

## 📱 2. Sử dụng trên iPhone / iPad (iOS Mobile)

Do Apple đóng kín hệ thống iOS (không cho phép chạy trực tiếp các file thực thi nhị phân như `.exe` hay `.command`), bạn có **2 cách** cực kỳ thuận tiện sau:

### Cách A: Dùng Web UI nội bộ từ Mac (Nhanh & Tiện nhất)
1. Đảm bảo iPhone và Mac cùng kết nối vào một mạng Wi-Fi.
2. Trên Mac, chạy file `run_mac.command`. Trên màn hình sẽ hiển thị địa chỉ LAN IP (ví dụ: `http://192.168.1.15:8000`).
3. Mở **Safari trên iPhone**, truy cập vào địa chỉ trên.
4. Dán link video Facebook/TikTok/YouTube -> Bấm **Tải video**.
5. Khi Safari hiện thông báo *"Bạn có muốn tải về file này không?"* -> Chọn **Tải về**.
6. Vào mục Tải về của Safari -> Bấm nút **Chia sẻ** -> Chọn **Lưu video (Save Video)** để lưu thẳng vào Album Ảnh (Photos) của iPhone!

### Cách B: Chạy độc lập trên iPhone (Không cần bật máy tính)
1. Lên App Store cài ứng dụng **a-Shell** (Ứng dụng chạy Terminal/Python miễn phí cho iOS).
2. Mở app **a-Shell** và gõ lệnh sau để cài yt-dlp:
   ```bash
   pip install yt-dlp
   ```
3. Khi muốn tải video bất kỳ, chỉ cần gõ:
   ```bash
   yt-dlp "LINK_VIDEO"
   ```
4. Sau khi tải xong, gõ:
   ```bash
   open <tên_file>.mp4
   ```
   rồi chọn Lưu vào ứng dụng Ảnh.
