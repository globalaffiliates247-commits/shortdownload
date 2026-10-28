import os
import sys
import telebot
import downloader

# Lấy token từ biến môi trường hoặc nhập trực tiếp khi chạy
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN:
    print("=" * 60)
    print(" [!] THIẾU TELEGRAM_BOT_TOKEN")
    print(" Bạn có thể chạy bot bằng lệnh:")
    print(" TELEGRAM_BOT_TOKEN='YOUR_BOT_TOKEN' python3 bot.py")
    print("=" * 60)
    if len(sys.argv) > 1:
        BOT_TOKEN = sys.argv[1].strip()
    else:
        sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

DOWNLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "👋 Chào bạn! Tôi là Bot tải Video tự động.\n\n"
        "✨ Hỗ trợ tải chất lượng cao không logo:\n"
        "• 🔴 YouTube / Shorts\n"
        "• 🔵 Facebook Reels / Video\n"
        "• ⚫ TikTok Video & Album ảnh\n\n"
        "👉 Chỉ cần gửi hoặc dán đường link video vào đây, tôi sẽ tải và gửi lại file cho bạn ngay lập tức!"
    )
    bot.reply_to(message, welcome_text)

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = (message.text or "").strip()
    
    # Kiểm tra xem có chứa đường link hay không
    if not (text.startswith("http://") or text.startswith("https://")):
        bot.reply_to(message, "⚠️ Vui lòng gửi một đường link video hợp lệ (bắt đầu bằng https://...)")
        return

    # Thông báo đang xử lý
    status_msg = bot.reply_to(message, "⏳ Đang trích xuất và tải video từ máy chủ, vui lòng đợi giây lát (khoảng 5 - 20s)...")
    
    saved_file = None
    try:
        # Tải video
        saved_file = downloader.download_video(text, output_dir=DOWNLOAD_DIR)
        
        # Kiểm tra dung lượng file (Telegram giới hạn gửi file tối đa 50MB qua Bot API)
        file_size_mb = os.path.getsize(saved_file) / (1024 * 1024)
        if file_size_mb > 50:
            bot.edit_message_text(
                f"⚠️ Video tải về có dung lượng {file_size_mb:.1f} MB, vượt quá giới hạn 50 MB của Telegram Bot API.",
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
            return

        bot.edit_message_text("📤 Đang gửi video về chat của bạn...", chat_id=message.chat.id, message_id=status_msg.message_id)

        # Gửi video hoặc tài liệu
        if saved_file.endswith('.mp4'):
            with open(saved_file, 'rb') as f:
                bot.send_video(
                    chat_id=message.chat.id,
                    video=f,
                    caption="✅ Video đã tải thành công không logo!",
                    supports_streaming=True
                )
        else:
            with open(saved_file, 'rb') as f:
                bot.send_document(
                    chat_id=message.chat.id,
                    document=f,
                    caption="✅ File đã tải thành công!"
                )

        # Xóa tin nhắn trạng thái chờ sau khi gửi xong
        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass

    except Exception as e:
        bot.edit_message_text(
            f"❌ Có lỗi xảy ra trong quá trình tải:\n{str(e)}",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )
    finally:
        # Dọn dẹp file tạm sau khi gửi xong để tránh đầy dung lượng máy chủ
        if saved_file and os.path.exists(saved_file):
            try:
                os.remove(saved_file)
            except Exception:
                pass

if __name__ == '__main__':
    print("=" * 60)
    print(" 🤖 TELEGRAM BOT ĐANG CHẠY...")
    print(" Gửi link vào Telegram bot của bạn để tải video!")
    print("=" * 60)
    bot.infinity_polling()
