import os
import sys
import glob
import json
import re
import ssl
import shutil
import subprocess
import urllib.request
import urllib.parse

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# Bỏ qua lỗi SSL xác thực chứng chỉ thường gặp trên macOS / container
SSL_CONTEXT = ssl._create_unverified_context()

def get_yt_dlp():
    if yt_dlp is None:
        raise RuntimeError("Thư viện 'yt-dlp' chưa được cài đặt. Vui lòng chạy: pip install yt-dlp")
    return yt_dlp

def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url.lower()

def ensure_ios_compatibility(filepath: str, is_tiktok: bool = False) -> str:
    """
    Chuẩn hóa video tương thích iOS siêu tốc, tối ưu hóa triệt để cho máy chủ Cloud (0.1 CPU):
    - Video TikTok: Giữ nguyên 100% (đã chuẩn H.264 + AAC từ CDN), không tốn 1ms CPU.
    - Video khác (FB/YouTube): Chỉ chuyển đổi âm thanh sang AAC nếu là Opus (rất nhẹ, <0.2s).
    - Chỉ re-encode video nếu bắt buộc là VP9/AV1 với preset ultrafast để tránh treo CPU.
    """
    if not filepath or not filepath.endswith(".mp4") or not os.path.exists(filepath):
        return filepath

    # TikTok từ CDN vốn dĩ đã là H.264/AAC tương thích mọi thiết bị, không cần can thiệp
    if is_tiktok:
        return filepath

    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    if not ffmpeg_bin:
        return filepath

    dir_name = os.path.dirname(filepath)
    base_name = os.path.basename(filepath)
    temp_ios_file = os.path.join(dir_name, f"ios_compat_{base_name}")

    v_codec = "h264"
    a_codec = "aac"

    if ffprobe_bin:
        try:
            res_v = subprocess.run(
                [ffprobe_bin, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", filepath],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5
            )
            v_codec = res_v.stdout.strip().lower()

            res_a = subprocess.run(
                [ffprobe_bin, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", filepath],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5
            )
            a_codec = res_a.stdout.strip().lower()
        except Exception:
            pass

    # Chỉ re-encode nếu video là VP9 hoặc AV1 (iOS không hỗ trợ)
    if v_codec in ["vp9", "av01", "av1"]:
        v_args = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast"]
    else:
        v_args = ["-c:v", "copy"]

    # Nếu audio là Opus/Vorbis (FB Reels), chuyển sang AAC (siêu nhẹ, <0.2s)
    if a_codec not in ["aac", "mp4a"]:
        a_args = ["-c:a", "aac", "-b:a", "128k"]
    else:
        a_args = ["-c:a", "copy"]

    # Nếu cả 2 đều đã chuẩn thì không cần chạy ffmpeg
    if v_args == ["-c:v", "copy"] and a_args == ["-c:a", "copy"]:
        return filepath

    cmd = [
        ffmpeg_bin, "-y", "-i", filepath,
        *v_args,
        *a_args,
        "-movflags", "+faststart",
        temp_ios_file
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if proc.returncode == 0 and os.path.exists(temp_ios_file) and os.path.getsize(temp_ios_file) > 1000:
            os.replace(temp_ios_file, filepath)
            print("[✓] Đã chuẩn hóa video tương thích iOS siêu tốc.")
    except Exception as e:
        print(f"[!] Bỏ qua bước chuẩn hóa iOS do: {e}")
        if os.path.exists(temp_ios_file):
            try:
                os.remove(temp_ios_file)
            except Exception:
                pass

    return filepath

def extract_tiktok_fallback(url: str):
    """
    Lấy thông tin TikTok qua API trực tiếp.
    Hỗ trợ nhận diện Video hoặc Album ảnh.
    """
    clean_url = url.split('?')[0] if '?' in url else url
    api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(clean_url)}&hd=1"
    req = urllib.request.Request(api_url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        
    if data.get('code') == 0:
        video_info = data.get('data', {})
        images = video_info.get('images')
        is_images = bool(images and len(images) > 0)
        return {
            'title': video_info.get('title', 'TikTok Post'),
            'thumbnail': video_info.get('cover', ''),
            'duration': video_info.get('duration', 0),
            'uploader': video_info.get('author', {}).get('nickname', 'TikTok Creator'),
            'extractor': 'TikTok (Album ảnh)' if is_images else 'TikTok',
            'is_photo_mode': is_images,
            'images': images or [],
            'music': video_info.get('music'),
            '_direct_url': video_info.get('hdplay') or video_info.get('play'),
            '_id': video_info.get('id', 'video')
        }
    raise RuntimeError(data.get('msg', 'Không trích xuất được dữ liệu TikTok'))

def download_tiktok_fallback(url: str, output_dir: str = None) -> str:
    """
    Tải nội dung TikTok trực tiếp không logo/watermark:
    - Nếu là Video: Tải file MP4 sắc nét.
    - Nếu là Album ảnh: Tải trọn bộ ảnh HD + Nhạc nền, nén ZIP.
    """
    if output_dir is None:
        output_dir = os.path.expanduser("~/Downloads")
    os.makedirs(output_dir, exist_ok=True)
    info = extract_tiktok_fallback(url)
    title = info.get('title', 'tiktok')
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()[:70]
    if not safe_title:
        safe_title = "tiktok_post"
    video_id = info.get('_id', 'video')

    # TRƯỜNG HỢP 1: BÀI ĐĂNG LÀ ALBUM ẢNH (SLIDESHOW)
    if info.get('is_photo_mode') and info.get('images'):
        images = info['images']
        album_dir_name = f"{safe_title} [{video_id}]_AlbumAnh"
        album_dir = os.path.join(output_dir, album_dir_name)
        os.makedirs(album_dir, exist_ok=True)
        
        for i, img_url in enumerate(images, start=1):
            img_file = os.path.join(album_dir, f"anh_{i:02d}.jpeg")
            try:
                req = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as r, open(img_file, 'wb') as f:
                    f.write(r.read())
            except Exception:
                pass

        if info.get('music'):
            music_file = os.path.join(album_dir, "nhac_nen.mp3")
            try:
                req = urllib.request.Request(info['music'], headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as r, open(music_file, 'wb') as f:
                    f.write(r.read())
            except Exception:
                pass

        zip_path_base = os.path.join(output_dir, album_dir_name)
        shutil.make_archive(zip_path_base, 'zip', album_dir)
        final_zip = f"{zip_path_base}.zip"
        return final_zip

    # TRƯỜNG HỢP 2: VIDEO CLIP THÔNG THƯỜNG (MP4 KHÔNG LOGO)
    direct_url = info.get('_direct_url')
    if not direct_url:
        raise RuntimeError("Không tìm thấy đường dẫn video trực tiếp từ máy chủ")
        
    output_filename = os.path.join(output_dir, f"{safe_title} [{video_id}].mp4")
    
    req = urllib.request.Request(direct_url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    
    with urllib.request.urlopen(req, timeout=25, context=SSL_CONTEXT) as resp, open(output_filename, 'wb') as f:
        while chunk := resp.read(65536):
            f.write(chunk)
            
    return ensure_ios_compatibility(output_filename, is_tiktok=True)

def extract_info(url: str):
    """
    Trích xuất tiêu đề, hình thumbnail, thời lượng và thông tin cơ bản.
    """
    if is_tiktok_url(url):
        try:
            return extract_tiktok_fallback(url)
        except Exception:
            pass

    ydl = get_yt_dlp()
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }
    with ydl.YoutubeDL(ydl_opts) as y:
        info = y.extract_info(url, download=False)
        return {
            'title': info.get('title', 'Video không có tiêu đề'),
            'thumbnail': info.get('thumbnail', ''),
            'duration': info.get('duration', 0),
            'uploader': info.get('uploader', 'Unknown'),
            'extractor': info.get('extractor_key', 'Generic'),
        }

def download_video(url: str, output_dir: str = None) -> str:
    """
    Tải video từ YouTube, Facebook Reel, TikTok.
    - Với TikTok: Tải qua API trực tiếp cực nhanh không logo.
    - Với YouTube, Facebook: Sử dụng bộ giải mã yt-dlp chuyên dụng + tối ưu iOS.
    """
    if output_dir is None:
        output_dir = os.path.expanduser("~/Downloads")
    os.makedirs(output_dir, exist_ok=True)

    # 1. ĐỐI VỚI TIKTOK: Ưu tiên kênh tải trực tiếp
    if is_tiktok_url(url):
        try:
            return download_tiktok_fallback(url, output_dir)
        except Exception as e:
            print(f"[!] Kênh tải trực tiếp: {e}. Đang chuyển sang kênh dự phòng yt-dlp...")

    # 2. ĐỐI VỚI YOUTUBE / FACEBOOK / DỰ PHÒNG: Dùng yt-dlp
    ydl = get_yt_dlp()
    outtmpl = os.path.join(output_dir, '%(title).80s [%(id)s].%(ext)s')

    ydl_opts = {
        'outtmpl': outtmpl,
        'format': 'bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*[vcodec^=avc]+ba[acodec^=mp4a]/bv*[vcodec!=none]+ba[acodec!=none]/b[vcodec!=none]/bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': False,
        'no_warnings': True,
        'postprocessor_args': {
            'merger': [
                '-c:a', 'aac',
                '-movflags', '+faststart'
            ]
        }
    }

    try:
        with ydl.YoutubeDL(ydl_opts) as y:
            info = y.extract_info(url, download=True)
            filename = y.prepare_filename(info)
            
            base_name, _ = os.path.splitext(filename)
            mp4_filename = f"{base_name}.mp4"
            if os.path.exists(mp4_filename):
                final_path = mp4_filename
            elif os.path.exists(filename):
                final_path = filename
            else:
                files = glob.glob(os.path.join(output_dir, '*'))
                files.sort(key=os.path.getmtime, reverse=True)
                final_path = files[0] if files else filename

            return ensure_ios_compatibility(final_path, is_tiktok=is_tiktok_url(url))

    except Exception as e:
        error_msg = str(e)
        if is_tiktok_url(url):
            return download_tiktok_fallback(url, output_dir)
        raise e

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Cách dùng: python downloader.py <link_video>")
        sys.exit(1)
    url = sys.argv[1]
    print(f"Đang phân tích và tải: {url}...")
    saved_file = download_video(url)
    print(f"Đã lưu thành công tại: {saved_file}")
