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

# Thông tin cấu hình nghiệp vụ của Chủ sở hữu (Admin)
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "1509053609"))
ZALO_GROUP_URL = os.getenv("ZALO_GROUP_URL", "https://zalo.me/g/hhmdni386")
TELEGRAM_GROUP_URL = os.getenv("TELEGRAM_GROUP_URL", "https://t.me/your_community_channel")
CUSTOM_DOMAIN = os.getenv("CUSTOM_DOMAIN", "shotdownload.taio.club")

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
  <title>Universal Video Downloader</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    body { background: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    .glass { background: rgba(30, 41, 59, 0.75); backdrop-filter: blur(14px); border: 1px solid rgba(255, 255, 255, 0.1); }
    .glass-modal { background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(20px); border: 1px solid rgba(99, 102, 241, 0.3); }
  </style>
</head>
<body class="min-h-screen flex flex-col items-center justify-between p-4 sm:p-6">
  <div class="w-full max-w-xl mx-auto my-auto space-y-5">
    
    <!-- Top Bar: Community Link -->
    <div class="flex items-center justify-between text-xs text-slate-400 px-1">
      <span class="inline-flex items-center gap-1.5">
        <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
        Hệ thống hoạt động ổn định
      </span>
      <a href="__ZALO_GROUP_URL__" target="_blank" class="inline-flex items-center gap-1 text-sky-400 hover:text-sky-300 transition font-medium">
        <i class="fa-solid fa-users"></i> Tham gia Nhóm Zalo
      </a>
    </div>

    <!-- Header -->
    <div class="text-center space-y-2">
      <div class="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-tr from-rose-500 to-indigo-600 shadow-lg shadow-indigo-500/30">
        <i class="fa-solid fa-cloud-arrow-down text-2xl text-white"></i>
      </div>
      <h1 class="text-2xl sm:text-3xl font-extrabold tracking-tight">Tải Video Đa Năng</h1>
      <p class="text-sm text-slate-400">Tải TikTok không logo, Facebook Reels & YouTube VIP</p>
      
      <!-- Supported Badges -->
      <div class="flex items-center justify-center gap-2 pt-1">
        <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
          <i class="fa-brands fa-youtube"></i> YouTube <span class="text-[9px] bg-amber-500/20 px-1 py-0.5 rounded font-bold">VIP</span>
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
    <div class="glass rounded-3xl p-5 sm:p-7 shadow-2xl space-y-4">
      
      <!-- Quota Status Banner -->
      <div id="quotaBanner" class="flex items-center justify-between bg-slate-900/80 border border-slate-700/60 px-4 py-2.5 rounded-2xl text-xs">
        <div class="flex items-center gap-2">
          <i class="fa-solid fa-gift text-indigo-400"></i>
          <span class="text-slate-300">Lượt tải miễn phí:</span>
          <span id="quotaCount" class="font-bold text-amber-400 bg-amber-400/10 px-2 py-0.5 rounded-lg border border-amber-400/20">3 lượt</span>
        </div>
        <button onclick="openUnlockModal()" class="text-indigo-400 hover:text-indigo-300 font-medium underline text-[11px]">
          Nhận thêm lượt <i class="fa-solid fa-arrow-up-right-from-square ml-0.5"></i>
        </button>
      </div>

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
        <a id="saveFileBtn" href="#" class="w-full block text-center bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-3 rounded-xl text-sm transition font-semibold shadow-lg shadow-emerald-600/20">
          <i class="fa-solid fa-cloud-arrow-down mr-1"></i> Bấm để lưu video về máy
        </a>
      </div>
    </div>

    <!-- Telegram Bot VIP Card -->
    <div class="glass rounded-2xl p-4 text-xs text-slate-400 flex items-center justify-between border border-sky-500/20">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-sky-500/20 text-sky-400 flex items-center justify-center text-lg flex-shrink-0">
          <i class="fa-brands fa-telegram"></i>
        </div>
        <div>
          <div class="text-white font-semibold flex items-center gap-1.5">
            Bot Telegram 24/7
            <span class="bg-amber-400/20 text-amber-300 text-[10px] px-1.5 py-0.5 rounded font-bold border border-amber-400/30">VIP</span>
          </div>
          <div class="text-slate-400 text-[11px] mt-0.5">Tải video ngay trong chat • Cần đăng ký kích hoạt</div>
        </div>
      </div>
      <a href="__TELEGRAM_BOT_URL__" target="_blank" class="px-3 py-2 rounded-xl bg-gradient-to-r from-sky-500 to-blue-600 hover:from-sky-400 hover:to-blue-500 text-white font-semibold transition text-xs shadow-md shadow-sky-500/20 flex-shrink-0">
        Mở Bot VIP
      </a>
    </div>
  </div>

  <!-- MODAL MỞ KHÓA NHIỆM VỤ (LEAD GATE & VIRAL LOOP) -->
  <div id="unlockModal" class="hidden fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-md">
    <div class="glass-modal w-full max-w-md rounded-3xl p-6 sm:p-7 shadow-2xl space-y-5 relative max-h-[90vh] overflow-y-auto">
      
      <!-- Close Button -->
      <button onclick="closeUnlockModal()" class="absolute top-5 right-5 text-slate-400 hover:text-white transition">
        <i class="fa-solid fa-xmark text-lg"></i>
      </button>

      <!-- Modal Header -->
      <div class="text-center space-y-1.5 pt-1">
        <div class="inline-flex items-center justify-center w-12 h-12 rounded-2xl bg-gradient-to-tr from-amber-500 to-indigo-600 text-white text-xl shadow-lg shadow-indigo-500/30">
          <i class="fa-solid fa-gift"></i>
        </div>
        <h3 class="text-xl font-bold text-white">Mở Khóa Lượt Tải & Quà VIP</h3>
        <p class="text-xs text-slate-400">Bạn đã dùng hết 3 lượt tải miễn phí. Hoàn tất 3 bước dưới đây để nhận thêm <b>+5 lượt tải</b> và quyền kích hoạt Bot Telegram 24/7!</p>
      </div>

      <!-- Error box inside modal -->
      <div id="modalError" class="hidden rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-300 p-3 text-xs"></div>

      <!-- Step 1: Lead Information -->
      <div class="space-y-2.5">
        <label class="text-xs font-semibold text-slate-300 uppercase tracking-wider flex items-center gap-1.5">
          <span class="w-4 h-4 rounded-full bg-indigo-500 text-white text-[10px] flex items-center justify-center font-bold">1</span>
          Thông tin liên hệ nhận quyền VIP
        </label>
        <div class="space-y-2">
          <input id="leadName" type="text" placeholder="Họ và tên của bạn *" 
                 class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3.5 py-2.5 text-xs text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500">
          <input id="leadPhone" type="tel" placeholder="Số điện thoại / Zalo *" 
                 class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3.5 py-2.5 text-xs text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500">
          <input id="leadEmail" type="email" placeholder="Email nhận ưu đãi (không bắt buộc)" 
                 class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3.5 py-2.5 text-xs text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500">
        </div>
      </div>

      <!-- Step 2: Join Community -->
      <div class="space-y-2">
        <label class="text-xs font-semibold text-slate-300 uppercase tracking-wider flex items-center gap-1.5">
          <span class="w-4 h-4 rounded-full bg-indigo-500 text-white text-[10px] flex items-center justify-center font-bold">2</span>
          Tham gia cộng đồng hỗ trợ
        </label>
        <div class="grid grid-cols-2 gap-2">
          <a id="btnZaloJoin" href="__ZALO_GROUP_URL__" target="_blank" onclick="markStep('zalo')"
             class="flex items-center justify-center gap-2 py-2.5 px-3 rounded-xl bg-blue-600/20 hover:bg-blue-600/30 border border-blue-500/30 text-blue-300 text-xs font-medium transition text-center">
            <i class="fa-solid fa-comments"></i> 
            <span id="zaloLabel">Nhóm Zalo</span>
          </a>
          <a id="btnTeleJoin" href="__TELEGRAM_GROUP_URL__" target="_blank" onclick="markStep('tele')"
             class="flex items-center justify-center gap-2 py-2.5 px-3 rounded-xl bg-sky-600/20 hover:bg-sky-600/30 border border-sky-500/30 text-sky-300 text-xs font-medium transition text-center">
            <i class="fa-brands fa-telegram"></i> 
            <span id="teleLabel">Kênh Tele VIP</span>
          </a>
        </div>
      </div>

      <!-- Step 3: Viral Share -->
      <div class="space-y-2">
        <label class="text-xs font-semibold text-slate-300 uppercase tracking-wider flex items-center gap-1.5">
          <span class="w-4 h-4 rounded-full bg-indigo-500 text-white text-[10px] flex items-center justify-center font-bold">3</span>
          Chia sẻ lan tỏa công cụ
        </label>
        <div class="grid grid-cols-2 gap-2">
          <button onclick="shareFacebook()" 
                  class="flex items-center justify-center gap-1.5 py-2 px-3 rounded-xl bg-indigo-600/20 hover:bg-indigo-600/30 border border-indigo-500/30 text-indigo-300 text-xs font-medium transition">
            <i class="fa-brands fa-facebook"></i> Share Facebook
          </button>
          <button onclick="copyShareLink()" 
                  class="flex items-center justify-center gap-1.5 py-2 px-3 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 text-xs font-medium transition">
            <i class="fa-solid fa-link"></i> <span id="copyLabel">Copy link giới thiệu</span>
          </button>
        </div>
      </div>

      <!-- Submit Button -->
      <button id="modalSubmitBtn" onclick="submitUnlockForm()" class="w-full bg-gradient-to-r from-emerald-500 to-indigo-600 hover:from-emerald-600 hover:to-indigo-700 text-white font-semibold py-3 px-4 rounded-xl shadow-lg shadow-emerald-500/20 transition flex items-center justify-center gap-2 text-sm">
        <span>✨ Hoàn tất & Mở khóa (+5 lượt tải)</span>
      </button>

      <p class="text-[10px] text-center text-slate-500">Thông tin của bạn được bảo mật và dùng để hỗ trợ kích hoạt tài khoản.</p>
    </div>
  </div>

  <footer class="text-center text-xs text-slate-600 py-3">
    Universal Video Downloader &bull; Tương thích 100% iOS & Android &bull; Hotline / Zalo: Admin
  </footer>

  <script>
    // Quota Engine
    let quota = parseInt(localStorage.getItem('dl_free_quota') || '3');
    
    function updateQuotaUI() {
      const badge = document.getElementById('quotaCount');
      if (quota <= 0) {
        badge.innerText = '0 lượt (Hết)';
        badge.className = 'font-bold text-rose-400 bg-rose-400/10 px-2 py-0.5 rounded-lg border border-rose-400/20';
      } else {
        badge.innerText = quota + ' lượt';
        badge.className = 'font-bold text-amber-400 bg-amber-400/10 px-2 py-0.5 rounded-lg border border-amber-400/20';
      }
    }
    updateQuotaUI();

    function openUnlockModal() {
      document.getElementById('unlockModal').classList.remove('hidden');
    }

    function closeUnlockModal() {
      document.getElementById('unlockModal').classList.add('hidden');
    }

    function markStep(type) {
      if (type === 'zalo') {
        document.getElementById('zaloLabel').innerHTML = '✓ Đã tham gia Zalo';
      } else if (type === 'tele') {
        document.getElementById('teleLabel').innerHTML = '✓ Đã tham gia Tele';
      }
    }

    function shareFacebook() {
      const url = encodeURIComponent(window.location.origin);
      window.open('https://www.facebook.com/sharer/sharer.php?u=' + url, '_blank', 'width=600,height=400');
    }

    function copyShareLink() {
      const shareText = "Tải video Facebook Reels, TikTok không logo cực nhanh và nét tại: " + window.location.origin;
      navigator.clipboard.writeText(shareText).then(() => {
        document.getElementById('copyLabel').innerText = '✓ Đã chép link!';
        setTimeout(() => {
          document.getElementById('copyLabel').innerText = 'Copy link giới thiệu';
        }, 3000);
      });
    }

    async function submitUnlockForm() {
      const name = document.getElementById('leadName').value.trim();
      const phone = document.getElementById('leadPhone').value.trim();
      const email = document.getElementById('leadEmail').value.trim();
      const modalError = document.getElementById('modalError');

      if (!name) {
        modalError.innerText = 'Vui lòng nhập họ và tên của bạn!';
        modalError.classList.remove('hidden');
        return;
      }

      const cleanPhone = phone.replace(/\\D/g, '');
      if (cleanPhone.length < 9) {
        modalError.innerText = 'Vui lòng nhập số điện thoại / Zalo hợp lệ (tối thiểu 9 số)!';
        modalError.classList.remove('hidden');
        return;
      }

      modalError.classList.add('hidden');
      const submitBtn = document.getElementById('modalSubmitBtn');
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang kích hoạt lượt tải...';

      try {
        const response = await fetch('/api/lead', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            name: name,
            phone: phone,
            email: email,
            source: 'Web Unlock (Lead Gate)'
          })
        });

        const data = await response.json();
        if (response.ok && data.success) {
          quota += (data.bonus || 5);
          localStorage.setItem('dl_free_quota', quota.toString());
          localStorage.setItem('lead_registered', 'true');
          updateQuotaUI();
          closeUnlockModal();

          const statusBox = document.getElementById('statusBox');
          statusBox.className = "block bg-emerald-500/10 border border-emerald-500/20 text-emerald-300 p-4 rounded-2xl text-sm";
          statusBox.innerHTML = '<i class="fa-solid fa-gift mr-1"></i> <b>Chúc mừng!</b> Bạn đã được mở khóa thêm ' + (data.bonus || 5) + ' lượt tải và quyền ưu tiên duyệt Bot VIP!';
          
          const currentUrl = document.getElementById('videoUrl').value.trim();
          if (currentUrl) {
            handleDownload();
          }
        } else {
          modalError.innerText = data.error || 'Có lỗi xảy ra, vui lòng thử lại!';
          modalError.classList.remove('hidden');
        }
      } catch (err) {
        modalError.innerText = 'Lỗi kết nối máy chủ: ' + err.message;
        modalError.classList.remove('hidden');
      } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<span>✨ Hoàn tất & Mở khóa (+5 lượt tải)</span>';
      }
    }

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

      // Kiểm tra nếu là link YouTube -> Thông báo đặc quyền VIP / Trả phí
      const lowerUrl = url.toLowerCase();
      if (lowerUrl.includes('youtube.com') || lowerUrl.includes('youtu.be')) {
        statusBox.className = "block bg-amber-500/15 border border-amber-500/30 text-amber-200 p-5 rounded-2xl text-xs space-y-3";
        statusBox.innerHTML = `
          <div class="font-bold text-sm text-amber-300 flex items-center gap-2">
            <i class="fa-solid fa-crown text-amber-400"></i> Tính năng Tải YouTube là Đặc quyền VIP
          </div>
          <p class="text-slate-300 leading-relaxed text-xs">
            Hiện tại trích xuất video YouTube chất lượng cao (1080p, 4K, Tách âm thanh) là tiện ích nâng cao dành riêng cho thành viên đã kích hoạt gói VIP.
          </p>
          <div class="pt-1 flex flex-wrap items-center gap-2">
            <a href="__ZALO_GROUP_URL__" target="_blank" class="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl bg-blue-600 hover:bg-blue-500 text-white font-semibold transition text-xs shadow-md shadow-blue-600/20">
              <i class="fa-solid fa-comments"></i> Liên hệ Zalo kích hoạt
            </a>
            <button onclick="openUnlockModal()" class="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 font-medium transition text-xs">
              <i class="fa-solid fa-gift"></i> Đăng ký nhận ưu đãi
            </button>
          </div>
          <div class="text-[11px] text-slate-400 border-t border-amber-500/20 pt-2 flex items-center gap-1">
            <i class="fa-solid fa-circle-info text-sky-400"></i>
            <span>Mẹo: Bạn vẫn có thể tải <b>miễn phí 100%</b> video từ <b>TikTok (không logo)</b> và <b>Facebook Reels</b>!</span>
          </div>
        `;
        return;
      }

      // Kiểm tra Quota
      if (quota <= 0) {
        openUnlockModal();
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
          // Trừ 1 lượt tải
          quota = Math.max(0, quota - 1);
          localStorage.setItem('dl_free_quota', quota.toString());
          updateQuotaUI();

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

          // Nếu hết lượt sau lần tải này, hiển thị nhắc mở khóa sau 2.5 giây
          if (quota === 0) {
            setTimeout(() => {
              openUnlockModal();
            }, 2500);
          }
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

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
        if parsed.path == "/api/lead":
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

                # Kiểm tra chặn YouTube trên Web App (Tính năng VIP / Trả phí)
                if downloader.is_youtube_url(url):
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
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))
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
@bot.message_handler(commands=['myid'])
def handle_myid(message):
    bot.reply_to(
        message,
        f"🆔 <b>Chat ID của bạn là:</b> <code>{message.chat.id}</code>\n"
        f"👤 <b>Tên:</b> {message.from_user.first_name}\n"
        f"🌐 <b>Username:</b> @{message.from_user.username or 'Không có'}",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda msg: (msg.text or '').startswith('/duyet_'))
