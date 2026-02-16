FROM python:3.11-slim

# ffmpeg + curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Railway จะส่ง PORT มาให้
CMD ["bash","-lc","uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
