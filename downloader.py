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
    return bool(url and "tiktok.com" in url.lower())

def is_youtube_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u

def is_threads_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "threads.net" in u or "threads.com" in u

def ensure_ios_compatibility(filepath: str, is_tiktok: bool = False) -> str:
    """
    Chuẩn hóa video tương thích iOS siêu tốc (an toàn tuyệt đối cho 512MB RAM):
    - Video TikTok: Giữ nguyên 100% (đã chuẩn H.264 + AAC từ CDN), không tốn 1ms CPU.
    - Video khác (FB/YouTube):
      + Luôn sao chép luồng hình ảnh (-c:v copy). TUYỆT ĐỐI KHÔNG RE-ENCODE (-c:v libx264) để tránh OOM 512MB RAM!
      + Chỉ chuyển đổi âm thanh sang AAC nếu là Opus/Vorbis (chỉ tốn ~10MB RAM, 0.2s).
      + Gắn cờ +faststart cho iOS.
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

    a_codec = "aac"

    if ffprobe_bin:
        try:
            res_a = subprocess.run(
                [ffprobe_bin, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", filepath],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5
            )
            a_codec = res_a.stdout.strip().lower()
        except Exception:
            pass

    # Nếu audio đã là aac hoặc mp4a thì file mp4 đã chuẩn 100%, không cần chạy ffmpeg
    if a_codec in ["aac", "mp4a"]:
        return filepath

    # Chỉ chuyển đổi âm thanh sang AAC, hình ảnh sao chép nguyên bản (-c:v copy)
    cmd = [
        ffmpeg_bin, "-y", "-i", filepath,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        temp_ios_file
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if proc.returncode == 0 and os.path.exists(temp_ios_file) and os.path.getsize(temp_ios_file) > 1000:
            os.replace(temp_ios_file, filepath)
            print("[✓] Đã chuẩn hóa âm thanh AAC tương thích iOS an toàn RAM.")
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

def get_base_ydl_opts():
    """Tạo cấu hình yt-dlp tối ưu vượt qua tường lửa kiểm tra bot của YouTube trên Cloud"""
    js_runtimes = {}
    if shutil.which("node"):
        js_runtimes['node'] = {}
    elif shutil.which("deno"):
        js_runtimes['deno'] = {}

    opts = {
        'no_warnings': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'tv_embedded', 'mweb', 'web_creator', 'android'],
                'player_skip': ['webpage', 'configs']
            }
        }
    }
    if js_runtimes:
        opts['js_runtimes'] = js_runtimes

    # Hỗ trợ Cookies nếu người dùng cấu hình
    cookie_file = os.path.join(os.path.dirname(__file__), "cookies.txt")
    if os.path.exists(cookie_file):
        opts['cookiefile'] = cookie_file
    elif os.getenv("YOUTUBE_COOKIES"):
        temp_cookies = os.path.join(os.path.dirname(__file__), "temp_cookies.txt")
        try:
            with open(temp_cookies, "w", encoding="utf-8") as f:
                f.write(os.getenv("YOUTUBE_COOKIES"))
            opts['cookiefile'] = temp_cookies
        except Exception:
            pass

    return opts

def extract_threads_info(url: str) -> dict:
    m = re.search(r'/post/([A-Za-z0-9_-]+)', url)
    shortcode = m.group(1) if m else ''
    
    fetch_url = url.split('?')[0].rstrip('/')
    if not fetch_url.endswith('/media'):
        fetch_url += '/media'
        
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-Dest': 'document'
    }
    req = urllib.request.Request(fetch_url, headers=headers)
    with urllib.request.urlopen(req, timeout=12, context=SSL_CONTEXT) as r:
        html = r.read().decode('utf-8', errors='ignore')
        
    title_m = re.search(r'<title>(.*?)</title>', html)
    title = title_m.group(1).strip() if title_m else 'Threads Video'
    if ' on Threads' in title:
        title = title.split(' on Threads')[0]
        
    thumbnail = ''
    thumb_m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html)
    if thumb_m:
        thumbnail = thumb_m.group(1).replace('&amp;', '&')
        
    uploader = 'Threads'
    uploader_m = re.search(r'/@([A-Za-z0-9._-]+)', url)
    if uploader_m:
        uploader = f"@{uploader_m.group(1)}"
        
    return {
        'title': title,
        'thumbnail': thumbnail,
        'uploader': uploader,
        'duration': 0,
        'shortcode': shortcode
    }

def download_threads_video(url: str, output_dir: str = None) -> str:
    """
    Tải video Threads chất lượng cao gốc bằng browser-level navigation scraping.
    """
    if not output_dir:
        output_dir = os.path.expanduser("~/Downloads")
    os.makedirs(output_dir, exist_ok=True)
    
    m = re.search(r'/post/([A-Za-z0-9_-]+)', url)
    shortcode = m.group(1) if m else f"threads_{int(time.time())}"
    
    fetch_url = url.split('?')[0].rstrip('/')
    if not fetch_url.endswith('/media'):
        fetch_url += '/media'
        
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-Dest': 'document'
    }
    req = urllib.request.Request(fetch_url, headers=headers)
    with urllib.request.urlopen(req, timeout=12, context=SSL_CONTEXT) as r:
        html = r.read().decode('utf-8', errors='ignore')
        
    video_url = None
    if shortcode:
        pos = html.find(f'\"code\":\"{shortcode}\"')
        if pos != -1:
            chunk = html[pos: pos+25000]
            v_m = re.search(r'\"video_versions\":\s*(\[[^\]]+\])', chunk)
            if v_m:
                v_json = json.loads(v_m.group(1).encode('utf-8').decode('unicode_escape'))
                if v_json and isinstance(v_json, list):
                    video_url = v_json[0].get('url')
                    
    if not video_url:
        all_v = re.findall(r'\"video_versions\":\s*(\[[^\]]+\])', html)
        for cand in all_v:
            try:
                v_json = json.loads(cand.encode('utf-8').decode('unicode_escape'))
                if v_json and isinstance(v_json, list):
                    video_url = v_json[0].get('url')
                    if video_url: break
            except Exception:
                pass
                
    if not video_url:
        raise ValueError("Không tìm thấy luồng video công khai trong bài đăng Threads này. Vui lòng kiểm tra lại liên kết.")
        
    title_m = re.search(r'<title>(.*?)</title>', html)
    raw_title = title_m.group(1).strip() if title_m else shortcode
    safe_title = re.sub(r'[^\w\.\-\s]', '_', raw_title).strip()
    if not safe_title or len(safe_title) > 60:
        safe_title = f"Threads_{shortcode}"
        
    output_filepath = os.path.join(output_dir, f"{safe_title}.mp4")
    
    req_vid = urllib.request.Request(video_url, headers={'User-Agent': headers['User-Agent']})
    with urllib.request.urlopen(req_vid, timeout=30, context=SSL_CONTEXT) as resp, open(output_filepath, 'wb') as f:
        while chunk := resp.read(65536):
            f.write(chunk)
            
    return ensure_ios_compatibility(output_filepath, is_tiktok=False)

def extract_info(url: str):
    """
    Trích xuất tiêu đề, hình thumbnail, thời lượng và thông tin cơ bản.
    """
    if is_tiktok_url(url):
        try:
            return extract_tiktok_fallback(url)
        except Exception:
            pass

    if is_threads_url(url):
        try:
            return extract_threads_info(url)
        except Exception:
            pass

    ydl = get_yt_dlp()
    ydl_opts = get_base_ydl_opts()
    ydl_opts.update({
        'quiet': True,
        'skip_download': True,
    })
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
    - Với YouTube, Facebook: Sử dụng bộ giải mã yt-dlp chuyên dụng, ưu tiên H.264 + AAC tự nhiên.
    """
    if output_dir is None:
        output_dir = os.path.expanduser("~/Downloads")
    os.makedirs(output_dir, exist_ok=True)

    # 1. ĐỐI VỚI THREADS: Bóc tách trực tiếp luồng MP4 công khai
    if is_threads_url(url):
        return download_threads_video(url, output_dir)

    # 2. ĐỐI VỚI TIKTOK: Ưu tiên kênh tải trực tiếp
    if is_tiktok_url(url):
        try:
            return download_tiktok_fallback(url, output_dir)
        except Exception as e:
            print(f"[!] Kênh tải trực tiếp: {e}. Đang chuyển sang kênh dự phòng yt-dlp...")

    # 2. ĐỐI VỚI YOUTUBE / FACEBOOK / DỰ PHÒNG: Dùng yt-dlp
    ydl = get_yt_dlp()
    outtmpl = os.path.join(output_dir, '%(title).80s [%(id)s].%(ext)s')

    # Ưu tiên tuyệt đối H.264 (AVC) và AAC để tránh tải VP9/AV1 gây tràn RAM 512MB
    ydl_opts = get_base_ydl_opts()
    ydl_opts.update({
        'outtmpl': outtmpl,
        'format': (
            'bv*[vcodec^=avc1]+ba[acodec^=mp4a]/'
            'bv*[vcodec^=avc]+ba[acodec^=mp4a]/'
            'bv*[vcodec^=avc1]+ba/'
            'bv*[vcodec^=avc]+ba/'
            'bv*[vcodec!*=vp9][vcodec!*=vp09][vcodec!*=av01]+ba/'
            'b[vcodec^=avc1]/'
            'b[vcodec^=avc]/'
            'b[vcodec!*=vp9][vcodec!*=av01]/'
            'bestvideo[vcodec^=avc]+bestaudio/best'
        ),
        'format_sort': ['vcodec:h264', 'acodec:m4a'],
        'merge_output_format': 'mp4',
        'quiet': False,
        'postprocessor_args': {
            'merger': [
                '-c:a', 'aac',
                '-movflags', '+faststart'
            ]
        }
    })

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

