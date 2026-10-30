import os
import sys
import gradio as gr
import downloader

DOWNLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def process_download(url, progress=gr.Progress()):
    if not url or not url.strip():
        return None, None, "⚠️ Vui lòng nhập đường link video!"

    url = url.strip()
    progress(0.2, desc="🔍 Đang phân tích link video...")
    
    try:
        progress(0.5, desc="📥 Đang tải và chuẩn hóa video...")
        saved_file = downloader.download_video(url, output_dir=DOWNLOAD_DIR)
        
        progress(0.9, desc="✨ Đang hoàn tất chuẩn bị file...")
        if not saved_file or not os.path.exists(saved_file):
            raise RuntimeError("Không tìm thấy file sau khi tải xong.")
            
        file_size_mb = os.path.getsize(saved_file) / (1024 * 1024)
        status_text = (
            f"✅ <b>Tải thành công!</b> ({file_size_mb:.1f} MB)<br>"
            f"<i>Video đã được tự động chuẩn hóa tương thích 100% cho iPhone (Photos) & Android.</i>"
        )
        
        # Nếu là file zip (album ảnh)
        if saved_file.endswith(".zip"):
            return None, saved_file, status_text + "<br>📦 <b>Đã đóng gói thành file ZIP chứa toàn bộ ảnh gốc.</b>"
        
        return saved_file, saved_file, status_text

    except Exception as e:
        return None, None, f"❌ <b>Có lỗi xảy ra:</b> {str(e)}"

# Xây dựng giao diện Web hiện đại bằng Gradio
with gr.Blocks(theme=gr.themes.Soft(primary_hue="indigo"), title="Video Downloader") as demo:
    gr.Markdown(
        """
        # 🚀 Universal Video Downloader
        ### Tải video chất lượng cao không logo từ **YouTube**, **Facebook Reels** và **TikTok**.
        *Chạy trên máy chủ Cloud Hugging Face (16 GB RAM) - Tương thích 100% với iPhone / iOS & Android.*
        """
    )
    
    with gr.Row():
        with gr.Column(scale=4):
            url_input = gr.Textbox(
                placeholder="Dán link Facebook Reel, TikTok, hoặc YouTube vào đây và nhấn Enter...",
                label="Đường link Video",
                lines=1
            )
        with gr.Column(scale=1):
            download_btn = gr.Button("⬇️ Bắt đầu tải", variant="primary", size="lg")

    status_output = gr.Markdown()

    with gr.Row():
        video_preview = gr.Video(label="Xem trước Video (Preview)", interactive=False)
        file_download = gr.File(label="Bấm vào đây để tải file về máy (Direct Download)", interactive=False)

    download_btn.click(
        fn=process_download,
        inputs=[url_input],
        outputs=[video_preview, file_download, status_output]
    )
    url_input.submit(
        fn=process_download,
        inputs=[url_input],
        outputs=[video_preview, file_download, status_output]
    )

    gr.Markdown(
        """
        ---
        💡 **Mẹo trên điện thoại:** Sau khi tải xong, bạn có thể xem video trực tiếp ở khung bên trái hoặc bấm vào khung bên phải để lưu thẳng video vào Album Ảnh!
        """
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT", 7860)))
