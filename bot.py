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
PROFILES_FILE = os.path.join(DATA_DIR, "user_profiles.json")

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

# ==========================================
# QUẢN LÝ HỒ SƠ ĐỊNH DANH NGƯỜI DÙNG & BÁO CÁO MARKDOWN
# ==========================================
def load_user_profiles():
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error loading user profiles: {e}")
    return {"registered": {}, "unregistered": {}}

def save_user_profiles(profiles):
    try:
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving user profiles: {e}")

def record_user_activity(client_ip=None, phone=None, name=None, telegram_id=None, user_agent="", download_success=False, is_vip=False):
    """
    Ghi nhận và cập nhật hồ sơ định danh:
    - Người dùng đã đăng ký: Khóa chính là SĐT, lưu kèm mảng danh sách IP đã sử dụng (khi đổi mạng wifi/4G).
    - Người dùng chưa đăng ký: Khóa chính là IP, theo dõi số lượt đã sử dụng và thiết bị.
    """
    profiles = load_user_profiles()
    reg = profiles.setdefault("registered", {})
    unreg = profiles.setdefault("unregistered", {})
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    clean_phone = re.sub(r'[^\d+]', '', str(phone or "")).strip()
    if clean_phone:
        user = reg.get(clean_phone)
        if not user:
            user = {
                "phone": clean_phone,
                "name": name or "Thành viên TAIO",
                "telegram_id": str(telegram_id or ""),
                "ips": [client_ip] if client_ip else [],
                "download_count": 0,
                "first_seen": now_str,
                "last_seen": now_str,
                "is_vip": bool(is_vip)
            }
        else:
            if name and (not user.get("name") or user.get("name") == "Thành viên TAIO"):
                user["name"] = name
            if telegram_id:
                user["telegram_id"] = str(telegram_id)
            if client_ip and client_ip not in user.get("ips", []):
                user.setdefault("ips", []).append(client_ip)
            if is_vip:
                user["is_vip"] = True
            user["last_seen"] = now_str

        if download_success:
            user["download_count"] = user.get("download_count", 0) + 1

        reg[clean_phone] = user

        # Gộp lượt tải từ unregistered nếu có
        if client_ip and client_ip in unreg:
            unreg_count = unreg[client_ip].get("download_count", 0)
            if unreg_count > 0 and user["download_count"] <= 1:
                user["download_count"] += unreg_count
            del unreg[client_ip]

    else:
        # Nếu chưa có phone, thử kiểm tra IP này đã gắn với SĐT nào chưa
        matched_phone = None
        if client_ip:
            for p, u in reg.items():
                if client_ip in u.get("ips", []):
                    matched_phone = p
                    break

        if matched_phone:
            user = reg[matched_phone]
            user["last_seen"] = now_str
            if download_success:
                user["download_count"] = user.get("download_count", 0) + 1
        else:
            if client_ip:
                u_entry = unreg.get(client_ip)
                if not u_entry:
                    u_entry = {
                        "ip": client_ip,
                        "user_agent": user_agent[:120] if user_agent else "Trình duyệt Web",
                        "download_count": 0,
                        "first_seen": now_str,
                        "last_seen": now_str
                    }
                else:
                    u_entry["last_seen"] = now_str
                    if user_agent and not u_entry.get("user_agent"):
                        u_entry["user_agent"] = user_agent[:120]

                if download_success:
                    u_entry["download_count"] = u_entry.get("download_count", 0) + 1

                unreg[client_ip] = u_entry

    save_user_profiles(profiles)

