FROM python:3.11-slim

# ffmpeg + curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# ช่วยให้ Railway auto-detect พอร์ตได้ดีขึ้น
ENV PORT=8080
EXPOSE 8080

# สำคัญ: ใช้ sh -c + bind ตาม $PORT เท่านั้น + ใส่ proxy headers
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
