import os
import sys
import re
import time
import glob
import json
import datetime
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

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
LEADS_FILE = os.path.join(DATA_DIR, "leads.json")
WHITELIST_FILE = os.path.join(DATA_DIR, "whitelist.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
ADMIN_IPS_FILE = os.path.join(DATA_DIR, "admin_ips.json")

def load_admin_chat_id():
    """Lấy Admin Chat ID từ Settings nếu có, hoặc từ Env / Mặc định"""
    default_id = int(os.getenv("ADMIN_CHAT_ID", "1509053609"))
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "admin_chat_id" in data:
                    return int(data["admin_chat_id"])
    except Exception:
        pass
    return default_id

# Thông tin cấu hình nghiệp vụ của Chủ sở hữu (Admin)
ADMIN_CHAT_ID = load_admin_chat_id()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "vip_admin_taio")
ZALO_GROUP_URL = os.getenv("ZALO_GROUP_URL", "https://zalo.me/g/hhmdni386")
TELEGRAM_GROUP_URL = os.getenv("TELEGRAM_GROUP_URL", "https://t.me/your_community_channel")
CUSTOM_DOMAIN = os.getenv("CUSTOM_DOMAIN", "shotdownload.taio.club")

def is_admin_user(user_id, chat_id=None):
    """Kiểm tra xem user_id hoặc chat_id có phải là Admin không"""
    current_admin = ADMIN_CHAT_ID
    try:
        if user_id and int(user_id) == current_admin:
            return True
        if chat_id and int(chat_id) == current_admin:
            return True
        wl = load_whitelist()
        user_info = wl.get(str(user_id), {})
        if user_info.get("role") == "admin":
            return True
    except Exception:
        pass
    return False

# ==========================================
# HÀNG ĐỢI TUẦN TỰ (FIFO) & BẢO VỆ CHỐNG SPAM/DDOS
# ==========================================
class DownloadQueueManager:
    """Hệ thống hàng đợi tuần tự (FIFO) và Chống Spam/DDoS toàn diện cho máy chủ"""
    def __init__(self, max_queue_size=10):
        self.lock = threading.Lock()
        self.waiting = []
        self.active_users = set()
        self.max_queue_size = max_queue_size
        self.last_user_request = {} # user_key -> timestamp (cooldown)

    def request_slot(self, user_key, on_wait=None, timeout_seconds=120):
        """Yêu cầu vị trí tải tuần tự, đảm bảo tại 1 thời điểm chỉ có 1 video được tải/xử lý"""
        now = time.time()
        with self.lock:
            # 1. Kiểm tra Cooldown (chống spam liên tục trong 3 giây)
            last_time = self.last_user_request.get(user_key, 0)
            if now - last_time < 3:
                return False, "COOLDOWN", None

            # 2. Kiểm tra nếu user này đang có 1 video đang chạy hoặc đang trong hàng đợi
            if user_key in self.active_users:
                return False, "ALREADY_ACTIVE", None

            # 3. Kiểm tra nếu hàng đợi quá tải
            if len(self.waiting) >= self.max_queue_size:
                return False, "QUEUE_FULL", None

            self.last_user_request[user_key] = now
            self.active_users.add(user_key)
            ticket = {"id": user_key, "time": now}
            self.waiting.append(ticket)

        # Chờ đến lượt ở đầu hàng đợi (index 0)
        start_wait = time.time()
        last_notified_pos = -1

        while True:
            with self.lock:
                if ticket not in self.waiting:
                    return False, "CANCELLED", None
                idx = self.waiting.index(ticket)
                if idx == 0:
                    # Đã đến lượt xử lý
                    return True, "READY", ticket
                pos = idx

            # Nếu đang chờ, cập nhật thông báo cho user
            if pos != last_notified_pos:
                last_notified_pos = pos
                if on_wait:
                    try:
                        on_wait(pos)
                    except Exception:
                        pass

            if time.time() - start_wait > timeout_seconds:
                self.release_slot(ticket)
                return False, "TIMEOUT", None

            time.sleep(1.5)

    def release_slot(self, ticket):
        """Giải phóng vị trí sau khi tải xong để nhường lượt cho người tiếp theo"""
        if not ticket:
            return
        with self.lock:
            if ticket in self.waiting:
                self.waiting.remove(ticket)
            self.active_users.discard(ticket.get("id"))

download_queue = DownloadQueueManager(max_queue_size=10)

# ==========================================
# QUẢN LÝ DỮ LIỆU & PHÂN QUYỀN VIP
# ==========================================
def load_whitelist():
    """Tải danh sách các Telegram User ID đã được Admin cấp quyền VIP"""
    try:
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[!] Lỗi đọc whitelist: {e}")
    # Mặc định Admin luôn nằm trong whitelist
    return {str(ADMIN_CHAT_ID): {"name": "Admin", "phone": "Admin", "time": "Initial"}}

def save_whitelist(wl):
    """Lưu danh sách cấp quyền VIP vào file"""
    try:
        with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
            json.dump(wl, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] Lỗi lưu whitelist: {e}")

