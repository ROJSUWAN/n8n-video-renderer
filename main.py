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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Optional: GCS upload
try:
    from google.cloud import storage  # type: ignore
except Exception:
    storage = None

# -----------------------------
# Config
# -----------------------------
APP_NAME = "n8n-video-renderer-v2"

DEFAULT_FPS = int(os.getenv("VIDEO_FPS", "30"))
DEFAULT_WIDTH = int(os.getenv("VIDEO_WIDTH", "1080"))
DEFAULT_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "1920"))

# GCS Config จาก Railway Variables
GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
GCS_PREFIX = os.getenv("GCS_PREFIX", "renders/").strip()
GCS_PUBLIC = os.getenv("GCS_PUBLIC", "false").lower() in ("1", "true", "yes")
GCP_SA_JSON = os.getenv("GCP_SA_JSON", "").strip()

PORT = int(os.environ.get("PORT", "8080"))

app = FastAPI(title=APP_NAME)

# -----------------------------
# Models
# -----------------------------
class SceneItem(BaseModel):
    scene_number: int = Field(..., ge=1)
    script: str
    image_base64: str

class RenderRequest(BaseModel):
    stock_symbol: str = "UNKNOWN"
    trade_setup: dict = {}
    data: List[SceneItem]
    output_name: Optional[str] = None
    return_file: Optional[bool] = False

# -----------------------------
# Helpers & Processing
# -----------------------------
def _run(cmd: List[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg Error: {proc.stderr}")

def _save_base64_image(b64_str: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(b64_str))

async def _generate_audio(text: str, out_path: Path, voice: str = "th-TH-PremwadeeNeural") -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))

def _build_scene(image_in: Path, audio_in: Path, out_path: Path) -> None:
    vf = f"scale={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:force_original_aspect_ratio=decrease,pad={DEFAULT_WIDTH}:{DEFAULT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={DEFAULT_FPS}"
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-framerate", str(DEFAULT_FPS),
        "-i", str(image_in), "-i", str(audio_in),
        "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-shortest", str(out_path)
    ]
    _run(cmd)

def _concat_videos(scene_paths: List[Path], out_path: Path) -> None:
    list_file = out_path.parent / "concat_list.txt"
    content = "\n".join([f"file '{str(p.absolute())}'" for p in scene_paths])
    list_file.write_text(content)
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out_path)]
    _run(cmd)

# -----------------------------
# GCS Upload Logic
# -----------------------------
def _upload_to_gcs(local_path: Path, filename: str) -> str:
    if not storage or not GCS_BUCKET:
        return "GCS_NOT_CONFIGURED"
    
    try:
        if GCP_SA_JSON:
            client = storage.Client.from_service_account_info(json.loads(GCP_SA_JSON))
        else:
            client = storage.Client()
            
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(f"{GCS_PREFIX}{filename}")
        blob.upload_from_filename(str(local_path))
        
        if GCS_PUBLIC:
            blob.make_public()
            return blob.public_url
        return blob.generate_signed_url(expiration=3600)
    except Exception as e:
        print(f"GCS Upload Error: {e}")
        return f"UPLOAD_ERROR: {str(e)}"

# -----------------------------
# Core Background Processor
# -----------------------------
async def processing_task(req: RenderRequest):
    workdir = Path(tempfile.mkdtemp(prefix="render_"))
    try:
        assets_dir = workdir / "assets"
        scenes_dir = workdir / "scenes"
        assets_dir.mkdir(parents=True); scenes_dir.mkdir(parents=True)

        scene_paths = []
        scenes = sorted(req.data, key=lambda s: s.scene_number)

        for s in scenes:
            img = assets_dir / f"{s.scene_number}.png"
            aud = assets_dir / f"{s.scene_number}.mp3"
            scp = scenes_dir / f"{s.scene_number}.mp4"
            
            _save_base64_image(s.image_base64, img)
            await _generate_audio(s.script, aud)
            _build_scene(img, aud, scp)
            scene_paths.append(scp)

        filename = f"{req.stock_symbol}_{uuid.uuid4().hex[:6]}.mp4"
        final_video = workdir / filename
        _concat_videos(scene_paths, final_video)

        # อัปโหลดขึ้น Google Cloud
        url = _upload_to_gcs(final_video, filename)
        print(f"DONE: {req.stock_symbol} -> {url}")
        
        # ตรงนี้คุณสามารถเพิ่มคำสั่งส่ง LINE Notify เพื่อบอกตัวเองว่าเสร็จแล้วได้ครับ
        
    except Exception as e:
        print(f"Background Process Error for {req.stock_symbol}: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

# -----------------------------
# Main Route
# -----------------------------
@app.post("/render")
async def render(req: RenderRequest, background_tasks: BackgroundTasks):
    if not req.data:
        raise HTTPException(status_code=400, detail="Data is empty")

    # สั่งให้ทำงานเบื้องหลังทันที
    background_tasks.add_task(processing_task, req)

    # ตอบกลับ n8n ทันทีว่าได้รับงานแล้ว
    return {
        "ok": True,
        "message": f"Rendering {req.stock_symbol} in background. Check your GCS Bucket in 3-5 mins.",
        "bucket": GCS_BUCKET
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)