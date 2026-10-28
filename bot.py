import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
import downloader

# Khởi tạo executor cho tác vụ tải ngầm
executor = ThreadPoolExecutor(max_workers=3)

# Cổng kiểm tra máy chủ Cloud (Health check cho Render / Hugging Face)
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Telegram Video Downloader Bot is running online 24/7!")

    def log_message(self, format, *args):
        pass

def start_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# Lấy token từ biến môi trường, file .env hoặc tham số dòng lệnh
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

DOWNLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def safe_edit(chat_id, message_id, text):
    """Cập nhật nội dung tin nhắn an toàn, bỏ qua lỗi nếu người dùng đã xóa tin"""
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode='HTML')
    except Exception:
        pass

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

    # Xác định nền tảng để hiển thị icon sinh động
    platform = "Video"
    if "tiktok.com" in text.lower():
        platform = "TikTok (Không logo)"
    elif "facebook.com" in text.lower() or "fb.watch" in text.lower():
        platform = "Facebook Reel"
    elif "youtube.com" in text.lower() or "youtu.be" in text.lower():
        platform = "YouTube"

    # Tin nhắn trạng thái ban đầu
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
        # 1. Đưa tác vụ tải vào ThreadPool chạy ngầm kèm Watchdog giám sát tiến trình
        future = executor.submit(downloader.download_video, text, DOWNLOAD_DIR)
        
        start_time = time.time()
        timeout_seconds = 75  # Giới hạn tối đa 75 giây
        is_done = False

        # Vòng lặp cập nhật thanh tiến trình động theo thời gian thực
        while not is_done:
            time.sleep(2.5)
            elapsed = int(time.time() - start_time)

            if future.done():
                is_done = True
                break

            if elapsed > timeout_seconds:
                safe_edit(
                    chat_id, msg_id,
                    "⏱️ <b>Hết thời gian xử lý (Timeout sau 75 giây)!</b>\n\n"
                    "Máy chủ nguồn phản hồi quá chậm hoặc link video bị giới hạn riêng tư. Vui lòng kiểm tra lại link hoặc thử lại sau ít phút!"
                )
                return

            # Cập nhật thanh tiến trình sinh động
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

        # Lấy kết quả tải
        saved_file = future.result()

        if not saved_file or not os.path.exists(saved_file):
            raise RuntimeError("Không tìm thấy file video sau khi hoàn tất tải.")

        # 2. Kiểm tra dung lượng file
        file_size_mb = os.path.getsize(saved_file) / (1024 * 1024)
        if file_size_mb > 50:
            safe_edit(
                chat_id, msg_id,
                f"⚠️ Video tải về thành công nhưng có dung lượng <b>{file_size_mb:.1f} MB</b>, "
                f"vượt quá giới hạn gửi file 50 MB của Telegram Bot API."
            )
            return

        # 3. Tiến hành gửi video về Telegram
        safe_edit(
            chat_id, msg_id,
            f"📤 <b>[3/3]</b> Đang gửi file lên Telegram (<b>{file_size_mb:.1f} MB</b>)...\n"
            f"<code>[██████████] 100%</code>"
        )
        bot.send_chat_action(chat_id, 'upload_video')

        # Gửi video hoặc tài liệu
        if saved_file.endswith('.mp4'):
            with open(saved_file, 'rb') as f:
                bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=f"✅ <b>Tải thành công!</b> ({file_size_mb:.1f} MB)\nNguồn: {platform}",
                    parse_mode='HTML',
                    supports_streaming=True,
                    timeout=90
                )
        else:
            with open(saved_file, 'rb') as f:
                bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=f"✅ <b>Tải thành công!</b> ({file_size_mb:.1f} MB)",
                    parse_mode='HTML',
                    timeout=90
                )

        # Xóa tin nhắn thanh tiến trình sau khi video đã gửi thành công
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
        # Dọn dẹp file tạm để không chiếm bộ nhớ Cloud
        if saved_file and os.path.exists(saved_file):
            try:
                os.remove(saved_file)
            except Exception:
                pass

if __name__ == '__main__':
    # Bật cổng web kiểm tra trạng thái máy chủ (cần thiết cho Render)
    threading.Thread(target=start_health_check_server, daemon=True).start()
    port = os.getenv("PORT", "8080")
    print("=" * 60)
    print(" 🤖 TELEGRAM BOT ĐANG CHẠY (Bản v2 - Kèm Thanh Tiến Trình & Timeout)...")
    print(f" 🌐 Cổng Cloud Web Health-Check đang mở tại PORT: {port}")
    print("=" * 60)
    bot.infinity_polling()
