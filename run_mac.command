#!/bin/bash

# Di chuyển vào thư mục của file script
cd "$(dirname "$0")"

echo "=========================================================="
echo "    KHỞI ĐỘNG UNIVERSAL VIDEO DOWNLOADER CHO MAC & IOS    "
echo "=========================================================="

# Kiểm tra Python 3
if ! command -v python3 &> /dev/null; then
    echo "[!] Không tìm thấy Python 3 trên máy của bạn."
    echo "[!] Vui lòng cài đặt Python 3 từ https://www.python.org hoặc qua brew."
    read -p "Nhấn Enter để thoát..."
    exit 1
fi

# Kiểm tra thư viện yt-dlp và curl_cffi
python3 -c "import yt_dlp" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "[*] Đang tự động cài đặt thư viện 'yt-dlp' và 'curl_cffi'..."
    python3 -m pip install -U yt-dlp curl_cffi
    if [ $? -ne 0 ]; then
        echo "[!] Cài đặt thất bại. Vui lòng kiểm tra kết nối mạng."
        read -p "Nhấn Enter để thoát..."
        exit 1
    fi
fi

# Mở trình duyệt mặc định trên Mac sau 1 giây
(sleep 1 && open "http://localhost:8000") &

# Chạy server
python3 server.py