def generate_markdown_report():
    """Tạo báo cáo định danh người dùng chuẩn Markdown .md"""
    profiles = load_user_profiles()
    reg = profiles.get("registered", {})
    unreg = profiles.get("unregistered", {})
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total_reg = len(reg)
    total_unreg = len(unreg)
    
    all_unique_ips = set()
    total_reg_downloads = 0
    for u in reg.values():
        all_unique_ips.update(u.get("ips", []))
        total_reg_downloads += u.get("download_count", 0)

    total_unreg_downloads = 0
    for u in unreg.values():
        all_unique_ips.add(u.get("ip", ""))
        total_unreg_downloads += u.get("download_count", 0)
    all_unique_ips.discard("")

    total_downloads = total_reg_downloads + total_unreg_downloads

    lines = [
        "# 📊 Báo Cáo Định Danh Người Dùng TAIO Studio",
        f"*Thời gian xuất báo cáo: {now_str} (GMT+7)*",
        "",
        "---",
        "",
        "## 📈 Thống Kê Tổng Quan",
        f"- **👤 Người dùng đã đăng ký (Định danh theo SĐT):** `{total_reg}`",
        f"- **👥 Người dùng vãng lai (Chưa đăng ký - Quản lý theo IP):** `{total_unreg}`",
        f"- **🌐 Tổng số địa chỉ IP độc nhất đã ghi nhận:** `{len(all_unique_ips)}`",
        f"- **⚡ Tổng số lượt tải video thành công:** `{total_downloads}`",
        f"  - *Lượt tải từ thành viên đã đăng ký:* `{total_reg_downloads}`",
        f"  - *Lượt tải từ người dùng vãng lai:* `{total_unreg_downloads}`",
        "",
        "---",
        "",
        "## 👑 Người Dùng Đã Đăng Ký (Khoá chính: Số Điện Thoại)",
        "> *Hồ sơ này gom toàn bộ các địa chỉ IP của người dùng khi họ đổi mạng WiFi / 4G gắn với số điện thoại định danh.*",
        "",
        "| STT | Số Điện Thoại | Họ Tên | Telegram ID | Danh Sách IP Đã Sử Dụng | Lượt Tải | Lần Đầu | Lần Cuối | Phân Quyền |",
        "| :---: | :--- | :--- | :---: | :--- | :---: | :---: | :---: | :---: |"
    ]

    if not reg:
        lines.append("| - | *Chưa có dữ liệu* | - | - | - | - | - | - | - |")
    else:
        for idx, (phone, u) in enumerate(sorted(reg.items(), key=lambda x: x[1].get('last_seen', ''), reverse=True), 1):
            ips_str = "<br>".join([f"<code>{ip}</code>" for ip in u.get("ips", [])]) or "<i>Chưa rõ</i>"
            name = u.get("name") or "Khách hàng"
            tg_id = f"<code>{u.get('telegram_id')}</code>" if u.get("telegram_id") else "-"
            dl = u.get("download_count", 0)
            f_seen = u.get("first_seen", "")
            l_seen = u.get("last_seen", "")
            role = "**VIP** ⭐" if u.get("is_vip") else "Thường"
            lines.append(f"| {idx} | `{phone}` | {name} | {tg_id} | {ips_str} | {dl} | {f_seen} | {l_seen} | {role} |")

    lines.extend([
        "",
        "---",
        "",
        "## 👤 Người Dùng Chưa Đăng Ký (Quản lý & Giám sát theo IP)",
        "> *Danh sách người dùng vãng lai chưa để lại số điện thoại, hệ thống nhận diện và giới hạn quota theo IP.*",
        "",
        "| STT | Địa Chỉ IP | Thiết Bị / User Agent | Số Lượt Đã Tải | Lần Đầu Truy Cập | Lần Cuối Truy Cập |",
        "| :---: | :--- | :--- | :---: | :---: | :---: |"
    ])

    if not unreg:
        lines.append("| - | *Chưa có dữ liệu* | - | - | - | - |")
    else:
        for idx, (ip, u) in enumerate(sorted(unreg.items(), key=lambda x: x[1].get('download_count', 0), reverse=True), 1):
            ua = u.get("user_agent", "Trình duyệt")
            dl = u.get("download_count", 0)
            f_seen = u.get("first_seen", "")
            l_seen = u.get("last_seen", "")
            lines.append(f"| {idx} | `{ip}` | {ua} | **{dl}** | {f_seen} | {l_seen} |")

    lines.extend([
        "",
        "---",
        "*Bản quyền © 2026 TAIO Studio • Hệ thống phân tích định danh nội bộ.*"
    ])

    return "\n".join(lines)

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
# GIAO DIỆN WEB & STATIC ASSETS (ĐỘC LẬP THEO MODULE)
# ==========================================
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
INDEX_TEMPLATE_PATH = os.path.join(TEMPLATES_DIR, "index.html")