def handle_duyet(message):
    if message.chat.id != ADMIN_CHAT_ID:
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
    if message.chat.id != ADMIN_CHAT_ID:
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    match = re.match(r'^/khoa_(\d+)$', message.text.strip())
    if not match:
        bot.reply_to(message, "⚠️ Cú pháp không hợp lệ. Ví dụ: /khoa_123456789")
        return

    target_id = match.group(1)
    if int(target_id) == ADMIN_CHAT_ID:
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
    if message.chat.id != ADMIN_CHAT_ID:
        bot.reply_to(message, "⛔ Lệnh này chỉ dành riêng cho Admin.")
        return

    wl = load_whitelist()
    lines = [f"📋 <b>DANH SÁCH THÀNH VIÊN VIP ({len(wl)}):</b>\n"]
    for uid, info in list(wl.items())[-20:]:
        lines.append(f"• <code>{uid}</code> (Duyệt: {info.get('activated_at', 'N/A')})")

    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

@bot.message_handler(commands=['leads'])
def handle_leads(message):
    if message.chat.id != ADMIN_CHAT_ID:
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
    if message.chat.id != ADMIN_CHAT_ID:
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

# --- XỬ LÝ KHÁCH HÀNG GỬI SỐ ĐIỆN THOẠI ĐĂNG KÝ ---
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

    rm_markup = telebot.types.ReplyKeyboardRemove()
    bot.reply_to(
        message,
        f"✅ <b>ĐÃ TIẾP NHẬN YÊU CẦU KÍCH HOẠT!</b>\n\n"
        f"• Họ tên: <b>{name}</b>\n"
        f"• Số điện thoại: <code>{phone}</code>\n"
        f"• Telegram ID: <code>{user_id}</code>\n\n"
        f"Admin đã nhận được thông tin và sẽ phê duyệt kích hoạt cho bạn trong giây lát.\n"
        f"👉 Bạn hãy tham gia nhóm Zalo để được duyệt ngay: <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>",
        parse_mode="HTML",
        reply_markup=rm_markup,
        disable_web_page_preview=True
    )

