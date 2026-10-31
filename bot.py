import os
import sys
import time
import glob
import json
import mimetypes
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import apihelper
import downloader

# Cấu hình timeout mạng toàn cục cho telebot (tránh ngắt kết nối khi upload video)
apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 300

# Khởi tạo executor cho tác vụ tải ngầm
executor = ThreadPoolExecutor(max_workers=3)

DOWNLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def cleanup_old_files(directory=DOWNLOAD_DIR, max_age_seconds=600):
    """Tự động dọn dẹp file tạm cũ hơn 10 phút để tránh đầy ổ cứng máy chủ"""
    now = time.time()
    for f in glob.glob(os.path.join(directory, "*")):
        try:
            if os.path.isfile(f) and (now - os.path.getmtime(f) > max_age_seconds):
                os.remove(f)
        except Exception:
            pass

# ==========================================
# GIAO DIỆN WEB HIỆN ĐẠI (CÁCH 3) TRÊN CLOUD
# ==========================================
HTML_PAGE = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Universal Video Downloader</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    body { background: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    .glass { background: rgba(30, 41, 59, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.1); }
  </style>
</head>
<body class="min-h-screen flex flex-col items-center justify-between p-4 sm:p-6">
  <div class="w-full max-w-xl mx-auto my-auto space-y-6">
    <!-- Header -->
    <div class="text-center space-y-2">
      <div class="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-tr from-rose-500 to-indigo-600 shadow-lg shadow-indigo-500/30">
        <i class="fa-solid fa-cloud-arrow-down text-2xl text-white"></i>
      </div>
      <h1 class="text-2xl sm:text-3xl font-extrabold tracking-tight">Tải Video Đa Năng</h1>
      <p class="text-sm text-slate-400">Hỗ trợ YouTube, Facebook Reels & TikTok không logo</p>
      
      <!-- Supported Badges -->
      <div class="flex items-center justify-center gap-2 pt-2">
        <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-red-500/10 text-red-400 border border-red-500/20">
          <i class="fa-brands fa-youtube"></i> YouTube
        </span>
        <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-blue-500/10 text-blue-400 border border-blue-500/20">
          <i class="fa-brands fa-facebook"></i> FB Reels
        </span>
        <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
          <i class="fa-brands fa-tiktok"></i> TikTok
        </span>
      </div>
    </div>

    <!-- Main Card -->
    <div class="glass rounded-3xl p-5 sm:p-7 shadow-2xl space-y-5">
      <div class="space-y-2">
        <label class="text-xs font-semibold text-slate-300 uppercase tracking-wider block">Dán link video cần tải</label>
        <div class="relative flex items-center">
          <input id="videoUrl" type="url" placeholder="https://www.facebook.com/reel/... hoặc tiktok, youtube" 
                 class="w-full bg-slate-900/90 border border-slate-700 rounded-2xl px-4 py-3.5 pl-11 text-sm text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all">
          <i class="fa-solid fa-link absolute left-4 text-slate-500"></i>
          <button id="pasteBtn" class="absolute right-3 px-2.5 py-1 text-xs font-medium text-slate-400 hover:text-white bg-slate-800 rounded-lg border border-slate-700 transition">
            Dán
          </button>
        </div>
      </div>

      <button id="downloadBtn" onclick="handleDownload()" class="w-full bg-gradient-to-r from-indigo-500 hover:from-indigo-600 to-violet-600 hover:to-violet-700 text-white font-semibold py-3.5 px-4 rounded-2xl shadow-lg shadow-indigo-500/25 active:scale-[0.98] transition-all flex items-center justify-center gap-2">
        <i class="fa-solid fa-download"></i>
        <span>Bắt đầu tải video</span>
      </button>

      <!-- Status indicator -->
      <div id="statusBox" class="hidden rounded-2xl p-4 text-sm transition-all"></div>

      <!-- Preview card -->
      <div id="previewCard" class="hidden space-y-3 pt-3 border-t border-slate-700/50">
        <div class="flex gap-3 items-center">
          <img id="thumbImg" src="" class="w-20 h-20 object-cover rounded-xl bg-slate-800 flex-shrink-0" alt="Thumbnail">
          <div class="overflow-hidden">
            <h4 id="videoTitle" class="text-sm font-semibold text-white line-clamp-2"></h4>
            <p id="videoAuthor" class="text-xs text-slate-400 mt-1"></p>
          </div>
        </div>
        <a id="saveFileBtn" href="#" class="w-full block text-center bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-3 rounded-xl text-sm transition font-semibold">
          <i class="fa-solid fa-cloud-arrow-down mr-1"></i> Bấm để lưu video về máy
        </a>
      </div>
    </div>

    <!-- Telegram Bot Quick Access -->
    <div class="glass rounded-2xl p-4 text-xs text-slate-400 flex items-center justify-between">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-sky-500/20 text-sky-400 flex items-center justify-center text-base">
          <i class="fa-brands fa-telegram"></i>
        </div>
        <div>
          <div class="text-white font-semibold">Bot Telegram 24/7</div>
          <div class="text-slate-400 text-[11px]">Gửi link trong chat là nhận lại video</div>
        </div>
      </div>
      <a href="https://t.me/Clipbot" target="_blank" class="px-3 py-1.5 rounded-lg bg-sky-500 hover:bg-sky-400 text-white font-medium transition text-xs">
        Mở Bot
      </a>
    </div>
  </div>

  <footer class="text-center text-xs text-slate-600 py-4">
    Universal Video Downloader &bull; Tương thích 100% iOS & Android
  </footer>

  <script>
    // Nút Dán nhanh từ Clipboard
    document.getElementById('pasteBtn').addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) document.getElementById('videoUrl').value = text;
      } catch (err) {
        alert('Vui lòng cấp quyền dán cho trình duyệt.');
      }
    });

    async function handleDownload() {
      const urlInput = document.getElementById('videoUrl');
      const url = urlInput.value.trim();
      const statusBox = document.getElementById('statusBox');
      const btn = document.getElementById('downloadBtn');
      const previewCard = document.getElementById('previewCard');

      if (!url) {
        statusBox.className = "block bg-amber-500/10 border border-amber-500/20 text-amber-300 p-4 rounded-2xl text-sm";
        statusBox.innerHTML = '<i class="fa-solid fa-triangle-exclamation mr-1"></i> Vui lòng nhập link video!';
        return;
      }

      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> <span>Đang xử lý & tải từ nguồn...</span>';
      statusBox.className = "block bg-indigo-500/10 border border-indigo-500/20 text-indigo-300 p-4 rounded-2xl text-sm";
      statusBox.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-1"></i> Hệ thống đang trích xuất luồng video tốt nhất. Quá trình này mất khoảng 5 - 20 giây...';
      previewCard.classList.add('hidden');

      try {
        const response = await fetch('/api/download', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ url: url })
        });

        const data = await response.json();

        if (response.ok && data.success) {
          statusBox.className = "block bg-emerald-500/10 border border-emerald-500/20 text-emerald-300 p-4 rounded-2xl text-sm";
          statusBox.innerHTML = '<i class="fa-solid fa-circle-check mr-1"></i> Tải và chuẩn hóa video thành công!';
          
          previewCard.classList.remove('hidden');
          document.getElementById('videoTitle').innerText = data.title || "Video";
          document.getElementById('videoAuthor').innerText = "Kênh / Tác giả: " + (data.uploader || "N/A");
          if (data.thumbnail) {
            document.getElementById('thumbImg').src = data.thumbnail;
          } else {
            document.getElementById('thumbImg').src = "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=200&q=80";
          }
          
          const saveBtn = document.getElementById('saveFileBtn');
          saveBtn.href = '/api/file/' + encodeURIComponent(data.filename);
          saveBtn.setAttribute('download', data.filename);
          
          // Kích hoạt lưu ngầm trực tiếp không chuyển trang gây lỗi 502
          const autoDownloadLink = document.createElement('a');
          autoDownloadLink.href = saveBtn.href;
          autoDownloadLink.setAttribute('download', data.filename);
          document.body.appendChild(autoDownloadLink);
          autoDownloadLink.click();
          document.body.removeChild(autoDownloadLink);
        } else {
          statusBox.className = "block bg-rose-500/10 border border-rose-500/20 text-rose-300 p-4 rounded-2xl text-sm";
          statusBox.innerHTML = '<i class="fa-solid fa-circle-xmark mr-1"></i> Lỗi: ' + (data.error || 'Không tải được video');
        }
      } catch (err) {
        statusBox.className = "block bg-rose-500/10 border border-rose-500/20 text-rose-300 p-4 rounded-2xl text-sm";
        statusBox.innerHTML = '<i class="fa-solid fa-circle-xmark mr-1"></i> Lỗi kết nối: ' + err.message;
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-download"></i> <span>Tải video khác</span>';
      }
    }
  </script>