def load_html_template() -> str:
    """Tải mã nguồn giao diện HTML từ file template độc lập."""
    try:
        with open(INDEX_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Lỗi đọc template HTML: {e}")
        return "<h1>500 - Lỗi không tìm thấy file giao diện templates/index.html</h1>"

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

            # Ghi nhận truy cập vào hồ sơ định danh
            record_user_activity(client_ip=client_ip, user_agent=self.headers.get('User-Agent', ''), is_vip=is_admin)

            # Lấy username bot thật nếu có để tạo link mở bot chuẩn xác
            bot_username = "Clipbot"
            try:
                me = bot.get_me()
                if me and me.username:
                    bot_username = me.username
            except Exception:
                pass

            html_rendered = load_html_template().replace("__ZALO_GROUP_URL__", ZALO_GROUP_URL)
            html_rendered = html_rendered.replace("__TELEGRAM_GROUP_URL__", TELEGRAM_GROUP_URL)
            html_rendered = html_rendered.replace("__TELEGRAM_BOT_URL__", f"https://t.me/{bot_username}")
            html_rendered = html_rendered.replace("__IS_ADMIN_VIP__", "true" if is_admin else "false")
            html_rendered = html_rendered.replace("__CLIENT_IP__", client_ip)
            html_rendered = html_rendered.replace("__ADMIN_TOKEN__", ADMIN_TOKEN if is_admin else "")

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if is_admin:
                self.send_header("Set-Cookie", f"admin_token={ADMIN_TOKEN}; Path=/; Max-Age=31536000; SameSite=Lax")
            self.end_headers()
            self.wfile.write(html_rendered.encode('utf-8'))

        elif path == "/connect":
            client_ip = self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()
            user_agent = self.headers.get('User-Agent', '')
            query_params = urllib.parse.parse_qs(parsed.query)
            t_uid = query_params.get('t_uid', [''])[0].strip()

            phone_found = ""
            name_found = ""
            wl = load_whitelist()
            if t_uid and t_uid in wl:
                phone_found = wl[t_uid].get('phone', '')
                name_found = wl[t_uid].get('name', '')

            if not phone_found and os.path.exists(LEADS_FILE):
                try:
                    with open(LEADS_FILE, "r", encoding="utf-8") as f:
                        for ld in json.load(f):
                            if str(ld.get("extra", {}).get("user_id")) == t_uid:
                                phone_found = ld.get("phone", "")
                                name_found = ld.get("name", "")
                                break
                except Exception:
                    pass

            record_user_activity(
                client_ip=client_ip,
                phone=phone_found,
                name=name_found,
                telegram_id=t_uid,
                user_agent=user_agent
            )

            # Chuyển hướng người dùng về trang chủ
            self.send_response(302)
            self.send_header("Location", "/")
            if t_uid:
                self.send_header("Set-Cookie", f"taio_tg_uid={t_uid}; Path=/; Max-Age=31536000")
            self.end_headers()

        elif path in ("/report.md", "/analytics.md", "/data.md"):
            md_content = generate_markdown_report()
            md_bytes = md_content.encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(md_bytes)))
            self.end_headers()
            self.wfile.write(md_bytes)

        elif path in ("/report", "/analytics"):
            md_content = generate_markdown_report()
            html_report = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Báo Cáo Định Danh - TAIO Studio</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