def load_admin_ips():
    """Tải danh sách các IP của Admin / VIP được miễn kiểm tra quota và mở khóa mọi tính năng"""
    ips = set()
    env_ips = os.getenv("ADMIN_IPS", "")
    if env_ips:
        for ip in env_ips.split(","):
            if ip.strip():
                ips.add(ip.strip())
    try:
        if os.path.exists(ADMIN_IPS_FILE):
            with open(ADMIN_IPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for ip in data:
                        ips.add(str(ip).strip())
                elif isinstance(data, dict):
                    for ip in data.keys():
                        ips.add(str(ip).strip())
    except Exception as e:
        print(f"[!] Lỗi đọc admin_ips: {e}")
    return ips

def save_admin_ip(ip, note="Registered via Web/Telegram"):
    """Thêm một IP vào danh sách Admin / VIP bền vững"""
    if not ip:
        return
    ip = ip.strip()
    try:
        data = {}
        if os.path.exists(ADMIN_IPS_FILE):
            with open(ADMIN_IPS_FILE, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, dict):
                    data = content
                elif isinstance(content, list):
                    data = {str(item): {"time": "legacy"} for item in content}
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data[ip] = {
            "time": now_str,
            "note": note
        }
        with open(ADMIN_IPS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[✓] Đã lưu IP Admin/VIP: {ip} ({note})")
    except Exception as e:
        print(f"[!] Lỗi ghi admin_ips: {e}")

def remove_admin_ip(ip):
    """Xóa IP khỏi danh sách Admin"""
    try:
        if os.path.exists(ADMIN_IPS_FILE):
            with open(ADMIN_IPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if ip in data:
                del data[ip]
                with open(ADMIN_IPS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                return True
    except Exception:
        pass
    return False

def is_admin_or_vip_request(handler, request_data=None):
    """
    Kiểm tra xem request từ Web có phải là Admin / VIP không.
    Hỗ trợ 4 tầng nhận diện:
    1. IP nằm trong danh sách Admin/VIP đã nạp (admin_ips.json hoặc ADMIN_IPS)
    2. Cookie 'admin_token' khớp với ADMIN_TOKEN
    3. Query param hoặc Header 'X-Admin-Token' khớp với ADMIN_TOKEN
    4. Payload JSON có 'admin_token' khớp với ADMIN_TOKEN
    """
    client_ip = handler.headers.get('X-Forwarded-For', handler.client_address[0]).split(',')[0].strip()
    admin_ips = load_admin_ips()
    if client_ip in admin_ips:
        return True, client_ip, "IP Whitelist"

    auth_header = handler.headers.get('X-Admin-Token', '').strip()
    if auth_header and auth_header == ADMIN_TOKEN:
        save_admin_ip(client_ip, note="Auto-saved via X-Admin-Token header")
        return True, client_ip, "Header Token"

    cookie_header = handler.headers.get('Cookie', '')
    if cookie_header:
        for cookie_part in cookie_header.split(';'):
            if '=' in cookie_part:
                k, v = cookie_part.strip().split('=', 1)
                if k == 'admin_token' and v == ADMIN_TOKEN:
                    save_admin_ip(client_ip, note="Auto-saved via Cookie")
                    return True, client_ip, "Cookie Session"

    parsed = urllib.parse.urlparse(handler.path)
    query_params = urllib.parse.parse_qs(parsed.query)
    url_token = query_params.get('token', [''])[0] or query_params.get('vip', [''])[0]
    if url_token and url_token == ADMIN_TOKEN:
        save_admin_ip(client_ip, note="Auto-saved via URL query param")
        return True, client_ip, "URL Token"

    if request_data and isinstance(request_data, dict):
        body_token = request_data.get('admin_token', '').strip()
        if body_token and body_token == ADMIN_TOKEN:
            save_admin_ip(client_ip, note="Auto-saved via Request Body")
            return True, client_ip, "Body Token"

    return False, client_ip, "Normal User"

def save_lead(name, phone, email, source, extra=None):
    """Lưu thông tin khách hàng và bắn thông báo trực tiếp về Telegram của Admin"""
    leads = []
    try:
        if os.path.exists(LEADS_FILE):
            with open(LEADS_FILE, "r", encoding="utf-8") as f:
                leads = json.load(f)
    except Exception:
        leads = []

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lead_entry = {
        "name": name,
        "phone": phone,
        "email": email or "",
        "source": source,
        "created_at": now_str,
        "extra": extra or {}
    }
    leads.append(lead_entry)

    try:
        with open(LEADS_FILE, "w", encoding="utf-8") as f:
            json.dump(leads, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] Lỗi ghi lead: {e}")

    # Bắn thông báo ngay lập tức cho Admin qua Telegram
    if bot and ADMIN_CHAT_ID:
        try:
            admin_msg = (
                f"🔔 <b>[CÓ KHÁCH HÀNG MỚI ĐĂNG KÝ]</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Họ tên:</b> {name}\n"
                f"📞 <b>SĐT / Zalo:</b> <code>{phone}</code>\n"
                f"📧 <b>Email:</b> {email or 'Không để lại'}\n"
                f"🌐 <b>Nguồn:</b> {source}\n"
                f"⏰ <b>Thời gian:</b> {now_str}\n"
            )
            if source == "telegram_bot" and extra and "user_id" in extra:
                uid = extra["user_id"]
                admin_msg += f"\n👉 <b>Bấm lệnh sau để DUYỆT VIP cho khách:</b>\n/duyet_{uid}"

            bot.send_message(ADMIN_CHAT_ID, admin_msg, parse_mode="HTML")
        except Exception as e:
            print(f"[!] Lỗi gửi thông báo lead tới Admin: {e}")

    return lead_entry

def cleanup_old_files(directory=DOWNLOAD_DIR, max_age_seconds=600):
    """Tự động dọn dẹp file tạm cũ hơn 10 phút để tránh đầy dung lượng máy chủ"""
    now = time.time()
    for f in glob.glob(os.path.join(directory, "*")):
        try:
            if os.path.isfile(f) and (now - os.path.getmtime(f) > max_age_seconds):
                os.remove(f)
        except Exception:
            pass

def is_sync_enabled():
    """Kiểm tra xem Admin có bật tính năng tự động nhận bản sao video hay không"""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("sync_to_admin", True)
    except Exception:
        pass
    return True

def set_sync_enabled(enabled: bool):
    """Bật / Tắt tính năng tự động nhận bản sao video"""
    try:
        data = {}
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["sync_to_admin"] = enabled
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] Lỗi lưu settings: {e}")

def sync_video_to_admin(filepath, original_url, source_name):
    """Tự động gửi bản sao video khách vừa tải về Telegram của Admin"""
    if not is_sync_enabled() or not ADMIN_CHAT_ID:
        return

    if not filepath or not os.path.exists(filepath):
        return

    try:
        file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
        filename = os.path.basename(filepath)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        caption = (
            f"📥 <b>[BẢN SAO VIDEO KHÁCH VỪA TẢI]</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎬 <b>File:</b> <code>{filename[:60]}</code>\n"
            f"🌐 <b>Nguồn:</b> {source_name}\n"
            f"🔗 <b>Link gốc:</b> {original_url}\n"
            f"📦 <b>Dung lượng:</b> {file_size_mb:.1f} MB\n"
            f"⏰ <b>Thời gian:</b> {now_str}"
        )

        # Trường hợp file > 50MB (vượt trần của Telegram Bot API)
        if file_size_mb > 50:
            encoded_fn = urllib.parse.quote(filename)
            download_link = f"https://{CUSTOM_DOMAIN}/api/file/{encoded_fn}"
            big_file_msg = (
                f"{caption}\n\n"
                f"⚠️ <i>Video này có dung lượng lớn ({file_size_mb:.1f} MB > 50MB) nên Telegram không cho gửi file trực tiếp.</i>\n"
                f"👉 <b>Bấm link sau để tải về máy:</b>\n"
                f"<a href='{download_link}'>{download_link}</a>"
            )
            bot.send_message(ADMIN_CHAT_ID, big_file_msg, parse_mode="HTML", disable_web_page_preview=True)
            return

        # Trường hợp bình thường <= 50MB: Bắn video trực tiếp
        is_mp4 = filepath.endswith('.mp4')
        with open(filepath, 'rb') as f:
            if is_mp4:
                bot.send_video(
                    chat_id=ADMIN_CHAT_ID,
                    video=f,
                    caption=caption,
                    parse_mode='HTML',
                    supports_streaming=True,
                    timeout=180
                )
            else:
                bot.send_document(
                    chat_id=ADMIN_CHAT_ID,
                    document=f,
                    caption=caption,
                    parse_mode='HTML',
                    timeout=240
                )
        print(f"[✓] Đã gửi bản sao video sang Admin Telegram ({file_size_mb:.1f} MB).")
    except Exception as e:
        print(f"[!] Lỗi khi đồng bộ video sang Admin: {e}")

# ==========================================
# GIAO DIỆN WEB HIỆN ĐẠI (CÁCH 3) TRÊN CLOUD
# TÍCH HỢP QUOTA 3 LẦN + VIRAL LEAD GATE
# ==========================================
HTML_PAGE = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>TAIO Video Downloader Studio | Tải Video Đa Năng</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=Segoe+UI:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    /* Desktop OS Styles */
    body {
      background: radial-gradient(circle at 50% 20%, #1e1b4b 0%, #090d16 60%, #030712 100%);
      color: #f1f5f9;
      min-height: 100vh;
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
    }

    /* Windows 11 Fluent Theme */
    .win-mode {
      font-family: 'Segoe UI', 'Segoe UI Variable', system-ui, sans-serif;
    }
    .win-mode .os-window {
      background: rgba(30, 32, 40, 0.94);
      backdrop-filter: blur(28px) saturate(180%);
      -webkit-backdrop-filter: blur(28px) saturate(180%);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 10px;
      box-shadow: 0 25px 60px -15px rgba(0, 0, 0, 0.8), 0 0 0 1px rgba(255, 255, 255, 0.08);
    }
    .win-mode .os-titlebar {
      background: rgba(22, 24, 30, 0.85);
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 9px 9px 0 0;
    }
    .win-mode .win-control-btn {
      width: 46px;
      height: 34px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: all 0.15s ease;
      font-size: 11px;
      color: #94a3b8;
    }
    .win-mode .win-control-btn:hover {
      background: rgba(255, 255, 255, 0.1);
      color: #ffffff;
    }
    .win-mode .win-close-btn:hover {
      background: #e81123 !important;
      color: #ffffff !important;
    }

    /* macOS Cupertino Theme */
    .mac-mode {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", system-ui, sans-serif;
    }
    .mac-mode .os-window {
      background: rgba(26, 31, 46, 0.82);
      backdrop-filter: blur(35px) saturate(190%);
      -webkit-backdrop-filter: blur(35px) saturate(190%);
      border: 1px solid rgba(255, 255, 255, 0.15);
      border-radius: 18px;
      box-shadow: 0 30px 70px -12px rgba(0, 0, 0, 0.85), 0 0 1px 1px rgba(255, 255, 255, 0.12);
    }
    .mac-mode .os-titlebar {
      background: rgba(18, 22, 33, 0.7);
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 17px 17px 0 0;
    }
    .mac-mode .mac-dot {
      width: 12px;
      height: 12px;
      border-radius: 9999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      position: relative;
      transition: all 0.15s ease;
    }
    .mac-mode .mac-dot:hover svg {
      opacity: 1;
    }

    /* Custom Scrollbar & Inputs */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.15); border-radius: 9999px; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.3); }

    .glow-effect {
      box-shadow: 0 0 30px -5px rgba(99, 102, 241, 0.3);
    }
  </style>
</head>
<body id="osBody" class="win-mode flex flex-col justify-between p-3 sm:p-6 transition-all duration-300">

  <!-- TOP HEADER / BRAND BAR -->
  <header class="w-full max-w-4xl mx-auto flex items-center justify-between py-2 px-1 text-xs text-slate-400">
    <!-- TAIO Brand Logo -->
    <div class="flex items-center gap-3">
      <div class="relative w-9 h-9 rounded-xl bg-gradient-to-tr from-cyan-400 via-indigo-500 to-fuchsia-500 p-[1.5px] shadow-lg shadow-indigo-500/25">
        <div class="w-full h-full bg-slate-950 rounded-[10px] flex items-center justify-center">
          <svg class="w-5 h-5 text-indigo-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
            <polyline points="2 17 12 22 22 17"></polyline>
            <polyline points="2 12 12 17 22 12"></polyline>
          </svg>
        </div>
      </div>
      <div>
        <div class="flex items-center gap-2">
          <span class="text-base font-extrabold tracking-wider text-white">TAIO<span class="text-indigo-400">.CLUB</span></span>
          <span class="text-[9px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-md bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">PRO STUDIO</span>
        </div>
        <p class="text-[11px] text-slate-400 font-normal">Trình Tải Video Đa Năng Chuẩn Desktop</p>
      </div>
    </div>

    <!-- Quick Community Links -->
    <div class="flex items-center gap-2 sm:gap-3">
      <span class="hidden sm:inline-flex items-center gap-1.5 text-emerald-400 bg-emerald-500/10 px-2.5 py-1 rounded-full border border-emerald-500/20">
        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping"></span>
        Online 24/7
      </span>
      <a href="__TELEGRAM_GROUP_URL__" target="_blank" class="inline-flex items-center gap-1.5 text-sky-400 hover:text-sky-300 transition font-medium bg-sky-500/10 px-2.5 py-1.5 rounded-lg border border-sky-500/20">
        <i class="fa-brands fa-telegram"></i>
        <span>Nhóm Tele</span>
      </a>
      <a href="__ZALO_GROUP_URL__" target="_blank" class="inline-flex items-center gap-1.5 text-blue-400 hover:text-blue-300 transition font-medium bg-blue-500/10 px-2.5 py-1.5 rounded-lg border border-blue-500/20">
        <i class="fa-solid fa-users"></i>
        <span>Nhóm Zalo</span>
      </a>
    </div>
  </header>

  <!-- MAIN DESKTOP WINDOW CONTAINER -->
  <main class="w-full max-w-4xl mx-auto my-auto py-3">
    <div id="desktopWindow" class="os-window transition-all duration-300">
      
      <!-- DESKTOP TITLE BAR (Dynamic for Windows 11 vs macOS) -->
      <div class="os-titlebar flex items-center justify-between px-3 py-2 select-none">
        
        <!-- macOS Left Traffic Lights (Shown in mac-mode) -->
        <div id="macTrafficLights" class="hidden items-center gap-2 pl-1">
          <div class="mac-dot bg-[#ff5f56] border border-[#e0443e]" onclick="simulateMinMax('close')" title="Đóng">
            <svg class="w-2 h-2 text-[#4c0000] opacity-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </div>
          <div class="mac-dot bg-[#ffbd2e] border border-[#dea123]" onclick="simulateMinMax('min')" title="Thu nhỏ">
            <svg class="w-2 h-2 text-[#5a3a00] opacity-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4"><path d="M5 12h14"/></svg>
          </div>
          <div class="mac-dot bg-[#27c93f] border border-[#1aab29]" onclick="simulateMinMax('max')" title="Phóng to">
            <svg class="w-2 h-2 text-[#003800] opacity-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4"><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/></svg>
          </div>
        </div>

        <!-- Windows Left Window Icon & Title (Shown in win-mode) -->
        <div id="winTitleInfo" class="flex items-center gap-2 text-xs text-slate-300 font-medium">
          <i class="fa-solid fa-cloud-arrow-down text-indigo-400"></i>
          <span>TAIO Studio v2.4 (x64) — Windows 11 Fluent Edition</span>
        </div>

        <!-- macOS Center App Title (Shown in mac-mode) -->
        <div id="macCenterTitle" class="hidden items-center gap-1.5 text-xs text-slate-300 font-semibold tracking-wide">
          <i class="fa-solid fa-lock text-[10px] text-emerald-400"></i>
          <span>TAIO Studio — Video Downloader (Universal)</span>
        </div>

        <!-- Right Side: OS SWITCHER & WINDOWS CONTROLS -->
        <div class="flex items-center gap-2">
          
          <!-- OS Switcher Toggle Button (Win 11 / Mac) -->
          <div class="flex items-center bg-black/50 p-0.5 rounded-lg border border-white/10 text-xs">
            <button id="btnSwitchWin" onclick="setOS('win')" class="flex items-center gap-1 px-2.5 py-1 rounded-md transition font-medium text-white bg-indigo-600/70 shadow-sm" title="Chuyển sang giao diện Windows 11">
              <i class="fa-brands fa-windows text-sky-400"></i>
              <span class="hidden sm:inline">Windows</span>
            </button>
            <button id="btnSwitchMac" onclick="setOS('mac')" class="flex items-center gap-1 px-2.5 py-1 rounded-md transition font-medium text-slate-400 hover:text-white" title="Chuyển sang giao diện macOS Cupertino">
              <i class="fa-brands fa-apple text-slate-200"></i>
              <span class="hidden sm:inline">macOS</span>
            </button>
          </div>

          <!-- Windows 11 Right Action Controls (Min, Max, Close) -->
          <div id="winRightControls" class="flex items-center -mr-3 -my-2">
            <button class="win-control-btn" onclick="simulateMinMax('min')" title="Thu nhỏ">
              <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14"/></svg>
            </button>
            <button class="win-control-btn" onclick="simulateMinMax('max')" title="Phóng to / Thu lại">
              <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
            </button>
            <button class="win-control-btn win-close-btn" onclick="simulateMinMax('close')" title="Đóng">
              <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
          </div>

        </div>

      </div>

      <!-- DESKTOP MENU BAR (Windows 11 / macOS Toolbar) -->
      <div id="osMenuBar" class="flex items-center justify-between px-4 py-1.5 text-xs bg-slate-900/60 border-b border-white/5 text-slate-400">
        <div class="flex items-center gap-4">
          <div class="relative group cursor-pointer hover:text-white transition">
            <span>Tệp (File)</span>
            <div class="hidden group-hover:block absolute left-0 top-full mt-1 w-44 bg-slate-800 border border-slate-700 rounded-lg shadow-xl py-1 z-50 text-xs">
              <button onclick="pasteFromClipboard()" class="w-full text-left px-3 py-1.5 hover:bg-slate-700/80 flex items-center justify-between">
                <span>Dán URL</span><span class="text-slate-400">Ctrl+V</span>
              </button>
              <button onclick="clearInput()" class="w-full text-left px-3 py-1.5 hover:bg-slate-700/80 flex items-center justify-between">
                <span>Xóa ô nhập</span><span class="text-slate-400">Esc</span>
              </button>
              <div class="border-t border-slate-700/60 my-1"></div>
              <button onclick="location.reload()" class="w-full text-left px-3 py-1.5 hover:bg-slate-700/80 flex items-center justify-between">
                <span>Làm mới app</span><span class="text-slate-400">F5</span>
              </button>
            </div>
          </div>
          <div class="relative group cursor-pointer hover:text-white transition">
            <span>Công cụ</span>
            <div class="hidden group-hover:block absolute left-0 top-full mt-1 w-48 bg-slate-800 border border-slate-700 rounded-lg shadow-xl py-1 z-50 text-xs">
              <button onclick="openVipModal()" class="w-full text-left px-3 py-1.5 hover:bg-slate-700/80 flex items-center gap-2">
                <i class="fa-solid fa-crown text-amber-400"></i>
                <span>Xác thực Admin / VIP</span>
              </button>
              <button onclick="openUnlockModal()" class="w-full text-left px-3 py-1.5 hover:bg-slate-700/80 flex items-center gap-2">
                <i class="fa-solid fa-gift text-pink-400"></i>
                <span>Nhận thêm lượt tải</span>
              </button>
            </div>
          </div>
          <div class="relative group cursor-pointer hover:text-white transition">
            <span>Nền tảng</span>
            <div class="hidden group-hover:block absolute left-0 top-full mt-1 w-48 bg-slate-800 border border-slate-700 rounded-lg shadow-xl py-1 z-50 text-xs">
              <button onclick="switchPlatform('tiktok')" class="w-full text-left px-3 py-1.5 hover:bg-slate-700/80 flex items-center gap-2">
                <i class="fa-brands fa-tiktok text-cyan-400"></i>
                <span>TikTok (Không Logo)</span>
              </button>
              <button onclick="switchPlatform('facebook')" class="w-full text-left px-3 py-1.5 hover:bg-slate-700/80 flex items-center gap-2">
                <i class="fa-brands fa-facebook text-blue-400"></i>
                <span>Facebook Reels</span>
              </button>
              <button onclick="switchPlatform('youtube')" class="w-full text-left px-3 py-1.5 hover:bg-slate-700/80 flex items-center gap-2">
                <i class="fa-brands fa-youtube text-red-400"></i>
                <span>YouTube (VIP)</span>
              </button>
            </div>
          </div>
          <a href="__TELEGRAM_BOT_URL__" target="_blank" class="hover:text-white transition">
            Bot Telegram
          </a>
        </div>
        <div class="flex items-center gap-3 text-[11px]">
          <span id="vipBadgeTop" class="hidden items-center gap-1 text-amber-400 font-semibold bg-amber-500/10 px-2 py-0.5 rounded border border-amber-500/20">
            <i class="fa-solid fa-crown"></i> VIP ADMIN
          </span>
          <span class="text-slate-400 font-mono">IP: <span id="clientIpDisplay">__CLIENT_IP__</span></span>
        </div>
      </div>

      <!-- WINDOW BODY CONTENT -->
      <div id="windowBodyContent" class="p-4 sm:p-7 space-y-6">

        <!-- 1. SPOTLIGHT CARD: TIKTOK KHÔNG LOGO (BẮT MẮT) -->
        <div class="relative overflow-hidden rounded-2xl bg-gradient-to-r from-cyan-950/60 via-slate-900/80 to-pink-950/60 border border-cyan-500/30 p-4 sm:p-5 glow-effect">
          <div class="absolute -right-6 -bottom-6 w-36 h-36 bg-cyan-500/10 rounded-full blur-2xl pointer-events-none"></div>
          <div class="absolute -left-6 -top-6 w-36 h-36 bg-pink-500/10 rounded-full blur-2xl pointer-events-none"></div>

          <div class="relative z-10 space-y-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-cyan-500/15 border border-cyan-400/30 text-cyan-300 text-xs font-bold tracking-wide">
                <i class="fa-solid fa-bolt text-yellow-400 animate-pulse"></i>
                <span>TÍNH NĂNG TÂM ĐIỂM CỦA STUDIO</span>
              </div>
              <span class="text-xs text-slate-300 font-medium">Bóc Tách CDN Trực Tiếp &bull; Cập nhật 2026</span>
            </div>

            <div class="space-y-1">
              <h2 class="text-lg sm:text-xl font-black text-transparent bg-clip-text bg-gradient-to-r from-cyan-300 via-white to-pink-300">
                Tải TikTok Không Logo Siêu Tốc (0.5s) &bull; Chuẩn 4K / HD
              </h2>
              <p class="text-xs sm:text-sm text-slate-300 leading-relaxed">
                Hệ thống tự động phân tích luồng phát gốc từ CDN máy chủ TikTok. Xóa sạch <span class="text-pink-300 font-semibold">100% Logo & ID nhảy góc</span>, hỗ trợ tải trọn bộ <span class="text-cyan-300 font-semibold">Album ảnh lướt (Slideshow)</span> kèm trọn vẹn file nhạc MP3 chất lượng 320kbps!
              </p>
            </div>

            <!-- 4 Spotlight Feature Pills -->
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-1 text-xs">
              <div class="flex items-center gap-2 bg-slate-950/60 border border-white/10 rounded-xl p-2.5">
                <i class="fa-solid fa-gauge-high text-cyan-400 text-base"></i>
                <div>
                  <div class="font-bold text-white">0.5s CDN</div>
                  <div class="text-[10px] text-slate-400">Tốc độ tức thì</div>
                </div>
              </div>
              <div class="flex items-center gap-2 bg-slate-950/60 border border-white/10 rounded-xl p-2.5">
                <i class="fa-solid fa-wand-magic-sparkles text-pink-400 text-base"></i>
                <div>
                  <div class="font-bold text-white">100% Sạch Logo</div>
                  <div class="text-[10px] text-slate-400">Không dính watermark</div>
                </div>
              </div>
              <div class="flex items-center gap-2 bg-slate-950/60 border border-white/10 rounded-xl p-2.5">
                <i class="fa-solid fa-images text-indigo-400 text-base"></i>
                <div>
                  <div class="font-bold text-white">Full Album Ảnh</div>
                  <div class="text-[10px] text-slate-400">Hỗ trợ Slideshow</div>
                </div>
              </div>
              <div class="flex items-center gap-2 bg-slate-950/60 border border-white/10 rounded-xl p-2.5">
                <i class="fa-solid fa-music text-emerald-400 text-base"></i>
                <div>
                  <div class="font-bold text-white">Nhạc Gốc 320k</div>
                  <div class="text-[10px] text-slate-400">Âm thanh chuẩn phòng thu</div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 2. PLATFORM TABS (ORDER: TIKTOK -> FB REELS -> YOUTUBE) -->
        <div class="space-y-2">
          <label class="block text-xs font-semibold text-slate-400 uppercase tracking-wider">
            Chọn Nền Tảng Video:
          </label>
          <div class="grid grid-cols-3 gap-2 sm:gap-3">
            
            <!-- 1. TIKTOK TAB (DEFAULT) -->
            <button id="tabTiktok" onclick="switchPlatform('tiktok')" class="group relative flex items-center justify-center gap-2 py-3 px-3 rounded-xl border border-cyan-500/50 bg-gradient-to-b from-cyan-500/20 to-slate-900 text-white font-semibold text-xs sm:text-sm shadow-md transition-all">
              <i class="fa-brands fa-tiktok text-cyan-400 text-base group-hover:scale-110 transition"></i>
              <span>TikTok <span class="hidden sm:inline text-cyan-300 text-[11px] font-normal">(Không logo)</span></span>
              <span class="absolute -top-1.5 -right-1 px-1.5 py-0.2 bg-cyan-500 text-slate-950 text-[9px] font-extrabold rounded-full uppercase">Ưu tiên</span>
            </button>

            <!-- 2. FACEBOOK REELS TAB -->
            <button id="tabFacebook" onclick="switchPlatform('facebook')" class="group flex items-center justify-center gap-2 py-3 px-3 rounded-xl border border-white/10 bg-slate-900/60 text-slate-300 hover:text-white hover:border-blue-500/40 font-semibold text-xs sm:text-sm transition-all">
              <i class="fa-brands fa-facebook text-blue-400 text-base group-hover:scale-110 transition"></i>
              <span>FB Reels</span>
            </button>

            <!-- 3. YOUTUBE TAB -->
            <button id="tabYoutube" onclick="switchPlatform('youtube')" class="group relative flex items-center justify-center gap-2 py-3 px-3 rounded-xl border border-white/10 bg-slate-900/60 text-slate-300 hover:text-white hover:border-red-500/40 font-semibold text-xs sm:text-sm transition-all">
              <i class="fa-brands fa-youtube text-red-500 text-base group-hover:scale-110 transition"></i>
              <span>YouTube <span class="text-amber-400 text-[10px] font-bold">(VIP)</span></span>
            </button>

          </div>
        </div>

        <!-- 3. QUOTA / VIP STATUS BAR -->
        <div class="flex items-center justify-between p-3 rounded-xl bg-slate-900/70 border border-white/5 text-xs">
          <div class="flex items-center gap-2">
            <i class="fa-solid fa-gift text-pink-400 text-sm"></i>
            <span class="text-slate-300">Lượt tải miễn phí hôm nay:</span>
            <span id="quotaCountBadge" class="px-2 py-0.5 rounded-md bg-amber-500/20 text-amber-300 font-bold border border-amber-500/30">
              3 lượt
            </span>
          </div>
          <button onclick="openUnlockModal()" class="text-indigo-400 hover:text-indigo-300 font-semibold transition flex items-center gap-1">
            <span>Nhận thêm lượt tải</span>
            <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>
          </button>
        </div>

        <!-- 4. URL INPUT & DOWNLOAD FORM -->
        <div class="space-y-3">
          <div class="relative">
            <div class="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-slate-400">
              <i id="inputPlatformIcon" class="fa-solid fa-link text-indigo-400"></i>
            </div>
            <input 
              type="text" 
              id="videoUrl" 
              placeholder="Dán link video TikTok cần tải không logo (https://vt.tiktok.com/...)" 
              class="w-full pl-10 pr-24 py-3.5 bg-slate-950/80 border border-white/15 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/30 rounded-xl text-sm text-white placeholder-slate-500 transition outline-none"
            >
            <button 
              type="button" 
              onclick="pasteFromClipboard()" 
              class="absolute inset-y-1.5 right-1.5 px-3.5 bg-slate-800 hover:bg-slate-700 active:bg-slate-600 text-slate-200 text-xs font-semibold rounded-lg border border-white/10 transition flex items-center gap-1.5"
            >
              <i class="fa-regular fa-paste"></i>
              <span>Dán</span>
            </button>
          </div>

          <!-- Download Action Button -->
          <button 
            id="downloadBtn" 
            onclick="handleDownload()" 
            class="w-full py-4 px-6 rounded-xl bg-gradient-to-r from-indigo-600 via-indigo-500 to-purple-600 hover:from-indigo-500 hover:to-purple-500 active:scale-[0.99] text-white font-bold text-sm sm:text-base shadow-lg shadow-indigo-600/30 transition-all flex items-center justify-center gap-2"
          >
            <i class="fa-solid fa-cloud-arrow-down text-lg"></i>
            <span>Bắt Đầu Tải Video Ngay</span>
          </button>
        </div>

        <!-- 5. DOWNLOAD PROGRESS / STATUS NOTIFICATION -->
        <div id="statusBox" class="hidden p-4 rounded-xl text-sm transition-all duration-300"></div>

        <!-- 6. VIDEO PREVIEW & RESULT CARD -->
        <div id="resultCard" class="hidden p-4 sm:p-5 rounded-2xl bg-slate-900/90 border border-indigo-500/30 space-y-4">
          <div class="flex items-start gap-4">
            <img id="resultThumb" src="" alt="Thumbnail" class="w-24 h-24 sm:w-28 sm:h-28 rounded-xl object-cover bg-slate-950 border border-white/10 shadow-md">
            <div class="space-y-1.5 flex-1 min-w-0">
              <span class="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
                <i class="fa-solid fa-circle-check"></i> Trích Xuất Thành Công
              </span>
              <h3 id="resultTitle" class="text-sm sm:text-base font-bold text-white truncate">Tiêu đề video</h3>
              <p id="resultUploader" class="text-xs text-slate-400">Tác giả: Đang cập nhật</p>
              <div class="flex items-center gap-2 pt-1">
                <a id="resultDownloadLink" href="#" download class="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs sm:text-sm shadow-md transition">
                  <i class="fa-solid fa-download"></i>
                  <span>Lưu về thiết bị</span>
                </a>
              </div>
            </div>
          </div>
        </div>

        <!-- 7. TELEGRAM BOT VIP CALLOUT -->
        <div class="flex flex-col sm:flex-row items-center justify-between gap-3 p-4 rounded-xl bg-gradient-to-r from-sky-950/40 via-slate-900/60 to-slate-900/80 border border-sky-500/20">
          <div class="flex items-center gap-3">
            <div class="w-10 h-10 rounded-xl bg-sky-500/20 border border-sky-500/30 flex items-center justify-center text-sky-400 text-lg">
              <i class="fa-brands fa-telegram"></i>
            </div>
            <div>
              <div class="flex items-center gap-2">
                <span class="font-bold text-white text-sm">Bot Telegram Trợ Lý 24/7</span>
                <span class="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">VIP CLOUD</span>
              </div>
              <p class="text-xs text-slate-400">Tải không giới hạn, hỗ trợ YouTube & TikTok chất lượng gốc</p>
            </div>
          </div>
          <a href="__TELEGRAM_BOT_URL__" target="_blank" class="w-full sm:w-auto text-center px-4 py-2.5 rounded-lg bg-sky-600 hover:bg-sky-500 text-white font-bold text-xs shadow-md transition">
            Mở Telegram Bot
          </a>
        </div>

      </div>

      <!-- DESKTOP STATUS BAR (Windows 11 / macOS Bottom Frame) -->
      <div id="osStatusBar" class="flex items-center justify-between px-4 py-2 text-[11px] bg-slate-950/80 border-t border-white/5 text-slate-400 select-none">
        <div class="flex items-center gap-3">
          <span class="flex items-center gap-1.5 text-emerald-400">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
            <span id="osStatusText">Sẵn sàng</span>
          </span>
          <span class="hidden sm:inline text-slate-500">|</span>
          <span class="hidden sm:inline">Động cơ: TikCDN v4 + yt-dlp</span>
        </div>
        <div class="flex items-center gap-3 font-mono text-[10px]">
          <span class="hidden sm:inline">RAM: 64MB / 512MB</span>
          <span class="hidden sm:inline text-slate-500">&bull;</span>
          <span id="osSystemLabel">Windows 11 Fluent</span>
        </div>
      </div>

    </div>
  </main>

  <!-- FOOTER -->
  <footer class="w-full max-w-4xl mx-auto text-center py-2 text-xs text-slate-500">
    <p>TAIO.Club Universal Studio &bull; Hỗ trợ iOS, Android, macOS & Windows &bull; Bản quyền 2026</p>
  </footer>

  <!-- MODAL: NHẬN THÊM LƯỢT TẢI (LEAD GATE) -->
  <div id="unlockModal" class="hidden fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
    <div class="relative w-full max-w-md rounded-2xl bg-slate-900 border border-indigo-500/30 p-6 space-y-4 shadow-2xl">
      <div class="text-center space-y-1">
        <div class="w-12 h-12 mx-auto rounded-full bg-pink-500/20 border border-pink-500/30 flex items-center justify-center text-pink-400 text-xl">
          <i class="fa-solid fa-gift"></i>
        </div>
        <h3 class="text-lg font-bold text-white">Nhận Thêm 10 Lượt Tải Miễn Phí!</h3>
        <p class="text-xs text-slate-400">Nhập số điện thoại Zalo của bạn để hệ thống cấp thêm lượt tải ngay lập tức</p>
      </div>

      <div class="space-y-3">
        <input 
          type="tel" 
          id="leadPhone" 
          placeholder="Số điện thoại Zalo của bạn (VD: 0912345678)" 
          class="w-full px-4 py-3 bg-slate-950 border border-white/10 rounded-xl text-sm text-white placeholder-slate-500 outline-none focus:border-indigo-500"
        >
        <button 
          onclick="submitLeadPhone()" 
          class="w-full py-3 bg-gradient-to-r from-pink-600 to-indigo-600 hover:from-pink-500 hover:to-indigo-500 text-white font-bold text-sm rounded-xl transition"
        >
          Xác Nhận & Mở Khóa Lượt Tải
        </button>
      </div>

      <div class="pt-2 text-center">
        <button onclick="closeUnlockModal()" class="text-xs text-slate-400 hover:text-white transition">
          Để sau, tôi sẽ tiếp tục
        </button>
      </div>
    </div>
  </div>

  <!-- MODAL: XÁC THỰC ADMIN / VIP -->
  <div id="vipAuthModal" class="hidden fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
    <div class="relative w-full max-w-md rounded-2xl bg-slate-900 border border-amber-500/30 p-6 space-y-4 shadow-2xl">
      <div class="text-center space-y-1">
        <div class="w-12 h-12 mx-auto rounded-full bg-amber-500/20 border border-amber-500/30 flex items-center justify-center text-amber-400 text-xl">
          <i class="fa-solid fa-crown"></i>
        </div>
        <h3 class="text-lg font-bold text-white">Xác Thực Quản Trị / Gói VIP</h3>
        <p class="text-xs text-slate-400">Nhập mã Token Admin hoặc tài khoản VIP để mở khóa tải YouTube & không giới hạn lượt</p>
      </div>

      <div class="space-y-3">
        <input 
          type="text" 
          id="vipTokenInput" 
          placeholder="Nhập Token Admin hoặc mã bí mật" 
          class="w-full px-4 py-3 bg-slate-950 border border-white/10 rounded-xl text-sm text-white placeholder-slate-500 outline-none focus:border-amber-500"
        >
        <button 
          onclick="submitVipToken()" 
          class="w-full py-3 bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-500 hover:to-orange-500 text-white font-bold text-sm rounded-xl transition"
        >
          Xác Thực Ngay
        </button>
      </div>

      <div class="pt-2 text-center">
        <button onclick="closeVipModal()" class="text-xs text-slate-400 hover:text-white transition">
          Đóng cửa sổ
        </button>
      </div>
    </div>
  </div>

  <!-- JAVASCRIPT LOGIC -->
  <script>
    // 1. Quản lý Theme Desktop OS (Windows 11 vs macOS)
    let currentOS = localStorage.getItem('taio_desktop_os') || 'win';
    const osBody = document.getElementById('osBody');
    const macTrafficLights = document.getElementById('macTrafficLights');
    const winTitleInfo = document.getElementById('winTitleInfo');
    const macCenterTitle = document.getElementById('macCenterTitle');
    const winRightControls = document.getElementById('winRightControls');
    const btnSwitchWin = document.getElementById('btnSwitchWin');
    const btnSwitchMac = document.getElementById('btnSwitchMac');
    const osSystemLabel = document.getElementById('osSystemLabel');

    function applyOSTheme() {
      if (currentOS === 'mac') {
        osBody.classList.remove('win-mode');
        osBody.classList.add('mac-mode');

        macTrafficLights.classList.remove('hidden');
        macTrafficLights.classList.add('flex');
        macCenterTitle.classList.remove('hidden');
        macCenterTitle.classList.add('flex');

        winTitleInfo.classList.add('hidden');
        winRightControls.classList.add('hidden');

        btnSwitchMac.className = "flex items-center gap-1 px-2.5 py-1 rounded-md transition font-medium text-white bg-indigo-600/70 shadow-sm";
        btnSwitchWin.className = "flex items-center gap-1 px-2.5 py-1 rounded-md transition font-medium text-slate-400 hover:text-white";

        osSystemLabel.innerText = "macOS Sequoia 15.1 Glass";
      } else {
        osBody.classList.remove('mac-mode');
        osBody.classList.add('win-mode');

        macTrafficLights.classList.add('hidden');
        macTrafficLights.classList.remove('flex');
        macCenterTitle.classList.add('hidden');
        macCenterTitle.classList.remove('flex');

        winTitleInfo.classList.remove('hidden');
        winRightControls.classList.remove('hidden');

        btnSwitchWin.className = "flex items-center gap-1 px-2.5 py-1 rounded-md transition font-medium text-white bg-indigo-600/70 shadow-sm";
        btnSwitchMac.className = "flex items-center gap-1 px-2.5 py-1 rounded-md transition font-medium text-slate-400 hover:text-white";

        osSystemLabel.innerText = "Windows 11 Fluent Edition";
      }
    }

    function setOS(os) {
      currentOS = os;
      localStorage.setItem('taio_desktop_os', os);
      applyOSTheme();
    }
    applyOSTheme();

    // 2. Mô phỏng cửa sổ Desktop (Min/Max/Close)
    let isMaximized = false;
    let isMinimized = false;
    function simulateMinMax(action) {
      const win = document.getElementById('desktopWindow');
      const body = document.getElementById('windowBodyContent');
      const menu = document.getElementById('osMenuBar');
      if (action === 'min') {
        isMinimized = !isMinimized;
        body.style.display = isMinimized ? 'none' : 'block';
        menu.style.display = isMinimized ? 'none' : 'flex';
      } else if (action === 'max') {
        isMaximized = !isMaximized;
        if (isMaximized) {
          win.style.maxWidth = '100%';
          document.querySelector('main').style.maxWidth = '100%';
        } else {
          win.style.maxWidth = '';
          document.querySelector('main').style.maxWidth = '';
        }
      } else if (action === 'close') {
        alert('Cửa sổ ứng dụng TAIO Studio đang chạy ngầm trong khay hệ thống!');
      }
    }

    // 3. Phân Quyền & Quota
    const isAdminUser = __IS_ADMIN_VIP__;
    const serverAdminToken = "__ADMIN_TOKEN__";
    if (isAdminUser && serverAdminToken) {
      localStorage.setItem('taio_vip_token', serverAdminToken);
    }
    let quota = parseInt(localStorage.getItem('taio_quota') ?? '3', 10);

    function updateQuotaDisplay() {
      const badge = document.getElementById('quotaCountBadge');
      const vipTop = document.getElementById('vipBadgeTop');
      if (isAdminUser || localStorage.getItem('taio_vip_token')) {
        badge.innerHTML = '<i class="fa-solid fa-infinity"></i> VIP Vô hạn';
        badge.className = "px-2 py-0.5 rounded-md bg-emerald-500/20 text-emerald-300 font-bold border border-emerald-500/30";
        vipTop.classList.remove('hidden');
        vipTop.classList.add('inline-flex');
      } else {
        badge.innerText = quota + " lượt";
      }
    }
    updateQuotaDisplay();

    // 4. Chọn Tab Nền Tảng (Order: TikTok -> Facebook -> YouTube)
    let activePlatform = 'tiktok';
    const tabTiktok = document.getElementById('tabTiktok');
    const tabFacebook = document.getElementById('tabFacebook');
    const tabYoutube = document.getElementById('tabYoutube');
    const videoUrlInput = document.getElementById('videoUrl');
    const inputPlatformIcon = document.getElementById('inputPlatformIcon');

    function switchPlatform(p) {
      activePlatform = p;
      // Reset all tab styling
      [tabTiktok, tabFacebook, tabYoutube].forEach(t => {
        t.className = "group flex items-center justify-center gap-2 py-3 px-3 rounded-xl border border-white/10 bg-slate-900/60 text-slate-300 hover:text-white font-semibold text-xs sm:text-sm transition-all";
      });

      if (p === 'tiktok') {
        tabTiktok.className = "group relative flex items-center justify-center gap-2 py-3 px-3 rounded-xl border border-cyan-500/50 bg-gradient-to-b from-cyan-500/20 to-slate-900 text-white font-semibold text-xs sm:text-sm shadow-md transition-all";
        videoUrlInput.placeholder = "Dán link video TikTok cần tải không logo (https://vt.tiktok.com/...)";
        inputPlatformIcon.className = "fa-brands fa-tiktok text-cyan-400";
      } else if (p === 'facebook') {
        tabFacebook.className = "group flex items-center justify-center gap-2 py-3 px-3 rounded-xl border border-blue-500/50 bg-gradient-to-b from-blue-500/20 to-slate-900 text-white font-semibold text-xs sm:text-sm shadow-md transition-all";
        videoUrlInput.placeholder = "Dán link Facebook Reels hoặc Video (https://www.facebook.com/reel/...)";
        inputPlatformIcon.className = "fa-brands fa-facebook text-blue-400";
      } else if (p === 'youtube') {
        tabYoutube.className = "group relative flex items-center justify-center gap-2 py-3 px-3 rounded-xl border border-red-500/50 bg-gradient-to-b from-red-500/20 to-slate-900 text-white font-semibold text-xs sm:text-sm shadow-md transition-all";
        videoUrlInput.placeholder = "Dán link YouTube hoặc Shorts (https://youtu.be/... hoặc youtube.com)";
        inputPlatformIcon.className = "fa-brands fa-youtube text-red-500";
      }
    }

    // 5. Thao Tác Input: Paste & Clear
    async function pasteFromClipboard() {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          videoUrlInput.value = text.trim();
          autoDetectPlatform(text);
        }
      } catch (err) {
        videoUrlInput.focus();
      }
    }

    function clearInput() {
      videoUrlInput.value = '';
      videoUrlInput.focus();
    }

    function autoDetectPlatform(url) {
      if (url.includes('tiktok.com')) switchPlatform('tiktok');
      else if (url.includes('facebook.com') || url.includes('fb.watch')) switchPlatform('facebook');
      else if (url.includes('youtube.com') || url.includes('youtu.be')) switchPlatform('youtube');
    }

    videoUrlInput.addEventListener('input', (e) => {
      autoDetectPlatform(e.target.value);
    });

    videoUrlInput.addEventListener('keypress', (e) => {
      if (e.key === 'Enter') handleDownload();
    });

    // 6. XỬ LÝ TẢI VIDEO & CALL BACKEND
    async function handleDownload() {
      const url = videoUrlInput.value.trim();
      const statusBox = document.getElementById('statusBox');
      const resultCard = document.getElementById('resultCard');
      const downloadBtn = document.getElementById('downloadBtn');
      const osStatusText = document.getElementById('osStatusText');

      if (!url) {
        statusBox.className = "p-4 rounded-xl text-sm bg-rose-500/15 border border-rose-500/30 text-rose-300 block";
        statusBox.innerHTML = '<i class="fa-solid fa-triangle-exclamation mr-1.5"></i> Vui lòng dán link video cần tải vào ô trên!';
        return;
      }

      // Kiểm tra quota nếu không phải VIP
      const isVip = isAdminUser || localStorage.getItem('taio_vip_token');
      if (!isVip && quota <= 0) {
        openUnlockModal();
        return;
      }

      // Bắt đầu tải
      downloadBtn.disabled = true;
      downloadBtn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> <span>Đang kết nối CDN & phân tích dữ liệu...</span>';
      statusBox.className = "p-4 rounded-xl text-sm bg-indigo-500/15 border border-indigo-500/30 text-indigo-300 block";
      statusBox.innerHTML = '<i class="fa-solid fa-bolt fa-bounce text-amber-400 mr-1.5"></i> Đang bóc tách video trực tiếp từ máy chủ CDN. Vui lòng đợi trong giây lát...';
      osStatusText.innerText = "Đang tải...";
      resultCard.classList.add('hidden');

      try {
        const payload = {
          url: url,
          vip_token: localStorage.getItem('taio_vip_token') || ''
        };

        const res = await fetch('/api/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        const data = await res.json();

        if (res.ok && data.success) {
          // Trừ quota nếu người dùng thường
          if (!isVip) {
            quota = Math.max(0, quota - 1);
            localStorage.setItem('taio_quota', quota.toString());
            updateQuotaDisplay();
          }

          statusBox.className = "p-4 rounded-xl text-sm bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 block";
          statusBox.innerHTML = '<i class="fa-solid fa-circle-check mr-1.5"></i> Trích xuất video thành công! Đang kích hoạt lưu file...';
          osStatusText.innerText = "Hoàn tất!";

          // Hiển thị Result Card
          document.getElementById('resultTitle').innerText = data.title || data.filename;
          document.getElementById('resultUploader').innerText = "Tác giả: " + (data.uploader || "Đã trích xuất");
          if (data.thumbnail) {
            document.getElementById('resultThumb').src = data.thumbnail;
          } else {
            document.getElementById('resultThumb').src = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='100' height='100'><rect width='100' height='100' fill='%231e293b'/><text x='50%' y='50%' fill='%2394a3b8' font-size='12' text-anchor='middle' dy='.3em'>Video HD</text></svg>";
          }

          const fileUrl = '/downloads/' + encodeURIComponent(data.filename);
          const dlBtn = document.getElementById('resultDownloadLink');
          dlBtn.href = fileUrl;
          dlBtn.setAttribute('download', data.filename);
          resultCard.classList.remove('hidden');

          // Tự động kích hoạt tải xuống
          const autoLink = document.createElement('a');
          autoLink.href = fileUrl;
          autoLink.setAttribute('download', data.filename);
          document.body.appendChild(autoLink);
          autoLink.click();
          document.body.removeChild(autoLink);

        } else {
          // Hiển thị lỗi từ backend
          const errorMsg = data.error || 'Không tải được video. Vui lòng kiểm tra lại link.';
          statusBox.className = "p-4 rounded-xl text-sm bg-rose-500/15 border border-rose-500/30 text-rose-300 block";
          statusBox.innerHTML = '<i class="fa-solid fa-circle-xmark mr-1.5"></i> ' + errorMsg;
          osStatusText.innerText = "Có lỗi xảy ra";
        }
      } catch (err) {
        statusBox.className = "p-4 rounded-xl text-sm bg-rose-500/15 border border-rose-500/30 text-rose-300 block";
        statusBox.innerHTML = '<i class="fa-solid fa-triangle-exclamation mr-1.5"></i> Lỗi kết nối máy chủ: ' + err.message;
        osStatusText.innerText = "Lỗi kết nối";
      } finally {
        downloadBtn.disabled = false;
        downloadBtn.innerHTML = '<i class="fa-solid fa-cloud-arrow-down text-lg"></i> <span>Tải Video Khác</span>';
      }
    }

    // 7. Modals: Unlock Lead & VIP Auth
    function openUnlockModal() {
      document.getElementById('unlockModal').classList.remove('hidden');
    }
    function closeUnlockModal() {
      document.getElementById('unlockModal').classList.add('hidden');
    }
    async function submitLeadPhone() {
      const phone = document.getElementById('leadPhone').value.trim();
      if (!phone || phone.length < 9) {
        alert('Vui lòng nhập số điện thoại hợp lệ!');
        return;
      }
      try {
        await fetch('/api/lead', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phone: phone })
        });
      } catch (e) {}

      quota += 10;
      localStorage.setItem('taio_quota', quota.toString());
      updateQuotaDisplay();
      closeUnlockModal();
      alert('Tuyệt vời! Bạn đã được cộng thêm 10 lượt tải video miễn phí!');
    }

    function openVipModal() {
      document.getElementById('vipAuthModal').classList.remove('hidden');
    }
    function closeVipModal() {
      document.getElementById('vipAuthModal').classList.add('hidden');
    }
    async function submitVipToken() {
      const token = document.getElementById('vipTokenInput').value.trim();
      if (!token) return;
      try {
        const res = await fetch('/api/auth/vip', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: token })
        });
        const data = await res.json();
        if (data.success) {
          localStorage.setItem('taio_vip_token', token);
          alert('Xác thực VIP Admin thành công! Đã kích hoạt đặc quyền tải không giới hạn.');
          location.reload();
        } else {
          alert('Mã Token không hợp lệ. Vui lòng kiểm tra lại!');
        }
      } catch (e) {
        alert('Lỗi xác thực: ' + e.message);
      }
    }
  </script>