# --- CÁC TIN NHẮN VĂN BẢN VÀ LINK TẢI VIDEO ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user_id = message.from_user.id
    wl = load_whitelist()

    if user_id == ADMIN_CHAT_ID or str(user_id) in wl:
        welcome_text = (
            "👋 Chào bạn! Tôi là <b>Bot tải Video tự động</b>.\n\n"
            "✨ Hỗ trợ tải chất lượng cao không logo:\n"
            "• ⚫ TikTok Video & Album ảnh (Miễn phí 100%)\n"
            "• 🔵 Facebook Reels & Video (Miễn phí 100%)\n"
            "• 🔴 YouTube / Shorts (Gói VIP Nâng cao)\n\n"
            "👉 Chỉ cần dán đường link video vào đây, tôi sẽ tải và gửi lại file cho bạn ngay lập tức!"
        )
        if user_id == ADMIN_CHAT_ID:
            welcome_text += "\n\n👑 <b>Menu Quản trị viên:</b>\n/leads : Xem khách hàng mới\n/danhsach : Xem danh sách VIP\n/sync : Bật/Tắt tự động nhận video khách tải"
        bot.reply_to(message, welcome_text, parse_mode='HTML')
    else:
        show_vip_locked_message(message)

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = (message.text or "").strip()
    user_id = message.from_user.id
    wl = load_whitelist()
    is_whitelisted = (user_id == ADMIN_CHAT_ID) or (str(user_id) in wl)

    # Nút bấm menu phụ
    if text == "💬 Vào Nhóm Zalo Hỗ Trợ":
        bot.reply_to(message, f"👉 Tham gia nhóm Zalo hỗ trợ tại: <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>", parse_mode="HTML")
        return

    # Nếu người dùng nhập số điện thoại bằng tay để đăng ký
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
        rm_markup = telebot.types.ReplyKeyboardRemove()
        bot.reply_to(
            message,
            f"✅ <b>ĐÃ TIẾP NHẬN SỐ ĐIỆN THOẠI {clean_digits}!</b>\n\n"
            f"Yêu cầu kích hoạt của bạn đã được gửi tới Admin. Vui lòng chờ phê duyệt trong giây lát.\n"
            f"👉 Nhóm Zalo hỗ trợ: <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>",
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

    # Chặn tải YouTube đối với thành viên thông thường -> Hướng dẫn kích hoạt gói VIP
    if downloader.is_youtube_url(text) and user_id != ADMIN_CHAT_ID:
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
