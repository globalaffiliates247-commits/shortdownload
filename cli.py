#!/usr/bin/env python3
import sys
import os
import downloader

def main():
    if len(sys.argv) < 2:
        print("=" * 55)
        print("  UNIVERSAL VIDEO DOWNLOADER - CLI")
        print("=" * 55)
        print("Cách sử dụng:")
        print("  python3 cli.py <link_video_youtube_fb_tiktok>")
        print("\nVí dụ:")
        print('  python3 cli.py "https://www.facebook.com/share/v/1CCMRE3ipH"')
        print('  python3 cli.py "https://www.tiktok.com/@user/video/..."')
        print('  python3 cli.py "https://www.youtube.com/watch?v=..."')
        print("=" * 55)
        sys.exit(1)

    url = sys.argv[1].strip()
    download_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/Downloads")
    os.makedirs(download_dir, exist_ok=True)

    print(f"[*] Đang nhận diện và chuẩn bị tải: {url}")
    try:
        info = downloader.extract_info(url)
        print(f"[*] Tiêu đề : {info.get('title')}")
        print(f"[*] Nền tảng: {info.get('extractor')}")
    except Exception as e:
        print(f"[!] Không trích xuất được metadata trước ({e}), tiếp tục tải trực tiếp...")

    try:
        saved_file = downloader.download_video(url, output_dir=download_dir)
        print(f"\n[✓] TẢI THÀNH CÔNG!")
        print(f"[✓] Đã lưu tại: {os.path.abspath(saved_file)}")
    except Exception as e:
        print(f"\n[✗] LỖI: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