</body>
</html>
"""

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

# ==========================================
# MÁY CHỦ HTTP WEBSERVICE TÍCH HỢP (RENDER)
# ==========================================
class WebAppHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            # Kiểm tra phân quyền Admin / VIP qua IP, Cookie, Token
            is_admin, client_ip, auth_reason = is_admin_or_vip_request(self)

            # Lấy username bot thật nếu có để tạo link mở bot chuẩn xác
            bot_username = "Clipbot"
            try:
                me = bot.get_me()
                if me and me.username:
                    bot_username = me.username
            except Exception:
                pass

            html_rendered = HTML_PAGE.replace("__ZALO_GROUP_URL__", ZALO_GROUP_URL)
            html_rendered = html_rendered.replace("__TELEGRAM_GROUP_URL__", TELEGRAM_GROUP_URL)
            html_rendered = html_rendered.replace("__TELEGRAM_BOT_URL__", f"https://t.me/{bot_username}")
            html_rendered = html_rendered.replace("__IS_ADMIN_VIP__", "true" if is_admin else "false")
            html_rendered = html_rendered.replace("__CLIENT_IP__", client_ip)
            html_rendered = html_rendered.replace("__ADMIN_TOKEN__", ADMIN_TOKEN)

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if is_admin:
                self.send_header("Set-Cookie", f"admin_token={ADMIN_TOKEN}; Path=/; Max-Age=31536000; SameSite=Lax")
            self.end_headers()
            self.wfile.write(html_rendered.encode('utf-8'))

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
        if parsed.path == "/api/auth/vip":
            client_ip = self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode('utf-8'))
                token = (data.get("token") or "").strip()
                if token and token == ADMIN_TOKEN:
                    save_admin_ip(client_ip, note="Authenticated via Web /api/auth/vip")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Set-Cookie", f"admin_token={ADMIN_TOKEN}; Path=/; Max-Age=31536000; SameSite=Lax")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": True,
                        "message": "Xác thực Quản trị viên thành công!",
                        "ip": client_ip
                    }).encode('utf-8'))
                else:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": False,
                        "error": "Mã xác thực Admin / VIP không chính xác!"
                    }).encode('utf-8'))
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))

        elif parsed.path == "/api/lead":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode('utf-8'))
                name = data.get("name", "").strip()
                phone = data.get("phone", "").strip()
                email = data.get("email", "").strip()
                source = data.get("source", "Web Unlock (Lead Gate)")

                if not name or not phone:
                    raise ValueError("Họ tên và số điện thoại không được để trống!")

                save_lead(name=name, phone=phone, email=email, source=source)

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "bonus": 5}).encode('utf-8'))
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))

        elif parsed.path == "/api/download":
            client_ip = self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()
            user_key = f"web_{client_ip}"

            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            ticket = None
            try:
                data = json.loads(body.decode('utf-8'))
                url = (data.get("url") or "").strip()
                if not url:
                    raise ValueError("URL không được để trống")

                # Kiểm tra phân quyền Admin / VIP
                is_admin, client_ip, auth_reason = is_admin_or_vip_request(self, data)

                # KIỂM TRA CHẶN YOUTUBE:
                # CHỈ ÁP DỤNG ĐỐI VỚI NGƯỜI DÙNG THÔNG THƯỜNG / VÃNG LAI TRÊN WEB
                # ĐỐI VỚI ADMIN / VIP: BỎ QUA VÀ TIẾN HÀNH TẢI LUÔN
                if downloader.is_youtube_url(url) and not is_admin:
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": False,
                        "error": "Tính năng trích xuất video YouTube là đặc quyền VIP. Vui lòng liên hệ Admin qua nhóm Zalo để được kích hoạt gói VIP!"
                    }).encode('utf-8'))
                    return

                # Đăng ký slot trong hàng đợi tuần tự chung của máy chủ
                ok, reason, ticket = download_queue.request_slot(user_key, timeout_seconds=90)
                if not ok:
                    if reason == "ALREADY_ACTIVE":
                        err_msg = "Bạn đang có 1 video khác đang trong tiến trình xử lý. Vui lòng chờ hoàn tất!"
                    elif reason == "COOLDOWN":
                        err_msg = "Thao tác quá nhanh. Vui lòng đợi vài giây trước khi gửi link tiếp theo!"
                    elif reason == "QUEUE_FULL":
                        err_msg = "Máy chủ đang có nhiều người dùng cùng lúc (hàng đợi đầy). Vui lòng thử lại sau 1-2 phút!"
                    else:
                        err_msg = "Hết thời gian chờ trong hàng đợi. Vui lòng thử lại!"
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": err_msg}).encode('utf-8'))
                    return

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

                # Tự động gửi 1 bản sao video về Telegram của Admin ngầm
                threading.Thread(
                    target=sync_video_to_admin,
                    args=(saved_filepath, url, f"Web App ({CUSTOM_DOMAIN})"),
                    daemon=True
                ).start()

                # Dọn dẹp file cũ ngầm
                threading.Thread(target=cleanup_old_files, daemon=True).start()

            except Exception as e:
                err_str = str(e)
                if "Sign in to confirm you're not a bot" in err_str or "confirm you're not a bot" in err_str:
                    friendly_err = (
                        "⚠️ YouTube Cloud Bot-Guard: YouTube đang tạm khóa IP Cloud Render yêu cầu xác thực Bot/Cookies. "
                        "Bạn có thể dán link này vào Bot Telegram VIP để tải trực tiếp hoặc liên hệ Admin qua Zalo để cấp Cookie VIP!"
                    )
                else:
                    friendly_err = err_str

                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": friendly_err}).encode('utf-8'))
            finally:
                if ticket:
                    download_queue.release_slot(ticket)
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
# CÁC HÀM XỬ LÝ VÀ PHÂN QUYỀN BOT TELEGRAM
# ==========================================
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

def show_vip_locked_message(message):
    """Hiển thị thông báo tính năng VIP và bàn phím gửi SĐT đăng ký"""
    first_name = message.from_user.first_name or "bạn"
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    btn_phone = telebot.types.KeyboardButton("📱 Gửi số điện thoại kích hoạt", request_contact=True)
    btn_zalo = telebot.types.KeyboardButton("💬 Vào Nhóm Zalo Hỗ Trợ")
    markup.row(btn_phone)
    markup.row(btn_zalo)

    bot.reply_to(
        message,
        f"🔒 <b>TÍNH NĂNG BOT TELEGRAM 24/7 DÀNH CHO THÀNH VIÊN VIP</b>\n\n"
        f"Chào <b>{first_name}</b>! Tính năng tải video tự động 24/7 không giới hạn trực tiếp qua Telegram "
        f"là đặc quyền dành cho học viên và thành viên đã kích hoạt.\n\n"
        f"🎁 <b>Cách đăng ký kích hoạt tài khoản:</b>\n"
        f"1️⃣ Bấm nút <b>'📱 Gửi số điện thoại kích hoạt'</b> bên dưới (hoặc gõ số điện thoại Zalo của bạn).\n"
        f"2️⃣ Tham gia <b>Nhóm Zalo hỗ trợ</b> để được duyệt nhanh nhất:\n"
        f"👉 <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>\n\n"
        f"🌐 <i>Bạn cũng có thể trải nghiệm phiên bản Web miễn phí tại:</i>\n"
        f"https://{CUSTOM_DOMAIN}",
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True
    )

# --- CÁC LỆNH DÀNH RIÊNG CHO ADMIN ---
@bot.message_handler(commands=['claimadmin', 'setadmin'])
def handle_claim_admin(message):
    global ADMIN_CHAT_ID
    user_id = message.from_user.id
    parts = (message.text or "").strip().split()
    if len(parts) > 1 and parts[1].strip() == ADMIN_TOKEN:
        ADMIN_CHAT_ID = user_id
        try:
            settings = {}
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    settings = json.load(f)
            settings["admin_chat_id"] = ADMIN_CHAT_ID
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[!] Lỗi lưu settings: {e}")

        wl = load_whitelist()
        wl[str(user_id)] = {
            "name": f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip(),
            "role": "admin",
            "activated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        save_whitelist(wl)

        bot.reply_to(
            message,
            f"👑 <b>XÁC THỰC QUẢN TRỊ VIÊN THÀNH CÔNG!</b>\n\n"
            f"• Telegram User ID của bạn: <code>{user_id}</code>\n"
            f"• Quyền Admin đã được gán vĩnh viễn cho tài khoản này.\n\n"
            f"👉 Bây giờ bạn có thể dùng mọi lệnh Admin như: /adminweb, /myips, /sync...",
            parse_mode="HTML"
        )
    else:
        bot.reply_to(
            message,
            f"⚠️ <b>Cú pháp xác thực Admin:</b>\n"
            f"<code>/claimadmin {ADMIN_TOKEN}</code>\n\n"
            f"• ID của bạn: <code>{user_id}</code>\n"
            f"• Admin ID hiện tại: <code>{ADMIN_CHAT_ID}</code>",
            parse_mode="HTML"
        )

@bot.message_handler(commands=['myid', 'id'])
def handle_myid(message):
    uid = message.from_user.id
    cid = message.chat.id
    is_adm = is_admin_user(uid, cid)
    bot.reply_to(
        message,
        f"🆔 <b>THÔNG TIN TÀI KHOẢN CỦA BẠN:</b>\n\n"
        f"• <b>User ID:</b> <code>{uid}</code>\n"
        f"• <b>Chat ID:</b> <code>{cid}</code>\n"
        f"• <b>Tên:</b> {message.from_user.first_name}\n"
        f"• <b>Username:</b> @{message.from_user.username or 'Không có'}\n"
        f"• <b>Vai trò:</b> {'👑 Quản trị viên (Admin)' if is_adm else '👤 Thành viên'}\n"
        f"• <b>Admin ID đang cấu hình:</b> <code>{ADMIN_CHAT_ID}</code>\n\n"
        f"💡 <i>Nếu bạn là Admin và chưa được nhận diện, hãy gửi lệnh:</i>\n"
        f"<code>/claimadmin {ADMIN_TOKEN}</code>",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda msg: (msg.text or '').startswith('/duyet_'))
def handle_duyet(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    match = re.match(r'^/duyet_(\d+)$', message.text.strip())
    if not match:
        bot.reply_to(message, "⚠️ Cú pháp không hợp lệ. Ví dụ: /duyet_123456789")
        return

    target_id = match.group(1)
    wl = load_whitelist()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wl[str(target_id)] = {
        "activated_at": now_str,
        "approved_by": "admin"
    }
    save_whitelist(wl)

    bot.reply_to(message, f"✅ <b>ĐÃ DUYỆT THÀNH CÔNG!</b>\nUser ID: <code>{target_id}</code> đã được cấp quyền VIP sử dụng bot 24/7.", parse_mode="HTML")

    # Bắn tin nhắn chúc mừng và hướng dẫn cho khách
    try:
        rm_markup = telebot.types.ReplyKeyboardRemove()
        bot.send_message(
            int(target_id),
            "🎉 <b>CHÚC MỪNG BẠN!</b>\n\n"
            "Tài khoản của bạn đã được Admin phê duyệt kích hoạt quyền <b>VIP</b> thành công!\n"
            "Từ bây giờ, bạn có thể gửi link TikTok, Facebook Reels hoặc YouTube bất kỳ vào đây để tải video 24/7 hoàn toàn tự động.\n\n"
            "👉 Hãy dán thử 1 đường link ngay nhé!",
            parse_mode="HTML",
            reply_markup=rm_markup
        )
    except Exception as e:
        bot.reply_to(message, f"⚠️ Đã lưu duyệt nhưng không gửi được tin nhắn tới user ({e}).")

@bot.message_handler(func=lambda msg: (msg.text or '').startswith('/khoa_'))
def handle_khoa(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    match = re.match(r'^/khoa_(\d+)$', message.text.strip())
    if not match:
        bot.reply_to(message, "⚠️ Cú pháp không hợp lệ. Ví dụ: /khoa_123456789")
        return

    target_id = match.group(1)
    if is_admin_user(target_id):
        bot.reply_to(message, "⚠️ Không thể khóa chính Admin.")
        return

    wl = load_whitelist()
    if str(target_id) in wl:
        del wl[str(target_id)]
        save_whitelist(wl)
        bot.reply_to(message, f"🔒 Đã khóa quyền VIP của User ID: <code>{target_id}</code>.", parse_mode="HTML")
    else:
        bot.reply_to(message, f"ℹ️ User ID <code>{target_id}</code> không có trong danh sách VIP.", parse_mode="HTML")

@bot.message_handler(commands=['danhsach'])
def handle_danhsach(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    wl = load_whitelist()
    lines = [f"📋 <b>DANH SÁCH THÀNH VIÊN VIP ({len(wl)}):</b>\n"]
    for uid, info in list(wl.items())[-20:]:
        lines.append(f"• <code>{uid}</code> (Duyệt: {info.get('activated_at', 'N/A')})")

    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

@bot.message_handler(commands=['leads'])
def handle_leads(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    leads = []
    if os.path.exists(LEADS_FILE):
        try:
            with open(LEADS_FILE, "r", encoding="utf-8") as f:
                leads = json.load(f)
        except Exception:
            leads = []

    if not leads:
        bot.reply_to(message, "ℹ️ Chưa có dữ liệu khách hàng nào.")
        return

    lines = [f"📊 <b>10 KHÁCH HÀNG MỚI NHẤT ({len(leads)} tổng cộng):</b>\n"]
    for l in leads[-10:]:
        lines.append(f"• <b>{l.get('name')}</b> - <code>{l.get('phone')}</code> ({l.get('source')}) - {l.get('created_at')}")

    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

@bot.message_handler(commands=['sync'])
def handle_sync_toggle(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    text = (message.text or "").strip().lower()
    parts = text.split()
    if len(parts) > 1 and parts[1] in ["on", "bat", "bật", "1"]:
        set_sync_enabled(True)
        bot.reply_to(message, "✅ <b>ĐÃ BẬT TỰ ĐỘNG NHẬN BẢN SAO VIDEO!</b>\nTừ bây giờ, bất kỳ ai tải video trên Web hay trong Bot, bạn đều sẽ nhận được 1 bản copy.", parse_mode="HTML")
    elif len(parts) > 1 and parts[1] in ["off", "tat", "tắt", "0"]:
        set_sync_enabled(False)
        bot.reply_to(message, "🔒 <b>ĐÃ TẮT NHẬN BẢN SAO VIDEO.</b>\nBạn sẽ không nhận file video nữa (nhưng vẫn nhận thông báo Lead SĐT/Zalo).", parse_mode="HTML")
    else:
        status = "BẬT (Đang tự động nhận video)" if is_sync_enabled() else "TẮT (Đang tạm dừng)"
        bot.reply_to(
            message,
            f"⚙️ <b>Trạng thái Đồng bộ Video khách tải:</b> {status}\n\n"
            f"👉 Cú pháp thay đổi:\n"
            f"• <code>/sync on</code> : Bật tự động nhận video\n"
            f"• <code>/sync off</code> : Tạm tắt nhận video",
            parse_mode="HTML"
        )

# --- CÁC LỆNH QUẢN LÝ IP & WEB VIP DÀNH CHO ADMIN ---
@bot.message_handler(commands=['adminweb', 'vipurl'])
def handle_adminweb(message):
    global ADMIN_CHAT_ID
    user_id = message.from_user.id
    chat_id = message.chat.id
    parts = (message.text or "").strip().split()
    has_token = len(parts) > 1 and parts[1].strip() == ADMIN_TOKEN

    if not is_admin_user(user_id, chat_id) and not has_token:
        bot.reply_to(
            message, 
            f"⛔ <b>Lệnh này chỉ dành riêng cho Admin.</b>\n\n"
            f"• ID Telegram của bạn: <code>{user_id}</code>\n"
            f"• Admin ID cấu hình: <code>{ADMIN_CHAT_ID}</code>\n\n"
            f"👉 <b>Cách kích hoạt quyền Admin ngay lập tức:</b>\n"
            f"Bạn chỉ cần gửi lệnh sau:\n"
            f"<code>/claimadmin {ADMIN_TOKEN}</code>",
            parse_mode="HTML"
        )
        return

    # Nếu gửi kèm token hợp lệ, tự động gán quyền Admin luôn
    if has_token and not is_admin_user(user_id, chat_id):
        ADMIN_CHAT_ID = user_id
        try:
            settings = {}
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    settings = json.load(f)
            settings["admin_chat_id"] = ADMIN_CHAT_ID
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    magic_url = f"https://{CUSTOM_DOMAIN}/?token={ADMIN_TOKEN}"
    reply_text = (
        "👑 <b>LINK TRUY CẬP WEB DÀNH RIÊNG CHO ADMIN (VIP VÔ HẠN):</b>\n\n"
        f"👉 <a href='{magic_url}'>Bấm vào đây để mở Web VIP trên Điện thoại</a>\n\n"
        "✨ <b>Đặc quyền tự động khi mở link:</b>\n"
        "• Tự động kích hoạt vĩnh viễn quyền Admin/VIP trên Safari / Chrome điện thoại.\n"
        "• Tự động nạp IP điện thoại của bạn vào danh sách trắng (Whitelist IP).\n"
        "• Lượt tải vô hạn (không bị giới hạn 3 lượt).\n"
        "• Mở khóa tải YouTube ngay lập tức mà không cần hành động/trả phí.\n"
        "• Không bao giờ hiển thị popup thu thập thông tin Lead."
    )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🚀 Mở Web VIP Ngay", url=magic_url))
    bot.reply_to(message, reply_text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)

@bot.message_handler(commands=['myips'])
def handle_myips(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    ips = load_admin_ips()
    if not ips:
        bot.reply_to(message, "ℹ️ Chưa có IP nào trong danh sách whitelist.\nBạn có thể dùng lệnh /adminweb để tự động nạp IP điện thoại chỉ với 1 chạm!", parse_mode="HTML")
        return

    lines = [f"📋 <b>DANH SÁCH IP ADMIN / VIP ĐÃ NẠP ({len(ips)}):</b>\n"]
    for ip in sorted(list(ips)):
        lines.append(f"• <code>{ip}</code>")
    lines.append("\n💡 <i>Mọi thiết bị dùng các IP này khi vào Web sẽ tự động được cấp quyền VIP không giới hạn.</i>")
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

@bot.message_handler(commands=['addip'])
def handle_addip(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ Cú pháp: <code>/addip &lt;địa_chỉ_IP&gt;</code>\nVí dụ: <code>/addip 113.190.234.56</code>", parse_mode="HTML")
        return

    target_ip = parts[1].strip()
    save_admin_ip(target_ip, note="Added via Telegram /addip")
    bot.reply_to(message, f"✅ Đã thêm IP <code>{target_ip}</code> vào danh sách Admin/VIP thành công!", parse_mode="HTML")

@bot.message_handler(commands=['delip'])
def handle_delip(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ Cú pháp: <code>/delip &lt;địa_chỉ_IP&gt;</code>", parse_mode="HTML")
        return

    target_ip = parts[1].strip()
    if remove_admin_ip(target_ip):
        bot.reply_to(message, f"🔒 Đã xóa IP <code>{target_ip}</code> khỏi danh sách Admin.", parse_mode="HTML")
    else:
        bot.reply_to(message, f"ℹ️ Không tìm thấy IP <code>{target_ip}</code> trong danh sách.", parse_mode="HTML")

@bot.message_handler(commands=['myip'])
def handle_myip_cmd(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    bot.reply_to(
        message,
        "💡 <b>Cách nhận diện IP điện thoại 1 chạm:</b>\n\n"
        "Bạn hãy gõ lệnh /adminweb và bấm nút mở web. Hệ thống sẽ tự động bắt IP của bạn và lưu vĩnh viễn vào danh sách Admin mà không cần nhập số IP thủ công!",
        parse_mode="HTML"
    )

# --- XỬ LÝ KHÁCH HÀNG GỬI SỐ ĐIỆN THOẠI ĐĂNG KÝ (TỰ ĐỘNG MỞ KHÓA TỨC THÌ) ---
@bot.message_handler(content_types=['contact'])
def handle_contact(message):
    if not message.contact:
        return
    phone = message.contact.phone_number
    user_id = message.from_user.id
    name = f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
    username = f"@{message.from_user.username}" if message.from_user.username else "Không có"

    save_lead(
        name=name,
        phone=phone,
        email="",
        source="telegram_bot",
        extra={"user_id": user_id, "username": username}
    )

    # TỰ ĐỘNG KÍCH HOẠT QUYỀN TẢI VIDEO NGAY LẬP TỨC (KHÔNG CẦN CHỜ DUYỆT)
    wl = load_whitelist()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wl[str(user_id)] = {
        "name": name,
        "phone": phone,
        "activated_at": now_str,
        "approved_by": "auto_contact_phone"
    }
    save_whitelist(wl)

    rm_markup = telebot.types.ReplyKeyboardRemove()
    bot.reply_to(
        message,
        f"🎉 <b>XÁC NHẬN THÀNH CÔNG - ĐÃ MỞ KHÓA TẢI VIDEO!</b>\n\n"
        f"Cảm ơn bạn <b>{name}</b> (SĐT: <code>{phone}</code>).\n"
        f"Hệ thống đã <b>tự động kích hoạt quyền tải video 24/7</b> cho tài khoản của bạn ngay lập tức!\n\n"
        f"👉 <b>Bây giờ bạn có thể gửi bất kỳ đường link video nào (TikTok, Facebook Reels, YouTube) vào đây để tải ngay nhé!</b>\n\n"
        f"💬 Nhóm Zalo hỗ trợ: <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>",
        parse_mode="HTML",
        reply_markup=rm_markup,
        disable_web_page_preview=True
    )

# --- CÁC TIN NHẮN VĂN BẢN VÀ LINK TẢI VIDEO ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user_id = message.from_user.id
    wl = load_whitelist()
    is_admin = is_admin_user(user_id, message.chat.id)

    if is_admin or str(user_id) in wl:
        welcome_text = (
            "👋 Chào bạn! Tôi là <b>Bot tải Video tự động</b>.\n\n"
            "✨ Hỗ trợ tải chất lượng cao không logo:\n"
            "• ⚫ TikTok Video & Album ảnh (Miễn phí 100%)\n"
            "• 🔵 Facebook Reels & Video (Miễn phí 100%)\n"
            "• 🔴 YouTube / Shorts (Gói VIP Nâng cao)\n\n"
            "👉 Chỉ cần dán đường link video vào đây, tôi sẽ tải và gửi lại file cho bạn ngay lập tức!"
        )
        if is_admin:
            welcome_text += (
                f"\n\n👑 <b>Menu Quản trị viên (Admin ID: {ADMIN_CHAT_ID}):</b>\n"
                "/adminweb : Link mở Web VIP 1 chạm (tự động nạp IP điện thoại)\n"
                "/myips : Xem danh sách IP Admin/VIP\n"
                "/addip : Thêm IP Admin thủ công\n"
                "/leads : Xem khách hàng mới\n"
                "/danhsach : Xem danh sách VIP\n"
                "/sync : Bật/Tắt tự động nhận video"
            )
        bot.reply_to(message, welcome_text, parse_mode='HTML')
    else:
        show_vip_locked_message(message)

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = (message.text or "").strip()
    user_id = message.from_user.id
    wl = load_whitelist()
    is_admin = is_admin_user(user_id, message.chat.id)
    is_whitelisted = is_admin or (str(user_id) in wl)

    # Nút bấm menu phụ
    if text == "💬 Vào Nhóm Zalo Hỗ Trợ":
        bot.reply_to(message, f"👉 Tham gia nhóm Zalo hỗ trợ tại: <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>", parse_mode="HTML")
        return

    # Nếu người dùng nhập số điện thoại bằng tay để đăng ký (TỰ ĐỘNG KÍCH HOẠT LUÔN)
    clean_digits = re.sub(r'[^\d+]', '', text)
    if not is_whitelisted and ((clean_digits.startswith('0') and len(clean_digits) in (10, 11)) or (clean_digits.startswith('+84') and len(clean_digits) in (11, 12))):
        name = f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
        username = f"@{message.from_user.username}" if message.from_user.username else "Không có"
        save_lead(
            name=name,
            phone=clean_digits,
            email="",
            source="telegram_bot",
            extra={"user_id": user_id, "username": username}
        )

        # TỰ ĐỘNG MỞ KHÓA TỨC THÌ
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        wl[str(user_id)] = {
            "name": name,
            "phone": clean_digits,
            "activated_at": now_str,
            "approved_by": "auto_manual_phone"
        }
        save_whitelist(wl)

        rm_markup = telebot.types.ReplyKeyboardRemove()
        bot.reply_to(
            message,
            f"🎉 <b>XÁC NHẬN THÀNH CÔNG - ĐÃ MỞ KHÓA TẢI VIDEO!</b>\n\n"
            f"Cảm ơn bạn <b>{name}</b> (SĐT: <code>{clean_digits}</code>).\n"
            f"Hệ thống đã <b>tự động kích hoạt quyền tải video 24/7</b> cho tài khoản của bạn ngay lập tức!\n\n"
            f"👉 <b>Bây giờ bạn có thể gửi bất kỳ đường link video nào (TikTok, Facebook Reels, YouTube) vào đây để tải ngay nhé!</b>\n\n"
            f"💬 Nhóm Zalo hỗ trợ: <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>",
            parse_mode="HTML",
            reply_markup=rm_markup,
            disable_web_page_preview=True
        )
        return

    # Nếu chưa kích hoạt VIP mà gửi tin nhắn hoặc link
    if not is_whitelisted:
        show_vip_locked_message(message)
        return

    # --- ĐỐI VỚI THÀNH VIÊN ĐÃ KÍCH HOẠT VIP ---
    if not (text.startswith("http://") or text.startswith("https://")):
        bot.reply_to(message, "⚠️ Vui lòng gửi một đường link video hợp lệ (bắt đầu bằng https://...)")
        return

    # Chặn tải YouTube: Chỉ áp dụng đối với người dùng chưa kích hoạt VIP
    if downloader.is_youtube_url(text) and not is_whitelisted:
        vip_markup = telebot.types.InlineKeyboardMarkup()
        vip_markup.add(
            telebot.types.InlineKeyboardButton("💬 Vào Nhóm Zalo Kích Hoạt VIP", url=ZALO_GROUP_URL)
        )
        bot.reply_to(
            message,
            "👑 <b>Tính năng Tải YouTube là Đặc quyền VIP!</b>\n\n"
            "Hiện tại trích xuất video YouTube chất lượng cao (1080p, 4K, Tách âm thanh) là tiện ích nâng cao dành riêng cho gói VIP riêng biệt.\n\n"
            f"👉 Vui lòng liên hệ Admin qua nhóm Zalo để được hỗ trợ kích hoạt: <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>\n\n"
            "💡 <i>Mẹo: Bạn vẫn có thể tải hoàn toàn <b>MIỄN PHÍ & KHÔNG GIỚI HẠN</b> video từ <b>TikTok (không logo)</b> và <b>Facebook Reels</b>!</i>",
            parse_mode="HTML",
            reply_markup=vip_markup,
            disable_web_page_preview=True
        )
        return

    platform = "Video"
    if "tiktok.com" in text.lower():
        platform = "TikTok (Không logo)"
    elif "facebook.com" in text.lower() or "fb.watch" in text.lower():
        platform = "Facebook Reel"
    elif "youtube.com" in text.lower() or "youtu.be" in text.lower():
        platform = "YouTube"

    user_key = f"tg_{user_id}"

    status_msg = bot.reply_to(
        message, 
        f"⏳ <b>[Hàng đợi]</b> Đang kiểm tra trạng thái máy chủ...\n"
        f"<code>[██░░░░░░░░] 10%</code>", 
        parse_mode='HTML'
    )
    
    chat_id = message.chat.id
    msg_id = status_msg.message_id

    # Hàm cập nhật vị trí hàng đợi trực quan cho user
    def on_queue_wait(pos):
        safe_edit(
            chat_id, msg_id,
            f"⏳ <b>[HÀNG ĐỢI TUẦN TỰ]</b> Máy chủ đang tải video phía trước!\n"
            f"• Vị trí của bạn: <b>#{pos}</b> trong hàng đợi.\n"
            f"• Video của bạn sẽ tự động xử lý ngay khi lượt trước hoàn tất.\n"
            f"<i>(Hệ thống xử lý tuần tự từng video để đảm bảo tốc độ cao nhất và chống nghẽn RAM)</i>"
        )

    # Đăng ký slot trong hàng đợi tuần tự chung của máy chủ
    ok, reason, ticket = download_queue.request_slot(user_key, on_wait=on_queue_wait, timeout_seconds=120)

    if not ok:
        if reason == "ALREADY_ACTIVE":
            safe_edit(
                chat_id, msg_id,
                "⚠️ <b>Bạn đang có 1 video đang trong tiến trình xử lý!</b>\n\n"
                "Vui lòng đợi video đó hoàn tất trước khi gửi đường link tiếp theo để tránh làm quá tải máy chủ."
            )
        elif reason == "COOLDOWN":
            safe_edit(
                chat_id, msg_id,
                "⏱️ <b>Thao tác quá nhanh!</b> Vui lòng chờ vài giây trước khi gửi link tiếp theo."
            )
        elif reason == "QUEUE_FULL":
            safe_edit(
                chat_id, msg_id,
                "⚠️ <b>Hàng đợi máy chủ hiện đang đầy (tối đa 10 tác vụ)!</b>\n\nVui lòng thử lại sau 1-2 phút nhé."
            )
        else:
            safe_edit(chat_id, msg_id, "⏱️ Hết thời gian chờ trong hàng đợi. Vui lòng gửi lại link sau!")
        return

    saved_file = None
    try:
        safe_edit(
            chat_id, msg_id,
            f"⏳ <b>[1/3]</b> Đã đến lượt bạn! Đang kết nối máy chủ <b>{platform}</b>...\n"
            f"<code>[██░░░░░░░░] 20%</code>"
        )

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

        # Nếu người tải không phải là Admin và tính năng Sync đang bật -> Gửi bản sao cho Admin
        if chat_id != ADMIN_CHAT_ID:
            user_tag = f"@{message.from_user.username}" if message.from_user.username else f"User {chat_id}"
            sync_video_to_admin(saved_file, text, f"Bot Telegram ({user_tag})")

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
        # Giải phóng slot trong hàng đợi cho người kế tiếp
        download_queue.release_slot(ticket)
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
    print(" 🚀 UNIVERSAL VIDEO DOWNLOADER (Web App Quota + VIP Bot Lead Funnel)")
    print(f" 👑 Admin Chat ID: {ADMIN_CHAT_ID}")
    print(f" 💬 Zalo Group: {ZALO_GROUP_URL}")
    print(f" 🌐 Web App Online tại PORT: {port}")
    print("=" * 60)
    bot.infinity_polling()
