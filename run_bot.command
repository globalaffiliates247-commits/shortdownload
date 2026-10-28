#!/bin/bash

# Di chuyển vào thư mục của file script
cd "$(dirname "$0")"

echo "=========================================================="
echo "         KHỞI ĐỘNG TELEGRAM VIDEO DOWNLOADER BOT          "
echo "=========================================================="

# Kiểm tra Python 3
if ! command -v python3 &> /dev/null; then
    echo "[!] Không tìm thấy Python 3 trên máy của bạn."
    read -p "Nhấn Enter để thoát..."
    exit 1
fi

# Cài đặt / cập nhật các thư viện cần thiết
python3 -c "import telebot, yt_dlp, curl_cffi" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "[*] Đang tự động cài đặt các thư viện cần thiết..."
    python3 -m pip install -U pyTelegramBotAPI yt-dlp curl_cffi
fi

# Chạy bot
python3 bot.py