</body>
</html>
"""

# ==========================================
# MÁY CHỦ HTTP WEBSERVICE TÍCH HỢP (RENDER)
# ==========================================
class WebAppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode('utf-8'))

        elif path.startswith("/api/file/"):
            filename = urllib.parse.unquote(path[len("/api/file/"):])
            filepath = os.path.join(DOWNLOAD_DIR, filename)

            if not os.path.commonpath([DOWNLOAD_DIR, filepath]) == DOWNLOAD_DIR or not os.path.exists(filepath):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"File not found")
                return

            file_size = os.path.getsize(filepath)
            content_type, _ = mimetypes.guess_type(filepath)
            if not content_type:
                content_type = "video/mp4" if filepath.endswith('.mp4') else "application/octet-stream"

            # Xử lý tên file an toàn cho HTTP Header (chuẩn ASCII fallback + RFC 5987 UTF-8)
            # Tránh lỗi UnicodeEncodeError khi tên video tiếng Việt gây mã lỗi 502 Bad Gateway
            raw_filename = os.path.basename(filepath)
            safe_ascii = re.sub(r'[^\w\.\-\s]', '_', raw_filename).strip()
            encoded_utf8 = urllib.parse.quote(raw_filename)

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded_utf8}')
            self.send_header("Content-Length", str(file_size))
            self.end_headers()

            with open(filepath, 'rb') as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/download":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode('utf-8'))
                url = data.get("url")
                if not url:
                    raise ValueError("URL không được để trống")

                meta = {}
                try:
                    meta = downloader.extract_info(url)
                except Exception:
                    pass

                saved_filepath = downloader.download_video(url, output_dir=DOWNLOAD_DIR)
                filename = os.path.basename(saved_filepath)

                response_data = {
                    "success": True,
                    "filename": filename,
                    "title": meta.get('title', filename),
                    "thumbnail": meta.get('thumbnail', ''),
                    "uploader": meta.get('uploader', '')
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(response_data).encode('utf-8'))

                # Dọn dẹp file cũ ngầm
                threading.Thread(target=cleanup_old_files, daemon=True).start()

            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Giảm bớt log thừa từ UptimeRobot ping
        pass

def start_web_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), WebAppHandler)
    server.serve_forever()

# ==========================================
# KHỞI TẠO TELEGRAM BOT (CÁCH 2)
# ==========================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN and os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                BOT_TOKEN = line.split("=", 1)[1].strip("'\" ")
                break

if not BOT_TOKEN and len(sys.argv) > 1:
    BOT_TOKEN = sys.argv[1].strip()

if not BOT_TOKEN:
    print("=" * 60)
    print(" [!] THIẾU TELEGRAM_BOT_TOKEN")
    print("=" * 60)
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

def safe_edit(chat_id, message_id, text):
    """Cập nhật nội dung tin nhắn an toàn"""
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode='HTML')
    except Exception:
        pass

def send_file_with_retry(chat_id, msg_id, file_path, platform, file_size_mb):
    """Cơ chế gửi file thông minh chống rớt mạng / timeout"""
    is_mp4 = file_path.endswith('.mp4')
    caption = f"✅ <b>Tải thành công!</b> ({file_size_mb:.1f} MB)\nNguồn: {platform}" if is_mp4 else f"✅ <b>Tải thành công!</b> ({file_size_mb:.1f} MB)"

    last_error = None
    for attempt in range(1, 4):
        try:
            if attempt == 2:
                safe_edit(
                    chat_id, msg_id,
                    f"📤 <b>[3/3]</b> Kết nối Telegram bị chậm, đang tự động thử gửi lại (lần 2/3)...\n"
                    f"<code>[██████████] 100%</code>"
                )
                time.sleep(2)
            elif attempt == 3:
                safe_edit(
                    chat_id, msg_id,
                    f"📤 <b>[3/3]</b> Đang chuyển sang luồng truyền tải tốc độ cao (lần 3/3)...\n"
                    f"<code>[██████████] 100%</code>"
                )
                time.sleep(1)

            bot.send_chat_action(chat_id, 'upload_video' if is_mp4 else 'upload_document')

            if is_mp4 and attempt < 3:
                with open(file_path, 'rb') as f:
                    bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=caption,
                        parse_mode='HTML',
                        supports_streaming=True,
                        timeout=180
                    )
                return True
            else:
                with open(file_path, 'rb') as f:
                    bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        caption=caption,
                        parse_mode='HTML',
                        timeout=240
                    )
                return True

        except Exception as e:
            last_error = e
            print(f"[!] Gửi file thất bại lần {attempt}: {e}")

    if last_error:
        raise last_error
    return False

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "👋 Chào bạn! Tôi là <b>Bot tải Video tự động</b>.\n\n"
        "✨ Hỗ trợ tải chất lượng cao không logo:\n"
        "• 🔴 YouTube / Shorts\n"
        "• 🔵 Facebook Reels / Video\n"
        "• ⚫ TikTok Video & Album ảnh\n\n"
        "👉 Chỉ cần dán đường link video vào đây, tôi sẽ tải và gửi lại file cho bạn ngay lập tức!"
    )
    bot.reply_to(message, welcome_text, parse_mode='HTML')

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = (message.text or "").strip()
    
    if not (text.startswith("http://") or text.startswith("https://")):
        bot.reply_to(message, "⚠️ Vui lòng gửi một đường link video hợp lệ (bắt đầu bằng https://...)")
        return

    platform = "Video"
    if "tiktok.com" in text.lower():
        platform = "TikTok (Không logo)"
    elif "facebook.com" in text.lower() or "fb.watch" in text.lower():
        platform = "Facebook Reel"
    elif "youtube.com" in text.lower() or "youtu.be" in text.lower():
        platform = "YouTube"

    status_msg = bot.reply_to(
        message, 
        f"⏳ <b>[1/3]</b> Đang kết nối máy chủ <b>{platform}</b>...\n"
        f"<code>[██░░░░░░░░] 20%</code>", 
        parse_mode='HTML'
    )
    
    chat_id = message.chat.id
    msg_id = status_msg.message_id
    saved_file = None

    try:
        future = executor.submit(downloader.download_video, text, DOWNLOAD_DIR)
        start_time = time.time()
        timeout_seconds = 80
        is_done = False

        while not is_done:
            time.sleep(2.5)
            elapsed = int(time.time() - start_time)

            if future.done():
                is_done = True
                break

            if elapsed > timeout_seconds:
                safe_edit(
                    chat_id, msg_id,
                    "⏱️ <b>Hết thời gian xử lý (Timeout sau 80 giây)!</b>\n\n"
                    "Máy chủ nguồn phản hồi quá chậm hoặc link video bị giới hạn riêng tư. Vui lòng thử lại sau ít phút!"
                )
                return

            if elapsed < 8:
                safe_edit(
                    chat_id, msg_id,
                    f"⏳ <b>[2/3]</b> Đang trích xuất luồng video tốt nhất...\n"
                    f"<code>[████░░░░░░] 40%</code> ({elapsed}s)"
                )
            elif elapsed < 16:
                safe_edit(
                    chat_id, msg_id,
                    f"⏳ <b>[2/3]</b> Đang tải dữ liệu video về máy chủ...\n"
                    f"<code>[██████░░░░] 65%</code> ({elapsed}s)"
                )
            else:
                safe_edit(
                    chat_id, msg_id,
                    f"⏳ <b>[2/3]</b> Đang hoàn tất chuẩn hóa tương thích iOS...\n"
                    f"<code>[████████░░] 85%</code> ({elapsed}s)"
                )

        saved_file = future.result()

        if not saved_file or not os.path.exists(saved_file):
            raise RuntimeError("Không tìm thấy file video sau khi hoàn tất tải.")

        file_size_mb = os.path.getsize(saved_file) / (1024 * 1024)
        if file_size_mb > 50:
            safe_edit(
                chat_id, msg_id,
                f"⚠️ Video tải về thành công nhưng có dung lượng <b>{file_size_mb:.1f} MB</b>, "
                f"vượt quá giới hạn gửi file 50 MB của Telegram Bot API."
            )
            return

        safe_edit(
            chat_id, msg_id,
            f"📤 <b>[3/3]</b> Đang gửi file lên Telegram (<b>{file_size_mb:.1f} MB</b>)...\n"
            f"<code>[██████████] 100%</code>"
        )
        
        send_file_with_retry(chat_id, msg_id, saved_file, platform, file_size_mb)

        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    except Exception as e:
        safe_edit(
            chat_id, msg_id,
            f"❌ <b>Có lỗi xảy ra:</b>\n<code>{str(e)}</code>\n\n"
            f"💡 Mẹo: Hãy thử gửi lại link hoặc kiểm tra video có đặt ở chế độ riêng tư không nhé."
        )
    finally:
        if saved_file and os.path.exists(saved_file):
            try:
                os.remove(saved_file)
            except Exception:
                pass

if __name__ == '__main__':
    # Bật Web App Server (chạy đồng thời làm Web UI và Health-Check cho UptimeRobot)
    threading.Thread(target=start_web_server, daemon=True).start()
    port = os.getenv("PORT", "8080")
    print("=" * 60)
    print(" 🚀 UNIVERSAL VIDEO DOWNLOADER (Web App + Telegram Bot 2-in-1)")
    print(f" 🌐 Web App Online tại PORT: {port}")
    print("=" * 60)
    bot.infinity_polling()