def get_audio_for_download(saved_video_path: str, url: str, output_dir: str = None) -> str:
    """
    Tạo hoặc trích xuất file MP3 âm thanh từ video đã tải:
    1. Với TikTok: Tải trực tiếp track MP3 chất lượng gốc từ API mà không cần re-encode.
    2. Với các video khác: Dùng ffmpeg xuất track audio MP3.
    """
    if not output_dir:
        output_dir = os.path.dirname(saved_video_path) if saved_video_path else os.path.expanduser("~/Downloads")
    os.makedirs(output_dir, exist_ok=True)

    base_name, _ = os.path.splitext(os.path.basename(saved_video_path))
    audio_path = os.path.join(output_dir, f"{base_name} [Audio].mp3")

    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1024:
        return audio_path

    # 1. ĐỐI VỚI TIKTOK: Lấy link MP3 trực tiếp từ API tikwm
    if is_tiktok_url(url):
        try:
            info = extract_tiktok_fallback(url)
            music_url = info.get('music')
            if music_url:
                req = urllib.request.Request(music_url, headers={
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
                })
                with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp, open(audio_path, 'wb') as f:
                    while chunk := resp.read(65536):
                        f.write(chunk)
                if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1024:
                    return audio_path
        except Exception as e:
            print(f"[!] Lỗi tải track MP3 TikTok trực tiếp: {e}")

    # 2. ĐỐI VỚI ALBUM ẢNH (.zip) ĐÃ TẢI
    if saved_video_path and saved_video_path.endswith('.zip'):
        album_dir = saved_video_path[:-4]
        if os.path.isdir(album_dir):
            music_cand = os.path.join(album_dir, "nhac_nen.mp3")
            if os.path.exists(music_cand):
                try:
                    shutil.copyfile(music_cand, audio_path)
                    return audio_path
                except Exception:
                    pass

    # 3. ĐỐI VỚI CÁC VIDEO KHÁC: Dùng ffmpeg tách MP3
    if saved_video_path and os.path.exists(saved_video_path) and not saved_video_path.endswith('.zip'):
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            cmd = [
                ffmpeg_bin, "-y",
                "-i", saved_video_path,
                "-vn",
                "-acodec", "libmp3lame",
                "-b:a", "192k",
                audio_path
            ]
            try:
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                if res.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 1024:
                    return audio_path
            except Exception as e:
                print(f"[!] Lỗi ffmpeg trích xuất audio: {e}")

    return ""

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Cách dùng: python downloader.py <link_video>")
        sys.exit(1)
    url = sys.argv[1]
    print(f"Đang phân tích và tải: {url}...")
    saved_file = download_video(url)
    print(f"Đã lưu thành công tại: {saved_file}")
