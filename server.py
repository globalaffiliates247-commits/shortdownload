import os
import json
import socket
import mimetypes
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import downloader

PORT = 8000
DOWNLOAD_DIR = os.path.expanduser("~/Downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

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
      <p class="text-sm text-slate-400">Hỗ trợ YouTube, Facebook Reel, TikTok không logo</p>
      
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
        <a id="saveFileBtn" href="#" class="w-full block text-center bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-2.5 rounded-xl text-sm transition">
          <i class="fa-solid fa-cloud-arrow-down mr-1"></i> Bấm để lưu file về máy
        </a>
      </div>
    </div>

    <!-- Mobile Connection Guide -->
    <div class="glass rounded-2xl p-4 text-xs text-slate-400 space-y-2">
      <div class="flex items-center gap-2 text-indigo-400 font-semibold">
        <i class="fa-solid fa-mobile-screen-button"></i>
        <span>Cách dùng trên iPhone / iPad (iOS):</span>
      </div>
      <p>1. Kết nối iPhone cùng mạng Wi-Fi với Mac này.</p>
      <p>2. Mở trình duyệt Safari trên iPhone và vào địa chỉ:</p>
      <div class="bg-slate-900 px-3 py-2 rounded-lg font-mono text-indigo-300 text-center select-all" id="lanIp">
        Đang lấy IP...
      </div>
      <p>3. Dán link và bấm tải, video sẽ lưu thẳng vào <b>Tệp (Files)</b> hoặc <b>Ảnh (Photos)</b> của iPhone!</p>
    </div>
  </div>

  <footer class="text-center text-xs text-slate-600 py-4">
    Universal Video Downloader &bull; Hỗ trợ Mac & iOS
  </footer>

  <script>
    // Tự động lấy LAN IP
    fetch('/api/ip').then(r => r.json()).then(d => {
      document.getElementById('lanIp').innerText = 'http://' + d.ip + ':' + d.port;
    }).catch(() => {});

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

      // Trạng thái đang tải
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> <span>Đang xử lý & tải từ nguồn...</span>';
      statusBox.className = "block bg-indigo-500/10 border border-indigo-500/20 text-indigo-300 p-4 rounded-2xl text-sm";
      statusBox.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-1"></i> Hệ thống đang trích xuất luồng video tốt nhất. Quá trình này mất khoảng 5 - 20 giây tuỳ độ dài video...';
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
          statusBox.innerHTML = '<i class="fa-solid fa-circle-check mr-1"></i> Tải và ghép video thành công!';
          
          // Hiện thẻ preview và nút lưu
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
          
          // Tự kích hoạt lưu nếu trên iPhone/Mac
          window.location.href = saveBtn.href;
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

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode('utf-8'))

        elif path == "/api/ip":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ip": get_local_ip(),
                "port": PORT
            }).encode('utf-8'))

        elif path.startswith("/api/file/"):
            filename = urllib.parse.unquote(path[len("/api/file/"):])
            filepath = os.path.join(DOWNLOAD_DIR, filename)

            # Bảo mật path traversal
            if not os.path.commonpath([DOWNLOAD_DIR, filepath]) == DOWNLOAD_DIR or not os.path.exists(filepath):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"File not found")
                return

            file_size = os.path.getsize(filepath)
            content_type, _ = mimetypes.guess_type(filepath)
            if not content_type:
                content_type = "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(filepath)}"')
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

                # 1. Trích thông tin video
                meta = {}
                try:
                    meta = downloader.extract_info(url)
                except Exception:
                    pass

                # 2. Thực hiện tải
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

            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def run_server():
    server_address = ('0.0.0.0', PORT)
    httpd = HTTPServer(server_address, RequestHandler)
    local_ip = get_local_ip()
    print("=" * 60)
    print(" 🚀 UNIVERSAL VIDEO DOWNLOADER ĐANG CHẠY")
    print("=" * 60)
    print(f"👉 Trên Mac: Mở trình duyệt vào  : http://localhost:{PORT}")
    print(f"👉 Trên iPhone/iPad (iOS)       : http://{local_ip}:{PORT}")
    print("=" * 60)
    print("Nhấn Ctrl + C để dừng máy chủ.")
    httpd.serve_forever()

if __name__ == '__main__':
    run_server()
