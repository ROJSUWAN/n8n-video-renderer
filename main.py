import os
import re
import json
import uuid
import shutil
import base64
import asyncio
import tempfile
import subprocess
from pathlib import Path
from typing import List, Optional

import requests
import edge_tts
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

# -----------------------------
# Google Cloud Storage Setup
# -----------------------------
try:
    from google.cloud import storage
except ImportError:
    storage = None

# -----------------------------
# Config & Environment Variables
# -----------------------------
APP_NAME = "n8n-video-renderer-pro"
DEFAULT_FPS = int(os.getenv("VIDEO_FPS", "30"))
DEFAULT_WIDTH = int(os.getenv("VIDEO_WIDTH", "1080"))
DEFAULT_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "1920"))

GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
GCS_PREFIX = os.getenv("GCS_PREFIX", "renders/").strip()
GCS_PUBLIC = os.getenv("GCS_PUBLIC", "false").lower() in ("1", "true", "yes")
GCP_SA_JSON = os.getenv("GCP_SA_JSON", "").strip()

app = FastAPI(title=APP_NAME)

# -----------------------------
# Models (ข้อมูลที่รับจาก n8n)
# -----------------------------
class SceneItem(BaseModel):
    scene_number: int
    script: str
    image_base64: str

class RenderRequest(BaseModel):
    stock_symbol: str = "UNKNOWN"
    trade_setup: dict = {}
    data: List[SceneItem]

# -----------------------------
# Video Processing Functions
# -----------------------------
def _run_ffmpeg(cmd: List[str]):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg Error: {proc.stderr}")

async def render_video_task(req: RenderRequest):
    """ฟังก์ชันหลักที่ทำงานเบื้องหลัง (Background Task)"""
    workdir = Path(tempfile.mkdtemp(prefix="render_"))
    try:
        assets_dir = workdir / "assets"
        scenes_dir = workdir / "scenes"
        assets_dir.mkdir(parents=True); scenes_dir.mkdir(parents=True)

        scene_mp4s = []
        # เรียงลำดับฉาก 1-6
        scenes = sorted(req.data, key=lambda s: s.scene_number)

        for s in scenes:
            img_p = assets_dir / f"{s.scene_number}.png"
            aud_p = assets_dir / f"{s.scene_number}.mp3"
            scn_p = scenes_dir / f"{s.scene_number}.mp4"

            # 1. Save Image
            with open(img_p, "wb") as f:
                f.write(base64.b64decode(s.image_base64))

            # 2. Generate Audio (Thai Voice)
            tts = edge_tts.Communicate(s.script, "th-TH-PremwadeeNeural")
            await tts.save(str(aud_p))

            # 3. Build Scene Video
            vf = (f"scale={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:force_original_aspect_ratio=decrease,"
                  f"pad={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={DEFAULT_FPS}")
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-framerate", str(DEFAULT_FPS),
                "-i", str(img_p), "-i", str(aud_p),
                "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-shortest", str(scn_p)
            ]
            _run_ffmpeg(cmd)
            scene_mp4s.append(scn_p)

        # 4. Concat all scenes
        final_name = f"{req.stock_symbol}_{uuid.uuid4().hex[:6]}.mp4"
        final_path = workdir / final_name
        list_p = workdir / "list.txt"
        list_p.write_text("\n".join([f"file '{str(p.absolute())}'" for p in scene_mp4s]))
        
        _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_p), "-c", "copy", str(final_path)])

        # 5. Upload to Google Cloud Storage
        if storage and GCS_BUCKET:
            if GCP_SA_JSON:
                client = storage.Client.from_service_account_info(json.loads(GCP_SA_JSON))
            else:
                client = storage.Client()
            
            bucket = client.bucket(GCS_BUCKET)
            blob = bucket.blob(f"{GCS_PREFIX}{final_name}")
            blob.upload_from_filename(str(final_path))
            
            if GCS_PUBLIC:
                blob.make_public()
            print(f"Successfully uploaded: {final_name}")

    except Exception as e:
        print(f"Error rendering {req.stock_symbol}: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

# -----------------------------
# API Endpoints
# -----------------------------
@app.post("/render")
async def create_render_job(req: RenderRequest, background_tasks: BackgroundTasks):
    if not req.data:
        raise HTTPException(status_code=400, detail="No scene data provided")

    # สั่งให้เรนเดอร์เบื้องหลังทันที
    background_tasks.add_task(render_video_task, req)

    # ตอบกลับ n8n ทันที (ใช้เวลาไม่ถึง 1 วินาที)
    return {
        "status": "accepted",
        "message": f"Rendering job for {req.stock_symbol} started.",
        "bucket": GCS_BUCKET
    }

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))