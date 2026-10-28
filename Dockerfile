FROM python:3.11-slim

# Cài đặt ffmpeg cho xử lý video và chứng chỉ bảo mật CA
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài đặt các thư viện Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sao chép mã nguồn vào container
COPY . .

# Khởi động Telegram Bot
CMD ["python", "bot.py"]