.prose table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; margin-bottom: 1.5rem; }}
.prose th, .prose td {{ border: 1px solid #e2e8f0; padding: 8px 12px; font-size: 13px; }}
.prose th {{ background: #f8fafc; font-weight: 700; color: #1e293b; text-align: left; }}
.prose tr:nth-child(even) {{ background: #fdfdfe; }}
.prose tr:hover {{ background: #f1f5f9; }}
.prose code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 12px; color: #0f172a; }}
.prose h1 {{ font-size: 1.6rem; font-weight: 800; color: #0f172a; margin-bottom: 0.5rem; }}
.prose h2 {{ font-size: 1.25rem; font-weight: 700; color: #1e293b; margin-top: 1.5rem; margin-bottom: 0.5rem; border-bottom: 2px solid #e2e8f0; padding-bottom: 4px; }}
.prose blockquote {{ border-left: 4px solid #3b82f6; padding-left: 12px; font-style: italic; color: #64748b; margin: 8px 0; }}
</style>
</head>
<body class="bg-slate-50 text-slate-800 p-4 sm:p-8">
  <div class="max-w-5xl mx-auto bg-white rounded-2xl border border-slate-200 p-6 sm:p-10 shadow-sm">
    <div class="flex items-center justify-between pb-4 border-b border-slate-200 mb-6">
      <div class="flex items-center gap-2">
        <span class="w-3 h-3 rounded-full bg-emerald-500 animate-pulse"></span>
        <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Hệ Thống Phân Tích Định Danh</span>
      </div>
      <a href="/report.md" download="taio_user_report.md" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white font-semibold text-xs shadow-xs transition">
        <i class="fa-solid fa-file-arrow-down"></i> Tải file .md
      </a>
    </div>
    <div id="content" class="prose max-w-none"></div>
  </div>
  <script>
    const rawMd = {json.dumps(md_content)};
    document.getElementById('content').innerHTML = marked.parse(rawMd);
  </script>
</body>
</html>"""
            html_bytes = html_report.encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)

        elif path in ("/extension.zip", "/download-extension", "/extension"):
            import zipfile
            import io
            ext_dir = os.path.join(os.path.dirname(__file__), "extension")
            zip_buffer = io.BytesIO()
            if os.path.exists(ext_dir):
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                    for root, dirs, files in os.walk(ext_dir):
                        for file in files:
                            if file.endswith('.DS_Store'): continue
                            file_path = os.path.join(root, file)
                            rel_path = os.path.relpath(file_path, ext_dir)
                            zip_file.write(file_path, rel_path)
            zip_bytes = zip_buffer.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="taio-cookie-exporter.zip"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)

        elif path.startswith("/api/file/") or path.startswith("/downloads/"):
            prefix = "/api/file/" if path.startswith("/api/file/") else "/downloads/"
            filename = urllib.parse.unquote(path[len(prefix):])
            filepath = os.path.join(DOWNLOAD_DIR, filename)

            if not os.path.commonpath([DOWNLOAD_DIR, filepath]) == DOWNLOAD_DIR or not os.path.exists(filepath):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"File not found")
                return

            file_size = os.path.getsize(filepath)
            content_type, _ = mimetypes.guess_type(filepath)
            if not content_type:
                if filepath.endswith('.mp4'):
                    content_type = "video/mp4"
                elif filepath.endswith('.mp3'):
                    content_type = "audio/mpeg"
                else:
                    content_type = "application/octet-stream"

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

        elif path == "/manifest.json":
            manifest_file = os.path.join(WEB_DIR, "manifest.json")
            if os.path.exists(manifest_file):
                with open(manifest_file, 'rb') as f:
                    data = f.read()
            else:
                data = b'{"name":"TAIO Studio","short_name":"TAIO","start_url":"/","display":"standalone"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        elif path in ("/api/app-icon.svg", "/api/app-icon.png"):
            icon_file = os.path.join(WEB_DIR, "app-icon.svg")
            if os.path.exists(icon_file):
                with open(icon_file, 'rb') as f:
                    data = f.read()
            else:
                data = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" fill="#2563eb"/></svg>'
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)

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
            client_ip = self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode('utf-8'))
                phone = data.get("phone", "").strip()
                name = data.get("name", "").strip() or "Khách Web"
                email = data.get("email", "").strip()
                source = data.get("source", "Web Unlock (Lead Gate)")

                if not phone:
                    raise ValueError("Số điện thoại không được để trống!")

                save_lead(name=name, phone=phone, email=email, source=source, extra={"ip": client_ip})
                record_user_activity(client_ip=client_ip, phone=phone, name=name, user_agent=self.headers.get('User-Agent', ''))

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "bonus": 10}).encode('utf-8'))
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

                # Trích xuất file âm thanh MP3 (Tính năng dành riêng cho Web)
                audio_filename = ""
                try:
                    audio_filepath = downloader.get_audio_for_download(saved_filepath, url, output_dir=DOWNLOAD_DIR)
                    if audio_filepath and os.path.exists(audio_filepath):
                        audio_filename = os.path.basename(audio_filepath)
                except Exception as e:
                    logger.warning(f"Audio extraction skipped or failed: {e}")

                response_data = {
                    "success": True,
                    "filename": filename,
                    "audio_filename": audio_filename,
                    "title": meta.get('title', filename),
                    "thumbnail": meta.get('thumbnail', ''),
                    "uploader": meta.get('uploader', '')
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(response_data).encode('utf-8'))

                # Ghi nhận lượt tải thành công vào hồ sơ định danh
                record_user_activity(
                    client_ip=client_ip,
                    user_agent=self.headers.get('User-Agent', ''),
                    download_success=True,
                    is_vip=is_admin
                )

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

# --- CÁC LỆNH DÀNH RIÊNG CHO ADMIN (ẨN HOÀN TOÀN KHỎI PUBLIC) ---
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
            f"👉 Sử dụng /adminweb để tạo link Web VIP tự động nạp IP.",
            parse_mode="HTML"
        )
    else:
        # Giữ bí mật tuyệt đối, không phản hồi token hoặc cú pháp
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")

@bot.message_handler(commands=['myid', 'id'])
def handle_myid(message):
    uid = message.from_user.id
    cid = message.chat.id
    is_adm = is_admin_user(uid, cid)
    wl = load_whitelist()
    is_vip = is_adm or (str(uid) in wl)
    role_str = "👑 Quản trị viên" if is_adm else ("⭐ Thành viên VIP (Đã kích hoạt)" if is_vip else "👤 Thành viên thông thường")

    bot.reply_to(
        message,
        f"🆔 <b>THÔNG TIN TÀI KHOẢN CỦA BẠN:</b>\n\n"
        f"• <b>User ID:</b> <code>{uid}</code>\n"
        f"• <b>Tên:</b> {message.from_user.first_name}\n"
        f"• <b>Username:</b> @{message.from_user.username or 'Không có'}\n"
        f"• <b>Trạng thái:</b> {role_str}\n\n"
        f"💬 Tham gia nhóm Zalo hỗ trợ: <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )

@bot.message_handler(func=lambda msg: (msg.text or '').startswith('/duyet_'))
def handle_duyet(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
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
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
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
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
        return

    wl = load_whitelist()
    lines = [f"📋 <b>DANH SÁCH THÀNH VIÊN VIP ({len(wl)}):</b>\n"]
    for uid, info in list(wl.items())[-20:]:
        lines.append(f"• <code>{uid}</code> (Duyệt: {info.get('activated_at', 'N/A')})")

    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

@bot.message_handler(commands=['leads'])
def handle_leads(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
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

@bot.message_handler(commands=['report', 'baocao', 'md'])
def handle_report_command(message):
    user_id = message.from_user.id
    if not is_admin_user(user_id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh này chỉ dành cho Super Admin.")
        return

    profiles = load_user_profiles()
    reg = profiles.get("registered", {})
    unreg = profiles.get("unregistered", {})

    report_url = f"https://{CUSTOM_DOMAIN}/report"
    raw_md_url = f"https://{CUSTOM_DOMAIN}/report.md"

    summary_text = (
        f"📊 <b>BÁO CÁO ĐỊNH DANH NGƯỜI DÙNG TAIO:</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Người dùng đã đăng ký (SĐT):</b> {len(reg)}\n"
        f"👥 <b>Người dùng vãng lai (theo IP):</b> {len(unreg)}\n\n"
        f"👉 <b>Xem báo cáo trực quan (HTML):</b>\n<a href='{report_url}'>{report_url}</a>\n\n"
        f"📥 <b>Xem file Markdown gốc (.md):</b>\n<a href='{raw_md_url}'>{raw_md_url}</a>"
    )
    bot.reply_to(message, summary_text, parse_mode="HTML", disable_web_page_preview=True)

@bot.message_handler(commands=['sync'])
def handle_sync_toggle(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
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

# --- QUẢN LÝ COOKIES YOUTUBE CHO ADMIN ---
@bot.message_handler(commands=['cookies'])
def handle_cookies_check(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
        return

    cookie_file = os.path.join(os.path.dirname(__file__), "cookies.txt")
    has_file = os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 10
    has_env = bool(os.getenv("YOUTUBE_COOKIES"))

    if has_file or has_env:
        status_text = (
            f"🍪 <b>Trạng thái Cookies YouTube:</b> ĐANG HOẠT ĐỘNG ✅\n\n"
            f"• <b>File cookies.txt:</b> {'Có (' + str(os.path.getsize(cookie_file)) + ' bytes)' if has_file else 'Không có'}\n"
            f"• <b>Biến môi trường YOUTUBE_COOKIES:</b> {'Đã thiết lập' if has_env else 'Chưa có'}\n\n"
            f"💡 <b>Mẹo:</b> Để làm mới hoặc thay thế cookies, bạn chỉ cần <b>gửi trực tiếp file <code>cookies.txt</code> vào khung chat này</b>, bot sẽ tự lưu ngay lập tức!"
        )
    else:
        status_text = (
            f"🍪 <b>Trạng thái Cookies YouTube:</b> CHƯA CÓ ⚠️\n\n"
            f"Do bot chạy trên Cloud (Render), YouTube chặn 100% các request không có cookies.\n\n"
            f"👉 <b>Cách nạp siêu nhanh:</b>\n"
            f"1. Dùng Chrome/Edge cài extension <b>Get cookies.txt LOCALLY</b>.\n"
            f"2. Đăng nhập YouTube và xuất file <code>cookies.txt</code>.\n"
            f"3. <b>Kéo thả và gửi trực tiếp file <code>cookies.txt</code> đó vào khung chat bot này!</b> Bot sẽ tự động nạp và kích hoạt ngay."
        )
    bot.reply_to(message, status_text, parse_mode="HTML")

@bot.message_handler(content_types=['document'])
def handle_admin_upload_document(message):
    if not is_admin_user(message.from_user.id, message.chat.id):
        return

    doc = message.document
    fname = (doc.file_name or "").lower()
    if fname.endswith(".txt") or "cookie" in fname:
        status_msg = bot.reply_to(message, "⏳ Đang tiếp nhận và cập nhật file Cookies...")
        try:
            file_info = bot.get_file(doc.file_id)
            file_bytes = bot.download_file(file_info.file_path)
            
            cookie_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
            with open(cookie_path, 'wb') as f:
                f.write(file_bytes)

            file_size = len(file_bytes)
            bot.edit_message_text(
                f"🍪 <b>CẬP NHẬT COOKIES THÀNH CÔNG!</b> ✅\n\n"
                f"• Tên file: <code>{doc.file_name}</code>\n"
                f"• Dung lượng: <b>{file_size} bytes</b>\n"
                f"• Vị trí lưu: <code>/app/cookies.txt</code>\n\n"
                f"🎉 Bot đã sẵn sàng tải mọi video và Shorts từ YouTube! Hãy gửi link để thử ngay.",
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                parse_mode="HTML"
            )
        except Exception as e:
            bot.edit_message_text(
                f"❌ Lỗi khi tải và lưu file cookies: {e}",
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )

@bot.message_handler(commands=['extension', 'getextension'])
def handle_extension_download_command(message):
    ext_dir = os.path.join(os.path.dirname(__file__), "extension")
    if not os.path.exists(ext_dir):
        bot.reply_to(message, "⚠️ Thư mục extension chưa được khởi tạo.")
        return

    import zipfile
    import io
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for root, dirs, files in os.walk(ext_dir):
            for file in files:
                if file.endswith('.DS_Store'): continue
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, ext_dir)
                zip_file.write(file_path, rel_path)
    zip_bytes = zip_buffer.getvalue()
    zip_file_obj = io.BytesIO(zip_bytes)
    zip_file_obj.name = "taio-cookie-exporter.zip"

    caption = (
        "🍪 <b>TIỆN ÍCH TAIO COOKIE EXPORTER (v1.0)</b>\n\n"
        "Tiện ích tự viết riêng để xuất Cookies chuẩn Netscape cho YouTube, Facebook, TikTok...\n"
        "• An toàn & Riêng tư 100% (chạy offline trên máy của bạn).\n"
        "• Dùng để xuất file <code>cookies.txt</code> và nạp cho Bot YouTube.\n\n"
        "👉 <b>Cách cài đặt lên Chrome / Edge:</b>\n"
        "1. Tải file zip này về máy tính và giải nén ra 1 thư mục.\n"
        "2. Mở Chrome -> vào <code>chrome://extensions/</code> -> Bật <i>Developer mode</i>.\n"
        "3. Bấm <i>Load unpacked</i> -> chọn thư mục vừa giải nén.\n\n"
        "🌐 Hoặc tải trực tiếp từ Web: <code>/download-extension</code>"
    )
    bot.send_document(
        message.chat.id,
        document=zip_file_obj,
        caption=caption,
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
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
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
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
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
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
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
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
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
        bot.reply_to(message, "⛔ Lệnh không hợp lệ.")
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
    record_user_activity(phone=phone, name=name, telegram_id=user_id, is_vip=True)

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
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        btn_guide = telebot.types.KeyboardButton("❓ Hướng dẫn sử dụng")
        btn_zalo = telebot.types.KeyboardButton("💬 Vào Nhóm Zalo Hỗ Trợ")
        btn_web = telebot.types.KeyboardButton("🌐 Mở Web Tải Video")
        markup.row(btn_guide)
        markup.row(btn_zalo, btn_web)

        welcome_text = (
            "👋 Chào bạn! Tôi là <b>Bot Tải Video Đa Nền Tảng</b> của TAIO.Club.\n\n"
            "✨ <b>Hỗ trợ tải chất lượng cao:</b>\n"
            "• ⚫ <b>TikTok</b>: Video & Album ảnh chất lượng gốc\n"
            "• 🔵 <b>Facebook Reels</b>: Video HD/FullHD sắc nét\n"
            "• 🧵 <b>Threads</b>: Video Full HD sắc nét\n"
            "• 🔴 <b>YouTube</b>: Hỗ trợ video dài & Shorts\n\n"
            "👉 <b>Cách sử dụng:</b> Bạn chỉ cần dán đường link video vào đây, Bot sẽ tự động tải và gửi lại file cho bạn ngay lập tức!\n\n"
            f"🌐 <i>Trải nghiệm phiên bản Web:</i> <a href='https://{CUSTOM_DOMAIN}'>https://{CUSTOM_DOMAIN}</a>\n"
            f"💬 <i>Nhóm Zalo hỗ trợ:</i> <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>"
        )
        if is_admin:
            welcome_text += (
                f"\n\n👑 <b>Menu Quản trị viên:</b>\n"
                "/adminweb : Link mở Web VIP 1 chạm (tự động nạp IP điện thoại)\n"
                "/report : Xem báo cáo định danh người dùng (.md)\n"
                "/myips : Xem danh sách IP Admin/VIP\n"
                "/addip : Thêm IP Admin thủ công\n"
                "/leads : Xem khách hàng mới\n"
                "/danhsach : Xem danh sách VIP\n"
                "/sync : Bật/Tắt tự động nhận video"
            )
        bot.reply_to(message, welcome_text, parse_mode='HTML', reply_markup=markup, disable_web_page_preview=True)
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
        bot.reply_to(message, f"👉 Tham gia nhóm Zalo hỗ trợ cộng đồng tại: <a href='{ZALO_GROUP_URL}'>{ZALO_GROUP_URL}</a>", parse_mode="HTML", disable_web_page_preview=True)
        return

    if text == "🌐 Mở Web Tải Video":
        connect_url = f"https://{CUSTOM_DOMAIN}/connect?t_uid={user_id}"
        bot.reply_to(message, f"🌐 Truy cập ngay phiên bản Web Studio tại: <a href='{connect_url}'>{connect_url}</a>", parse_mode="HTML", disable_web_page_preview=True)
        return

    if text == "❓ Hướng dẫn sử dụng":
        guide_text = (
            "📖 <b>HƯỚNG DẪN TẢI VIDEO 3 BƯỚC ĐƠN GIẢN:</b>\n\n"
            "1️⃣ Mở ứng dụng <b>TikTok</b>, <b>Facebook</b> hoặc <b>YouTube</b>.\n"
            "2️⃣ Bấm nút <b>Chia sẻ</b> ➔ Chọn <b>Sao chép liên kết</b>.\n"
            "3️⃣ Dán link trực tiếp vào khung chat này.\n\n"
            "⚡ Bot sẽ gửi lại file video hoàn chỉnh chất lượng gốc về máy cho bạn!\n\n"
            f"💬 <i>Hỗ trợ giải đáp thắc mắc:</i> <a href='{ZALO_GROUP_URL}'>Nhóm Zalo TAIO</a>"
        )
        bot.reply_to(message, guide_text, parse_mode="HTML", disable_web_page_preview=True)
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
        record_user_activity(phone=clean_digits, name=name, telegram_id=user_id, is_vip=True)

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
            "💡 <i>Mẹo: Bạn vẫn có thể tải hoàn toàn <b>MIỄN PHÍ & KHÔNG GIỚI HẠN</b> video từ <b>TikTok</b> và <b>Facebook Reels</b>!</i>",
            parse_mode="HTML",
            reply_markup=vip_markup,
            disable_web_page_preview=True
        )
        return

    platform = "Video"
    if "tiktok.com" in text.lower():
        platform = "TikTok"
    elif "facebook.com" in text.lower() or "fb.watch" in text.lower():
        platform = "Facebook Reel"
    elif "threads.net" in text.lower() or "threads.com" in text.lower():
        platform = "Threads"
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
        err_str = str(e)
        if "Sign in to confirm you're not a bot" in err_str:
            error_text = (
                f"🛡️ <b>YouTube yêu cầu xác thực Cookies máy chủ!</b>\n\n"
                f"Do bot chạy trên Cloud (Render), YouTube chặn tải ẩn danh.\n\n"
                f"👉 <b>Cách mở khóa (Dành cho Admin):</b>\n"
                f"1. Xuất file <code>cookies.txt</code> từ YouTube (dùng extension <i>Get cookies.txt LOCALLY</i> trên máy tính).\n"
                f"2. <b>Gửi trực tiếp file <code>cookies.txt</code> đó vào khung chat bot này!</b> (Bot sẽ tự nhận và lưu lại ngay)."
            )
        else:
            error_text = (
                f"❌ <b>Có lỗi xảy ra:</b>\n<code>{err_str}</code>\n\n"
                f"💡 Mẹo: Hãy thử gửi lại link hoặc kiểm tra video có đặt ở chế độ riêng tư không nhé."
            )
        safe_edit(chat_id, msg_id, error_text)
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
